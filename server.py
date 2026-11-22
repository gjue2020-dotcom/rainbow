#!/usr/bin/env python3
"""무지개 물고기 질문 정리 웹앱. Python 표준 라이브러리만 사용."""
from __future__ import annotations
import base64, csv, hashlib, hmac, io, json, os, re, secrets, sqlite3, sys, threading, time, urllib.error, urllib.request, zipfile
from datetime import datetime, timezone
from html import escape as html_escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

ROOT=Path(__file__).resolve().parent
WEB=ROOT/'web'
BUNDLED_DATA=ROOT/'data'
DATA=Path(os.getenv('DATA_DIR',str(ROOT/'runtime_data'))).resolve()
DATA.mkdir(parents=True,exist_ok=True)
PASSAGE=json.loads((BUNDLED_DATA/'passage.json').read_text(encoding='utf-8'))['passages']
DEFAULT_REFERENCE_TEXT='\n\n'.join(PASSAGE)
NUMS=[1]+list(range(3,18))
DB_PATH=DATA/'classroom.sqlite3'
PRIVATE_PATH=DATA/'settings.private.json'
API_MODEL=os.getenv('OPENAI_MODEL','gpt-4o-mini')
KEY=os.getenv('OPENAI_API_KEY','').strip()
DEMO_MODE=os.getenv('DEMO_MODE','1').lower() in ('1','true','yes')
REQUIRE_PIN=os.getenv('REQUIRE_STUDENT_PIN','0').lower() in ('1','true','yes')
PORT=int(os.getenv('PORT','8787'))
HOST=os.getenv('HOST','127.0.0.1')
LOCK=threading.RLock()
LOGIN_LOCK=threading.RLock()
LOGIN_FAILURES={}
DEFAULT_CRITERIA={
 'vivid':'글에 나타난 사건, 인물의 말과 행동, 장소, 수량 등 본문을 확인해 답할 수 있는 질문',
 'deep':'글을 근거로 등장인물의 마음, 행동의 이유, 의미 등을 생각하고 해석하는 질문',
 'if':'자신의 경험·가치 판단과 연결하거나 만약 ~라면의 상황을 가정하여 생각하는 질문',
 'extra':'학생이 쓴 문장은 가능한 그대로 남기고, 질문이 아닌 생각·느낌은 제외합니다. 문맥 속의 추측을 핵심 질문으로 오인하지 않습니다. 의미가 같은 중복 질문은 한 번만 추출합니다.'
}

def utcnow():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def dumps(x):return json.dumps(x,ensure_ascii=False,separators=(',',':'))
def read_json(s):return json.loads(s) if s else None

def private():
 if PRIVATE_PATH.exists():return json.loads(PRIVATE_PATH.read_text('utf-8'))
 settings={'secret':secrets.token_urlsafe(48),'admin_password':secrets.token_urlsafe(14),
           'student_pins':{str(n):str(secrets.randbelow(900000)+100000) for n in NUMS}}
 PRIVATE_PATH.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
 try:PRIVATE_PATH.chmod(0o600)
 except Exception:pass
 return settings
PRIV=private()
ADMIN_PASSWORD=os.getenv('ADMIN_PASSWORD') or PRIV['admin_password']

def db():
 c=sqlite3.connect(DB_PATH,timeout=12)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=12000')
 c.execute('PRAGMA journal_mode=WAL')
 return c

