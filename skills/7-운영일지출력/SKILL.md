# 스킬 7: 운영일지 MD 출력 (v1.0)

> **260424 전환 후 v1.0.** 이전 v0.5(카드형·TOP 3·투두·서술형)는 폐기. v1.0은 `docs/output_format_spec_v1.md`를 단일 진실 원천으로 사용. 본 문서는 스킬 7의 역할만 간결히 기술.

## 역할 (Step 4 + Step 4.6)

1. **Step 4 — 리포트 렌더링 (`generate_ops_md.py`)**
   - 입력: classified_cells.json + sessions.json(deals_no_schedule)
   - 출력: `outputs/ops_report_YYYYMMDD.md` + `.slack.txt`
   - 결정론 (LLM 없음)
   - PENDING 셀 발견 시 🔴 fallback 자동 적용

2. **Step 4.6 — 출력 검증 (`verify_output_format.py`)**
   - MD가 `output_format_spec_v1.md` §11 체크리스트 지켰는지 검사
   - 실패 시 `--strict` 옵션으로 exit 1

## 섹션 구조 (v1.0)

| 섹션 | 조건 | 내용 |
|---|---|---|
| 헤더 | 항상 | `📋 **운영일지** · YYYY-MM-DD (요일)` |
| 🔴 즉각 해결 필요 | 항상 (0건 시 "안정 상태") | label=🔴 전부, severity→D-day 정렬 |
| 📋 확인 필요 / 관찰 | 항상 (0건 시 "해당 없음") | label in [🟡, ⚪], 행 뷰 테이블 |
| ✅ 최근 처리 | **조건부 생략** | label=✅ 있을 때만 |
| ⚠️ 일정 미확정 | **조건부 생략** | deals_no_schedule 있을 때만 |
| 푸터 | 항상 | `v1.0 · 생성 HH:MM` |

## 핵심 원칙 (출력 시)

- **누락 드러내기 우선** — 🔴 전부 상단
- **시각적 구분** — 이모지 화이트리스트 (✅/🟡/🔴/⚪) 외 금지
- **조건부 생략** — 0건 섹션은 자체 제거, "해당 없음" 반복 금지

## v0.5 → v1.0 변경점

| 항목 | v0.5 | v1.0 |
|---|---|---|
| 상단 | 🚨 TOP 3 (LLM 서술형) | 🔴 전부 (결정론 정렬) |
| 중단 | 카드형 (📍·📋 서술형) | 행 뷰 테이블 (5컬럼) |
| 투두 | LLM 자동 생성 | 제거 (🔴/⚪가 대체) |
| 액션 | LLM 자동 생성 | 제거 |
| 변화 감지 | 🔄 어제 대비 신규·종료 | 제거 (셀 상태 변화로 대체) |
| 임박도 이모지 | 🔥 / ⏰ | 제거 (D-X 텍스트) |

## 참고 문서

- `docs/output_format_spec_v1.md` — 단일 진실 원천 (필수 참조)
- `skills/오케스트레이터.md` — 전체 흐름
- `scripts/generate_ops_md.py`·`verify_output_format.py` — 구현
- `scripts/md_to_slack.py` — 슬랙 호환 변환

## 버전

- v1.0 (2026-04-24): 전환 완료. 🔴 상단·행 뷰·조건부 생략 구조
- v0.5 (2026-04-23): 폐기 (archive/260424_pivot/ 참고)
