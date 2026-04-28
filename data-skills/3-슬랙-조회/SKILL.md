# 스킬 3: 슬랙 조회

> 스킬 1 메모 이후 발생한 딜/교육 관련 최신 대화 보완. **`channel_set`(deal/ops) + LD 필터(`from:` or 멘션)를 쿼리 단계에서 강제**해 false positive·토큰 폭증을 원천 봉쇄.
> 오늘 E2E(260423) 실증 기반 v0.4 → v0.6 (260427: ops 채널 P1/P2/P3 우선순위 도입) → **v0.7 (260427 저녁: 운영 요청 채널 LD 필터 면제 + 부모+답글 thread 자동 수집 — 강의 차수 일정이 답글에 있는 케이스 대응)**.

## 역할

스킬 1(세일즈맵)의 **보조 스킬**. 메모에 기록되지 않은 최신 내부 대화 수집:
- 팀 채널에서 딜 관련 업데이트
- OM → LD DM으로 온 긴급 사항
- Owner Name이 공지한 일정 변경·요청 사항

**오늘 E2E 핵심 발견:** 슬랙이 세일즈맵 메모보다 **훨씬 최신 정보**를 갖고 있는 경우 많음 (Customer B 4/20 일정 변경 등). 단, **고객사명만으로 검색하면 false positive 90%** (Project X → 결제봇 메시지 등). Owner Name 필터 필수.

## 토큰·시간 예산 (원칙 4)

| 상황 | 응답 크기 목표 | 호출 수 |
|---|---|---|
| 평상시 (4딜, Owner Name 필터 적용) | 딜당 ~5KB, 총 ~20KB | 딜당 1회 |
| 폴백 1 — 쿼리 실패 (OR 너무 김) | 고객사 2~3개씩 분할 | 최대 2회 |
| 폴백 2 — 결과 >30KB | 서브에이전트 요약 위임 | 1회 추가 |

**원천 봉쇄 전략 (핵심):**
- `from:<@Owner Name_user_id>` 또는 Owner Name 멘션 **쿼리에 항상 포함** — Owner Name 무관 메시지 원천 차단
- `include_context=false` 기본 (컨텍스트 메시지 제외)
- `response_format="concise"` 기본
- `limit=10` (기본 20의 절반)
- `channel_set` 필터 `in:` 모디파이어로 강제

## 인풋

| 파라미터 | 출처 | 용도 |
|---|---|---|
| `organization_name` | 스킬 1 | 고객사명 키워드 |
| `deal_name` / `course_id` | 스킬 1 | 과정명 추가 키워드 (다중 딜 구분) |
| `owner_name` | `settings.owner.name` | "Owner Name" (멘션용) |
| `owner_slack_user_id` | `settings.owner.slack_user_id` | Slack user_id (`from:` 필터용) |
| `channel_set` | 오케스트레이터 | `"deal"` or `"ops"` |
| `last_touch_date` | 스킬 1 | 검색 시작 시점 (이 날짜 이후) |

## 채널 구성 — `channel_set`별 분기

### `channel_set = "deal"` (딜 플러그인)
3개 축:
1. **공통**: `slack_common_channels`
2. **팀**: `slack_team_channels[owner.team]`
3. **조건부**: `slack_conditional_channels` (고객사 키워드 매칭 시만 추가)

### `channel_set = "ops"` (운영 플러그인) — v0.6 우선순위 구조

채널을 **P1(직접 운영) > P2(강사·매칭) > P3(전체)** 우선순위로 분류해 **검색 단계에서 계단형 적용**. 운영 관련 내용은 P1 채널에 거의 집중되므로 평상시 P1만 검색하고, 부족할 때만 단계적으로 확장.

3개 축 + 우선순위:
1. **공통**: `slack_ops_common_channels.{P1,P2,P3}` (삼성전자 채널은 v0.5에서 conditional로 이전)
2. **팀**: `slack_ops_team_channels[owner.team].{P1,P2,P3}`
3. **조건부**: `slack_conditional_channels` — Won 딜 고객사가 키워드 매칭될 때만 해당 채널 추가 (P 분류 없이 별도 처리). 예: Owner Name Won 4딜에 삼성 없음 → `b2b_삼성전자` 미추가.

