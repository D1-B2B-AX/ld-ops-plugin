"""
Step 0.5 — 일정 조립 레이어 (ops-plugin v1.1, 260427)

4소스(세일즈맵·캘린더·드라이브·슬랙) + 슬랙 운영 요청 thread 병합.
v1.1: --slack-ops-requests 인자 추가 — 운영 요청 채널 정형 thread에서 차수 분해 추출.
       Customer F·Customer E 같이 다차수지만 드라이브 분해 정보 부족한 케이스 보강.

처리 단계:
  1. 세일즈맵 edu_start~edu_end 기간으로 유형 1차 판정
     단차수(≤7일) / 모호(7~14일) / 다차수(>14일) / 미상(공백)
  2. 유형별 병합 규칙으로 3소스(세일즈맵·캘린더·드라이브) 결정론 조립
  3. 슬랙 일정 관련 메시지 필터 + LLM 프롬프트 생성 (PENDING)
     → 오케스트레이터가 LLM 호출 후 sessions.json 직접 업데이트
  4. 4소스 다 공백이면 deals_no_schedule 분리

충돌 해결 원칙:
  - 기본 권위: 세일즈맵·캘린더 (공식 소스)
  - 단, 특이 건(변경 감지 시) 드라이브·슬랙이 최신 우선
  - LD마다 주력 소스 다르므로 4소스 모두 수집·병합

입력:
  --salesmap  runtime/s1_deals.json
  --calendar  runtime/s2_calendar.json
  --drive     runtime/s5_drive.json
  --slack     runtime/s3_slack.json
  --out       runtime/sessions.json

출력 구조:
  {
    "deals_with_schedule": {
      "<deal_id>": {
        "deal_name": "...",
        "customer": "...",
        "course_id": "...",
        "session_type": "single" | "multi" | "ambiguous" | "unknown",
        "confidence": "high" | "medium" | "low",
        "sessions": [
          {"session_no": 1, "edu_start": "YYYY-MM-DD", "edu_end": "...", "source": ["salesmap", "calendar"]}
        ],
        "warnings": []
      }
    },
    "deals_no_schedule": [...],
    "pending_slack_updates": [
      {
        "deal_id": "...",
        "deal_name": "...",
        "candidates": [{date, channel, text}],
        "llm_prompt": "...",
        "status": "PENDING"
      }
    ],
    "meta": {...}
  }

오케스트레이터 후속 처리:
  pending_slack_updates 각 항목의 llm_prompt를 Claude에 전달 →
  {"sessions": [...], "no_schedule_info": bool} 응답 받아
  sessions.json의 해당 deal 세션에 병합/덮어쓰기.

v1.0 (2026-04-24)
"""

import argparse
import json
import os
import re
import sys
import io
from datetime import datetime, date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


SINGLE_MAX_DAYS = 7
MULTI_MIN_DAYS = 15  # >14 = multi

SLACK_SCHEDULE_KEYWORDS = [
    "일정", "변경", "미뤄", "미뤄짐", "연기", "앞당김",
    "갱신", "취소", "리스케줄", "옮김", "바뀜", "조정",
]
DATE_PATTERNS = [
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}",
    r"\d{1,2}월\s*\d{1,2}일",
    r"\d{1,2}/\d{1,2}",
]


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_to_dict(data):
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {item.get("deal_id"): item for item in data if isinstance(item, dict) and item.get("deal_id")}
    return {}


def parse_date(s):
    if not s:
        return None
    # UTC ISO 문자열(예: "2025-12-21T15:00:00.000Z") → KST 변환
    if isinstance(s, str) and "T" in s and s.endswith("Z"):
        try:
            dt_utc = datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
            kst_date = (dt_utc.astimezone() if dt_utc.tzinfo else dt_utc).date()
            # UTC+9 강제 변환 (시스템 타임존 의존 제거)
            from datetime import timedelta
            kst = dt_utc + timedelta(hours=9)
            return kst.date()
        except ValueError:
            pass
    s = str(s)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def date_str(s):
    """UTC ISO 문자열이면 KST로 변환 후 YYYY-MM-DD 반환."""
    if not s:
        return None
    if isinstance(s, str) and "T" in s and s.endswith("Z"):
        d = parse_date(s)
        return d.isoformat() if d else None
    return str(s)[:10]


