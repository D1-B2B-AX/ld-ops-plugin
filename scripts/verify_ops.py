"""
Step 3.5 — 분류 결과 스키마 검증 (ops-plugin v1.0, 260424 전환)

classified_cells.json(Step 3 출력)이 기대 스키마·무결성 지키는지 검증.

체크 항목:
  1. classified_cells 배열 존재, meta 포함
  2. 각 셀 필수 필드: cell, label, evidence_count
  3. label 값은 ✅/🟡/🔴/⚪/PENDING 중 하나
  4. PENDING 잔여 시 경고 (오케스트레이터가 LLM 처리 놓침 의심)
  5. cell 내부 필드: deal_id, checkpoint_id, severity, category, d_day
  6. evidence_count total == slack + gmail + drive
  7. auto_decided=True 인 셀은 label="🔴" 이어야 함 (자동 판정 규칙)

입력:
  --classified runtime/classified_cells.json
  [--strict]  오류 있으면 exit 1

v1.0 (2026-04-24)
"""

import argparse
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


VALID_LABELS = {"✅", "🟡", "🔴", "⚪", "PENDING"}
VALID_SEVERITIES = {"즉각해결", "확인필요", "관찰"}
VALID_CATEGORIES = {"필수", "조건부"}
REQUIRED_CELL_FIELDS = ("deal_id", "checkpoint_id", "severity", "category", "d_day")


def validate(data):
    errors = []
    warnings = []

    if not isinstance(data, dict):
        errors.append("최상위 구조가 dict 아님")
        return errors, warnings

    cells = data.get("classified_cells")
    if not isinstance(cells, list):
        errors.append("classified_cells 배열 없음 or 타입 오류")
        return errors, warnings

    if "meta" not in data:
        warnings.append("meta 필드 없음 (권장)")

    pending_count = 0
    for idx, c in enumerate(cells):
        if not isinstance(c, dict):
            errors.append(f"#{idx}: 셀이 dict 아님")
            continue

        # 필수 필드
        for field in ("cell", "label", "evidence_count"):
            if field not in c:
                errors.append(f"#{idx}: 필수 필드 누락 — {field}")

        label = c.get("label")
        if label not in VALID_LABELS:
            errors.append(f"#{idx}: 잘못된 label — {label!r} (허용: {VALID_LABELS})")
        if label == "PENDING":
            pending_count += 1

        cell = c.get("cell", {})
        if isinstance(cell, dict):
            for field in REQUIRED_CELL_FIELDS:
                if field not in cell:
                    errors.append(f"#{idx}: cell.{field} 누락")
            sev = cell.get("severity")
            if sev and sev not in VALID_SEVERITIES:
                errors.append(f"#{idx}: cell.severity 오류 — {sev!r}")
            cat = cell.get("category")
            if cat and cat not in VALID_CATEGORIES:
                errors.append(f"#{idx}: cell.category 오류 — {cat!r}")

        # evidence_count 합계 검증
        ec = c.get("evidence_count", {})
        if isinstance(ec, dict) and "total" in ec:
            expected_total = ec.get("slack", 0) + ec.get("gmail", 0) + ec.get("drive", 0)
            if ec["total"] != expected_total:
                errors.append(
                    f"#{idx}: evidence_count.total({ec['total']}) != slack+gmail+drive({expected_total})"
                )

        # 자동 판정 규칙: auto_decided=True면 label은 🔴
        if c.get("auto_decided") and label != "🔴":
            errors.append(
                f"#{idx}: auto_decided=True인데 label={label!r} — 🔴이어야 함"
            )

    if pending_count > 0:
        warnings.append(
            f"PENDING 셀 {pending_count}건 잔여 — 오케스트레이터 LLM 호출 누락 의심. "
            f"generate_ops_md.py가 🔴 fallback 처리함"
        )

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Step 3.5: 분류 결과 스키마 검증 (v1.0)")
    parser.add_argument("--classified", required=True)
    parser.add_argument("--strict", action="store_true", help="오류 있으면 exit 1")
    args = parser.parse_args()

    try:
        with open(args.classified, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] 파일 없음: {args.classified}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON 파싱 실패: {e}", file=sys.stderr)
        sys.exit(1)

    errors, warnings = validate(data)

    if errors:
        print(f"[FAIL] 스키마 검증 실패 ({len(errors)}건)")
        for e in errors:
            print(f"  ❌ {e}")
    if warnings:
        print(f"[WARN] 경고 ({len(warnings)}건)")
        for w in warnings:
            print(f"  ⚠️  {w}")

    if not errors and not warnings:
        total = len(data.get("classified_cells", []))
        print(f"[OK] 모든 검증 통과 (셀 {total}건)")
    elif not errors:
        print(f"[OK] 필수 검증 통과 (경고 {len(warnings)}건)")

    if errors and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
