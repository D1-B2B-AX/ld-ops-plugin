"""
Step 4 — 리포트 렌더러 (ops-plugin v1.1, 260427 분류 프레임 재설계)

파트장 가이드 반영 4구역 레이아웃:
  1) 🚨 즉각 해결 필요  — 🔴 + severity=즉각해결 + status in (arriving, overdue)
  2) ⚠️ 확인 필요       — 🔴 + severity=확인필요 + status in (arriving, overdue)
  3) 📋 진행 현황       — 그 외 모든 셀, 옵션 A 그룹화 (deal·session 단위 묶음)
                          → 라벨별 그룹: 🔴(관찰) · 🟡 · ✅ · 📅 · ⚪
  4) ⚠️ 일정 미확정     — sessions_data.deals_no_schedule 기반 (조건부)

알림 위계(alert_tier) 결정 룰:
  · severity == "즉각해결" + label == "🔴" + status in (arriving, overdue) → "immediate"
  · severity == "확인필요" + label == "🔴" + status in (arriving, overdue) → "review"
  · 그 외 (📅·✅·🟡·⚪ 모두 + 관찰 severity의 🔴) → "progress"

  ※ severity=관찰의 🔴 셀(예: 세금계산서 미발행 D+10)은 진행 현황 영역에 포함.
    별도 🟢 관찰 영역 만들지 않음 (사용자 의도: 진행 현황 흐름에 합쳐 보기).

입력:
  --classified  runtime/classified_cells.json (v1.1: 5분류 라벨 + auto_reason)
  --checkpoints config/checkpoints.json
  --sessions    runtime/sessions.json (선택, 일정 미확정 섹션용)
  --out         outputs/ops_report_YYYYMMDD.md
  --today       YYYY-MM-DD (선택)

stale 필터:
  세션 종료(today > edu_end) AND today_offset > window_to + 7 → 렌더 제외
  (build_matrix.py와 동일 규칙. 구 classified_cells.json 재사용 시 필요)

v1.0 (2026-04-24)
v1.1 (2026-04-27): 4구역 레이아웃 + 옵션 A 그룹화 진행 현황 + 알림 위계 동적 결정.
v1.7 (2026-04-28): llm_reason 활용 — 알림 부연·진행 현황 우선 표시. (다차수 맥락 손실 차단)
"""

import argparse
import json
import os
import sys
import io
from collections import defaultdict
from datetime import datetime, date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


SEPARATOR = "━" * 43
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
STALE_GRACE_DAYS = 7
SEVERITY_ORDER = {"즉각해결": 0, "확인필요": 1, "관찰": 2}
UNRESOLVED_TAG = "진행 중 (세부 회차 미확정)"

# v1.1.1 (260427): 데이터 꼬임 케이스 — 모든 알림 후보 셀의 today_offset이
# 임계값을 초과하면 세일즈맵 일정과 실제 운영이 어긋난 딜로 판정
# (예: Customer G 1년 싱글플랜) → 즉각해결 영역에서 제외, 확인필요로 통합
DATA_MISMATCH_THRESHOLD_DAYS = 30

# 진행 현황 섹션 라벨 출력 순서·레이블 (v1.1)
PROGRESS_LABEL_ORDER = [
    ("🔴", "🔴 미확보  "),
    ("🟡", "🟡 진행 중  "),
    ("✅", "✅ 완료 추정 "),
    ("📅", "📅 예정     "),
    ("⚪", "⚪ 모호     "),
]

# v1.2 (260427): 진행 현황 영역 데이터 표시 — 압축 발췌 길이
BRIEF_MAX_LEN = 90


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def format_date_header(d):
    return f"{d.isoformat()}({WEEKDAY_KR[d.weekday()]})"


def format_date_short(d):
    if not d:
        return "-"
    return f"{d.month}/{d.day}({WEEKDAY_KR[d.weekday()]})"


def short_customer(customer):
    if not customer:
        return "(고객사 미상)"
    if "(" in customer and ")" in customer:
        inside = customer.split("(", 1)[1].split(")", 1)[0].strip()
        if inside:
            return inside
    stripped = customer.strip()
    return stripped if len(stripped) <= 12 else stripped[:12] + "…"


def build_evidence_index(evidence_data):
    """(deal_id, checkpoint_id, session_no) → evidence dict 매핑 (v1.2 신규)."""
    out = {}
    for e in (evidence_data or {}).get("evidence_per_cell", []) or []:
        c = e.get("cell", {}) or {}
        key = (c.get("deal_id"), c.get("checkpoint_id"), c.get("session_no"))
        out[key] = e.get("evidence", {}) or {}
    return out


def shorten_text(text, max_len=BRIEF_MAX_LEN):
    """텍스트를 max_len 이내로 압축 + 줄바꿈/공백 정리."""
    if not text:
        return ""
    s = " ".join(str(text).split())
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip() + "…"


