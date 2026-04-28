# 스킬 4: 지메일 조회

> 고객 담당자와의 **메일 교환 현황**을 확인해 반응 온도·요구·액션 파악. **테스트 모드(공용 라벨) vs 배포 모드(개인 메일) 2분기** — 테스트 환경의 false positive가 배포 시 자연 완화됨.
> 오늘 E2E(260423) 실증 기반 v0.4.

## 역할

스킬 1의 보조 스킬. **외부 고객과의 직접 커뮤니케이션** 상태:
- 고객이 응답 중인가 (반응 온도)
- 고객 요구/조건
- 우리가 해야 할 후속 액션

## 토큰·시간 예산 (원칙 4)

| 상황 | 응답 크기 목표 | 호출 수 |
|---|---|---|
| 평상시 검색 (4딜) | ~5KB (ID 리스트만) | 1~2회 (통합 OR 쿼리) |
| 메타데이터 배치 | ~10KB (4딜 스레드 메타) | 1회 (25개씩 묶음) |
| 풀 읽기 (선별) | ~10KB | 스레드당 최신 1건만, 최대 8건 |
| **4딜 합계** | **~25KB** | 3~4회 |

**원천 봉쇄 전략:**
- **스레드별 최신 1건만** 풀 읽기 (나머지는 메타)
- 풀 읽기 최대 8건 상한 (4딜 × 2스레드 기준)
- 배포 모드에선 쿼리 자체가 좁아짐 (개인 메일함)

## 인풋

| 파라미터 | 출처 | 용도 |
|---|---|---|
| `contact.email` | 스킬 1 | 고객 담당자 이메일 |
| `organization_name` | 스킬 1 | 키워드 폴백용·제목 매칭 |
| `deal_name`, `course_id` | 스킬 1 | 다중 딜 구분용 (제목 토큰 조합) |
| `owner.email` | `settings.owner.email` | **딜 매칭용** LD 본인 이메일 (발신자 판정·테스트 모드 필터 1겹) |
| `owner.name` | `settings.owner.name` | 테스트 모드 본문 필터 (owner 이름 포함 메일만) |
| `owner_aliases` | `settings.owner.notion_name_aliases` | 테스트 모드 본문 필터 (이름 표기 편차 흡수 — "Owner"·"Owner" 등) |
| `api_caller_email` | `settings.data_sources.api_caller_email` | **API 호출용** 인증 계정. 테스트 모드에서 `owner.email`과 다를 수 있음 |
| `gmail_mode` | `settings.data_sources.gmail_mode` | `"test"` or `"deploy"` |
| `gmail_label` | `settings.data_sources.gmail_label` | 테스트 모드 전용 (기본 `B2b_2팀메일`) |

### API caller vs owner 구분 (v0.4 핵심)

**용어:**
- **API caller** = Google API 호출 주체 (workspace-mcp OAuth된 계정)
- **owner** = 운영일지 주인공 LD

| 모드 | `user_google_email` 파라미터 값 | 왜 |
|---|---|---|
| `test` | `api_caller_email` (예: `api-caller@example.com`) | 2팀 공용 라벨이 이 계정에 설정됨 |
| `deploy` | `owner.email` (예: `your-email@example.com`) | 본인 메일함 조회 |

**설정 예 (테스트 시):**
```json
{
  "owner": {"email": "your-email@example.com"},
  "data_sources": {
    "gmail_mode": "test",
    "api_caller_email": "api-caller@example.com",
    "gmail_label": "B2b_2팀메일"
  }
}
```

## 모드 분기 (핵심 신규)

### 테스트 모드 (`gmail_mode="test"`) — v0.5 필터 강화

```
쿼리: label:{gmail_label}
      AND (from:{contact.email} OR to:{contact.email}
           OR (subject:{고객사명} AND subject:{과정명 토큰})
           OR (from:{owner.email} OR to:{owner.email}))
      AND ({owner.name} OR {owner_alias} OR {고객사명})
      after:...
```

