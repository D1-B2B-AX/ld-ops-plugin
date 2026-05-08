# 교육 운영일지 플러그인 (ld-ops-plugin)

LD가 **교육 운영 체크포인트의 빈칸(증거 없음)을 기한 전에 찾아내는** Claude Code 플러그인.
슬래시 커맨드: `/운영일지`

---

## 페인 & 솔루션

> **페인:** 파편화된 교육 운영 데이터를 매번 직접 추적하다 놓칠까봐 불안한 것
>
> **솔루션:** 준비 완료돼 있어야 할 시점에 아직 증거가 없는 것을 기한 전에 알려주는 도구

**사고방식:** 해석·집약·강조 ❌ → **누락(빈칸) 감지** ✅

---

## 9개 체크포인트

교육-세션 × 체크포인트 격자를 만들고 오늘 감시 구간에 들어온 셀만 처리.

**필수 7종:**
1. 기업계약 체결
2. 강사계약 체결
3. 싱크업 미팅 (LD→OM 인계·다차수)
4. 고객사 교육 환경 확인 (강의장·장비·보안)
5. 강사 교안·교육 환경 요청/검수
6. 교안 컨펌 + 고객사 전달
7. 거래명세서·세금계산서 발행

**조건부 2종 (딜별 플래그):**
8. 교육생 입과·강의 안내 메일 (`has_customer_announcement`)
9. 만족도 리포팅 (`has_satisfaction_report`)

**셀 라벨 (5분류):**
- 📅 시점 미도래 / ✅ 완료 추정 / 🟡 진행 중 / 🔴 미확보 / ⚪ 모호

---

## 설치

```bash
git clone https://github.com/D1-B2B-AX/ld-ops-plugin.git ~/.claude/commands/ld-ops-plugin
```

설치 후 `config/settings.example.json`을 복사해서 본인 정보로 수정:

```bash
cp ~/.claude/commands/ld-ops-plugin/config/settings.example.json \
   ~/.claude/commands/ld-ops-plugin/config/settings.json
```

`config/settings.json`을 열어 다음을 본인 값으로 변경:
- `owner.name` — 본인 이름
- `owner.email` — 본인 이메일
- `owner.team` — `교육 1팀` / `교육 2팀` / `교육 1팀 운영 파트` / `교육 2팀 운영 파트`
- `owner.calendars[].id` — 본인 구글 캘린더 ID

---

💡 **[Tip] 또는 아래 통합 메시지 초안 (LD가 클로드 코드에 통째로 복붙)을 복사하시어 활용**해도 됩니다!

운영일지 플러그인 환경 설정해줘. 아래 순서대로 진행하고, 각 단계 끝날 때마다 한 줄 결과만 보고해.

1. 슬랙 MCP 인증 확인 — /mcp 입력해서 슬랙 연결 상태 확인. 미연결이면 "내 슬랙 연결해줘" 진행.
2. workspace-mcp 인증 확인 — Gmail·Calendar·Drive 통합 인증. 미연결이면 "내 구글 드라이브 연결해줘" 진행.
3. gh CLI 설치 확인 — PowerShell에서 gh --version 실행. 미설치면 다음 명령어 안내:
winget install --id GitHub.cli
4. 세일즈맵 DB 다운로드 — PowerShell에서 다음 명령어 실행 안내:
mkdir -p $HOME\salesmap
gh release download salesmap-db-latest --repo sabinanfranz/data_analysis_ai --pattern "salesmap_latest.db"
--dir $HOME\salesmap --clobber
5. 플러그인 설치 — PowerShell에서:
git clone https://github.com/D1-B2B-AX/ld-ops-plugin $HOME\.claude\commands\ld-ops-plugin
6. settings 파일 만들기 — PowerShell에서:
cp $HOME\.claude\commands\ld-ops-plugin\config\settings.example.json
$HOME\.claude\commands\ld-ops-plugin\config\settings.json
7. settings.json 본인 정보 채우기 (자연어로 직접 박기):
    - owner: 이름·회사 이메일·팀(교육1팀/2팀/운영파트)·캘린더 ID
    - 슬랙 user ID 자동 검색해서 박기 (없으면 null로 두면 @멘션 폴백)

