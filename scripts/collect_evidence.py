"""
Step 2 — 증거 수집 (ops-plugin v1.0, 260424 전환)

각 도래 셀마다 해당 체크포인트의 evidence_keywords로
Step 0에서 수집된 슬랙·메일·드라이브 데이터를 필터링해
증거 스니펫 추출.

전제:
  Step 0에서 이미 'Owner Name 필터 + 고객사/과정명'으로 좁혀 수집됨.
  이 스크립트는 해당 데이터에 체크포인트별 키워드 추가 필터만.

입력:
  --cells       runtime/arriving_cells.json
  --checkpoints config/checkpoints.json
  --slack       runtime/s3_slack.json
  --gmail       runtime/s4_gmail.json
  --drive       runtime/s5_drive.json
  --out         runtime/evidence.json

출력:
  {
    "evidence_per_cell": [
      {
        "cell": {deal_id, checkpoint_id, d_day, severity, ...},
        "evidence": {
          "slack": [{snippet, date, channel, author, link}, ...],
          "gmail": [{snippet, subject, date, link}, ...],
          "drive": [{file_name, tab_name, type, link, snippet}, ...]
        },
        "evidence_count": {slack: N, gmail: N, drive: N, total: N}
      }
    ],
    "meta": {...}
  }

v1.0 (2026-04-24)
v1.1 (2026-04-27): ops 슬랙 채널 P1/P2/P3 우선순위 반영. settings 받아 채널→priority 매핑 후
                   evidence에 channel_priority 필드 추가, P1→P2→P3 정렬 + 카운트 분해.
"""

import argparse
import json
import os
import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_to_dict(data):
    """리스트/딕트를 {deal_id: entry} 형태로 정규화."""
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {item.get("deal_id"): item for item in data if isinstance(item, dict) and item.get("deal_id")}
    return {}


def keyword_match(text, keywords):
    """텍스트에 키워드 하나라도 포함되면 True (대소문자 무시)."""
    if not text or not keywords:
        return False
    lower = str(text).lower()
    return any(str(kw).lower() in lower for kw in keywords)


PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2, "conditional": 3, "unknown": 4}


def build_channel_priority_map(settings, channel_set="ops"):
    """
    settings에서 채널→우선순위 매핑 dict 생성 (ops 모드).
    예: {"b2b_2팀_운영요청": "P1", "b2b_2팀_skillmatch": "P2", ...}
    팀 채널이 공통보다 우선 (같은 채널이 양쪽에 있으면 팀이 덮어씀).
    """
    if channel_set != "ops":
        return {}

    ds = (settings or {}).get("data_sources", {}) or {}
    common = ds.get("slack_ops_common_channels", {}) or {}
    team_channels = ds.get("slack_ops_team_channels", {}) or {}
    owner_team = ((settings or {}).get("owner", {}) or {}).get("team", "")
    team = team_channels.get(owner_team, {}) if isinstance(team_channels, dict) else {}

    priority_map = {}
    for source in (common, team):
        if isinstance(source, dict):
            for p in ("P1", "P2", "P3"):
                for ch in source.get(p, []) or []:
                    priority_map[ch] = p
    return priority_map


def lookup_channel_priority(channel_name, priority_map):
    """채널명에서 우선순위 추출. 매핑 없으면 'unknown'."""
    if not channel_name or not priority_map:
        return "unknown"
    name = str(channel_name).strip().lstrip("#").strip()
    if name.startswith("in:"):
        name = name[3:]
    return priority_map.get(name, "unknown")


def collect_slack_evidence(deal_id, keywords, slack_data, priority_map=None):
    """슬랙 Step 0 수집 결과에서 키워드 매칭 메시지 추출. priority_map: 채널→P1/P2/P3 매핑 (v1.1)."""
    priority_map = priority_map or {}
    entry = slack_data.get(deal_id, {})
    if not isinstance(entry, dict):
        return []
    messages = entry.get("slack_results") or entry.get("messages") or []
    evidence = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = (
            m.get("message_preview")
            or m.get("text")
            or m.get("message")
            or ""
        )
        if keyword_match(text, keywords):
            channel = m.get("channel")
            evidence.append(
                {
                    "snippet": str(text)[:300],
                    "date": m.get("date") or m.get("ts"),
                    "channel": channel,
                    "channel_priority": lookup_channel_priority(channel, priority_map),
                    "author": m.get("author") or m.get("user"),
                    "link": m.get("thread_link") or m.get("link") or m.get("permalink"),
                }
            )
    # summary에서도 키워드 매칭 (보완)
    summary = entry.get("slack_summary") or ""
    if summary and keyword_match(summary, keywords) and not evidence:
        evidence.append({
            "snippet": str(summary)[:300],
            "date": None,
            "channel": None,
            "channel_priority": "unknown",
            "author": None,
            "link": None,
            "source": "summary",
        })
    # 우선순위 정렬: P1 → P2 → P3 → unknown
    evidence.sort(key=lambda e: PRIORITY_ORDER.get(e.get("channel_priority", "unknown"), 99))
    return evidence


