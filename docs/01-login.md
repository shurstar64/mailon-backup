# 01. 로그인 모듈

**파일**: `mailon/login.py` + `mailon/totp.py` + `mailon/browser.py`
**CLI**: `python -m mailon.main login`
**목적**: mailon.kr에 ID/PW/TOTP로 완전 자동 로그인

---

## 1. 작동 원리

### 1.1 TOTP 공유 비밀 방식

TOTP(RFC 6238)는 **시간 기반 일회용 비밀번호**입니다:

```
6자리 코드 = HMAC-SHA1(secret, floor(unix_time / 30))의 하위 6자리
```

- 같은 시크릿을 가진 모든 기기는 **같은 시각에 동일한 코드**를 만듭니다
- **폰의 Google Authenticator와 이 스크립트에 같은 시크릿을 공유해도 완벽 정상**
- 서버는 "이 코드가 해당 시각에 유효한가"만 검증 → 누가 계산했는지 모릅니다

### 1.2 mailon.kr 로그인 플로우

```
1. GET  https://mailon.kr/                     → /integrated/login로 리디렉트
2. POST /rsa.json                              → 서버 공개키 수신
3. (브라우저 JS) ID, PW를 RSA로 암호화
4. POST /integrated/login                       
   Form: emailId=<암호화>, pw=<암호화>, authCode=<6자리OTP>,
         CSRFToken=<토큰>, loginType=web
5. 응답:
   성공: {"result":true, "redirectURL":"/mail", "twoFactorResult":true}
   실패: {"result":false, "msg":"아이디 또는 비밀번호를 다시 확인하세요."}
6. /mail로 navigate
```

**핵심**: RSA 암호화/CSRF 토큰은 **mailon.kr 자체 JS가 처리**합니다. 우리는 ID/PW/OTP를 **input field에 값만 채우고** `login()` JS 함수만 호출하면 됩니다.

---

## 2. 코드 구조

### 2.1 `mailon/totp.py`

```python
def generate_code(secret: str, at: float | None = None) -> str:
    """현재(또는 주어진 시각의) 6자리 TOTP 코드."""

def seconds_until_next_code(at: float | None = None) -> int:
    """다음 30초 윈도우까지 남은 시간."""

def verify_code(secret: str, code: str, window: int = 1) -> bool:
    """디버깅용 코드 검증."""
```

### 2.2 `mailon/login.py`

```python
def login(browser: AgentBrowser, cfg: Config) -> None:
    """
    1. 로그인 URL로 navigate
    2. 공지 팝업 최대 3개 닫기 (find text "닫기" click)
    3. TOTP 윈도우 ≥10초 대기
    4. input[name="ipt-id"/ipt-pw/ipt-otp]에 값 채우기
    5. login() JS 함수 호출
    6. URL이 /mail로 바뀔 때까지 25초 대기
    """
```

### 2.3 `mailon/browser.py`

agent-browser CLI를 Popen + 스레드 드레인 방식으로 감쌉니다 (Windows pipe deadlock 회피).

---

## 3. 사용법

### 3.1 TOTP 코드 확인

```cmd
.venv\Scripts\python -m mailon.main totp
```

출력:
```
TOTP code: 439265  (valid for 25s more)
```

**이 6자리가 폰의 Google Authenticator 화면과 같아야 정상.** 다르면 시크릿이 틀린 것.

### 3.2 로그인 점검

```cmd
.venv\Scripts\python -m mailon.main login
```

성공 시:
```
OK: https://mailon.kr/mail#xxxxxxxxx
```

실패 시:
```
FAIL: login did not leave /integrated/login. url=... snippet=...
```

### 3.3 타임라인 (정상 실행 시)

```
[0초]   🔵 로그인 페이지 열기 (Chrome 데몬 첫 기동 ~13초)
[13초]  🔵 DOM 로드 완료
[13초]  🔵 공지 팝업 닫기 시도 (~25초 소요, networkidle 대기)
[38초]  🔵 TOTP 윈도우 체크
        └ 남은 시간 <10초면 다음 30초 대기 (최대 20초)
[45초]  🔵 TOTP 코드 생성 + ID/PW/OTP 입력
[46초]  🔵 login() JS 호출
[46초]  ⏳ 서버 RSA 암호화 + 2FA 검증
[77초]  ✅ /mail로 이동 완료
```

**총 1~2분** (데몬 재사용 시 30초대로 단축).

---

## 4. 주요 실패 원인과 해결

### 4.1 "아이디 또는 비밀번호를 다시 확인하세요"

**⚠️ 중요**: mailon.kr 서버는 ID 틀림 / PW 틀림 / **OTP 만료** 전부 같은 메시지로 응답합니다. 원인 판별 불가.

