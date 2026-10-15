#!/usr/bin/env python3
"""노션 → 로컬 캐시(.cache/) 동기화.

config.json의 섹션 루트 페이지부터 하위 페이지·데이터베이스·행 페이지를 재귀로 크롤링한다.
- 페이지 블록 트리: .cache/notion/pages/<id>.json
- 데이터베이스(스키마+행 목록): .cache/notion/dbs/<id>.json
- 노션 호스팅 파일(이미지·첨부): .cache/files/<hash>/<name>  (서명 URL이 1시간 뒤 만료되므로 즉시 다운로드)

증분: 페이지 last_edited_time이 캐시와 같으면 블록을 다시 받지 않는다.
사용: NOTION_TOKEN=... python3 sync.py   (또는 .env)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
PAGES = CACHE / "notion" / "pages"
DBS = CACHE / "notion" / "dbs"
FILES = CACHE / "files"
API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
MAX_FILE_BYTES = 95 * 1024 * 1024
UA = "autoworker-wiki-sync/1.0 (+https://wiki.autoworkers.ai)"


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def norm_id(s):
    m = re.search(r"([0-9a-f]{32})", s.replace("-", ""))
    if not m:
        raise ValueError(f"노션 ID가 아님: {s}")
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class RateLimiter:
    """노션 API 평균 3 req/s 제한 — 2.7 req/s로 균등 분배."""

    def __init__(self, per_sec=2.7):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        if at > now:
            time.sleep(at - now)


class Notion:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "User-Agent": UA,
        })
        self.rl = RateLimiter()
        self.calls = 0

    def req(self, method, path, body=None, params=None):
        for attempt in range(6):
            self.rl.wait()
            self.calls += 1
            try:
                r = self.s.request(method, API + path, json=body, params=params, timeout=60)
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                last = str(e)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                last = r.text[:200]
                continue
            if r.status_code >= 400:
                raise NotionError(r.status_code, r.json().get("message", r.text[:200]))
            return r.json()
        raise NotionError(0, f"재시도 초과: {last}")

    def paginate(self, method, path, body=None):
        out, cursor = [], None
        while True:
            if method == "GET":
                params = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                d = self.req("GET", path, params=params)
            else:
                b = dict(body or {}, page_size=100)
                if cursor:
                    b["start_cursor"] = cursor
                d = self.req("POST", path, body=b)
            out += d.get("results", [])
            if not d.get("has_more"):
                return out
            cursor = d["next_cursor"]


class NotionError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"[{status}] {msg}")
        self.status = status


# ───────── 파일 다운로드 ─────────

def safe_name(name):
    name = unicodedata.normalize("NFC", unquote(name)).split("/")[-1] or "file"
    name = re.sub(r"[\\/:*?\"<>|#%&{}$!'@+`=\s]+", "_", name).strip("._") or "file"
    stem, dot, ext = name.rpartition(".")
    if dot and len(stem) > 80:
        name = stem[:80] + "." + ext
    return name[:120]


def file_key(url):
    """서명 쿼리를 뗀 S3 경로로 키를 만든다 → 같은 파일은 재다운로드하지 않음."""
    p = urlparse(url)
    return hashlib.sha1((p.netloc + p.path).encode()).hexdigest()[:16], safe_name(p.path)


IMAGE_EXT = {".png", ".jpg", ".jpeg"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MAX_DOWNLOAD_BYTES = 800 * 1024 * 1024   # 큰 영상은 받아서 줄여본다


def optimize_image(path):
    """150KB 넘는 PNG/JPG → 가로 최대 2000px WebP. 더 작아질 때만 교체."""
    try:
        from PIL import Image
    except ImportError:
        return path
    if path.stat().st_size < 150 * 1024:
        return path
    try:
        with Image.open(path) as im:
            im.load()
            if im.width > 2000:
                im = im.resize((2000, round(im.height * 2000 / im.width)), Image.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            out = path.with_suffix(".webp")
            im.save(out, "WEBP", quality=82, method=4)
    except Exception:
        return path
    if out.stat().st_size < path.stat().st_size:
        path.unlink()
        return out
    out.unlink()
    return path


def shrink_video(path):
    """95MB 넘는 영상 → 720p H.264 (ffmpeg 있을 때만)."""
    if not shutil.which("ffmpeg"):
        return path
    out = path.with_name(path.stem + "_720p.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", "scale='min(1280,iw)':-2",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart", str(out)]
    if subprocess.run(cmd).returncode == 0 and out.exists():
        path.unlink()
        return out
    out.unlink(missing_ok=True)
    return path


def download(url, stats):
    key, name = file_key(url)
    folder = FILES / key
    done = [p for p in folder.glob("*") if not p.name.endswith(".part")] if folder.exists() else []
    if done:
        return {"local": f"files/{key}/{done[0].name}", "size": done[0].stat().st_size}
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    ext = dest.suffix.lower()
    limit = MAX_DOWNLOAD_BYTES if ext in VIDEO_EXT else MAX_FILE_BYTES
    tmp = folder / (name + ".part")
    try:
        with requests.get(url, stream=True, timeout=300, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            n = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    n += len(chunk)
                    if n > limit:
                        break
                    f.write(chunk)
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        stats["failed"].append((name, str(e)[:120]))
        return {"local": None, "error": str(e)[:120]}
    if n > limit:
        tmp.unlink(missing_ok=True)
        stats["skipped_big"].append((name, n))
        return {"local": None, "size": n, "too_big": True}
    tmp.rename(dest)
    if ext in IMAGE_EXT:
        dest = optimize_image(dest)
    elif ext in VIDEO_EXT and dest.stat().st_size > MAX_FILE_BYTES:
        dest = shrink_video(dest)
    size = dest.stat().st_size
    if size > MAX_FILE_BYTES:
        dest.unlink()
        stats["skipped_big"].append((name, size))
        return {"local": None, "size": size, "too_big": True}
    stats["downloaded"] += 1
    return {"local": f"files/{key}/{dest.name}", "size": size}


# ───────── 크롤러 ─────────

MEDIA_TYPES = {"image", "video", "file", "pdf", "audio"}


def is_notion_hosted(url):
    host = urlparse(url or "").netloc
    return "prod-files-secure" in host or host.startswith("file.notion") or "notion-static" in host


def block_files(b):
    """블록이 참조하는 노션 호스팅 파일 정보 dict 목록 (다운로드 후 "local"이 채워진다)."""
    t = b["type"]
    holders = []
    if t in MEDIA_TYPES:
        holders.append(b[t])
    if t == "callout":
        holders.append(b["callout"].get("icon"))
    out = []
    for h in holders:
        ht = (h or {}).get("type")
        if ht in ("file", "custom_emoji") and (h.get(ht) or {}).get("url"):
            out.append(h[ht])
    if t == "embed" and is_notion_hosted(b["embed"].get("url")):
        out.append(b["embed"])
    return out


class Crawler:
    def __init__(self, notion, include_private):
        self.n = notion
        self.include_private = include_private
        self.seen_pages = set()
        self.seen_dbs = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.files = []  # 파일 정보를 담은 dict들 — 마지막에 일괄 다운로드 후 "local" 키를 채운다
        self.stats = {"pages": 0, "pages_fetched": 0, "dbs": 0, "downloaded": 0,
                      "skipped_big": [], "failed": [], "errors": []}

    # 파일 참조를 모아두었다가 크롤링 끝에서 병렬 다운로드
    def queue_file(self, holder):
        """holder: {"type": "file", "file": {...}} 또는 {"type": "custom_emoji", "custom_emoji": {...}}"""
        if not holder:
            return
        t = holder.get("type")
        if t in ("file", "custom_emoji") and (holder.get(t) or {}).get("url"):
            self.files.append(holder[t])

    def collect_files_in_props(self, props):
        for p in (props or {}).values():
            if p.get("type") == "files":
                for f in p.get("files", []):
                    self.queue_file(f)

    def collect_icon(self, obj):
        self.queue_file(obj.get("icon"))
        self.queue_file(obj.get("cover"))

    def fetch_children(self, block_id, depth=0):
        if depth > 12:
            return []
        blocks = self.n.paginate("GET", f"/blocks/{block_id}/children")
        for b in blocks:
            t = b["type"]
            self.files += block_files(b)
            if b.get("has_children") and t not in ("child_page", "child_database"):
                src = block_id
                if t == "synced_block" and (b["synced_block"].get("synced_from") or {}).get("block_id"):
                    src = b["synced_block"]["synced_from"]["block_id"]
                    try:
                        b["children"] = self.fetch_children(src, depth + 1)
                    except NotionError as e:
                        b["children"] = []
                        self.stats["errors"].append(f"synced {src}: {e}")
                    continue
                b["children"] = self.fetch_children(b["id"], depth + 1)
        return blocks

    def walk_refs(self, blocks, parent_id):
        """블록 트리 안의 하위 페이지·DB를 찾아 크롤링 예약."""
        futs = []
        for b in blocks:
            t = b["type"]
            if t == "child_page":
                futs.append(self.pool.submit(self.crawl_page, b["id"], {"type": "page", "id": parent_id}))
            elif t == "child_database":
                futs.append(self.pool.submit(self.crawl_db, b["id"], {"type": "page", "id": parent_id}))
            if b.get("children"):
                futs += self.walk_refs(b["children"], parent_id)
        return futs

    def crawl_page(self, page_id, parent, page_obj=None):
        page_id = norm_id(page_id)
        with self.lock:
            if page_id in self.seen_pages:
                return []
            self.seen_pages.add(page_id)
        try:
            page = page_obj or self.n.req("GET", f"/pages/{page_id}")
        except NotionError as e:
            self.stats["errors"].append(f"page {page_id}: {e}")
            return []
        if page.get("in_trash") or page.get("archived"):
            return []
        cache_path = PAGES / f"{page_id}.json"
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else None
        if (cached and cached.get("last_edited_time") == page["last_edited_time"]
                and self._cached_files_ok(cached["blocks"])):
            blocks = cached["blocks"]
            # 캐시된 블록의 파일이 지워졌을 수 있으니 다운로드 큐에는 다시 넣는다(이미 있으면 스킵됨)
            self._requeue_cached(blocks)
        else:
            try:
                blocks = self.fetch_children(page_id)
            except NotionError as e:
                self.stats["errors"].append(f"blocks {page_id}: {e}")
                blocks = []
            self.stats["pages_fetched"] += 1
        self.collect_files_in_props(page.get("properties"))
        self.collect_icon(page)
        rec = {
            "id": page_id,
            "parent": parent,
            "url": page.get("url"),
            "created_time": page.get("created_time"),
            "last_edited_time": page["last_edited_time"],
            "icon": page.get("icon"),
            "cover": page.get("cover"),
            "properties": page.get("properties", {}),
            "blocks": blocks,
        }
        self._pending_writes.append((cache_path, rec))
        self.stats["pages"] += 1
        return self.walk_refs(blocks, page_id)

    def _requeue_cached(self, blocks):
        for b in blocks:
            self.files += block_files(b)
            if b.get("children"):
                self._requeue_cached(b["children"])

    def _cached_files_ok(self, blocks):
        """캐시 블록이 참조하는 파일이 디스크에 다 있어야 재사용(서명 URL은 이미 만료됐으므로)."""
        for b in blocks:
            for info in block_files(b):
                if info.get("too_big"):
                    continue
                if not info.get("local") or not (CACHE / info["local"]).exists():
                    return False
            if b.get("children") and not self._cached_files_ok(b["children"]):
                return False
        return True

    def crawl_db(self, db_id, parent):
        db_id = norm_id(db_id)
        with self.lock:
            if db_id in self.seen_dbs:
                return []
            self.seen_dbs.add(db_id)
        if not self.include_private and db_id in self.private_ids:
            return []
        try:
            db = self.n.req("GET", f"/databases/{db_id}")
        except NotionError as e:
            # 링크된 DB 뷰 등 API 미지원 — 빌드에서 안내 문구로 대체
            self.stats["errors"].append(f"db {db_id}: {e}")
            self._pending_writes.append((DBS / f"{db_id}.json", {"id": db_id, "parent": parent, "error": str(e)}))
            return []
        self.collect_icon(db)
        rec = {
            "id": db_id,
            "parent": parent,
            "title": db.get("title", []),
            "description": db.get("description", []),
            "icon": db.get("icon"),
            "is_inline": db.get("is_inline"),
            "data_sources": [],
        }
        futs = []
        for ds in db.get("data_sources", []):
            try:
                meta = self.n.req("GET", f"/data_sources/{ds['id']}")
                rows = self.n.paginate("POST", f"/data_sources/{ds['id']}/query")
            except NotionError as e:
                self.stats["errors"].append(f"data_source {ds['id']}: {e}")
                continue
            rows = [r for r in rows if not r.get("in_trash") and not r.get("archived")]
            rec["data_sources"].append({
                "id": ds["id"],
                "name": ds.get("name") or "",
                "properties": meta.get("properties", {}),
                "rows": [norm_id(r["id"]) for r in rows],
            })
            for r in rows:
                futs.append(self.pool.submit(self.crawl_page, r["id"], {"type": "database", "id": db_id}, r))
        self._pending_writes.append((DBS / f"{db_id}.json", rec))
        self.stats["dbs"] += 1
        return futs

    def run(self, roots, private_ids):
        self.private_ids = {norm_id(x) for x in private_ids}
        self._pending_writes = []
        futs = []
        for pid in roots:
            futs.append(self.pool.submit(self.crawl_page, pid, None))
        # 퓨처가 또 퓨처를 낳으므로 비워질 때까지 반복
        while futs:
            f = futs.pop()
            more = f.result()
            if more:
                futs.extend(more)
        self.pool.shutdown()
        return self._pending_writes


def main():
    load_env()
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        sys.exit("NOTION_TOKEN이 없습니다 — .env에 NOTION_TOKEN=ntn_... 를 넣어주세요.")
    cfg = json.loads((ROOT / "config.json").read_text())
    pf = cfg.get("private_feedback", {})
    include_private = bool(pf.get("enabled"))
    roots = [s["page_id"] for s in cfg["sections"]]
    if include_private:
        roots.append(pf["page_id"])
    private_ids = [] if include_private else [pf.get("db_id", "")] if pf.get("db_id") else []

    for d in (PAGES, DBS, FILES):
        d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    n = Notion(token)
    c = Crawler(n, include_private)
    writes = c.run(roots, private_ids)

    # 파일 다운로드 (서명 URL이 살아있을 때)
    # 같은 파일이 여러 블록에서 참조될 수 있으므로 키별로 한 번만 받는다
    by_key = {}
    for info in c.files:
        by_key.setdefault(file_key(info["url"]), []).append(info)

    def dl(infos):
        res = download(infos[0]["url"], c.stats)
        for info in infos:
            info.update(res)
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(dl, by_key.values()))

    # 이번 크롤링에서 도달한 것만 남기고 고아 캐시 정리
    live = set()
    for path, rec in writes:
        path.write_text(json.dumps(rec, ensure_ascii=False))
        live.add(path.name)
    for d in (PAGES, DBS):
        for p in d.glob("*.json"):
            if p.name not in live:
                p.unlink()
    (CACHE / "notion" / "manifest.json").write_text(json.dumps({
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "roots": [norm_id(r) for r in roots],
        "include_private": include_private,
    }, ensure_ascii=False))

    s = c.stats
    print(f"완료 {time.time() - t0:.0f}s · API {n.calls}회 · 페이지 {s['pages']}개(새로 받음 {s['pages_fetched']}) · "
          f"DB {s['dbs']}개 · 파일 다운로드 {s['downloaded']}개")
    for name, size in s["skipped_big"]:
        print(f"  ⚠️ 용량 초과로 건너뜀: {name} ({size / 1e6:.0f}MB)")
    for name, err in s["failed"]:
        print(f"  ⚠️ 다운로드 실패: {name} — {err}")
    for e in s["errors"]:
        print(f"  ⚠️ {e}")


if __name__ == "__main__":
    main()