def collect_gmail_evidence(deal_id, keywords, gmail_data):
    """메일 Step 0 수집 결과에서 키워드 매칭 스레드 추출."""
    entry = gmail_data.get(deal_id, {})
    if not isinstance(entry, dict):
        return []
    threads = entry.get("thread_summary") or entry.get("threads") or []
    email_parsed = entry.get("email_parsed") or {}

    evidence = []
    for t in threads:
        if not isinstance(t, dict):
            continue
        subject = t.get("subject", "") or ""
        body = t.get("snippet") or ""
        combined = f"{subject} {body}"
        if keyword_match(combined, keywords):
            evidence.append(
                {
                    "snippet": combined[:300],
                    "subject": subject,
                    "date": t.get("last_date") or t.get("date"),
                    "link": t.get("link") or t.get("thread_link"),
                }
            )

    # email_parsed 요약에서도 키워드 매칭
    for field in ("customer_request", "next_action", "situation_summary"):
        val = email_parsed.get(field)
        if val and keyword_match(val, keywords):
            evidence.append(
                {
                    "snippet": str(val)[:300],
                    "subject": f"[파싱 결과] {field}",
                    "date": entry.get("last_received") or entry.get("last_sent"),
                    "link": None,
                    "source": "email_parsed",
                }
            )
            break  # 중복 방지, 하나만

    return evidence


def collect_drive_evidence(deal_id, keywords, drive_data):
    """드라이브 planning_sheet 내용에서 키워드 매칭."""
    entry = drive_data.get(deal_id, {})
    if not isinstance(entry, dict):
        return []
    ps = entry.get("planning_sheet")
    if not ps or not isinstance(ps, dict):
        return []

    file_name = ps.get("file_name", "") or ""
    tabs_read = ps.get("tabs_read", []) or []
    link = ps.get("link")
    evidence = []

    # 파일명 매칭
    if keyword_match(file_name, keywords):
        evidence.append(
            {
                "file_name": file_name,
                "link": link,
                "type": "file_name_match",
            }
        )

    # 탭명 매칭
    for tab in tabs_read:
        if keyword_match(tab, keywords):
            evidence.append(
                {
                    "file_name": file_name,
                    "tab_name": tab,
                    "link": link,
                    "type": "tab_name_match",
                }
            )

    # parsed 내용에서 키워드 매칭 (직렬화 후 검색)
    parsed = ps.get("parsed")
    if parsed and isinstance(parsed, dict):
        try:
            parsed_str = json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):
            parsed_str = ""
        if keyword_match(parsed_str, keywords):
            evidence.append(
                {
                    "file_name": file_name,
                    "link": link,
                    "type": "content_match",
                    "snippet": parsed_str[:300],
                }
            )

    return evidence


def collect(cells_data, checkpoints_data, slack_data, gmail_data, drive_data, settings=None):
    cp_by_id = {cp["id"]: cp for cp in checkpoints_data.get("checkpoints", [])}
    slack_dict = normalize_to_dict(slack_data)
    gmail_dict = normalize_to_dict(gmail_data)
    drive_dict = normalize_to_dict(drive_data)
    priority_map = build_channel_priority_map(settings or {}, channel_set="ops")

    evidence_per_cell = []
    for cell in cells_data.get("arriving_cells", []):
        cp = cp_by_id.get(cell["checkpoint_id"])
        if not cp:
            continue
        kw = cp.get("evidence_keywords", {}) or {}
        deal_id = cell["deal_id"]

        slack_ev = collect_slack_evidence(deal_id, kw.get("slack", []), slack_dict, priority_map)
        gmail_ev = collect_gmail_evidence(deal_id, kw.get("gmail", []), gmail_dict)
        drive_ev = collect_drive_evidence(deal_id, kw.get("drive", []), drive_dict)

        evidence_per_cell.append(
            {
                "cell": cell,
                "evidence": {
                    "slack": slack_ev,
                    "gmail": gmail_ev,
                    "drive": drive_ev,
                },
                "evidence_count": {
                    "slack": len(slack_ev),
                    "slack_p1": sum(1 for e in slack_ev if e.get("channel_priority") == "P1"),
                    "slack_p2": sum(1 for e in slack_ev if e.get("channel_priority") == "P2"),
                    "slack_p3": sum(1 for e in slack_ev if e.get("channel_priority") == "P3"),
                    "gmail": len(gmail_ev),
                    "drive": len(drive_ev),
                    "total": len(slack_ev) + len(gmail_ev) + len(drive_ev),
                },
            }
        )

    return {
        "evidence_per_cell": evidence_per_cell,
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "version": "v1.1",
            "channel_priority_active": bool(priority_map),
            "channel_priority_count": len(priority_map),
            "cells_processed": len(evidence_per_cell),
            "cells_with_evidence": sum(
                1 for e in evidence_per_cell if e["evidence_count"]["total"] > 0
            ),
            "cells_without_evidence": sum(
                1 for e in evidence_per_cell if e["evidence_count"]["total"] == 0
            ),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Step 2: 증거 수집 (v1.1, ops 채널 P1/P2/P3 우선순위)")
    parser.add_argument("--cells", required=True)
    parser.add_argument("--checkpoints", required=True)
    parser.add_argument("--slack", required=True)
    parser.add_argument("--gmail", required=True)
    parser.add_argument("--drive", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--settings", required=False, help="settings.json (ops 슬랙 채널 P1/P2/P3 매핑용, 미지정 시 priority=unknown)")
    args = parser.parse_args()

    cells = load_json(args.cells)
    checkpoints = load_json(args.checkpoints)
    slack = load_json(args.slack, default={})
    gmail = load_json(args.gmail, default={})
    drive = load_json(args.drive, default={})
    settings = load_json(args.settings, default={}) if args.settings else {}

    result = collect(cells, checkpoints, slack, gmail, drive, settings)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    meta = result["meta"]
    print(f"[OK] 증거 수집 완료 → {os.path.abspath(args.out)}")
    print(
        f"     셀 {meta['cells_processed']}건 처리 | "
        f"증거 있음 {meta['cells_with_evidence']}건, 없음 {meta['cells_without_evidence']}건"
        + (f" | P1/P2/P3 우선순위 적용 (채널 {meta.get('channel_priority_count', 0)}개)"
           if meta.get("channel_priority_active") else "")
    )


if __name__ == "__main__":
    main()
