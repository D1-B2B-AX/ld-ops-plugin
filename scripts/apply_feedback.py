"""
apply_feedback.py — 자연어 피드백 조작 명령을 state 파일에 반영

원칙 1 (최종 판단 = Owner Name) 의 실제 동작부:

  Owner Name 자연어
     ↓
  (LLM 파싱, llm_prompts.md의 feedback_parsing 프롬프트)
     ↓
  조작 명령 JSON 배열
     ↓
  본 스크립트
     ↓
  state/ops_state.json 업데이트
     ↓
  다음 회차 운영일지 MD 렌더 시 자동 반영

입력 (stdin 또는 --commands 파일):
  {
    "confirmation": "이렇게 이해했습니다 — ...",
    "commands": [
      {"operation": "progress_update", "deal_id": "D-001", "payload": {"value": "확정"}},
      {"operation": "todo_done", "deal_id": "D-001", "payload": {"text": "강사 리마인드"}}
    ]
  }
  또는 그냥 commands 배열 하나만도 허용.

지원 operation:

v0.5 유지 (하위호환, 7개):
  - progress_update, action_update, todo_done, todo_add, tag_add, tag_remove, note

v1.0 신규 (260424 셀 매트릭스 전환, 5개):
  - watchlist_exclude   : 딜 추적 제외 (build_matrix가 읽음)
  - watchlist_include   : watchlist 제외 해제
  - deal_flag_set       : deal_flag_overrides 설정 (build_matrix가 읽음)
  - cell_override       : 특정 셀 label 수동 변경 (v1 classify_evidence 연동)
  - schedule_move       : 세션 일정 이동 (v1 compose_schedule 연동)

사용법:
  cat commands.json | python scripts/apply_feedback.py --state state/ops_state.json
  python scripts/apply_feedback.py --state state/ops_state.json --commands commands.json
  python scripts/apply_feedback.py --state state/ops_state.json --commands commands.json --dry-run

v0.1 (2026-04-22)
"""

import argparse
import json
import os
import sys
import io
from datetime import datetime, date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

TODAY = date.today().isoformat()


# ════════════════════════════════════════════
# State 파일 구조 + 로드·저장
# ════════════════════════════════════════════

def default_state():
    """처음 실행 시 state 파일 없을 때 기본 구조 (v2 = 260424 v1.0 전환 반영)."""
    return {
        "version": 2,
        "last_updated": None,
        "deals": {},
        "recently_handled": [],
        "change_log": [],
        # v1.0 신규 (셀 매트릭스 구조) — build_matrix.py가 읽음
        "watchlist_exclusions": [],           # [deal_id] — 추적 제외 딜
        "deal_flag_overrides": {},            # {deal_id: {flag_id: bool}}
        "cell_overrides": [],                 # v1 연동 (classify_evidence가 반영)
        "schedule_overrides": {}              # v1 연동 (compose_schedule이 반영)
    }


def ensure_deal_state(state, deal_id):
    """deal_id에 해당하는 상태 블록이 없으면 생성."""
    if deal_id not in state["deals"]:
        state["deals"][deal_id] = {
            "진행_현황_override": None,
            "태그_override": None,
            "액션_override": None,
            "투두_state": []
        }
    return state["deals"][deal_id]


def load_state(path):
    if not path or not os.path.exists(path):
        return default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        for key, default_val in default_state().items():
            state.setdefault(key, default_val)
        return state
    except Exception as e:
        print(f"state 로드 실패, 기본값으로 시작: {e}", file=sys.stderr)
        return default_state()


def save_state(path, state):
    state["last_updated"] = datetime.now().isoformat(timespec="seconds")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_change(state, operation, deal_id, payload, deal_name=None):
    state["change_log"].append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "operation": operation,
        "deal_id": deal_id,
        "deal_name": deal_name,
        "payload": payload
    })


# ════════════════════════════════════════════
# Operations (7개)
# ════════════════════════════════════════════

def op_progress_update(state, deal_id, payload, deal_name=None):
    ds = ensure_deal_state(state, deal_id)
    new_value = payload.get("value")
    ds["진행_현황_override"] = new_value
    log_change(state, "progress_update", deal_id, payload, deal_name)
    return f"진행 현황 → '{new_value}'"


def op_action_update(state, deal_id, payload, deal_name=None):
    ds = ensure_deal_state(state, deal_id)
    new_value = payload.get("value")
    ds["액션_override"] = new_value
    log_change(state, "action_update", deal_id, payload, deal_name)
    return f"액션 → '{new_value}'"