def extract_short_reason(llm_reason, max_len=45):
    """llm_reason에서 알림 부연용 짧은 핵심구를 추출 (v1.7).

    형식 가정: "<context> — <reason>" or "<reason>"
    em-dash 뒤를 우선 채택, 첫 문장(. 분리)만 사용, 길면 잘라냄.
    """
    if not llm_reason:
        return ""
    s = " ".join(str(llm_reason).split())
    parts = s.split("—", 1)
    text = parts[1].strip() if len(parts) == 2 else s
    for sep in [". ", "."]:
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def short_email_from(from_field):
    """'Person A (someone@example.com)' → 'Person A'."""
    if not from_field:
        return ""
    s = str(from_field).strip()
    if "(" in s:
        s = s.split("(", 1)[0].strip()
    if "<" in s:
        s = s.split("<", 1)[0].strip()
    return s


def short_date(d):
    if not d:
        return ""
    s = str(d)[:10]
    return s


def format_evidence_brief(evidence, label):
    """evidence에서 가장 최신 1건을 짧게 압축 (v1.2).

    우선순위 (Owner 가이드 — 실시간성):
      메일(thread_summary) > 슬랙(slack_results) > 드라이브(planning_sheet/files)

    📅 예정·🔴 미확보·자동 ✅(후속 추정)은 호출 측에서 메타 텍스트 사용.
    이 함수는 본 셀에 evidence 직접 있는 케이스 처리.
    """
    if not evidence:
        return None

    gmail = evidence.get("gmail") or []
    if gmail:
        latest = max(gmail, key=lambda x: x.get("date", "") or "")
        date = short_date(latest.get("date", ""))
        from_ = short_email_from(latest.get("from", ""))
        subj = latest.get("subject", "") or ""
        snippet = latest.get("snippet", "") or ""
        # 제목 우선, 길면 축약
        body = subj if subj else snippet
        body = shorten_text(body, BRIEF_MAX_LEN)
        prefix_parts = [p for p in [date, from_] if p]
        prefix = " ".join(prefix_parts) + " 메일" if prefix_parts else "메일"
        return f"{prefix} - {body}"

    slack = evidence.get("slack") or []
    if slack:
        latest = max(slack, key=lambda x: x.get("date", "") or "")
        date = short_date(latest.get("date", ""))
        author = latest.get("author", "") or ""
        snippet = latest.get("snippet", "") or ""
        body = shorten_text(snippet, BRIEF_MAX_LEN)
        prefix_parts = [p for p in [date, author] if p]
        prefix = " ".join(prefix_parts) + " 슬랙" if prefix_parts else "슬랙"
        return f"{prefix} - {body}"

    drive = evidence.get("drive") or []
    if drive:
        latest = drive[0]
        file_name = latest.get("file_name", "") or ""
        tab = latest.get("tab_name", "") or ""
        dtype = latest.get("type", "") or ""
        snippet = latest.get("snippet", "") or ""
        # 드라이브는 파일명·탭·내용 발췌 조합
        body_parts = []
        if file_name:
            body_parts.append(shorten_text(file_name, 40))
        if tab and tab != file_name:
            body_parts.append(tab)
        if snippet:
            body_parts.append(shorten_text(snippet, 60))
        elif dtype and not body_parts:
            body_parts.append(dtype)
        body = " · ".join(body_parts) if body_parts else "(파일 정보)"
        return f"드라이브 - {body}"

    return None


def determine_alert_tier(classified_entry):
    """v1.1: 5분류 라벨 + severity + status → alert_tier 결정.

    Returns: "immediate" | "review" | "progress"
    """
    cell = classified_entry.get("cell", {}) or {}
    label = classified_entry.get("label", "")
    severity = cell.get("severity", "")
    status = cell.get("status", "")

    if label == "🔴" and status in ("arriving", "overdue"):
        if severity == "즉각해결":
            return "immediate"
        if severity == "확인필요":
            return "review"
    return "progress"


def apply_pending_fallback(cells):
    """PENDING 라벨을 🔴로 안전 fallback (LLM 호출 미완 대비)."""
    pending = [c for c in cells if c.get("label") == "PENDING"]
    if pending:
        print(
            f"[WARN] PENDING 셀 {len(pending)}건 — 🔴 fallback 처리",
            file=sys.stderr,
        )
        for c in pending:
            c["label"] = "🔴"
            c["fallback_from_pending"] = True