def judge_session_type(sm_start, sm_end):
    """세일즈맵 기간 기반 유형 판정."""
    if not sm_start:
        return "unknown"
    start = parse_date(sm_start)
    end = parse_date(sm_end) if sm_end else None
    if start is None:
        return "unknown"
    if end is None:
        return "ambiguous"
    duration = (end - start).days
    if duration <= SINGLE_MAX_DAYS:
        return "single"
    elif duration >= MULTI_MIN_DAYS:
        return "multi"
    else:
        return "ambiguous"


def extract_calendar_events(cal_entry):
    """캘린더 entry에서 {edu_start, edu_end} 리스트 추출 + 연속 날짜 병합.

    스킬 2는 이벤트를 '날짜별로 분리' 반환 (예: LG 3일 교육 → 3개 이벤트).
    연속된 날짜(하루 차이)는 같은 회차로 보고 1개 세션으로 병합.
    """
    if not isinstance(cal_entry, dict):
        return []
    events = cal_entry.get("matched_events") or cal_entry.get("events") or []
    result = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        start = ev.get("start") or ev.get("edu_start") or ev.get("date")
        if not start:
            continue
        start = date_str(start)
        end = date_str(ev.get("end") or ev.get("edu_end") or start)
        result.append({"edu_start": start, "edu_end": end})

    # 연속 날짜 병합: 정렬 후 이전 세션 edu_end + 1일 = 현재 edu_start 이면 합치기
    if not result:
        return []
    result.sort(key=lambda e: e["edu_start"])
    merged = [dict(result[0])]
    for ev in result[1:]:
        prev = merged[-1]
        prev_end = parse_date(prev["edu_end"])
        curr_start = parse_date(ev["edu_start"])
        if (
            prev_end is not None
            and curr_start is not None
            and (curr_start - prev_end).days <= 1
        ):
            # 연속 → 병합 (edu_end 확장)
            prev["edu_end"] = ev["edu_end"]
        else:
            merged.append(dict(ev))
    return merged


def extract_notion_events(notion_entry):
    """노션 entry에서 {edu_start, edu_end} 리스트 추출.
    parse_notion_csv.py 출력 구조: {deal_id: {notion_events: [{edu_start, edu_end, title, ...}]}}
    """
    if not isinstance(notion_entry, dict):
        return []
    events = notion_entry.get("notion_events") or []
    result = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        start = ev.get("edu_start")
        if not start:
            continue
        end = ev.get("edu_end") or start
        result.append({"edu_start": start, "edu_end": end})
    return sorted(result, key=lambda e: e["edu_start"])


def extract_drive_schedule(drive_entry):
    """드라이브 planning_sheet.parsed.session_schedule 추출."""
    if not isinstance(drive_entry, dict):
        return []
    ps = drive_entry.get("planning_sheet")
    if not ps or not isinstance(ps, dict):
        return []
    parsed = ps.get("parsed")
    if not parsed or not isinstance(parsed, dict):
        return []
    schedule = parsed.get("session_schedule", [])
    result = []
    for s in schedule:
        if not isinstance(s, dict):
            continue
        d = s.get("date") or s.get("edu_start")
        if not d:
            continue
        result.append({"edu_start": date_str(d), "edu_end": date_str(d)})
    return sorted(result, key=lambda s: s["edu_start"])


def compose_single(deal, calendar_events, notion_events, drive_schedule):
    """단차수 조립: 세일즈맵·캘린더 최상위 → 노션 → 드라이브 순 보강."""
    sm_start = date_str(deal.get("edu_start"))
    sm_end = date_str(deal.get("edu_end")) or sm_start

    session = {
        "session_no": 1,
        "edu_start": sm_start,
        "edu_end": sm_end,
        "source": ["salesmap"] if sm_start else [],
    }
    warnings = []

    # 1순위: 캘린더 보강·override (세일즈맵과 다르면 캘린더 우선 — 최신 공식)
    for ev in calendar_events:
        if ev["edu_start"] == session["edu_start"]:
            session["source"].append("calendar")
        else:
            warnings.append(
                f"세일즈맵({session['edu_start']}) ↔ 캘린더({ev['edu_start']}) 날짜 불일치 — 캘린더 우선 적용"
            )
            session["edu_start"] = ev["edu_start"]
            session["edu_end"] = ev["edu_end"]
            session["source"] = ["calendar"]
            break

    # 2순위: 노션 — 같은 날짜면 소스 추가. 세일즈맵·캘린더 둘 다 없을 때만 override
    if not session["source"]:
        if notion_events:
            session["edu_start"] = notion_events[0]["edu_start"]
            session["edu_end"] = notion_events[0]["edu_end"]
            session["source"] = ["notion"]
    else:
        for ev in notion_events:
            if ev["edu_start"] == session["edu_start"]:
                session["source"].append("notion")
                break

    # 3순위: 드라이브 — 같은 날짜면 소스 추가 (override 안 함)
    for s in drive_schedule:
        if s["edu_start"] == session["edu_start"]:
            session["source"].append("drive")
            break

    session["source"] = sorted(list(set(session["source"])))
    if not session["edu_start"]:
        return [], warnings + ["세일즈맵·캘린더·노션·드라이브 모두 공백"]
    return [session], warnings


