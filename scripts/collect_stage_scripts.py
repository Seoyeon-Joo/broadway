"""
Broadway show scripts (and musical scores) - free / legitimate sources.

Input
  data/shows_list.csv      show_id, show, genre, ..., find   ("O"/"S" = already found by hand -> skipped)
  data/shows_creators.csv  show, creators (";"-separated playwright / book / composer / lyricist names)
                           Used to make sure a hit is the right work (titles like "Company", "Home", "Art"
                           are otherwise ambiguous).

Sources (per show)
  1. archive.org  - texts whose title matches the show and whose creator matches a listed creator.
                    Freely downloadable items -> PDF/TXT saved. Lending-library items (borrow with a free
                    archive.org account) -> link recorded.
  2. Project Gutenberg (gutendex API) - public-domain plays (txt saved).
  3. Open Library - published editions (acting editions, play collections, vocal selections):
                    publisher, year, ISBN, ebook access (public / borrowable).
  4. Licensing houses - MTI and Theatrical Rights Worldwide show pages (perusal scripts / scores are
                    available there to registered users).
For musicals every source is also checked for scores (vocal score / vocal selections / piano-vocal).

Output
  data/stage_scripts.xlsx         one row per show: status, downloaded files, borrowable links, editions, licensing
  data/stage_files/<id>_<show>_<script|score>.<ext>
"""

from __future__ import annotations

import argparse
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (compatible; StageScriptFinder/1.0; academic research)"})
NON_SCRIPT = {"Jonas Brothers": "concert", "Ben Platt: Live at the Palace": "concert",
              "Celebrity Autobiography": "comedy reading", "El Mago Pop": "magic show",
              "Rob Lake Magic with Special Guests The Muppets": "magic show",
              "Mike Birbiglia: The Old Man & the Pool": "stand-up solo",
              "Jeff Ross: Take a Banana For the Ride": "stand-up solo"}
SCORE_RX = re.compile(r"vocal|score|piano|selections|songbook|sheet music", re.I)
SCRIPT_RX = re.compile(r"libretto|script|play|acting edition|book of the musical", re.I)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", " and ")).strip()


ALIAS = {"Pirates! The Penzance Musical": "Pirates of Penzance", "Cats: The Jellicle Ball": "Cats",
         "Melissa Etheridge: My Window": "My Window", "Stephen Sondheim's Old Friends": "Old Friends",
         "David Byrne's American Utopia": "American Utopia", "The Who's Tommy": "Tommy",
         "Two Strangers (Carry a Cake Across New York)": "Two Strangers"}


def core_title(show: str) -> str:
    if show in ALIAS:
        return ALIAS[show]
    t = re.sub(r"\(.*?\)", "", show)
    if ":" in t and len(t.split(":")[0].strip()) >= 3:
        t = t.split(":")[0]
    t = re.split(r"[-:,]\s*(?:the )?(?:[a-z ]+ musical|a new musical|the life and times.*)$", t, flags=re.I)[0]
    t = re.sub(r"\b(the musical|a new musical|musical)\b!?", "", t, flags=re.I)
    return t.strip(" -:,!") or show


def creator_match(names, creators: list[str]) -> bool:
    text = fold(" ".join(names if isinstance(names, list) else [str(names or "")]))
    for c in creators:
        last = fold(c).split()[-1] if fold(c) else ""
        if len(last) >= 3 and re.search(rf"\b{re.escape(last)}\b", text):
            return True
    return False


def title_match(title: str, show: str) -> bool:
    a, b = fold(title), fold(core_title(show))
    return bool(b) and (a.startswith(b) or f" {b} " in f" {a} " or a == b)


def get(url, **kw):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            return r
        except Exception:  # noqa: BLE001
            time.sleep(5)
    return None


# ------------------------------------------------------------------ archive.org
def archive_items(show: str, creators: list[str]) -> list[dict]:
    q = f'title:("{core_title(show)}") AND mediatype:texts'
    r = get("https://archive.org/advancedsearch.php",
            params={"q": q, "fl[]": ["identifier", "title", "creator", "year", "subject"], "rows": 50, "output": "json"})
    if r is None or not r.ok:
        return []
    out = []
    for d in r.json().get("response", {}).get("docs", []):
        t = str(d.get("title", ""))
        if not title_match(t, show):
            continue
        cr = d.get("creator", [])
        if not creator_match(cr if isinstance(cr, list) else [cr], creators):
            continue
        out.append({"id": d["identifier"], "title": t, "year": d.get("year", ""),
                    "kind": "score" if SCORE_RX.search(t) else "script"})
    return out