def apply_stale_filter(cells, checkpoints_data, today):
    """build_matrix.py와 동일한 stale 규칙으로 후필터."""
    cp_window_to = {
        cp["id"]: cp["window"]["to"]
        for cp in checkpoints_data.get("checkpoints", [])
    }
    kept = []
    dropped = 0
    for c in cells:
        cell = c.get("cell", {})
        # v1.6.2 (260428): manual_override 셀만 stale 면제 (build_matrix.py v1.2.2와 동기화).
        # LD 자연어 피드백 등록된 (deal_id, cp_id)만 살림 — 외 deal 단위 cp는 자연 stale 처리.
        if c.get("auto_reason") == "manual_override":
            kept.append(c)
            continue
        edu_end = parse_date(cell.get("edu_end"))
        session_ended = edu_end is not None and today > edu_end
        cp_id = cell.get("checkpoint_id")
        window_to = cp_window_to.get(cp_id)
        offset = cell.get("today_offset", 0)
        if (
            session_ended
            and window_to is not None
            and offset > window_to + STALE_GRACE_DAYS
        ):
            dropped += 1
            continue
        kept.append(c)
    if dropped:
        print(f"[INFO] stale 셀 {dropped}건 제외 (지난 세션 + 창 지남)", file=sys.stderr)
    return kept


def render_header(today):
    return "\n".join(
        [
            SEPARATOR,
            f"📋 **{format_date_header(today)} 수주 과정 운영 현황**",
            SEPARATOR,
        ]
    )


# ── 알림 영역 (🚨 즉각 / ⚠️ 확인) ─────────────────────────────────────


def group_alert_cells(alert_cells):
    """같은 (deal_id, checkpoint_id) 묶어 회차 범위로 축약."""
    groups = defaultdict(list)
    for c in alert_cells:
        cell = c["cell"]
        key = (cell.get("deal_id"), cell.get("checkpoint_id"))
        groups[key].append(c)

    out = []
    for (deal_id, cp_id), items in groups.items():
        cell0 = items[0]["cell"]
        session_nos = sorted(
            [i["cell"].get("session_no") for i in items
             if i["cell"].get("session_no") is not None]
        )
        offsets = [i["cell"].get("today_offset", 0) for i in items]
        urgent_offset = max(offsets) if offsets else 0
        urgent_idx = offsets.index(urgent_offset)
        urgent_d_day = items[urgent_idx]["cell"].get("d_day", "")
        out.append(
            {
                "deal_id": deal_id,
                "checkpoint_id": cp_id,
                "customer": cell0.get("customer", ""),
                "checkpoint_label": cell0.get("checkpoint_label", ""),
                "severity": cell0.get("severity", "확인필요"),
                "session_nos": session_nos,
                "urgent_offset": urgent_offset,
                "urgent_d_day": urgent_d_day,
                "count": len(items),
                "llm_reasons": [i.get("llm_reason") for i in items if i.get("llm_reason")],
            }
        )
    return out


def format_session_range(session_nos):
    """[4,5,6] → '4~6회차', [4] → '4회차', [] → ''. 비연속이면 ','로."""
    if not session_nos:
        return ""
    if len(session_nos) == 1:
        return f"{session_nos[0]}회차"
    is_continuous = all(
        session_nos[i + 1] - session_nos[i] == 1
        for i in range(len(session_nos) - 1)
    )
    if is_continuous:
        return f"{session_nos[0]}~{session_nos[-1]}회차"
    return f"{','.join(str(n) for n in session_nos)}회차"


def build_data_mismatch_deal_ids(cells):
    """
    딜의 모든 알림 후보 셀(🔴 + arriving/overdue)이 today_offset>임계값이면
    세일즈맵 일정과 실 운영 어긋난 데이터 꼬임 딜로 판정 (v1.1.1, 260427).

    예: Customer G 1년 싱글플랜 (edu_start=12/21이지만 실 교육은 1/26~2/11 종료)
    → 모든 체크포인트 today_offset이 100일 이상 → 데이터 꼬임으로 판정.
    """
    by_deal = defaultdict(list)
    for c in cells:
        did = (c.get("cell") or {}).get("deal_id")
        if did:
            by_deal[did].append(c)

    out = set()
    for did, items in by_deal.items():
        alert_candidates = [
            c
            for c in items
            if c.get("label") == "🔴"
            and (c.get("cell") or {}).get("status") in ("arriving", "overdue")
        ]
        if alert_candidates and all(
            (c.get("cell") or {}).get("today_offset", 0) > DATA_MISMATCH_THRESHOLD_DAYS
            for c in alert_candidates
        ):
            out.add(did)
    return out


