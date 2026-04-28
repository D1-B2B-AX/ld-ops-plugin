"""
Step 0.3 — 노션 강의 캘린더 CSV 파서 (ops-plugin v1.0, 260424 추가)

노션 [2팀-OM] 강의 캘린더 DB 내보내기 CSV → runtime/s6_notion.json 변환.
integration 권한 MCP 우회 MVP — 사용자가 노션 페이지 → 내보내기(Markdown&CSV) → 지정 경로 저장 → 본 스크립트 실행.

입력:
  --csv      노션 내보내기 CSV 파일 경로 (예: runtime/notion_calendar.csv)
  --salesmap runtime/s1_deals.json (세일즈맵 딜 매칭 기준)
  --settings config/settings.json (owner.notion_name_aliases + notion_csv_columns)
  --out      runtime/s6_notion.json

이름 매칭 (노션 '기획' 컬럼 ↔ owner):
  settings.owner.notion_name_aliases 순회 + 느슨한 매칭
  (공백 제거 후 동일 / 글자 집합 포함)

날짜 파싱 (3종 포맷):
  1. "YYYY년 MM월 DD일" 단일
  2. "YYYY년 MM월 DD일 → YYYY년 MM월 DD일" 범위
  3. "YYYY년 MM월 DD일 오전 HH:MM (GMT+9) → ..." 시간포함
  → 모두 YYYY-MM-DD 부분만 추출

딜 매칭 (노션 row ↔ 세일즈맵 딜):
  - '기업명' ↔ deal.organization.name: 공백 제거 후 한쪽이 다른쪽 포함
  - 'Name' ↔ deal.deal_name: 토큰 교집합 ≥1
  두 조건 모두 만족 시 매칭.

v1.0 (2026-04-24)
"""

import argparse
import csv
import json
import os
import re
import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


DATE_PATTERN = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_notion_date(s):
    """'2026년 5월 15일' 또는 '... → ...' 또는 시간 포함 → (edu_start, edu_end).

    반환: (YYYY-MM-DD, YYYY-MM-DD) tuple. 파싱 실패 시 (None, None).
    """
    if not s:
        return None, None
    matches = DATE_PATTERN.findall(str(s))
    if not matches:
        return None, None
    parsed = []
    for y, m, d in matches:
        try:
            dt = datetime(int(y), int(m), int(d)).date()
            parsed.append(dt.isoformat())
        except ValueError:
            continue
    if not parsed:
        return None, None
    return parsed[0], parsed[-1]


def normalize_for_match(s):
    """공백·특수문자 제거, 소문자화 (영문 포함)."""
    if not s:
        return ""
    return re.sub(r"[\s\-·_()]+", "", str(s)).lower()


def is_owner_row(planner_value, aliases):
    """'기획' 컬럼 값이 owner aliases 중 하나와 매칭."""
    if not planner_value:
        return False
    planner_norm = normalize_for_match(planner_value)
    for alias in aliases:
        alias_norm = normalize_for_match(alias)
        if not alias_norm:
            continue
        if planner_norm == alias_norm:
            return True
        # 느슨한 매칭: alias 글자 전부 planner에 포함 (순서 무관)
        if len(alias_norm) <= len(planner_norm) and all(c in planner_norm for c in alias_norm):
            return True
    return False


def tokenize(text):
    """한글·영문 토큰 분리. 공백·`_`·`-`·`·`·괄호 경계."""
    if not text:
        return set()
    tokens = re.split(r"[\s_\-·\(\)\[\],]+", str(text))
    return {t for t in tokens if t.strip()}


def match_deal(notion_row, deal, col_customer, col_name):
    """노션 row와 세일즈맵 딜이 매칭되는지 판정."""
    notion_org = normalize_for_match(notion_row.get(col_customer, ""))
    deal_org_raw = ""
    if isinstance(deal.get("organization"), dict):
        deal_org_raw = deal["organization"].get("name", "")
    deal_org = normalize_for_match(deal_org_raw or deal.get("organization_name", ""))

    if not notion_org or not deal_org:
        return False

    # 기업명 부분 포함 매칭
    if notion_org not in deal_org and deal_org not in notion_org:
        return False

    # Name 토큰 매칭 (공백 제거 후 토큰 집합)
    notion_tokens = tokenize(notion_row.get(col_name, ""))
    deal_tokens = tokenize(deal.get("deal_name", ""))
    if not notion_tokens or not deal_tokens:
        # 기업명 매칭만으로는 너무 느슨 — 딜 이름 정보 부족 시 포기
        return False

    # 공통 토큰 1개 이상
    common = {normalize_for_match(t) for t in notion_tokens} & {normalize_for_match(t) for t in deal_tokens}
    common.discard("")
    return len(common) > 0


