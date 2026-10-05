"""One-off: snapshot candidate script-source pages (for parser development)."""
import json, pathlib, re, time, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; ScriptSourceProbe/1.0; academic research)"}
out = pathlib.Path("debug_html"); out.mkdir(exist_ok=True)
PAGES = [
    "https://scripts-onscreen.com/robots.txt", "https://scripts-onscreen.com/sitemap.xml",
    "https://scripts-onscreen.com/sitemap_index.xml", "https://scripts-onscreen.com/movie-sitemap.xml",
    "https://scripts-onscreen.com/movie/dune-part-two-script-links/",
    "https://scripts-onscreen.com/movie/oppenheimer-script-links/",
    "https://scrapsfromtheloft.com/robots.txt", "https://scrapsfromtheloft.com/sitemap_index.xml",
    "https://scrapsfromtheloft.com/movies/godzilla-minus-one-transcript/",
    "https://subslikescript.com/robots.txt", "https://subslikescript.com/sitemap.xml",
    "https://subslikescript.com/movies", "https://subslikescript.com/movies_letter-D",
    "https://www.springfieldspringfield.co.uk/robots.txt",
    "https://www.springfieldspringfield.co.uk/movie_scripts.php?order=D",
    "https://www.springfieldspringfield.co.uk/movie_script.php?movie=dune-part-two",
    "https://archive.org/advancedsearch.php?q=title%3A%28%22bullet+train%22%29+AND+%28screenplay+OR+script%29&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=year&fl%5B%5D=mediatype&rows=20&output=json",
    "https://archive.org/advancedsearch.php?q=%28screenplay%29+AND+mediatype%3Atexts+AND+year%3A%5B2020+TO+2026%5D&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=year&rows=50&output=json",
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
