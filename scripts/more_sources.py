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
    heads = {s, s.replace("-and-", "-")}
    for a in index:
        slug = a["slug"]
        if any(slug.startswith(f"{h}-{w}") for h in heads
               for w in ("screenplay", "script-read", "script-screenplay", "read-the-screenplay",
                         "read-the-script", "script-")) or \
                any(slug.startswith(f"read-{h}-") or slug.startswith(f"{h}-movie-screenplay") for h in heads):
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


# ---------------------------------------------------------------- round 4
SOS_EXCLUDE = re.compile(r"scripts-onscreen\.com|amazon\.|scriptcity\.com|scriptfly|scribd\.com|imdb\.com|"
                         r"themoviedb\.org|wordpress\.org|gmpg\.org|scrapsfromtheloft|subslikescript|"
                         r"springfieldspringfield|twitter\.com|facebook\.com|x\.com/|youtube\.com|"
                         r"hollywoodscriptshop|ebay\.|etsy\.", re.I)


def scriptsonscreen_index(session, sleep: float = 1.0) -> dict:
    """slug ('dune-part-two') -> movie page URL, from the WordPress post sitemaps."""
    idx = {}
    for i in range(1, 15):
        r = session.get(f"https://scripts-onscreen.com/wp-sitemap-posts-post-{i}.xml", timeout=60)
        if r.status_code == 404:
            break
        for u in _locs(r.text):
            m = re.search(r"/movie/(.+?)-script-links/?$", u)
            if m:
                idx.setdefault(m.group(1), u)
        time.sleep(sleep)
    print(f"  Scripts On Screen: {len(idx)} movie pages")
    return idx


def scriptsonscreen_candidates(title: str, rel, idx: dict, session) -> list[dict]:
    """Free (non-paid, non-Scribd, non-transcript) script links listed on the movie's page."""
    from bs4 import BeautifulSoup
    s = slugify(title)
    pages = [idx[k] for k in (f"{s}-{rel}" if rel else "", s) if k and k in idx]
    out = []
    for p in pages[:1]:
        try:
            r = session.get(p, timeout=60)
            if not r.ok:
                continue
        except Exception:  # noqa: BLE001
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        body = soup.select_one("article") or soup
        for a in body.find_all("a", href=True):
            href, text = a["href"].strip(), " ".join(a.get_text(" ", strip=True).split())
            if not href.startswith("http") or SOS_EXCLUDE.search(href):
                continue
            out.append({"source": "scriptsonscreen", "title": title, "url": href, "year": "",
                        "award_year": "", "info": f"scripts-onscreen: {text[:80]} ({p})"})
        time.sleep(1)
    return out


_ARCHIVE_BAD = re.compile(r"promo|trailer|cbfc|teaser|song|lyrics|novel|book|review|poster|press kit|"
                          r"production notes|pages? \d|pgs", re.I)


def _archive_search_docs(q: str, session, rows: int = 20) -> list[dict]:
    try:
        r = session.get("https://archive.org/advancedsearch.php",
                        params={"q": q, "fl[]": ["identifier", "title", "year"], "rows": rows, "output": "json"},
                        timeout=60)
        return r.json().get("response", {}).get("docs", [])
    except Exception:  # noqa: BLE001
        return []


def archive_candidates(title: str, rel, session) -> list[dict]:
    """archive.org texts whose title is '<movie> ... screenplay/script'."""
    docs = _archive_search_docs(
        f'title:("{title}") AND (title:screenplay OR title:script OR subject:screenplay) AND mediatype:texts',
        session, rows=10)
    if not docs:
        # fallback: just the title + mediatype:texts, no screenplay/script keyword requirement
        # (catches items filed under looser metadata, e.g. "<Movie> (shooting draft)");
        # still gated below by startswith-match + _ARCHIVE_BAD, and by year_check downstream
        docs = _archive_search_docs(f'title:("{title}") AND mediatype:texts', session, rows=20)
    n = re.sub(r"[^a-z0-9]", "", title.lower())
    seen, out = set(), []
    for d in docs:
        ident = d.get("identifier")
        if ident in seen:
            continue
        t = str(d.get("title", ""))
        tn = re.sub(r"[^a-z0-9]", "", t.lower())
        if not tn.startswith(n) or _ARCHIVE_BAD.search(t):
            continue
        seen.add(ident)
        yr = re.search(r"(19|20)\d{2}", t)
        out.append({"source": "archive_org", "title": title,
                    "url": f"https://archive.org/details/{ident}",
                    "year": yr.group(0) if yr else "", "award_year": "", "info": f"archive.org: {t[:100]}"})
    return out


def archive_pdf_url(details_url: str, session) -> str:
    ident = details_url.rstrip("/").rsplit("/", 1)[-1]
    meta = session.get(f"https://archive.org/metadata/{ident}", timeout=60).json()
    files = [f["name"] for f in meta.get("files", []) if f.get("name", "").lower().endswith((".pdf", ".txt", ".docx", ".doc"))
             and not f["name"].lower().endswith(("_djvu.txt", "_hocr.txt"))]
    files.sort(key=lambda n: (not n.lower().endswith(".pdf"), len(n)))
    if not files:
        raise ValueError("archive.org item has no pdf/txt file")
    from urllib.parse import quote
    return f"https://archive.org/download/{ident}/{quote(files[0])}"


# ---------------------------------------------------------------- transcripts (dialogue only)
SPRING = "https://www.springfieldspringfield.co.uk"


def springfield_index(session, sleep: float = 0.4) -> dict:
    """norm title -> list of (year, url) from the A-Z movie index pages."""
    idx: dict = {}
    letters = ["0"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    pages = 0
    for L in letters:
        for page in range(1, 400):
            url = f"{SPRING}/movie_scripts.php?order={L}" + (f"&page={page}" if page > 1 else "")
            try:
                r = session.get(url, timeout=60)
            except Exception:  # noqa: BLE001
                break
            pages += 1
            found = re.findall(r'href="/movie_script\.php\?movie=([^"]+)"[^>]*>([^<]+)</a>', r.text)
            if not found:
                break
            for slug, label in found:
                m = re.match(r"(.*)\((\d{4})\)\s*$", label.strip())
                t, y = (m.group(1).strip(), m.group(2)) if m else (label.strip(), "")
                idx.setdefault(re.sub(r"[^a-z0-9]", "", t.lower()), []).append((y, f"{SPRING}/movie_script.php?movie={slug}"))
            if f"page={page + 1}" not in r.text:
                break
            time.sleep(sleep)
    print(f"  Springfield: {sum(len(v) for v in idx.values())} movie transcripts from {pages} index pages")
    return idx


def springfield_transcript(url: str, session) -> str:
    from bs4 import BeautifulSoup
    r = session.get(url, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    node = soup.select_one(".scrolling-script-container") or soup.select_one(".movie_script") or \
        max(soup.find_all("div"), key=lambda d: len(d.get_text()), default=None)
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    return "\n".join(l.strip() for l in node.get_text().splitlines() if l.strip())
