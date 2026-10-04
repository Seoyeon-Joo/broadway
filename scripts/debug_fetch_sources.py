"""One-off: save raw HTML of script-source index pages for parser development."""
import json, pathlib, requests, time
from bs4 import BeautifulSoup
H = {"User-Agent": "Mozilla/5.0 (compatible; ScriptSourceProbe/1.0; academic research)"}
out = pathlib.Path("debug_html"); out.mkdir(exist_ok=True)
pages = {"ss_movie-screenplays": "https://www.simplyscripts.com/movie-screenplays.html",
         "ss_movie-scripts": "https://www.simplyscripts.com/movie-scripts.html",
         "ss_oscar_winners": "https://www.simplyscripts.com/oscar_winners.html",
         "sb_tag_p1": "https://www.studiobinder.com/tag/free-screenplays/",
         "sb_thriller": "https://www.studiobinder.com/blog/thriller-scripts/",
         "sb_best": "https://www.studiobinder.com/blog/best-free-movie-scripts-online/",
         "sb_goodfellas": "https://www.studiobinder.com/blog/goodfellas-script-screenplay-pdf-download/"}
for n in range(88, 99):
    pages[f"ss_oscar_{n}"] = f"https://www.simplyscripts.com/oscar-screenplays-{n}.html"
meta = {}
for k, u in pages.items():
    try:
        r = requests.get(u, headers=H, timeout=40)
        meta[k] = {"url": u, "status": r.status_code, "final": r.url, "len": len(r.content)}
        if r.ok:
            (out / f"{k}.html").write_bytes(r.content)
    except Exception as e:
        meta[k] = {"url": u, "error": str(e)}
    time.sleep(1)
# follow a few bit.ly links from the thriller page
try:
    soup = BeautifulSoup((out / "sb_thriller.html").read_bytes(), "html.parser")
    bl = [a["href"] for a in soup.find_all("a", href=True) if "bit.ly" in a["href"]][:8]
    res = []
    for b in bl:
        try:
            r = requests.get(b, headers=H, timeout=40, allow_redirects=True, stream=True)
            res.append({"bitly": b, "final": r.url, "status": r.status_code,
                        "ctype": r.headers.get("content-type"), "hist": [h.url for h in r.history]})
            r.close()
        except Exception as e:
            res.append({"bitly": b, "error": str(e)})
        time.sleep(1)
    meta["_bitly"] = res
except Exception as e:
    meta["_bitly_err"] = str(e)
(out / "_meta.json").write_text(json.dumps(meta, indent=1))
print(json.dumps(meta, indent=1)[:3000])
