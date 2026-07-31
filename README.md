# mailon-backup

mailon.kr 개인 메일함을 **Markdown 파일로 백업**하고, 이후 **새로 도착한 메일만 증분 동기화**하는 커맨드라인 도구입니다. 메일 서버의 비공개 API를 직접 호출하지 않고, [`agent-browser`](https://www.npmjs.com/package/agent-browser)(CDP 기반 Chrome 자동화 CLI)로 실제 브라우저를 조작해 사람이 하는 것과 같은 순서로 로그인하고 메일을 읽어옵니다.

- 받은메일함과 보낸메일함을 YAML front-matter가 붙은 Markdown으로 저장합니다.
- 첨부파일을 메일 UID별 폴더로 내려받습니다.
- SQLite 상태 DB로 이미 저장한 메일을 건너뛰므로, 반복 실행해도 중복이 쌓이지 않습니다.
- ID + 비밀번호 + Google OTP(TOTP) 2단계 인증을 자동으로 통과합니다.
- 메일 작성/발송, 수신자 이름→이메일 조회 기능도 포함되어 있습니다.

---

## 이 도구는 무엇이고, 무엇이 아닌가 (범위와 윤리)

이 프로젝트를 사용하기 전에 아래 내용을 반드시 읽고 동의해 주세요.

- **본인 계정 전용입니다.** `.env`에 넣는 크레덴셜은 본인이 정당하게 소유한 mailon.kr 계정의 것이어야 합니다.
- **타인의 메일을 수집하는 용도로 사용하지 마세요.** 다른 사람의 계정에 접근하거나, 위임받지 않은 메일함을 백업하는 행위는 이 프로젝트의 범위 밖이며 법적 책임이 따를 수 있습니다.
- **mailon.kr은 KISTI(한국과학기술정보연구원)가 운영하는 공공기관 메일 서비스입니다.** 공용 인프라를 자동화로 두드리는 것이므로 남용하지 마세요. 대량 트래픽 유발, 병렬 실행, 다계정 크롤링은 금지합니다.
- **크론(작업 스케줄러) 실행 간격은 10분 미만으로 설정하지 마세요.** 개인 백업 목적이라면 1시간 주기로도 충분합니다.
- **비공식 도구이며 무보증입니다.** mailon.kr 또는 KISTI와 아무런 제휴 관계가 없습니다. 이 도구는 특정 시점의 웹 UI 구조(DOM 셀렉터, 내부 JS 함수)에 의존하므로, **사이트가 개편되면 예고 없이 동작이 깨집니다.**
- **로그인 실패가 누적되면 계정이 잠길 수 있습니다.** 서비스 정책상 연속 실패 시 계정이 잠길 수 있으므로, **3회 연속 실패하면 즉시 중단**하고 원인을 먼저 확인하세요. 실패 직후 곧바로 재시도하지 말고 최소 30초 이상 기다리는 것을 권장합니다.
- **모든 로그인에는 2단계 인증이 필요하며 우회할 수 없습니다.** 이 도구는 사용자가 직접 등록한 TOTP 시크릿으로 폰과 동일한 6자리 코드를 계산할 뿐, 인증 자체를 건너뛰지 않습니다.

한마디로: **개인이 자기 메일을 자기 PC에 보관하기 위한 백업 도구**이며, 크롤러나 스크래핑 프레임워크가 아닙니다.

---

## 요구사항

| 항목 | 내용 |
|---|---|
| 운영체제 | **Windows 10 / 11** (배치 스크립트와 작업 스케줄러 연동이 Windows 기준으로 작성됨) |
| Python | **3.13 기준**으로 개발·검증. 오프라인 테스트는 3.12에서도 통과 확인 |
| Node.js | `agent-browser` 설치를 위해 필요 (LTS 권장) |
| 브라우저 자동화 | `npm i -g agent-browser` 후 `agent-browser install` 로 Chrome 런타임 준비 |
| 계정 | mailon.kr 계정 + **Google OTP(2FA) 등록 필수**, 그리고 등록 시 표시되는 **Base32 시크릿 문자열** |

```bash
npm i -g agent-browser
agent-browser install
agent-browser doctor --offline --quick
```

> **TOTP 시크릿을 확보하는 법**: mailon.kr에서 Google OTP를 (재)등록하면 QR 코드와 함께 Base32 문자열이 표시됩니다. QR은 평소처럼 폰의 Authenticator 앱으로 스캔하고, **화면에 보이는 Base32 문자열을 따로 기록**해 `.env`의 `MAILON_TOTP_SECRET`에 넣으세요. TOTP는 "공유 비밀" 방식이므로 폰과 이 도구가 같은 시크릿으로 같은 코드를 만들어도 정상입니다. 자세한 절차는 [`LOGIN_MANUAL.md`](LOGIN_MANUAL.md)를 참고하세요.

---

## 5분 빠른 시작

아래는 `C:\Users\<사용자명>\Documents\mailon-backup` 에 설치한다고 가정한 명령입니다. 명령 프롬프트(cmd)에서 그대로 복사해 붙여넣을 수 있습니다.

```bat
REM 1) 내려받기
cd C:\Users\<사용자명>\Documents
git clone https://github.com/<your-org>/mailon-backup.git
cd mailon-backup

REM 2) 가상환경 + 의존성
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

REM 3) 설정 파일 생성
copy .env.example .env
notepad .env
```

`notepad`로 열린 `.env`에 본인 값을 채웁니다(아래는 예시 형식이며, 실제 값으로 바꾸세요).

```ini
MAILON_ID=your_id@mailon.kr
MAILON_PW=your_password_here
MAILON_TOTP_SECRET=YOUR_BASE32_TOTP_SECRET_HERE
HEADLESS=true
MAX_MAILS_PER_RUN=0
MAILON_LOGIN_URL=https://mailon.kr/
```

이제 단계별로 확인합니다. **한 번에 `sync`부터 실행하지 말고 순서대로** 진행하세요.

```bat
REM 4) TOTP 코드가 폰의 Authenticator와 같은 숫자인지 확인
.venv\Scripts\python -m mailon.main totp

REM 5) 로그인만 시험 (메일은 건드리지 않음). "OK: https://mailon.kr/mail#..." 이면 성공
.venv\Scripts\python -m mailon.main login

REM 6) 메일 3개만 시험 수집 (폴더당 3개)
.venv\Scripts\python -m mailon.main sync --limit 3

REM 7) 현재 상태 확인
.venv\Scripts\python -m mailon.main status
```

여기까지 성공했다면 `--limit` 없이 `sync`를 실행해 전체 백업을 진행하고, 이후 작업 스케줄러에 등록해 주기적으로 돌리면 됩니다. 스케줄 등록 방법은 [`docs/04-cronjob.md`](docs/04-cronjob.md)를 참고하세요.

> `totp`가 폰과 다른 숫자를 출력한다면 시크릿이 잘못되었거나 PC 시계가 어긋난 것입니다. 관리자 명령 프롬프트에서 `w32tm /resync`로 시각을 맞춰 보세요. TOTP는 남은 유효시간이 **10초 이상**일 때 제출해야 안전합니다.

---

## 명령어 요약표

모든 명령은 `python -m mailon.main <명령> [옵션]` 형태로 실행합니다.

| 명령 | 옵션 | 설명 | 종료 코드 |
|---|---|---|---|
| `totp` | 없음 | 현재 TOTP 6자리 출력 | `0` |
| `login` | 없음 | 로그인만 하고 종료(크레덴셜 스모크 테스트) | `0` / `2`(LoginError) |
| `probe` | 없음 | 받은편지함 HTML·AX 트리·URL 덤프 | `0` / `2` |
| `sync` | `--limit N` (기본: `MAX_MAILS_PER_RUN`, **폴더당** 상한), `--folders inbox,sent` (기본 둘 다) | 로그인 후 새 메일 저장 | `0` / `2`(로그인·브라우저·설정·잘못된 폴더) / `3`(예기치 못한 예외) |
| `send` | `--to`(반복 가능, 필수), `--cc`(반복 가능), `--subject`(필수), `--body`(필수), `--attachment`(반복 가능), `--dry-run`, `--confirm-send`, `--json` | 메일 작성/발송 | `0` / `1`(`--dry-run`이 아닌데 `--confirm-send` 누락) / `2`(검증·안전장치 실패) |
| `resolve` | `--name`(필수), `--json` | 수신자 이름→이메일 자동완성 조회(읽기 전용) | `0` / `2` |
| `status` | 없음 | DB 상태와 마지막 실행 결과 출력 | `0` |

설정/환경변수 문제로 `RuntimeError`가 나면 종료 코드는 `1`이고, argparse 인자 오류(없는 옵션, 필수 옵션 누락 등)는 `2`입니다.

### 명령별 참고

- **`sync`** — 기본값은 `--folders inbox,sent`이므로 **받은메일함과 보낸메일함을 모두** 동기화합니다. 한쪽만 원하면 `--folders inbox` 또는 `--folders sent`를 지정하세요. `--limit`은 전체 합계가 아니라 **폴더당 상한**입니다.
- **`send`** — 사고 방지를 위해 이중 안전장치가 있습니다. 실제로 보내려면 `--confirm-send`를 반드시 붙여야 하고, 붙이지 않으면 아무것도 하지 않고 종료 코드 `1`로 끝납니다. 미리 점검만 하려면 `--dry-run`을 쓰세요. 발송 직전 폼의 실제 파라미터(본문·수신자)를 검증하고 불일치 시 발송을 거부합니다(fail-closed).
- **`resolve`** — 읽기 전용입니다. 메일 작성 화면의 자동완성 그리드만 조회하며 아무것도 발송하지 않습니다.
- **`status`** — 크레덴셜이 필요 없습니다. 로컬 SQLite DB만 읽습니다.
- **`probe`** — 사이트 구조가 바뀌어 수집이 실패할 때, 현재 DOM을 `logs/`에 덤프해 원인을 찾는 디버깅용 명령입니다.

```bat
REM 발송 예시: 먼저 dry-run으로 점검
.venv\Scripts\python -m mailon.main send --to someone@example.com --subject "제목" --body "본문" --dry-run

REM 실제 발송 (--confirm-send 필수)
.venv\Scripts\python -m mailon.main send --to someone@example.com --cc other@example.com --subject "제목" --body "본문" --confirm-send

REM 수신자 이름으로 이메일 주소 후보 조회 (읽기 전용)
.venv\Scripts\python -m mailon.main resolve --name "홍길동" --json
```

---

## 환경변수

모든 설정은 프로젝트 루트의 `.env` 파일로 주입합니다(실제 환경변수가 이미 설정되어 있으면 그 값이 우선합니다).

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `MAILON_ID` | 필수 | — | mailon.kr 로그인 ID(전체 메일 주소) |
| `MAILON_PW` | 필수 | — | mailon.kr 비밀번호 |
| `MAILON_TOTP_SECRET` | 필수 | — | Google OTP 등록 시 발급된 **Base32** 시크릿 |
| `MAILON_LOGIN_URL` | 선택 | `https://mailon.kr/` | 로그인 시작 URL |
| `HEADLESS` | 선택 | `true` | 브라우저를 숨김 모드로 실행할지 여부. 참으로 인정되는 값: `1` / `true` / `yes` / `on` / `y` (그 외 값은 거짓) |
| `MAX_MAILS_PER_RUN` | 선택 | `0` (무제한) | 한 번 실행에서 처리할 최대 메일 수. **폴더당** 적용되며, `sync --limit N`으로 덮어쓸 수 있음 |
| `AGENT_BROWSER_BIN` | 선택 | — | `agent-browser` 실행 파일의 절대 경로를 직접 지정. PATH에서 자동으로 찾지 못할 때 사용 |

세 개의 필수 변수 중 하나라도 비어 있으면 `RuntimeError`가 발생하며 종료 코드 `1`로 끝납니다. `.env`에 키를 추가했다면 `.env.example`에도 같은 키를 반영해 템플릿 패리티를 유지하세요.

---

## 출력물

실행 결과는 모두 프로젝트 폴더 아래에 생깁니다. `data/`와 `logs/`는 `.gitignore`에 등재되어 있어 커밋되지 않습니다.

```
data/
  mails/YYYY/MM/YYYY-MM-DD_<제목슬러그>_<uid>.md   메일 본문 (YAML front-matter 포함)
  attachments/<uid>/<파일명>                        첨부파일 (메일 UID별 폴더)
  state.db                                          SQLite 상태 DB (중복 방지 / 증분 동기화)
logs/
  sync-YYYY-MM-DD.log                               일자별 실행 로그
  probe-<타임스탬프>.html                            probe 명령의 HTML 덤프
  probe-<타임스탬프>-ax.txt                          probe 명령의 접근성(AX) 트리 덤프
  probe-<타임스탬프>-url.txt                         probe 명령이 기록한 현재 URL
  send-attempts.jsonl                               발송 시도 기록 (JSON Lines)
```

- 메일 파일명의 `<제목슬러그>`는 제목을 파일시스템에 안전하게 변환한 문자열로 **최대 60자**이며, **한글은 그대로 보존**됩니다.
- `data/state.db`에는 저장된 메일, 첨부파일 상태, 실행 이력이 기록됩니다. 이 파일을 지우면 다음 실행에서 모든 메일을 다시 수집합니다.

메일 Markdown은 대략 다음 형태입니다.

```markdown
---
uid: "10001"
folder: "inbox"
subject: "예시 제목"
from: "홍길동 <sender@example.com>"
to: "your_id@mailon.kr"
cc: ""
date: "2026-01-15T09:30:15"
collected_at: "2026-01-15T10:00:00.000000"
attachments:
  - "첨부.pdf"
---

# 예시 제목

(메일 본문…)
```

---

## 보안 요약

- **`.env`는 절대 git에 커밋하거나 클라우드에 동기화하지 마세요.** `.gitignore`에 이미 등재되어 있으며, 새 PC에서는 `copy .env.example .env` 후 패스워드 매니저에서 값을 채우는 방식을 권장합니다.
- **비밀번호와 TOTP 코드를 로그·이슈·스크린샷에 남기지 마세요.** 이 프로젝트의 코드는 비밀 값을 로그에 기록하지 않도록 작성되어 있습니다. 버그 리포트를 올릴 때도 같은 원칙을 지켜 주세요.
- **스크린샷이나 `probe` 덤프에 로그인 폼(아이디·비밀번호 입력란)이 찍혔다면 즉시 삭제**하세요. `logs/probe-*.html`에는 페이지 원본 HTML이 들어가므로 공유 전에 반드시 확인해야 합니다.
- TOTP 시크릿이 유출되면 2단계 인증이 무력화됩니다. 유출이 의심되면 mailon.kr에서 OTP를 초기화해 새 시크릿을 발급받으세요.

자세한 보안 지침은 [`docs/07-security.md`](docs/07-security.md)를 참고하세요.

---

## 문서 인덱스

| 문서 | 내용 |
|---|---|
| [`LOGIN_MANUAL.md`](LOGIN_MANUAL.md) | 로그인·2FA 설정 사용자 매뉴얼 |
| [`AGENTS.md`](AGENTS.md) | 이 저장소에서 작업하는 개발자/AI 에이전트를 위한 지침 |
| [`docs/00-architecture.md`](docs/00-architecture.md) | 시스템 전체 아키텍처 |
| [`docs/01-login.md`](docs/01-login.md) | 로그인 모듈 (ID + 비밀번호 + TOTP) |
| [`docs/02-scraper.md`](docs/02-scraper.md) | 메일 목록·상세·첨부 스크래퍼 |
| [`docs/03-backup.md`](docs/03-backup.md) | 전체 백업 + 증분 동기화 |
| [`docs/04-cronjob.md`](docs/04-cronjob.md) | Windows 작업 스케줄러 등록 |
| [`docs/05-send.md`](docs/05-send.md) | 메일 작성·발송 (`send`) |
| [`docs/06-resolve.md`](docs/06-resolve.md) | 수신자 이름→이메일 조회 (`resolve`) |
| [`docs/07-security.md`](docs/07-security.md) | 보안·비밀 관리 지침 |

---

## 테스트

오프라인 테스트 스위트는 **네트워크와 크레덴셜 없이** 실행됩니다. 브라우저 호출은 모두 가짜(mock) 객체로 대체되므로 mailon.kr에 접속하지 않습니다.

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests/ -v
```

총 **53개**의 오프라인 테스트가 있습니다. 코드를 수정한 뒤에는 이 스위트가 전부 통과하는지 확인하세요. 실패하는 테스트를 삭제해서 통과시키지 마세요.

---

## 라이선스 / 면책

**MIT License.**

이 소프트웨어는 **있는 그대로(AS IS)** 제공되며, 명시적이든 묵시적이든 **어떠한 보증도 하지 않습니다.** 사용으로 인해 발생하는 데이터 손실, 계정 잠금, 서비스 이용약관 위반 등 모든 결과에 대한 책임은 사용자 본인에게 있습니다.

이 프로젝트는 mailon.kr 및 운영 기관과 아무런 제휴 관계가 없는 **비공식 도구**이며, **개인이 본인 메일을 백업하는 용도**로만 사용해야 합니다.