def archive_files(ident: str) -> tuple[bool, list[str]]:
    r = get(f"https://archive.org/metadata/{ident}")
    if r is None or not r.ok:
        return True, []
    meta = r.json()
    restricted = str(meta.get("metadata", {}).get("access-restricted-item", "")).lower() == "true"
    files = [f["name"] for f in meta.get("files", [])
             if f.get("name", "").lower().endswith((".pdf", ".txt", ".epub"))
             and not f["name"].lower().endswith(("_djvu.txt", "_hocr.txt", "_chocr.html"))
             and f.get("private") != "true"]
    files.sort(key=lambda n: (not n.lower().endswith(".pdf"), len(n)))
    return restricted, files


# ------------------------------------------------------------------ Gutenberg
def gutenberg(show: str, creators: list[str]) -> list[dict]:
    r = get("https://gutendex.com/books", params={"search": core_title(show)})
    if r is None or not r.ok:
        return []
    out = []
    for b in r.json().get("results", [])[:10]:
        if not title_match(b.get("title", ""), show):
            continue
        if not creator_match([a.get("name", "") for a in b.get("authors", [])], creators):
            continue
        fm = b.get("formats", {})
        url = next((u for k, u in fm.items() if k.startswith("text/plain")), "") or fm.get("application/pdf", "")
        if url:
            out.append({"title": b["title"], "url": url, "id": b["id"]})
    return out


# ------------------------------------------------------------------ Open Library
def openlibrary(show: str, creators: list[str]) -> list[dict]:
    r = get("https://openlibrary.org/search.json",
            params={"title": core_title(show), "limit": 30,
                    "fields": "key,title,author_name,first_publish_year,publisher,isbn,ebook_access,ia"})
    if r is None or not r.ok:
        return []
    out = []
    for d in r.json().get("docs", []):
        if not title_match(d.get("title", ""), show) or not creator_match(d.get("author_name", []), creators):
            continue
        out.append({"title": d.get("title"), "year": d.get("first_publish_year", ""),
                    "publishers": "; ".join((d.get("publisher") or [])[:4]),
                    "isbn": (d.get("isbn") or [""])[0], "ebook_access": d.get("ebook_access", ""),
                    "ia": (d.get("ia") or [""])[0], "url": f"https://openlibrary.org{d['key']}",
                    "kind": "score" if SCORE_RX.search(d.get("title", "")) else "script"})
    return out


# ------------------------------------------------------------------ licensing houses
def licensing_index() -> dict:
    idx = {}
    for name, sitemaps, pat in [
        ("MTI", [f"http://live-mti-drupal-partners.pantheonsite.io/sitemap.xml?page={i}" for i in (1, 2, 3)],
         r"https?://[^<\s]*?/(?:show|shows)/([^</\s?#]+)/?"),
        ("TRW", ["https://www.theatricalrights.com/trw-shows-sitemap.xml"], r"https?://[^<\s]*?/(?:show|shows)/([^</\s?#]+)/?"),
    ]:
        n = 0
        for sm in sitemaps:
            r = get(sm)
            if r is None or not r.ok:
                continue
            for m in re.finditer(r"<loc>\s*(" + pat + r")\s*</loc>", r.text):
                url, slug = m.group(1), m.group(2).strip("/")
                if name == "MTI":
                    url = re.sub(r"^https?://[^/]+", "https://www.mtishows.com", url)
                idx.setdefault(fold(slug.replace("-", " ")), []).append((name, url))
                n += 1
            time.sleep(1)
        print(f"  {name}: {n} show pages")
    return idx


def licensing_hits(show: str, idx: dict) -> list[str]:
    key = fold(core_title(show))
    hits = idx.get(key, []) + [h for k, v in idx.items() if k.startswith(key + " ") for h in v]
    return [f"{n}: {u}" for n, u in hits[:3]]


