"""
오케스트레이터 LLM 응답 병합 헬퍼 (ops-plugin v1.0, 260424)

두 종류의 PENDING 처리를 자동 병합:
  1) Step 0.5 compose_schedule의 pending_slack_updates (일정 변경 공지 날짜 추출)
  2) Step 3 classify_evidence의 PENDING 셀 (✅/🟡/🔴/⚪ 라벨)

사용법:
  # 슬랙 일정 변경 병합
  python scripts/apply_llm_responses.py --mode slack \
    --target runtime/sessions.json --responses slack_responses.json

  # 셀 라벨 병합
  python scripts/apply_llm_responses.py --mode labels \
    --target runtime/classified_cells.json --responses label_responses.json

입력 JSON 포맷:

[slack 모드]
  {
    "deal_001": {"sessions": [{"edu_start": "...", "edu_end": "...", "note": "..."}],
                 "no_schedule_info": false},
    "deal_002": {"sessions": [], "no_schedule_info": true}
  }
  → sessions.json의 해당 deal.sessions 덮어쓰기 + pending_slack_updates[i].status = "DONE"

[labels 모드]
  {
    "0": {"label": "🔴", "confidence": 0.9, "reason": "증거 전무"},
    "deal_001:curriculum_confirm:1": {"label": "🟡", "confidence": 0.8}
  }
  key = classified_cells 인덱스(int 문자열) 또는 "deal_id:checkpoint_id[:session_no]"
  → classified_cells.json의 해당 셀 label/confidence/auto_decided 업데이트

v1.0 (2026-04-24)
"""

import argparse
import json
import os
import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

VALID_LABELS = {"✅", "🟡", "🔴", "⚪"}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def apply_slack(target_path, responses):
    """슬랙 일정 응답을 sessions.json에 병합."""
    data = load_json(target_path)
    deals_with = data.setdefault("deals_with_schedule", {})
    pending = data.setdefault("pending_slack_updates", [])

    applied = 0
    skipped = 0

    for deal_id, resp in responses.items():
        if not isinstance(resp, dict):
            skipped += 1
            continue

        # status 업데이트
        pending_item = None
        for p in pending:
            if p.get("deal_id") == deal_id:
                pending_item = p
                p["status"] = "DONE"
                p["applied_at"] = datetime.now().isoformat(timespec="seconds")

        if resp.get("no_schedule_info"):
            # 일정 정보 없음 - sessions 변경 없음
            if pending_item:
                pending_item["result"] = "no_schedule_info"
            applied += 1
            continue

        new_sessions = resp.get("sessions") or []
        if not new_sessions:
            skipped += 1
            continue

        if deal_id not in deals_with:
            # 미확정 트랙에 있던 딜이면 복원 (선택)
            skipped += 1
            continue

        # 세션 덮어쓰기 (slack이 최신 권위)
        normalized = []
        for idx, s in enumerate(
            sorted(new_sessions, key=lambda x: x.get("edu_start", ""))
        ):
            normalized.append(
                {
                    "session_no": s.get("session_no", idx + 1),
                    "edu_start": s.get("edu_start"),
                    "edu_end": s.get("edu_end") or s.get("edu_start"),
                    "source": sorted(set((s.get("source") or []) + ["slack"])),
                    "slack_note": s.get("note"),
                }
            )
        deals_with[deal_id]["sessions"] = normalized
        deals_with[deal_id]["slack_override_applied"] = True
        applied += 1

    # meta 업데이트
    meta = data.setdefault("meta", {})
    meta["slack_applied_at"] = datetime.now().isoformat(timespec="seconds")
    meta["pending_slack_count"] = sum(1 for p in pending if p.get("status") == "PENDING")

    save_json(target_path, data)
    return applied, skipped, meta["pending_slack_count"]


def _find_cell_index(cells, key):
    """key(인덱스 문자열 or 'deal_id:cp_id[:session_no]')로 셀 인덱스 찾기."""
    if isinstance(key, int):
        return key if 0 <= key < len(cells) else None
    if isinstance(key, str) and key.isdigit():
        idx = int(key)
        return idx if 0 <= idx < len(cells) else None
    if isinstance(key, str) and ":" in key:
        parts = key.split(":")
        deal_id = parts[0]
        cp_id = parts[1]
        session_no = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        for i, c in enumerate(cells):
            cc = c.get("cell", {})
            if cc.get("deal_id") == deal_id and cc.get("checkpoint_id") == cp_id:
                if session_no is None or cc.get("session_no") == session_no:
                    return i
    return None


def apply_labels(target_path, responses):
    """LLM 라벨 응답을 classified_cells.json에 병합."""
    data = load_json(target_path)
    cells = data.setdefault("classified_cells", [])

    applied = 0
    skipped = 0
    errors = []

    for key, resp in responses.items():
        if not isinstance(resp, dict):
            skipped += 1
            continue
        idx = _find_cell_index(cells, key)
        if idx is None:
            errors.append(f"셀 찾기 실패: {key}")
            skipped += 1
            continue
        label = resp.get("label")
        if label not in VALID_LABELS:
            errors.append(f"{key}: 잘못된 label={label!r}")
            skipped += 1
            continue

        cells[idx]["label"] = label
        cells[idx]["confidence"] = resp.get("confidence")
        cells[idx]["auto_decided"] = False
        if resp.get("reason"):
            cells[idx]["llm_reason"] = resp["reason"]
        applied += 1

    # meta 업데이트
    remaining = sum(1 for c in cells if c.get("label") == "PENDING")
    meta = data.setdefault("meta", {})
    meta["pending_llm"] = remaining
    meta["llm_applied_at"] = datetime.now().isoformat(timespec="seconds")

    save_json(target_path, data)
    return applied, skipped, remaining, errors


def main():
    parser = argparse.ArgumentParser(description="LLM 응답 병합 헬퍼 (v1.0)")
    parser.add_argument("--mode", choices=["slack", "labels"], required=True)
    parser.add_argument("--target", required=True, help="병합 대상 JSON 경로")
    parser.add_argument("--responses", required=True, help="LLM 응답 JSON 경로")
    args = parser.parse_args()

    if not os.path.exists(args.target):
        print(f"[ERROR] target 없음: {args.target}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.responses):
        print(f"[ERROR] responses 없음: {args.responses}", file=sys.stderr)
        sys.exit(1)

    responses = load_json(args.responses)
    if not isinstance(responses, dict):
        print("[ERROR] responses JSON은 객체여야 함 {key: {...}}", file=sys.stderr)
        sys.exit(1)

    if args.mode == "slack":
        applied, skipped, remaining = apply_slack(args.target, responses)
        print(f"[OK] 슬랙 응답 병합: 적용 {applied} / 스킵 {skipped}")
        print(f"     남은 PENDING 슬랙: {remaining}건 → {args.target}")
    else:
        applied, skipped, remaining, errors = apply_labels(args.target, responses)
        print(f"[OK] 라벨 응답 병합: 적용 {applied} / 스킵 {skipped}")
        print(f"     남은 PENDING 셀: {remaining}건 → {args.target}")
        if errors:
            print("경고:")
            for e in errors[:5]:
                print(f"  - {e}")


if __name__ == "__main__":
    main()
