#!/usr/bin/env python3
"""노션 변경 감시 → 바뀌면 바로 배포 트리거.

1분마다 노션 검색 API에 "최근 수정순" 1회만 물어본다(전체 동기화 대비 API 1/50).
연결된 페이지(공통·프리미엄반 등) 중 마지막 수정 시각이 이전보다 새로우면 GitHub 배포를 실행한다.
실제 동기화·빌드는 GitHub Actions가 하므로 이 스크립트는 가볍다.

사용:
  python3 watch.py          # 한 번 확인 (launchd가 1분마다 실행)
  python3 watch.py --loop   # 포그라운드에서 계속 확인 (Ctrl+C로 종료)
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import sync

REPO = "nossssi/autoworker-wiki"
STATE = sync.CACHE / "watch_state.json"
GH = "/opt/homebrew/bin/gh"


def latest_edit(n):
    d = n.req("POST", "/search", body={"sort": {"direction": "descending", "timestamp": "last_edited_time"},
                                       "page_size": 5})
    times = [r["last_edited_time"] for r in d.get("results", []) if r.get("last_edited_time")]
    return max(times) if times else None


def run_active():
    out = subprocess.run([GH, "run", "list", "-R", REPO, "--workflow", "deploy.yml", "--limit", "5",
                          "--json", "status"], capture_output=True, text=True).stdout
    return any(r["status"] in ("queued", "in_progress", "waiting", "pending", "requested")
               for r in json.loads(out or "[]"))


def check(n):
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    latest = latest_edit(n)
    if not latest:
        return
    now = datetime.now(timezone.utc)
    edited = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    # 수정 시각이 분 단위라 같은 분 안의 연속 수정을 놓치지 않도록, 최근 2분 수정은 한 번 더 배포
    changed = latest != state.get("seen")
    recent = now - edited <= timedelta(minutes=2)
    pending = state.get("pending", False) or changed or (recent and state.get("deployed_for") != f"{latest}#2")
    if pending and not run_active():
        subprocess.run([GH, "workflow", "run", "deploy.yml", "-R", REPO, "--ref", "main"],
                       check=True, capture_output=True)
        print(f"{now.astimezone():%m-%d %H:%M} 노션 변경({edited.astimezone():%H:%M}) → 배포 시작", flush=True)
        state["deployed_for"] = f"{latest}#2" if (recent and not changed) else latest
        pending = False
    state.update(seen=latest, pending=pending)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def main():
    sync.load_env()
    import os
    n = sync.Notion(os.environ["NOTION_TOKEN"].strip())
    if "--loop" not in sys.argv:
        check(n)
        return
    print("노션 변경 감시 시작 (Ctrl+C로 종료)", flush=True)
    while True:
        try:
            check(n)
        except Exception as e:  # 네트워크 오류 등 — 다음 회차에 재시도
            print(time.strftime("%H:%M"), "확인 실패:", str(e)[:100], flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