def initialize():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS student (number INTEGER PRIMARY KEY, phase INTEGER NOT NULL DEFAULT 1, original_first TEXT NOT NULL, original_second TEXT NOT NULL, first TEXT NOT NULL, second TEXT NOT NULL, initial_cards TEXT NOT NULL DEFAULT "[]", cards TEXT NOT NULL DEFAULT "[]", excluded TEXT NOT NULL DEFAULT "[]", criteria_version INTEGER, updated_at TEXT, stage1_at TEXT, submitted_at TEXT, revision INTEGER NOT NULL DEFAULT 0)')
  # Older deployed databases lack import provenance. Do not reset or alter those records.
  columns={r['name'] for r in c.execute('PRAGMA table_info(student)')}
  if 'current_import_id' not in columns:
   c.execute('ALTER TABLE student ADD COLUMN current_import_id INTEGER')
  c.execute('CREATE TABLE IF NOT EXISTS import_batch (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, imported_at TEXT NOT NULL, student_numbers TEXT NOT NULL, sha256 TEXT NOT NULL, file_bytes INTEGER NOT NULL DEFAULT 0)')
  c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
  c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',('criteria',dumps(DEFAULT_CRITERIA)))
  c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',('criteria_version','1'))
  c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',('reference_text',DEFAULT_REFERENCE_TEXT))
  c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',('reference_version','1'))
  for n in NUMS:
   arr=dumps(['']*len(PASSAGE));c.execute('INSERT OR IGNORE INTO student(number,original_first,original_second,first,second,updated_at) VALUES (?,?,?,?,?,?)',(n,arr,arr,arr,arr,utcnow()))
initialize()

def get_student(c,n):
 row=c.execute('SELECT * FROM student WHERE number=?',(n,)).fetchone()
 if not row:raise ValueError('없는 학생 번호입니다.')
 out=dict(row)
 for k in ['original_first','original_second','first','second','initial_cards','cards','excluded']:out[k]=read_json(out[k])
 return out

def is_imported_record(s):
 return any(x.strip() for x in s['original_first']+s['original_second'])

def import_listing(c):
 # The existing app never stored uploaded filenames. Present older data honestly.
 students={n:get_student(c,n) for n in NUMS}
 batches=[]
 for b in c.execute('SELECT id,filename,imported_at,student_numbers FROM import_batch ORDER BY id DESC'):
  original=[int(n) for n in read_json(b['student_numbers'])]
  active=[n for n in original if students[n]['current_import_id']==b['id']]
  blocked=[n for n in active if students[n]['phase']!=1 or students[n]['first']!=students[n]['original_first'] or students[n]['second']!=students[n]['original_second']]
  batches.append({'id':b['id'],'filename':b['filename'],'imported_at':b['imported_at'],'numbers':original,'active_numbers':active,'blocked_numbers':blocked,'can_delete':not blocked})
 legacy=[n for n,s in students.items() if s['current_import_id'] is None and is_imported_record(s)]
 blocked=[n for n in legacy if students[n]['phase']!=1 or students[n]['first']!=students[n]['original_first'] or students[n]['second']!=students[n]['original_second']]
 if legacy:
  batches.append({'id':'legacy','filename':'기존 등록 자료 (파일명 확인 불가)','imported_at':None,'numbers':legacy,'active_numbers':legacy,'blocked_numbers':blocked,'can_delete':not blocked})
 return batches

def clear_imported_students(c,numbers):
 blank=dumps(['']*len(PASSAGE))
 now=utcnow()
 for n in numbers:
  c.execute('UPDATE student SET original_first=?,original_second=?,first=?,second=?,initial_cards="[]",cards="[]",excluded="[]",current_import_id=NULL,updated_at=?,revision=revision+1 WHERE number=?',
   (blank,blank,blank,blank,now,n))


def sign_token(kind,number=0):
 payload=dumps({'kind':kind,'number':number,'exp':int(time.time())+60*60*24*7}).encode()
 raw=base64.urlsafe_b64encode(payload).rstrip(b'=')
 signature=hmac.new(PRIV['secret'].encode(),raw,hashlib.sha256).digest()
 return raw.decode()+'.'+base64.urlsafe_b64encode(signature).rstrip(b'=').decode()

def verify_token(value,kind):
 try:
  if not value.startswith('Bearer '):raise ValueError()
  a,b=value[7:].split('.',1)
  signature=base64.urlsafe_b64decode(b+'='*(-len(b)%4))
  expected=hmac.new(PRIV['secret'].encode(),a.encode(),hashlib.sha256).digest()
  if not hmac.compare_digest(signature,expected):raise ValueError()
  obj=json.loads(base64.urlsafe_b64decode(a+'='*(-len(a)%4)))
  if obj['kind']!=kind or obj['exp']<time.time():raise ValueError()
  return obj
 except Exception:raise ApiError(401,'로그인이 필요합니다. 다시 접속해 주세요.')

class ApiError(Exception):
 def __init__(self,status,message):self.status=status;self.message=message