**우선순위 의미:**
- **P1** = 체크포인트 증거 1차 소스 (직접 운영 채널). 운영 관련 내용 거의 여기 집중 → **기본 검색 대상**
- **P2** = 강사·매칭 (강사계약·교안 컨펌 체크포인트). P1 0건일 때 1차 폴백으로 확장
- **P3** = 보충 정보 (전체 채널, 노이즈 多). P1+P2 0건일 때 2차 폴백으로 확장

**검색 흐름 (계단형):**
1. **기본 쿼리**: P1 채널만 `in:` 필터에 포함 → 평상시 토큰·시간 절약 + 노이즈 자연 감소
2. **폴백 1** (P1 0건): P1+P2로 재검색
3. **폴백 2** (P1+P2 0건): P1+P2+P3 전체로 재검색
4. **그래도 0건**: `activity_flag: 대화 부재` 처리 (STEP 4 폴백 4)

**부가 메타데이터:** 검색 결과의 채널은 `collect_evidence.py`에서 `channel_priority` 필드(P1/P2/P3/unknown)로 라벨링됨 — 디버깅·검증·어느 폴백 단계까지 갔는지 추적용 메타데이터.

## 처리 흐름

```
STEP 1: 채널 목록 조립 (settings 기준, ops는 P1만 기본)
  ↓
STEP 2: 딜별 검색 쿼리 조립 (Owner Name 필터 강제 포함)
  ↓
STEP 3: 딜별 검색 실행 (concise + context 제외)
  ↓
STEP 4: 폴백 (P1→P2→P3 단계 확장, 결과 큼 시 분할/위임)
  ↓
STEP 5: 딜별 메시지 정리 + LLM 요약 (간결)
  ↓
STEP 6: 대화 부재 판별
```

## STEP 1~2: 쿼리 조립

### 기본 쿼리 포맷 (핵심 변경)

```
(from:<@{slack_user_id}> OR @{owner_name}) {고객사명} {과정명토큰} {채널필터} after:{last_touch_date}
```

**필수 요소:**
- **`from:` OR 멘션 둘 중 하나 필수** (Owner Name 무관 메시지 원천 차단)
- `{고객사명}` — 기본 키워드
- `{과정명토큰}` — 다중 딜 고객사 구분 (예: "Bootcamp Z", "Project X", "PO교육")
- `in:` 채널 필터 — `channel_set` 해당 채널만
- `after:` — last_touch_date 이후만 (통상 최근 2주)

### 쿼리 예시 (운영 모드, 4딜) — v0.6 P1 기본 검색

**기본 (P1 채널만, 교육 2팀 기준):**

| 딜 | 쿼리 |
|---|---|
| Customer B AI 정기특강 | `(from:<@U123> OR @Owner Name) Customer B 정기특강 in:b2b_2팀_운영요청 in:b2b_2팀_운영논의 in:b2b_운영요청_alert after:2026-04-09` |
| Customer C Bootcamp Z | `(from:<@U123> OR @Owner Name) Customer C Bootcamp Z in:<P1 3채널> after:2026-04-09` |
| Customer H Project X PO | `(from:<@U123> OR @Owner Name) Customer H Project X in:<P1 3채널> after:2026-04-09` |
| Customer I 전사 생성형AI | `(from:<@U123> OR @Owner Name) Customer I in:<P1 3채널> after:2026-04-09` |

**P1 0건 시 폴백 — 단계 확장:**
- 폴백 1차 (P2 추가): `... in:<P1 3채널> in:b2b_2팀_skillmatch in:b2b_skillmatch ...`
- 폴백 2차 (P3 추가): `... in:<P1·P2 5채널> in:b2b_2팀_all ...`
- 조건부 채널은 P 단계와 무관 — Won 딜 고객사 매칭 시 항상 추가 (예: `in:b2b_삼성전자`)