def op_todo_done(state, deal_id, payload, deal_name=None):
    ds = ensure_deal_state(state, deal_id)
    text = (payload.get("text") or "").strip()
    if not text:
        return "text 비어있음 — 스킵"

    found = False
    for t in ds["투두_state"]:
        if t.get("text") == text:
            t["done"] = True
            t["completed_at"] = TODAY
            found = True
            break
    if not found:
        ds["투두_state"].append({
            "text": text,
            "done": True,
            "completed_at": TODAY
        })

    # recently_handled 자동 이관
    state["recently_handled"].append({
        "deal_id": deal_id,
        "deal_name": deal_name or payload.get("deal_name") or "",
        "item": text,
        "completed_at": TODAY
    })

    log_change(state, "todo_done", deal_id, payload, deal_name)
    return f"투두 완료: '{text}'"


def op_todo_add(state, deal_id, payload, deal_name=None):
    ds = ensure_deal_state(state, deal_id)
    text = (payload.get("text") or "").strip()
    if not text:
        return "text 비어있음 — 스킵"
    if any(t.get("text") == text for t in ds["투두_state"]):
        return f"이미 존재: '{text}' — 스킵"
    ds["투두_state"].append({
        "text": text,
        "done": False,
        "added_at": TODAY
    })
    log_change(state, "todo_add", deal_id, payload, deal_name)
    return f"투두 추가: '{text}'"


def op_tag_add(state, deal_id, payload, deal_name=None):
    ds = ensure_deal_state(state, deal_id)
    tag = (payload.get("tag") or "").strip()
    if not tag:
        return "tag 비어있음 — 스킵"

    current = ds.get("태그_override")
    if current is None:
        base_tags = payload.get("base_tags", [])
        # 중복 제거하며 추가
        new_tags = list(base_tags)
        if tag not in new_tags:
            new_tags.append(tag)
        ds["태그_override"] = new_tags
    else:
        if tag not in current:
            current.append(tag)
    log_change(state, "tag_add", deal_id, payload, deal_name)
    return f"태그 추가: '{tag}'"


def op_tag_remove(state, deal_id, payload, deal_name=None):
    ds = ensure_deal_state(state, deal_id)
    tag = (payload.get("tag") or "").strip()
    if not tag:
        return "tag 비어있음 — 스킵"

    current = ds.get("태그_override")
    if current is None:
        base_tags = payload.get("base_tags", [])
        ds["태그_override"] = [t for t in base_tags if t != tag]
    else:
        ds["태그_override"] = [t for t in current if t != tag]
    log_change(state, "tag_remove", deal_id, payload, deal_name)
    return f"태그 제거: '{tag}'"


def op_note(state, deal_id, payload, deal_name=None):
    """구조 변경 없이 change_log에만 기록."""
    log_change(state, "note", deal_id or "", payload, deal_name)
    return f"메모 기록: '{payload.get('text', '')}'"


def op_watchlist_exclude(state, deal_id, payload, deal_name=None):
    """딜을 추적 제외 대상으로 등록. build_matrix가 skip."""
    excl = state.setdefault("watchlist_exclusions", [])
    if deal_id not in excl:
        excl.append(deal_id)
    log_change(state, "watchlist_exclude", deal_id, payload, deal_name)
    return f"watchlist 제외 등록: {deal_id}"


def op_watchlist_include(state, deal_id, payload, deal_name=None):
    """watchlist 제외 해제."""
    excl = state.setdefault("watchlist_exclusions", [])
    if deal_id in excl:
        excl.remove(deal_id)
    log_change(state, "watchlist_include", deal_id, payload, deal_name)
    return f"watchlist 제외 해제: {deal_id}"


def op_deal_flag_set(state, deal_id, payload, deal_name=None):
    """딜별 조건부 체크포인트 플래그 on/off."""
    flag_id = payload.get("flag_id")
    value = payload.get("value")
    if not flag_id:
        return "flag_id 누락 — 스킵"
    overrides = state.setdefault("deal_flag_overrides", {})
    overrides.setdefault(deal_id, {})[flag_id] = bool(value)
    log_change(state, "deal_flag_set", deal_id, payload, deal_name)
    return f"{deal_id}.{flag_id} = {bool(value)}"


def op_cell_override(state, deal_id, payload, deal_name=None):
    """특정 셀 label 수동 override (v1 연동 — classify_evidence가 반영)."""
    entry = {
        "deal_id": deal_id,
        "session_no": payload.get("session_no"),
        "checkpoint_id": payload.get("checkpoint_id"),
        "label": payload.get("label"),
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "reason": payload.get("reason"),
    }
    state.setdefault("cell_overrides", []).append(entry)
    log_change(state, "cell_override", deal_id, payload, deal_name)
    return f"{deal_id}/{payload.get('checkpoint_id')} → {payload.get('label')}"


