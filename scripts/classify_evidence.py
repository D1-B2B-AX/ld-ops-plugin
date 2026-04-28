"""
Step 3 — LLM 분류 입력 준비 (ops-plugin v1.1, 260427 분류 프레임 재설계)

5분류 라벨 부여 + LLM 4분류 입력 준비.

라벨 5종 (v1.1):
  📅 예정      — status="future" 셀 자동 (시점 미도래)
  ✅ 완료 추정  — 증거 0건 + 같은 (deal, session)의 후속 phase 셀에 증거 있음 → 자동
  🟡 진행 중   — 증거 있음, LLM 판정 (PENDING)
  🔴 실제 미확보 — 증거 0건 + 후속 phase에도 증거 없음 → 자동 / 또는 LLM 판정
  ⚪ 모호      — 증거 있음, 완료/진행 판단 불가, LLM 판정 (PENDING)

자동 부여 (LLM 호출 없음):
  - 📅 (future 셀)
  - ✅ (증거 0 + 후속 단계 증거 있음)
  - 🔴 (증거 0 + 후속 단계 증거도 없음)

LLM 판정 (PENDING):
  - 증거 있는 셀 → ✅ / 🟡 / 🔴 / ⚪ 중 하나

알림 위계(alert_tier) 부여는 generate_ops_md.py(묶음 3)에서 라벨·severity·status 기반으로
동적 결정 (LLM 응답 후 재계산 부담 회피).

실제 LLM 호출은 오케스트레이터(Claude)가:
  1) classified_cells.json 읽기
  2) label=="PENDING" 셀의 llm_prompt를 Claude에게 전달
  3) 응답 JSON({"label": "이모지", "confidence": 숫자}) 받기
  4) 해당 셀의 label/confidence 필드 업데이트
  5) PENDING이 0이 되면 generate_ops_md.py에 입력

입력:
  --evidence    runtime/evidence.json
  --checkpoints config/checkpoints.json (v0.2: phases 정의)
  --out         runtime/classified_cells.json

출력 각 셀 구조:
  {
    "cell": {...},
    "evidence_summary": "짧은 요약 문자열",
    "evidence_count": {...},
    "llm_prompt": "..." | null,
    "label": "📅" | "✅" | "🟡" | "🔴" | "⚪" | "PENDING",
    "confidence": 1.0 | null,
    "auto_decided": true | false,
    "auto_reason": "future" | "successor_evidence" | "no_evidence_no_successor" | null
  }

v1.0 (2026-04-24)
v1.1 (2026-04-27): 5분류 확장 + 후속 phase 증거 lookup 기반 자동 ✅ 부여.
v1.2 (2026-04-27): granularity 적용 - deal 단위 셀은 deal 전체 lookup, session 단위는 (deal, session) lookup.
v1.3 (2026-04-27): state.cell_overrides 적용 — 자연어 피드백으로 라벨 강제 변경 반영. session_no=null 매칭 시 같은 deal·cp 모든 회차 적용.
"""

import argparse
import json
import os
import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_evidence_indexes(evidence_data):
    """
    v1.2: 두 개 인덱스 동시 빌드.
      - by_deal: deal_id → {phase_order: has_evidence} (deal 단위 lookup)
      - by_session: (deal_id, session_no) → {phase_order: has_evidence} (session 단위 lookup)
    """
    by_deal = {}
    by_session = {}
    for entry in evidence_data.get("evidence_per_cell", []):
        cell = entry.get("cell", {})
        deal_id = cell.get("deal_id")
        phase_order = cell.get("phase_order")
        session_no = cell.get("session_no")
        if deal_id is None or phase_order is None:
            continue
        total = (entry.get("evidence_count", {}) or {}).get("total", 0)
        # deal 단위
        if deal_id not in by_deal:
            by_deal[deal_id] = {}
        by_deal[deal_id][phase_order] = by_deal[deal_id].get(phase_order, False) or (total > 0)
        # session 단위 (session_no 있는 경우만)
        if session_no is not None:
            key = (deal_id, session_no)
            if key not in by_session:
                by_session[key] = {}
            by_session[key][phase_order] = by_session[key].get(phase_order, False) or (total > 0)
    return by_deal, by_session


def has_successor_evidence(cell, by_deal, by_session):
    """v1.2: 셀의 granularity 기반 후속 단계 evidence lookup.
    - deal 단위 셀: 같은 deal의 모든 후속 phase 어디든 증거 있으면 True
    - session 단위 셀: 같은 (deal, session)의 후속 phase에만 한정
    """
    deal_id = cell.get("deal_id")
    phase_order = cell.get("phase_order")
    if deal_id is None or phase_order is None:
        return False
    granularity = cell.get("granularity", "session")
    if granularity == "deal":
        phases = by_deal.get(deal_id, {})
    else:
        session_no = cell.get("session_no")
        if session_no is None:
            return False
        phases = by_session.get((deal_id, session_no), {})
    return any(ph > phase_order and ev for ph, ev in phases.items())