2팀 공용 라벨은 2팀 전체 LD(Owner·다른 LD들) 메일 혼재. **owner 기준 필터 2겹 적용**:
1. **이메일 주소 기준**: 고객 담당자 이메일(from/to) OR owner.email(Owner 본인이 보낸/받은 메일)
2. **본문·제목 키워드**: `{owner.name}` 포함 OR `{owner_alias}` (예: 노션 별칭 "Owner", "Owner") OR `{고객사명}` — 2팀 타 LD 메일 배제

**다중 딜 구분:**
- 제목에 `subject:{고객사명} AND subject:{과정명 토큰}` 조합 강제 (예: "하나금융 AND 정기특강")
- 제목에 고객사·과정명 둘 다 있는 메일은 false positive 크게 감소

### 배포 모드 (`gmail_mode="deploy"`)
```
쿼리: (from:{contact.email} OR to:{contact.email}) after:...
```
- 개인 메일함(받은편지함+보낸편지함)
- owner 본인이 주고받은 메일만 → false positive 자연 감소
- label·owner.name 조건 불필요 (본인 계정이라 구조적으로 깨끗)

**오늘 E2E 발견:** 테스트 모드에서 하나금융 67%, Customer C·Customer H 0% 정확도. v0.5 필터 강화로 테스트 모드의 false positive 감소 목표.

### owner 이름 alias

`settings.owner.notion_name_aliases`에 정의된 이름 표기 리스트 활용. Gmail 검색도 2팀 메일에서 `"Owner"` 같은 역순 표기가 실제 쓰일 수 있어 OR 조건으로 포함.

## 슬랙과의 차이 (유지)

| | 슬랙 | 지메일 |
|---|---|---|
| 검색 키워드 | 고객사명 | **고객 이메일 주소** (정확 매칭) |
| 대화 상대 | 내부 팀원 | **외부 고객** |
| 핵심 시그널 | 내부 논의 | **고객 반응 온도·요구** |

## 처리 흐름

```
STEP 1: 4딜 고객 이메일 OR 통합 쿼리 (1~2회)
  ↓
STEP 2: Message ID 수집 → 메타데이터 배치 조회 (1회)
  ↓
STEP 3: 스레드별 중복 제거 (유니크 스레드 목록)
  ↓
STEP 4: 딜 매칭 — 이메일 주소·제목 패턴 기반
  ↓
STEP 5: 스레드별 최신 1건만 풀 내용 조회 (선별)
  ↓
STEP 6: 응답 상태 판별 + LLM 파싱
```

## STEP 1: 검색 쿼리 조립

### 4딜 통합 OR 쿼리 (1회 호출 목표)

```
배포 모드:
(from:emailA@... OR from:emailB@... OR from:emailC@... OR from:emailD@...
 OR to:emailA@... OR to:emailB@... OR to:emailC@... OR to:emailD@...)
after:{2주 전}

테스트 모드 (v0.5 강화):
label:B2b_2팀메일
AND (
  from:emailA@... OR from:emailB@... OR from:emailC@... OR from:emailD@...
  OR to:emailA@... OR to:emailB@... OR to:emailC@... OR to:emailD@...
  OR from:{owner.email} OR to:{owner.email}
  OR (subject:{고객사1} AND subject:{과정토큰1})  -- 제목 조합 (딜별)
  OR (subject:{고객사2} AND subject:{과정토큰2})
  ...
)
AND (
  "{owner.name}" OR "{owner_alias_1}" OR "{owner_alias_2}"
  OR "{고객사1}" OR "{고객사2}" OR "{고객사3}" OR "{고객사4}"
)
after:{2주 전}
```

테스트 모드의 2겹 필터가 핵심:
- 1겹(from/to + 제목 조합): Owner 본인 또는 Owner 관련 고객 이메일
- 2겹(본문 owner 이름/alias/고객사): 2팀 타 LD 메일 완전 배제

### 폴백: 쿼리 길이 초과
- 쿼리 너무 길면 2딜씩 분할 (최대 2회 호출)
- 그래도 실패면 딜별 개별 호출 (비상)

## STEP 2~3: 메타데이터 배치 + 스레드 축약