def compose_multi(deal, calendar_events, notion_events, drive_schedule):
    """다차수 조립: 캘린더+노션 합쳐 회차 세트 구성. 드라이브 보강."""
    warnings = []

    # 캘린더 + 노션 병합 (같은 날짜는 소스만 합침)
    combined = {}
    for ev in calendar_events:
        k = ev["edu_start"]
        combined.setdefault(k, {"edu_start": ev["edu_start"], "edu_end": ev["edu_end"], "sources": set()})
        combined[k]["sources"].add("calendar")
    for ev in notion_events:
        k = ev["edu_start"]
        combined.setdefault(k, {"edu_start": ev["edu_start"], "edu_end": ev["edu_end"], "sources": set()})
        combined[k]["sources"].add("notion")

    if len(combined) >= 2:
        sessions = []
        drive_dates = {s["edu_start"] for s in drive_schedule}
        for idx, key in enumerate(sorted(combined.keys())):
            entry = combined[key]
            sources = list(entry["sources"])
            if key in drive_dates:
                sources.append("drive")
            sessions.append(
                {
                    "session_no": idx + 1,
                    "edu_start": entry["edu_start"],
                    "edu_end": entry["edu_end"],
                    "source": sorted(sources),
                }
            )
        # 세일즈맵 기간 범위 검증
        sm_start = parse_date(deal.get("edu_start"))
        sm_end = parse_date(deal.get("edu_end"))
        if sm_start and sm_end:
            for s in sessions:
                sd = parse_date(s["edu_start"])
                if sd and (sd < sm_start or sd > sm_end):
                    warnings.append(f"회차 {s['session_no']} 날짜 {s['edu_start']}가 세일즈맵 기간 범위 밖")
        return sessions, warnings

    # 캘린더+노션 부족 → 드라이브로 분해 시도
    if drive_schedule:
        sessions = []
        for idx, s in enumerate(drive_schedule):
            sessions.append(
                {
                    "session_no": idx + 1,
                    "edu_start": s["edu_start"],
                    "edu_end": s["edu_end"],
                    "source": ["drive"],
                }
            )
        warnings.append("캘린더·노션 부족 — 드라이브 session_schedule로 회차 분해")
        return sessions, warnings

    # 분해 불가 — 세일즈맵 전체 기간 1세션 + 경고
    sm_start = date_str(deal.get("edu_start"))
    sm_end = date_str(deal.get("edu_end"))
    warnings.append("다차수 유형이나 세부 회차 정보 없음 — 세일즈맵 기간 전체를 1세션으로 처리")
    return [
        {
            "session_no": 1,
            "edu_start": sm_start,
            "edu_end": sm_end,
            "source": ["salesmap"] if sm_start else [],
            "warning_flag": "multi_unresolved",
        }
    ], warnings


def compose_ambiguous(deal, calendar_events, notion_events, drive_schedule):
    """모호 유형: 캘린더·노션 이벤트 합계 개수로 재판정."""
    total_cal_notion = len(calendar_events) + len(notion_events)
    if total_cal_notion >= 2:
        return compose_multi(deal, calendar_events, notion_events, drive_schedule)
    return compose_single(deal, calendar_events, notion_events, drive_schedule)


