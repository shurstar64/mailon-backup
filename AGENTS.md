# AGENTS.md — 이 프로젝트에서 작업하는 AI 에이전트를 위한 지침

**프로젝트명**: mailon-backup (mailon.kr 자동 이메일 백업 + 증분 동기화 시스템)
**플랫폼**: Windows 10/11
**언어**: Python 3.13, PowerShell, Batch
**브라우저 자동화**: `agent-browser` (CDP 기반 Chrome 자동화 CLI)

---

## 0. 이 문서의 목적

이 저장소에서 작업하는 **모든 AI 에이전트** (Claude, GPT, 기타)는 작업 시작 전 이 문서를 **반드시** 읽어야 한다. 이유:

1. 본 프로젝트는 **민감 정보 (mailon.kr 계정 크레덴셜)** 를 다룬다
2. Windows 환경에서 `subprocess` / `.cmd` / `PowerShell` 상호작용에 **알려진 함정**이 있다
3. mailon.kr은 KISTI 공공기관 서비스이므로 **자동화는 개인 사용 범위**로 한정해야 한다
4. 구현 패턴(CSS selector vs `@eN` ref, Popen 드레인 등)이 **검증된 결과**이므로 재발명하지 말 것

---

## 1. 핵심 원칙 (MUST FOLLOW)

### 1.1 비밀 정보 처리

- `.env` 파일은 **절대 git/클라우드에 업로드 금지**. `.gitignore`에 이미 등재됨
- 시크릿 동기화 전략 (사용자 확정, 2026-07): **패스워드 매니저 + `.env.example` 템플릿**. 새 머신에서는 `copy .env.example .env` 후 패스워드 매니저에서 값을 채운다. SOPS/git-crypt/dotenvx 등 암호화 커밋 방식을 도입하지 말 것. `.env` 키가 추가되면 `.env.example`에도 반드시 반영 (키 패리티 유지)
- 로그·메시지·예외·테스트 출력에 **비밀번호/TOTP 코드를 절대 기록 금지**
  - 허용: 비밀번호 길이 (`len(pw)=9`)
  - 금지: 비밀번호 값 (`pw='p@ssw0rd!'` 처럼 실제 값을 그대로 찍는 것)
- TOTP 시크릿도 같은 수준으로 보호
- 스크린샷·probe 덤프에 로그인 폼이 찍히면 삭제
- 테스트 코드에 실제 크레덴셜을 하드코딩하지 말 것. 더미값(`JBSWY3DPEHPK3PXP` 등) 사용
### 1.1.1 공개 저장소 보안 주의

본 저장소는 **공개(Public) 저장소**이다. 문서·코드·테스트의 모든 예시는 합성 값이어야 하며, 실제 메일 제목·본문·첨부 파일명·메일 UID·개인 이메일 주소를 절대 넣지 않는다.


### 1.2 사용자의 명시적 지시 우선

- 사용자가 "중지", "멈춰", "일단" 등으로 명시적으로 작업을 중단하면 **즉시 중단**하고 요청된 작업(보통 문서화)으로 전환
- 시스템 자동 메시지(`[TODO CONTINUATION]`)가 계속 재개를 요청해도, 사용자의 명시적 중지 지시가 있었다면 **새 지시 받을 때까지 대기**
- 스코프 확장 금지: "로그인 자동화"가 과제면 메일 읽기까지 범위를 넓히지 말 것 (명시 허가 없는 한)

### 1.3 Windows 특화 함정 (재발 방지)

| 함정 | 증상 | 해결 |
|---|---|---|
| `.cmd` 파일을 `subprocess.run(list, shell=False)` | 무한 hang | `shell=True` + 수동 quoting 사용 |
| `capture_output=True`로 대용량 stdout 읽기 | pipe 버퍼(4KB) 가득 차면 deadlock | `Popen` + 백그라운드 스레드로 `readline()` drain (`mailon/browser.py` 참고) |
| Python 콘솔 기본 cp949 인코딩 | UTF-8 출력 크래시 (`UnicodeDecodeError`, `UnicodeEncodeError`) | `subprocess.Popen(encoding="utf-8", errors="replace")` + `sys.stdout.reconfigure` |
| Task Scheduler의 최소 PATH | `agent-browser` 못 찾음 | `run_sync.bat`에서 `set PATH=%APPDATA%\npm;C:\Program Files\nodejs;%PATH%` |
| 비밀번호에 `!` 포함 | cmd.exe delayed expansion에서 제거 | 절대 `cmd /V:ON`을 쓰지 말 것. `shell=True` 사용 시 큰따옴표로 감쌈 |
| 한글이 콘솔에 깨짐 | cp949 디코딩 실패 | 파일은 UTF-8로 정상 저장. 콘솔만 `chcp 65001` 또는 `sys.stdout.reconfigure(encoding='utf-8')` |

