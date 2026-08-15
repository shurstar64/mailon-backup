# Mailon 기관메일 백업 설정 가이드

mailon.kr 기관메일을 로컬 마크다운 파일로 백업하는 완전한 설정 및 사용 가이드입니다.

## 목차

1. [사전 요구사항](#1-사전-요구사항)
2. [저장소 클론 및 설치](#2-저장소-클론-및-설치)
3. [환경 설정 (.env)](#3-환경-설정-env)
4. [TOTP 비밀키 추출](#4-totp-비밀키-추출)
5. [agent-browser 설치](#5-agent-browser-설치)
6. [Chrome CDP 모드 설정](#6-chrome-cdp-모드-설정)
7. [백업 실행](#7-백업-실행)
8. [자동화 스크립트](#8-자동화-스크립트)
9. [문제 해결](#9-문제-해결)

---

## 1. 사전 요구사항

### 필수 소프트웨어

- **Python 3.10+**: [python.org](https://www.python.org/downloads/)에서 다운로드
- **Node.js 18+**: [nodejs.org](https://nodejs.org/)에서 다운로드
- **Git**: [git-scm.com](https://git-scm.com/)에서 다운로드
- **Google Chrome**: 시스템에 설치된 Chrome 브라우저

### 필수 정보

- mailon.kr 로그인 ID (이메일 주소)
- mailon.kr 비밀번호
- TOTP 인증 비밀키 (2단계 인증용)

---

## 2. 저장소 클론 및 설치

### 2.1 저장소 클론

```bash
# 홈 디렉토리로 이동
cd ~

# 저장소 클론
git clone https://github.com/orientpine/mailon-backup.git

# 프로젝트 디렉토리로 이동
cd mailon-backup
```

### 2.2 Python 의존성 설치

```bash
pip install -r requirements.txt
```

설치되는 패키지:
- `pyotp>=2.9.0` - TOTP 코드 생성
- `python-dotenv>=1.0.1` - 환경 변수 관리
- `beautifulsoup4>=4.12.3` - HTML 파싱
- `lxml>=5.2.0` - XML/HTML 처리

---

## 3. 환경 설정 (.env)

### 3.1 환경 파일 생성

```bash
# 예제 파일 복사
cp .env.example .env
```

### 3.2 .env 파일 편집

`.env` 파일을 열어 다음 값들을 설정:

```env
# mailon.kr 로그인 정보
MAILON_ID=your-email@kimm.re.kr
MAILON_PW=your-password

# TOTP 비밀키 (Base32 형식, 아래 섹션 참조)
MAILON_TOTP_SECRET=YOUR_BASE32_SECRET

# 브라우저 설정
HEADLESS=false

# Chrome CDP 포트 (Windows 필수)
AGENT_BROWSER_CDP_PORT=9222
```

---

## 4. TOTP 비밀키 추출

mailon.kr은 2단계 인증(TOTP)을 사용합니다. Google Authenticator 등에서 비밀키를 추출해야 합니다.

### 4.1 Google Authenticator에서 내보내기

1. Google Authenticator 앱 열기
2. 우측 상단 메뉴 (⋮) → **계정 내보내기**
3. mailon 계정 선택
4. QR 코드 생성됨

### 4.2 QR 코드에서 비밀키 추출

QR 코드를 스캔하면 다음과 같은 URL이 나옵니다:
```
otpauth-migration://offline?data=Ch8KCja%2BDAZt29OuVFASC0tJTU0g66mU7J28IAEoATACEAIYASAA
```

이 URL은 protobuf로 인코딩되어 있습니다. 실제 Base32 비밀키를 추출해야 합니다.

### 4.3 온라인 디코더 사용

1. [https://alexbakker.me/post/parsing-google-auth-export-qr-codes.html](https://alexbakker.me/post/parsing-google-auth-export-qr-codes.html) 방문
2. QR 코드 데이터 입력
3. Base32 형식의 비밀키 확인

### 4.4 비밀키 형식 확인

올바른 Base32 비밀키:
- 대문자 A-Z와 숫자 2-7만 포함
- 예: `G27AYBTN3PJ24VCQ`

잘못된 형식 (URL 인코딩/protobuf):
- `%2B`, `%3D` 등의 문자 포함
- 예: `Ch8KCja%2BDAZt29OuVFAS...`

### 4.5 TOTP 테스트

```bash
cd ~/mailon-backup
python -m mailon.main totp
```

출력 예시:
```
TOTP code: 857065  (valid for 26s more)
```

휴대폰 앱의 코드와 일치하면 설정 완료입니다.

---

## 5. agent-browser 설치

### 5.1 npm으로 설치

```bash
npm i -g agent-browser
```

### 5.2 브라우저 설치

```bash
agent-browser install
```

이 명령은 Chrome for Testing을 다운로드합니다.

### 5.3 설치 확인

```bash
agent-browser doctor
```

---

## 6. Chrome CDP 모드 설정

### Windows에서의 문제점

Windows에서 `agent-browser`가 자체적으로 Chrome을 실행할 때 문제가 발생할 수 있습니다:
- Chrome이 정상적으로 시작되지 않음
- DevToolsActivePort 파일 생성 실패

### 해결책: 수동 Chrome CDP 실행

시스템에 설치된 Chrome을 직접 CDP 모드로 실행합니다.

### 6.1 Chrome 수동 실행 (명령줄)

**PowerShell 또는 CMD:**
```cmd
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%USERPROFILE%\.agent-browser\mailon-sync-profile" ^
  --no-first-run ^
  --no-default-browser-check ^
  "about:blank"
```

**Git Bash:**
```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.agent-browser/mailon-sync-profile" \
  --no-first-run \
  --no-default-browser-check \
  "about:blank"
```

### 6.2 Chrome 실행 확인

```bash
curl -s http://localhost:9222/json/version
```

정상 출력:
```json
{
   "Browser": "Chrome/151.0.7922.138",
   "Protocol-Version": "1.3",
   ...
}
```

### 6.3 환경 변수 설정

`.env` 파일에 CDP 포트 추가:
```env
AGENT_BROWSER_CDP_PORT=9222
```

---

## 7. 백업 실행

### 7.1 사전 확인

1. Chrome이 CDP 모드로 실행 중인지 확인
2. `.env` 파일 설정 완료 확인

### 7.2 로그인 테스트

```bash
cd ~/mailon-backup
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main login
```

성공 출력:
```
OK: https://mailon.kr/mail#zOTPDzj...
```

### 7.3 메일 동기화

```bash
# 10개 메일 동기화
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 10

# 100개 메일 동기화
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 100

# 1000개 메일 동기화
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 1000
```

### 7.4 상태 확인

```bash
python -m mailon.main status
```

출력 예시:
```
Saved mails: 10000
Sent mails: 0
Last run #12: status=ok new=3349 started=2026-08-15T23:05:01 finished=2026-08-15T23:32:13
```

### 7.5 백업된 파일 위치

```
mailon-backup/
├── data/
│   ├── mails/           # 백업된 메일 (마크다운)
│   │   └── 2026/
│   │       └── 08/
│   │           └── 2026-08-15_메일제목_12345.md
│   ├── attachments/     # 첨부파일
│   └── state.db         # 동기화 상태 DB
└── logs/                # 로그 파일
```

---

## 8. 자동화 스크립트

### 8.1 mailon_cdp.bat (범용 명령 래퍼)

Chrome을 자동으로 시작하고 mailon 명령을 실행합니다.

**파일 위치:** `mailon-backup/mailon_cdp.bat`

**사용법 (Windows CMD):**
```cmd
mailon_cdp.bat totp          # TOTP 코드 생성
mailon_cdp.bat login         # 로그인 테스트
mailon_cdp.bat status        # 상태 확인
mailon_cdp.bat sync --limit 100   # 100개 동기화
```

### 8.2 run_sync_cdp.bat (전체 동기화)

Chrome 시작, 동기화, 상태 확인을 한 번에 실행합니다.

**파일 위치:** `mailon-backup/run_sync_cdp.bat`

**사용법 (Windows CMD):**
```cmd
run_sync_cdp.bat         # 기본 50개 동기화
run_sync_cdp.bat 100     # 100개 동기화
run_sync_cdp.bat 1000    # 1000개 동기화
```

### 8.3 Git Bash에서 실행

Git Bash에서는 배치 파일 대신 직접 명령 사용:

```bash
cd ~/mailon-backup
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 100
```

---

## 9. 문제 해결

### 9.1 TOTP 코드 불일치

**증상:** `binascii.Error: Non-base32 digit found`

**원인:** TOTP 비밀키가 올바른 Base32 형식이 아님

**해결:**
1. 비밀키에 `%`, `+`, `/` 등이 포함되어 있는지 확인
2. 섹션 4 참조하여 올바른 Base32 키 추출

### 9.2 Chrome 실행 실패

**증상:** `Chrome exited early without writing DevToolsActivePort`

**원인:** Chrome이 정상적으로 시작되지 않음

**해결:**
1. 실행 중인 Chrome 프로세스 모두 종료
2. 새 사용자 데이터 디렉토리로 Chrome 수동 실행
3. CDP 포트 확인: `curl http://localhost:9222/json/version`

### 9.3 로그인 실패 - Element not found

**증상:** `Element not found: input[name="ipt-id"]`

**원인:** 브라우저가 이미 로그인된 상태

**해결:** 이미 로그인된 경우 정상 동작. 그냥 sync 명령 실행.

### 9.4 HTTP 404 오류

**증상:** `fetch_mail_html failed (404)`

**원인:** 브라우저가 mailon.kr에서 다른 페이지로 이동함

**해결:**
1. Chrome 종료 후 새로 시작
2. 새 프로필로 시작: `--user-data-dir` 경로 변경
3. 동기화 limit 줄여서 실행

### 9.5 첨부파일 다운로드 실패

**증상:** `TypeError: Failed to fetch`

**원인:** 네트워크 오류 또는 파일 접근 불가

**해결:**
- 일시적 오류는 다음 sync 시 자동 재시도
- 지속적 실패는 해당 파일이 서버에서 삭제되었을 수 있음

### 9.6 세션 만료

**증상:** 동기화 중 갑자기 실패

**원인:** mailon 세션 타임아웃

**해결:**
1. Chrome에서 mailon.kr 페이지 새로고침
2. 다시 sync 명령 실행 (자동 재로그인)

---

## 부록: 명령어 요약

| 명령어 | 설명 |
|--------|------|
| `python -m mailon.main totp` | TOTP 코드 생성 |
| `python -m mailon.main login` | 로그인 테스트 |
| `python -m mailon.main status` | 동기화 상태 확인 |
| `python -m mailon.main sync --limit N` | N개 메일 동기화 |
| `python -m mailon.main probe` | 디버깅용 페이지 덤프 |

---

## 부록: 전체 백업 예시

```bash
# 1. Chrome CDP 모드로 시작
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.agent-browser/mailon-sync-profile" \
  --no-first-run "about:blank" &

# 2. Chrome 준비 대기
sleep 5

# 3. 프로젝트 디렉토리로 이동
cd ~/mailon-backup

# 4. 로그인 테스트
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main login

# 5. 전체 백업 (단계별)
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 1000
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 2000
AGENT_BROWSER_CDP_PORT=9222 python -m mailon.main sync --limit 5000

# 6. 상태 확인
python -m mailon.main status
```

---

## 작성 정보

- **작성일:** 2026-08-16
- **테스트 환경:** Windows 10/11, Python 3.10+, Chrome 151+
- **백업 결과:** 10,000개 메일 성공적 백업
