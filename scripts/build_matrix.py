"""
Step 1 — 셀 매트릭스 구축 (ops-plugin v1.1, 260427 분류 프레임 재설계)

일정(sessions) × 체크포인트(9개) 격자를 만들고
"감시 대상 셀(arriving·overdue)" + "미래 셀(future)" 모두 추출.
각 셀에 phase 정보(시간 순서) 부여.

입력:
  --sessions    runtime/sessions.json  (Step 0.5 compose_schedule 출력)
  --checkpoints config/checkpoints.json (v0.2: phases 정의 + 각 cp에 phase id)
  --out         runtime/arriving_cells.json
  --today       YYYY-MM-DD (선택, 기본은 오늘)
  --state       state/ops_state.json (선택) — 자연어 피드백 누적 상태
                  · watchlist_exclusions: [deal_id] — 추적 제외
                  · deal_flag_overrides: {deal_id: {flag_id: bool}} — 딜별 플래그
  --flags       JSON 문자열 (선택) — 전역 플래그 덮어쓰기 (테스트용)

로직 (v1.1 변경):
  for deal in sessions.deals_with_schedule:
    if deal_id in watchlist_exclusions: 건너뜀
    deal_flags = default(checkpoints.deal_flags) + state.deal_flag_overrides[deal_id]
    for session in deal.sessions:
      for checkpoint in checkpoints:
        if checkpoint.flag_id and not deal_flags[checkpoint.flag_id]: 건너뜀
        anchor_date = session[checkpoint.anchor]
        today_offset = (today - anchor_date).days
        phase = phases[cp.phase]  # phase_order, phase_id, phase_label
        if today_offset < window.from: status="future"  # 📅 예정 자동 부여 위해 출력
        elif window.from <= today_offset <= window.to: status="arriving"
        elif today_offset > window.to:
          if session_ended + STALE_GRACE: 건너뜀 (stale)
          else: status="overdue"

  ※ future 셀도 출력에 포함 — classify_evidence가 5분류 자동 부여(📅) +
    같은 (deal, session)의 후속 phase에 진행 증거 있으면 ✅ 완료 추정 lookup용으로 활용

v1.0 (2026-04-24)
v1.1 (2026-04-27): future 셀 포함 + phase 정보(order·id·label) 부여. 5분류·시간 순서 반영용.
v1.2 (2026-04-27): granularity 필드 적용 - deal 단위 cp는 같은 deal에서 1셀만 생성
                  (가장 빠른 회차의 anchor 기준, session_no=null). 다차수 노이즈 제거.
"""

import argparse
import json
import os
import sys
import io
from datetime import datetime, date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_date(s):
    if not s:
        return None
    s = str(s)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_d_day(offset):
    if offset == 0:
        return "D-0"
    return f"D{offset:+d}"


STALE_GRACE_DAYS = 7


def build_phase_lookup(checkpoints_data):
    """phases 정의에서 phase_id → {order, label} 매핑 생성. v1.1 신규."""
    lookup = {}
    for ph in checkpoints_data.get("phases", []) or []:
        pid = ph.get("id")
        if pid:
            lookup[pid] = {
                "order": ph.get("order", 0),
                "label": ph.get("label", pid),
            }
    return lookup


