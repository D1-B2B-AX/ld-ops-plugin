# 스킬 5: 드라이브 기획·견적·싱크업 조회

> 딜/교육별 관련 **구글 시트·엑셀 문서**를 Drive에서 찾아 핵심 정보를 파싱하는 보조 스킬. 딜·운영 플러그인 공용 (`file_mode` 분기).
> 오늘 E2E(260423) 실증 결과를 반영해 전면 재작성 (v0.4).

## 역할

스킬 1(세일즈맵)의 **보조 스킬**이나 실질적으로 **가장 풍부한 정보원**. 특히 운영(`file_mode=ops`)에서 싱크업 문서의 미팅로그·현황이 🎯 액션·📊 진행 현황·✅ 투두의 핵심 재료.

| file_mode | 대상 문서 | 주 활용 |
|---|---|---|
| `deal` | 기획시트, 견적서 | 딜 플러그인 (SQL 판단) |
| `ops` | 기획시트, 싱크업, 강의준비문서 | 운영 플러그인 (Won 운영) |

## 토큰·시간 예산 (원칙 4)

| 상황 | 응답 크기 목표 | 호출 수 목표 | 비고 |
|---|---|---|---|
| 평상시 (딜 4건) | 총 ~20KB | 4~10회 | 딜당 파일 1개, 파일당 3~5탭 |
| 폴백 1 — 큰 파일 | 요약 LLM 1회 추가 | | 탭 >10개 또는 단일 탭 >1000행 |
| 폴백 2 — 병합 매칭 실패 | 경고 + `planning_sheet: null` | | 매칭 실패 = 포기 |

**원천 봉쇄 전략:**
- 탭 whitelist 필수 (키워드 기반) — 전체 탭 스캔 금지
- 탭당 40행 상한
- 파일당 최대 읽을 탭 수 = **5개** (싱크업은 최신 3개 세션 탭만)
- 매칭 안 된 딜은 2차 검색 1회만 시도, 실패 시 포기

## 인풋

| 파라미터 | 출처 | 용도 | 우선순위 |
|---|---|---|---|
| `organization_name` | 스킬 1 | Drive 검색 키워드, 매칭 1순위 | 🥇 |
| `deal_name` | 스킬 1 | 과정명 토큰 매칭 (LLM 보조) | 🥇 |
| `owner.email` | `settings.owner.email` | **1차 검색 소유자 필터** ("in owners") — 딜 주인공 LD 기준 | — |
| `api_caller_email` | `settings.data_sources.api_caller_email` (테스트) 또는 `owner.email` (배포) | **API 호출용** 인증 계정 (`user_google_email` 파라미터) | — |
| `course_id` | 스킬 1 | 파일명/내용 ID 보조 매칭 | 🥉 |
| `file_mode` | 오케스트레이터 | `deal` or `ops` | — |

**주의 (v0.4 교정):** 드라이브 1차 검색에서 `'{owner.email}' in owners` 조건은 **딜 주인공 LD가 소유한 파일**을 찾는 것. `api_caller_email`은 **API를 호출하는 인증 계정**(workspace-mcp OAuth). 테스트 시 둘이 다르지만 API Caller 계정이 Owner Name 파일 공유받은 상태라 매칭 가능.

## file_mode별 검색 키워드

### `deal` 모드
- 파일명 키워드: `"기획"` OR `"견적서"`

### `ops` 모드
- 파일명 키워드: `settings.data_sources.drive_file_keywords` (기본: `["기획", "싱크업", "씽크업"]`)
- 견적서 제외 (수주 완료 후라 불필요)

## 처리 흐름

```
STEP 1: Drive 1차 검색 (소유자 + 키워드, 1회 통합 호출)
  ↓
STEP 2: 매칭 안 된 딜만 fallback 검색 (다층)
  ↓
STEP 3: 딜-파일 매칭 (4단계 우선순위)
  ↓
STEP 4: 파일 타입 판정 (탭 구조 + 내용 기반)
  ↓
STEP 5: 파일 포맷별 읽기 (Google Sheets / Excel 분기)
  ↓
STEP 6: 생애단계 판정 + LLM 파싱
  ↓
STEP 7: 아웃풋 조립 (타입별·생애단계별 다름)
```