def parse(csv_path, salesmap_path, settings_path):
    settings = load_json(settings_path)
    owner = settings.get("owner", {})
    aliases = owner.get("notion_name_aliases") or [owner.get("name", "")]
    cols = settings.get("data_sources", {}).get("notion_csv_columns") or {}
    col_name = cols.get("name", "Name")
    col_date = cols.get("date", "강의관리 일정")
    col_customer = cols.get("customer", "기업명")
    col_planner = cols.get("planner", "기획")

    salesmap = load_json(salesmap_path)
    deals = salesmap.get("deals", []) if isinstance(salesmap, dict) else salesmap

    # 노션 CSV 로드 — utf-8-sig로 BOM 자동 제거 (노션 내보내기 기본값)
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        notion_rows = list(reader)

    result = {}
    stats = {
        "csv_rows_total": len(notion_rows),
        "owner_matched_rows": 0,
        "deal_matched_events": 0,
        "date_parse_failed": 0,
    }

    # 1. owner 기획 row만 필터
    owner_rows = []
    for row in notion_rows:
        if is_owner_row(row.get(col_planner, ""), aliases):
            owner_rows.append(row)
    stats["owner_matched_rows"] = len(owner_rows)

    # 2. 각 row를 딜과 매칭
    for row in owner_rows:
        edu_start, edu_end = parse_notion_date(row.get(col_date, ""))
        if edu_start is None:
            stats["date_parse_failed"] += 1
            continue

        event = {
            "edu_start": edu_start,
            "edu_end": edu_end,
            "title": row.get(col_name, "").strip(),
            "customer": row.get(col_customer, "").strip(),
            "raw_date": row.get(col_date, "").strip(),
            "planner": row.get(col_planner, "").strip(),
        }

        matched_any = False
        for deal in deals:
            deal_id = deal.get("deal_id")
            if not deal_id:
                continue
            if match_deal(row, deal, col_customer, col_name):
                if deal_id not in result:
                    result[deal_id] = {
                        "deal_id": deal_id,
                        "deal_name": deal.get("deal_name", ""),
                        "notion_events": [],
                    }
                result[deal_id]["notion_events"].append(event)
                matched_any = True
                stats["deal_matched_events"] += 1

        # 매칭 안된 노션 이벤트도 별도 버킷에 기록 (참고용)
        if not matched_any:
            result.setdefault("_unmatched", {"events": []})["events"].append(event)

    return {
        **result,
        "_meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "version": "v1.0",
            "csv_path": os.path.abspath(csv_path),
            "owner_aliases": aliases,
            **stats,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="노션 강의 캘린더 CSV → s6_notion.json (v1.0)")
    parser.add_argument("--csv", required=True, help="노션 내보내기 CSV 경로")
    parser.add_argument("--salesmap", required=True, help="세일즈맵 s1_deals.json 경로")
    parser.add_argument("--settings", required=True, help="config/settings.json 경로")
    parser.add_argument("--out", required=True, help="출력 s6_notion.json 경로")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"[ERROR] CSV 없음: {args.csv}", file=sys.stderr)
        sys.exit(1)

    result = parse(args.csv, args.salesmap, args.settings)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    m = result["_meta"]
    print(f"[OK] 노션 CSV 파싱 완료 → {os.path.abspath(args.out)}")
    print(
        f"     전체 row {m['csv_rows_total']} | owner 매칭 {m['owner_matched_rows']} | "
        f"딜 매칭 이벤트 {m['deal_matched_events']}"
    )
    if m["date_parse_failed"]:
        print(f"     ⚠️ 날짜 파싱 실패 {m['date_parse_failed']}건")


if __name__ == "__main__":
    main()
