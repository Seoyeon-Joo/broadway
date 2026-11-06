"""극장 소개글(Playbill/Broadway.com/StageAgent 등) 원문 수집. 후보 텍스트를 전부 저장해 사람이 고른다."""
import re, json, requests, pandas as pd
from bs4 import BeautifulSoup
T = {
 50: ("The Minutes", ["https://stageagent.com/shows/play/30145/the-minutes","https://broadwaydirect.com/show/the-minutes/","https://www.theatermania.com/shows/washington-dc/the-minutes_1829040/","https://playbill.com/article/tracy-letts-the-minutes-opens-on-broadway-april-17"]),
 78: ("The Collaboration", ["https://playbill.com/production/the-collaboration-broadway-samuel-j-friedman-theatre-2022","https://www.newyorktheatreguide.com/show/the-collaboration","https://broadwayplayhome.com/shows/collaboration.htm"]),
 102: ("The Cottage", ["https://playbill.com/production/the-cottage-broadway-helen-hayes-theatre-2023","https://playwright.co/plays/the-cottage-sandy-rustin","https://www.concordtheatricals.co.uk/p/98976/the-cottage"]),
 212: ("The Balusters", ["https://www.broadway.com/shows/the-balusters/story/","https://broadwayplayhome.com/shows/balusters.htm","https://broadwaypodcastnetwork.com/shows/the-balusters"]),
}
H = {"User-Agent": "Mozilla/5.0 (research script; academic thesis)"}
rows = []
for wid, (title, urls) in T.items():
    for u in urls:
        try:
            r = requests.get(u, headers=H, timeout=30)
            s = BeautifulSoup(r.text, "html.parser")
            cands = []
            for m in s.find_all("meta"):
                if (m.get("name") in ("description",) or m.get("property") in ("og:description",)) and m.get("content"):
                    cands.append(("meta:" + (m.get("name") or m.get("property")), m["content"]))
            for sc in s.find_all("script", type="application/ld+json"):
                try:
                    j = json.loads(sc.string or "")
                    for o in (j if isinstance(j, list) else [j]):
                        if isinstance(o, dict) and o.get("description"): cands.append(("jsonld", o["description"]))
                except Exception: pass
            for p in s.find_all(["p", "div"]):
                t = p.get_text(" ", strip=True)
                if 40 <= len(t.split()) <= 400 and p.name == "p": cands.append(("p", t))
            seen = set()
            for k, t in cands:
                t = re.sub(r"\s+", " ", t).strip()
                if t in seen: continue
                seen.add(t)
                rows.append(dict(work_id=wid, title=title, url=u, http=r.status_code, kind=k, words=len(t.split()), text=t))
            if not cands: rows.append(dict(work_id=wid, title=title, url=u, http=r.status_code, kind="none", words=0, text=""))
        except Exception as e:
            rows.append(dict(work_id=wid, title=title, url=u, http=-1, kind="error", words=0, text=str(e)[:200]))
df = pd.DataFrame(rows)
df.to_excel("data/theater_synopses_candidates.xlsx", index=False)
print(df.groupby(["title", "url"]).agg(n=("kind", "size"), http=("http", "first")))
