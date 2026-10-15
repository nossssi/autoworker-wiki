#!/usr/bin/env python3
"""강의 라이브 모드 — 1분마다 노션 변경을 확인하고, 바뀌었으면 바로 배포를 트리거한다.

강의 중 노션에 실시간으로 메모하면 2~3분 안에 위키에 반영된다.
사용: python3 live.py [시간]   (기본 4시간 뒤 자동 종료, Ctrl+C로 언제든 종료)
"""
import json
import subprocess
import sys
import time

import sync

REPO = "nossssi/autoworker-wiki"
INTERVAL = 60


def run_active():
    out = subprocess.run(["gh", "run", "list", "-R", REPO, "--workflow", "deploy.yml", "--limit", "5",
                          "--json", "status"], capture_output=True, text=True).stdout
    return any(r["status"] in ("queued", "in_progress", "waiting", "pending", "requested")
               for r in json.loads(out or "[]"))


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 4
    end = time.time() + hours * 3600
    pending = False
    print(f"라이브 모드 시작 — {hours:g}시간 동안 1분마다 노션 확인", flush=True)
    while time.time() < end:
        try:
            pending = sync.main(quiet=True)["changed"] or pending
        except Exception as e:  # 네트워크 오류 등 — 다음 회차에 재시도
            print(time.strftime("%H:%M"), "확인 실패:", str(e)[:100], flush=True)
        if pending and not run_active():
            subprocess.run(["gh", "workflow", "run", "deploy.yml", "-R", REPO, "--ref", "main"],
                           check=True, capture_output=True)
            print(time.strftime("%H:%M"), "노션 변경 감지 → 배포 시작 (2~3분 뒤 반영)", flush=True)
            pending = False
        time.sleep(INTERVAL)
    print("라이브 모드 종료", flush=True)


if __name__ == "__main__":
    main()