def op_schedule_move(state, deal_id, payload, deal_name=None):
    """세션 일정 이동 (v1 연동 — compose_schedule이 반영)."""
    sessions = payload.get("sessions")
    if not sessions:
        return "sessions 누락 — 스킵"
    overrides = state.setdefault("schedule_overrides", {})
    overrides[deal_id] = {"sessions": sessions}
    log_change(state, "schedule_move", deal_id, payload, deal_name)
    return f"{deal_id} 일정 {len(sessions)}세션 override"


OPERATIONS = {
    # v0.5 기존 유지 (5항목 체계, 하위호환)
    "progress_update": op_progress_update,
    "action_update": op_action_update,
    "todo_done": op_todo_done,
    "todo_add": op_todo_add,
    "tag_add": op_tag_add,
    "tag_remove": op_tag_remove,
    "note": op_note,
    # v1.0 신규 (셀 매트릭스 구조, 260424)
    "watchlist_exclude": op_watchlist_exclude,
    "watchlist_include": op_watchlist_include,
    "deal_flag_set": op_deal_flag_set,
    "cell_override": op_cell_override,
    "schedule_move": op_schedule_move,
}


# ════════════════════════════════════════════
# Apply
# ════════════════════════════════════════════

def apply_commands(state, commands):
    results = []
    for cmd in commands:
        if not isinstance(cmd, dict):
            results.append({"status": "skip", "reason": "잘못된 명령 형식"})
            continue

        op_name = cmd.get("operation")
        deal_id = cmd.get("deal_id")
        deal_name = cmd.get("deal_name")
        payload = cmd.get("payload") or {}

        if not op_name:
            results.append({"status": "skip", "reason": "operation 누락"})
            continue
        if op_name not in OPERATIONS:
            results.append({
                "status": "skip",
                "reason": f"알 수 없는 operation: {op_name}"
            })
            continue
        if not deal_id and op_name != "note":
            results.append({
                "status": "skip",
                "operation": op_name,
                "reason": "deal_id 누락"
            })
            continue

        try:
            msg = OPERATIONS[op_name](state, deal_id, payload, deal_name)
            results.append({
                "status": "ok",
                "operation": op_name,
                "deal_id": deal_id,
                "message": msg
            })
        except Exception as e:
            results.append({
                "status": "error",
                "operation": op_name,
                "deal_id": deal_id,
                "error": str(e)
            })

    return results


# ════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="자연어 피드백 조작 명령을 state 파일에 반영 (원칙 1)"
    )
    parser.add_argument("--state", required=True, help="state 파일 경로")
    parser.add_argument("--commands", default=None,
                       help="명령 JSON 경로 (없으면 stdin)")
    parser.add_argument("--dry-run", action="store_true",
                       help="state 저장 없이 결과만 출력 (미리보기)")
    args = parser.parse_args()

    # 명령 로드
    if args.commands:
        with open(args.commands, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = json.load(sys.stdin)

    # "commands" 래퍼 또는 직접 배열 모두 허용
    if isinstance(raw, dict):
        commands = raw.get("commands", [])
        confirmation = raw.get("confirmation")
    elif isinstance(raw, list):
        commands = raw
        confirmation = None
    else:
        print("입력이 배열 또는 {commands: [...]} 형태여야 합니다.", file=sys.stderr)
        sys.exit(1)

    # state 로드 + apply
    state = load_state(args.state)
    results = apply_commands(state, commands)

    # 요약
    ok_count = sum(1 for r in results if r["status"] == "ok")
    skip_count = sum(1 for r in results if r["status"] == "skip")
    err_count = sum(1 for r in results if r["status"] == "error")

    if confirmation:
        print(f"📝 해석: {confirmation}", file=sys.stderr)

    print(
        f"피드백 적용: 총 {len(results)}건 "
        f"(성공 {ok_count} / 스킵 {skip_count} / 오류 {err_count})",
        file=sys.stderr
    )
    for r in results:
        if r["status"] == "ok":
            print(f"  ✓ [{r['operation']}] {r.get('deal_id','')}: {r['message']}",
                  file=sys.stderr)
        elif r["status"] == "skip":
            print(f"  - 스킵: {r.get('reason','')}", file=sys.stderr)
        else:
            print(f"  ✗ 오류: {r.get('error','')}", file=sys.stderr)

    # 저장
    if not args.dry_run:
        save_state(args.state, state)
        print(f"→ state 저장: {args.state}", file=sys.stderr)
    else:
        print("(dry-run — state 저장 안 함)", file=sys.stderr)

    # stdout JSON 결과 (다운스트림 용)
    print(json.dumps({
        "applied": ok_count,
        "skipped": skip_count,
        "errors": err_count,
        "results": results
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
