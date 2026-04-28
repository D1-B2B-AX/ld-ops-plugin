# 스킬 1: 세일즈맵 조회 (status_scope 분기)

> 담당자(LD) 기준으로 **지정한 상태의 딜 + 고객사 정보 + 고객사 담당자 + 최근 메모(파싱 포함)** 를 하나의 구조화된 JSON으로 반환하는 스킬.
> **딜 플러그인·운영 플러그인 공용** — `status_scope` 파라미터로 SQL/Won 분기.
> v0.6 (2026-04-27): **Won 모드 시 과정포맷 화이트리스트 적용**. `settings.data_sources.ops_included_course_formats` 치환. 파트장 미팅 결과 온라인·구독·컨설팅 등 9개 체크포인트 매칭 어려운 형태 제외.

## 역할

파이프라인의 **출발점**. 이 스킬의 출력이 스킬 2~5(캘린더/슬랙/지메일/기획시트)의 인풋이 되고, 각 플러그인의 고유 로직(딜: 스코어링, 운영: 항목 분류)의 기반 컨텍스트가 됨.

## 딜 정의 — status_scope별 분기

| status_scope | 쿼리 조건 | 주 활용 플러그인 |
|---|---|---|
| `'SQL'` | `상태 = 'SQL'` (수주 전 활성 딜) | 딜 플러그인 |
| `'Won'` | `상태 = 'Won'` + 진행 중 조건 | 운영 플러그인 |
| `'Both'` | 위 둘 통합 | (향후 통합 뷰용) |

### Won 조건 상세

단순 `상태 = 'Won'`만으로는 과거에 종료된 교육까지 모두 잡힘. **진행 중 Won**만 필요하므로 다음 조건 추가:

```sql
AND (edu_end IS NULL OR edu_end >= date('now'))
```

→ 교육 종료일이 미래이거나 아직 모름(날짜 미확정)인 딜만.

## 인풋

| 파라미터 | 예시 | 설명 |
|---|---|---|
| `owner_name` | `settings.owner.name` | 세일즈맵 담당자 이름 (JSON 컬럼 LIKE 검색) |
| `status_scope` | `'SQL'` or `'Won'` or `'Both'` | 오케스트레이터에서 지정 |

**주의:** `owner_name`은 **settings.json의 owner.name을 치환**. 하드코딩 금지. `query.sql`의 `{owner_name}` + `{status_scope}` 플레이스홀더를 값으로 대체.

## 처리 흐름

```
STEP 1: SQL 쿼리 (status_scope 분기 — 딜 + 고객사 + 담당자)
  ↓
STEP 2: SQL 쿼리 (각 딜의 최근 메모 3건)
  ↓
STEP 3: LLM 메모 파싱 (다음 액션+기한 / 현재 상황 1줄 요약)
  ↓
STEP 4: JSON 구조화 + 검색 키워드 정리
```

## STEP 1~2: SQL 쿼리

전체 쿼리: `query.sql` 참조 (파라미터화됨)

**핵심 쿼리 요약:**
```sql
SELECT ... FROM deal d
LEFT JOIN organization o ON d.organizationId = o.id
LEFT JOIN people p ON d.peopleId = p.id
WHERE d."담당자" LIKE '%{owner_name}%'
  AND d."상태" = '{status_scope}'
  -- Won 모드에서만 진행 중 교육 필터 (종료일 미래 or 미확정)
  AND ('{status_scope}' != 'Won' OR d."수강종료일" IS NULL OR d."수강종료일" >= date('now'))
ORDER BY d."최근 파이프라인 단계 수정 날짜" DESC;
```

**보조 쿼리 (메모):**
```sql
SELECT dealId, createdAt, text FROM memo
WHERE dealId IN (:deal_ids)
ORDER BY dealId, createdAt DESC;
-- 애플리케이션 레벨에서 dealId별 최근 3건으로 slice
```

## STEP 3: LLM 메모 파싱

### ⚡ v0.5 병렬화 (이슈 #32)

**딜별 메모 파싱은 반드시 병렬 LLM 호출로 처리** — 한 번의 `<function_calls>` 블록에 4딜(혹은 N딜) LLM 호출 동시에 넣기.