### Slack 검색 문법 주의
- 공백은 AND. OR는 명시적으로 써야 함
- `in:` 다중 지정 가능 — 결과가 해당 채널들 **중 하나**에 있으면 매칭 (사실상 OR)
- `from:<@USER_ID>` 꺾쇠 필수 (username 형태보다 안정)

### 다중 딜 구분 강화 (과정명 토큰 필수)

**오늘 E2E 교훈:** Customer C 고객사명만으로 검색 시 15건 중 3건만 Bootcamp Z 관련. **과정명 토큰을 쿼리에 반드시 포함**해야 정확도 유지.

추출 규칙:
- 딜명에서 핵심 명사 1~2개 (예: "Customer C 26년 Bootcamp Z" → "Bootcamp Z")
- 강의명 키워드 (예: "PM/PO 교육" → "PO교육")
- 제품/코드명 (예: "Project X")

## STEP 3: 검색 실행

```
도구: mcp__claude_ai_Slack__slack_search_public_and_private
파라미터 기본값 (원칙 4):
  limit: 10
  include_context: false
  response_format: "concise"
  sort: "timestamp"
  sort_dir: "desc"
```

**왜 이렇게 기본값을 빡빡하게:** 지난 테스트에서 `limit=20` + `include_context=true` (기본값)으로 돌려 쿼리당 **78K 문자** 응답. 이 기본값이면 쿼리당 ~5KB로 수렴.

## STEP 4: 폴백 (필요 시만)

### 폴백 1 — 쿼리 문법 실패 (OR 너무 김)
- 증상: Slack API 에러 또는 결과 이상
- 대응: OR 쿼리를 두 번에 분할 (from: 쿼리 / 멘션 쿼리 각각)

### 폴백 2 — 결과 여전히 크다 (>30KB)
- 증상: limit=10 적용했어도 메시지 텍스트가 너무 김
- 대응: **서브에이전트에 요약 위임**. 파일로 저장 후 에이전트가 읽고 구조화 요약 반환

### 폴백 3 — P1 0건 (우선순위 단계 확장, v0.6 신규)
- 증상: ops 모드에서 기본 P1 채널만 검색했는데 결과 0건
- 대응 (계단형 확장):
  - **1차**: `in:` 목록에 **P2 채널 추가** 후 재검색 (강사·매칭 채널)
  - **2차**: 1차에서도 0건이면 **P3 채널 추가** 후 재검색 (전체 채널)
  - **3차**: 그래도 0건이면 폴백 4(대화 부재)로 진입
- 의도: 평상시 P1만 검색해 토큰·시간 절약 + 노이즈 자연 감소. 정말 P1에 흔적 없는 케이스만 확장 비용 발생
- 적용: 채널 목록은 `settings.json`의 `slack_ops_*_channels.{P1,P2,P3}`에서 단계별 조립

### 폴백 4 — 전체 0건 (대화 부재)
- 폴백 3까지 진행해도 0건일 때
- 쿼리 조건 완화 (Owner Name 필터는 유지, 과정명 토큰만 빼기)
- 그래도 0건이면 `activity_flag: "⚠️ 대화 부재 — N일간 소통 없음"`

## STEP 5: 딜별 LLM 요약

딜별 검색 결과(최대 10건)를 **한 줄로 압축**:

```
예시: "4/20 Owner Name이 팀에 '일정 변경 공지(싱크업 문서 반영)'; Instructor A 강사 고객사 자료 전달 스레드 진행 중"
```

### 왜 LLM 일괄 처리 안 하는가 (기존 유지)
딜별 슬랙 메시지는 맥락 완전히 다름. 일괄 LLM에서 딜 간 혼동 시 중요 정보 누락 위험 → **딜별 개별 LLM 호출**.

**토큰 이코노미:** 딜당 LLM 호출 1회, 입력 ~3KB, 출력 ~100자 → 딜당 요약 1KB 수준. 4딜 = ~4KB.

