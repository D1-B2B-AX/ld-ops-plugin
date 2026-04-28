#!/bin/bash
# ops-plugin 자동 실행 스크립트 (v1.0, 260424)
#
# 사용법:
#   ./run_pipeline.sh pre  [--today YYYY-MM-DD]   # Step 0.5~3 (PENDING 생성까지)
#   ./run_pipeline.sh post [--today YYYY-MM-DD]   # Step 4·4.6·슬랙 변환 (PENDING 처리 후)
#
# 전제: runtime/ 에 s1_deals.json, s2_calendar.json, s3_slack.json, s4_gmail.json, s5_drive.json 이
#       오케스트레이터(Claude)의 Step 0 수집 결과로 저장돼 있어야 함.
#
# pre 단계 종료 시 PENDING 남아있으면 안내:
#   - sessions.json의 pending_slack_updates
#   - classified_cells.json의 label=="PENDING"
#   오케스트레이터가 apply_llm_responses.py로 처리 후 post 실행.

set -eo pipefail

STAGE="${1:-}"
shift || true

# 인자 파싱
TODAY_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --today) TODAY_ARG="--today $2"; shift 2;;
    *) echo "알 수 없는 옵션: $1"; exit 1;;
  esac
done

# 경로 설정
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE="$SCRIPT_DIR"
CORE="$BASE"
TODAY_FMT=$(date +%Y%m%d)

check_file() {
  [ -f "$1" ] || { echo "[ERROR] 파일 없음: $1"; exit 1; }
}

get_meta_value() {
  local file="$1"
  local key="$2"
  PYTHONIOENCODING=utf-8 python -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        d = json.load(f)
    print(d.get('meta', {}).get(sys.argv[2], 0))
except Exception:
    print(0)
" "$file" "$key"
}

run_pre() {
  echo "════════════════════════════════════════════"
  echo " PRE 단계 시작 (Step 0.5 → 3)"
  echo "════════════════════════════════════════════"

  # Step 0 산출물 확인
  for f in s1_deals.json s2_calendar.json s3_slack.json s4_gmail.json s5_drive.json; do
    check_file "$BASE/runtime/$f"
  done
  check_file "$CORE/config/checkpoints.json"

  # Step 0.3 — 노션 CSV 파싱 (선택, 2팀 전용)
  if [ -f "$BASE/runtime/notion_calendar.csv" ]; then
    echo "[0.3] parse_notion_csv (2팀 노션)..."
    python "$BASE/scripts/parse_notion_csv.py" \
      --csv      "$BASE/runtime/notion_calendar.csv" \
      --salesmap "$BASE/runtime/s1_deals.json" \
      --settings "$CORE/config/settings.json" \
      --out      "$BASE/runtime/s6_notion.json"
    echo ""
  fi

  echo "[1/4] Step 0.5 compose_schedule..."
  NOTION_OPT=""
  [ -f "$BASE/runtime/s6_notion.json" ] && NOTION_OPT="--notion $BASE/runtime/s6_notion.json"
  OPS_REQ_OPT=""
  [ -f "$BASE/runtime/s3_slack_ops_requests.json" ] && OPS_REQ_OPT="--slack-ops-requests $BASE/runtime/s3_slack_ops_requests.json"
  python "$BASE/scripts/compose_schedule.py" \
    --salesmap "$BASE/runtime/s1_deals.json" \
    --calendar "$BASE/runtime/s2_calendar.json" \
    --drive    "$BASE/runtime/s5_drive.json" \
    --slack    "$BASE/runtime/s3_slack.json" \
    --out      "$BASE/runtime/sessions.json" \
    $NOTION_OPT $OPS_REQ_OPT

  echo ""
  echo "[2/4] Step 1 build_matrix..."
  STATE_OPT=""
  [ -f "$BASE/state/ops_state.json" ] && STATE_OPT="--state $BASE/state/ops_state.json"
  python "$BASE/scripts/build_matrix.py" \
    --sessions    "$BASE/runtime/sessions.json" \
    --checkpoints "$CORE/config/checkpoints.json" \
    --out         "$BASE/runtime/arriving_cells.json" \
    $STATE_OPT $TODAY_ARG

  echo ""
  echo "[3/4] Step 2 collect_evidence..."
  python "$BASE/scripts/collect_evidence.py" \
    --cells       "$BASE/runtime/arriving_cells.json" \
    --checkpoints "$CORE/config/checkpoints.json" \
    --slack       "$BASE/runtime/s3_slack.json" \
    --gmail       "$BASE/runtime/s4_gmail.json" \
    --drive       "$BASE/runtime/s5_drive.json" \
    --settings    "$CORE/config/settings.json" \
    --out         "$BASE/runtime/evidence.json"

  echo ""
  echo "[4/4] Step 3 classify_evidence..."
  python "$BASE/scripts/classify_evidence.py" \
    --evidence    "$BASE/runtime/evidence.json" \
    --checkpoints "$CORE/config/checkpoints.json" \
    --out         "$BASE/runtime/classified_cells.json" \
    $STATE_OPT

  echo ""
  echo "════════════════════════════════════════════"
  echo " PRE 단계 완료"
  echo "════════════════════════════════════════════"

  # PENDING 현황 안내
  SLACK_PEND=$(get_meta_value "$BASE/runtime/sessions.json" "pending_slack_count")
  LABEL_PEND=$(get_meta_value "$BASE/runtime/classified_cells.json" "pending_llm")

  if [ "$SLACK_PEND" != "0" ] || [ "$LABEL_PEND" != "0" ]; then
    echo ""
    echo "⚠️  PENDING 처리 필요:"
    [ "$SLACK_PEND" != "0" ] && echo "   - 슬랙 일정 변경 후보: $SLACK_PEND 딜"
    [ "$LABEL_PEND" != "0" ] && echo "   - LLM 분류 대기 셀: $LABEL_PEND 건"
    echo ""
    echo "오케스트레이터(Claude)가 각 PENDING에 대해 LLM 호출 후"
    echo "scripts/apply_llm_responses.py --mode {slack|labels} 로 병합 → 그 다음 post 실행."
  else
    echo ""
    echo "✅ PENDING 없음 — 바로 post 단계 실행 가능"
  fi
}