## STEP 1: 1차 검색 (소유자 + 이름/딜명 병행, v0.5 확장)

v0.4는 소유자 필터만 썼으나, 2팀 운영 싱크업은 **OM이 만든 파일**이 많아 owner 소유 외에 누락 발생. v0.5부터 3개 경로를 병렬로 돌린다.

### 쿼리 구성 (1회 호출에 OR 통합)

```
쿼리 (OR 통합):
  (
    '{owner.email}' in owners                             # 경로 A: owner 소유
    OR fullText contains '{owner.name}'                   # 경로 B: 파일 내용·제목에 LD 이름
    OR name contains '{고객사명}'                          # 경로 C: 파일명에 딜 고객사명
    OR name contains '{deal_name_token}'                  # 경로 C': 딜명 주요 토큰
  )
  AND (name contains '기획' OR name contains '싱크업' OR name contains '씽크업')
  AND (
    mimeType = 'application/vnd.google-apps.spreadsheet'
    OR mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  )
```

**경로별 커버 범위:**
- A (owner 소유): LD 본인이 만든 기획·싱크업 문서
- B (owner 이름): OM이나 다른 멤버가 만들었지만 LD 이름이 시트 내 담당자란·참석자란에 들어간 경우 (노션 alias 포함 — 예: "Owner", "Owner")
- C (고객사명): 파일명에 고객사명이 들어가 있으면 — 2팀 운영 표준 네이밍 `_{고객사}_과정명_싱크업 문서.xlsx` 대응
- C' (딜명 토큰): 다중 딜 구분 (딜명 2~3 주요 명사 토큰)

