# mailon.kr 자동 로그인 매뉴얼

**대상**: ID, 비밀번호, TOTP 시크릿(Base32 문자열)을 가진 사용자
**결과**: mailon.kr 받은편지함에 **자동 로그인된 브라우저 세션** 확보

---

## 📋 1회 초기 설정 (최초 1번만)

### 1단계: 프로젝트 폴더로 이동

```cmd
cd C:\Users\<사용자명>\Documents\mailon-backup
```

### 2단계: `.env` 파일에 크레덴셜 저장

메모장으로 `.env` 편집:

```cmd
notepad .env
```

다음 3줄을 정확히 본인 값으로 채웁니다:

```
MAILON_ID=your_id@mailon.kr
MAILON_PW=your_password_here
MAILON_TOTP_SECRET=JBSWY3DPEHPK3PXP

HEADLESS=true
MAX_MAILS_PER_RUN=0
MAILON_LOGIN_URL=https://mailon.kr/
```

| 필드 | 설명 | 예시 |
|---|---|---|
| `MAILON_ID` | mailon.kr 로그인 ID (메일 주소 전체) | `your_id@mailon.kr` |
| `MAILON_PW` | mailon.kr 로그인 비밀번호 | (본인 비밀번호) |
| `MAILON_TOTP_SECRET` | Google OTP 등록 시 QR 코드 아래에 표시됐던 Base32 문자열 | `JBSWY3DPEHPK3PXP` |

⚠️ **`.env` 파일은 절대 git/클라우드에 올리지 말 것**. 이미 `.gitignore`에 포함되어 있음.

### 3단계: Python 가상환경 & 의존성 설치

처음 받았다면 가상환경을 만들고 의존성을 설치합니다.

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

설치가 끝나면 정상적으로 불러오는지 확인합니다.

```cmd
.venv\Scripts\python -c "import pyotp, dotenv; print('OK')"
```

`OK`가 출력되면 준비 완료입니다. 테스트까지 돌려보려면 `.venv\Scripts\pip install -r requirements-dev.txt`로 pytest도 설치하세요.

### 4단계: `agent-browser` 확인

```cmd
where agent-browser
```

출력이 `C:\Users\<사용자명>\AppData\Roaming\npm\agent-browser.CMD` 이런 식이면 OK.

없으면 설치:
```cmd
npm i -g agent-browser
agent-browser install
```

---

## 🔐 매번 로그인하는 방법

### 방법 A: 전체 자동 (권장)

```cmd
cd C:\Users\<사용자명>\Documents\mailon-backup
.venv\Scripts\python -m mailon.main login
```

**동작**:
1. 자동으로 mailon.kr 열기
2. 공지 팝업 자동 닫기
3. ID/비밀번호/TOTP 6자리 자동 입력 (TOTP는 `.env`의 시크릿으로 실시간 생성)
4. 로그인 클릭
5. `/mail`로 이동 확인

**소요 시간**: 약 **1~2분** (Chrome 데몬 첫 기동 포함)

**성공 표시**:
```
OK: https://mailon.kr/mail#xxxxxx
```

**실패 표시**:
```
FAIL: login did not leave /integrated/login. url=... snippet=...
```

### 방법 B: 단계별 수동 실행

각 단계를 따로 확인하고 싶을 때.

**B-1. TOTP 코드가 폰과 일치하는지 먼저 검증**

```cmd
.venv\Scripts\python -m mailon.main totp
```

출력 예:
```
TOTP code: 439265  (valid for 25s more)
```

지금 폰의 Google Authenticator 화면과 **같은 6자리**여야 합니다. 다르면 `.env`의 `MAILON_TOTP_SECRET`이 잘못된 것.

**B-2. 받은편지함 구조 덤프 (스크래퍼 튜닝용, 선택)**

```cmd
.venv\Scripts\python -m mailon.main probe
```

- 로그인 후 받은편지함까지 진입
- `logs/probe-YYYYMMDD-HHMMSS.html` 및 `-ax.txt`, `-url.txt` 생성
- 메일 행(row)의 HTML 구조 확인용

---

## 🛠️ 내부 동작 설명

### TOTP 코드 생성 원리

```
Base32 시크릿 (예: JBSWY3DPEHPK3PXP)
         ↓
HMAC-SHA1(시크릿, floor(현재시각 / 30초))
         ↓
하위 6자리 = TOTP 코드
```