run_post() {
  echo "════════════════════════════════════════════"
  echo " POST 단계 시작 (Step 4 → 4.6 → 슬랙 변환)"
  echo "════════════════════════════════════════════"

  check_file "$BASE/runtime/classified_cells.json"
  check_file "$BASE/runtime/sessions.json"

  # PENDING 잔존 체크 (경고만, 차단 아님 - generate_ops_md가 🔴 fallback)
  LABEL_PEND=$(get_meta_value "$BASE/runtime/classified_cells.json" "pending_llm")
  if [ "$LABEL_PEND" != "0" ]; then
    echo "⚠️  PENDING 셀 $LABEL_PEND 건 잔존 — 🔴 fallback 처리됩니다"
  fi

  OUT_MD="$BASE/outputs/ops_report_$TODAY_FMT.md"
  OUT_SLACK="$BASE/outputs/ops_report_$TODAY_FMT.slack.txt"

  echo "[1/3] Step 4 generate_ops_md..."
  python "$BASE/scripts/generate_ops_md.py" \
    --classified  "$BASE/runtime/classified_cells.json" \
    --checkpoints "$CORE/config/checkpoints.json" \
    --sessions    "$BASE/runtime/sessions.json" \
    --evidence    "$BASE/runtime/evidence.json" \
    --out         "$OUT_MD" \
    $TODAY_ARG

  echo ""
  echo "[2/3] Step 4.6 verify_output_format..."
  python "$BASE/scripts/verify_output_format.py" --md "$OUT_MD" --strict

  echo ""
  echo "[3/3] md_to_slack 변환..."
  python "$BASE/scripts/md_to_slack.py" "$OUT_MD" -o "$OUT_SLACK"

  echo ""
  echo "════════════════════════════════════════════"
  echo " POST 단계 완료"
  echo "════════════════════════════════════════════"
  echo "📄 MD:      $OUT_MD"
  echo "💬 Slack:   $OUT_SLACK"
}

case "$STAGE" in
  pre)  run_pre ;;
  post) run_post ;;
  "")
    echo "사용법: $0 {pre|post} [--today YYYY-MM-DD]"
    exit 1 ;;
  *)
    echo "알 수 없는 단계: $STAGE (pre|post 만 지원)"
    exit 1 ;;
esac
