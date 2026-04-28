"""
md_to_slack.py — GitHub-flavored MD를 Slack mrkdwn 호환 포맷으로 변환

변환 규칙:
- **bold** → *bold*           (슬랙은 단일 별표)
- # ~ ###### 헤더 → *제목*    (슬랙은 헤더 미지원)
- --- ━━ 구분선 → 빈 줄
- | col | 테이블 → 불릿 리스트
- [text](url) → <url|text>    (슬랙 링크 포맷)
- `code` → `code`             (그대로 유지, 슬랙 호환)

사용법:
  python scripts/md_to_slack.py input.md [-o output.slack.txt]
  python scripts/md_to_slack.py input.md  # 자동으로 .slack.txt 생성

출력: 슬랙 DM에 복붙하면 제대로 렌더되는 텍스트
"""

import argparse
import os
import re
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def convert_tables(text):
    """MD 테이블(`| col | col |` + `|---|---|`)을 불릿 리스트로 변환."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 테이블 시작: `|` 로 시작하고 다음 줄이 구분선(`|---|---|`)
        if line.strip().startswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r"^\|[\s\-\|:]+\|?$", next_line):
                # 테이블 감지됨 — 헤더 + 데이터 행
                headers = [c.strip() for c in line.strip().strip("|").split("|")]
                i += 2  # 구분선 스킵
                rows = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    rows.append(cells)
                    i += 1
                # 헤더·데이터 불릿으로 변환
                for row in rows:
                    # 행을 "헤더: 값" 형태로 엮기
                    parts = []
                    for h, v in zip(headers, row):
                        if v and v != "-":
                            parts.append(f"*{h}:* {v}" if h else v)
                    out.append("  • " + " · ".join(parts))
                out.append("")
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def md_to_slack(md_text):
    """MD 전체를 슬랙 mrkdwn으로 변환."""
    out = md_text

    # 1) MD 테이블 → 불릿 리스트 (반드시 먼저, 다른 변환 전)
    out = convert_tables(out)

    # 2) 헤더 # ~ ###### → *bold* (공백 한 줄 추가)
    out = re.sub(
        r"^#{1,6}\s+(.+?)$",
        r"*\1*",
        out,
        flags=re.MULTILINE,
    )

    # 3) **bold** → *bold*
    out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", out)

    # 4) MD 링크 [text](url) → 슬랙 링크 <url|text>
    out = re.sub(r"\[([^\]]+?)\]\(([^)]+?)\)", r"<\2|\1>", out)

    # 5) 구분선 (━ ─ - 3자 이상) → 빈 줄
    out = re.sub(r"^[━─\-]{3,}$", "", out, flags=re.MULTILINE)

    # 6) 인용 > 는 그대로 유지 (슬랙 호환)

    # 7) 연속 빈 줄 2개로 제한
    out = re.sub(r"\n{3,}", "\n\n", out)

    return out.strip() + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="MD → Slack mrkdwn 변환 (슬랙 DM 복붙용)"
    )
    parser.add_argument("input", help="입력 MD 파일 경로")
    parser.add_argument("-o", "--output", default=None,
                       help="출력 파일 경로 (기본: input.slack.txt)")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"파일 없음: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "r", encoding="utf-8") as f:
        md = f.read()

    slack = md_to_slack(md)

    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        # 기본값: input_path에서 .md → .slack.txt
        base, _ = os.path.splitext(args.input)
        output_path = os.path.abspath(base + ".slack.txt")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(slack)

    print(f"✅ 슬랙 변환 완료", file=sys.stderr)
    print(f"📄 입력:  {os.path.abspath(args.input)}", file=sys.stderr)
    print(f"💬 출력:  {output_path}", file=sys.stderr)
    # stdout: 출력 경로 (호출자 용)
    print(output_path)


if __name__ == "__main__":
    main()
