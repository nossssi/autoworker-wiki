#!/usr/bin/env python3
"""노션 변경 감시 → 바뀌면 바로 배포 트리거. (표준 라이브러리만 사용 — 맥 기본 python3로 동작)

1분마다 노션 검색 API에 "최근 수정순" 1회만 물어본다(전체 동기화 대비 API 1/50).
연결된 페이지(공통·프리미엄반 등) 중 마지막 수정 시각이 이전보다 새로우면 GitHub 배포를 실행한다.
실제 동기화·빌드는 GitHub Actions가 하므로 이 스크립트는 0.5초 만에 끝난다.

필요한 값(.env, 이 파일과 같은 폴더):
  NOTION_TOKEN=ntn_...          노션 읽기 전용 통합 토큰
  GH_DISPATCH_TOKEN=github_pat_ 이 저장소 Actions 쓰기 권한만 있는 fine-grained 토큰 (없으면 gh CLI 사용)

사용:
  python3 watch.py          # 한 번 확인 (launchd가 1분마다 실행)
  python3 watch.py --loop   # 포그라운드에서 계속 확인 (Ctrl+C로 종료)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "nossssi/autoworker-wiki"
STATE = HERE / ".cache" / "watch_state.json"
UA = "autoworker-wiki-watch/1.0 (+https://wiki.autoworkers.ai)"  # 기본 파이썬 UA는 Cloudflare가 차단
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers, **{"User-Agent": UA, "Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def latest_edit():
    d = http("POST", "https://api.notion.com/v1/search",
             {"Authorization": "Bearer " + os.environ["NOTION_TOKEN"], "Notion-Version": "2025-09-03"},
             {"sort": {"direction": "descending", "timestamp": "last_edited_time"}, "page_size": 5})
    times = [r["last_edited_time"] for r in d.get("results", []) if r.get("last_edited_time")]
    return max(times) if times else None


class GitHub:
    """GH_DISPATCH_TOKEN이 있으면 REST API, 없으면 gh CLI."""

    def __init__(self):
        self.token = os.environ.get("GH_DISPATCH_TOKEN", "").strip()
        self.h = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json"}

    def run_active(self):
        if self.token:
            d = http("GET", f"https://api.github.com/repos/{REPO}/actions/workflows/deploy.yml/runs?per_page=5", self.h)
            return any(r["status"] in ACTIVE for r in d.get("workflow_runs", []))
        out = subprocess.run(["gh", "run", "list", "-R", REPO, "--workflow", "deploy.yml", "--limit", "5",
                              "--json", "status"], capture_output=True, text=True).stdout
        return any(r["status"] in ACTIVE for r in json.loads(out or "[]"))

    def dispatch(self):
        if self.token:
            http("POST", f"https://api.github.com/repos/{REPO}/actions/workflows/deploy.yml/dispatches",
                 self.h, {"ref": "main"})
        else:
            subprocess.run(["gh", "workflow", "run", "deploy.yml", "-R", REPO, "--ref", "main"],
                           check=True, capture_output=True)


def check(gh):
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    latest = latest_edit()
    if not latest:
        return
    now = datetime.now(timezone.utc)
    edited = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    # 수정 시각이 분 단위라 같은 분 안의 연속 수정을 놓치지 않도록, 최근 2분 수정은 한 번 더 배포
    changed = latest != state.get("seen")
    recent = now - edited <= timedelta(minutes=2)
    pending = state.get("pending", False) or changed or (recent and state.get("deployed_for") != latest + "#2")
    if pending and not gh.run_active():
        gh.dispatch()
        print(f"{now.astimezone():%m-%d %H:%M} 노션 변경({edited.astimezone():%H:%M}) → 배포 시작", flush=True)
        state["deployed_for"] = latest + "#2" if (recent and not changed) else latest
        pending = False
    state.update(seen=latest, pending=pending)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def main():
    load_env()
    gh = GitHub()
    if "--loop" not in sys.argv:
        check(gh)
        return
    print("노션 변경 감시 시작 (Ctrl+C로 종료)", flush=True)
    while True:
        try:
            check(gh)
        except Exception as e:  # 네트워크 오류 등 — 다음 회차에 재시도
            print(time.strftime("%H:%M"), "확인 실패:", str(e)[:100], flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