**용어:**
- **MIME 타입(MIME type)** = 파일 종류 식별자. Google Sheets와 Excel(.xlsx)은 MIME가 다름. **둘 다 포함**해야 실무 파일 누락 안 됨 (이슈 #7)

**page_size**: 50. 경로 확장으로 결과 많아지면 딜별 매칭 단계에서 LLM 보조가 필요할 수 있음.

## STEP 2: Fallback 검색 (매칭 안 된 딜만)

3경로 순차 시도. 각 경로 1회 호출만 허용 (토큰 절약).

### 2-a) 고객사명 + 키워드 (주력 경로)
```
쿼리: name contains '{고객사명}' AND (키워드) AND (MIME 스펙)
```

### 2-b) 폴더 검색 + 폴더 내 파일 조회 (폴더 안 케이스)

하나금융 사례처럼 파일이 **폴더 안에 있는 경우**:
```
Step 1) 폴더 검색: name contains '{고객사명}' AND mimeType = folder
Step 2) 매칭 폴더의 자식 파일: 'FOLDER_ID' in parents
        (mimeType 필터 생략 — 폴더 안에 있으면 대부분 관련 파일)
```

### 2-c) course_id 직접 검색 (보조 경로)
```
쿼리: name contains '{course_id}' (6자리 숫자)
```

**적용 조건:** Owner Name 미래 파일 + 네이밍 규칙(`_{course_id}`) 따르는 LD만. 다른 LD는 기대하지 말 것.

## STEP 3: 딜-파일 매칭 (4단계 우선순위)

**오늘 E2E에서 발견한 핵심: 파일명-딜명 불일치는 예외가 아니라 기본값**. 퇴사자 인수, 과정 리브랜딩, 폴더 구조 차이 등.

| 순위 | 방법 | 적용 케이스 |
|---|---|---|
| 🥇 1 | **고객사명 + 딜명 토큰 유사도 + LLM 보조** | 기본 경로 (모든 LD) |
| 🥈 2 | **폴더명·파일명 토큰 매칭** | 대부분 LD |
| 🥉 3 | **파일명 `_{course_id}` 패턴** | Owner Name 미래 파일 |
| 4 | **파일 내용 백오피스 URL course_id** | 우연적 보너스 (기존 일부 파일) |

### 매칭 로직

```python
# pseudo
for deal in deals:
    candidates = search_results_for_deal(deal)
    if len(candidates) == 0:
        deal["planning_sheet"] = None
        deal["match_warning"] = "매칭 파일 없음"
        continue
    if len(candidates) == 1:
        deal["planning_sheet_file"] = candidates[0]
        continue
    # 여러 파일 후보 — LLM 호출
    prompt = "이 딜의 가장 매칭되는 파일을 고르세요: 딜명={deal_name}, course_id={course_id}, 후보 파일={candidates}"
    deal["planning_sheet_file"] = llm_pick_best(prompt)
```

**LLM 호출 비용 최소화:** 후보가 2개 이상일 때만 호출. 1개면 자동 매칭.

## STEP 4: 파일 타입 판정 (탭 구조 기반)

**오늘 E2E에서 발견한 교훈: 파일명만으론 타입 판정 부정확** (이슈 #8). "싱크업문서"라는 파일명이지만 실제 내용이 "강의준비문서"인 경우 있음(하나금융 케이스).

### 타입 판정 알고리즘 (순차)

Google Sheets인 경우 `get_spreadsheet_info`로 탭 이름 리스트 먼저 확인. Excel인 경우 `get_drive_file_content`로 추출 후 텍스트 헤더 분석.

| 타입 | 판정 조건 (탭 이름 키워드) |
|---|---|
| **싱크업 (실운영)** | "미팅로그" + "교육 개요" + ("현황" or "명단") 동시 존재 (Customer I 유형) |
| **강의준비문서** | "강의 캘린더" + "과정 미팅로그" + "강사" 관련 탭 (하나금융 Excel 유형) |
| **기획문서** | "기획" / "초안" / "[A] 기획문서" / "[B]" 명 (Customer H 유형) |
| **견적서** | 탭 이름에 "견적" + 날짜 패턴(YYMMDD) |
| **병합** | 위 2개 이상 겹침 |

### 템플릿 placeholder 감지 (신규)

**배경:** Customer H 기획문서처럼 **템플릿만 복사하고 값 안 채운 파일** 존재. 운영일지에 쓸 수 없음.

감지 시그널:
- 이름 필드에 "홍길동" / "피드백 참여자: 홍길동, 홍길동"
- 값 필드들이 전부 라벨 자체 (예: "제안 차별화 전략 3가지" 같이 안내문 그대로)
- 실 텍스트 비율 < 20% (전체 셀 중 의미있는 값 셀 비율)

**판정 결과 `placeholder_detected: true`이면** parsed 필드는 `{ "placeholder": true, "filled_ratio": 0.1 }` 수준으로만 반환. 파싱 스킵.

### 생애단계(lifecycle_stage) 판정 (신규)

**배경:** 같은 파일이 시간에 따라 "준비 → 진행 → 마무리"로 성격 바뀜. 하나금융(교육 시작 전), Customer I(운영 중), Customer H(재무 대기) 3가지가 전부 다른 단계.

| 단계 | 시그널 |
|---|---|
| `preparation` | 미팅로그 0건, 일정·강사 정보만 채워짐 |
| `in_progress` | 미팅로그 >0건, 현황·교육생 탭 값 채워짐 |
| `wrapping_up` | 교육 종료일이 과거, 회고 탭 활성 |
| `idle` | 미팅로그 0건 + 값 필드 전반 비어있음 (Customer H 유형) |

판정 결과는 아웃풋 `lifecycle_stage` 필드로. 운영일지의 📊 진행 현황에 반영 가능.

## STEP 5: 파일 포맷별 읽기 전략

### Google Sheets 원본 (MIME = `google-apps.spreadsheet`)
- `get_spreadsheet_info` → 탭 이름 목록
- 탭 whitelist 필터링 → 최대 5개 선정
- 탭별 `read_sheet_values` range=`A1:Z40` (40행 상한)

### Excel 파일 (MIME = `openxmlformats-officedocument.spreadsheetml.sheet`)
- `get_drive_file_content` 1회 호출 (자동 파싱, 모든 탭 텍스트 추출) — **이슈 #7 해결 경로**
- 탭 분리는 내부적으로 이미 됨
- 내용 추출 후 탭 whitelist로 필터링

### 폴백: 파일이 너무 큰 경우 (탭 >10 또는 단일 탭 >1000행)
- 요약 LLM 1회 호출 — "이 스프레드시트에서 [운영일지에 쓸 정보 7종] 추출"
- 원칙 4-2 준수 (폴백 전용, 평상시 경로 아님)

## STEP 6: 생애단계별 LLM 파싱

타입 + 생애단계 조합으로 파싱 항목 다름. 모든 항목 억지로 추출하지 말 것 (비어있으면 null).

### 타입별 파싱 스펙

#### 기획시트 (8 항목 — 값 있을 때만)
- `deal_context`, `proposal_deadline`, `quote_amount`, `education_schedule`
- `key_requirements`, `competitor_situation`, `decision_process`, `risk_signals`

#### 견적서 (3~4 항목)
- `quote_amount`, `education_schedule`, `deal_context`

#### 싱크업 (5 항목 + 양방향 확장, 신규 #14 반영)
| 항목 | 설명 |
|---|---|
| `sync_date` | 최근 싱크업 일자 |
| `attendees` | 참석자 |
| `main_topics` | 주요 논의 주제 |
| `action_items_fc_to_customer` | 우리 → 고객 요청사항 (**기존 단일 `action_items`에서 분리**) |
| `action_items_customer_to_fc` | 고객 → 우리 요청사항 |
| `open_issues` | 미해결 이슈·다음 싱크업 안건 |

#### 강의준비문서 (신규 타입)
| 항목 | 설명 |
|---|---|
| `course_overview` | 과정명·교육형태·장소·견적 |
| `session_schedule[]` | **강사별 세부 세션** `[{date, instructor, module, duration}]` ⭐ 📅 일정 핵심 재료 |
| `instructors[]` | 강사 명단 + 모듈 |
| `operations_setup` | 노트북 대여·교재·출석·만족도 플래그 |
| `meeting_logs[]` | 있으면 (비어있으면 `[]`) |

## STEP 7: 아웃풋

### 싱크업 (운영 모드 예시, 생애단계 포함)
```json
{
  "planning_sheet": {
    "file_name": "2602~2605_Customer I_AI 역량 강화 과정_싱크업 문서",
    "link": "...",
    "type": "싱크업",
    "lifecycle_stage": "in_progress",
    "tabs_read": ["교육 개요", "미팅로그", "현황"],
    "parsed": {
      "sync_date": "2026-02-24",
      "attendees": ["류건훈", "Owner Name", "홍제환"],
      "main_topics": ["사전온라인→초급 평가 체계", "멘토링 시간 증대"],
      "action_items_fc_to_customer": ["정확한 접속 허용 AI 툴 리스트"],
      "action_items_customer_to_fc": ["시험문제 출제 + 주관식 검수", "멘토링 1인당 2시간 4회"],
      "open_issues": ["일정 픽스", "AI 툴 사내망 설치 협의"]
    }
  }
}
```

### 강의준비문서 (신규)
```json
{
  "planning_sheet": {
    "file_name": "2604_Customer B_정기특강(5회)_싱크업문서.xlsx",
    "link": "...",
    "type": "강의준비문서",
    "lifecycle_stage": "preparation",
    "parsed": {
      "course_overview": {
        "name": "26년 Customer B AI 정기특강",
        "format": "대면교육",
        "venue": "하나금융그룹 명동사옥 4층",
        "quote": 4900000
      },
      "session_schedule": [
        {"date": "2026-04-23", "instructor": "Instructor A", "module": "금융의 판을 바꾸는 생성형 AI", "duration_hrs": 1.5},
        {"date": "2026-06-18", "instructor": "Instructor A", "module": "복붙 업무 탈출하기", "duration_hrs": 1.5}
      ],
      "meeting_logs": []
    }
  }
}
```

### 템플릿 placeholder 감지됨
```json
{
  "planning_sheet": {
    "file_name": "...",
    "type": "기획문서",
    "placeholder_detected": true,
    "filled_ratio": 0.12,
    "parsed": null,
    "warning": "템플릿만 복사된 상태. 실제 기획 내용 없음."
  }
}
```

### 매칭 실패
```json
{
  "planning_sheet": null,
  "match_warning": "매칭 파일 없음 — 초기 단계이거나 파일명 규칙 미준수"
}
```

## MCP 호출 정리

| 시나리오 | 호출 | 평균 토큰 |
|---|---|---|
| 검색 (1차) | `search_drive_files` 1회 | ~3KB |
| 폴백 검색 | `search_drive_files` 최대 3회 | ~9KB |
| Google Sheets 파일 스캔 | `get_spreadsheet_info` + `read_sheet_values` × N탭 | ~5KB/파일 |
| Excel 파일 스캔 | `get_drive_file_content` 1회 | ~3KB/파일 |
| 매칭 LLM | 후보 2+ 일 때만 | ~1KB |
| **4딜 합계 (평상시)** | | **~20KB** |

## 에러·폴백 처리

| 상황 | 처리 |
|---|---|
| 1차 검색 결과 0건 | 전체 폴백(STEP 2) 시도 |
| 폴백도 0건 | `planning_sheet: null`, 다음 딜 진행 |
| Excel 파싱 실패 | 에러 로그 + `planning_sheet: null` |
| 큰 파일(탭 >10) | 요약 LLM 폴백 (원칙 4-2) |
| 템플릿 감지 | parsed 스킵, `placeholder_detected: true`만 반환 |
| 같은 딜 여러 파일 매칭 | LLM로 최적 1개 선별 |

## LD 운영 가이드 (사용자 가이드 연결)

1. 기획·싱크업·견적 네이밍 일관성 유지
2. 싱크업 탭명 날짜 패턴 (`YYMMDD` or `YY-MM-DD`) 권장
3. **선택: 파일명 끝에 `_{course_id}` 붙이면 매칭 정확도 ↑** (Owner Name 합의 규칙. 다른 LD는 선택)
4. Excel 업로드보다 Google Sheets 네이티브 권장 (읽기 안정성)

## 기존 스펙과의 차이 (v0.3 → v0.4)

| 항목 | v0.3 | v0.4 |
|---|---|---|
| course_id 매칭 우선순위 | 1순위 | **보조 경로 (3순위)** |
| Excel(.xlsx) 지원 | ❌ | ✅ `get_drive_file_content` |
| 파일 타입 판정 | 파일명 기반 | **탭 구조 기반** |
| 템플릿 placeholder 감지 | ❌ | ✅ (filled_ratio 기반) |
| 생애단계 판정 | ❌ | ✅ (preparation/in_progress/wrapping_up/idle) |
| 싱크업 action_items | 단일 구조 | **FC↔고객 양방향 분리** |
| session_schedule | ❌ | ✅ 강의준비문서 신규 타입 |
| 토큰 예산 명시 | ❌ | ✅ 평상시 ~20KB 목표 + 폴백 |
| 폴더 안 파일 검색 | ❌ | ✅ 2-b 경로 |

## 버전

- v0.3 (2026-04-22): course_id 1순위 매칭 초안
- v0.4 (2026-04-23): E2E 실증 기반 전면 재작성. 원칙 4(토큰·시간 이코노미) 반영
- v0.5 (2026-04-24): 1차 검색을 owner 소유 단독 → owner 소유 **OR** owner 이름 포함 **OR** 고객사명·딜명 토큰 포함으로 확장. 2팀 운영 표준이 OM이 싱크업 문서 만드는 경우라 owner 소유 필터만으로는 누락 발생 대응.