def build_llm_prompt(cell, checkpoint, evidence):
    """셀 + 증거 → LLM 판정용 프롬프트 문자열 생성. LLM은 4분류만 판정 (📅는 코드 자동)."""
    label = checkpoint.get("label", "")
    completion_hint = checkpoint.get("completion_hint", "")
    customer = cell.get("customer", "")
    deal_name = cell.get("deal_name", "")
    d_day = cell.get("d_day", "")
    phase_label = cell.get("phase_label", "")

    slack_list = evidence.get("slack", []) or []
    gmail_list = evidence.get("gmail", []) or []
    drive_list = evidence.get("drive", []) or []

    sections = []
    if slack_list:
        lines = [f"[슬랙 {len(slack_list)}건]"]
        for s in slack_list[:5]:
            date = s.get("date", "") or ""
            snippet = (s.get("snippet", "") or "")[:200]
            lines.append(f"- ({date}) {snippet}")
        sections.append("\n".join(lines))

    if gmail_list:
        lines = [f"[메일 {len(gmail_list)}건]"]
        for g in gmail_list[:5]:
            date = g.get("date", "") or ""
            subj = g.get("subject", "") or ""
            snippet = (g.get("snippet", "") or "")[:150]
            lines.append(f"- ({date}) {subj} | {snippet}")
        sections.append("\n".join(lines))

    if drive_list:
        lines = [f"[드라이브 {len(drive_list)}건]"]
        for d in drive_list[:5]:
            fname = d.get("file_name", "") or ""
            tab = d.get("tab_name", "") or ""
            dtype = d.get("type", "") or ""
            snippet = (d.get("snippet", "") or "")[:150]
            marker = tab if tab else dtype
            lines.append(f"- {fname} / {marker} / {snippet}")
        sections.append("\n".join(lines))

    evidence_block = "\n\n".join(sections) if sections else "(증거 없음)"

    prompt = f"""체크포인트: {label} (단계: {phase_label})
완료 정의: {completion_hint}

교육 메타:
- 고객사: {customer}
- 과정명: {deal_name}
- D-day: {d_day}

수집 증거:
{evidence_block}

위 증거가 해당 체크포인트의 "완료 증거"로 충분한지 판정하시오.
아래 4개 중 정확히 하나를 선택:
  ✅ 완료 증거 명확
  🟡 진행 중 흔적만 (완료 확인 불가)
  🔴 증거 전무
  ⚪ 흔적은 있으나 완료 여부 모호

응답은 반드시 JSON 한 줄: {{"label": "<이모지>", "confidence": <0.0~1.0>}}
자유 서술·해석 금지."""
    return prompt


