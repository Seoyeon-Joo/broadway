"""One-off: snapshot candidate script-source pages (for parser development)."""
import json, pathlib, re, time, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; ScriptSourceProbe/1.0; academic research)"}
out = pathlib.Path("debug_html"); out.mkdir(exist_ok=True)
PAGES = [
    "https://www.concordtheatricals.com/robots.txt", "https://www.concordtheatricals.com/sitemap.xml",
    "https://www.mtishows.com/robots.txt", "https://www.mtishows.com/sitemap.xml",
    "https://www.broadwaylicensing.com/robots.txt", "https://www.broadwaylicensing.com/sitemap.xml",
    "https://www.dramatists.com/robots.txt", "https://www.dramatists.com/sitemap.xml",
    "https://www.theatricalrights.com/robots.txt", "https://www.theatricalrights.com/sitemap.xml",
    "https://www.halleonard.com/robots.txt", "https://www.halleonard.com/sitemap.xml",
    "https://www.tcg.org/robots.txt", "https://www.tcg.org/sitemap.xml",
    "https://www.musicnotes.com/robots.txt",
    "https://archive.org/advancedsearch.php?q=title%3A%28%22kimberly+akimbo%22%29&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=mediatype&rows=30&output=json",
    "https://archive.org/advancedsearch.php?q=title%3A%28%22a+strange+loop%22%29&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=mediatype&rows=30&output=json",
    "https://archive.org/advancedsearch.php?q=title%3A%28%22vocal+selections%22%29+AND+mediatype%3Atexts&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=year&rows=100&sort%5B%5D=year+desc&output=json",
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