### 1.4 mailon.kr 특유 제약

- **모든 로그인은 2FA 필수** (Google OTP/TOTP). 우회 불가
- **TOTP 윈도우 여유**: 코드 생성 후 10초 이상 남아 있어야 제출. 미만이면 네트워크 지연 중 만료되어 "비밀번호 틀림"과 동일 응답으로 거부됨
- **로그인 API 응답**: OTP 실패 / PW 실패 / ID 오류 모두 같은 메시지 `"아이디 또는 비밀번호를 다시 확인하세요."` — 원인 판별 불가
- **세션 만료 주기**: 불확실. 공공기관 보안 가이드라인상 수 시간~수십일. 따라서 **매번 새로 로그인**이 안정적
- **받은편지함 UID**: `data-id="1000001"` 속성이 실제 메일 UID
- **메일 상세 열기**: `_view.initView({mailUid, folderUid, folderType, authWrite})` JS 함수 호출
- **UI 기술 스택**: Crinity G-Cloud + jQuery + 자체 서버(NST) + RSA 암호화 + CSRF 토큰

### 1.5 구현 패턴 선택 (검증됨)

- **브라우저 요소 조작**: `@eN` ref가 아닌 **CSS selector** 사용 (`input[name="ipt-id"]`)
  - 이유: ref는 snapshot 직후만 유효하고 DOM 변경 시 무효화됨
  - 예외: ref가 임시적 오버레이 메뉴 같은 동적 요소에만 붙어 있을 때
- **로그인 submit**: 버튼 클릭보다 `login()` JS 함수 직접 호출이 안정적
  - 이유: 팝업 오버레이가 로그인 버튼을 가리는 경우가 있음
- **네트워크 응답 캡처**: `$.ajax` 몽키패치로 success/error 콜백 가로채기 (`logs/probe-*` 참고)
- **페이지 로드 대기**: `networkidle` 대신 `domcontentloaded` 선호 (mailon은 백그라운드 polling이 있어 networkidle이 영원히 안 옴)


### 1.6 발송(send) 경로 함정 (검증됨)

W4-2 triage 게이트가 승인 초안을 `python -m mailon.main send`로 실발송하는 경로에서
확인된 함정들. 전부 승인된 초안의 실발송이 종료 코드 2로 반복 실패하는 원인이었다. 재발명하지 말 것.

|| 함정 | 증상 | 해결 |
|---|---|---|
| `window._compose.beforeSend()` 직접 호출 | 메일이 실제로 발송되지 않음 — PRE-send 훅일 뿐 (자가시험 미도착, 757개 동기화 메일 중 0건) | 사람과 동일하게 compose 폼의 **보내기 버튼 클릭** — 전체 핸들러 체인(검증→beforeSend→실제 submit) 실행 (`mailon/send_trigger.py` 참고) |
| 자동화 환경에서 웹에디터 엔진 사망 | `mail_editor.setContent()`가 no-op, `getContent()`가 `undefined`/빈값 — `getForm()`이 `param.content = mail_editor.getContent()`로 본문을 수집하므로 발신 본문 undefined | setContent 시도 후 readback 검증, 죽어 있으면 **`ed.getContent`를 직접 override**해 결정론적 본문 주입 (`mailon/send_trigger.py` 참고) |
| 콤마 결합 다중 수신자 일괄 입력 | 수신자 chip 0개 등록 → getForm 수신자 0명 → 폼 게이트 거부 | 주소를 **한 명씩 Enter 토큰화**로 입력 (`#adr-to-ipt_ta`/`#adr-cc-ipt_ta`) |
| triage가 `--to "a, b, c"` 한 줄 결합 문자열 전달 | 단일 수신자로 오인 | `_split_addresses()`가 콤마/세미콜론 분리 정규화 — 반복 플래그도 수용 (`mailon/main.py` 참고) |
| 발송 검증의 ALL-recipient 매칭 | 목록 뷰(list_async)가 다중 수신자 메일의 **첫 수신자만** 표시 → 성공 발송을 실패로 오판 → 재시도 루프 | **ANY-recipient** + 정확 제목 + 시간창 매칭 (`mailon/send_verify.py` 참고) |
| 발송이 `mailon-sync` 브라우저 세션 공유 | 동시 실행 중인 sync가 죽음 | 발송 전용 격리 세션 `-send` 접미사 |