def render_alert_section(
    cells, header_emoji, header_label, severity_filter, unresolved_deal_ids=None,
    data_mismatch_deal_ids=None,
):
    """🚨 또는 ⚠️ 알림 섹션 렌더 — severity_filter 매칭 셀만.

    v1.1.1: 데이터 꼬임 딜은 즉각해결 영역에서 제외 → 확인필요로 통합 표시.
    """
    unresolved_deal_ids = unresolved_deal_ids or set()
    data_mismatch_deal_ids = data_mismatch_deal_ids or set()

    target = [
        c
        for c in cells
        if c.get("label") == "🔴"
        and c["cell"].get("severity") == severity_filter
        and c["cell"].get("status") in ("arriving", "overdue")
    ]

    # v1.1.1: 즉각해결 영역에선 데이터 꼬임 딜 제외 (확인필요로 통합)
    if severity_filter == "즉각해결" and data_mismatch_deal_ids:
        target = [c for c in target if c["cell"].get("deal_id") not in data_mismatch_deal_ids]
    # 확인필요 영역에선 데이터 꼬임 딜의 즉각해결 셀까지 포함 (통합 보고)
    elif severity_filter == "확인필요" and data_mismatch_deal_ids:
        immediate_for_mismatch = [
            c
            for c in cells
            if c.get("label") == "🔴"
            and c["cell"].get("severity") == "즉각해결"
            and c["cell"].get("status") in ("arriving", "overdue")
            and c["cell"].get("deal_id") in data_mismatch_deal_ids
        ]
        target = target + immediate_for_mismatch

    cp_groups = group_alert_cells(target)

    # (deal_id, urgent_d_day)로 재그룹 — 회차 합집합, 체크포인트 묶음
    merged = defaultdict(
        lambda: {
            "deal_id": "",
            "customer": "",
            "session_nos": set(),
            "urgent_d_day": "",
            "urgent_offset": 0,
            "checkpoint_labels": [],
            "llm_reasons": [],
        }
    )
    for g in cp_groups:
        key_dday = UNRESOLVED_TAG if g["deal_id"] in unresolved_deal_ids else g["urgent_d_day"]
        key = (g["deal_id"], key_dday)
        m = merged[key]
        m["deal_id"] = g["deal_id"]
        m["customer"] = g["customer"]
        m["session_nos"].update(g["session_nos"])
        m["urgent_d_day"] = key_dday
        m["urgent_offset"] = g["urgent_offset"]
        m["checkpoint_labels"].append(g["checkpoint_label"])
        m["llm_reasons"].extend(g.get("llm_reasons", []))

    groups = list(merged.values())
    groups.sort(key=lambda g: -g["urgent_offset"])

    lines = [f"## {header_emoji} {header_label}", ""]

    if not groups:
        lines.append("_(해당 없음)_")
        return "\n".join(lines)

    for g in groups:
        customer = short_customer(g["customer"])
        is_unresolved = g["deal_id"] in unresolved_deal_ids
        cps_text = "·".join(g["checkpoint_labels"])

        if is_unresolved:
            sentence = (
                f"**{customer}** 교육 진행 중 (세부 회차 미확정) — "
                f"{cps_text} 관련 증거 없음, 확인 필요"
            )
        else:
            session_nos = sorted(g["session_nos"])
            sess = format_session_range(session_nos)
            offset = g["urgent_offset"]
            if offset < 0:
                sentence = (
                    f"**{customer}** {sess} 교육 **{g['urgent_d_day']}** 임박 — "
                    f"{cps_text} 관련 증거 없음"
                )
            elif offset > 0:
                sentence = (
                    f"**{customer}** {sess} 교육 종료 후 **{g['urgent_d_day']}** — "
                    f"{cps_text} 미처리 상태"
                )
            else:
                sentence = (
                    f"**{customer}** {sess} 교육 **당일** — "
                    f"{cps_text} 즉시 확인"
                )

        # v1.7: llm_reason 부연 (다차수 맥락 보강)
        short_reason = ""
        for r in g.get("llm_reasons", []):
            ext = extract_short_reason(r)
            if ext:
                short_reason = ext
                break
        if short_reason:
            sentence = f"{sentence} ({short_reason})"
        lines.append(f"- {sentence}")
    return "\n".join(lines)


# ── 진행 현황 영역 (📋 옵션 A 그룹화) ─────────────────────────────────


def compute_session_d_day(items, today):
    """같은 session 묶음의 헤더용 D-day 계산. edu_start 기준."""
    cell0 = items[0]["cell"]
    edu_start = parse_date(cell0.get("edu_start"))
    if edu_start:
        offset = (today - edu_start).days
        return offset, edu_start
    # fallback: 가장 빠른 today_offset
    offsets = [i["cell"].get("today_offset", 0) for i in items]
    return min(offsets) if offsets else 0, None


def fingerprint_progress_group(items):
    """라벨별 체크포인트 set의 정렬 튜플로 fingerprint 생성 (v1.1.1).

    같은 fingerprint = 같은 라벨 패턴 = 진행 현황에서 합칠 수 있는 그룹.
    """
    by_label = defaultdict(set)
    for c in items:
        label = c.get("label", "⚪")
        cp_label = (c.get("cell") or {}).get("checkpoint_label", "")
        if cp_label:
            by_label[label].add(cp_label)
    return tuple(sorted((label, tuple(sorted(cps))) for label, cps in by_label.items()))