## STEP 6: 대화 부재 판별

| 조건 | 판별 | 플래그 |
|---|---|---|
| 슬랙 결과 있음 | 정상 | 없음 |
| 슬랙 없음 + 메모 2주 이내 | 정상 | 없음 |
| 슬랙 없음 + 메모 2주 초과 | **대화 부재** | `"⚠️ 대화 부재 — 마지막 메모 N일 전..."` |

## STEP 7: 운영 요청 채널 thread 자동 수집 (v1.1, 운영 플러그인 전용)

> `channel_set == "ops"`일 때만 실행. 딜 플러그인은 STEP 7 건너뛰기.
>
> **목적:** 다차수 교육의 회차 정보(`N차 M/D~M/D`, `M월 D, D` 등)는 LD 멘션 없는 OM 답글에 들어있는 경우 많음. 이를 자동 수집해 `compose_schedule.py`의 정형 차수 분해 입력으로 제공.
>
> **배경:** v1.0에서는 `runtime/s3_slack_ops_requests.json`을 LD가 수동 큐레이션해야 했음 → 안 하면 "세부 회차 미확정" lump 발생 (260428 E2E 실측). v1.1에서 자동화.

### 7-1. 검색 채널

`settings.slack_ops_team_channels[owner.team].P1` 값 그대로 사용.
- 예: 교육 2팀 → `["b2b_2팀_운영요청", "b2b_2팀_운영논의"]`

### 7-2. 딜별 thread parent 검색 (병렬, 한 번의 function_calls 블록)

각 Won 딜에 대해 `mcp__claude_ai_Slack__slack_search_public_and_private` 호출:

**쿼리 포맷 (v1.1.1, 260428 보강):**
```
in:<P1채널1> in:<P1채널2> {customer_token} after:<edu_start - 60일 YYYY-MM-DD>
```

- `customer_token`: 딜의 `organization_name`만 사용 (예: `Customer F`). **과정명 토큰은 1차 검색에서 제외** — 세일즈맵 deal_name과 운영 채널 코스명이 다를 수 있음 (260428 발견: deal `호텔롯데-AI 시너지` vs ops thread `롯데호텔앤리조트_바이브코딩 과정`)
- **검색 윈도우**: `after: edu_start - 60일` (딜마다 동적). 운영 요청 thread는 보통 운영 시작 1~2개월 전 작성. 30일 윈도우는 회차 정보 thread 놓침 (260428 실측)
- **STEP 1~6과 다른 점:** owner 멘션·`from:` 필터 **포함하지 않음** (운영 요청 채널은 OM·다른 팀원 답글이 핵심 정보원)
- **`response_format="detailed"` 필수** (260428 검증) — concise 모드는 ts·thread_ts·channel_id 추출 어려움 → detailed로 호출

**부모 메시지 추출:**
- 검색 결과 각 메시지에서 `thread_ts` 또는 `ts` 확인
- `thread_ts == ts`인 메시지만 부모 (답글 제외)
- 같은 thread는 중복 제거 (parent_ts 기준 unique)

### 7-2.1. LLM 본문 검증 (false positive 차단 + 정형 thread 우선순위, 260428 보강)

Slack 검색은 단일 토큰 매칭도 결과에 포함하므로, **검색 결과 raw 본문을 LLM이 한 번 더 검증**:

1. **고객사명 직접 등장 확인** (필수)
   - 부모 본문에 `customer_token` 단어가 명확히 등장하는가? (다른 고객사 메시지 OR 매칭 제거)
   - 등장 안 하면 → false positive로 제외

