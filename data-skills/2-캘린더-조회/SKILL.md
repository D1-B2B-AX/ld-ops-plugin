# 스킬 2: 캘린더 조회 (모드 무관 personal 단일)

> 스킬 1의 시간 정보 보강. **ops/deal 모두 owner의 개인 캘린더(personal) 단일 사용**.
> v0.5 (2026-04-24): 테스트 대상 Owner Name 전환 + LD별 운영 방식 차이 반영해 ops도 personal로 단순화. 공용 캘린더(team-shared-calendar@example.com) 경로는 일정 조립 레이어의 노션(2순위) 루트로 대체.

## 역할

| 모드 | 사용 캘린더 | 용도 |
|---|---|---|
| `ops` (운영) | **personal 단일** (`{owner.email}`) | 📅 일정 핵심 재료 (LD 본인 캘린더에 등록된 교육 일정) |
| `deal` (딜) | **personal 단일** (`{owner.email}`) | 딜 미팅·팔로업 |

**설계 전제:** LD는 본인 개인 캘린더에 담당 교육 일정을 등록·관리함. 공용 캘린더 사용 여부는 LD별 편차가 커서 의존하지 않는다.
- 다차수 교육의 세부 회차 일정은 **노션 강의 캘린더(스킬 6, 2팀 전용)** 에서 별도 수집. 스킬 2는 일반 교육·미팅·팔로업 이벤트 커버.
- 테스트 모드에서는 api_caller(API Caller)가 owner(Owner Name) personal 캘린더를 **구독**한 상태라 `calendar_id=owner.email` 호출로 접근 가능.

## 토큰·시간 예산 (원칙 4)

| 상황 | 응답 크기 목표 | 호출 수 |
|---|---|---|
| 평상시 (운영 모드, 6주 범위) | ~5KB | 1회 |
| 평상시 (딜 모드, 2주 범위) | ~5KB | 1회 |
| 폴백 — 이벤트 >100 | 날짜 범위 축소 (3주로) | 1회 재호출 |

**원천 봉쇄 전략:**
- `max_results=50` 상한
- `detailed=false` (기본값) — 참석자·설명 등 제외. 필요하면 개별 `get_events(event_id)` 호출
- 고정 날짜 범위 (운영 6주, 딜 2주)

## 인풋

| 파라미터 | 출처 | 설명 |
|---|---|---|
| `search_keywords` | 스킬 1 | 딜별 고객사명·과정명 토큰 |
| `calendar_id` | `settings.owner.email` (role=personal) | ops/deal 모두 `{owner.email}` 고정 |
| `api_caller_email` | `settings.data_sources.api_caller_email` (테스트) 또는 `owner.email` (배포) | `user_google_email` 파라미터에 쓸 값. 스킬 4와 동일 규칙 |
| `time_range` | 모드별 고정 | `ops`: 6주 (지난 1주~미래 5주), `deal`: 2주 (오늘~14일) |
| `mode` | 오케스트레이터 지정 | `ops` or `deal` (범위만 달라짐, 캘린더는 동일 personal) |

**API caller vs owner 구분:** 스킬 4 참조. 테스트 모드에선 `api-caller@example.com`(API Caller)이 호출 주체이고, owner(Owner Name) personal 캘린더를 구독한 상태라 `calendar_id={owner.email}`로 접근 가능. 배포 시 api_caller=owner 자동 대체.

## 처리 흐름

```
STEP 1: 모드에 따라 날짜 범위만 설정 (캘린더는 personal 고정)
  ↓
STEP 2: personal 캘린더에서 날짜 범위 이벤트 조회 (1회 호출)
  ↓
STEP 3: 딜별 매칭 (브래킷 패턴 → 고객사명 포함 순)
  ↓
STEP 4: 매칭 안 된 이벤트 자동 제외 (사무실·반복 위클리 등 노이즈 포함)
  ↓
STEP 5: 딜별 이벤트 리스트 구조화
```

## STEP 1~2: 캘린더 조회

### 운영 모드 (`mode=ops`)
```
calendar_id: {owner.email}
time_min: 오늘 - 7일 (지난 주 진행분 파악)
time_max: 오늘 + 35일 (5주 앞까지)
max_results: 50
detailed: false
```

### 딜 모드 (`mode=deal`)
```
calendar_id: {owner.email}
time_min: 오늘
time_max: 오늘 + 14일
max_results: 50
detailed: false
```

## STEP 3: 딜 매칭 규칙

personal 캘린더는 LD별 기록 방식 편차가 있으므로 **브래킷 패턴 우선 + 고객사명 단순 포함 폴백** 2단계로 매칭.

### 매칭 알고리즘 (우선순위)

**1단계: 브래킷 패턴 매칭 (LD가 `[고객사] 교육명` 형식 쓸 때)**
```
이벤트 제목이 "[{고객사}] {교육명}" 패턴 → 고객사 토큰으로 딜 후보 필터
```

예:
- `[Customer B] 1회차 ~ AI 핵심 용어와 금융권 도입 사례` → 고객사 "Customer B" 매칭
- `[Customer C] Bootcamp Z 1회차` → 고객사 "Customer C" + 교육명 매칭
- `[Customer H] PM/PO 교육 1회차` → 고객사 "Customer H" + 교육명 매칭

