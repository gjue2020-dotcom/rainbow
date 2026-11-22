# 무지개 물고기 질문 웹앱 — Railway 배포 가이드

## 준비

- `Dockerfile`, `server.py`, `web/`, `data/passage.json`이 **같은 최상위 폴더**에 있어야 합니다.
- GitHub에서는 새 **Private** 저장소를 만들고, 압축을 푼 폴더 안의 파일/폴더를 전부 업로드합니다. 학생 자료 `무지개물고기_학생_손글씨_추출.json`은 **GitHub에 업로드하지 마세요**.
- Railway 로그인 → New Project → Deploy from GitHub repo → 해당 저장소를 선택합니다.

## Railway 서비스 설정

1. Variables에 아래 환경변수를 추가합니다. 항목별 값은 사용자가 직접 입력합니다.

```
DEMO_MODE=1
REQUIRE_STUDENT_PIN=1
DATA_DIR=/data
HOST=0.0.0.0
ADMIN_PASSWORD=직접_정한_긴_비밀번호
```

- `DEMO_MODE=1`은 유료 AI 호출이 없는 시험 모드입니다.
- `ADMIN_PASSWORD` 값은 16자 이상의 긴 비밀번호를 권장합니다.
- `PORT`는 Railway가 자동 제공하므로 설정하지 않는 것이 좋습니다.
2. 서비스에 **Volume**을 추가하고 **Mount Path를 정확히 `/data`**로 지정합니다. SQLite 학생 데이터와 학생별 접속코드가 보존되는 필수 설정입니다.
3. 설정 변경사항을 Deploy합니다. Service Settings → Networking → Generate Domain에서 HTTPS 주소를 만듭니다.
4. 학생 주소 `https://생성주소/`, 교사 주소 `https://생성주소/teacher`로 접속합니다.
5. 교사용 비밀번호로 로그인하여 교사용 페이지의 학생별 접속 코드 목록을 확인합니다.
6. `무지개물고기_학생_손글씨_추출.json`은 교사 화면 **학생 자료 등록**에서만 업로드합니다. 학생 자료가 들어간 다음 번호별 데이터가 맞는지 먼저 확인합니다.

## OpenAI 실제 분류 연결 (학교의 외부 AI 사용 및 개인정보 처리 절차 확인 후)

1. OpenAI 플랫폼의 API Billing에서 결제·크레딧을 설정하고, Project의 API Keys에서 새 Secret Key를 발급받습니다. ChatGPT 구독과 별개입니다.
2. Railway 서비스 Variables에 `OPENAI_API_KEY=...`, `OPENAI_MODEL=gpt-4o-mini`, `DEMO_MODE=0`을 등록합니다.
3. Deploy한 뒤 교사 화면의 'OpenAI API 연결됨' 상태를 확인합니다.
4. 교사 화면의 **AI 연결 시험** 버튼을 눌러 시험 문장만으로 분류해 봅니다. 이 기능은 학생 데이터나 단계를 바꾸지 않습니다. (실제 API 호출 비용이 소액 발생할 수 있음.)

## 중요 제약

- 현재 버전은 **손글씨와 질문 텍스트**의 수정·제출을 지원합니다. **밑줄/형광펜을 본문에서 선택·수정하는 기능은 아직 구현되지 않았습니다.** 해당 기능을 연결하기 전에는 학생 대상 최종 배포로 간주하지 마세요.
- 공개 URL에서는 다른 학생 글 보호를 위해 `REQUIRE_STUDENT_PIN=1`을 유지하세요. 학생 번호 선택 후 자기 코드를 입력하게 됩니다.
- 기기 간 자동 저장은 Railway Volume이 올바르게 연결된 경우에만 보장할 수 있습니다. 수업 전 2대 이상의 태블릿으로 검증하세요.
- 학생 이름이나 원본 사진은 GitHub 저장소에 넣지 마세요. OpenAI API 비밀키도 GitHub에 올리지 마세요.
- 데이터는 Railway Volume에 보관됩니다. 수업 후 교사 화면의 엑셀 내려받기를 통해 별도 백업하세요.