def merge_progress_groups(group_infos):
    """같은 deal_id + 동일 fingerprint인 인접 그룹 합치기 (v1.1.1).

    Customer D 26회차 같은 케이스: 같은 라벨 패턴이 여러 회차에 반복될 때
    한 블록으로 압축. 회차 범위 + D-day 범위로 표시.
    """
    if not group_infos:
        return []

    merged = []
    current = None
    for g in group_infos:
        fp = fingerprint_progress_group(g["items"])
        if current is None:
            current = dict(g)
            current["session_nos_list"] = [g["session_no"]] if g["session_no"] else []
            current["offsets_list"] = [g["offset"]]
            current["fingerprint"] = fp
            current["items_merged"] = list(g["items"])
            continue

        same_deal = g["deal_id"] == current["deal_id"]
        same_fp = fp == current["fingerprint"]
        both_resolved = (not g["is_unresolved"]) and (not current["is_unresolved"])
        # v1.3: deal-level 블록은 절대 합치지 않음
        either_is_deal_level = g.get("is_deal_level") or current.get("is_deal_level")

        if same_deal and same_fp and both_resolved and not either_is_deal_level:
            # 합치기
            if g["session_no"] is not None:
                current["session_nos_list"].append(g["session_no"])
            current["offsets_list"].append(g["offset"])
            current["items_merged"].extend(g["items"])
        else:
            merged.append(current)
            current = dict(g)
            current["session_nos_list"] = [g["session_no"]] if g["session_no"] else []
            current["offsets_list"] = [g["offset"]]
            current["fingerprint"] = fp
            current["items_merged"] = list(g["items"])

    if current is not None:
        merged.append(current)

    return merged


def format_session_range_v2(session_nos):
    """[4,5,6] → '4~6회차', [4,7,9] → '4·7·9회차', [4] → '4회차', [] → ''."""
    if not session_nos:
        return ""
    sorted_nos = sorted(set(s for s in session_nos if s is not None))
    if not sorted_nos:
        return ""
    if len(sorted_nos) == 1:
        return f"{sorted_nos[0]}회차"
    is_continuous = all(
        sorted_nos[i + 1] - sorted_nos[i] == 1 for i in range(len(sorted_nos) - 1)
    )
    if is_continuous:
        return f"{sorted_nos[0]}~{sorted_nos[-1]}회차"
    return f"{'·'.join(str(n) for n in sorted_nos)}회차"


def format_d_day_range(offsets):
    """offsets 리스트 → D-day 범위 문자열."""
    if not offsets:
        return ""
    sorted_off = sorted(offsets)
    if len(sorted_off) == 1:
        o = sorted_off[0]
        return f"D{o:+d}" if o != 0 else "D-0"
    if len(sorted_off) == 2:
        a, b = sorted_off
        a_s = f"D{a:+d}" if a != 0 else "D-0"
        b_s = f"D{b:+d}" if b != 0 else "D-0"
        return f"{a_s}·{b_s}"
    a, b = sorted_off[0], sorted_off[-1]
    a_s = f"D{a:+d}" if a != 0 else "D-0"
    b_s = f"D{b:+d}" if b != 0 else "D-0"
    return f"{a_s} ~ {b_s}"