- **폰의 Google Authenticator와 완전히 동일한 코드**가 같은 시각에 생성됨
- 폰에서 등록을 지울 필요 없음 (양쪽 병행 가능)
- Windows 시계가 NTP 동기화되어 있어야 함 (기본값)

### 로그인 타이밍

```
[0초]  agent-browser 데몬 기동 + Chrome 런치 (~13초)
[13초] DOM 로드 대기 (~25초, 백그라운드 리소스 포함)
[38초] 공지 팝업 닫기 (~3초)
[41초] TOTP 윈도우 여유 확인 (남은 시간이 10초 미만이면 다음 30초 윈도우 대기)
[~45초] ID/PW/OTP 입력 (~1초)
[~46초] 로그인 폼 제출 (서버 RSA 암호화 + 검증 ~30초)
[~77초] /mail로 이동 완료
```

**⚠️ TOTP 만료 방지**: 코드가 5초 이하 남았을 때 제출하면 네트워크 지연으로 서버에서 이미 다음 윈도우가 됨 → **로그인 실패(비밀번호 틀림과 동일한 메시지)**. 본 스크립트는 **10초 이상 남은 윈도우**에서만 제출함.

---

## ⚠️ 주요 실패 원인과 해결

### 1. "login did not leave /integrated/login"

서버가 로그인을 거부한 상태. 원인 3가지:

| 원인 | 확인 방법 | 해결 |
|---|---|---|
| 비밀번호 오타 | 브라우저로 직접 로그인 시도 | `.env`의 `MAILON_PW` 재입력 |
| TOTP 시크릿 오타 | `python -m mailon.main totp` 결과가 폰과 일치? | 일치 안 하면 `.env`의 `MAILON_TOTP_SECRET` 재확인 |
| 계정 잠김 | 잘못된 PW로 여러 번 시도한 상태 | 브라우저로 수동 로그인 성공 후 재시도 |

### 2. 시간 동기화 문제

```cmd
w32tm /resync
```

Windows 시계가 30초 이상 어긋나면 TOTP 실패.

### 3. agent-browser가 안 뜸

```cmd
agent-browser --session mailon-sync close
agent-browser doctor --fix
```

### 4. 세션 충돌

기존 세션이 남아있으면:
```cmd
agent-browser --session mailon-sync close
```

---

## 🧪 로그인 성공 후 확인

### 현재 브라우저 상태 확인

```cmd
agent-browser --session mailon-sync get url
agent-browser --session mailon-sync get title
```

예상 출력:
```
https://mailon.kr/mail#xxxxxx
Science MailON
```

### 받은편지함 스크린샷

```cmd
agent-browser --session mailon-sync screenshot inbox.png
```

### 헤드리스 해제(브라우저 창 보기)

`.env`에서:
```
HEADLESS=false
```
로 변경 후 다시 실행하면 브라우저 창이 화면에 뜸.

---

## 🗂️ 프로젝트 파일 구조

```
C:\Users\<사용자명>\Documents\mailon-backup\
├── .env                     ← 크레덴셜 (gitignored)
├── .env.example             ← 템플릿
├── .venv\                   ← Python 가상환경 (gitignored)
├── mailon\                  ← 자동화 코드
│   ├── config.py            .env 로더
│   ├── totp.py              TOTP 생성기
│   ├── browser.py           agent-browser 래퍼 (Popen + 스레드 드레인)
│   ├── login.py             로그인 플로우
│   ├── folders.py           폴더 UID 해석 (받은편지함/보낸편지함)
│   ├── scraper.py           메일 목록·상세·첨부 스크래퍼
│   ├── send.py              발송 오케스트레이션
│   ├── send_trigger.py      compose 폼 자동화
│   ├── send_verify.py       발송 후 보낸편지함 검증
│   ├── resolve.py           수신자 이름→이메일 조회
│   ├── state.py             SQLite 상태 DB
│   ├── writer.py            Markdown 저장
│   └── main.py              CLI 진입점
├── tests\                   ← 오프라인 유닛테스트 (53개)
├── docs\                    ← 기능별 상세 문서 (00~07)
├── data\                    ← 수집된 메일 저장소 (gitignored)
├── logs\                    ← 일자별 로그 + probe 덤프 (gitignored)
├── requirements.txt         ← 실행 의존성
├── requirements-dev.txt     ← 테스트 의존성 (pytest)
├── run_sync.bat             ← 작업 스케줄러가 호출
├── run_full_backup.bat      ← 전체 백업 수동 실행
├── monitor_backup.bat       ← 진행 상황 확인
├── register_task.ps1        ← 1시간마다 자동 실행 등록
├── LICENSE
├── README.md                ← 전체 프로젝트 문서
├── AGENTS.md                ← 개발자/AI 에이전트 지침
└── LOGIN_MANUAL.md          ← 이 파일
```