def compose_unknown(deal, calendar_events, notion_events, drive_schedule):
    """세일즈맵 공백: 캘린더+노션 → 드라이브 순 복원."""
    warnings = []

    # 캘린더+노션 병합
    combined = {}
    for ev in calendar_events:
        combined.setdefault(ev["edu_start"], {"edu_start": ev["edu_start"], "edu_end": ev["edu_end"], "sources": set()})["sources"].add("calendar")
    for ev in notion_events:
        combined.setdefault(ev["edu_start"], {"edu_start": ev["edu_start"], "edu_end": ev["edu_end"], "sources": set()})["sources"].add("notion")

    if combined:
        warnings.append("세일즈맵 edu_start 공백 — 캘린더+노션으로 복원")
        sessions = []
        drive_dates = {s["edu_start"] for s in drive_schedule}
        for idx, key in enumerate(sorted(combined.keys())):
            entry = combined[key]
            sources = list(entry["sources"])
            if key in drive_dates:
                sources.append("drive")
            sessions.append(
                {
                    "session_no": idx + 1,
                    "edu_start": entry["edu_start"],
                    "edu_end": entry["edu_end"],
                    "source": sorted(sources),
                }
            )
        return sessions, warnings

    if drive_schedule:
        warnings.append("세일즈맵·캘린더·노션 공백 — 드라이브 session_schedule로 복원")
        sessions = []
        for idx, s in enumerate(drive_schedule):
            sessions.append(
                {
                    "session_no": idx + 1,
                    "edu_start": s["edu_start"],
                    "edu_end": s["edu_end"],
                    "source": ["drive"],
                }
            )
        return sessions, warnings

    return [], warnings


def judge_confidence(sessions):
    if not sessions:
        return "none"
    avg = sum(len(s.get("source", [])) for s in sessions) / len(sessions)
    if avg >= 2.0:
        return "high"
    if avg >= 1.0:
        return "medium"
    return "low"


def detect_slack_schedule_candidates(slack_entry):
    """슬랙 entry에서 일정 관련 메시지 후보 추출."""
    if not isinstance(slack_entry, dict):
        return []
    messages = slack_entry.get("slack_results") or slack_entry.get("messages") or []
    candidates = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = m.get("message_preview") or m.get("text") or m.get("message") or ""
        text = str(text)
        if not text:
            continue
        has_kw = any(kw in text for kw in SLACK_SCHEDULE_KEYWORDS)
        has_date = any(re.search(p, text) for p in DATE_PATTERNS)
        if has_kw and has_date:
            candidates.append(
                {
                    "date": m.get("date") or m.get("ts"),
                    "channel": m.get("channel"),
                    "text": text[:300],
                }
            )
    return candidates


def build_slack_llm_prompt(deal_name, customer, candidates):
    """슬랙 일정 관련 메시지 → LLM 날짜 추출 프롬프트."""
    lines = [
        f"교육: {customer} / {deal_name}",
        "",
        "아래는 슬랙에서 수집한 일정 변경·공지 관련 메시지입니다.",
        "이 메시지에 포함된 교육 일정 정보(회차별 날짜)를 추출하시오.",
        "",
    ]
    for idx, c in enumerate(candidates, 1):
        lines.append(f"{idx}. ({c.get('date', '')}) {c.get('text', '')}")
    lines.extend(
        [
            "",
            "응답은 반드시 아래 JSON 한 줄 형식:",
            '{"sessions": [{"edu_start": "YYYY-MM-DD", "edu_end": "YYYY-MM-DD", "note": "1회차 연기 등"}], "no_schedule_info": false}',
            '일정 정보 없으면: {"sessions": [], "no_schedule_info": true}',
            "자유 서술·해석 금지. JSON만 출력.",
        ]
    )
    return "\n".join(lines)


# ============================================================
# v1.1 (260427): 운영 요청 채널 thread 정형 차수 추출
# ============================================================

# Customer F형: "N차 M/D(요일)~M/D(요일)[, M/D~M/D]"
OPS_REQ_LOTTE_PATTERN = re.compile(
    r"(?P<sno>\d+)차\s+"
    r"(?P<a_m>\d+)/(?P<a_d>\d+)(?:\([월화수목금토일]\))?"
    r"(?:\s*[~∼\-]\s*(?:(?P<b_m>\d+)/)?(?P<b_d>\d+)(?:\([월화수목금토일]\))?)?"
    r"(?:\s*,\s*(?P<c_m>\d+)/(?P<c_d>\d+)(?:\([월화수목금토일]\))?"
    r"(?:\s*[~∼\-]\s*(?:(?P<d_m>\d+)/)?(?P<d_d>\d+)(?:\([월화수목금토일]\))?)?)?"
)