| 가능성 | 확인 방법 | 해결 |
|---|---|---|
| 비밀번호 틀림 | 브라우저로 직접 수동 로그인 시도 | `.env`의 `MAILON_PW` 재입력 |
| TOTP 시크릿 틀림 | `totp` 명령 출력을 폰과 비교 | `.env`의 `MAILON_TOTP_SECRET` 재확인 |
| **TOTP 만료** (가장 흔함) | `login` 실행 시 TOTP가 5초 이하 남아있던 경우 | 코드가 자동 대기함. 실패 시 재시도 |
| 계정 잠김 | 브라우저로 수동 로그인 에러 메시지 확인 | 관리자에게 연락 |

**본 스크립트의 방어책**: `login.py`가 TOTP 윈도우 ≥10초 남을 때까지 자동 대기합니다.

### 4.2 Windows 시계 편차

TOTP는 시간에 민감합니다. Windows 시계가 NTP 동기화되어 있는지 확인:

```cmd
w32tm /resync
w32tm /query /status
```

### 4.3 agent-browser 못 찾음 / hang

```cmd
agent-browser --session mailon-sync close
agent-browser doctor --fix
```

`run_sync.bat`이 `%APPDATA%\npm`을 PATH에 추가하므로 Task Scheduler 환경에서도 동작합니다.

### 4.4 브라우저 세션 좀비

```cmd
agent-browser --session mailon-sync close
```

실행 중 중단된 이전 세션이 있으면 새 세션과 충돌할 수 있습니다.

---

## 5. 보안

### 5.1 절대 로그에 기록되지 않는 것

`login.py` + `browser.py`가 다음을 **절대 로그로 남기지 않습니다**:
- 비밀번호 값 (길이만: `len(pw)=9`)
- TOTP 코드 값 (길이만: `length=6`)
- RSA 암호화 페이로드

### 5.2 `.env` 파일 보호

- `.gitignore`에 포함되어 있어 git에 올라가지 않음
- Windows 파일 시스템에서 **본인만 읽기 권한** 설정 권장:
  ```cmd
  icacls .env /inheritance:r /grant:r "%USERNAME%:R"
  ```

### 5.3 브라우저 세션 격리

- `--session mailon-sync` 전용 프로필 사용
- 다른 agent-browser 사용이나 평소 Chrome 프로필과 분리됨

---

## 6. 코드 세부 구현

### 6.1 왜 CSS selector, `@eN` ref가 아닌가

초기에는 agent-browser의 `@e17`/`@e18`/`@e19` 같은 accessibility ref를 썼습니다. 하지만:

- ref는 **snapshot 직후만 유효**. DOM이 조금만 바뀌어도 무효
- CSS selector (`input[name="ipt-id"]`)는 **HTML name 속성에 기반**해 안정적

### 6.2 왜 버튼 클릭이 아닌 `login()` JS 호출

- SMS 공지 팝업이 로그인 버튼을 **시각적으로 가리는 경우**가 있어 click이 실패
- `login()` 함수를 직접 호출하면 오버레이 무관하게 제출

```python
browser.eval_js("typeof login === 'function' ? (login(), 'ok') : 'no-login-fn'")
```

### 6.3 TOTP 윈도우 여유 10초

5초로 설정했다가 실제 로그인이 실패하는 사례 발견:
- TOTP 생성 → RSA 암호화 → POST → 서버 검증 사이에 **수 초** 소요
- 네트워크 지연까지 포함하면 남은 시간 5초는 부족
- **10초**로 상향 (검증 완료)

---

## 7. FAQ

**Q. 폰에서 Google Authenticator를 지워도 되나요?**
A. 아니요. 지우지 마세요. 같은 시크릿을 여러 곳에서 동시 사용 가능합니다 (TOTP 설계상 정상).

**Q. OTP 재설정하면 이 스크립트도 다시 설정해야 하나요?**
A. 네. `.env`의 `MAILON_TOTP_SECRET`을 새 시크릿으로 업데이트해야 합니다.

**Q. 매 1시간마다 실패 없이 로그인 되나요?**
A. 제대로 설정되었다면 거의 100% 성공. 드물게 실패 시 다음 시간 크론잡이 자동 재시도합니다.

**Q. `totp` 명령만 자주 실행해서 계정 잠김?**
A. 아니오. `totp` 명령은 **코드 생성만** 하고 서버에는 접근하지 않습니다.

**Q. 헤드 모드로 보면서 디버깅 가능?**
A. 네. `.env`에서 `HEADLESS=false` 설정 후 실행하면 Chrome 창이 보입니다.

---

## 8. 관련 문서

- [AGENTS.md §1.4](../AGENTS.md) - mailon.kr 특유 제약
- [docs/02-scraper.md](02-scraper.md) - 로그인 후 메일 수집
- [LOGIN_MANUAL.md](../LOGIN_MANUAL.md) - 사용자 빠른 가이드