2. **BOT 작성 + 정형 양식 thread 최우선 채택** (v1.1.3, 260428 보강 — 필수)
   - 운영 요청 채널의 **OM 배정 요청 BOT**(예: 작성자 이름 `2팀 OM 배정 요청` 또는 봇 ID 형식 `B*`)이 작성한 thread가 핵심 정형 소스
   - 본문에 다음 패턴 **모두 또는 다수 등장**하면 **무조건 채택** (검색 결과의 다른 자유 서술 thread보다 우선):
     - `1. 요청자명:` 또는 `요청자명:`
     - `2. 코스명:` 또는 `코스명:` / `과정명:`
     - `3. 강의 일정:` 또는 `강의 일정:`
     - `1차 M/D` / `M월 D일` 회차 정형 표기
   - **검색 후 본문 검증 단계에서 BOT 정형 thread를 false positive로 제외하지 말 것** — 봇은 owner 멘션 없어도 정형 thread 정확. 단일 토큰 매칭 X 룰의 예외.
   - 운영 시작 전 OM이 BOT 통해 만든 thread는 회차 분해의 단일 진실 소스(single source of truth)

3. **자유 형식 thread (회차 정보 약함)**
   - LD가 자유 서술한 thread (예: "3월 11일 10:00-11:00 씽크업", "7/1·7/2·7/8·7/9 4시간×4회")는 정규식 매칭 약함
   - **BOT 정형 thread가 없을 때만 폴백 소스로 활용** — BOT thread 있으면 자유 서술형은 무시
   - 답글까지 봐야 차수 분해 가능 → STEP 7-3에서 read_thread 후 combined_text 활용

**260428 실측 (정통 흐름 미분해 사례):**
- 호텔롯데 BOT thread `1770855296.526319` (2/12, 부모 "1차 4/6~8 ... 8차 6/30~7/8" 8차수 정형) ⭐ 핵심 소스
- HL만도 BOT thread `1772069983.632159` (2/26, 답글에 차수 분해) ⭐ 핵심 소스
- 위 BOT thread를 LLM이 검색 결과에서 false positive로 제외 → 자유 서술형만 채택 → 정규식 매칭 0건 → 다차수 분해 lump
- → BOT 정형 thread 최우선 채택 룰로 차단

### 7-3. Thread 답글 자동 수집 (병렬)

각 부모 thread마다 `mcp__claude_ai_Slack__slack_read_thread`:

```
slack_read_thread(channel_id=<채널 ID>, message_ts=<thread_ts>, limit=100)
```

- 부모 + 모든 답글 가져오기
- `combined_text` 조립: 부모 본문 + `\n\n--- 답글 ---\n` + 답글 본문 N개 시간순 join

**답글이 차수 정보 핵심인 경우 (260428 발견):**
- 부모 본문이 `<시트 링크>`나 단순 알림(`@강연정 HL만도_2026 AX 교육`)만 있어도 답글에 차수 분해 정형 데이터(`기초: 4월 6, 8 / 5월 11, 12`) 들어있는 경우 多
- 모든 답글을 빠짐없이 합쳐 `combined_text`에 포함 (compose_schedule의 정규식이 답글 텍스트도 매칭)

### 7-4. 출력 형식 (compose_schedule.py 입력 호환 — 변경 없음)

`runtime/s3_slack_ops_requests.json`:

```json
{
  "_comment": "v1.1 자동 수집 (스킬 3 STEP 7) — 수동 큐레이션 불필요",
  "_meta": {
    "generated_at": "2026-04-28T10:43:00",
    "channels_searched": ["b2b_2팀_운영요청", "b2b_2팀_운영논의"],
    "deal_count": 3,
    "thread_count": 7
  },
  "<deal_id_1>": {
    "deal_name": "...",
    "threads": [
      {
        "channel": "b2b_2팀_운영요청",
        "thread_ts": "1745678900.123456",
        "permalink": "https://...",
        "combined_text": "부모 메시지 본문\n\n--- 답글 ---\n답글1\n답글2..."
      }
    ]
  }
}
```

### 7-5. False positive 방지 (정확도 룰, 260428 보강)

- **STEP 7-2.1 LLM 본문 검증 강제** — Slack 검색 자체가 OR 처리 가능하므로 결과 본문 재확인 필수
- **30일 이내** thread만 (오래된 차수 정보는 변경 가능성 ↑ 노이즈)
- 매칭 0건 시 → 해당 deal에 빈 `threads: []`로 저장 (compose_schedule이 폴백 처리)