```
❌ 순차 (금지): 딜 1 파싱 완료 → 딜 2 파싱 → ... (4딜 × 10초 = 40초)
✅ 병렬 (필수): 4딜 동시 호출 (max 10초)
```

**근거:** 각 딜 메모는 완전히 독립된 텍스트. 딜 간 참조·공유 없음. 병렬 안전.

**대상 LLM:** Claude 런타임 (또는 Claude API 직접 호출 시 `anthropic.messages.create()` 동시 async 호출).

---

각 딜의 메모 원문에서 **2가지만** 추출:

| 파싱 항목 | 설명 | 예시 |
|---|---|---|
| **다음 액션 + 기한** | 메모에서 CTA/TODO/다음 할 일 추출 | "3/13(금) 판교 대면 미팅 가능 여부 확인 필요" |
| **현재 상황 1줄 요약** | 이 딜이 지금 어떤 상태인지 한 줄 압축 | "RFP 기반 연간 교육 제안 준비 중, 대면 미팅 일정 조율 단계" |

**파싱 규칙:**
- 다음 액션이 메모에 없으면 `null` (억지로 만들지 않음)
- 기한이 상대적이면 메모 작성일 기준으로 절대 날짜 변환 (예: "차주 목" + 메모일 3/4 → "3/11")
- 메모 원문 텍스트도 그대로 보존하여 후속 스킬에 전달

### LLM 프롬프트 (구체)

```
당신은 B2B 영업 딜 메모를 분석하는 어시스턴트입니다.
아래 메모 원문을 읽고 정확히 2가지 항목만 JSON으로 반환하세요.

[메모 원문]
{memo_text}

[메모 작성일]
{memo_date}

[딜 기본 정보 (참고용)]
딜명: {deal_name}
고객사: {customer_name}
파이프라인 단계: {pipeline_stage}

[추출 규칙]
1. next_action (다음 액션 + 기한)
   - 메모에서 "해야 할 것", "팔로업", "다음 단계", "약속", "TODO" 성격의 내용 찾기
   - 형식: "{행동} → {대상/목적} (기한: {절대 날짜})"
   - 상대 날짜("내일", "차주 목", "다음주")는 메모 작성일 기준 절대 날짜(YYYY-MM-DD)로 변환
   - 기한이 명시되지 않은 액션이면 날짜 부분 생략
   - 다음 액션이 메모에 전혀 없으면 null (억지로 만들지 말 것)

2. current_status (현재 상황 1줄 요약)
   - 이 딜이 "지금" 어떤 상태인지를 한 문장(40~60자)으로 압축
   - 포함: 진행 단계 + 핵심 맥락 1~2개 + (해당 시) 위험 시그널
   - 감정 표현 제거, 팩트만
   - 메모가 비어있으면 "메모 없음"으로 반환

[아웃풋 형식 — JSON만]
{
  "next_action": "3/11(화) 견적서 v2 발송 → 신대리 검토 요청" | null,
  "current_status": "RFP 기반 연간 교육 제안 준비 중, 대면 미팅 일정 조율 단계"
}

[주의]
- JSON 외의 다른 텍스트 출력 금지
- 메모에 없는 내용을 추론하거나 상상해서 넣지 말 것
- 상대 날짜 변환이 모호하면 원문 표현 그대로 유지
```

## STEP 4: 아웃풋 JSON 구조

### SQL 단계 (딜 플러그인용)

```json
{
  "deal_id": "019baba5-418d-733f-aab9-4d330145b8f6",
  "deal_name": "Customer A_26년도 전사 AI 연간 교육",
  "deal_type": "수주 전",
  "status": "SQL",
  "stage": "최종 f-up",
  "win_probability": "높음",
  "expected_amount": 326480000,
  "course_format": "출강",
  "expected_close_date": "2026-04-29",
  "days_to_close": 15,
  "organization": {
    "name": "Customer A",
    "industry": "금융/보험업",
    "past_won_deals": 0,
    "is_existing_customer": false,
    "total_revenue": 0
  },
  "contact": {
    "name": "담당자",
    "email": "contact@example.com",
    "title": "실무자"
  },
  "recent_activity": {
    "last_note_date": "2026-03-10",
    "days_since_last_note": 35,
    "memo_parsed": {
      "next_action": "3/13(금) 판교 대면 미팅 가능 여부 확인",
      "current_status": "RFP 기반 연간 교육 제안 준비 중..."
    },
    "recent_memos_raw": [
      { "date": "2026-03-10", "text": "(원문 보존)" }
    ]
  },
  "search_keywords": {
    "deal_name_tokens": ["Customer A", "26년도", "AI", "연간", "교육"],
    "organization_name": "Customer A",
    "contact_name": "담당자"
  }
}
```