**오늘 E2E 교훈 (이슈 #22, #23):**
- `search_gmail_messages`는 ID만 반환. 내용 읽으려면 **2-step 호출 필요**
- 같은 스레드에서 여러 메시지 → **유니크 스레드로 축약**

```
search_gmail_messages → Message IDs 리스트
  ↓
get_gmail_messages_content_batch(format='metadata', message_ids=[...])
  ↓
Thread ID별로 그룹화 → 유니크 스레드 목록 (중복 제거)
```

메타데이터만으로 얻는 정보:
- Subject (제목)
- From / To / Cc
- Date

→ **이걸로 딜 매칭 1차 판정** 가능. 전체 본문 안 읽어도 됨.

## STEP 4: 딜 매칭 (메타데이터 기반)

### 매칭 시그널 (이슈 #26)

이메일 제목 패턴에서:
- `[고객사]` 브래킷 → 고객사 식별
- `[패스트캠퍼스] {강사명}` 패턴 → 우리 내부 발신 (강사 섭외·조율)
- 과정명 키워드 (예: "AI 정기특강", "Project X", "전사 생성형AI") → 다중 딜 구분
- `RE: Re: Re:` 깊이 = 대화 왕복 횟수 (참고 시그널)

### 다중 딜 고객사 대응
```
하나금융 3 스레드 → 
  - 제목 "[하나금융] DT University 특별과정 - 2026 AI 정기특강" → Customer B 딜 (course_id 263026)
  - 제목 "하나금융티아이 데이터분석실무과정" → 다른 딜 (제외)
  - 제목 "하나금융티아이_디지털 마케팅스쿨" → 다른 딜 (제외)
```

## STEP 5: 선별 풀 읽기 (토큰 절약 핵심)

딜당 최신 1건만 풀 읽기. 나머지 스레드는 메타 유지.

```
get_gmail_message_content(format='full', message_id=latest_per_thread)
  최대 8건 상한 (4딜 × 2스레드 가정)
```

풀 읽기 대상 우선순위:
1. 딜별 가장 최근 스레드의 최신 메시지
2. 그 스레드의 이전 메시지 1건 (대화 맥락 1회 왕복)
3. 다른 스레드는 메타만 (제목·날짜·발신자)

## STEP 6: 응답 상태 판별 + LLM 파싱

### 응답 상태 (이슈 #27 해결)

**스레드 마지막 메시지 작성자의 도메인으로 판정:**

| 조건 | `response_status` |
|---|---|
| 고객사 도메인 (예: `@customer-domain.example.com`, `@customer-domain.example.com`, `@customer-domain.example.com`) | `"🔴 고객이 마지막"` (내 회신 필요) |
| `@your-domain.example.com` | `"우리가 마지막"` (고객 회신 대기) |
| 2주 내 양방향 교환 있음 + 최근 < 3일 | `"주고받는 중"` |
| 2주간 메일 0건 | `"⚠️ 메일 교환 없음"` |

### LLM 파싱 (딜별 개별, 유지)

풀 읽기한 본문에서 4항목:

| 항목 | 설명 |
|---|---|
| `customer_request` | 고객이 원하는 것·조건 변경 |
| `next_action` | 우리 쪽 할 일 + 기한 |
| `customer_sentiment` | 고객 태도·온도 |
| `situation_summary` | 메일 흐름 1줄 |

메타만 있는 스레드는 제목 기반 요약만 ("[강사명] 강사 섭외 진행 중" 수준).

**왜 딜별 개별 LLM 호출 유지:** 딜별 메일 내용 맥락 완전히 다름. 일괄 LLM에서 고객 요구 혼동되면 치명적.

## 아웃풋

```json
{
  "deal_id": "019b6ca9-4766-7666-a81d-d7afce24713e",
  "deal_name": "Customer I_전사 생성형AI",
  "response_status": "우리가 마지막",
  "last_sent": "2026-04-21",
  "last_received": "2026-04-21",
  "thread_summary": [
    {
      "thread_id": "19dad69e87f4c401",
      "subject": "Customer I 중급과정 과정 논의 미팅 일자 조율의 건",
      "msg_count": 2,
      "last_date": "2026-04-21",
      "last_author_domain": "gmail.com",
      "type": "external"
    },
    {
      "thread_id": "19daebb323f651dc",
      "subject": "임승현 강사님께 - 기업교육 출강 문의",
      "msg_count": 1,
      "last_date": "2026-04-21",
      "last_author_domain": "your-domain.example.com",
      "type": "internal_instructor"
    }
  ],
  "email_parsed": {
    "customer_request": null,
    "next_action": "김인섭 강사 미팅 일자 확정 (오늘 18시 예정)",
    "customer_sentiment": null,
    "situation_summary": "중급과정 강사 섭외·일정 조율 병행 진행 중"
  },
  "meta": {
    "search_count": 6,
    "unique_threads": 3,
    "full_read_count": 1,
    "fallback_used": null
  }
}
```

### 스레드 타입 분류 (신규, 이슈 #28)

| 타입 | 설명 |
|---|---|
| `external` | 고객사 도메인과 주고받음 |
| `internal_instructor` | 강사 섭외·조율 (중요한 운영 준비 액션) |
| `internal_team` | 내부 팀원끼리 |

운영일지에서는 `external`과 `internal_instructor` 둘 다 중요.

## 실패·폴백 처리

| 상황 | 처리 |
|---|---|
| workspace-mcp 인증 실패 | 전체 default |
| 검색 0건 | `response_status: "⚠️ 메일 교환 없음"` |
| 메타 배치 실패 | 개별 메시지 조회로 폴백 (느리지만 안전) |
| 쿼리 길이 초과 | 2딜씩 분할 |
| 풀 읽기 실패 | 해당 스레드 메타만 유지 |
| 딜 매칭 실패 | 해당 딜 `email_parsed: null` |

## settings.json 구조 (신규 `gmail_mode`)

```json
"data_sources": {
  "gmail_mode": "test",
  "gmail_label": "B2b_2팀메일",
  "_comment_gmail_mode": "'test' — 공용 라벨 경유(테스트용, false positive 있음) / 'deploy' — 개인 메일함 기반(배포 시 권장)"
}
```

배포 시 Owner Name 본인 환경:
- `gmail_mode: "deploy"`
- `gmail_label` 값은 무시됨

## 기존 v0.3과의 차이

| 항목 | v0.3 | v0.4 |
|---|---|---|
| 모드 분기 | 테스트 전용 (단일) | **test / deploy 2모드** |
| 2-step 호출 | 암묵적 | **명시적** (검색 ID → 메타 배치 → 선별 풀) |
| 스레드 중복 | 미처리 | **유니크 스레드 축약** |
| 응답 상태 판정 | "주고받는 중/미응답" 수준 | **마지막 작성자 도메인 기반** (고객/우리) |
| 풀 읽기 | 제한 없음 | **스레드당 최신 1건, 최대 8건 상한** |
| 제목 패턴 활용 | 미사용 | `[고객사]`, `[패스트캠퍼스]` 브래킷 매칭 |
| 토큰 예산 | 미명시 | 평상시 ~25KB 목표 |
| 스레드 타입 | 단일 | `external` / `internal_instructor` / `internal_team` |

## LD 체크 포인트

1. 고객 메일 외 카톡·전화 소통 시 "미응답" 판정 부정확 가능 (실제는 다른 채널로 오간 상태)
2. 팀 공용 계정 발송분이 본인 메일함에 없으면 배포 모드에서 누락 — 공용 계정 사용 시 테스트 모드 유지 또는 공용 라벨 연동 방식 유지
3. 강사 섭외 메일은 `internal_instructor` 타입으로 별도 분류됨 — 운영일지 📊 진행 현황에 "강사 섭외 중" 시그널로 활용

## 버전

- v0.3 (2026-04-22): 통합 OR 쿼리 도입
- v0.4 (2026-04-23): E2E 실증 — test/deploy 모드 분리, 2-step 명시, 스레드 축약, 도메인 기반 응답 판정, 토큰 예산
- v0.5 (2026-04-24): 테스트 모드 필터 2겹 강화. 1겹은 이메일 주소/제목 조합, 2겹은 본문 owner 이름·alias·고객사명. 2팀 공용 라벨 false positive (Owner Name 외 타 LD 메일) 구조적 배제. `owner.email`·`owner.notion_name_aliases` 활용.