# ------------------------------------------------------------------ main
def save(url: str, dest: Path) -> int:
    r = get(url)
    if r is None or not r.ok or len(r.content) < 5000:
        return 0
    dest.write_bytes(r.content)
    return len(r.content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shows", default="data/shows_list.csv")
    ap.add_argument("--creators", default="data/shows_creators.csv")
    ap.add_argument("--output", default="data/stage_scripts.xlsx")
    ap.add_argument("--files", default="data/stage_files")
    a = ap.parse_args()

    shows = pd.read_csv(a.shows)
    creators = {r.show: [c.strip() for c in str(r.creators).split(";") if c.strip()]
                for r in pd.read_csv(a.creators).itertuples()}
    fdir = Path(a.files)
    fdir.mkdir(parents=True, exist_ok=True)
    print("Licensing catalogs...")
    lic = licensing_index()

    rows = []
    for s in shows.itertuples():
        show, genre, found = str(s.show), str(s.genre), str(getattr(s, "find", "") or "")
        row = {"show_id": s.show_id, "show": show, "genre": genre, "your_find": found if found != "nan" else ""}
        if row["your_find"] in ("O", "S"):
            rows.append({**row, "status": "already found (skipped)"})
            continue
        if show in NON_SCRIPT:
            rows.append({**row, "status": "no script expected", "note": NON_SCRIPT[show]})
            continue
        cr = creators.get(show, [])
        safe = re.sub(r'[\\/:*?"<>|]+', "_", f"{s.show_id}_{show}").strip(" ._")[:120]
        got = {"script": [], "score": []}
        borrow = {"script": [], "score": []}

        for it in archive_items(show, cr):
            restricted, files = archive_files(it["id"])
            link = f"https://archive.org/details/{it['id']}"
            if not restricted and files:
                ext = Path(files[0]).suffix
                dest = fdir / f"{safe}_{it['kind']}_{len(got[it['kind']]) + 1}{ext}"
                if save(f"https://archive.org/download/{it['id']}/{quote(files[0])}", dest):
                    got[it["kind"]].append(f"{dest.name} <- {link}")
                    continue
            borrow[it["kind"]].append(f"{it['title']} ({it['year']}) {link}")
            time.sleep(0.5)

        for g in gutenberg(show, cr):
            dest = fdir / f"{safe}_script_gutenberg{Path(g['url'].split('?')[0]).suffix or '.txt'}"
            if save(g["url"], dest):
                got["script"].append(f"{dest.name} <- https://www.gutenberg.org/ebooks/{g['id']}")

        eds = openlibrary(show, cr)
        for e in eds:
            if e["ebook_access"] == "public" and e["ia"]:
                restricted, files = archive_files(e["ia"])
                if not restricted and files:
                    dest = fdir / f"{safe}_{e['kind']}_ol{Path(files[0]).suffix}"
                    if save(f"https://archive.org/download/{e['ia']}/{quote(files[0])}", dest):
                        got[e["kind"]].append(f"{dest.name} <- {e['url']}")
            elif e["ebook_access"] == "borrowable" and e["ia"]:
                borrow[e["kind"]].append(f"{e['title']} ({e['year']}) https://archive.org/details/{e['ia']}")

        lic_hits = licensing_hits(show, lic)
        is_musical = genre.lower() == "musical"
        have_script, have_score = bool(got["script"]), bool(got["score"])
        if have_script or have_score:
            status = "downloaded: " + "+".join(k for k in ("script", "score") if got[k])
        elif borrow["script"] or borrow["score"]:
            status = "borrowable on archive.org (free account)"
        elif eds:
            status = "published edition exists (buy / library)"
        elif lic_hits:
            status = "licensing house only (perusal for registered users)"
        else:
            status = "not found"
        rows.append({**row, "status": status,
                     "script_files": " | ".join(got["script"]),
                     "score_files": " | ".join(got["score"]) if is_musical else "",
                     "script_borrow": " | ".join(dict.fromkeys(borrow["script"]))[:1500],
                     "score_borrow": " | ".join(dict.fromkeys(borrow["score"]))[:1500] if is_musical else "",
                     "published_editions": " | ".join(
                         f"{e['title']} ({e['year']}; {e['publishers']}; ISBN {e['isbn']}) {e['url']}" for e in eds[:5]),
                     "licensing": " | ".join(lic_hits),
                     "creators_used": "; ".join(cr)})
        print(f"  [{status}] {show}", flush=True)
        time.sleep(1)

    res = pd.DataFrame(rows)
    res.to_excel(a.output, index=False)
    print(f"::notice::shows={len(res)} | status={res['status'].value_counts().to_dict()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::error::{type(e).__name__}: {str(e)[:500]}")
        raise
