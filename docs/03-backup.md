# 03. 전체 백업 + 증분 동기화 운영 가이드

**목적**: 받은편지함과 보낸편지함의 **전체 메일 백업** (1회) 후 **신규 메일만 매시간 수집** (크론잡)
**파일**: `mailon/state.py`, `mailon/writer.py`, `mailon/main.py`
**CLI**: `python -m mailon.main sync [--limit N] [--folders inbox,sent]`

---

## 1. 단계별 워크플로우

### 1.1 Phase A: 최초 전체 백업 (1회)

```
┌────────────────────────────────┐
│ 메일함 (받은편지함 + 보낸편지함) │
└────────────────────────────────┘
              │
              ▼
   python -m mailon.main sync
              │
              ▼
┌────────────────────────────────┐
│ 1. 로그인                       │
│ 2. list_async.json 페이지 반복  │
│ 3. 각 메일 처리:                │
│    a. view_async HTML 파싱      │
│    b. 첨부파일 다운로드          │
│    c. Markdown 파일 작성         │
│    d. state.db 기록              │
└────────────────────────────────┘
              │
              ▼
    data/mails/YYYY/MM/*.md
    data/attachments/<uid>/*
    data/state.db (중복 방지 인덱스)
```

**운영 전략**: 처음에는 `--limit 5` 정도로 소량만 실행하여 Markdown 파일과 첨부파일이 정상적으로 저장되는지 확인하세요. 문제가 없다면 `--limit` 없이 실행하여 전체 백업을 완료합니다. 대량의 메일을 수집할 때는 시간이 오래 걸리지만, 중간에 끊겨도 `state.db` 덕분에 다음 실행 시 이어서 진행됩니다.

### 1.2 Phase B: 증분 동기화 (매시간 자동화)

```
┌────────────────────────────────┐
│ Windows 작업 스케줄러 (1시간)    │
└────────────────────────────────┘
              │
              ▼
       run_sync.bat
              │
              ▼
┌────────────────────────────────┐
│ 1. 로그인 (매번 새로 수행)       │
│ 2. list_async.json 목록 조회    │
│ 3. state.db 대조 (중복 제외)     │
│ 4. 신규 메일만 상세 조회 및 저장  │
└────────────────────────────────┘
              │
              ▼
  신규 메일 Markdown 추가 저장
  sync-YYYY-MM-DD.log에 결과 기록
```

**소요 시간**: 로그인과 목록 조회에 약 3~5분이 소요되며, 신규 메일이 없다면 즉시 종료됩니다. 보낸편지함 동기화 기능은 2026년 7월 업데이트로 추가되었습니다.

---

## 2. 중복 방지 및 상태 관리 (SQLite)

### 2.1 데이터베이스 스키마

`data/state.db` 파일은 수집 이력을 관리하여 같은 메일을 중복해서 받지 않도록 합니다.

```sql
CREATE TABLE messages (
    uid           TEXT PRIMARY KEY,    -- 메일 서버 고유 ID (예: "1000001")
    folder        TEXT NOT NULL DEFAULT 'inbox',
    subject       TEXT,
    sender        TEXT,
    recv_date     TEXT,                 -- ISO 8601 형식
    markdown_path TEXT,                 -- 프로젝트 루트 기준 상대 경로
    saved_at      INTEGER NOT NULL      -- 수집 시각 (Unix Timestamp)
);

CREATE TABLE attachments (
    uid           TEXT NOT NULL,      -- 부모 메일 UID
    filename      TEXT NOT NULL,      -- 저장된 파일명
    href          TEXT NOT NULL,      -- 다운로드 주소
    status        TEXT NOT NULL,      -- 'ok' (성공) | 'fail' (실패) | 'pending' (대기)
    attempts      INTEGER NOT NULL,   -- 시도 횟수
    error_msg     TEXT,               -- 실패 시 에러 메시지
    PRIMARY KEY (uid, filename)
);
```

### 2.2 중복 방지 메커니즘

1. `messages.uid`를 PRIMARY KEY로 사용하여 동일한 UID를 가진 메일이 중복 기록되는 것을 원천 차단합니다.
2. 실행 시 `existing_uids`를 조회하여 이미 DB에 존재하는 메일은 목록 단계에서 건너뜁니다.
3. 폴더가 다르더라도 UID가 충돌하는 경우를 대비한 가드 로직이 포함되어 있습니다.

### 2.3 첨부파일 수명주기 및 재시도

- 첨부파일은 `pending` 상태로 시작하여 성공 시 `ok`, 실패 시 `fail`로 기록됩니다.
- `attempts` 카운터가 시도 횟수를 추적합니다.
- `retry_failed_attachments` 로직이 실행될 때마다 이전에 실패한 첨부파일을 자동으로 재시도합니다.
- `python -m mailon.main sync` 실행 결과 요약에 재시도 성공 및 실패 건수가 포함됩니다.

---

## 3. Markdown 파일 저장 규칙

### 3.1 경로 구조

```
data/mails/
├── 2026/
│   ├── 07/
│   │   ├── 2026-07-31_예시학회_기계_제어_로봇부문_2026년_춘계학술대회_개최_안내_1000001.md
│   │   └── 2026-07-31_붙임2-1_2026년_정기_세미나_발표자료_1_1000002.md
│   └── 08/
│       └── ...
└── 2025/
    └── ...
```

