"""
Step 4.6 — 출력 포맷 검증 (ops-plugin v1.1, 260427 분류 프레임 재설계)

generate_ops_md.py가 생성한 MD가 v1.1 포맷 규약을 지켰는지 검증.
실패 시 재생성 권고 (strict 모드에서는 exit 1).

v1.1 검증 항목:
  1. 헤더 존재 (📋 **YYYY-MM-DD(요일) 수주 과정 운영 현황**)
  2. 4구역 헤더 존재
     - ## 🚨 즉각 해결 필요
     - ## ⚠️ 확인 필요
     - ## 📋 진행 현황
     ※ ## ⚠️ 일정 미확정은 조건부 — 검증 안 함
  3. 푸터 v1.1 + 생성 시각 포함
  4. 진짜 v0.5 유물 이모지 차단 (🔥 ⏰ 🏆 🎯)
     ※ 🚨·⚠️는 v1.1 정식 알림 헤더 이모지 — 금지 X
  5. PENDING 누출 차단 (LLM 호출 미완 fallback 검증)
  6. D-day 형식 (D-N / D+N / D-0)
  7. 라벨 이모지 화이트리스트 (✅ 🟡 🔴 ⚪ 📅)

입력:
  --md      outputs/ops_report_YYYYMMDD.md
  [--strict] 오류 있으면 exit 1

v1.0 (2026-04-24)
v1.1 (2026-04-27): 4구역 헤더 검증 + 📅 라벨 추가 + 🚨·⚠️ 정식 이모지로 등록.
"""

import argparse
import re
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


ALLOWED_LABEL_EMOJIS = ["✅", "🟡", "🔴", "⚪", "📅"]
FORBIDDEN_EMOJIS_V05 = ["🔥", "⏰", "🏆", "🎯"]  # 진짜 v0.5 유물만
REQUIRED_SECTION_HEADERS = [
    "## 🚨 즉각 해결 필요",
    "## ⚠️ 확인 필요",
    "## 📋 진행 현황",
]


def verify(md_content):
    errors = []
    warnings = []

    # 1. 헤더 존재
    if "📋 **" not in md_content or "수주 과정 운영 현황" not in md_content:
        errors.append("헤더 누락: '📋 **YYYY-MM-DD(요일) 수주 과정 운영 현황**' 패턴 없음")

    # 2. 4구역 헤더 존재 (일정 미확정은 조건부라 검증 X)
    for header in REQUIRED_SECTION_HEADERS:
        if header not in md_content:
            errors.append(f"필수 섹션 누락: '{header}'")

    # 3. 푸터 (v1.1 ~ v1.6 — 점진 증분, 신규 추가 시 여기 갱신)
    if not any(v in md_content for v in ("v1.1", "v1.2", "v1.3", "v1.4", "v1.5", "v1.6")):
        errors.append("푸터에 버전 'v1.1'~'v1.6' 누락")
    if "생성" not in md_content:
        warnings.append("푸터에 '생성 HH:MM' 시각 표시 의심")

    # 4. v0.5 유물 이모지 차단
    for emoji in FORBIDDEN_EMOJIS_V05:
        if emoji in md_content:
            errors.append(f"v0.5 유물 이모지 발견 — 제거 필요: {emoji}")

    # 5. PENDING 상태 누출 감지
    if "PENDING" in md_content:
        errors.append(
            "MD에 'PENDING' 문자열 발견 — LLM 호출 미완료 상태가 그대로 렌더됨. "
            "generate_ops_md.py의 apply_pending_fallback()가 작동했는지 확인"
        )

    # 6. D-day 형식 체크
    d_day_pattern = re.findall(r"D[-+]\d+", md_content)
    if not d_day_pattern:
        warnings.append(
            "D-day 표기 발견 안 됨 — 리포트가 비어있거나 미생성 가능성"
        )

    # 7. 의심 상태 이모지 (테이블 셀에서 화이트리스트 외 라벨)
    state_row_pattern = re.compile(r"\|\s*(🟢|🔵|🟠|🟣|⚫)\s*[^|]*\|")
    misuse = state_row_pattern.findall(md_content)
    if misuse:
        warnings.append(
            f"의심 상태 이모지 테이블 셀에 발견: {set(misuse)}. "
            f"화이트리스트만 쓰세요: {ALLOWED_LABEL_EMOJIS}"
        )

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Step 4.6: 출력 포맷 검증 (v1.1, 4구역+5분류)")
    parser.add_argument("--md", required=True, help="검증할 MD 파일 경로")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="오류 1건이라도 있으면 exit 1",
    )
    args = parser.parse_args()

    try:
        with open(args.md, "r", encoding="utf-8") as f:
            md_content = f.read()
    except FileNotFoundError:
        print(f"[ERROR] MD 파일 없음: {args.md}", file=sys.stderr)
        sys.exit(1)

    errors, warnings = verify(md_content)

    if errors:
        print(f"[FAIL] 검증 실패 ({len(errors)}건)")
        for e in errors:
            print(f"  ❌ {e}")
    if warnings:
        print(f"[WARN] 경고 ({len(warnings)}건)")
        for w in warnings:
            print(f"  ⚠️  {w}")

    if not errors and not warnings:
        print("[OK] 모든 검증 통과")
    elif not errors:
        print(f"[OK] 필수 검증 통과 (경고 {len(warnings)}건)")

    if errors and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
