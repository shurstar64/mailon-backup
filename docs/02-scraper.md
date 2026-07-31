# 스크래퍼 레퍼런스

이 문서는 mailon.kr의 메일 데이터를 수집하는 `mailon/scraper.py` 모듈의 상세 동작과 API 구조를 설명합니다.

## 1. 설계 원칙

### 1.1 API 기반 수집

이 시스템은 브라우저 화면의 DOM 요소를 직접 긁어오는 방식이 아니라, Crinity G-Cloud 웹메일이 내부적으로 사용하는 **JSON API와 HTML 조각(fragment) API**를 직접 호출합니다.

*   **장점**: DOM 구조 변경에 강하며, 페이지네이션 처리가 빠르고 정확합니다.
*   **방식**: 브라우저의 인증 세션(쿠키)을 사용하여 `fetch()` 요청을 실행하고 그 결과를 파싱합니다.

### 1.2 주요 메서드 (InboxScraper)

*   `resolve_inbox_folder_uid()`: 로그인 후 받은편지함의 고유 ID를 파악합니다.
*   `fetch_list_page(page)`: 특정 페이지의 메일 목록 JSON을 가져옵니다.
*   `list_inbox()`: 전체 페이지를 순회하며 메일 목록을 수집합니다.
*   `fetch_mail_html(uid, folder_uid)`: 단일 메일의 전체 HTML 내용을 가져옵니다.
*   `read_mail(ref)`: HTML을 파싱하여 `Mail` 객체로 변환하고 첨부파일을 다운로드합니다.
*   `iter_new_mails(skip_uids, limit)`: 중복을 제외한 신규 메일들을 순차적으로 처리합니다.
*   `retry_failed_attachments(db)`: 이전에 실패한 첨부파일 다운로드를 재시도합니다.
*   `probe_and_dump(out_dir)`: 현재 페이지의 구조를 디버깅용으로 저장합니다.
*   `goto_inbox()`: 받은편지함으로 이동하는 하위 호환용 메서드입니다.

## 2. 데이터 구조 및 규약

### 2.1 MailRef 구조

목록 조회 시 반환되는 메일의 요약 정보입니다.
*   `uid`: 메일 고유 ID (예: `1000001`)
*   `folder_uid`: 소속 폴더 ID (예: `10001`)
*   `subject`: 메일 제목
*   `sender`: 발신자 이름 및 이메일
*   `date`: 수신 일시
*   `size`: 메일 크기 (바이트)
*   `attach_count`: 첨부파일 개수

### 2.2 UID 규약

mailon.kr에서 각 메일의 고유 식별자는 HTML 요소의 `data-id` 속성이나 API 응답의 `mailUid` 필드에 담긴 7자리 숫자입니다. 메일 상세 내용을 열람할 때는 내부적으로 `_view.initView({mailUid, folderUid, folderType, authWrite})`와 같은 JavaScript 함수가 호출됩니다.

## 3. 파싱 유틸리티

수집된 데이터를 정규화하기 위해 다음 유틸리티를 사용합니다.
*   `parse_view_async_html(html)`: 상세 HTML에서 제목, 발신자, 수신자, 본문, 첨부 링크를 추출합니다.
*   `parse_korean_date(text)`: "26.07.31 14:30" 형식의 한국어 날짜 문자열을 datetime 객체로 변환합니다.
*   `parse_millis(ms)`: 서버에서 내려주는 Unix 밀리초 타임스탬프를 변환합니다.
*   `split_filename_and_size(raw)`: "보고서.pdf 1024"와 같이 파일명 뒤에 크기가 붙은 문자열을 분리합니다.

## 4. probe 서브커맨드

사이트 구조가 변경되었거나 특정 메일이 정상적으로 파싱되지 않을 때 `probe` 명령을 사용하여 현재 브라우저 상태를 덤프할 수 있습니다.

```bash
python -m mailon.main probe
```

이 명령은 `logs/` 디렉토리에 다음 세 가지 파일을 생성합니다.
1.  `logs/probe-<ts>.html`: 현재 페이지의 전체 HTML 소스
2.  `logs/probe-<ts>-ax.txt`: 브라우저의 Accessibility Tree (요소 구조 파악용)
3.  `logs/probe-<ts>-url.txt`: 현재 브라우저가 위치한 URL

**주의**: probe 덤프 파일에는 실제 메일 내용이나 개인정보가 포함될 수 있으므로 공개 저장소에 커밋하지 마십시오. `logs/` 폴더는 기본적으로 `.gitignore`에 등록되어 있습니다.

## 5. 첨부파일 처리

첨부파일은 다음과 같은 상태 전이를 거칩니다.
1.  **pending**: 다운로드 대기 상태
2.  **ok**: 다운로드 성공 및 로컬 저장 완료
3.  **fail**: 네트워크 오류 등으로 실패 (에러 메시지와 함께 기록)

실패한 첨부파일은 `attempts` 횟수를 기반으로 다음 `sync` 실행 시 자동으로 재시도됩니다. 파일 크기가 큰 경우 브라우저 메모리 제한을 피하기 위해 Python의 `urllib`을 사용하여 스트리밍 방식으로 다운로드합니다.

## 6. 폴더 확장 방법

현재 시스템은 받은편지함(inbox)과 보낸편지함(sent)을 기본으로 지원합니다. `folders.resolve_folder_uid`를 사용하여 대상 폴더의 UID를 찾고, `InboxScraper` 생성 시 `folder_label` 파라미터에 해당 폴더 이름을 전달하면 휴지통이나 사용자 정의 폴더로 수집 범위를 쉽게 확장할 수 있습니다.

## 7. 성능 노트

*   **목록 조회**: 페이지당 20개씩 수집하며, 8000개 기준 약 3분이 소요됩니다.
*   **상세 수집**: 메일 하나당 HTML 요청 및 파싱에 약 1초가 소요됩니다.
*   **병목 지점**: 상세 내용을 하나씩 순차적으로 요청하는 과정이 전체 실행 시간의 대부분을 차지합니다.

## 8. 수집 예시 (가상 데이터)

### Markdown 출력 예시

```markdown
---
uid: "1000001"
folder: "inbox"
subject: "예시학회 기계·제어·로봇부문 2026년 춘계학술대회 개최 안내"
from: "\"예시학회\" <no-reply@example.org>"
to: "user@example.com"
cc: ""
date: "2026-07-31T14:00:00"
collected_at: "2026-07-31T15:00:00"
attachments:
  - "안내문.pdf"
---

# 예시학회 기계·제어·로봇부문 2026년 춘계학술대회 개최 안내

**From**: "예시학회" <no-reply@example.org>  
**Date**: 2026-07-31T14:00:00  
**To**: user@example.com  

## Attachments

- [안내문.pdf](../../attachments/1000001/안내문.pdf) (102400 bytes)

## Body

2026년 춘계학술대회 발표 신청을 아래와 같이 안내드립니다.
(이하 생략)
```
