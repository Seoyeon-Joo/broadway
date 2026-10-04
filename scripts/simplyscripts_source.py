"""
SimplyScripts (simplyscripts.com) catalog + script URL resolver.

Sources parsed
- movie-screenplays.html  : full list, <p><a class=style10>Title</a> <span class=txt_3>(date draft) by X host:</span> <a>Host</a></p>
- oscar-screenplays-NN.html (awards-season pages, studio FYC scripts) + oscar_winners.html:
  <div id=movie_wide> ... <a href=SCRIPT><b>Title</b></a> - date draft script by X - hosted by: <a>Host</a> - in pdf format

Many entries link to other hosts (DocumentCloud, studio award sites, Google Drive, Daily Script,
archive.org ...). resolve_download_url() turns known viewer URLs into direct-download URLs.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

BASE = "https://www.simplyscripts.com/"
FULL_LIST = "movie-screenplays.html"
AWARD_PAGES = [f"oscar-screenplays-{n}.html" for n in range(90, 99)]
# oscar-screenplays-NN: NN = Academy Awards edition; ceremony year = 1928 + NN (e.g. 96 -> 2024)

SKIP_HOST = ("imdb.com", "wikipedia.org", "amazon.com", "simplyscripts.com/forum", "facebook.com", "twitter.com")


def _clean(s: str) -> str:
    return " ".join(str(s or "").replace("\xa0", " ").split())


def _year(text: str) -> str:
    m = re.search(r"\b(19[2-9]\d|20[0-4]\d)\b", text)
    return m.group(1) if m else ""


def parse_full_list(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for span in soup.find_all("span", class_="txt_3"):
        a = span.find_previous_sibling("a")
        if a is None or not a.get("href"):
            continue
        meta = _clean(span.get_text(" "))
        host_a = span.find_next_sibling("a")
        out.append({
            "source_page": page_url, "title": _clean(a.get_text(" ")),
            "url": urljoin(page_url, a["href"].strip()),
            "host": _clean(host_a.get_text(" ")) if host_a else "",
            "draft_info": meta, "script_year": _year(meta), "award_year": "",
        })
    return out


def parse_award_page(html: str, page_url: str) -> list[dict]:
    """Each film is a <div id="movie_wide">; its script link is the first bold link that
    is not IMDb / Wikipedia. Layout of the text around it changes by year, so the whole
    block text is kept as draft_info."""
    soup = BeautifulSoup(html, "html.parser")
    m = re.search(r"oscar-screenplays-(\d+)", page_url)
    award_year = str(1928 + int(m.group(1))) if m else ""
    out = []
    for div in soup.find_all("div", id="movie_wide"):
        script_a = None
        for a in div.find_all("a", href=True):
            if a.find("b") is None or any(h in a["href"].lower() for h in SKIP_HOST):
                continue
            script_a = a
            break
        if script_a is None:
            continue
        href = script_a["href"].strip()
        href = re.sub(r"^https?://", lambda x: x.group(0).lower(), href, flags=re.I)
        block = _clean(div.get_text(" "))
        hm = re.search(r"hosted by:?\s*([^-|(]+?)(?:\s+-|\s*\(|$)", block, re.I)
        dm = re.search(r"\(([^()]*draft[^()]*|[^()]*script[^()]*)\)", block, re.I)
        draft = dm.group(1) if dm else block[:200]
        out.append({
            "source_page": page_url, "title": _clean(script_a.get_text(" ")),
            "url": urljoin(page_url, href), "host": _clean(hm.group(1)) if hm else "",
            "draft_info": draft, "script_year": _year(draft), "award_year": award_year,
        })
    return out


def resolve_download_url(url: str) -> str:
    """Map viewer/landing URLs to direct-download URLs where the pattern is known."""
    u = url.strip()
    p = urlparse(u)
    host = p.netloc.lower()
    # DocumentCloud: embed./www.documentcloud.org/documents/<id>-<slug>/ -> s3 pdf
    m = re.search(r"documentcloud\.org/documents/(\d+)-([^/?#.]+)", u)
    if m and "s3.documentcloud.org" not in host:
        return f"https://s3.documentcloud.org/documents/{m.group(1)}/{m.group(2)}.pdf"
    # Google Drive file viewer -> direct download
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([\w-]+)", u)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if host.endswith("drive.google.com") and "id" in parse_qs(p.query):
        return f"https://drive.google.com/uc?export=download&id={parse_qs(p.query)['id'][0]}"
    # Dropbox share -> direct
    if "dropbox.com" in host:
        return re.sub(r"([?&])dl=0", r"\1dl=1", u) if "dl=0" in u else u + ("&" if "?" in u else "?") + "dl=1"
    return u


def build_catalog(session, sleep: float = 1.0) -> list[dict]:
    """Fetch SimplyScripts award pages + full list and return entries (award pages first)."""
    import time
    entries: list[dict] = []
    for page in AWARD_PAGES + [FULL_LIST]:
        url = urljoin(BASE, page)
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            html = r.content.decode(r.apparent_encoding or "utf-8", errors="replace")
            recs = parse_full_list(html, url) if page == FULL_LIST else parse_award_page(html, url)
            print(f"  SimplyScripts {page}: {len(recs)}")
            if not recs:
                print(f"::warning::SimplyScripts {page}: 0 entries parsed (layout change?)")
            entries += recs
        except Exception as e:  # noqa: BLE001
            print(f"::warning::SimplyScripts {page} fetch failed: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(sleep)
    return entries