---

## 필요한 MCP

| MCP | 종류 | 용도 | 필수도 |
|---|---|---|---|
| **salesmap** (SQLite, User) | 세일즈맵 DB 조회 | 딜·메모·고객사 | 필수 |
| **workspace-mcp** (Local Python) | Google Calendar / Gmail / Drive | 일정·메일·기획시트 | 필수 |
| **claude.ai Slack** | 슬랙 채널 검색 | 운영 메시지 | 필수 |
| claude.ai Gmail | 메일 보조 | 일부 라벨 | 선택 |
| claude.ai Notion | 노션 강의 캘린더 | 다차수 회차 | 선택 (v1.0 비활성) |

`/운영일지` 첫 실행 시 자동으로 인증 상태를 점검하고, 미인증 항목은 안내합니다.

---

## 사용

Claude Code에서 슬래시 커맨드만 입력하면 끝:

```
/운영일지
```

플러그인이 자동으로:
1. **세일즈맵 DB 최신 여부 확인** (오래되면 자동 다운로드 — gh CLI·별도 명령어 불필요)
2. 5소스 데이터 병렬 수집 (스킬 1~5)
3. 일정 조립 → 셀 매트릭스 → 증거 수집 → LLM 분류
4. 운영일지 MD 생성 + 슬랙 호환본 변환

### 첫 실행 시 안내
- 세일즈맵 DB(약 420MB)가 자동 다운로드됩니다 (1~2분 소요)
- 이후 실행은 당일 갱신본만 받아옴 (몇 초)
- 인증·환경 문제 발견 시 한국어 안내 메시지 출력 → 그대로 조치 후 재실행

### 자동 슬랙 알림
별도 가이드 파일 없음 — Claude Code에 자연어로 요청하면 됩니다:
> "오전 9시에 슬랙으로 운영일지 받게 해줘"

---

## 폴더 구조

```
ld-ops-plugin/
├── 운영일지.md              # 슬래시 커맨드 정의 (Step 0~2)
├── config/
│   ├── settings.example.json  # 가명 예시 (이걸 복사해서 settings.json 만들기)
│   ├── settings.json          # 실제 본인 정보 (.gitignore — 절대 push 금지)
│   └── checkpoints.json       # 9개 체크포인트 정의
├── data-skills/             # 데이터 수집 스킬 1~5
│   ├── 1-salesmap-조회/
│   ├── 2-캘린더-조회/
│   ├── 3-슬랙-조회/
│   ├── 4-지메일-조회/
│   └── 5-드라이브-기획시트-조회/
├── skills/
│   ├── 오케스트레이터.md     # 메인 흐름 (단일 기준 문서)
│   ├── 6-항목분류/
│   └── 7-운영일지출력/
├── scripts/                 # 11종 Python 스크립트
│   ├── check_env.py             # Phase 0: 환경 점검 + DB 자동 다운로드
│   ├── compose_schedule.py      # Step 0.5: 일정 조립
│   ├── build_matrix.py          # Step 1: 셀 매트릭스
│   ├── collect_evidence.py      # Step 2: 증거 수집
│   ├── classify_evidence.py     # Step 3: LLM 분류 입력
│   ├── apply_llm_responses.py   # Step 0.5/3 후속: LLM 응답 병합
│   ├── apply_feedback.py        # Step 5: 자연어 피드백 → state 누적
│   ├── generate_ops_md.py       # Step 4: 리포트 생성
│   ├── verify_output_format.py  # Step 4.6: 검증
│   ├── md_to_slack.py           # 슬랙 호환 변환
│   └── parse_notion_csv.py      # 노션 CSV (비활성)
├── outputs/                 # 일자별 리포트 (.gitignore — 실제 데이터 차단)
├── runtime/                 # 중간 산출물 (매 실행 재생성, .gitignore)
├── state/                   # 자연어 피드백 누적 상태 (.gitignore)
├── archive/                 # 일별 스냅샷 (.gitignore)
├── run_pipeline.sh / .ps1   # bash·PowerShell 자동 실행 스크립트
└── README.md
```