### Won 단계 (운영 플러그인용) — 추가 필드

```json
{
  "deal_id": "...",
  "deal_name": "...",
  "course_id": "263026",
  "deal_type": "수주 후",
  "status": "Won",
  "organization": {...},
  "contact": {...},
  "recent_activity": {...},

  "edu_start": "2026-04-23",
  "edu_end": "2026-05-15",
  "education_schedule_confirmed": true,

  "search_keywords": {...}
}
```

**Won 전용 필드:**
- `edu_start` — 수강 시작일 (📅 일정 항목의 핵심 baseline)
- `edu_end` — 수강 종료일 (📊 진행 현황 계산용)
- `education_schedule_confirmed` — `edu_start is not null`

**공통 신규 필드 (v0.3, 2026-04-23):**
- `course_id` — 6자리 코스 고유 ID. 드라이브·지메일·슬랙의 **보조 매칭 키**로 활용.

**매칭 우선순위 교정 (2026-04-23 저녁):**
- 초안은 course_id를 1순위로 설계했으나, 네이밍 규칙은 **Owner Name이 앞으로 따를 합의일 뿐 다른 LD는 기본 안 지킴**. 재조정:
  - 🥇 **고객사명 + 딜명 토큰 유사도 + LLM 보조** (모든 LD 기본)
  - 🥈 **폴더명·파일명 토큰** (대부분 LD)
  - 🥉 **파일명 `_{course_id}` 패턴** (Owner Name 미래 파일)
  - 4 **파일 내용 백오피스 URL course_id** (우연적)
- 상세: `data-skills/5-드라이브-기획시트-조회/SKILL.md` STEP 3 참조

### 필드 의미 (공통)

| 필드 | 용도 |
|---|---|
| `deal_id`, `deal_name`, `stage`, `status` | 표시·분류 |
| `expected_amount`, `expected_close_date`, `days_to_close` | 딜: 스코어링 |
| `edu_start`, `edu_end` | 운영: 일정·진행 현황 |
| `organization.past_won_deals`, `is_existing_customer` | 딜: 기고객 시그널 / 운영: 관계 컨텍스트 |
| `contact.name`, `contact.email` | 스킬 2, 3, 4 (캘린더·슬랙·지메일 검색 키워드) |
| `memo_parsed.next_action` | 딜: 우선순위 / 운영: 🎯 액션 후보 |
| `memo_parsed.current_status` | 딜: 보고서 요약 / 운영: 📊 진행 현황 보조 |
| `recent_memos_raw` | LLM 추가 맥락 참조 |
| `search_keywords` | 스킬 2, 3, 4 (검색 쿼리) |

## 알려진 제약·주의사항

| 항목 | 내용 |
|---|---|
| JSON 컬럼 | `담당자`, `파이프라인 단계`, `성사 가능성` 등은 JSON 문자열 — 후처리 파싱 필요 |
| NULL 필드 다수 | `최근 연락일`, `기획시트 링크`, `기업 니즈` 등 대부분 비어 있음 |
| 라스트 터치 | `최근 연락일` NULL → `최근 노트 작성일`로 대체 |
| 메모 형태 편차 | 콜로그 / 한 줄 요약 / 웹폼 / 고객 DM 복붙 등 — 비정형이므로 "다음 액션" 파싱 불가 시 null |

## 기존 플러그인과의 차이

| 항목 | `deal-priority-plugin` (풀) | `deal-summary-plugin` (SQL) | **본 스킬 (통합)** |
|---|---|---|---|
| 딜 범위 | SQL + Won 고정 | SQL만 | **status_scope 파라미터** (SQL/Won/Both) |
| Won 필드 | `edu_start`, `edu_end` 포함 | 제외 | **status_scope='Won'일 때 포함** |
| 담당자 | "Owner Name" 하드코딩 | settings.owner.name | settings.owner.name |
| 활용 플러그인 | 통합 | 딜만 | **딜·운영 공용** |