- **연/월별 폴더**: Obsidian이나 Logseq 같은 도구에서 관리하기 편하도록 분류합니다.
- **파일명**: `YYYY-MM-DD_<슬러그>_<uid>.md` 형식입니다.
  - 날짜 접두사로 시간순 정렬이 가능합니다.
  - 슬러그는 제목에서 금지 문자(`\/:*?"<>|`)를 `_`로 치환하고 최대 60자까지 보존합니다. 한글은 그대로 유지됩니다.
  - UID 접미사로 제목이 같은 메일 간의 충돌을 방지합니다.

### 3.2 Front-matter 메타데이터

```yaml
---
uid: "1000001"
folder: "inbox"
subject: "예시학회 기계·제어·로봇부문 2026년 춘계학술대회 개최 안내"
from: "\"홍길동\" <hgd@example.com>"
to: "user@mailon.kr"
cc: ""
date: "2026-07-31T14:46:15"
collected_at: "2026-07-31T15:00:00"
attachments:
  - "발표자료.pdf"
---
```

---

## 4. 첨부파일 저장 구조

```
data/attachments/
└── 1000001/                                        ← 메일 UID별 폴더
    └── 발표자료.pdf
```

- **UID별 폴더**: 서로 다른 메일에 포함된 같은 이름의 첨부파일이 덮어씌워지는 것을 방지합니다.
- **상대 경로 참조**: Markdown 본문에서 첨부파일을 클릭하면 바로 열 수 있도록 상대 경로 링크를 생성합니다.

---

## 5. 주요 CLI 명령어 사용법

### 5.1 동기화 실행 (`sync`)

```cmd
REM 받은편지함과 보낸편지함 모두 동기화 (기본값)
.venv\Scripts\python -m mailon.main sync

REM 특정 폴더만 지정 (inbox 또는 sent)
.venv\Scripts\python -m mailon.main sync --folders inbox

REM 폴더당 최대 수집 개수 제한 (최초 테스트용)
.venv\Scripts\python -m mailon.main sync --limit 10
```

- **종료 코드**: 0(성공), 2(로그인/브라우저/설정 오류 또는 잘못된 폴더), 3(예기치 못한 예외).
- `--limit` 미지정 시 `.env`의 `MAX_MAILS_PER_RUN` 설정을 따르며, 이 값이 0이면 무제한으로 수집합니다.

### 5.2 상태 확인 (`status`)

```cmd
.venv\Scripts\python -m mailon.main status
```

- **출력 내용**: 받은편지함과 보낸편지함 각각에 저장된 메일 수, 마지막 실행 결과(성공 여부, 수집 개수, 시작/종료 시각, 에러 메시지 일부)를 보여줍니다. 이 명령은 로컬 DB만 읽으므로 로그인이 필요 없습니다.

---

## 6. 운영 및 트러블슈팅

### 6.1 전체 재수집이 필요한 경우

기존 데이터를 모두 지우고 처음부터 다시 받으려면 다음 명령을 순서대로 실행하세요.

```cmd
del /F data\state.db
rmdir /S /Q data\mails data\attachments
mkdir data\mails data\attachments
.venv\Scripts\python -m mailon.main sync
```

### 6.2 특정 메일만 다시 받기

DB에서 해당 UID를 삭제한 후 다시 `sync`를 실행하면 해당 메일만 다시 수집합니다.

```cmd
.venv\Scripts\sqlite3 data\state.db "DELETE FROM messages WHERE uid='1000001';"
.venv\Scripts\python -m mailon.main sync --limit 5
```

### 6.3 용량 관리

- **Markdown**: 메일 8,000개 기준 약 200MB 내외입니다.
- **첨부파일**: 사용자의 메일 사용 패턴에 따라 수 GB까지 늘어날 수 있으므로 충분한 디스크 공간을 확보하세요.
- **로그**: `run_sync.bat`이 30일이 지난 로그를 자동으로 삭제합니다.

---

## 7. 보안 주의사항

- **개인정보 보호**: 수집된 메일에는 민감한 개인정보나 업무 내용이 포함될 수 있습니다. 백업 폴더를 공용 클라우드에 동기화하지 않도록 주의하세요.
- **디스크 암호화**: Windows의 BitLocker 등을 사용하여 로컬에 저장된 데이터를 보호하는 것을 권장합니다.
- **AUP 준수**: 본 도구는 개인적인 백업 용도로만 사용해야 하며, 예시연구원의 정보 보안 규정을 준수해야 합니다.
- **비밀번호 관리**: `.env` 파일에는 계정 비밀번호와 TOTP 시크릿이 평문으로 저장됩니다. 이 파일이 외부에 노출되지 않도록 절대 주의하세요.

---

## 8. 관련 문서

- [docs/04-cronjob.md](04-cronjob.md) - Windows 작업 스케줄러 자동화 설정
- [LOGIN_MANUAL.md](../LOGIN_MANUAL.md) - TOTP 시크릿 확보 및 로그인 설정 가이드
- [AGENTS.md](../AGENTS.md) - 프로젝트 구조 및 에이전트 지침