### 7-6. LD 안내 (자동 수집 결과 부족 시)

**케이스 A — `thread_count == 0`:**
> *"운영 요청 채널에서 회차 정보 자동 수집 0건. 슬랙에 직접 thread 있으면 키워드(고객사명+과정명) 정확도 확인 필요."*

**케이스 B — 자유 형식 thread (정형 패턴 매칭 어려움):**
260428 E2E 발견 — 일부 딜은 thread는 있으나 본문이 자유 서술형(예: *"3월 9일부터 매주 월요일..."*)이라 `compose_schedule.py`의 정형 정규식(`N차 M/D~M/D` / `M월 D, D`)에 매칭 안 됨. 이 경우 thread 수집은 성공하나 차수 분해 결과 0.
- LD 안내: *"{deal_name}: 운영 채널 thread 자유 형식 — 자동 차수 분해 어려움. 드라이브 session_schedule 또는 자연어 피드백으로 보완 권장."*

LD가 자연어 피드백으로 보완 가능. (시스템 본질 — 100% 자동 ≠ 목표)

---

## 아웃풋

```json
{
  "deal_id": "019c03e2-54b4-7112-9c4b-9c3ed96656d0",
  "deal_name": "Customer H_PO 교육(Project X개발그룹)",
  "slack_results": [
    {
      "date": "2026-04-23",
      "channel": "b2b_2팀_운영요청",
      "channel_priority": "P1",
      "author": "API Caller",
      "message_preview": "...",
      "thread_link": "..."
    }
  ],
  "slack_summary": "4/23 API Caller → Owner Name DM: Project X PO 교육 4주 일정 확인 요청",
  "activity_flag": null,
  "meta": {
    "query_used": "(from:<@U123> OR @Owner Name) Customer H Project X ...",
    "result_count": 1,
    "fallback_used": null
  }
}
```

## 채널 목록 예시 (교육 2팀 기준, settings.json 참조)

### 딜 플러그인 (총 7채널, 삼성 조건부 +1)
- 공통 3: `b2b_all`, `b2b_lead`, `b2b_skillmatch`
- 2팀 4: `b2b_2팀_견적제안`, `b2b_2팀_all`, `b2b_2팀_skillmatch`, `b2b_2팀_제안노트`
- 조건부: 삼성 딜 있으면 `b2b_삼성전자`

### 운영 플러그인 (교육 2팀 기준, 총 6채널 + 조건부) — v0.6 P1/P2/P3 분류

| 우선순위 | 공통 | 팀 (교육 2팀) |
|---|---|---|
| **P1** (직접 운영) | `b2b_운영요청_alert` | `b2b_2팀_운영요청`, `b2b_2팀_운영논의` |
| **P2** (강사·매칭) | `b2b_skillmatch` | `b2b_2팀_skillmatch` |
| **P3** (전체) | — | `b2b_2팀_all` |

조건부: Won 딜에 삼성 고객사 있으면 `b2b_삼성전자` 추가 (P 분류 없음).

### 운영 플러그인 (교육 1팀 기준)

| 우선순위 | 공통 | 팀 (교육 1팀) |
|---|---|---|
| **P1** (직접 운영) | `b2b_운영요청_alert` | `b2b_1팀_운영논의`, `b2b_1팀_운영파트` |
| **P2** (강사·매칭) | `b2b_skillmatch` | — |
| **P3** (전체) | — | `b2b_1팀_all` |

※ 1, 2팀 채널 구성이 약간 비대칭 (1팀 운영요청 채널 없음 / 2팀 운영파트 채널 없음 / 1팀 팀별 skillmatch 없음). 채널 관리 방식 차이로 정상.

## OAuth·인증 (이슈 #15, #16 대응)

**배포 시 주의사항:**
- 슬랙 MCP는 claude.ai 커넥터 경유. 새 세션·새 설치 시 `/mcp` 커맨드로 재인증 필요
- 인증 완료 후 도구 반영에 수초~수십 초 지연 가능 — 안 잡히면 세션 재시작