# Customer E형: "M월 D, D, D" / "M월 D~D" — 라인 단위
OPS_REQ_HL_LINE_PATTERN = re.compile(
    r"(\d+)월\s*([\d,\s~∼\-]+)"
)


def _normalize_year(month, base_year):
    """1~3월이면 base_year+1, 그 외는 base_year. (오늘 4/27 기준 1~3월은 다음 해 추정)"""
    if 1 <= month <= 3 and base_year >= 2026:
        # 4월 이후 데이터에서 1~3월 등장하면 다음 해
        return base_year + 1
    return base_year


def _parse_lotte_pattern(text, base_year):
    """Customer F형 'N차 M/D~M/D' 패턴에서 sessions[] 추출."""
    sessions = []
    for m in OPS_REQ_LOTTE_PATTERN.finditer(text):
        try:
            sno = int(m.group("sno"))
            a_m, a_d = int(m.group("a_m")), int(m.group("a_d"))
            b_m_raw, b_d_raw = m.group("b_m"), m.group("b_d")
            c_m_raw, c_d_raw = m.group("c_m"), m.group("c_d")
            d_m_raw, d_d_raw = m.group("d_m"), m.group("d_d")

            start_y = _normalize_year(a_m, base_year)
            start = f"{start_y:04d}-{a_m:02d}-{a_d:02d}"

            # 종료 결정: 두 번째 묶음 있으면 그 종료, 아니면 첫 묶음 종료
            if c_d_raw:
                end_m = int(d_m_raw or c_m_raw)
                end_d = int(d_d_raw or c_d_raw)
            elif b_d_raw:
                end_m = int(b_m_raw or a_m)
                end_d = int(b_d_raw)
            else:
                end_m, end_d = a_m, a_d

            end_y = _normalize_year(end_m, base_year)
            end = f"{end_y:04d}-{end_m:02d}-{end_d:02d}"

            sessions.append({
                "session_no": sno,
                "edu_start": start,
                "edu_end": end,
                "source": ["slack_ops_request"],
                "raw_match": m.group(0),
            })
        except (TypeError, ValueError):
            continue
    return sessions


def _parse_hl_pattern(text, base_year):
    """Customer E형 'M월 D, D, D' 또는 'M월 D~D' 라인 단위 추출.

    각 날짜를 1세션으로 변환 (session_no 자동 증가).
    트랙 라벨(기초·심화)은 무시하고 모든 날짜를 통합.
    v1.1.1 (260427): 한 라인 안 여러 'M월 ...' 매칭 위해 finditer 사용.
    """
    sessions = []
    sno_counter = 1
    for line in text.splitlines():
        # 한 라인 안에 여러 'M월 ...' 가능 (예: "4월 6, 8 / 5월 11, 12")
        for m in OPS_REQ_HL_LINE_PATTERN.finditer(line):
            month = int(m.group(1))
            days_part = m.group(2)
            # 콤마로 분리하면 각 항목이 단일 일자 또는 범위
            items = [s.strip() for s in days_part.split(",") if s.strip()]
            for item in items:
                if "~" in item or "∼" in item or "-" in item:
                    parts = re.split(r"[~∼\-]", item)
                    if len(parts) >= 2:
                        try:
                            start_d = int(parts[0].strip())
                            end_d = int(parts[1].strip())
                            y = _normalize_year(month, base_year)
                            sessions.append({
                                "session_no": sno_counter,
                                "edu_start": f"{y:04d}-{month:02d}-{start_d:02d}",
                                "edu_end": f"{y:04d}-{month:02d}-{end_d:02d}",
                                "source": ["slack_ops_request"],
                                "raw_match": f"{month}월 {start_d}~{end_d}",
                            })
                            sno_counter += 1
                        except (ValueError, IndexError):
                            continue
                else:
                    try:
                        d = int(item)
                        y = _normalize_year(month, base_year)
                        sessions.append({
                            "session_no": sno_counter,
                            "edu_start": f"{y:04d}-{month:02d}-{d:02d}",
                            "edu_end": f"{y:04d}-{month:02d}-{d:02d}",
                            "source": ["slack_ops_request"],
                            "raw_match": f"{month}월 {d}",
                        })
                        sno_counter += 1
                    except ValueError:
                        continue
    return sessions


