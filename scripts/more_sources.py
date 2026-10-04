"""
Additional screenplay sources (round 2).

Script Slug (scriptslug.com)
  sitemap-scripts.xml lists every script page: /script/<title-slug>-<year>
  PDF: https://assets.scriptslug.com/live/pdf/scripts/<title-slug>-<year>.pdf
  TV episodes (/script/<series>-<NNN>-<episode>-<year>) are skipped using sitemap-series.xml.

Deadline "read the screenplay" articles
  Monthly post sitemaps (post-sitemapYYYYMM.xml). Articles whose slug mentions
  screenplay/script are kept; the article embeds the script (DocumentCloud / PDF),
  which collect_scripts.fetch_script follows.
"""

from __future__ import annotations

import datetime as dt
import re
import time

SS_BASE = "https://www.scriptslug.com"
SS_PDF = "https://assets.scriptslug.com/live/pdf/scripts/{slug}.pdf"


def _locs(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def scriptslug_catalog(session) -> list[dict]:
    r = session.get(f"{SS_BASE}/sitemap-scripts.xml", timeout=60)
    r.raise_for_status()
    script_urls = [u for u in _locs(r.text) if "/script/" in u]
    series = set()
    try:
        rs = session.get(f"{SS_BASE}/sitemap-series.xml", timeout=60)
        for u in _locs(rs.text):
            m = re.search(r"/series/(.+)-(?:19|20)\d{2}/?$", u)
            if m:
                series.add(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    out = []
    for u in script_urls:
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        m = re.match(r"(.+)-((?:19|20)\d{2})$", slug)
        name, year = (m.group(1), m.group(2)) if m else (slug, "")
        ep = re.match(r"(.+?)-\d{3}-", name)
        if ep and (ep.group(1) in series or re.search(r"-\d{3}-", name)):
            continue  # TV episode
        out.append({"source": "scriptslug", "title": name.replace("-", " "), "url": SS_PDF.format(slug=slug),
                    "page": u, "year": year, "award_year": "", "info": f"scriptslug {slug}"})
    print(f"  Script Slug: {len(out)} film scripts (of {len(script_urls)} script pages, {len(series)} series)")
    return out


DEADLINE_KEEP = re.compile(r"screenplay|-script-|-script$|read-the-script|scripts?-read", re.I)


def deadline_index(session, start: tuple[int, int] = (2020, 9), sleep: float = 0.5) -> list[dict]:
    today = dt.date.today()
    y, m = start
    out, months = [], 0
    while (y, m) <= (today.year, today.month):
        url = f"https://deadline.com/post-sitemap{y}{m:02d}.xml"
        try:
            r = session.get(url, timeout=60)
            if r.ok:
                months += 1
                for u in _locs(r.text):
                    slug = u.rstrip("/").rsplit("/", 1)[-1]
                    if DEADLINE_KEEP.search(slug):
                        out.append({"url": u, "slug": slug, "year": str(y)})
        except Exception as e:  # noqa: BLE001
            print(f"  deadline sitemap {y}{m:02d} failed: {e}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
        time.sleep(sleep)
    print(f"  Deadline: {len(out)} screenplay/script articles from {months} monthly sitemaps")
    return out


def slugify(title: str) -> str:
    t = str(title).lower().replace("&", "and").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def deadline_candidates(title: str, index: list[dict]) -> list[dict]:
    """Articles whose slug starts with the title slug and talks about the screenplay."""
    s = slugify(title)
    if len(s) < 3:
        return []
    out = []
    for a in index:
        if a["slug"].startswith(s + "-") or a["slug"].startswith("read-" + s + "-") or \
                a["slug"].startswith(s.replace("-and-", "-") + "-"):
            out.append({"source": "deadline", "title": title, "url": a["url"], "year": a["year"],
                        "award_year": "", "info": f"deadline {a['slug'][:120]}"})
    return out


# ---------------------------------------------------------------- round 3: pages that list PDF links
LINK_PAGES = [
    "https://scriptpdf.com/full-list/",
    "https://bulletproofscreenwriting.tv/free-screenplays-download/",
    "https://indiefilmhustle.com/free-screenplays-download/",
    "https://nofilmschool.com/2023-academy-award-screenplays",
    "https://www.writing.ninja/free-screenplay-downloads-pdf/",
] + ["https://www.simplyscripts.com/category/movie-scripts/oscar-contenders/"] + [
    f"https://www.simplyscripts.com/category/movie-scripts/oscar-contenders/page/{i}/" for i in range(2, 12)]
_GENERIC = re.compile(r"^(download|pdf|here|read|script|screenplay|link|click here|read (it|the script) here)\W*$", re.I)


def linkpages_catalog(session, pages: list[str] = LINK_PAGES, sleep: float = 1.0) -> list[dict]:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    out, seen = [], set()
    for page in pages:
        try:
            r = session.get(page, timeout=60)
            if r.status_code == 404:
                continue
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"  link page failed {page}: {type(e).__name__}")
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        n = 0
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not re.search(r"\.pdf(\?|#|$)|documentcloud\.org/documents/", href, re.I):
                continue
            href = re.sub(r"^https?://", lambda x: x.group(0).lower(), urljoin(page, href), flags=re.I)
            title = " ".join(a.get_text(" ", strip=True).split())
            title = re.sub(r"\s*[\(\[]?(pdf|screenplay|script|download)[\)\]]?\s*$", "", title, flags=re.I).strip()
            if not title or _GENERIC.match(title) or len(title) > 80 or href in seen:
                continue
            seen.add(href)
            n += 1
            out.append({"source": "linkpages", "title": title, "url": href, "year": "", "award_year": "",
                        "info": f"listed on {page}"})
        print(f"  link page {page}: {n} pdf links")
        time.sleep(sleep)
    return out