**2단계: 고객사명 단순 포함 매칭 (브래킷 안 쓰는 LD용 폴백)**
```
제목 또는 설명에 {고객사명} 토큰 포함 → 해당 딜 매칭
예: "Customer D 싱크업 미팅" → Customer D 딜 매칭
예: "Customer E 1회차 교육" → Customer E 딜 매칭 (약어도 별칭으로 인식)
```

**3단계: 교육명 토큰 매칭 (같은 고객사 다중 딜 구분)**

같은 고객사에 여러 딜 있을 때 — 교육명 토큰 유사도로 구분:
```
[Customer C] CL과정 vs [Customer C] Bootcamp Z
→ 딜명의 주요 토큰 (예: "Bootcamp Z" or "CL 승격자") 매칭
```

LLM 보조 불필요 (텍스트 토큰 매칭으로 충분). 토큰 겹치는 게 2개 이상이면 1순위 매칭.

### 매칭 안 되는 이벤트
- 개인 일정("사무실", "위클리", "AX 1:1 세션", 식사 등) — 자동 제외
- 고객사명·브래킷 어느 쪽도 없는 일반 미팅 — 자동 제외
- 딜 모드: 브래킷 없이 고객사명만 포함된 미팅도 매칭 허용 (예: "한화솔루션 싱크업 미팅")

## STEP 5: 아웃풋

```json
{
  "deal_id": "019c03e2-54b4-7112-9c4b-9c3ed96656d0",
  "deal_name": "Customer H_PO 교육(Project X개발그룹)",
  "matched_events": [
    {
      "date": "2026-04-27",
      "start_time": "09:00",
      "end_time": "18:00",
      "title": "[Customer H] PM/PO 교육 1회차",
      "type": "교육",
      "days_from_today": 4,
      "calendar_id": "{owner.email}"
    },
    {
      "date": "2026-04-28",
      "start_time": "09:00",
      "end_time": "18:00",
      "title": "[Customer H] PM/PO 교육 1회차",
      "type": "교육",
      "days_from_today": 5,
      "calendar_id": "{owner.email}"
    },
    ...
  ]
}
```

매칭 0건: `"matched_events": []`

### 이벤트 타입 자동 분류

| 제목 키워드 | 분류 |
|---|---|
| "교육", "N회차", "N일차", "N차수" | **교육** |
| "싱크업", "미팅", "회의", "콜", "방문" | **미팅** |
| "fu", "f-up", "팔로업", "메일" | **fu** |
| 그 외 | **기타** |

## 다른 스킬과의 연결

| 연결 | 내용 |
|---|---|
| 스킬 1 → 스킬 2 | `search_keywords.organization_name` 전달 |
| 스킬 2 → classify_items | `matched_events`가 📅 일정의 `schedule[]` 항목 구성 |
| 스킬 2 → generate_ops_md | 세션 세부 일정이 운영일지 카드에 표시 |

## MCP 호출

```
도구: mcp__workspace-mcp__get_events
단일 호출:
  user_google_email: {api_caller_email}  # 테스트=API Caller, 배포=owner.email
  calendar_id: {owner.email}             # 모드 무관 personal 고정
  time_min, time_max: 모드별 범위
  max_results: 50
  detailed: false
```

## 알려진 제약·주의사항

| 항목 | 내용 |
|---|---|
| 비공개 일정 | "No Title" / 비공개 이벤트 자동 제외 |
| 약어 매칭 누락 | "HDS" → "현대건설" 매칭 X — 제목에 정식 고객사명 사용 권장 |
| 테스트 구독 | 테스트 모드는 api_caller(API Caller)가 owner(Owner Name) personal 캘린더 구독 필수 |
| 다중 세션 | 같은 교육의 1일차·2일차·3일차는 각각 별도 이벤트로 수용. 운영일지에서 그룹화 |
| 다차수 세부 회차 | 노션 강의 캘린더(스킬 6, 2팀 전용)가 더 일관된 소스. 스킬 2는 일반 이벤트 커버용 |

## 사용자(LD) 가이드

1. **개인 캘린더에 `[{고객사}] {교육명} {N회차/N일차}` 패턴 등록** — 매칭 정확도 최고
2. 비대면 미팅은 제목에 "[비대면미팅]" 접두어
3. 개인 일정(사무실·집·반복 위클리)은 자동 필터됨 — 고객사·브래킷 없는 이벤트는 무시

## 버전별 변경

| 항목 | v0.3 | v0.4 | v0.5 |
|---|---|---|---|
| 운영 모드 캘린더 | 이중(personal + team) | 단일(team만) | **단일(personal만)** |
| 딜 모드 캘린더 | personal | personal | personal |
| 매칭 로직 | 고객사명만 | 브래킷 패턴 | **브래킷 → 고객사명 2단계** |
| 노션 캘린더 통합 | 없음 | 없음 | **스킬 6으로 분리** (2팀 전용) |
| 토큰 예산 | 미명시 | ~3KB | ~5KB |

## 버전

- v0.3 (2026-04-22): 이중 캘린더 지원 초안
- v0.4 (2026-04-23): E2E 실증 — 운영 모드 team 단일, 브래킷 패턴 매칭
- v0.5 (2026-04-24): 테스트 대상 Owner Name 전환 + LD별 운영 방식 편차 반영. ops/deal 모두 personal 단일. 공용 캘린더 경로는 노션 강의 캘린더(스킬 6)로 대체. 매칭 폴백에 고객사명 단순 포함 추가.
