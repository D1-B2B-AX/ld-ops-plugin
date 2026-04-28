"""
Phase 0: 환경 확인 스크립트 (ld-plugins core — deal·ops 공용)
MCP 연결 상태를 사전 검증하고, 세일즈맵 DB를 urllib로 자동 다운로드한다.

기존 `deal-priority-plugin`과의 차이:
- `gh release download` → `urllib.request.urlretrieve` 교체 (gh CLI 의존성 제거)
- settings.json의 `data_sources.salesmap_download_url` 참조

사용법:
  python scripts/check_env.py [--settings config/settings.json]

출력: JSON 형태로 각 MCP 상태 반환
  { "salesmap": {...}, "workspace_mcp": {...}, "slack": {...}, "all_critical_ok": true }
"""

import json
import os
import sys
import io
import urllib.request
import urllib.error

# Windows cp949 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


# ── 기본값 (settings 미제공 시) ──
DEFAULT_SALESMAP_DB_PATH = os.path.expanduser("~/salesmap/salesmap_latest.db")
DEFAULT_SALESMAP_URL = (
    "https://github.com/sabinanfranz/data_analysis_ai/releases/download/"
    "salesmap-db-latest/salesmap_latest.db"
)


def load_settings(path):
    """settings.json 로드 — 없으면 기본값 사용"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"settings 로드 실패, 기본값 사용: {e}", file=sys.stderr)
        return {}


def get_db_path(settings):
    path = settings.get("data_sources", {}).get("salesmap_db_path", DEFAULT_SALESMAP_DB_PATH)
    return os.path.expanduser(path)


def get_download_url(settings):
    return settings.get("data_sources", {}).get("salesmap_download_url", DEFAULT_SALESMAP_URL)


_last_progress_pct = -1


def download_progress(block_num, block_size, total_size):
    """다운로드 진행률 표시 (stderr). 2% 단위로만 출력해 로그 스팸 방지."""
    global _last_progress_pct
    if total_size <= 0:
        return
    downloaded = block_num * block_size
    pct = min(100, int(downloaded * 100 / total_size))
    # 직전 출력과 2% 이상 차이나거나 100%일 때만 찍음
    if pct != 100 and pct - _last_progress_pct < 2:
        return
    _last_progress_pct = pct
    mb_done = downloaded / (1024 * 1024)
    mb_total = total_size / (1024 * 1024)
    sys.stderr.write(f"\r    다운로드 중: {pct:3d}% ({mb_done:.0f}/{mb_total:.0f}MB)")
    sys.stderr.flush()
    if pct >= 100:
        sys.stderr.write("\n")


def check_and_update_salesmap_db(settings):
    """세일즈맵 DB 파일이 오늘 날짜인지 확인, 아니면 urllib로 자동 다운로드"""
    from datetime import datetime, date

    db_path = get_db_path(settings)
    db_url = get_download_url(settings)
    db_dir = os.path.dirname(db_path)

    # 폴더 없으면 생성
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    # DB 파일 존재 + 날짜 확인
    if os.path.exists(db_path):
        mtime = datetime.fromtimestamp(os.path.getmtime(db_path)).date()
        today = date.today()
        if mtime >= today:
            return {
                "status": True,
                "path": db_path,
                "db_date": str(mtime),
                "updated": False,
                "message": "DB가 오늘 날짜입니다"
            }

    # urllib로 자동 다운로드
    print(f"  DB 갱신 필요 — GitHub에서 최신 DB 다운로드 중...", file=sys.stderr)
    print(f"    URL: {db_url}", file=sys.stderr)
    try:
        urllib.request.urlretrieve(db_url, db_path, reporthook=download_progress)

        if os.path.exists(db_path):
            new_mtime = datetime.fromtimestamp(os.path.getmtime(db_path)).date()
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            print(f"  DB 다운로드 완료: {size_mb:.0f}MB ({new_mtime})", file=sys.stderr)
            return {
                "status": True,
                "path": db_path,
                "db_date": str(new_mtime),
                "updated": True,
                "size_mb": round(size_mb),
                "message": f"GitHub에서 최신 DB 다운로드 완료 ({size_mb:.0f}MB)"
            }
        else:
            return {
                "status": False,
                "path": None,
                "updated": False,
                "error": "다운로드 후 파일이 존재하지 않음",
                "message": "수동 다운로드 필요",
                "manual_url": db_url
            }

    except urllib.error.URLError as e:
        return {
            "status": True if os.path.exists(db_path) else False,
            "path": db_path if os.path.exists(db_path) else None,
            "updated": False,
            "error": f"네트워크 오류: {e}",
            "message": "이전 DB로 계속 진행" if os.path.exists(db_path) else "DB 없음 — 수동 다운로드 필요",
            "manual_url": db_url
        }
    except Exception as e:
        return {
            "status": True if os.path.exists(db_path) else False,
            "path": db_path if os.path.exists(db_path) else None,
            "updated": False,
            "error": f"다운로드 오류: {e}",
            "message": "이전 DB로 계속 진행" if os.path.exists(db_path) else "DB 없음",
            "manual_url": db_url
        }


def check_workspace_mcp():
    """workspace-mcp (구글 서비스) 인증 상태 확인"""
    import glob

    cred_dir = os.path.expanduser("~/.google_workspace_mcp/credentials/")
    if not os.path.exists(cred_dir):
        return {
            "status": False,
            "error": "credentials 폴더 없음 — workspace-mcp 미설치 또는 인증 미완료",
            "action": "workspace-mcp 설치 후 구글 OAuth 인증 필요"
        }

    cred_files = glob.glob(os.path.join(cred_dir, "*.json"))
    if not cred_files:
        return {
            "status": False,
            "error": "인증 토큰 파일 없음",
            "action": "구글 OAuth 인증 필요"
        }

    # 토큰 만료 여부 확인
    from datetime import datetime
    results = []
    for cred_file in cred_files:
        try:
            with open(cred_file, "r", encoding="utf-8") as f:
                cred = json.load(f)

            email = os.path.basename(cred_file).replace(".json", "")
            expiry_str = cred.get("expiry", "")
            has_refresh = bool(cred.get("refresh_token"))

            if expiry_str:
                expiry = datetime.fromisoformat(expiry_str)
                is_expired = expiry < datetime.now()
            else:
                is_expired = None

            results.append({
                "email": email,
                "has_refresh_token": has_refresh,
                "access_token_expired": is_expired,
                "status": True if has_refresh else False,
                "note": "refresh_token 있음 — 자동 갱신 가능" if has_refresh else "refresh_token 없음 — 재인증 필요"
            })
        except Exception as e:
            results.append({"file": cred_file, "status": False, "error": str(e)})

    all_ok = all(r.get("status") for r in results)
    return {"status": all_ok, "accounts": results}


def check_slack():
    """Slack MCP 연결 확인 — claude.ai 커넥터 기반"""
    return {
        "status": "unknown",
        "message": "Slack MCP는 Claude Code 내에서 도구 호출로 확인 필요",
        "action": "스킬 3(슬랙) 실행 시 자동 확인됨"
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Phase 0: 환경 확인")
    parser.add_argument("--settings", default=None, help="settings.json 경로 (선택)")
    args = parser.parse_args()

    settings = load_settings(args.settings)

    print("=" * 50)
    print("Phase 0: 환경 확인 (ld-plugins core)")
    print("=" * 50)

    results = {
        "salesmap": check_and_update_salesmap_db(settings),
        "workspace_mcp": check_workspace_mcp(),
        "slack": check_slack(),
    }

    # 전체 상태 판정
    critical_ok = True
    if results["salesmap"].get("status") == False:
        critical_ok = False
    if results["workspace_mcp"].get("status") == False:
        critical_ok = False

    results["all_critical_ok"] = critical_ok

    # 사람이 읽기 쉬운 요약
    print()
    print(f"  세일즈맵 DB: {'OK' if results['salesmap'].get('status') != False else 'FAIL'}")
    print(f"  workspace-mcp: {'OK' if results['workspace_mcp'].get('status') != False else 'FAIL'}")
    print(f"  Slack MCP: 실행 시 확인")
    print()

    if not critical_ok:
        print("  [FAIL] 필수 환경 미충족 — 아래 조치 후 재실행하세요:")
        if results["salesmap"].get("status") == False:
            print(f"    - 세일즈맵: {results['salesmap'].get('error', '')}")
            if results["salesmap"].get("manual_url"):
                print(f"      수동 다운로드: {results['salesmap']['manual_url']}")
        if results["workspace_mcp"].get("status") == False:
            print(f"    - workspace-mcp: {results['workspace_mcp'].get('action', '')}")
        print()
    else:
        print("  [OK] 필수 환경 확인 완료 — Phase 1 진행 가능")

    # JSON 출력
    print()
    print("--- JSON ---")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))

    return 0 if critical_ok else 1


if __name__ == "__main__":
    sys.exit(main())
