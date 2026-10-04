"""One-off: snapshot candidate script-source pages (for parser development)."""
import json, pathlib, re, time, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; ScriptSourceProbe/1.0; academic research)"}
out = pathlib.Path("debug_html"); out.mkdir(exist_ok=True)
PAGES = [
    "https://www.waltdisneystudiosawards.com/", "https://www.waltdisneystudiosawards.com/scripts/",
    "https://www.wbawards.com/", "https://www.wbawards.com/scripts/",
    "https://universalpicturesawards.com/", "https://www.sonypicturesawards.com/",
    "https://www.sonyclassics.com/awards-information/", "https://paramountpicturesfyc.com/",
    "https://amazonmgmstudiosguilds.com/", "https://film.netflixawards.com/", "https://a24awards.com/",
    "https://www.focusfeaturesguilds.com/", "https://lionsgateawards.com/", "https://www.searchlightpictures.com/fyc/",
    "https://www.appletvplusfyc.com/", "https://neonawards.com/", "https://www.mubiawards.com/",
    "https://bleeckerstreetmedia.com/guilds", "https://www.ifcfilmsawards.com/", "https://www.magpictures.com/fyc/",
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