---

## 📊 CLI 명령 요약

| 명령 | 동작 | 필요 설정 |
|---|---|---|
| `python -m mailon.main totp` | 현재 TOTP 코드 출력 | `MAILON_TOTP_SECRET`만 |
| `python -m mailon.main login` | 전체 로그인 테스트 후 종료 | ID/PW/TOTP 모두 |
| `python -m mailon.main probe` | 로그인 후 받은편지함 HTML/AX/URL 덤프 | ID/PW/TOTP 모두 |
| `python -m mailon.main sync [--limit N] [--folders inbox,sent]` | 로그인 + 새 메일 수집 (기본: 받은편지함·보낸편지함 둘 다) | ID/PW/TOTP 모두 |
| `python -m mailon.main send --to ... --subject ... --body ... [--dry-run\|--confirm-send]` | 메일 작성/발송 | ID/PW/TOTP 모두 |
| `python -m mailon.main resolve --name <이름> [--json]` | 수신자 이름→이메일 조회 (읽기 전용) | ID/PW/TOTP 모두 |
| `python -m mailon.main status` | 저장된 DB 상태 보기 | 없음 |

각 명령의 옵션과 종료 코드는 [`README.md`](README.md)의 명령어 요약표와 `docs/` 하위 문서에 자세히 정리되어 있습니다.

---

## 🔄 자동화 범위

| 단계 | 상태 |
|---|---|
| TOTP 코드 생성 | 지원 |
| 로그인 페이지 open + 공지 팝업 닫기 | 지원 |
| ID/비밀번호/OTP 자동 입력 및 제출 | 지원 |
| 받은편지함 진입 + probe 덤프 | 지원 |
| 메일 목록·상세 스크래핑 | 지원 |
| 첨부파일 다운로드 (실패분 재시도 포함) | 지원 |
| Markdown 저장 + SQLite 증분 동기화 | 지원 |
| 보낸편지함 동기화 | 지원 |
| 메일 발송 (`send`) / 수신자 조회 (`resolve`) | 지원 |
| 작업 스케줄러 1시간 주기 실행 | 지원 |

로그인부터 수집·발송까지 전 구간이 자동화되어 있습니다. 다만 이 도구는 mailon.kr의 특정 시점 웹 UI 구조(DOM 셀렉터, 내부 JS 함수)에 의존하므로 **사이트가 개편되면 동작이 깨질 수 있습니다.** 그럴 때는 `probe` 명령으로 현재 DOM을 덤프해 셀렉터를 다시 맞춰야 합니다.

---

## 📞 참고: mailon.kr 로그인 API 내부

로그인 폼의 실제 동작 (`integratedLogin.js` 리버스엔지니어링 결과):

```
1. 사용자 입력 → ID, PW, OTP
2. POST /rsa.json      → 공개키 수신
3. ID/PW를 RSA로 암호화 (브라우저 JS)
4. POST /integrated/login
   Form data: {
     emailId: <RSA-encrypted>,
     pw: <RSA-encrypted>,
     authCode: <6-digit OTP>,
     CSRFToken: <token>,
     loginType: 'web'
   }
5. 응답 (성공):
   { "result": true,
     "redirectURL": "/mail",
     "twoFactorResult": true }
6. 응답 (실패):
   { "result": false,
     "msg": "아이디 또는 비밀번호를 다시 확인하세요." }
   ※ PW 틀림 / OTP 만료 / OTP 틀림 모두 같은 메시지로 통합됨
```

이 스크립트는 **위 플로우를 실제 브라우저로 실행**하므로 RSA 암호화·CSRF 토큰 등을 따로 구현할 필요 없이 mailon.kr의 자체 JS가 처리함.