def parse_ops_request_threads(ops_requests_data, base_year=None):
    """v1.1: 운영 요청 thread 텍스트에서 차수 분해 추출.

    Returns: {deal_id: [{session_no, edu_start, edu_end, source, raw_match}]}

    추출 우선순위:
      1. Customer F형 'N차 M/D~M/D' — 정형도 최고
      2. Customer E형 'M월 D, D' — 부모/답글 라인 단위
    둘 다 매칭 안 되면 빈 리스트 (드라이브 등 다른 소스로 폴백).
    """
    if base_year is None:
        base_year = datetime.now().year

    out = {}
    for deal_id, deal_info in (ops_requests_data or {}).items():
        if not isinstance(deal_id, str) or deal_id.startswith("_"):
            continue
        if not isinstance(deal_info, dict):
            continue
        threads = deal_info.get("ops_request_threads", []) or []
        all_sessions = []
        for thread in threads:
            text = thread.get("combined_text", "") or ""
            # 1차: Customer F형 우선
            lotte_sessions = _parse_lotte_pattern(text, base_year)
            if lotte_sessions:
                all_sessions.extend(lotte_sessions)
                continue
            # 2차: Customer E형 폴백
            hl_sessions = _parse_hl_pattern(text, base_year)
            if hl_sessions:
                all_sessions.extend(hl_sessions)

        if all_sessions:
            # 중복 제거 (같은 (session_no, edu_start) 1번만)
            seen = set()
            unique = []
            for s in all_sessions:
                key = (s["session_no"], s["edu_start"])
                if key not in seen:
                    seen.add(key)
                    unique.append(s)
            unique.sort(key=lambda x: (x["edu_start"], x["session_no"]))
            # session_no 재배정 (1부터 순차)
            for i, s in enumerate(unique, start=1):
                s["session_no"] = i
            out[deal_id] = unique
    return out


