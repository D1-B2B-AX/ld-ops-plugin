# 스킬 6: 셀 매트릭스 분류 (운영 전용, v1.0)

> **260424 전환 후 v1.0.** 이전 v0.4는 "교육 단위 5항목 분류"였으나 v1.0에선 **"교육-세션 × 체크포인트 9개 셀 매트릭스"** 개념으로 전면 전환. 전체 구조·흐름은 `skills/오케스트레이터.md` 단일 기준. 본 문서는 운영 플러그인의 분류 단계 역할만 간결히 기술.

## 역할 (Step 1 + Step 3)

1. **Step 1 — 셀 매트릭스 구축 (`build_matrix.py`)**
   - 입력: sessions.json (Step 0.5 출력) + checkpoints.json
   - 출력: arriving_cells.json (오늘 감시할 셀만 추출)
   - 방식: 결정론 (LLM 없음). `anchor + window`로 도래 판정, `deal_flags`로 조건부 스킵

2. **Step 3 — 증거 4분류 (`classify_evidence.py`)**
   - 입력: evidence.json (Step 2 수집) + checkpoints.completion_hint
   - 출력: classified_cells.json
   - 규칙:
     - 증거 0건 → 자동 🔴 (auto_decided=True)
     - 증거 있음 → PENDING + LLM 프롬프트 준비
   - 실제 LLM 호출은 **오케스트레이터가 수행** 후 label 업데이트

## 4분류 라벨

| 라벨 | 의미 | 리포트 배치 |
|---|---|---|
| ✅ | 완료 증거 명확 | 하단 "최근 처리" (조건부) |
| 🟡 | 진행 중 흔적 (완료 확인 불가) | 중단 "확인 필요 / 관찰" |
| 🔴 | 증거 전무 | **상단 "즉각 해결"** (원칙 0 핵심) |
| ⚪ | 흔적 있으나 완료 여부 모호 | 중단 "확인 필요 / 관찰" |

## v0.4 vs v1.0 변경점

| 항목 | v0.4 | v1.0 |
|---|---|---|
| 분류 단위 | 교육 1건당 5항목 (일정·액션·진행·투두·태그) | **교육-세션 × 체크포인트 9개 격자** |
| LLM 역할 | 액션·투두 자유 생성 | **증거 4분류 라벨만** (자유 생성 금지) |
| 출력 파일 | classified_ops.json | arriving_cells.json + classified_cells.json |
| 관련 스크립트 | classify_items.py | build_matrix.py + classify_evidence.py |
| 검증 | verify_ops.py (5항목 스키마) | verify_ops.py v1.0 (셀 매트릭스 스키마) |

## 참고 문서

- `skills/오케스트레이터.md` — 전체 흐름 (v1.0 단일 기준)
- `config/checkpoints.json` — 9개 체크포인트 정의
- `~/.claude/projects/C--Users-GA/memory/project_ops_plugin_pivot_260424.md` §4.2, §6.7

## 버전

- v1.0 (2026-04-24): 셀 매트릭스 전환. v0.4 전체 재설계
- v0.4 (2026-04-23): 폐기 (archive/260424_pivot/ 참고)