---

## 자연어 피드백 (Step 5)

리포트 받은 후 LD가 자연어로 정정 가능:

> "Customer E 거래명세서 4/8에 끝났어"

→ Plugin이 파싱 → `state/ops_state.json`에 영구 저장 → 다음 실행부터 ✅로 자동 반영.

영구성 보장 — raw 데이터 재수집 후에도 LD 입력은 살아남음.

---

## 출력 예시

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 **2026-04-28(화) 수주 과정 운영 현황**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 🚨 즉각 해결 필요
_(해당 없음)_

## ⚠️ 확인 필요
- **Customer E** 2회차 교육 종료 후 **D+20** — 만족도 리포팅 미처리

## 📋 진행 현황

### 1. Customer D
**_(딜 전체 — 회차 무관)_**
  🔴 미확보  · 거래명세서·세금계산서 발행: 자동 검출 불가

**4~6회차** D-9 ~ D-0
  ✅ 완료 추정 · 교안 컨펌 + 고객사 전달
  📅 예정     · 만족도 리포팅
...
```

---

## 트러블슈팅

| 증상 | 조치 |
|---|---|
| `salesmap_latest.db` 다운로드 실패 | 네트워크 + GitHub 접근 확인. `manual_url`로 직접 다운로드 후 `~/salesmap/`에 저장 |
| `workspace-mcp credentials 폴더 없음` | workspace-mcp 설치 + 구글 OAuth 인증 |
| `slack 검색 결과 0건` | settings의 `slack_user_id`를 본인 ID로 설정 (없으면 멘션 폴백) |
| 보고서에 본인 딜 누락 | settings의 `owner.name`이 세일즈맵 담당자 컬럼과 정확히 일치하는지 확인 |
| 회차 분해가 안 됨 | 슬랙 운영 요청 채널의 thread를 `runtime/s3_slack_ops_requests.json`으로 수동 수집 (v1.0 한계, v1.1에서 자동화 예정) |

---

## 버전

**v1.1.4 (2026-04-28~)** — STEP 7 운영 요청 채널 thread 자동 수집·검색 윈도우·과정명 토큰·정형 thread 우선순위·출력 스키마 강제 (LLM 자유 생성 차단)·다차수 분해 차단 이슈 해결·회사명 어순 변형 검색.

**v1.0 (2026-04-28)** — 체크포인트 매트릭스 구조 + 자연어 피드백 영구성 + 자동 다운로드 통합 완료.

---

## 추후 보완 영역 (v2)

본 plugin v1은 *체크포인트 매트릭스 + 자연어 피드백* 뼈대를 박은 영역입니다. 다음 영역은 *v2 보강 영역*으로 진입 예정:

- **다차수 분해 자동화** — 차수 단위 모델 (트랙·박일짜리·묶음 단위)·셀 색상 인식·real-time 변동 추적
- **LD/딜별 customization 영역** — LD마다 운영 idiom 차이 흡수·딜별 차수 정의 유동성 반영
- **데이터 처리 layer 보강** — `compose_schedule`·`build_matrix`·`generate_ops_md` 영역 차수 단위 모델로 진화

상세 설계 회고: [📂 운영일지 플러그인 (260423~260428)](https://www.notion.so/260423-260428-35a11db511ea812e8673e56e613ae800)

> *"v1 1개월 작업의 가장 큰 산출 = 시스템이 자동 수집 못 하는 영역 = LD/딜이 명시적으로 입력해야 하는 영역의 경계 정의."*