def render_progress_section(
    cells, sessions_data, today, unresolved_deal_ids=None, evidence_index=None
):
    """📋 진행 현황 — 옵션 A 그룹화 + 회차 합치기 + 데이터 표시 (v1.3).

    v1.3: granularity="deal" 셀(session_no=null)은 "딜 전체" 블록으로 분리 출력.
          단일 라벨 블록은 한 줄 압축. 📅 메타는 cp_label만 표시.
    """
    unresolved_deal_ids = unresolved_deal_ids or set()
    evidence_index = evidence_index or {}
    progress = [c for c in cells if determine_alert_tier(c) == "progress"]

    lines = ["## 📋 진행 현황", ""]

    if not progress:
        lines.append("_(해당 없음)_")
        return "\n".join(lines)

    # v1.3: 셀 그룹핑 — granularity 기반 키 분리
    # deal-level 셀: (deal_id, "DEAL") / session-level 셀: (deal_id, session_no)
    groups = defaultdict(list)
    for c in progress:
        cell = c["cell"]
        granularity = cell.get("granularity", "session")
        if granularity == "deal":
            key = (cell.get("deal_id"), "DEAL")
        else:
            key = (cell.get("deal_id"), cell.get("session_no"))
        groups[key].append(c)

    # 묶음별 메타 + 정렬 키
    group_infos = []
    for (deal_id, session_no), items in groups.items():
        cell0 = items[0]["cell"]
        offset, edu_start = compute_session_d_day(items, today)
        is_unresolved = deal_id in unresolved_deal_ids
        is_deal_level = (session_no == "DEAL")  # v1.3: 딜 전체 블록 마커
        group_infos.append(
            {
                "deal_id": deal_id,
                "session_no": session_no,
                "items": items,
                "customer": cell0.get("customer", ""),
                "deal_name": cell0.get("deal_name", ""),
                "edu_start": edu_start,
                "offset": offset,
                "is_unresolved": is_unresolved,
                "is_deal_level": is_deal_level,
            }
        )

    # v1.4 (260427): 옵션 A 정렬 — 딜별 묶음 우선.
    #   1) 딜 간 순서: 딜의 가장 임박한 회차 기준 (음수→절대값 작은 것 우선)
    #   2) 딜 내 순서: deal-level 블록 먼저 → 회차 임박도 순 → session_no
    deal_priority = {}
    for g in group_infos:
        did = g["deal_id"] or ""
        g_key = (0 if g["offset"] <= 0 else 1, abs(g["offset"]))
        cur = deal_priority.get(did)
        if cur is None or g_key < cur:
            deal_priority[did] = g_key

    group_infos.sort(
        key=lambda g: (
            deal_priority.get(g["deal_id"] or "", (1, 9999)),  # 딜 묶음
            g["deal_id"] or "",                                 # 같은 우선순위 안정 정렬
            0 if g["is_deal_level"] else 1,                     # 딜 내 deal-level 먼저
            0 if g["offset"] <= 0 else 1,                       # 회차 임박도
            abs(g["offset"]),
            g["session_no"] if isinstance(g["session_no"], int) else 0,
        )
    )

    # v1.1.1: 같은 라벨 패턴인 인접 회차 합치기 (deal-level 그룹은 합치기 대상 아님)
    merged_groups = merge_progress_groups(group_infos)

    # v1.3: 단일 라벨 압축용 라벨 짧은 표현 매핑
    SINGLE_LABEL_SHORT = {
        "📅": "📅 시점 미도래",
        "✅": "✅ 완료 추정",
        "🔴": "🔴 미확보",
        "🟡": "🟡 진행 중",
        "⚪": "⚪ 모호",
    }

    # v1.4 (260427): 딜 단위 H3 헤더 + 넘버링 — 같은 딜의 첫 블록에서만 출력.
    # 회차/딜전체 블록 헤더에선 고객사명 제거 (H3에 이미 노출).
    prev_deal_id = None
    deal_counter = 0

    for g in merged_groups:
        customer = short_customer(g["customer"])
        session_nos = g.get("session_nos_list", [])
        offsets_list = g.get("offsets_list", [g["offset"]])
        items_for_render = g.get("items_merged", g["items"])
        is_deal_level = g.get("is_deal_level", False)
        deal_id = g["deal_id"]

        if deal_id != prev_deal_id:
            deal_counter += 1
            lines.append(f"### {deal_counter}. {customer}")
            lines.append("")
            prev_deal_id = deal_id

        # 헤더 줄 (v1.4: 고객사명은 H3 헤더에서 출력 — 회차 블록에선 생략)
        if is_deal_level:
            header = "**_(딜 전체 — 회차 무관)_**"
        elif g["is_unresolved"]:
            header = "**_(세부 회차 미확정)_**"
        else:
            sess_label = format_session_range_v2(session_nos)
            d_day_str = format_d_day_range(offsets_list)
            if sess_label:
                header = f"**{sess_label}**  {d_day_str}"
            else:
                header = f"**{customer}**  {d_day_str}"

        # 라벨별 + 체크포인트별 데이터 brief 추출 (v1.2)
        by_label_cp_brief = defaultdict(dict)
        by_label_cp_reason = defaultdict(dict)
        cp_to_phase_order = {}
        by_label_cp_polished = defaultdict(dict)
        by_label_cp_id = defaultdict(dict)  # v1.5: cp_label → cp_id (manual_only 처리용)
        by_label_cp_llm_reason = defaultdict(dict)  # v1.7: LLM이 부여한 사유 — polished 자리로 우선 사용
        # 헤더는 단일 라벨 압축 여부에 따라 다르게 출력하므로 일단 출력 보류

        for c in items_for_render:
            label = c.get("label", "⚪")
            cell = c.get("cell") or {}
            cp_label = cell.get("checkpoint_label", "")
            if not cp_label:
                continue
            phase_order = cell.get("phase_order")
            if phase_order is not None:
                cp_to_phase_order[cp_label] = phase_order

            key = (cell.get("deal_id"), cell.get("checkpoint_id"), cell.get("session_no"))
            evidence = evidence_index.get(key, {})
            brief = format_evidence_brief(evidence, label)
            polished = c.get("polished_brief")  # v1.2.2: LLM polish 결과
            llm_reason_val = c.get("llm_reason")  # v1.7: LLM 분류 단계 사유
            auto_reason = c.get("auto_reason")
            cp_id = cell.get("checkpoint_id", "")

            # 같은 cp_label에 이미 brief 있으면 더 풍부한 것 유지
            existing_brief = by_label_cp_brief[label].get(cp_label)
            if brief and (not existing_brief or len(brief) > len(existing_brief)):
                by_label_cp_brief[label][cp_label] = brief
                by_label_cp_reason[label][cp_label] = auto_reason
            elif cp_label not in by_label_cp_brief[label]:
                by_label_cp_brief[label][cp_label] = None
                by_label_cp_reason[label][cp_label] = auto_reason
            by_label_cp_id[label][cp_label] = cp_id
            # v1.6 (260427): manual override 셀은 polished 자리에 갱신 메시지 강제
            if auto_reason == "manual_override":
                manual_reason = c.get("manual_override_reason") or "자연어 피드백"
                manual_at = (c.get("manual_override_at") or "")[:10]
                msg = f"수동 갱신 ({manual_at}) — {manual_reason}" if manual_at else f"수동 갱신 — {manual_reason}"
                by_label_cp_polished[label][cp_label] = msg
            # polished brief는 가장 먼저 들어온 것 유지 (LLM 응답이라 신뢰도 동일)
            elif polished and cp_label not in by_label_cp_polished[label]:
                by_label_cp_polished[label][cp_label] = polished
            # v1.7: llm_reason 별도 누적 — polished 우선, 없으면 fallback으로 사용
            if llm_reason_val and cp_label not in by_label_cp_llm_reason[label]:
                by_label_cp_llm_reason[label][cp_label] = llm_reason_val

        # v1.4 (260427): 단일 라벨 블록 압축 — 📅(시점 미도래)에만 적용.
        #   다른 라벨(🟡·✅·🔴·⚪)은 brief 데이터가 있어야 하므로 일반 출력으로 fallback.
        active_labels = [l for l, _ in PROGRESS_LABEL_ORDER if by_label_cp_brief.get(l)]
        if len(active_labels) == 1 and active_labels[0] == "📅":
            only_label = "📅"
            cp_dict = by_label_cp_brief[only_label]
            cp_count = len(cp_dict)
            label_short = SINGLE_LABEL_SHORT.get(only_label, only_label)
            # cp 1~3건이면 cp 이름 표시, 4건 이상은 건수만 (📅는 데이터 없음 — cp 이름만으로 충분)
            if cp_count <= 3:
                sorted_cps = sorted(
                    cp_dict.keys(),
                    key=lambda cp: (cp_to_phase_order.get(cp, 99), cp)
                )
                cps_text = " · ".join(sorted_cps)
                lines.append(f"{header} — {label_short}: {cps_text}")
            else:
                lines.append(f"{header} — {label_short} (전체 {cp_count}건)")
            lines.append("")
            continue

        # 일반 출력 (라벨 다중)
        lines.append(header)
        for label, prefix in PROGRESS_LABEL_ORDER:
            cp_dict = by_label_cp_brief.get(label, {})
            if not cp_dict:
                continue
            lines.append(f"  {prefix}")
            sorted_cps = sorted(
                cp_dict.keys(),
                key=lambda cp: (cp_to_phase_order.get(cp, 99), cp)
            )
            for cp_label in sorted_cps:
                polished = by_label_cp_polished[label].get(cp_label)
                brief = cp_dict[cp_label]
                reason = by_label_cp_reason[label].get(cp_label)
                cp_id = by_label_cp_id[label].get(cp_label)
                llm_reason = by_label_cp_llm_reason[label].get(cp_label)
                # v1.5: tax_invoice 🔴은 polished/brief 무시하고 manual 안내 강제
                if cp_id == "tax_invoice" and label == "🔴":
                    meta = render_cell_meta(label, brief, reason, cp_id)
                else:
                    # v1.7: 우선순위 — polished > llm_reason > evidence brief
                    if polished:
                        meta = polished
                    elif llm_reason:
                        meta = shorten_text(llm_reason, 120)
                    else:
                        meta = render_cell_meta(label, brief, reason, cp_id)
                # v1.2.3: 자동 ✅(local+successor) 신뢰도 꼬리표
                if reason == "local_and_successor_evidence" and meta:
                    meta = f"{meta} _(후속 단계 추정)_"
                # v1.3: meta 비어 있으면 콜론·메타 생략 (cp_label만)
                if meta:
                    lines.append(f"    · {cp_label}: {meta}")
                else:
                    lines.append(f"    · {cp_label}")
        lines.append("")

    return "\n".join(lines).rstrip()


