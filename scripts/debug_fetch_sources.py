"""One-off: snapshot candidate script-source pages (for parser development)."""
import json, pathlib, re, time, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; ScriptSourceProbe/1.0; academic research)"}
out = pathlib.Path("debug_html"); out.mkdir(exist_ok=True)
PAGES = [
    "https://scriptpdf.com/full-list/",
    "https://bulletproofscreenwriting.tv/free-screenplays-download/",
    "https://indiefilmhustle.com/free-screenplays-download/",
    "https://nofilmschool.com/2023-academy-award-screenplays",
    "https://colehaddon.substack.com/p/2024s-award-season-screenplays-to",
    "https://screenplayhowto.com/category/movie-scripts-pdf/",
    "https://www.writing.ninja/free-screenplay-downloads-pdf/",
    "https://8flix.com/post-sitemap.xml",
    "https://www.simplyscripts.com/category/movie-scripts/oscar-contenders/",
    "https://nofilmschool.com/sitemap.xml",
    "https://screenplayhowto.com/sitemap.xml",
    "https://scriptpdf.com/sitemap.xml",
]
meta = {}
for u in PAGES:
    name = re.sub(r"[^a-z0-9]+", "_", u.lower().split("://", 1)[1])[:90] + ".html"
    try:
        r = requests.get(u, headers=H, timeout=60)
        meta[name] = {"url": u, "status": r.status_code, "final": r.url, "len": len(r.content)}
        if r.ok:
            (out / name).write_bytes(r.content[:15_000_000])
    except Exception as e:
        meta[name] = {"url": u, "error": str(e)}
    time.sleep(1)
(out / "_meta.json").write_text(json.dumps(meta, indent=1))
print(json.dumps(meta, indent=1))