- **fail-closed 안전망**: 발송 직전 `getForm()`의 실제 발신 파라미터(본문/수신자)를 검증하고 불일치 시 발송을 거부한다. 위 함정들이 "잘못된 메일이 조용히 나가는" 대신 rc=2 거부로 표면화된 것은 이 게이트가 의도대로 동작한 결과다.
- **잔여 취약점**: `window.mail_editor`·`#adr-to-ipt_ta` 등은 MailOn SPA 내부에 강결합 —
  사이트 개편 시 재발 가능. 인브라우저 JS라 오프라인 테스트 불가(에디터 override는 테스트 0,
  `_split_addresses`/ANY-match는 `tests/test_offline.py` 커버).

---

## 2. 폴더 구조 & 역할

```
mailon-backup/
├── AGENTS.md               ← 이 파일 (에이전트 지침)
├── LOGIN_MANUAL.md         ← 사용자 대상 로그인 매뉴얼 (한국어)
├── README.md               ← 프로젝트 소개
├── LICENSE                 ← MIT 라이선스
├── docs/                   ← 기능별 사용자 매뉴얼 (한국어)
│   ├── 00-architecture.md  시스템 전체 아키텍처
│   ├── 01-login.md         로그인 모듈
│   ├── 02-scraper.md       받은편지함 스크래퍼
│   ├── 03-backup.md        전체 백업 + 증분 동기화
│   ├── 04-cronjob.md       Windows Task Scheduler 등록
│   ├── 05-send.md          메일 작성/발송 (send)
│   ├── 06-resolve.md       수신자 이름→이메일 조회 (resolve)
│   └── 07-security.md      보안 및 비밀 관리 지침
├── mailon/                 ← Python 패키지
│   ├── __init__.py
│   ├── config.py           .env 로더 (load_config, load_totp_secret)
│   ├── totp.py             TOTP 생성 (pyotp 래퍼)
│   ├── browser.py          agent-browser CLI 래퍼 (Popen 드레인 포함)
│   ├── login.py            로그인 플로우 (CSS 셀렉터 + login() JS 호출)
│   ├── scraper.py          메일 리스트/상세/첨부 추출
│   ├── folders.py          폴더 UID 해석 (cross-frame 하베스트, inbox/sent)
│   ├── send.py             발송 오케스트레이션 (send_trigger+send_verify 결합)
│   ├── send_trigger.py     compose 폼 자동화 (본문 주입·수신자 토큰화·getForm 게이트·보내기 클릭)
│   ├── send_verify.py      발송 후 보낸편지함 검증 (ANY-recipient 매칭)
│   ├── resolve.py          수신자 이름→이메일 자동완성 해석 (read-only, -resolve 세션)
│   ├── state.py            SQLite 상태 DB (증분 동기화, 중복 방지)
│   ├── writer.py           Markdown 파일 작성 (YAML front-matter)
│   └── main.py             CLI 진입점 (totp/login/probe/sync/send/resolve/status)
├── tests/
│   └── test_offline.py     유닛 테스트 (네트워크/크레덴셜 없이 실행 가능)
├── data/                   ← gitignored
│   ├── mails/YYYY/MM/*.md  수집된 메일 (Markdown)
│   ├── attachments/UID/    첨부파일
│   └── state.db            SQLite 중복 방지 DB
├── logs/                   ← gitignored
│   ├── sync-YYYY-MM-DD.log 일자별 실행 로그
│   └── probe-*.html        받은편지함 구조 덤프
├── .env                    ← gitignored. 크레덴셜
├── .env.example            ← 템플릿
├── .gitignore
├── requirements.txt
├── requirements-dev.txt    ← 개발 및 테스트용 의존성
├── run_sync.bat            ← Task Scheduler가 호출 (증분 동기화)
├── run_full_backup.bat     ← 전체 백업 실행 배치
├── monitor_backup.bat      ← 백업 상태 모니터링 배치
├── register_task.ps1       ← 스케줄러 등록 스크립트
└── pyrightconfig.json      ← LSP 설정
```

