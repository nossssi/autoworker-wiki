#!/usr/bin/env python3
"""로컬 캐시(.cache/notion) → 정적 사이트(_site/).

- 섹션 루트에서 도달 가능한 페이지만 렌더링 (비공개 DB·속성은 config로 제외)
- URL: /           홈
       /<slug>/    섹션 루트 (common, premium)
       /p/<id>/    일반 페이지·DB 행
       /d/<id>/    풀페이지 DB (인라인이 아닌 DB)
- 검색 인덱스: /search-index.json
- 판(edition): 공개판은 루트(/)에 private 아닌 섹션만, 프반판은 /premium-<토큰>/ 아래에 전체 섹션.
  토큰 = HMAC(LINK_SALT) — 저장소에 남지 않으므로 링크를 받은 사람만 들어올 수 있다.
사용: python3 build.py
"""
import hashlib
import hmac
import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("WIKI_CACHE", ROOT / ".cache"))
OUT = Path(os.environ.get("WIKI_OUT", ROOT / "_site"))
STATIC = ROOT / "static"
KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = "월화수목금토일"

esc = html.escape


def hexid(i):
    return i.replace("-", "")


def norm_id(s):
    """UUID(대시 有/無) 또는 노션 URL(…/제목-<32hex>)에서 페이지 ID 추출."""
    s = (s or "").lower()
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s)
    if m:
        h = m.group(0).replace("-", "")
    else:
        # 제목 슬러그의 숫자가 ID에 붙지 않도록 '독립된' 32자리 토큰을 찾는다 (예: /9-065c...)
        m = re.search(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", s)
        if not m:
            return None
        h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def plain(rt):
    return "".join(t.get("plain_text", "") for t in (rt or []))


def parse_dt(s):
    if not s:
        return None, False
    if len(s) == 10:
        return datetime.fromisoformat(s).replace(tzinfo=KST), False
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d.astimezone(KST), True


def fmt_date(start, end=None, with_time=True):
    d, has_t = parse_dt(start)
    if not d:
        return ""
    s = f"{d.year}. {d.month}. {d.day} ({WEEKDAYS[d.weekday()]})"
    if has_t and with_time:
        s += " " + fmt_time(d)
    if end:
        e, e_t = parse_dt(end)
        if e:
            if e.date() == d.date() and e_t:
                s += " ~ " + fmt_time(e)
            else:
                s += f" ~ {e.month}. {e.day}"
    return s


def fmt_time(d):
    h, m = d.hour, d.minute
    ap = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    return f"{ap} {h12}:{m:02d}" if m else f"{ap} {h12}시"


def slugify(text):
    s = re.sub(r"[^\w가-힣\s-]", "", text).strip().lower()
    return re.sub(r"[\s_]+", "-", s)[:60] or "section"


# ───────────────────────── 데이터 로드 ─────────────────────────

class Site:
    def __init__(self, edition):
        self.ed = edition          # config.json의 판(edition) 하나
        self.base = edition["base"]  # 판 비밀 경로 (예: /aw2-xxxxxxxxxxxx)
        self.cfg = json.loads((ROOT / "config.json").read_text())
        self.pages = {}
        self.dbs = {}
        for p in (CACHE / "notion" / "pages").glob("*.json"):
            r = json.loads(p.read_text())
            self.pages[r["id"]] = r
        for p in (CACHE / "notion" / "dbs").glob("*.json"):
            r = json.loads(p.read_text())
            self.dbs[r["id"]] = r
        man = CACHE / "notion" / "manifest.json"
        self.manifest = json.loads(man.read_text()) if man.exists() else {}
        self.hide_props = set(self.cfg.get("hide_properties", []))
        self.hide_types = set(self.cfg.get("hide_property_types", []))
        self.sections = []
        for key in edition["sections"]:
            s = self.cfg["sections"][key]
            pid = norm_id(s["page_id"])
            if pid in self.pages:
                self.sections.append(dict(s, id=pid, key=key))
        self.section_of_root = {s["id"]: s for s in self.sections}
        self.curriculum_db = norm_id(edition.get("curriculum_db"))
        self.used_files = set()
        self.private_ids = set()
        pf = self.cfg.get("private_feedback", {})
        for k in ("page_id", "db_id"):
            if pf.get(k):
                self.private_ids.add(norm_id(pf[k]))
        self._reach()

    # 섹션 루트에서 도달 가능한 페이지/DB만 공개 대상
    def _reach(self):
        self.public_pages, self.public_dbs = set(), set()
        self.section_for = {}
        stack = [(s["id"], "page", s) for s in self.sections]
        while stack:
            oid, kind, sec = stack.pop()
            if oid in self.private_ids:
                continue
            if kind == "page":
                if oid in self.public_pages or oid not in self.pages:
                    continue
                self.public_pages.add(oid)
                self.section_for[oid] = sec
                for b in iter_blocks(self.pages[oid]["blocks"]):
                    if b["type"] == "child_page":
                        stack.append((norm_id(b["id"]), "page", sec))
                    elif b["type"] == "child_database":
                        stack.append((norm_id(b["id"]), "db", sec))
            else:
                if oid in self.public_dbs or oid not in self.dbs:
                    continue
                self.public_dbs.add(oid)
                self.section_for[oid] = sec
                for ds in self.dbs[oid].get("data_sources", []):
                    for rid in ds["rows"]:
                        stack.append((rid, "page", sec))

    # ── 제목·URL ──
    def title(self, pid):
        p = self.pages.get(pid)
        if not p:
            return "제목 없음"
        for v in p["properties"].values():
            if v.get("type") == "title":
                return plain(v["title"]).strip() or "제목 없음"
        return "제목 없음"

    def db_title(self, did):
        d = self.dbs.get(did) or {}
        t = plain(d.get("title")).strip()
        if not t and d.get("data_sources"):
            t = d["data_sources"][0].get("name", "")
        return t or "목록"

    def url(self, oid):
        if oid in self.section_of_root:
            return f"{self.base}/{self.section_of_root[oid]['slug']}/"
        if oid in self.public_pages:
            return f"{self.base}/p/{hexid(oid)}/"
        if oid in self.public_dbs:
            return f"{self.base}/d/{hexid(oid)}/"
        return None

    def parent_of(self, oid):
        rec = self.pages.get(oid) or self.dbs.get(oid)
        par = (rec or {}).get("parent")
        return norm_id(par["id"]) if par else None

    def ancestors(self, oid):
        """루트→직전 부모 순서의 (id, kind) 목록 — 섹션 루트에서 멈춘다."""
        chain, cur, guard = [], self.parent_of(oid), 0
        while cur and guard < 30:
            guard += 1
            kind = "page" if cur in self.pages else "db"
            chain.append((cur, kind))
            if cur in self.section_of_root:
                break
            cur = self.parent_of(cur)
        return list(reversed(chain))

    def has_content(self, pid):
        p = self.pages.get(pid)
        return bool(p and any(b["type"] != "paragraph" or plain(b["paragraph"]["rich_text"]).strip()
                              for b in p["blocks"]))

    def file_url(self, info):
        """노션 파일 정보 dict → 사이트 경로(다운로드된 경우) 또는 외부 URL."""
        if not info:
            return None
        if info.get("local"):
            self.used_files.add(info["local"])
            return "/" + info["local"]
        return info.get("url")

    def media_url(self, holder):
        t = (holder or {}).get("type")
        if t == "external":
            return holder["external"]["url"]
        if t in ("file", "custom_emoji"):
            return self.file_url(holder[t])
        return None

    def icon_html(self, icon, cls="ic"):
        if not icon:
            return ""
        t = icon.get("type")
        if t == "emoji":
            return f'<span class="{cls}">{esc(icon["emoji"])}</span>'
        u = self.media_url(icon)
        if u:
            return f'<img class="{cls} ic-img" src="{esc(u)}" alt="" loading="lazy">'
        return ""

    def map_href(self, href):
        if not href:
            return href, False
        if href.startswith("/") and not href.startswith("//"):
            nid = norm_id(href)
            if nid and self.url(nid):
                return self.url(nid), True
        host = urlparse(href).netloc
        if "notion.so" in host or "notion.site" in host or "notion.com" in host or host == "wiki.autoworkers.ai":
            nid = norm_id(urlparse(href).path)
            if nid and self.url(nid):
                return self.url(nid), True
        return href, False


def _is_dead_wiki_link(self, u):
    """예전 노션 사이트(wiki.autoworkers.ai/<slug>-<id>) 주소인데 우리 사이트에 없는 페이지."""
    p = urlparse(u)
    return p.netloc == "wiki.autoworkers.ai" and bool(norm_id(p.path)) and not self.url(norm_id(p.path))


Site.is_dead_wiki_link = _is_dead_wiki_link


def iter_blocks(blocks):
    for b in blocks or []:
        yield b
        if b.get("children"):
            yield from iter_blocks(b["children"])


# ───────────────────────── 리치 텍스트 ─────────────────────────

def rt_html(site, rts):
    out = []
    for t in rts or []:
        a = t.get("annotations", {})
        typ = t.get("type")
        text = esc(t.get("plain_text", "")).replace("\n", "<br>")
        href = (t.get("href") or "")
        if typ == "mention":
            m = t["mention"]
            mt = m.get("type")
            if mt in ("page", "database"):
                nid = norm_id(m[mt]["id"])
                u = site.url(nid) if nid else None
                if u:
                    label = esc(site.title(nid) if mt == "page" and nid in site.pages else t.get("plain_text", ""))
                    text = f'<a class="mention" href="{u}">{label}</a>'
                else:
                    text = f'<span class="mention muted">{text}</span>'
                href = ""
            elif mt == "date":
                d = m["date"]
                text = f'<span class="mention-date">{esc(fmt_date(d.get("start"), d.get("end")))}</span>'
            elif mt == "user":
                text = f'<span class="mention-user">@{text.lstrip("@")}</span>'
            elif mt in ("link_mention", "link_preview"):
                lm = m[mt]
                u, internal = site.map_href(lm.get("href") or lm.get("url") or t.get("href"))
                label = esc(lm.get("title") or t.get("plain_text", ""))
                if internal:
                    text = f'<a class="mention" href="{esc(u)}">{label}</a>'
                elif u and site.is_dead_wiki_link(u):
                    text = f'<span class="mention muted" title="노션 전용 페이지">{label}</span>'
                else:
                    text = f'<a class="mention" href="{esc(u)}" target="_blank" rel="noopener">{label} ↗</a>'
                href = ""
        elif typ == "equation":
            text = f'<code class="math">{esc(t["equation"]["expression"])}</code>'
        if a.get("code"):
            text = f"<code>{text}</code>"
        if a.get("bold"):
            text = f"<strong>{text}</strong>"
        if a.get("italic"):
            text = f"<em>{text}</em>"
        if a.get("strikethrough"):
            text = f"<s>{text}</s>"
        if a.get("underline"):
            text = f"<u>{text}</u>"
        color = a.get("color", "default")
        if color and color != "default":
            text = f'<span class="c-{color}">{text}</span>'
        if href and typ != "mention":
            u, internal = site.map_href(href)
            if not internal and site.is_dead_wiki_link(u):
                text = f'<span class="muted" title="노션 전용 페이지">{text}</span>'
            else:
                ext = "" if internal else ' target="_blank" rel="noopener"'
                text = f'<a href="{esc(u)}"{ext}>{text}</a>'
        out.append(text)
    return "".join(out)


# ───────────────────────── 블록 → HTML ─────────────────────────

def yt_id(u):
    p = urlparse(u)
    if "youtu.be" in p.netloc:
        return p.path.strip("/").split("/")[0]
    if "youtube.com" in p.netloc:
        if p.path.startswith("/watch"):
            return (parse_qs(p.query).get("v") or [None])[0]
        m = re.match(r"/(embed|shorts|live)/([\w-]+)", p.path)
        if m:
            return m.group(2)
    return None


def embed_src(u):
    """iframe으로 넣을 수 있는 서비스면 embed URL, 아니면 None."""
    if not u:
        return None
    v = yt_id(u)
    if v:
        return f"https://www.youtube.com/embed/{v}"
    p = urlparse(u)
    if "vimeo.com" in p.netloc:
        m = re.search(r"/(\d+)", p.path)
        if m:
            return f"https://player.vimeo.com/video/{m.group(1)}"
    if p.netloc in ("docs.google.com",) and any(k in p.path for k in ("/presentation/", "/document/", "/spreadsheets/", "/forms/")):
        return u
    if "drive.google.com" in p.netloc and "/file/d/" in p.path:
        return re.sub(r"/(view|edit).*$", "/preview", u)
    if "loom.com" in p.netloc and "/share/" in p.path:
        return u.replace("/share/", "/embed/")
    return None


def link_card(url, title=None, sub=None, icon="🔗"):
    host = urlparse(url).netloc.replace("www.", "") if url else ""
    return (f'<a class="link-card" href="{esc(url)}" target="_blank" rel="noopener">'
            f'<span class="lc-icon">{icon}</span><span class="lc-body">'
            f'<span class="lc-title">{esc(title or url)}</span>'
            f'<span class="lc-sub">{esc(sub or host)}</span></span><span class="lc-arrow">↗</span></a>')


def human_size(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


class Renderer:
    def __init__(self, site, page_id):
        self.site = site
        self.page_id = page_id
        self.headings = []
        self.text = []  # 검색용 평문

    def rt(self, rts):
        self.text.append(plain(rts))
        return rt_html(self.site, rts)

    def blocks(self, blocks):
        out, i = [], 0
        blocks = blocks or []
        while i < len(blocks):
            b = blocks[i]
            t = b["type"]
            if t in ("bulleted_list_item", "numbered_list_item", "to_do"):
                tag = {"bulleted_list_item": "ul", "numbered_list_item": "ol", "to_do": "ul"}[t]
                cls = ' class="todo"' if t == "to_do" else ""
                items = []
                while i < len(blocks) and blocks[i]["type"] == t:
                    items.append(self.list_item(blocks[i]))
                    i += 1
                out.append(f"<{tag}{cls}>{''.join(items)}</{tag}>")
                continue
            out.append(self.block(b))
            i += 1
        return "".join(out)

    def color_cls(self, data):
        c = data.get("color", "default")
        if not c or c == "default":
            return ""
        return f" c-{c}" if not c.endswith("_background") else f" bg-{c[:-11]}"

    def list_item(self, b):
        t = b["type"]
        d = b[t]
        kids = self.blocks(b.get("children")) if b.get("children") else ""
        body = self.rt(d.get("rich_text"))
        if t == "to_do":
            chk = " checked" if d.get("checked") else ""
            done = " done" if d.get("checked") else ""
            return f'<li class="todo-item{done}"><input type="checkbox" disabled{chk}><span>{body}</span>{kids}</li>'
        return f'<li class="{self.color_cls(d).strip()}">{body}{kids}</li>'

    def block(self, b):
        t = b["type"]
        d = b.get(t, {})
        S = self.site
        kids = lambda: self.blocks(b.get("children")) if b.get("children") else ""
        if t == "paragraph":
            body = self.rt(d.get("rich_text"))
            if not body.strip() and not b.get("children"):
                return '<div class="gap"></div>'
            inner = f'<div class="indent">{kids()}</div>' if b.get("children") else ""
            return f'<p class="{self.color_cls(d).strip()}">{body}</p>{inner}'
        if t in ("heading_1", "heading_2", "heading_3", "heading_4"):
            level = {"heading_1": 2, "heading_2": 3, "heading_3": 4, "heading_4": 5}[t]
            text = plain(d.get("rich_text"))
            hid = self.heading_id(text)
            self.headings.append((level, text, hid))
            h = f'<h{level} id="{hid}" class="{self.color_cls(d).strip()}">{self.rt(d.get("rich_text"))}</h{level}>'
            if d.get("is_toggleable"):
                return f'<details class="toggle toggle-h"><summary>{h}</summary><div class="toggle-body">{kids()}</div></details>'
            return h + kids()
        if t == "toggle":
            return (f'<details class="toggle{self.color_cls(d)}"><summary>{self.rt(d.get("rich_text"))}</summary>'
                    f'<div class="toggle-body">{kids()}</div></details>')
        if t == "quote":
            return f'<blockquote class="{self.color_cls(d).strip()}">{self.rt(d.get("rich_text"))}{kids()}</blockquote>'
        if t == "callout":
            icon = S.icon_html(d.get("icon"), "callout-icon")
            c = d.get("color", "gray_background")
            cc = f"bg-{c[:-11]}" if c.endswith("_background") else (f"c-{c} bordered" if c != "default" else "bordered")
            return (f'<div class="callout {cc}">{icon}<div class="callout-body">'
                    f'<div>{self.rt(d.get("rich_text"))}</div>{kids()}</div></div>')
        if t == "code":
            code = plain(d.get("rich_text"))
            self.text.append(code)
            lang = d.get("language", "")
            label = "" if lang in ("plain text", "markdown", "javascript") else lang
            cap = plain(d.get("caption"))
            return (f'<div class="code"><div class="code-bar"><span>{esc(cap or label)}</span>'
                    f'<button class="copy" type="button">복사</button></div>'
                    f'<pre><code>{esc(code)}</code></pre></div>')
        if t == "divider":
            return "<hr>"
        if t == "equation":
            return f'<div class="math-block"><code>{esc(d.get("expression", ""))}</code></div>'
        if t == "image":
            u = S.media_url(d)
            cap = self.rt(d.get("caption"))
            if not u:
                return ""
            figcap = f"<figcaption>{cap}</figcaption>" if cap else ""
            return f'<figure class="img"><img src="{esc(u)}" alt="{esc(plain(d.get("caption")))}" loading="lazy">{figcap}</figure>'
        if t == "video":
            u = S.media_url(d)
            cap = self.rt(d.get("caption"))
            figcap = f"<figcaption>{cap}</figcaption>" if cap else ""
            if d.get("type") == "external":
                es = embed_src(u)
                if es:
                    return f'<figure class="embed"><div class="ratio"><iframe src="{esc(es)}" allowfullscreen loading="lazy"></iframe></div>{figcap}</figure>'
                return link_card(u, plain(d.get("caption")) or None, icon="🎬")
            if u:
                return f'<figure class="video"><video controls preload="metadata" src="{esc(u)}"></video>{figcap}</figure>'
            return self.too_big(d)
        if t in ("file", "pdf", "audio"):
            u = S.media_url(d)
            info = d.get(d.get("type"), {}) or {}
            name = d.get("name") or (urlparse(u).path.split("/")[-1] if u else "파일")
            cap = plain(d.get("caption"))
            if not u:
                return self.too_big(d)
            if t == "file" and re.search(r"\.(mp4|mov|m4v|webm)$", u.lower()):
                return f'<figure class="video"><video controls preload="metadata" src="{esc(u)}"></video><figcaption>{esc(cap)}</figcaption></figure>'
            if t == "audio" and d.get("type") == "file":
                return f'<figure class="audio"><audio controls preload="none" src="{esc(u)}"></audio><figcaption>{esc(cap or name)}</figcaption></figure>'
            icon = {"pdf": "📕", "audio": "🎧"}.get(t, "📎")
            return (f'<a class="file-card" href="{esc(u)}" target="_blank" rel="noopener" download>'
                    f'<span class="lc-icon">{icon}</span><span class="lc-body"><span class="lc-title">{esc(cap or name)}</span>'
                    f'<span class="lc-sub">{esc(human_size(info.get("size")))} 다운로드</span></span><span class="lc-arrow">↓</span></a>')
        if t == "embed":
            cap = plain(d.get("caption"))
            if d.get("local") or d.get("too_big") or d.get("error"):
                # 노션에 올린 HTML 첨부 → 사이트에 함께 배포해서 iframe으로
                u = S.file_url(d)
                if not u or d.get("too_big") or d.get("error"):
                    return self.too_big(d) if d.get("too_big") else ""
                name = cap or urlparse(u).path.split("/")[-1]
                if u.lower().endswith((".html", ".htm")):
                    return (f'<figure class="html-embed"><iframe src="{esc(u)}" loading="lazy"></iframe>'
                            f'<figcaption><a href="{esc(u)}" target="_blank" rel="noopener">↗ 새 창에서 크게 보기</a></figcaption></figure>')
                return link_card(u, name, icon="📎")
            u = d.get("url")
            es = embed_src(u)
            if es:
                return f'<figure class="embed"><div class="ratio"><iframe src="{esc(es)}" allowfullscreen loading="lazy"></iframe></div></figure>'
            return link_card(u, cap or None)
        if t in ("bookmark", "link_preview"):
            u = d.get("url")
            if not u:
                return ""
            es = embed_src(u) if yt_id(u) else None
            if es:
                return f'<figure class="embed"><div class="ratio"><iframe src="{esc(es)}" allowfullscreen loading="lazy"></iframe></div></figure>'
            return link_card(u, plain(d.get("caption")) or None)
        if t == "table":
            rows = b.get("children") or []
            hr, hc = d.get("has_row_header"), d.get("has_column_header")
            out = []
            for ri, r in enumerate(rows):
                cells = r.get("table_row", {}).get("cells", [])
                tds = []
                for ci, cell in enumerate(cells):
                    tag = "th" if (hc and ri == 0) or (hr and ci == 0) else "td"
                    tds.append(f"<{tag}>{self.rt(cell)}</{tag}>")
                out.append(f"<tr>{''.join(tds)}</tr>")
            return f'<div class="table-wrap"><table>{"".join(out)}</table></div>'
        if t == "column_list":
            cols = []
            for c in b.get("children") or []:
                ratio = (c.get("column") or {}).get("width_ratio")
                style = f' style="flex:{ratio}"' if ratio else ""
                cols.append(f'<div class="column"{style}>{self.blocks(c.get("children"))}</div>')
            return f'<div class="columns">{"".join(cols)}</div>'
        if t == "column":
            return self.blocks(b.get("children"))
        if t == "child_page":
            pid = norm_id(b["id"])
            u = S.url(pid)
            if not u:
                return ""
            icon = S.icon_html((S.pages.get(pid) or {}).get("icon")) or '<span class="ic">📄</span>'
            return f'<a class="page-link" href="{u}">{icon}<span>{esc(S.title(pid))}</span></a>'
        if t == "link_to_page":
            target = norm_id(d.get(d.get("type"), "") or "")
            u = S.url(target) if target else None
            if not u:
                return ""
            label = S.title(target) if target in S.pages else S.db_title(target)
            return f'<a class="page-link" href="{u}"><span class="ic">↗</span><span>{esc(label)}</span></a>'
        if t == "child_database":
            did = norm_id(b["id"])
            return render_db_block(S, did, self)
        if t == "table_of_contents":
            return '<nav class="inline-toc" data-toc></nav>'
        if t in ("synced_block", "template"):
            return kids()
        return ""

    def too_big(self, d):
        info = d if "too_big" in d else (d.get(d.get("type"), {}) or {})
        if info.get("too_big"):
            return f'<div class="callout bg-yellow"><span class="callout-icon">⚠️</span><div class="callout-body">용량이 큰 파일({human_size(info.get("size"))})이라 노션에서만 볼 수 있어요.</div></div>'
        return ""

    def heading_id(self, text):
        base = slugify(text)
        hid, n = base, 2
        used = {h[2] for h in self.headings}
        while hid in used:
            hid, n = f"{base}-{n}", n + 1
        return hid


# ───────────────────────── 데이터베이스 ─────────────────────────

TAG_COLORS = {"default", "gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red"}


def chip(name, color="default"):
    color = color if color in TAG_COLORS else "default"
    return f'<span class="chip t-{color}">{esc(name)}</span>'


def visible_props(site, ds):
    """DB 스키마에서 표시할 속성 (title 제외, 숨김 규칙 적용)."""
    out = []
    for name, p in (ds.get("properties") or {}).items():
        if p["type"] == "title" or name in site.hide_props or p["type"] in site.hide_types:
            continue
        out.append((name, p["type"]))
    return out


def prop_value_html(site, prop, compact=False):
    t = prop.get("type")
    v = prop.get(t)
    if v in (None, [], ""):
        return ""
    if t == "select" or t == "status":
        return chip(v["name"], v.get("color"))
    if t == "multi_select":
        return "".join(chip(o["name"], o.get("color")) for o in v)
    if t == "date":
        return f'<span class="date">{esc(fmt_date(v.get("start"), v.get("end")))}</span>'
    if t == "rich_text":
        return rt_html(site, v)
    if t == "number":
        return esc(f"{v:,}" if isinstance(v, (int, float)) else str(v))
    if t == "checkbox":
        return "✅" if v else ""
    if t == "url":
        return f'<a href="{esc(v)}" target="_blank" rel="noopener">{esc(urlparse(v).netloc or v)}</a>'
    if t == "files":
        if compact:
            return f'<span class="muted">📎 {len(v)}</span>'
        parts = []
        for f in v:
            u = site.media_url(f)
            if not u:
                continue
            name = f.get("name") or "파일"
            if re.search(r"\.(png|jpe?g|gif|webp|svg)$", name.lower()) or re.search(r"\.(png|jpe?g|gif|webp)(\?|$)", u.lower()):
                parts.append(f'<a href="{esc(u)}" target="_blank" class="prop-img"><img src="{esc(u)}" alt="{esc(name)}" loading="lazy"></a>')
            else:
                parts.append(f'<a href="{esc(u)}" target="_blank" rel="noopener" download>📎 {esc(name)}</a>')
        return " ".join(parts)
    if t == "formula":
        f = v
        ft = f.get("type")
        fv = f.get(ft)
        if fv in (None, ""):
            return ""
        if ft == "date":
            return esc(fmt_date(fv.get("start"), fv.get("end")))
        if ft == "boolean":
            return "✅" if fv else ""
        return esc(str(fv))
    return ""


def row_date(page, ds):
    for name, p in (ds.get("properties") or {}).items():
        if p["type"] == "date":
            v = (page["properties"].get(name) or {}).get("date")
            if v and v.get("start"):
                return parse_dt(v["start"])[0]
    return None


def first_image(site, page):
    for p in page["properties"].values():
        if p.get("type") == "files":
            for f in p.get("files", []):
                name = (f.get("name") or "").lower()
                u = site.media_url(f)
                if u and (re.search(r"\.(png|jpe?g|gif|webp)$", name) or re.search(r"\.(png|jpe?g|gif|webp)$", urlparse(u).path.lower())):
                    return u
    if page.get("cover"):
        return site.media_url(page["cover"])
    return None


def natural_key(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


def sorted_rows(site, did, ds):
    rows = [site.pages[r] for r in ds["rows"] if r in site.pages and r in site.public_pages]
    has_date = any(row_date(r, ds) for r in rows)
    if has_date:
        far = datetime(2999, 1, 1, tzinfo=KST)
        return sorted(rows, key=lambda r: (row_date(r, ds) or far, r["created_time"]))
    titles = [site.title(r["id"]) for r in rows]
    if rows and sum(bool(re.match(r"^\s*\d", t)) for t in titles) >= max(1, len(rows) * 0.6):
        return sorted(rows, key=lambda r: natural_key(site.title(r["id"])))
    return sorted(rows, key=lambda r: r["created_time"])


def db_rows(site, did):
    db = site.dbs.get(did) or {}
    out = []
    for ds in db.get("data_sources", []):
        out += [(ds, r) for r in sorted_rows(site, did, ds)]
    return out


def render_schedule(site, did, rows):
    """커리큘럼 DB → 월별 일정표. 지난 일정은 흐리게, 가장 가까운 다음 일정 강조."""
    now = datetime.now(KST)
    items = []
    for ds, r in rows:
        d = row_date(r, ds)
        if d:
            items.append((d, ds, r))
    items.sort(key=lambda x: x[0])
    next_idx = next((i for i, (d, _, _) in enumerate(items) if d.date() >= now.date()), None)
    out, cur_month = [], None
    for i, (d, ds, r) in enumerate(items):
        month = (d.year, d.month)
        if month != cur_month:
            if cur_month:
                out.append("</ol>")
            out.append(f'<h4 class="sched-month">{d.month}월</h4><ol class="sched">')
            cur_month = month
        chips = []
        for name, typ in visible_props(site, ds):
            if typ in ("select", "multi_select", "status"):
                chips.append(prop_value_html(site, r["properties"].get(name, {})))
        state = "past" if d.date() < now.date() else ("next" if i == next_idx else "")
        u = site.url(r["id"]) if site.has_content(r["id"]) else None
        title = esc(site.title(r["id"]))
        title_html = f'<a href="{u}">{title}</a>' if u else title
        out.append(f'<li class="sched-item {state}"><div class="sched-date"><b>{d.day}</b><span>{WEEKDAYS[d.weekday()]}</span></div>'
                   f'<div class="sched-body"><div class="sched-title">{title_html}'
                   f'{"<span class=badge>다음 수업</span>" if state == "next" else ""}</div>'
                   f'<div class="chips">{"".join(chips)}</div></div></li>')
    if cur_month:
        out.append("</ol>")
    return f'<div class="schedule">{"".join(out)}</div>'


def group_rows(site, did, rows):
    """행이 6개 이상이고 '분류' 같은 select 값이 2종 이상이면 [(이름, 색, 행들)] 로 묶는다.
    순서는 노션 스키마의 옵션 순서, 값 없는 행은 맨 뒤 '기타'."""
    if did == site.curriculum_db or len(rows) < 6:
        return None, None
    ds = rows[0][0]
    cands = [(n, p) for n, p in (ds.get("properties") or {}).items()
             if p["type"] in ("select", "status") and n not in site.hide_props]
    cands.sort(key=lambda x: x[0] != "분류")
    for name, p in cands:
        def val(r):
            v = (r["properties"].get(name) or {}).get(p["type"])
            return v["name"] if v else None
        present = {val(r) for _, r in rows} - {None}
        if len(present) < 2:
            continue
        order = [(o["name"], o.get("color", "default")) for o in (p.get(p["type"]) or {}).get("options", [])]
        known = {n for n, _ in order}
        order += [(n, "default") for n in sorted(present - known)]
        groups = [(n, c, [(d, r) for d, r in rows if val(r) == n]) for n, c in order if n in present]
        rest = [(d, r) for d, r in rows if val(r) is None]
        if rest:
            groups.append(("기타", "default", rest))
        return name, groups
    return None, None


def render_db_list(site, did, rows, rend=None, skip_prop=None):
    if not rows:
        return '<p class="muted">아직 항목이 없어요.</p>'
    imgs = [first_image(site, r) for _, r in rows]
    gallery = sum(1 for x in imgs if x) >= max(1, len(rows) * 0.6)
    out = []
    for (ds, r), img in zip(rows, imgs):
        pid = r["id"]
        u = site.url(pid)
        linkable = site.has_content(pid) or any(
            (r["properties"].get(n) or {}).get("type") == "files" and r["properties"][n].get("files")
            for n, _ in visible_props(site, ds))
        title = esc(site.title(pid))
        if rend:
            rend.text.append(site.title(pid))
        chips, date = [], ""
        for name, typ in visible_props(site, ds):
            pv = r["properties"].get(name) or {}
            if name == skip_prop:
                continue
            if typ in ("select", "multi_select", "status"):
                chips.append(prop_value_html(site, pv))
            elif typ == "date" and not date:
                v = pv.get("date")
                if v and v.get("start"):
                    date = fmt_date(v["start"], v.get("end"))
        icon = site.icon_html(r.get("icon")) or ('<span class="ic">📄</span>' if not gallery else "")
        meta = f'<span class="row-meta">{"".join(chips)}{f"<span class=date>{esc(date)}</span>" if date else ""}</span>'
        if gallery:
            thumb = f'<span class="thumb"><img src="{esc(img)}" alt="" loading="lazy"></span>' if img else '<span class="thumb empty">📄</span>'
            inner = f'{thumb}<span class="card-body"><span class="row-title">{title}</span>{meta}</span>'
            out.append(f'<a class="card" href="{u}">{inner}</a>' if linkable else f'<div class="card">{inner}</div>')
        else:
            inner = f'{icon}<span class="row-title">{title}</span>{meta}'
            out.append(f'<a class="row" href="{u}">{inner}<span class="chev">›</span></a>' if linkable
                       else f'<div class="row nolink">{inner}</div>')
    cls = "gallery" if gallery else "rows"
    return f'<div class="{cls}">{"".join(out)}</div>'


def render_db_body(site, did, rows, rend=None):
    if did == site.curriculum_db:
        return render_schedule(site, did, rows)
    gprop, groups = group_rows(site, did, rows)
    if not groups:
        return render_db_list(site, did, rows, rend)
    out = []
    for name, color, grows in groups:
        out.append(f'<div class="db-group"><h4 class="db-group-title"><span class="dot t-{color if color in TAG_COLORS else "default"}"></span>'
                   f'{esc(name)}<span class="muted">{len(grows)}</span></h4>{render_db_list(site, did, grows, rend, skip_prop=gprop)}</div>')
    return "".join(out)


def render_db_block(site, did, rend=None):
    db = site.dbs.get(did)
    if not db or did not in site.public_dbs:
        return ""
    if db.get("error"):
        return '<div class="callout bg-gray"><span class="callout-icon">🔒</span><div class="callout-body">이 목록은 노션에서만 볼 수 있어요.</div></div>'
    if not any(r in site.public_pages for ds in db.get("data_sources", []) for r in ds["rows"]):
        return ""
    title = site.db_title(did)
    hid = rend.heading_id(title) if rend else slugify(title)
    if rend:
        rend.headings.append((3, title, hid))
    icon = site.icon_html(db.get("icon"))
    if not db.get("is_inline", True) and rend:
        n = sum(len(ds["rows"]) for ds in db.get("data_sources", []))
        return (f'<a class="page-link" id="{hid}" href="{site.url(did)}">{icon or "<span class=ic>🗂️</span>"}'
                f'<span>{esc(title)}</span><span class="muted">{n}개</span></a>')
    rows = db_rows(site, did)
    if not rows:
        return ""
    body = render_db_body(site, did, rows, rend)
    return f'<section class="db"><h3 class="db-title" id="{hid}">{icon}{esc(title)}</h3>{body}</section>'


# ───────────────────────── 레이아웃 ─────────────────────────

def nav_tree(site):
    """사이드바 트리: 섹션 → (루트 안의 DB·하위 페이지) → 행 → (행 안의 DB 행)."""
    def children_of_page(pid, depth):
        items = []
        if depth > 3:
            return items
        for b in iter_blocks(site.pages[pid]["blocks"]):
            if b["type"] == "child_database":
                did = norm_id(b["id"])
                if did in site.public_dbs and not site.dbs[did].get("error") and did != site.curriculum_db:
                    def row_items(rows):
                        return [{"id": r["id"], "label": site.title(r["id"]), "url": site.url(r["id"]),
                                 "kids": children_of_page(r["id"], depth + 2)}
                                for _, r in rows if site.has_content(r["id"])]
                    rows = db_rows(site, did)
                    _, groups = group_rows(site, did, rows)
                    if groups:
                        kids = []
                        for name, color, grows in groups:
                            gk = row_items(grows)
                            if gk:
                                kids.append({"id": f"{did}:{name}", "label": name, "url": None, "kids": gk,
                                             "count": len(gk), "color": color})
                    else:
                        kids = row_items(rows)
                    if kids:
                        items.append({"id": did, "label": site.db_title(did), "url": None, "kids": kids})
            elif b["type"] == "child_page":
                cid = norm_id(b["id"])
                if cid in site.public_pages:
                    items.append({"id": cid, "label": site.title(cid), "url": site.url(cid),
                                  "kids": children_of_page(cid, depth + 1)})
        return items
    tree = []
    for s in site.sections:
        kids = children_of_page(s["id"], 0)
        if len(kids) == 1 and kids[0]["url"] is None:
            kids = kids[0]["kids"]
        tree.append({"id": s["id"], "label": s["title"], "url": f"{site.base}/{s['slug']}/", "section": s, "kids": kids})
    return tree


def nav_html(site, tree, active_ids, current):
    def has_active(it):
        return it["id"] in active_ids or any(has_active(k) for k in it["kids"])

    def walk(items, depth):
        out = []
        for it in items:
            open_ = has_active(it)
            cur = ' aria-current="page"' if it["id"] == current else ""
            label = esc(it["label"])
            if depth == 0:
                sec = it["section"]
                icon = site.icon_html(site.pages[sec["id"]].get("icon"), "nav-ic")
                label = f"{icon}{label}"
            if it.get("count"):
                color = it.get("color", "default")
                label = (f'<span class="dot t-{color if color in TAG_COLORS else "default"}"></span>{label}'
                         f'<span class="nav-count">{it["count"]}</span>')
            link = (f'<a href="{it["url"]}"{cur}>{label}</a>' if it["url"]
                    else f'<span class="nav-group">{label}</span>')
            if it["kids"]:
                out.append(f'<li class="d{depth}"><details{" open" if open_ or depth == 0 else ""}>'
                           f'<summary>{link}</summary><ul>{walk(it["kids"], depth + 1)}</ul></details></li>')
            else:
                out.append(f'<li class="d{depth}">{link}</li>')
        return "".join(out)
    return f'<ul class="nav">{walk(tree, 0)}</ul>'


def page_shell(site, *, title, body, nav, crumbs="", toc="", desc="", body_cls=""):
    cfg = site.ed
    full_title = f"{title} · {cfg['short']}" if title != cfg["title"] else title
    synced = site.manifest.get("synced_at", "")
    synced_txt = ""
    if synced:
        d = datetime.strptime(synced, "%Y-%m-%dT%H:%M:%S%z").astimezone(KST)
        synced_txt = f"{d.month}월 {d.day}일 {fmt_time(d)} 노션 기준"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(desc or cfg['description'])}">
<meta property="og:title" content="{esc(full_title)}">
<meta property="og:description" content="{esc(desc or cfg['description'])}">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#191919" media="(prefers-color-scheme: dark)">
<link rel="icon" href="/assets/favicon.png" type="image/png">
<link rel="apple-touch-icon" href="/assets/logo.png">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<link rel="stylesheet" href="/assets/style.css?v={site.build_id}">
<script>try{{var t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script>
</head>
<body class="{body_cls}" data-base="{site.base}">
<header class="topbar">
  <button class="icon-btn menu-btn" type="button" aria-label="메뉴 열기" data-menu>☰</button>
  <a class="brand" href="{site.base}/"><img src="/assets/logo.png" alt="" class="brand-logo"><span>{esc(cfg['title'])}</span></a>
  <button class="search-btn" type="button" data-search-open><span>🔍 검색</span><kbd>⌘K</kbd></button>
  <button class="icon-btn" type="button" aria-label="다크 모드 전환" data-theme-toggle>◐</button>
</header>
<div class="layout">
  <aside class="sidebar" data-sidebar>
    <nav aria-label="전체 메뉴">{nav}</nav>
    <div class="sidebar-foot"><a href="{site.base}/">🏠 홈</a>{"".join(f'<a href="{esc(l["href"])}">{esc(l["label"])}</a>' for l in cfg.get("links", []))}</div>
  </aside>
  <div class="scrim" data-scrim></div>
  <main class="main">
    {crumbs}
    {body}
    <footer class="foot"><span>{esc(synced_txt)}</span><span>© 오토워커스</span></footer>
  </main>
  {toc}
</div>
<div class="search" data-search hidden>
  <div class="search-box" role="dialog" aria-label="검색">
    <input type="search" placeholder="강의 자료 검색 (예: 크몽, 쇼츠, API 키)" data-search-input autocomplete="off">
    <div class="search-results" data-search-results></div>
  </div>
</div>
<script src="/assets/app.js?v={site.build_id}" defer></script>
</body>
</html>"""


def crumbs_html(site, oid):
    parts = [f'<a href="{site.base}/">홈</a>']
    for aid, kind in site.ancestors(oid):
        if kind == "db":
            parts.append(f"<span>{esc(site.db_title(aid))}</span>")
        else:
            u = site.url(aid)
            label = site.section_of_root[aid]["title"] if aid in site.section_of_root else site.title(aid)
            parts.append(f'<a href="{u}">{esc(label)}</a>' if u else f"<span>{esc(label)}</span>")
    return f'<nav class="crumbs" aria-label="현재 위치">{" <i>›</i> ".join(parts)}</nav>'


def toc_html(headings):
    hs = [h for h in headings if h[0] <= 4]
    if len(hs) < 3:
        return ""
    items = "".join(f'<li class="l{lvl}"><a href="#{hid}">{esc(text)}</a></li>' for lvl, text, hid in hs)
    return f'<aside class="toc"><div class="toc-title">이 페이지 목차</div><ul>{items}</ul></aside>'


def props_block(site, page):
    """DB 행 페이지 상단 속성 표."""
    par = page.get("parent") or {}
    if par.get("type") != "database":
        return ""
    db = site.dbs.get(norm_id(par["id"])) or {}
    ds = next((d for d in db.get("data_sources", []) if page["id"] in d["rows"]), None)
    if not ds:
        return ""
    rows = []
    for name, typ in visible_props(site, ds):
        v = prop_value_html(site, page["properties"].get(name) or {})
        if v:
            rows.append(f"<dt>{esc(name)}</dt><dd>{v}</dd>")
    return f'<dl class="props">{"".join(rows)}</dl>' if rows else ""


def prev_next(site, page):
    par = page.get("parent") or {}
    if par.get("type") != "database":
        return ""
    did = norm_id(par["id"])
    rows = [r for _, r in db_rows(site, did) if site.has_content(r["id"])]
    ids = [r["id"] for r in rows]
    if page["id"] not in ids:
        return ""
    i = ids.index(page["id"])
    out = []
    if i > 0:
        out.append(f'<a class="pn prev" href="{site.url(ids[i-1])}"><small>← 이전</small><span>{esc(site.title(ids[i-1]))}</span></a>')
    else:
        out.append("<span></span>")
    if i < len(ids) - 1:
        out.append(f'<a class="pn next" href="{site.url(ids[i+1])}"><small>다음 →</small><span>{esc(site.title(ids[i+1]))}</span></a>')
    return f'<nav class="prevnext">{"".join(out)}</nav>'


# ───────────────────────── 페이지 생성 ─────────────────────────

def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def build_page(site, tree, pid, index):
    page = site.pages[pid]
    rend = Renderer(site, pid)
    body_html = rend.blocks(page["blocks"])
    is_root = pid in site.section_of_root
    title = site.section_of_root[pid]["title"] if is_root else site.title(pid)
    full_title = site.title(pid)
    icon = site.icon_html(page.get("icon"), "page-icon")
    cover = ""
    if page.get("cover"):
        cu = site.media_url(page["cover"])
        if cu:
            cover = f'<div class="cover"><img src="{esc(cu)}" alt=""></div>'
    edited = fmt_date(page["last_edited_time"], with_time=False)
    head = (f'{cover}<header class="page-head">{icon}<h1>{esc(full_title)}</h1>'
            f'{props_block(site, page)}</header>')
    if not body_html.strip() and not props_block(site, page):
        body_html = '<p class="muted">아직 내용이 없어요.</p>'
    article = f'<article class="content">{head}{body_html}</article>{prev_next(site, page)}<p class="edited">마지막 수정 {esc(edited)}</p>'
    active = {pid} | {a for a, _ in site.ancestors(pid)}
    html_ = page_shell(site, title=title, body=article, nav=nav_html(site, tree, active, pid),
                       crumbs=crumbs_html(site, pid), toc=toc_html(rend.headings))
    url = site.url(pid)
    write(OUT / url.strip("/") / "index.html", html_)
    sec = site.section_for.get(pid, {})
    path = [site.db_title(a) if k == "db" else site.title(a) for a, k in site.ancestors(pid)[1:]]
    path = [x for x in path if x.strip() != sec.get("title", "")]
    index.append({"t": full_title, "u": url, "s": sec.get("title", ""), "p": " › ".join(path),
                  "x": re.sub(r"\s+", " ", " ".join(rend.text))[:6000]})


def build_db_page(site, tree, did, index):
    rend = Renderer(site, did)
    rows = db_rows(site, did)
    body = render_db_body(site, did, rows, rend)
    title = site.db_title(did)
    icon = site.icon_html(site.dbs[did].get("icon"), "page-icon")
    article = f'<article class="content"><header class="page-head">{icon}<h1>{esc(title)}</h1></header>{body}</article>'
    active = {did} | {a for a, _ in site.ancestors(did)}
    write(OUT / site.url(did).strip("/") / "index.html",
          page_shell(site, title=title, body=article, nav=nav_html(site, tree, active, did), crumbs=crumbs_html(site, did)))


def upcoming(site, n=4):
    did = site.curriculum_db
    if not did or did not in site.dbs:
        return []
    now = datetime.now(KST)
    items = []
    for ds, r in db_rows(site, did):
        d = row_date(r, ds)
        if d and d.date() >= now.date():
            items.append((d, ds, r))
    return sorted(items, key=lambda x: x[0])[:n]


def build_home(site, tree):
    cfg = site.ed
    cards = []
    for s in site.sections:
        page = site.pages[s["id"]]
        icon = site.icon_html(page.get("icon"), "sec-icon") or '<span class="sec-icon">📘</span>'
        n = sum(1 for p in site.public_pages if site.section_for.get(p, {}).get("slug") == s["slug"] and site.has_content(p))
        cards.append(f'<a class="sec-card" href="{site.base}/{s["slug"]}/">{icon}<span class="sec-name">{esc(s["title"])}</span>'
                     f'<span class="sec-tag">{esc(s.get("tagline", ""))}</span><span class="sec-count">자료 {n}개 →</span></a>')
    # 다가오는 일정
    up = upcoming(site)
    now = datetime.now(KST).date()
    up_html = ""
    if up:
        lis = []
        for d, ds, r in up:
            dd = (d.date() - now).days
            dday = "오늘" if dd == 0 else f"D-{dd}"
            chips = "".join(prop_value_html(site, r["properties"].get(nm, {}))
                            for nm, typ in visible_props(site, ds) if typ == "select")
            lis.append(f'<li><span class="dday{" today" if dd == 0 else ""}">{dday}</span>'
                       f'<span class="up-date">{d.month}/{d.day} ({WEEKDAYS[d.weekday()]})</span>'
                       f'<span class="up-title">{esc(site.title(r["id"]))}</span><span class="chips">{chips}</span></li>')
        up_html = (f'<section class="home-block"><div class="hb-head"><h2>다가오는 일정</h2>'
                   f'<a href="{site.base}/common/#{slugify(site.db_title(site.curriculum_db))}">전체 일정 →</a></div>'
                   f'<ul class="upcoming">{"".join(lis)}</ul></section>')
    # 최근 업데이트
    recent = sorted((p for p in site.public_pages if p not in site.section_of_root and site.has_content(p)),
                    key=lambda p: site.pages[p]["last_edited_time"], reverse=True)[:8]
    rec_html = "".join(
        f'<a class="row" href="{site.url(p)}">{site.icon_html(site.pages[p].get("icon")) or "<span class=ic>📄</span>"}'
        f'<span class="row-title">{esc(site.title(p))}</span><span class="row-meta"><span class="muted">'
        f'{esc(site.section_for.get(p, {}).get("title", ""))}</span><span class="date">'
        f'{esc(fmt_date(site.pages[p]["last_edited_time"], with_time=False))}</span></span><span class="chev">›</span></a>'
        for p in recent)
    # 3원칙 콜아웃은 공통 페이지 첫 콜아웃을 그대로 가져온다
    principle = ""
    if site.sections:
        first = next((b for b in site.pages[site.sections[0]["id"]]["blocks"] if b["type"] == "callout"), None)
        if first:
            principle = Renderer(site, None).block(first)
    title_txt, desc_txt = cfg["title"], cfg["description"]
    notice = ""
    if cfg.get("notice"):
        notice = (f'<div class="callout bg-purple"><span class="callout-icon">🔒</span><div class="callout-body">'
                  f'<b>{esc(cfg["notice"])}</b> 즐겨찾기 해두시면 편해요.</div></div>')
    else:
        notice = ('<div class="callout bg-gray"><span class="callout-icon">🔖</span><div class="callout-body">'
                  '수강생에게만 안내된 주소예요. 즐겨찾기 해두시면 편해요.</div></div>')
    body = f"""
<section class="hero">
  <h1>{esc(title_txt)}</h1>
  <p>{esc(desc_txt)}</p>
  <button class="hero-search" type="button" data-search-open>🔍 무엇이든 검색해 보세요 <kbd>⌘K</kbd></button>
</section>
{notice}
<div class="sec-cards">{''.join(cards)}</div>
{principle}
{up_html}
<section class="home-block"><div class="hb-head"><h2>최근 업데이트</h2></div><div class="rows">{rec_html}</div></section>
"""
    write(OUT / site.base.strip("/") / "index.html", page_shell(site, title=cfg["title"], body=body,
                                         nav=nav_html(site, tree, set(), None), body_cls="home"))


def root_page(title, heading, text):
    """루트(/)와 404 — 어떤 판의 주소도 노출하지 않는 안내 페이지."""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{esc(title)}</title>
<link rel="icon" href="/assets/favicon.png" type="image/png">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<link rel="stylesheet" href="/assets/style.css">
<style>.gate{{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:24px;text-align:center}}
.gate img{{width:96px;height:96px;object-fit:contain}}.gate h1{{margin:4px 0 0;font-size:26px}}.gate p{{margin:0;color:var(--text-soft)}}</style>
</head><body><main class="gate"><img src="/assets/logo.png" alt=""><h1>{esc(heading)}</h1><p>{esc(text)}</p></main></body></html>"""


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def edition_base(salt, ed):
    """판 비밀 경로 — 저장소엔 key만 있고 LINK_SALT가 없으면 계산할 수 없다."""
    return f"/{ed['prefix']}-" + hmac.new(salt.encode(), ed["key"].encode(), hashlib.sha256).hexdigest()[:12]


def build_edition(site, build_id):
    site.build_id = build_id
    tree = nav_tree(site)
    index = []
    for pid in sorted(site.public_pages):
        build_page(site, tree, pid, index)
    for did in sorted(site.public_dbs):
        db = site.dbs[did]
        if not db.get("error") and not db.get("is_inline", True):
            build_db_page(site, tree, did, index)
    build_home(site, tree)
    write(OUT / site.base.strip("/") / "search-index.json", json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    return tree


def main():
    load_env()
    salt = os.environ.get("LINK_SALT", "").strip()
    if not salt:
        raise SystemExit("LINK_SALT가 없습니다 — 모든 판이 비밀 경로라 LINK_SALT 없이는 빌드하지 않습니다.")
    cfg = json.loads((ROOT / "config.json").read_text())
    build_id = hashlib.sha1(str(datetime.now(timezone.utc).timestamp()).encode()).hexdigest()[:8]
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    sites = []
    for ed in cfg["editions"]:
        site = Site(dict(ed, base=edition_base(salt, ed)))
        if not site.sections:
            print(f"⚠️ {ed['title']}: 노션 페이지가 캐시에 없어 건너뜀 (통합 연결 확인)")
            continue
        sites.append(site)
    if not sites:
        raise SystemExit("빌드할 판이 없습니다 — 먼저 python3 sync.py 를 실행하세요.")
    used = set()
    for site in sites:
        build_edition(site, build_id)
        used |= site.used_files
    write(OUT / "index.html", root_page("오토워커 위키", "오토워커 위키",
                                        "수강생에게 안내된 주소로 접속해 주세요."))
    write(OUT / "404.html", root_page("찾을 수 없음", "페이지를 찾을 수 없어요",
                                      "주소가 바뀌었거나 없는 페이지예요. 안내받은 주소를 다시 확인해 주세요."))
    # 정적 자산 + 사용된 노션 파일만 복사 (비공개 페이지 파일이 섞이지 않도록)
    shutil.copytree(STATIC, OUT / "assets")
    for rel in used:
        src = CACHE / rel
        if src.exists():
            dst = OUT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    legacy = ROOT / "legacy" / "gen1"
    if legacy.exists():
        shutil.copytree(legacy, OUT / "gen1")
    for f in ("CNAME", ".nojekyll"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, OUT / f)
    write(OUT / "robots.txt", "User-agent: *\nDisallow: /\n")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    for site in sites:
        print(f"{site.ed['title']}: 페이지 {len(site.public_pages)}개 · DB {len(site.public_dbs)}개")
    print(f"빌드 완료 → _site/ · 파일 {len(used)}개 · {size / 1e6:.1f}MB")
    # 공개 저장소의 Actions 로그에 비밀 주소가 찍히지 않도록 로컬에서만 출력
    if os.environ.get("CI") != "true":
        for site in sites:
            print(f"  {site.ed['title']}: https://{cfg['domain']}{site.base}/")


if __name__ == "__main__":
    main()
