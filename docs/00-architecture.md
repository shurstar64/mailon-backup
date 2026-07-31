# 전체 아키텍처

이 문서는 mailon.kr 자동 이메일 백업 및 증분 동기화 시스템의 전체 구조와 데이터 흐름을 설명합니다.

## 1. 데이터 흐름 (Data Flow)

시스템은 매 실행 시 다음과 같은 순서로 데이터를 처리합니다.

1.  **로그인 (Login)**: `login.py` 모듈이 `agent-browser`를 통해 mailon.kr에 접속합니다. TOTP 코드를 생성하여 2차 인증을 통과하고 세션을 확보합니다.
2.  **폴더 UID 해석 (Folder Resolution)**: `folders.resolve_folder_uid` 함수가 받은편지함과 보낸편지함의 고유 ID를 파악합니다. 사이드바 프레임의 DOM을 탐색하거나 전체 메일 목록의 통계를 분석하여 UID를 결정론적으로 찾아냅니다.
3.  **목록 조회 (Listing)**: `scraper.py`의 `list_inbox` 메서드가 Crinity 내부 API인 `list_async.json`을 호출하여 메일 목록을 가져옵니다.
4.  **상세 조회 및 파싱 (Scraping)**: 신규 메일 UID에 대해 `read_mail` 메서드가 `view_async` API로 HTML 본문을 가져와 BeautifulSoup으로 파싱합니다.
5.  **Markdown 작성 (Writing)**: `writer.py` 모듈이 파싱된 메타데이터와 본문을 YAML 프론트매터가 포함된 Markdown 파일로 저장합니다.
6.  **상태 기록 (State Recording)**: `state.py` 모듈이 수집 완료된 메일 UID와 첨부파일 상태를 SQLite DB에 기록하여 다음 실행 시 중복 수집을 방지합니다.

## 2. 모듈 지도 (Module Map)

`mailon/` 패키지는 총 14개의 핵심 모듈로 구성되어 있습니다.

*   `__init__.py`: 패키지 초기화 및 버전을 정의합니다.
*   `browser.py`: `agent-browser` CLI를 제어하는 래퍼로 Windows 환경의 파이프 데드락 방지 로직을 포함합니다.
*   `config.py`: `.env` 파일에서 계정 정보와 실행 설정을 로드하고 검증합니다.
*   `folders.py`: 받은편지함 외의 폴더 UID를 프레임 탐색이나 데이터 추론 방식으로 해석합니다.
*   `login.py`: mailon.kr의 로그인 폼을 자동화하여 인증된 브라우저 세션을 생성합니다.
*   `main.py`: CLI 진입점으로 `sync`, `send`, `resolve`, `status` 등 모든 명령을 디스패치합니다.
*   `resolve.py`: 메일 작성 시 수신자 이름을 입력하여 주소록 자동완성 결과를 조회합니다.
*   `scraper.py`: Crinity 내부 API를 직접 호출하여 메일 목록 조회, 상세 파싱, 첨부파일 다운로드를 수행합니다.
*   `send.py`: 메일 발송 프로세스를 관리하고 발송 성공 여부를 검증합니다.
*   `send_trigger.py`: 메일 작성 폼에 데이터를 주입하고 실제 발송 버튼 클릭을 트리거합니다.
*   `send_verify.py`: 발송 후 보낸편지함을 확인하여 메일이 실제로 나갔는지 최종 검증합니다.
*   `state.py`: SQLite DB를 사용하여 수집 이력, 첨부파일 상태, 실행 로그를 관리합니다.
*   `totp.py`: RFC 6238 표준에 따라 2차 인증용 TOTP 코드를 생성합니다.
*   `writer.py`: 메일 데이터를 구조화된 Markdown 파일로 변환하여 디스크에 저장합니다.

## 3. 데이터베이스 스키마 (state.db)

시스템의 상태는 `data/state.db` SQLite 파일에 저장되며 세 개의 테이블로 구성됩니다.

### 3.1 messages 테이블

수집된 메일의 기본 정보를 관리합니다.
*   `uid` (TEXT, PK): 메일 서버의 고유 UID
*   `folder` (TEXT): 저장된 폴더 (inbox, sent 등)
*   `subject` (TEXT): 메일 제목
*   `sender` (TEXT): 발신자 정보
*   `recv_date` (TEXT): 메일 수신 일시 (ISO 8601)
*   `markdown_path` (TEXT): 저장된 Markdown 파일의 상대 경로
*   `saved_at` (INTEGER): 수집 완료 시각 (Unix timestamp)
*   인덱스: `(folder, recv_date)`

### 3.2 attachments 테이블

첨부파일의 다운로드 상태와 재시도 이력을 관리합니다.
*   `uid` (TEXT): 부모 메일의 UID
*   `filename` (TEXT): 저장된 파일명
*   `href` (TEXT): 원본 다운로드 URL
*   `status` (TEXT): 다운로드 상태 (ok, fail, pending)
*   `size_bytes` (INTEGER): 파일 크기
*   `error_msg` (TEXT): 실패 시 에러 메시지
*   `attempts` (INTEGER): 다운로드 시도 횟수
*   `local_path` (TEXT): 로컬 저장 경로
*   `first_seen` (INTEGER): 처음 발견된 시각
*   `last_attempt` (INTEGER): 마지막 시도 시각
*   PK: `(uid, filename)`, 인덱스: `(status)`

### 3.3 runs 테이블

시스템 실행 이력을 기록합니다.
*   `run_id` (INTEGER, PK): 실행 고유 ID
*   `started_at` (INTEGER): 시작 시각
*   `finished_at` (INTEGER): 종료 시각
*   `status` (TEXT): 실행 결과 (running, ok, fail)
*   `new_mails` (INTEGER): 이번 실행에서 새로 수집된 메일 수
*   `error` (TEXT): 발생한 에러 내용

## 4. 저장소 구조 (Storage Layout)

*   **메일**: `data/mails/YYYY/MM/YYYY-MM-DD_<슬러그>_<uid>.md`
    *   슬러그는 제목을 기반으로 최대 60자까지 생성되며 한글을 보존합니다.
    *   파일 상단에 YAML 형식의 메타데이터가 포함됩니다.
*   **첨부파일**: `data/attachments/<uid>/<파일명>`
*   **상태 DB**: `data/state.db`

## 5. 로그 목록 (Log Inventory)

*   `logs/sync-YYYY-MM-DD.log`: 일자별 시스템 실행 로그
*   `logs/probe-<ts>.html`: `probe` 명령 실행 시 덤프된 전체 HTML
*   `logs/probe-<ts>-ax.txt`: `probe` 명령 실행 시 덤프된 Accessibility Tree
*   `logs/probe-<ts>-url.txt`: `probe` 명령 실행 시의 현재 URL
*   `logs/send-attempts.jsonl`: 메일 발송 시도 및 결과 기록 (JSONL)

## 6. 기술 스택 (Tech Stack)

*   **대상 서비스**: mailon.kr (Crinity G-Cloud 기반)
    *   jQuery 기반 SPA 구조
    *   RSA 암호화 로그인 및 CSRF 토큰 보호
*   **브라우저 자동화**: `agent-browser`
    *   CDP(Chrome DevTools Protocol) 기반의 경량 Chrome 제어 도구
*   **언어 및 라이브러리**:
    *   Python 3.13
    *   BeautifulSoup4 (HTML 파싱)
    *   SQLite3 (상태 관리)
    *   PyOTP (TOTP 생성)