---

## 3. 작업 재개 시 체크리스트

새 세션을 시작할 때 다음을 순서대로 확인:

1. **pyrightconfig.json**의 `venv`가 `.venv`를 가리키는지 확인
2. **`.env` 존재 여부**: 없으면 `cp .env.example .env` 후 사용자에게 크레덴셜 요청
3. **agent-browser 건강 상태**: `agent-browser doctor --offline --quick`
4. **기존 세션 정리**: `agent-browser --session mailon-sync close` (좀비 세션 제거)
5. **offline 테스트 통과**: `python -m tests.test_offline` 또는 `python -m pytest tests/` → 53/53 pass
6. **로그인 smoke test**: `.venv/Scripts/python -m mailon.main login` → `OK: https://mailon.kr/mail#...`

---

## 4. 금지 행위 (HARD RULES)

- **계정 잠금 유발**: 비밀번호 brute-force 시도 금지. 실패 3회 후 중단
- **서비스 남용**: 짧은 간격(<10분) 반복 크론잡 설정 금지
- **다른 사용자 메일 수집**: 본 계정 외 접근 절대 금지
- **비밀 정보 노출**:
  - `--headed` 모드로 브라우저 띄운 스크린샷을 로그에 포함 금지 (ID 보임)
  - 에러 트레이스에 환경변수 dump 금지
- **테스트 삭제**: 실패하는 테스트를 삭제해서 "통과"시키지 말 것. 코드를 고치거나 테스트를 고치거나
- **의존성 추가**: `requirements.txt`에 추가할 때 **minimum version만** 지정, maximum 지정 금지 (유지보수 지옥)
- **mailon.kr 내부 API 직접 호출**: RSA 암호화 수동 재구현 금지. 반드시 브라우저 JS에 맡길 것
- **세션 쿠키 덤프**: state save는 로컬 디버깅용. 메일 수집 자동화에 세션 재사용 금지 (만료 판별 복잡)

---

## 5. 코드 스타일 & 규약

### 5.1 Python

- **타입 힌트**: 모든 public 함수에 필수. Pyright `basic` 모드 통과
- **Docstring**: public 함수/클래스는 최소 한 줄 설명
- **에러 처리**: 무분별한 `try/except Exception`보다 구체 예외
- **로그 레벨**:
  - `DEBUG`: 내부 명령 raw 출력
  - `INFO`: 주요 상태 전이 (로그인 시작, 메일 n개 발견, …)
  - `WARNING`: 비치명적 이상 (팝업 닫기 실패 → 스킵)
  - `ERROR`: 실행 중단 유발
- **dataclass 우선**: 단순 데이터 보관은 `@dataclass(frozen=True)`
- **f-string**: `%` 포맷 금지. `f"..."` 또는 `.format`
- **경로**: `pathlib.Path` 항상. 문자열 경로 금지

### 5.2 셀렉터 우선순위

1. `input[name="..."]` (가장 안정)
2. `#id-selector`
3. `[data-*]` 속성
4. 텍스트 기반 (`find text "..." click`)
5. **마지막 수단**: `@eN` ref (snapshot 직후 즉시 사용)

### 5.3 커밋 규칙

- 이 저장소는 **단일 초기 커밋**으로 시작한다. `.env`가 `.gitignore`에 있는지 항상 확인한다.
- 커밋 메시지 규약: `<scope>: <summary>` (예: `login: wait TOTP window ≥10s before submit`)
- 비밀이 커밋되면 크레덴셜을 회전한 뒤 `git filter-repo`로 제거한다.

---

## 6. 테스트 지침

### 6.1 오프라인 테스트 (`tests/test_offline.py`)