def compose(salesmap_data, calendar_data, drive_data, slack_data, notion_data=None, ops_requests_data=None):
    deals = salesmap_data.get("deals", []) if isinstance(salesmap_data, dict) else salesmap_data
    if not isinstance(deals, list):
        raise ValueError("세일즈맵 데이터에서 'deals' 리스트를 찾을 수 없습니다")

    cal_dict = normalize_to_dict(calendar_data)
    drv_dict = normalize_to_dict(drive_data)
    slk_dict = normalize_to_dict(slack_data)
    # 노션 데이터는 parse_notion_csv.py 출력 형식 (meta 필드 포함)
    ntn_dict = normalize_to_dict(notion_data) if notion_data else {}
    # v1.1 (260427): 운영 요청 thread 정형 차수 추출
    ops_req_sessions_by_deal = parse_ops_request_threads(ops_requests_data) if ops_requests_data else {}

    deals_with = {}
    deals_no = []
    pending_slack = []

    type_counts = {"single": 0, "multi": 0, "ambiguous": 0, "unknown": 0}

    for deal in deals:
        deal_id = deal.get("deal_id")
        if not deal_id:
            continue

        session_type = judge_session_type(deal.get("edu_start"), deal.get("edu_end"))
        type_counts[session_type] = type_counts.get(session_type, 0) + 1

        cal_events = extract_calendar_events(cal_dict.get(deal_id))
        ntn_events = extract_notion_events(ntn_dict.get(deal_id))
        drv_sched = extract_drive_schedule(drv_dict.get(deal_id))

        # v1.1 (260427): 운영 요청 thread 정형 차수 추출 결과 우선 사용
        # 결과 있으면 multi로 처리하고 드라이브·캘린더 기반 분해 무시 (정형 슬랙이 신뢰도 최고)
        ops_req_sessions = ops_req_sessions_by_deal.get(deal_id, [])

        if ops_req_sessions:
            sessions = ops_req_sessions
            warnings = []
            session_type = "multi"  # ops_req 사용은 multi 강제 (위 카운트는 그대로)
            # 드라이브 분해보다 우선 — 운영 요청 채널이 정형 양식
        elif session_type == "single":
            sessions, warnings = compose_single(deal, cal_events, ntn_events, drv_sched)
        elif session_type == "multi":
            sessions, warnings = compose_multi(deal, cal_events, ntn_events, drv_sched)
        elif session_type == "ambiguous":
            sessions, warnings = compose_ambiguous(deal, cal_events, ntn_events, drv_sched)
        else:  # unknown
            sessions, warnings = compose_unknown(deal, cal_events, ntn_events, drv_sched)

        confidence = judge_confidence(sessions)

        customer = ""
        org = deal.get("organization")
        if isinstance(org, dict):
            customer = org.get("name", "")
        customer = customer or deal.get("organization_name", "")

        record = {
            "deal_name": deal.get("deal_name", ""),
            "customer": customer,
            "course_id": deal.get("course_id"),
            "session_type": session_type,
        }

        # 슬랙 일정 변경 후보 탐지
        slack_candidates = detect_slack_schedule_candidates(slk_dict.get(deal_id))
        if slack_candidates:
            prompt = build_slack_llm_prompt(
                record["deal_name"], customer, slack_candidates
            )
            pending_slack.append(
                {
                    "deal_id": deal_id,
                    "deal_name": record["deal_name"],
                    "customer": customer,
                    "candidates": slack_candidates,
                    "llm_prompt": prompt,
                    "status": "PENDING",
                }
            )

        if not sessions:
            deals_no.append(
                {
                    "deal_id": deal_id,
                    **record,
                    "reason": "4소스(세일즈맵·캘린더·드라이브·슬랙) 모두 공백 또는 일정 정보 없음",
                }
            )
        else:
            deals_with[deal_id] = {
                **record,
                "confidence": confidence,
                "sessions": sessions,
                "warnings": warnings,
            }

    return {
        "deals_with_schedule": deals_with,
        "deals_no_schedule": deals_no,
        "pending_slack_updates": pending_slack,
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "version": "v1.1",
            "ops_requests_used_for": list(ops_req_sessions_by_deal.keys()) if ops_req_sessions_by_deal else [],
            "total_deals": len(deals),
            "with_schedule": len(deals_with),
            "no_schedule": len(deals_no),
            "pending_slack_count": len(pending_slack),
            "session_type_distribution": type_counts,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Step 0.5: 일정 조립 레이어 (v1.1)")
    parser.add_argument("--salesmap", required=True)
    parser.add_argument("--calendar", required=True)
    parser.add_argument("--drive", required=True)
    parser.add_argument("--slack", required=True)
    parser.add_argument("--notion", default=None, help="parse_notion_csv.py 출력 (선택, 2팀 전용)")
    parser.add_argument("--slack-ops-requests", default=None,
                        help="운영 요청 thread 데이터 (선택, v1.1) — runtime/s3_slack_ops_requests.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    salesmap = load_json(args.salesmap)
    if salesmap is None:
        print(f"[ERROR] 세일즈맵 파일 로드 실패: {args.salesmap}", file=sys.stderr)
        sys.exit(1)
    calendar = load_json(args.calendar, default={})
    drive = load_json(args.drive, default={})
    slack = load_json(args.slack, default={})
    notion = load_json(args.notion, default={}) if args.notion else {}
    ops_requests = load_json(args.slack_ops_requests, default={}) if args.slack_ops_requests else {}

    result = compose(salesmap, calendar, drive, slack, notion_data=notion, ops_requests_data=ops_requests)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    meta = result["meta"]
    dist = meta["session_type_distribution"]
    print(f"[OK] 일정 조립 완료 → {os.path.abspath(args.out)}")
    print(
        f"     총 {meta['total_deals']}딜 | 일정 확보 {meta['with_schedule']}건, "
        f"미확정 {meta['no_schedule']}건"
    )
    print(
        f"     유형: 단차수 {dist.get('single', 0)} · 다차수 {dist.get('multi', 0)} · "
        f"모호 {dist.get('ambiguous', 0)} · 미상 {dist.get('unknown', 0)}"
    )
    if meta["pending_slack_count"] > 0:
        print(
            f"     ⚠️ 슬랙 일정 변경 후보 {meta['pending_slack_count']}딜 — "
            f"오케스트레이터 LLM 호출 필요"
        )


if __name__ == "__main__":
    main()
