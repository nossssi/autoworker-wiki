#!/usr/bin/env python3
"""빌드 결과 점검: 각 판 폴더 밖(루트·다른 판)에 비밀 경로나 다른 판 전용 페이지가 새지 않았는지."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build

build.load_env()
salt = os.environ["LINK_SALT"]
cfg = json.loads((build.ROOT / "config.json").read_text())
sites = [build.Site(dict(e, base=build.edition_base(salt, e))) for e in cfg["editions"]]
sites = [s for s in sites if s.sections]
bad = []
for s in sites:
    mine = s.base.strip("/")
    own_ids = {i.replace("-", "") for i in s.public_pages}
    for other in sites:
        if other is s:
            continue
        # 다른 판의 비밀 경로가 이 판 HTML에 있으면 안 됨
        for root, _, files in os.walk(build.OUT / mine):
            for f in files:
                if f.endswith((".html", ".json")):
                    txt = open(os.path.join(root, f), encoding="utf-8").read()
                    if other.base.strip("/") in txt:
                        bad.append((f"{s.ed['title']} 안에 {other.ed['title']} 경로", os.path.join(root, f)))
                    foreign = {i.replace("-", "") for i in other.public_pages} - own_ids
                    hit = next((i for i in foreign if f"/p/{i}/" in txt), None)
                    if hit:
                        bad.append((f"{s.ed['title']} 안에 {other.ed['title']} 전용 페이지 링크", os.path.join(root, f)))
# 판 폴더 밖(루트 페이지·gen1 등)에 어떤 판 경로도 없어야 함
bases = [s.base.strip("/") for s in sites]
for root, dirs, files in os.walk(build.OUT):
    rel = os.path.relpath(root, build.OUT)
    if rel.split(os.sep)[0] in bases:
        continue
    for f in files:
        if f.endswith((".html", ".json", ".txt")):
            txt = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            for b in bases:
                if b in txt:
                    bad.append(("판 폴더 밖에 비밀 경로", os.path.join(root, f)))
def mask(path):  # CI 로그(공개)에 비밀 경로가 찍히지 않도록 가린다
    for b in bases:
        path = path.replace(b, "<판>")
    return path


print("누출 없음 ✅" if not bad else "\n".join(f"❌ {m}: {mask(p)}" for m, p in bad[:20]))
sys.exit(1 if bad else 0)
