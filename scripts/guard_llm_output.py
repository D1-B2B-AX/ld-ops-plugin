"""
LLM 호출 전후 결정론 항목 변경 감지·복원 (원칙 2 불변성 가드)

설계 가정:
  - 결정론 3항목(일정·진행_현황·태그)은 classify_items.py가 결정
  - LLM은 액션·투두 2항목만 생성 (llm_prompts.md 스펙)
  - 본 가드는 LLM 병합 후에 혹시 결정론 항목이 바뀌었는지 검사·복원

입력:
  --before classify_items.py 출력 (결정론 3항목 채워짐, 액션·투두는 비어있음)
  --after  LLM 호출 후 병합본 (전 항목 채워짐)

출력:
  - 결정론 3항목은 before 기준 강제 복원
  - 액션·투두는 after 기준 유지
  - 위반(violation) 리스트 기록

사용법:
  python scripts/guard_llm_output.py --before pre.json --after post.json -o guarded.json

v0.1 (2026-04-22)
"""

import argparse
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

DETERMINISTIC_KEYS = ("일정", "진행_현황", "태그")


def deal_map(data):
    deals = data.get("classified", []) if isinstance(data, dict) else data
    return {
        d.get("deal_id"): d
        for d in deals
        if isinstance(d, dict) and d.get("deal_id")
    }


def guard(before_data, after_data):
    before = deal_map(before_data)
    after = deal_map(after_data)

    violations = []
    guarded_deals = []

    for did, before_deal in before.items():
        after_deal = after.get(did)

        if not after_deal:
            violations.append({
                "deal_id": did,
                "type": "missing_in_after",
                "message": "after에 해당 교육 없음 — before 기준으로 유지"
            })
            guarded_deals.append(before_deal)
            continue

        guarded = dict(after_deal)
        guarded_items = dict(after_deal.get("items", {}))

        before_items = before_deal.get("items", {})
        for key in DETERMINISTIC_KEYS:
            before_val = before_items.get(key)
            after_val = guarded_items.get(key)
            if before_val != after_val:
                violations.append({
                    "deal_id": did,
                    "type": "deterministic_changed",
                    "key": key,
                    "before": before_val,
                    "after": after_val,
                })
                guarded_items[key] = before_val  # 강제 복원

        guarded["items"] = guarded_items
        guarded_deals.append(guarded)

    # after에 추가된 교육 (LLM이 만들어낸 것) — 제외
    extras = set(after.keys()) - set(before.keys())
    for eid in extras:
        violations.append({
            "deal_id": eid,
            "type": "extra_in_after",
            "message": "LLM이 만들어낸 것으로 추정 — 최종 결과에서 제외"
        })

    return guarded_deals, violations


def main():
    parser = argparse.ArgumentParser(
        description="LLM 호출 전후 결정론 항목 변경 감지·복원"
    )
    parser.add_argument("--before", required=True, help="classify_items.py 출력")
    parser.add_argument("--after", required=True, help="LLM 병합 후 출력")
    parser.add_argument("-o", "--output", default=None, help="guarded 출력 (기본 stdout)")
    args = parser.parse_args()

    with open(args.before, "r", encoding="utf-8") as f:
        before_data = json.load(f)
    with open(args.after, "r", encoding="utf-8") as f:
        after_data = json.load(f)

    guarded, violations = guard(before_data, after_data)

    v_types = {}
    for v in violations:
        v_types[v["type"]] = v_types.get(v["type"], 0) + 1

    verdict = "CLEAN" if not violations else "RESTORED"
    print(
        f"[{verdict}] 결정론 가드: 교육 {len(guarded)}건, 위반 {len(violations)}건",
        file=sys.stderr
    )
    for t, c in v_types.items():
        print(f"  - {t}: {c}건", file=sys.stderr)

    for v in violations[:10]:
        if v["type"] == "deterministic_changed":
            print(
                f"  ⚠ [{v['deal_id']}] {v['key']} 복원 "
                f"(LLM 시도: {v['after']} → 복원값: {v['before']})",
                file=sys.stderr
            )
        else:
            print(f"  ⚠ [{v['deal_id']}] {v['type']}: {v.get('message','')}",
                  file=sys.stderr)

    output = {"classified": guarded, "violations": violations}
    out_json = json.dumps(output, ensure_ascii=False, indent=2, default=str)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out_json)
        print(f"→ {args.output}", file=sys.stderr)
    else:
        print(out_json)


if __name__ == "__main__":
    main()