def build_matrix(
    sessions_data,
    checkpoints_data,
    today,
    default_flags,
    watchlist_exclusions=None,
    deal_flag_overrides=None,
    cell_override_keys=None,
):
    if watchlist_exclusions is None:
        watchlist_exclusions = set()
    else:
        watchlist_exclusions = set(watchlist_exclusions)
    if deal_flag_overrides is None:
        deal_flag_overrides = {}
    if cell_override_keys is None:
        cell_override_keys = set()
    else:
        cell_override_keys = set(cell_override_keys)

    checkpoints = checkpoints_data.get("checkpoints", [])
    phase_lookup = build_phase_lookup(checkpoints_data)
    deals_with = sessions_data.get("deals_with_schedule", {})

    arriving_cells = []
    skipped_inactive = 0
    skipped_nodate = 0
    skipped_watchlist = 0
    skipped_stale = 0

    for deal_id, deal in deals_with.items():
        # watchlist 제외 딜
        if deal_id in watchlist_exclusions:
            skipped_watchlist += 1
            continue

        deal_name = deal.get("deal_name", "")
        customer = deal.get("customer", "")
        course_id = deal.get("course_id")
        sessions = deal.get("sessions", [])

        # 딜별 플래그 결정: 기본값 + state 오버라이드
        effective_flags = dict(default_flags)
        if deal_id in deal_flag_overrides:
            effective_flags.update(deal_flag_overrides[deal_id])

        # v1.2: deal 단위 cp용 primary session 선정 — 가장 빠른 edu_start
        primary_session = None
        if sessions:
            sorted_sessions = sorted(
                sessions,
                key=lambda s: parse_date(s.get("edu_start")) or date.max,
            )
            primary_session = sorted_sessions[0]

        # v1.2: deal 단위 cp는 primary session 기준 1셀만 생성
        # session 단위 cp는 모든 회차에 셀 생성
        deal_level_cps = [cp for cp in checkpoints if cp.get("granularity") == "deal"]
        session_level_cps = [cp for cp in checkpoints if cp.get("granularity") != "deal"]

        # === deal 단위 cp 처리 (deal당 1셀, session_no=null) ===
        if primary_session is not None:
            session = primary_session
            edu_start_str = session.get("edu_start")
            edu_end_str = session.get("edu_end")
            anchor_dates = {
                "edu_start": parse_date(edu_start_str),
                "edu_end": parse_date(edu_end_str),
            }
            edu_end_date = anchor_dates["edu_end"]
            session_ended = edu_end_date is not None and today > edu_end_date

            for cp in deal_level_cps:
                cp_id = cp["id"]
                flag_id = cp.get("flag_id")
                if flag_id and not effective_flags.get(flag_id, False):
                    skipped_inactive += 1
                    continue
                anchor_key = cp["anchor"]
                anchor_date = anchor_dates.get(anchor_key)
                if anchor_date is None:
                    skipped_nodate += 1
                    continue

                today_offset = (today - anchor_date).days
                window_from = cp["window"]["from"]
                window_to = cp["window"]["to"]

                if today_offset < window_from:
                    status = "future"
                elif window_from <= today_offset <= window_to:
                    status = "arriving"
                else:
                    # v1.2.2 (260428): deal 단위 cp stale 분기 좁힘.
                    # state cell_override 등록된 (deal_id, cp_id) 키만 stale 면제 (LD 자연어 피드백 영구성 보장).
                    # 등록 없는 deal 단위 cp는 자연 stale 처리 — 출력 노이즈 감소 (v19 형태 복원).
                    has_override = (deal_id, cp_id) in cell_override_keys
                    if session_ended and today_offset > window_to + STALE_GRACE_DAYS and not has_override:
                        skipped_stale += 1
                        continue
                    status = "overdue"

                phase_id = cp.get("phase")
                phase_info = phase_lookup.get(phase_id, {}) if phase_id else {}

                arriving_cells.append({
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "customer": customer,
                    "course_id": course_id,
                    "session_no": None,  # v1.2: deal 단위 셀
                    "granularity": "deal",
                    "edu_start": edu_start_str,
                    "edu_end": edu_end_str,
                    "checkpoint_id": cp_id,
                    "checkpoint_label": cp["label"],
                    "category": cp["category"],
                    "severity": cp["severity"],
                    "phase_id": phase_id,
                    "phase_order": phase_info.get("order"),
                    "phase_label": phase_info.get("label"),
                    "anchor": anchor_key,
                    "today_offset": today_offset,
                    "d_day": format_d_day(today_offset),
                    "status": status,
                })

        # === session 단위 cp 처리 (회차별 셀) ===
        for session in sessions:
            session_no = session.get("session_no")
            edu_start_str = session.get("edu_start")
            edu_end_str = session.get("edu_end")
            anchor_dates = {
                "edu_start": parse_date(edu_start_str),
                "edu_end": parse_date(edu_end_str),
            }
            edu_end_date = anchor_dates["edu_end"]
            session_ended = edu_end_date is not None and today > edu_end_date

            for cp in session_level_cps:
                cp_id = cp["id"]
                flag_id = cp.get("flag_id")
                if flag_id and not effective_flags.get(flag_id, False):
                    skipped_inactive += 1
                    continue
                anchor_key = cp["anchor"]
                anchor_date = anchor_dates.get(anchor_key)
                if anchor_date is None:
                    skipped_nodate += 1
                    continue

                today_offset = (today - anchor_date).days
                window_from = cp["window"]["from"]
                window_to = cp["window"]["to"]

                if today_offset < window_from:
                    status = "future"
                elif window_from <= today_offset <= window_to:
                    status = "arriving"
                else:
                    if session_ended and today_offset > window_to + STALE_GRACE_DAYS:
                        skipped_stale += 1
                        continue
                    status = "overdue"

                phase_id = cp.get("phase")
                phase_info = phase_lookup.get(phase_id, {}) if phase_id else {}

                arriving_cells.append({
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "customer": customer,
                    "course_id": course_id,
                    "session_no": session_no,
                    "granularity": "session",
                    "edu_start": edu_start_str,
                    "edu_end": edu_end_str,
                    "checkpoint_id": cp_id,
                    "checkpoint_label": cp["label"],
                    "category": cp["category"],
                    "severity": cp["severity"],
                    "phase_id": phase_id,
                    "phase_order": phase_info.get("order"),
                    "phase_label": phase_info.get("label"),
                    "anchor": anchor_key,
                    "today_offset": today_offset,
                    "d_day": format_d_day(today_offset),
                    "status": status,
                })

    # 정렬: phase_order(시간순서) → today_offset(임박도) → checkpoint_id
    arriving_cells.sort(
        key=lambda c: (
            c.get("phase_order") or 99,
            c["today_offset"],
            c["checkpoint_id"],
        )
    )

    return {
        "arriving_cells": arriving_cells,
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "version": "v1.1",
            "today": today.isoformat(),
            "arriving_count": sum(1 for c in arriving_cells if c["status"] == "arriving"),
            "overdue_count": sum(1 for c in arriving_cells if c["status"] == "overdue"),
            "future_count": sum(1 for c in arriving_cells if c["status"] == "future"),
            "skipped_inactive": skipped_inactive,
            "skipped_nodate": skipped_nodate,
            "skipped_watchlist": skipped_watchlist,
            "skipped_stale": skipped_stale,
            "phases_loaded": len(phase_lookup),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Step 1: 셀 매트릭스 구축 (v1.1, phase 정보 + future 셀 포함)")
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--checkpoints", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--today", default=None, help="YYYY-MM-DD (기본: 오늘)")
    parser.add_argument(
        "--state",
        default=None,
        help="state/ops_state.json (선택) — 자연어 피드백 누적 상태",
    )
    parser.add_argument(
        "--flags",
        default=None,
        help='전역 플래그 오버라이드 JSON 문자열. 예: \'{"has_customer_announcement": true}\'',
    )
    args = parser.parse_args()

    sessions_data = load_json(args.sessions)
    checkpoints_data = load_json(args.checkpoints)

    if args.today:
        today = parse_date(args.today)
        if today is None:
            print(f"[ERROR] --today 형식 오류: {args.today}", file=sys.stderr)
            sys.exit(1)
    else:
        today = date.today()

    # 기본 플래그: checkpoints.json의 default
    default_flags = {
        fid: info.get("default", False)
        for fid, info in checkpoints_data.get("deal_flags", {}).items()
    }

    # 전역 오버라이드 (테스트용 --flags)
    if args.flags:
        try:
            override = json.loads(args.flags)
            default_flags.update(override)
        except json.JSONDecodeError as e:
            print(f"[ERROR] --flags JSON 파싱 실패: {e}", file=sys.stderr)
            sys.exit(1)

    # state 파일 로드 (자연어 피드백 누적)
    watchlist_exclusions = []
    deal_flag_overrides = {}
    cell_override_keys = set()
    if args.state and os.path.exists(args.state):
        state = load_json(args.state, default={})
        watchlist_exclusions = state.get("watchlist_exclusions", []) or []
        deal_flag_overrides = state.get("deal_flag_overrides", {}) or {}
        # v1.2.2 (260428): cell_override 등록된 (deal_id, cp_id) 키 set — stale 면제 매칭용
        for ov in (state.get("cell_overrides", []) or []):
            d = ov.get("deal_id")
            cp = ov.get("checkpoint_id")
            if d and cp:
                cell_override_keys.add((d, cp))

    result = build_matrix(
        sessions_data,
        checkpoints_data,
        today,
        default_flags,
        watchlist_exclusions=watchlist_exclusions,
        deal_flag_overrides=deal_flag_overrides,
        cell_override_keys=cell_override_keys,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    meta = result["meta"]
    print(f"[OK] 셀 매트릭스 구축 완료 → {os.path.abspath(args.out)}")
    print(
        f"     오늘={meta['today']} | arriving {meta['arriving_count']}건 · "
        f"overdue {meta['overdue_count']}건 · future {meta.get('future_count', 0)}건"
    )
    print(
        f"     스킵: 조건부비활성 {meta['skipped_inactive']} · "
        f"날짜없음 {meta['skipped_nodate']} · watchlist {meta['skipped_watchlist']} · "
        f"stale {meta.get('skipped_stale', 0)} | phases={meta.get('phases_loaded', 0)}개"
    )


if __name__ == "__main__":
    main()