def render_cell_meta(label, brief, auto_reason, cp_id=None):
    """라벨·brief·auto_reason → 한 줄 표시 텍스트.

    v1.3 (260427): 📅 메타 제거 (cp_label만으로 충분, 중복 표기 제거)
    v1.5 (260427): tax_invoice는 LD 메일함에서 자동 검출 어려움 (회계팀·OM 처리 영역) →
                   🔴 라벨일 때 manual 안내 메시지로 교체. 자연어 피드백으로 갱신 권장.
    """
    if label == "📅":
        return ""  # cp_label 자체로 "시점 미도래" 명확
    if cp_id == "tax_invoice" and label == "🔴":
        return "자동 검출 불가 — OM·재무팀 확인 후 자연어 피드백으로 갱신"
    if label == "🔴" and auto_reason == "no_evidence_no_successor":
        return "본 셀·후속 단계 모두 증거 없음 (실제 미확보 추정)"
    if label == "✅" and auto_reason == "successor_evidence":
        return "본 셀 직접 증거 없음, 후속 단계 증거 기반 추정"
    if brief:
        return brief
    return ""


# ── 일정 미확정 + 푸터 ──────────────────────────────────────────────


def render_no_schedule_section(deals_no_schedule):
    if not deals_no_schedule:
        return None
    lines = ["## ⚠️ 일정 미확정", ""]
    for d in deals_no_schedule:
        customer = short_customer(d.get("customer", ""))
        deal_name = d.get("deal_name", "") or "(딜명 미상)"
        reason = d.get("reason", "일정 정보 없음")
        lines.append(f"- **{customer} · {deal_name}** — {reason}")
    return "\n".join(lines)