- 네트워크 / 크레덴셜 없이 실행
- 모든 `AgentBrowser` 호출은 MagicMock 또는 FakeBrowser 사용
- 매 PR 전 53/53 pass 확인: `python -m tests.test_offline` (표준 라이브러리) 또는 `python -m pytest tests/ -v` (`pip install -r requirements-dev.txt` 필요)

### 6.2 수동 smoke test

```
totp           → 폰과 일치하는 6자리 출력
login          → "OK: https://mailon.kr/mail#..."
probe          → logs/probe-*.html, probe-*-ax.txt 생성
sync --limit 3 → data/mails/에 3개 .md 생성
status         → "Saved mails: 3. Last run #1: status=ok..."
```

`resolve --name <이름> [--json]` = 수신자 이름→이메일 자동완성 해석 (read-only): compose 자동완성 그리드 조회만 수행하고 발송 트리거는 호출하지 않으며, 자체 브라우저 세션(`-resolve` 접미사)을 사용한다.

### 6.3 라이브 테스트 주의

- 매 테스트마다 로그인하면 **계정 잠금 위험** (mailon.kr 정책상 n회 연속 실패로 잠금 가능)
- 실패하면 다음 테스트까지 최소 30초 대기
- OTP는 30초 윈도우마다 1회만 유효 → 연속 테스트는 1분 간격 권장

---

## 7. 문제 발생 시 순서

1. **`logs/sync-YYYY-MM-DD.log`** 확인 (tail -50)
2. **`agent-browser --session mailon-sync get url`** 로 브라우저 상태 확인
3. **`agent-browser --session mailon-sync screenshot logs/debug.png`**
4. **`agent-browser --session mailon-sync snapshot -i | head -40`**
5. **`python -m mailon.main probe`** 로 HTML 덤프
6. 그래도 모르면 사용자에게 보고 (추측 금지)

---

## 8. 향후 확장 가능한 지점

- **다중 폴더 지원**: 받은편지함 + **보낸편지함(2026-07-20 지원)** — `mailon/folders.py`가 cross-frame 하베스트/allFolder 역산으로 folderUid를 해석하고 `sync --folders inbox,sent`(기본 둘 다)로 동기화한다. 휴지통/사용자 폴더 확장 시 같은 패턴(folders.resolve_folder_uid + InboxScraper folder_label)을 재사용
- **Gmail IMAP 연동**: mailon.kr이 포워딩을 허용하면 IMAP 경로가 훨씬 안정적 (미구현 아이디어)
- **알림**: 신규 메일 도착 시 Slack/Telegram 봇 → `mailon/writer.py` write hook 추가 (미구현 아이디어)
- **AI 요약**: 수집된 Markdown을 월별로 LLM 요약 → `docs/05-ai-summary.md` (미구현 아이디어)
- **타 Crinity 사이트**: 스키마가 유사하므로 `mailon/login.py`에 `site` 인자 추가로 재활용 가능

---

## 9. 참조 — 외부 서비스

- **mailon.kr**: https://mailon.kr/
- **운영**: KISTI 통합운영센터 (mailon@kisti.re.kr) (공공 정보)
- **플랫폼**: Crinity G-Cloud (https://crinity.com)
- **TOTP 표준**: RFC 6238
- **agent-browser**: https://www.npmjs.com/package/agent-browser (CDP 브라우저 자동화 CLI)

---

## 10. 이 문서 유지보수

- 새 함정을 발견하면 §1.3 테이블에 추가
- 새 기능을 구현하면 §2 폴더 구조에 반영 + `docs/XX-name.md` 추가
- `CHANGELOG.md`를 만들지 말 것 — 변경 이력은 git log에 맡김
- 이 문서는 **한국어**로 유지 (사용자 편의)

## 11. 문서 갱신 규칙 (2026-07-20, 소유자 지시)

**코드 작업은 관련 문서 갱신까지 마쳐야 종결이다.** 기능을 추가/변경/제거하면 같은 커밋 사이클에서 AGENTS.md와 docs/의 낡아지는 문구를 함께 고친다. 에이전트는 이 문서들을 사실 근거로 답하므로, 낡은 문서는 곧 소유자에게 잘못된 안내가 된다(선례: 2026-07-20 보낸메일함 동기화가 배포됐는데 본 문서의 "받은편지함만" 문구 때문에 기능이 없다고 잘못 안내함).