사용자 가이드에 반드시 명시 (실배포 LD 온보딩용).

## 알려진 제약·주의사항

| 항목 | 내용 |
|---|---|
| Slack 검색 boolean | `AND` 기본, `OR`는 명시. `NOT` 없음 |
| 고객사 약어 | 메시지에 "S1", "LGE" 등 약어 쓰면 매칭 누락 — 딜명 토큰에 약어도 포함 고려 |
| 플러그인 메타 대화 | 개발 관련 DM이 검색에 걸림 — 요약 단계에서 "플러그인 개발 맥락" 감지 시 제외 |
| 채널 이름 특수문자 | 한글 채널명도 `in:b2b_2팀_all` 형태로 지원 |

## LD 체크 포인트

1. 딜 논의 시 고객사명 명확히 언급 (약어 피하기)
2. 중요 공지는 **DM보다 채널** (검색에 잘 걸림)
3. 과정명 키워드 일관 사용 (예: "Bootcamp Z" vs "Bootcamp Z" — 하나로 통일)

## 기존 v0.3과의 차이

| 항목 | v0.3 | v0.4 |
|---|---|---|
| Owner Name 필터 | "사용 권장" (선택) | **쿼리에 강제 포함** |
| `include_context` | 기본값(true) 사용 | **false 강제** |
| `response_format` | 미지정 | **"concise" 강제** |
| `limit` | 미명시(기본 20) | **10 강제** |
| 과정명 토큰 | 선택적 | **다중 딜 고객사에 필수** |
| 서브에이전트 | 운영 중 발견 시 | **폴백 경로로 명문화** |
| 토큰 예산 | 미명시 | **딜당 ~5KB 목표** |

## 버전

- v0.3 (2026-04-22): channel_set 분기 도입
- v0.4 (2026-04-23): E2E 실증 기반 — 원천 봉쇄 원칙 4 반영, Owner Name 필터 강제, 기본값 타이트닝
- v0.5 (2026-04-24): ops 공통 채널에서 `b2b_삼성전자` 제거 후 조건부(`slack_conditional_channels`)로 이전. Won 딜에 해당 고객사 있을 때만 추가되는 구조로 통일 (deal/ops 모두).
- v0.6 (2026-04-27): ops 채널 P1/P2/P3 우선순위 도입 — **검색 단계 계단형 적용**. 기본 검색은 P1 채널만 → 0건 시 P2 폴백 → 여전히 0건이면 P3 폴백 (STEP 4 폴백 3). 평상시 토큰·시간 절약 + 노이즈 자연 감소가 의도. settings.json 구조 평면 리스트 → P 객체. `collect_evidence.py`의 `channel_priority` 라벨링은 메타데이터·폴백 단계 추적용으로 유지. `b2b_2팀_운영요청` 신규 추가.
- v0.7 (2026-04-27 저녁): **운영 요청 채널 (`b2b_2팀_운영요청`·`b2b_운영요청_alert`) 특수 처리**.
  - **LD 필터 면제**: 채널 자체가 운영 신호 강한 정형 양식이라 `from:LD OR @LD` 강제 안 함. OM·운영 매니저가 LD 멘션으로 작성한 강의 일정·차수 메시지 누락 방지.
  - **부모 + 답글(thread) 자동 수집**: 운영 요청 채널은 보통 부모(코스명+시트 링크) + 답글(차수별 일정 분해) 구조. `slack_search`만으론 부모만 잡힘 → reply_count > 0이면 `slack_read_thread` 자동 호출.
  - **결과 형식**: `s3_slack.json`에 `ops_request_threads[]` 필드 추가 (부모+답글 결합 텍스트). `compose_schedule.py`가 이 필드 보고 차수 분해 추출.
  - **검색 쿼리 예시 (운영 요청 채널, LD 필터 면제):**
    ```
    {고객사명} in:b2b_2팀_운영요청 after:2026-01-01
    ```