def load_state_safe(path):
    """state/ops_state.json 안전 로드. 없으면 빈 dict."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_cell_overrides(classified_cells, state):
    """
    state.cell_overrides의 라벨을 분류 결과에 강제 적용 (v1.3, 자연어 피드백 반영).

    매칭 키: deal_id + checkpoint_id + session_no
      · session_no가 명시되면 해당 회차만
      · session_no가 None이면 같은 deal·cp의 모든 셀 (deal-level cp 또는 전체 회차 일괄)

    동일 셀에 override 여러 건이면 적용순(applied_at) 마지막 값이 유효.
    """
    overrides = (state or {}).get("cell_overrides") or []
    if not overrides:
        return 0

    # applied_at 오름차순 정렬 (나중 것이 덮어씀)
    overrides_sorted = sorted(overrides, key=lambda o: o.get("applied_at") or "")

    applied = 0
    for ov in overrides_sorted:
        target_deal = ov.get("deal_id")
        target_cp = ov.get("checkpoint_id")
        target_session = ov.get("session_no")
        target_label = ov.get("label")
        if not target_deal or not target_cp or not target_label:
            continue
        for c in classified_cells:
            cell = c.get("cell", {})
            if cell.get("deal_id") != target_deal:
                continue
            if cell.get("checkpoint_id") != target_cp:
                continue
            if target_session is not None and cell.get("session_no") != target_session:
                continue
            c["label"] = target_label
            c["confidence"] = 1.0
            c["auto_decided"] = False
            c["auto_reason"] = "manual_override"
            c["llm_prompt"] = None
            c["manual_override_reason"] = ov.get("reason") or "자연어 피드백"
            c["manual_override_at"] = ov.get("applied_at")
            applied += 1
    return applied


def classify(evidence_data, checkpoints_data):
    cp_by_id = {cp["id"]: cp for cp in checkpoints_data.get("checkpoints", [])}
    by_deal_index, by_session_index = build_evidence_indexes(evidence_data)
    classified = []

    for entry in evidence_data.get("evidence_per_cell", []):
        cell = entry.get("cell", {})
        evidence = entry.get("evidence", {}) or {}
        counts = entry.get("evidence_count", {}) or {}
        total = counts.get("total", 0)
        status = cell.get("status", "")
        cp = cp_by_id.get(cell.get("checkpoint_id"))
        if not cp:
            continue

        # ── 5분류 라벨 부여 ──
        if status == "future":
            # 미래 셀 → 📅 예정 자동
            label = "📅"
            confidence = 1.0
            auto_decided = True
            auto_reason = "future"
            prompt = None
            summary = "시점 미도래 → 자동 📅 예정"
        elif total == 0:
            # 증거 0건 → 후속 phase 증거 lookup
            if has_successor_evidence(cell, by_deal_index, by_session_index):
                label = "✅"
                confidence = 0.7  # 추정이므로 LLM 직접 판정(1.0)보다 낮음
                auto_decided = True
                auto_reason = "successor_evidence"
                prompt = None
                summary = "본 셀 증거 없음 + 후속 단계 증거 있음 → 자동 ✅ 완료 추정"
            else:
                label = "🔴"
                confidence = 1.0
                auto_decided = True
                auto_reason = "no_evidence_no_successor"
                prompt = None
                summary = "증거 없음 + 후속 단계도 증거 없음 → 자동 🔴 실제 미확보"
        else:
            # 증거 있음
            if has_successor_evidence(cell, by_deal_index, by_session_index):
                # 본 셀 증거 + 후속 phase 증거 모두 있음 → 자동 ✅ 완료 추정 (보수적)
                # v1.1.1 (260427): Customer F 기업계약처럼 본 셀 키워드 매칭 약하나
                # 후속 단계(교안·교육)에 진행 증거 있는 케이스 자동 ✅ 처리해 알림 노이즈 제거
                label = "✅"
                confidence = 0.55  # 자동 ✅ 중 가장 낮은 신뢰도 (LLM 미경유)
                auto_decided = True
                auto_reason = "local_and_successor_evidence"
                prompt = None
                summary = (
                    f"본 셀 증거 있음(슬랙 {counts.get('slack', 0)}/메일 {counts.get('gmail', 0)}/드라이브 {counts.get('drive', 0)}) "
                    f"+ 후속 단계 증거 있음 → 자동 ✅ 완료 추정 (보수적)"
                )
            else:
                # 본 셀 증거만 있음 + 후속 단계 증거 없음 → LLM 4분류 호출 필요
                label = "PENDING"
                confidence = None
                auto_decided = False
                auto_reason = None
                prompt = build_llm_prompt(cell, cp, evidence)
                summary = (
                    f"슬랙 {counts.get('slack', 0)}건 / "
                    f"메일 {counts.get('gmail', 0)}건 / "
                    f"드라이브 {counts.get('drive', 0)}건"
                )

        classified.append(
            {
                "cell": cell,
                "evidence_summary": summary,
                "evidence_count": counts,
                "llm_prompt": prompt,
                "label": label,
                "confidence": confidence,
                "auto_decided": auto_decided,
                "auto_reason": auto_reason,
            }
        )

    return {
        "classified_cells": classified,
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "version": "v1.1",
            "total": len(classified),
            "auto_scheduled": sum(1 for c in classified if c["auto_reason"] == "future"),
            "auto_completed_inferred": sum(
                1 for c in classified if c["auto_reason"] == "successor_evidence"
            ),
            "auto_completed_local_and_successor": sum(
                1 for c in classified if c["auto_reason"] == "local_and_successor_evidence"
            ),
            "auto_missing": sum(
                1 for c in classified if c["auto_reason"] == "no_evidence_no_successor"
            ),
            "pending_llm": sum(1 for c in classified if c["label"] == "PENDING"),
            "sessions_indexed": len(by_session_index),
            "deals_indexed": len(by_deal_index),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Step 3: LLM 분류 입력 준비 (v1.3, 5분류 + cell_overrides)")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--checkpoints", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--state", default=None, help="state/ops_state.json (선택, 자연어 피드백 반영)")
    args = parser.parse_args()

    evidence = load_json(args.evidence)
    checkpoints = load_json(args.checkpoints)
    state = load_state_safe(args.state) if args.state else {}

    result = classify(evidence, checkpoints)
    overrides_applied = apply_cell_overrides(result["classified_cells"], state)
    result["meta"]["manual_overrides_applied"] = overrides_applied
    if overrides_applied:
        print(f"[OK] cell_overrides {overrides_applied}건 적용")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    meta = result["meta"]
    print(f"[OK] 분류 입력 준비 완료 → {os.path.abspath(args.out)}")
    print(
        f"     전체 {meta['total']}건 | "
        f"자동 📅 {meta['auto_scheduled']} · "
        f"자동 ✅(후속 단계 추정) {meta['auto_completed_inferred']} · "
        f"자동 🔴 {meta['auto_missing']} · "
        f"PENDING(LLM 호출 필요) {meta['pending_llm']}"
    )
    if meta["pending_llm"] > 0:
        print(
            f"     ⚠️ 오케스트레이터가 PENDING 셀들의 llm_prompt를 LLM에 전달 후 label 업데이트 필요"
        )


if __name__ == "__main__":
    main()