def render_footer():
    now = datetime.now().strftime("%H:%M")
    return f'_자연어로 수정 가능 (예: "Customer D 거래명세서 완료됐어") · 생성 {now} · v1.7_'


def build_unresolved_deal_ids(sessions_data):
    """multi_unresolved 플래그가 있는 딜 id 집합 (세부 회차 미확정)."""
    out = set()
    if not sessions_data:
        return out
    for deal_id, deal in (sessions_data.get("deals_with_schedule") or {}).items():
        for s in deal.get("sessions", []) or []:
            if s.get("warning_flag") == "multi_unresolved":
                out.add(deal_id)
                break
    return out


def render_report(classified_data, checkpoints_data, sessions_data, today, evidence_data=None):
    cells = classified_data.get("classified_cells", [])
    apply_pending_fallback(cells)
    cells = apply_stale_filter(cells, checkpoints_data, today)

    deals_no = []
    if sessions_data and isinstance(sessions_data, dict):
        deals_no = sessions_data.get("deals_no_schedule", []) or []
    unresolved = build_unresolved_deal_ids(sessions_data)

    # v1.2: evidence index 미리 빌드 (셀 단위 brief 생성용)
    evidence_index = build_evidence_index(evidence_data) if evidence_data else {}

    # v1.1.1: 데이터 꼬임 딜 미리 식별 (즉각·확인 통합 처리용)
    data_mismatch = build_data_mismatch_deal_ids(cells)

    parts = [render_header(today), ""]

    # 1) 🚨 즉각 해결 필요
    parts.append(
        render_alert_section(cells, "🚨", "즉각 해결 필요", "즉각해결", unresolved, data_mismatch)
    )
    parts.append("")
    parts.append(SEPARATOR)
    parts.append("")

    # 2) ⚠️ 확인 필요 (데이터 꼬임 딜의 즉각해결 셀까지 통합)
    parts.append(
        render_alert_section(cells, "⚠️", "확인 필요", "확인필요", unresolved, data_mismatch)
    )
    parts.append("")
    parts.append(SEPARATOR)
    parts.append("")

    # 3) 📋 진행 현황 (옵션 A 그룹화 + 데이터 표시 v1.2)
    parts.append(render_progress_section(cells, sessions_data, today, unresolved, evidence_index))
    parts.append("")

    # 4) ⚠️ 일정 미확정 (조건부)
    no_sched = render_no_schedule_section(deals_no)
    if no_sched:
        parts.append(SEPARATOR)
        parts.append("")
        parts.append(no_sched)
        parts.append("")

    parts.append(render_footer())
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Step 4: 리포트 렌더러 (v1.2, 4구역+옵션A+데이터)")
    parser.add_argument("--classified", required=True)
    parser.add_argument("--checkpoints", required=True, help="config/checkpoints.json")
    parser.add_argument("--sessions", default=None)
    parser.add_argument("--evidence", default=None, help="runtime/evidence.json (v1.2: 진행 현황 brief 표시용)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--today", default=None)
    args = parser.parse_args()

    classified = load_json(args.classified)
    checkpoints = load_json(args.checkpoints)
    sessions = load_json(args.sessions) if args.sessions else {}
    evidence = load_json(args.evidence) if args.evidence else {}

    if args.today:
        today = datetime.strptime(args.today, "%Y-%m-%d").date()
    else:
        today = date.today()

    md = render_report(classified, checkpoints, sessions, today, evidence_data=evidence)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"[OK] 리포트 생성 완료 → {os.path.abspath(args.out)}")
    print(f"     파일 크기: {len(md):,} 문자")


if __name__ == "__main__":
    main()
