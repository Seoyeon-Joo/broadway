"""One-off: snapshot candidate script-source sites (robots, sitemaps, sample pages) for parser development."""
import json, pathlib, re, time, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; ScriptSourceProbe/1.0; academic research)"}
out = pathlib.Path("debug_html"); out.mkdir(exist_ok=True)
sites = ["https://www.scriptslug.com", "https://laper.ai", "https://8flix.com", "https://www.screenplaydb.com",
         "https://thescriptsavant.com", "https://moviescriptsandscreenplays.com", "https://www.scripts.com",
         "https://deadline.com"]
meta = {}
def save(name, url):
    try:
        r = requests.get(url, headers=H, timeout=60)
        meta[name] = {"url": url, "status": r.status_code, "final": r.url, "len": len(r.content),
                      "ctype": r.headers.get("content-type")}
        if r.ok:
            (out / name).write_bytes(r.content[:15_000_000])
        return r
    except Exception as e:
        meta[name] = {"url": url, "error": str(e)}
    finally:
        time.sleep(1)
for s in sites:
    host = re.sub(r"https?://(www\.)?", "", s)
    r = save(f"{host}__robots.txt", s + "/robots.txt")
    maps = re.findall(r"(?im)^sitemap:\s*(\S+)", r.text) if r is not None and r.ok else []
    maps = maps or [s + "/sitemap.xml", s + "/sitemap_index.xml"]
    for i, m in enumerate(maps[:3]):
        r2 = save(f"{host}__sitemap{i}.xml", m)
        # follow first-level child sitemaps that look script-related (or all if few)
        if r2 is not None and r2.ok and b"<sitemapindex" in r2.content[:2000]:
            kids = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", r2.text)
            pick = [k for k in kids if re.search(r"script|screenplay|post|movie|film", k, re.I)] or kids
            if "deadline" in host:
                pick = [k for k in kids if "post-sitemap" in k][-6:]
            for j, k in enumerate(pick[:12]):
                save(f"{host}__sitemap{i}_{j}.xml", k)
    save(f"{host}__home.html", s + "/")
for name, url in {"scriptslug__oppenheimer.html": "https://www.scriptslug.com/script/oppenheimer-2023",
                  "scriptslug__scripts.html": "https://www.scriptslug.com/scripts",
                  "scriptslug__film2024.html": "https://www.scriptslug.com/scripts/medium/film?year=2024",
                  "laper__oppenheimer.html": "https://laper.ai/screenplays/oppenheimer/",
                  "8flix__home2.html": "https://8flix.com/screenplays/"}.items():
    save(name, url)
(out / "_meta.json").write_text(json.dumps(meta, indent=1))
print(json.dumps(meta, indent=1)[:4000])