def validate_rows(records):
 if not isinstance(records,dict):raise ApiError(400,'기록 자료의 형식을 확인해 주세요.')
 for key in ('first','second'):
  a=records.get(key)
  if not isinstance(a,list) or len(a)!=len(PASSAGE) or any(not isinstance(x,str) or len(x)>2000 for x in a):
   raise ApiError(400,f'{key}는 {len(PASSAGE)}개 행의 문자열로 입력해야 합니다.')
 return records['first'],records['second']

def login_check(ip,kind):
 now=time.time();key=(ip,kind)
 with LOGIN_LOCK:
  recent=[t for t in LOGIN_FAILURES.get(key,[]) if now-t<300]
  LOGIN_FAILURES[key]=recent
  if len(recent)>=20:raise ApiError(429,'로그인 시도가 많아요. 잠시 후 다시 시도해 주세요.')

def login_failed(ip,kind):
 now=time.time();key=(ip,kind)
 with LOGIN_LOCK:
  LOGIN_FAILURES[key]=[t for t in LOGIN_FAILURES.get(key,[]) if now-t<300]+[now]

def category_demo(q):
 if re.search(r'만약|나라면|내가 .*라면|너라면|나도 .*한 적|경험|한 적|내가 .*했을까',q):return 'if'
 if re.search(r'몇 개|몇 마리|언제|어디|누가|누구|무엇을|얼마나|몇 명',q):return 'vivid'
 return 'deep'

def classify_demo(first,second):
 cards=[];seen=set();ex=[]
 for i in range(len(PASSAGE)):
  for origin,contents in [('first',first[i]),('second',second[i])]:
   for paragraph in contents.replace('\r','').split('\n'):
    p=paragraph.strip(); pos=p.find('?')
    if pos<0:
     if p:ex.append({'text':p[:200],'reason':'질문 형태가 아님','origin':origin,'sourceRow':i+1})
     continue
    q=re.sub(r'^[\s•\-\d.()]+','',p[:pos+1]).strip()
    if len(q)<4:continue
    sig=re.sub(r'[\W_]+','',q).lower()
    if sig in seen:
     ex.append({'text':q,'reason':'중복 질문','origin':origin,'sourceRow':i+1});continue
    seen.add(sig)
    cards.append({'id':'ai-'+str(len(cards)+1),'text':q,'category':category_demo(q),'origin':origin,'sourceRow':i+1,'added':False})
 return cards,ex

def classify_openai(first,second,criteria,reference_text):
 if not KEY:raise ApiError(503,'OpenAI API 키가 연결되지 않았어요. 기록은 그대로 보존돼요.')
 data=[{'row':i+1,'passage':PASSAGE[i],'first':first[i],'second':second[i]} for i in range(len(PASSAGE)) if first[i].strip() or second[i].strip()]
 if not data:return [],[]
 schema={'type':'object','additionalProperties':False,'required':['questions','excluded'],'properties':{
  'questions':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['text','category','origin','sourceRow'],'properties':{
   'text':{'type':'string'},'category':{'type':'string','enum':['vivid','deep','if']},'origin':{'type':'string','enum':['first','second']},'sourceRow':{'type':'integer'}}}},
  'excluded':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['text','reason','origin','sourceRow'],'properties':{
   'text':{'type':'string'},'reason':{'type':'string'},'origin':{'type':'string','enum':['first','second']},'sourceRow':{'type':'integer'}}}}
 }}
 system='''초등학교 6학년 국어 수업용 질문 추출·분류 담당자다. 학생이 고친 원문에서 실제로 궁금한 핵심 질문만 뽑는다. 학생 표현은 가능한 바꾸지 않는다. "무지개 물고기는 왜 행복할까? 친구를 사귀어서 그런가? 나도 비슷한 경험이 있어."에서는 "무지개 물고기는 왜 행복할까?"만 핵심 질문으로 추출한다. 뒤의 추측·생각·느낌을 별개의 질문으로 무리하게 추출하지 않는다. 서로 의미가 같은 질문은 한 번만 남긴다. 다른 뜻의 질문은 모두 살린다. 어색한 질문도 함부로 교정하지 않는다. 질문은 무조건 vivid/deep/if 중 하나로 분류한다. excluded에는 버린 후보 중 학생이 확인할 만한 내용만 간결히 기록한다. 표 행은 1부터 시작한다. 반드시 입력 데이터의 origin과 sourceRow를 정확히 유지한다. 분류 기준:\n'''+dumps(criteria)
 body={'model':API_MODEL,'messages':[{'role':'system','content':system + '\n질문을 생생/곰곰/라면으로 분류할 때 반드시 제공된 참고 원문 전체와 행별 본문을 함께 고려한다. ' + '본문에 직접 확인되는 사실인지, 본문에 근거한 해석인지, 텍스트 밖의 경험과 가치 판단인지를 구분한다. 질문에 쓰인 어미만 보고 유형을 결정하지 않는다.'},{'role':'user','content':dumps({'referenceText':reference_text,'studentRecords':data})}],
       'temperature':0,'response_format':{'type':'json_schema','json_schema':{'name':'questions_result','strict':True,'schema':schema}}}
 req=urllib.request.Request('https://api.openai.com/v1/chat/completions',data=dumps(body).encode('utf-8'),headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'},method='POST')
 try:
  with urllib.request.urlopen(req,timeout=75) as response:raw=json.loads(response.read())
  content=raw['choices'][0]['message']['content'];result=json.loads(content)
 except urllib.error.HTTPError as ex:
  detail=ex.read(500).decode('utf-8','replace')
  print('OpenAI HTTP error:',ex.code,detail,file=sys.stderr)
  raise ApiError(502,f'AI 분류 요청에 실패했어요 (HTTP {ex.code}). 잠시 후 다시 시도해 주세요.')
 except Exception as ex:
  print('OpenAI response error:',repr(ex),file=sys.stderr)
  raise ApiError(502,'AI 결과를 받는 중 오류가 발생했어요. 1단계 수정 내용은 보존돼요.')
 cards=[];seen=set()
 for item in result.get('questions',[])[:200]:
  q=str(item.get('text','')).strip();cat=item.get('category');origin=item.get('origin');row=item.get('sourceRow')
  if not q or not 1<=len(q)<=500 or cat not in ('vivid','deep','if') or origin not in ('first','second') or not isinstance(row,int) or not 1<=row<=len(PASSAGE):continue
  sig=re.sub(r'[\W_]+','',q).lower()
  if sig in seen:continue
  seen.add(sig)
  cards.append({'id':'ai-'+str(len(cards)+1),'text':q,'category':cat,'origin':origin,'sourceRow':row,'added':False})
 return cards,result.get('excluded',[])[:200]

def validate_cards(incoming,existing):
 if not isinstance(incoming,list) or len(incoming)>350:raise ApiError(400,'질문카드 데이터를 확인해 주세요.')
 by_id={x['id']:x for x in existing}
 if len(by_id)!=len(existing):raise ApiError(500,'저장된 카드 ID 오류')
 seen=set();out=[]
 for q in incoming:
  if not isinstance(q,dict):raise ApiError(400,'질문카드 형식 오류')
  id=q.get('id');cat=q.get('category');txt=q.get('text')
  if not isinstance(id,str) or len(id)>100 or id in seen or cat not in ('vivid','deep','if') or not isinstance(txt,str) or not txt.strip() or len(txt)>500:
   raise ApiError(400,'질문 내용 또는 분류를 확인해 주세요.')
  seen.add(id)
  if id in by_id:
   old=by_id[id]
   if txt!=old['text']:raise ApiError(400,'기존 질문의 글은 변경할 수 없어요.')
   out.append({**old,'category':cat})
  else:
   if not id.startswith('added-'):raise ApiError(400,'추가한 질문의 ID가 올바르지 않아요.')
   out.append({'id':id,'text':txt.strip(),'category':cat,'origin':'student','sourceRow':None,'added':True})
 if any(id not in seen for id in by_id):raise ApiError(400,'질문카드를 삭제할 수 없어요.')
 return out

def xlsx_bytes(snapshots):
 # Office Open XML 파일을 파이썬 기본 모듈로 구성 (설치할 추가 패키지 없음)
 rows1=[['학생번호','단계','최근 수정','1단계 완료','최종 제출','AI 질문 수','최종 카드 수','직접 추가 수','이동 수']]
 rows2=[['학생번호','행','본문','AI 1차 기록','학생 수정 1차 기록','AI 2차 기록','학생 수정 2차 기록']]
 rows3=[['학생번호','질문 ID','질문','기록 차수','본문 행','최초 AI 분류','최종 분류','직접 추가']]
 labels={'vivid':'생생','deep':'곰곰','if':'라면'}
 for s in snapshots:
  initial={q['id']:q for q in s['initial_cards']};moved=sum(q['category']!=initial[q['id']]['category'] for q in s['cards'] if q['id'] in initial)
  rows1.append([s['number'],s['phase'],s['updated_at'],s['stage1_at'],s['submitted_at'],len(initial),len(s['cards']),sum(q.get('added',False) for q in s['cards']),moved])
  for i,passage in enumerate(PASSAGE):rows2.append([s['number'],i+1,passage,s['original_first'][i],s['first'][i],s['original_second'][i],s['second'][i]])
  for q in s['cards']:
   old=initial.get(q['id']);rows3.append([s['number'],q['id'],q['text'],('1차' if q.get('origin')=='first' else '2차' if q.get('origin')=='second' else '학생 추가'),q.get('sourceRow'),labels.get(old['category'],'') if old else '',labels[q['category']],'O' if q.get('added') else ''])
 def xmlsheet(rows):
  x=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetData>']
  for r,row in enumerate(rows,1):
   x.append(f'<row r="{r}">')
   for col,val in enumerate(row):
    if val is None:continue
    a='';v=col+1
    while v:a=chr(65+(v-1)%26)+a;v=(v-1)//26
    coord=f'{a}{r}'
    if isinstance(val,int):x.append(f'<c r="{coord}"><v>{val}</v></c>')
    else:x.append(f'<c r="{coord}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(str(val))}</t></is></c>')
   x.append('</row>')
  x.append('</sheetData></worksheet>');return ''.join(x)
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('[Content_Types].xml','''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'''+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,4))+'</Types>')
  z.writestr('_rels/.rels','''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
  z.writestr('xl/workbook.xml','''<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'''+''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i,name in enumerate(['학생별 현황','자세히 읽기 수정','질문 분류 결과'],1))+'</sheets></workbook>')
  z.writestr('xl/_rels/workbook.xml.rels','''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'''+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,4))+'</Relationships>')
  for i,rows in enumerate([rows1,rows2,rows3],1):z.writestr(f'xl/worksheets/sheet{i}.xml',xmlsheet(rows))
 return out.getvalue()

class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*a,**kw):super().__init__(*a,directory=str(WEB),**kw)
 def log_message(self,format,*args):pass
 def json_response(self,status,obj):
  b=dumps(obj).encode('utf-8');self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def binary(self,data,filename,ctype):
  self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Content-Disposition',f'attachment; filename="{filename}"');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def incoming(self):
  if int(self.headers.get('Content-Length','0'))>2_000_000:raise ApiError(413,'파일이 너무 큽니다.')
  return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode('utf-8'))
 def auth(self,role):return verify_token(self.headers.get('Authorization',''),role)
 def execute(self,action):
  try:return action()
  except ApiError as ex:return self.json_response(ex.status,{'error':ex.message})
  except (ValueError,KeyError,TypeError,json.JSONDecodeError) as ex:return self.json_response(400,{'error':'요청 내용을 확인해 주세요.'})
  except Exception as ex:
   print('Server exception:',repr(ex),file=sys.stderr)
   return self.json_response(500,{'error':'서버 오류가 발생했어요.'})
 def do_GET(self):return self.execute(self.get_request)
 def do_POST(self):return self.execute(self.post_request)
 def get_request(self):
  path=urlsplit(self.path).path
  if path=='/api/config':return self.json_response(200,{'numbers':NUMS,'passages':PASSAGE,'demo':DEMO_MODE,'requiresPin':REQUIRE_PIN,'rows':len(PASSAGE)})
  if path=='/api/student/me':
   number=self.auth('student')['number']
   with db() as c:s=get_student(c,number)
   return self.json_response(200,{'student':s})
  if path=='/api/teacher/overview':
   self.auth('teacher')
   with db() as c:
    items=[get_student(c,n) for n in NUMS]
    criteria=json.loads(c.execute('SELECT value FROM settings WHERE key="criteria"').fetchone()['value'])
    version=int(c.execute('SELECT value FROM settings WHERE key="criteria_version"').fetchone()['value'])
    reference_text=c.execute('SELECT value FROM settings WHERE key="reference_text"').fetchone()['value']
    reference_version=int(c.execute('SELECT value FROM settings WHERE key="reference_version"').fetchone()['value'])
    imports=import_listing(c)
   return self.json_response(200,{'students':items,'imports':imports,'criteria':criteria,'criteria_version':version,'reference_text':reference_text,'reference_version':reference_version,'mode':('demo' if DEMO_MODE else ('api' if KEY else 'no-key')),'requiresPin':REQUIRE_PIN,'student_pins':PRIV['student_pins'] if REQUIRE_PIN else {}})
  if path=='/api/teacher/export':
   self.auth('teacher')
   with db() as c:items=[get_student(c,n) for n in NUMS]
   return self.binary(xlsx_bytes(items),'rainbow_question_results.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
  if path=='/api/teacher/template':
   self.auth('teacher')
   payload={'students':{str(n):{'first':['']*len(PASSAGE),'second':['']*len(PASSAGE)} for n in NUMS}}
   return self.binary(json.dumps(payload,ensure_ascii=False,indent=2).encode('utf-8'),'student_records_template.json','application/json; charset=utf-8')
  if path in ('/','/index.html'):self.path='/index.html';return super().do_GET()
  if path=='/teacher':self.path='/teacher.html';return super().do_GET()
  if path.startswith('/api/'):raise ApiError(404,'요청한 기능이 없습니다.')
  return super().do_GET()
 def post_request(self):
  path=urlsplit(self.path).path
  body=self.incoming()
  if path=='/api/student/login':
   n=body.get('number')
   if type(n)!=int or n not in NUMS:raise ApiError(400,'학생 번호를 선택해 주세요.')
   login_check(self.client_address[0],'student')
   if REQUIRE_PIN and str(body.get('pin',''))!=PRIV['student_pins'][str(n)]:
    login_failed(self.client_address[0],'student')
    raise ApiError(403,'접속 코드가 일치하지 않습니다.')
   return self.json_response(200,{'token':sign_token('student',n),'number':n})
  if path=='/api/teacher/login':
   login_check(self.client_address[0],'teacher')
   if not hmac.compare_digest(str(body.get('password','')),ADMIN_PASSWORD):
    login_failed(self.client_address[0],'teacher')
    raise ApiError(403,'교사 비밀번호가 일치하지 않습니다.')
   return self.json_response(200,{'token':sign_token('teacher')})
  if path.startswith('/api/student/'):
   n=self.auth('student')['number']
   if path=='/api/student/save-records':
    first,second=validate_rows(body)
    with LOCK,db() as c:
     s=get_student(c,n)
     if s['phase']!=1:raise ApiError(409,'이미 다음 단계로 이동해 기록을 수정할 수 없어요.')
     if body.get('revision')!=s['revision']:raise ApiError(409,'다른 화면에서 수정한 기록이 있어요. 새로고침해 주세요.')
     c.execute('UPDATE student SET first=?,second=?,revision=revision+1,updated_at=? WHERE number=?',(dumps(first),dumps(second),utcnow(),n))
     s=get_student(c,n)
    return self.json_response(200,{'student':s})
   if path=='/api/student/advance':
    with LOCK,db() as c:
     s=get_student(c,n)
     if s['phase']!=1:return self.json_response(200,{'student':s})
     first,second=s['first'],s['second']
     criteria=json.loads(c.execute('SELECT value FROM settings WHERE key="criteria"').fetchone()['value'])
     version=int(c.execute('SELECT value FROM settings WHERE key="criteria_version"').fetchone()['value'])
     reference_text=c.execute('SELECT value FROM settings WHERE key="reference_text"').fetchone()['value']
    cards,excluded=classify_demo(first,second) if DEMO_MODE else classify_openai(first,second,criteria,reference_text)
    with LOCK,db() as c:
     cur=get_student(c,n)
     if cur['phase']!=1:return self.json_response(200,{'student':cur})
     if cur['revision']!=s['revision']:raise ApiError(409,'기록이 다시 수정됐어요. 확인 후 다음 단계로 이동해 주세요.')
     now=utcnow()
     c.execute('UPDATE student SET phase=2,initial_cards=?,cards=?,excluded=?,criteria_version=?,stage1_at=?,updated_at=?,revision=revision+1 WHERE number=?',(dumps(cards),dumps(cards),dumps(excluded),version,now,now,n))
     cur=get_student(c,n)
    return self.json_response(200,{'student':cur,'classificationMode':'demo' if DEMO_MODE else 'ai'})
   if path=='/api/student/save-cards':
    with LOCK,db() as c:
     s=get_student(c,n)
     if s['phase']!=2:raise ApiError(409,'분류 중이거나 이미 제출한 상태가 아니에요.')
     if body.get('revision')!=s['revision']:raise ApiError(409,'다른 화면에서 질문이 수정됐어요. 새로고침해 주세요.')
     cards=validate_cards(body.get('cards'),s['cards'])
     c.execute('UPDATE student SET cards=?,updated_at=?,revision=revision+1 WHERE number=?',(dumps(cards),utcnow(),n))
     s=get_student(c,n)
    return self.json_response(200,{'student':s})
   if path=='/api/student/submit':
    with LOCK,db() as c:
     s=get_student(c,n)
     if s['phase']==3:return self.json_response(200,{'student':s})
     if s['phase']!=2:raise ApiError(409,'질문 분류 단계를 마쳐 주세요.')
     now=utcnow();c.execute('UPDATE student SET phase=3,submitted_at=?,updated_at=?,revision=revision+1 WHERE number=?',(now,now,n));s=get_student(c,n)
    return self.json_response(200,{'student':s})
  if path.startswith('/api/teacher/'):
   self.auth('teacher')
   if path=='/api/teacher/test-ai':
    if DEMO_MODE:raise ApiError(400,'현재 시험 모드입니다. DEMO_MODE=0을 설정한 후 시험해 주세요.')
    with db() as c:
     criteria=json.loads(c.execute('SELECT value FROM settings WHERE key="criteria"').fetchone()['value'])
     reference_text=c.execute('SELECT value FROM settings WHERE key="reference_text"').fetchone()['value']
    test_first=['무지개 물고기는 왜 행복해졌을까?']+['']*(len(PASSAGE)-1)
    test_second=['내가 무지개 물고기라면 비늘을 나누어 주었을까?']+['']*(len(PASSAGE)-1)
    cards,excluded=classify_openai(test_first,test_second,criteria,reference_text)
    return self.json_response(200,{'ok':True,'count':len(cards),'categories':[c['category'] for c in cards]})
   if path=='/api/teacher/reference':
    reference_text=body.get('reference_text')
    if not isinstance(reference_text,str) or not reference_text.strip() or len(reference_text)>40000:
     raise ApiError(400,'참고 본문은 빈칸 없이 40,000자 이내로 입력해 주세요.')
    with LOCK,db() as c:
     c.execute('UPDATE settings SET value=? WHERE key="reference_text"',(reference_text.strip(),))
     c.execute('UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key="reference_version"')
    return self.json_response(200,{'ok':True})
   if path=='/api/teacher/criteria':
    criteria=body.get('criteria')
    if not isinstance(criteria,dict) or any(not isinstance(criteria.get(k),str) or not criteria[k].strip() or len(criteria[k])>3000 for k in DEFAULT_CRITERIA):raise ApiError(400,'세 가지 분류 기준과 추가 지침을 모두 입력해 주세요.')
    with LOCK,db() as c:
     c.execute('UPDATE settings SET value=? WHERE key="criteria"',(dumps({k:criteria[k] for k in DEFAULT_CRITERIA}),))
     c.execute('UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key="criteria_version"')
    return self.json_response(200,{'ok':True})
   if path=='/api/teacher/delete-import':
    if body.get('confirm') is not True:raise ApiError(400,'삭제 확인이 필요합니다.')
    batch_id=body.get('id')
    if batch_id!='legacy' and (type(batch_id)!=int or batch_id<1):raise ApiError(400,'삭제할 등록 내역을 확인해 주세요.')
    with LOCK,db() as c:
     rows=import_listing(c)
     target=next((r for r in rows if r['id']==batch_id),None)
     if not target:raise ApiError(404,'등록 내역을 찾을 수 없어요. 새로고침해 주세요.')
     if not target['can_delete']:
      raise ApiError(409,'학생 '+', '.join(map(str,target['blocked_numbers']))+'번이 이미 기록을 수정했거나 다음 단계에 진입했어요. 해당 자료는 삭제하지 않았어요.')
     nums=target['active_numbers']
     if batch_id=='legacy':
      # Previous app versions only stored the extracted text, not a source file name.
      clear_imported_students(c,nums)
     else:
      clear_imported_students(c,nums)
      c.execute('DELETE FROM import_batch WHERE id=?',(batch_id,))
    return self.json_response(200,{'ok':True,'cleared':nums})
   if path=='/api/teacher/import':
    students=body.get('students')
    if not isinstance(students,dict):raise ApiError(400,'students 항목을 확인해 주세요.')
    staged=[]
    for number,payload in students.items():
     if not str(number).isdigit() or int(number) not in NUMS:raise ApiError(400,f'학생 번호 {number}는 사용할 수 없어요.')
     first,second=validate_rows(payload);staged.append((int(number),first,second))
    if not staged:raise ApiError(400,'학생 자료가 비어 있어요.')
    raw_filename=body.get('source_filename','등록된 학생 자료.json')
    if not isinstance(raw_filename,str):raise ApiError(400,'파일 이름이 올바르지 않아요.')
    filename=''.join(x for x in raw_filename.replace('\\','/').split('/')[-1] if x.isprintable()).strip()[:180] or '등록된 학생 자료.json'
    # Provenance is recorded for future uploads; the original JSON bytes are not saved.
    digest=hashlib.sha256(dumps(students).encode('utf-8')).hexdigest()
    file_bytes=len(dumps(students).encode('utf-8'))
    with LOCK,db() as c:
     for n,first,second in staged:
      s=get_student(c,n)
      if s['phase']!=1:raise ApiError(409,f'{n}번은 이미 2단계 이상 진행해 덮어쓸 수 없어요.')
      if s['first']!=s['original_first'] or s['second']!=s['original_second']:
       raise ApiError(409,f'{n}번은 학생이 수정을 시작했어요. 덮어쓰지 않았어요.')
     cur=c.execute('INSERT INTO import_batch(filename,imported_at,student_numbers,sha256,file_bytes) VALUES(?,?,?,?,?)',
       (filename,utcnow(),dumps([n for n,_,_ in staged]),digest,file_bytes))
     import_id=cur.lastrowid
     for n,first,second in staged:
      c.execute('UPDATE student SET original_first=?,original_second=?,first=?,second=?,current_import_id=?,updated_at=?,revision=revision+1 WHERE number=?',
       (dumps(first),dumps(second),dumps(first),dumps(second),import_id,utcnow(),n))
    return self.json_response(200,{'ok':True,'updated':[x[0] for x in staged]})
  raise ApiError(404,'요청한 기능이 없습니다.')

if __name__=='__main__':
 print('\n  무지개 물고기 질문 정리 웹앱')
 print(f'  학생 화면: http://{HOST}:{PORT}/')
 print(f'  교사 화면: http://{HOST}:{PORT}/teacher')
 print(f'  교사 암호: {ADMIN_PASSWORD}')
 print('  AI 분류:', '테스트용 가상 분류 (DEMO_MODE=1)' if DEMO_MODE else ('OpenAI API 연결' if KEY else '미연결: API 키 필요'))
 if not REQUIRE_PIN:print('  ⚠ 학생 번호만으로 접속 가능. 실제 배포 전 접속 보안 검토 필요')
 if HOST!='127.0.0.1':print('  ⚠ 다른 기기 접속 시 HTTPS, 접근 통제, 개인정보 보호 설정 필요')
 print('')
 ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
