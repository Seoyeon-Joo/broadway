"""
Combined screenplay collector — one script per movie, sources in priority order.

  1. SimplyScripts award-season pages (studio FYC scripts, 2018-2026)
  2. Script Slug (scriptslug.com, ~2k film scripts incl. 2021-2026)
  3. SimplyScripts full movie list (links to many hosts)
  4. Deadline "read the screenplay" articles (2020-09 onward)
  5. Pages listing screenplay PDF links (ScriptPDF, Bulletproof Screenwriting, Indie Film Hustle,
     No Film School, writing.ninja, SimplyScripts Oscar-contenders category)
  6. IMSDb
  7. Daily Script

Movie list handling
  - Box Office Mojo re-release suffixes ("Alien2020 Re-release", "Coraline15th Anniversary")
    are stripped before matching; re-releases (suffix, or Box Office Mojo "Weeks" >= 52 at
    first appearance) accept scripts from any earlier year.
  - Non-film items (concerts, UFC, opera/stage broadcasts, TV-episode screenings, shorts
    programmes) are classified (non_film_type) and skipped.
  - --previous <results.xlsx|csv>: movies already collected there are carried over and skipped.
  - Any file format is kept: if no text can be extracted, the original PDF/DOC is still saved
    (status ok_raw_only).

For every movie in data/movie_list.xlsx:
  - candidates = exact (normalized) title matches in every source, plus fuzzy matches
    (>= --fuzzy) that pass the year check
  - year check uses the first Box Office Mojo year from data/hollywood.xlsx:
      award-season entry : award_year - 1 within [release-1, release+1]
      other entries      : script/film year within [release-8, release+1]
    candidates that fail are dropped (removes remakes / same-title older films);
    candidates with no year are kept but tried after year-verified ones.
  - candidates are tried in order until one yields real screenplay text
    (so a dead SimplyScripts link falls back to IMSDb / Daily Script automatically).

Outputs
  data/script_collection.xlsx   one row per movie: status, chosen source/url, year check, all candidates
  data/scripts_txt/             <movie_id>_<title>.txt
  data/scripts_raw/             original pdf/html/txt as downloaded
"""

from __future__ import annotations

import argparse
import io
import signal
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

import dailyscript_matcher as ds
import imsdb_matcher as im
import more_sources as ms
import simplyscripts_source as ss
import title_utils as tu
from imsdb_matcher import norm  # handles "Title, The" -> "the title"

session = ds.session
MIN_CHARS = 15000  # a feature screenplay is typically 100k+ characters
SOURCE_RANK = {"simplyscripts_award": 0, "scriptslug": 1, "simplyscripts": 2, "deadline": 3,
               "linkpages": 4, "imsdb": 5, "dailyscript": 6}
RAW_OK_EXT = {".pdf", ".doc", ".docx", ".rtf"}
RAW_MIN_BYTES = 30_000


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(s).replace(" and ", " "))


# ----------------------------------------------------------------- catalogs
def load_catalog() -> list[dict]:
    cands: list[dict] = []
    print("SimplyScripts...")
    for e in ss.build_catalog(session):
        cands.append({"source": "simplyscripts_award" if e["award_year"] else "simplyscripts",
                      "title": e["title"], "url": e["url"], "year": e["script_year"],
                      "award_year": e["award_year"], "info": f"{e['host']} | {e['draft_info']}"})
    print("Script Slug...")
    try:
        cands += ms.scriptslug_catalog(session)
    except Exception as e:  # noqa: BLE001
        print(f"::warning::Script Slug catalog failed: {e}")
    print("PDF link pages...")
    try:
        cands += ms.linkpages_catalog(session)
    except Exception as e:  # noqa: BLE001
        print(f"::warning::link pages failed: {e}")
    print("IMSDb...")
    try:
        for e in im.build_catalog():
            y = re.search(r"(19|20)\d{2}", e["script_date"] or "")
            cands.append({"source": "imsdb", "title": e["imsdb_title"], "url": e["movie_page"],
                          "year": y.group(0) if y else "", "award_year": "",
                          "info": f"{e['script_date']} | {e['writers']}"})
    except Exception as e:  # noqa: BLE001
        print(f"::warning::IMSDb catalog failed: {e}")
    print("Daily Script...")
    try:
        for e in ds.build_catalog():
            cands.append({"source": "dailyscript", "title": e["script_title"], "url": e["url"],
                          "year": e["year"], "award_year": "", "info": e["metadata"][:200]})
    except Exception as e:  # noqa: BLE001
        print(f"::warning::Daily Script catalog failed: {e}")
    for c in cands:
        c["norm"] = norm(c["title"])
        c["compact"] = compact(c["title"])
    counts = pd.Series([c["source"] for c in cands]).value_counts().to_dict()
    print(f"Catalog: {len(cands)} entries {counts}")
    return [c for c in cands if c["norm"]]


def release_info(path: str) -> tuple[dict, dict, dict]:
    """(first box-office year, weeks-in-release at first appearance, distributor) per movie_id."""
    try:
        h = pd.read_excel(path, usecols=["movie_id", "Year", "Weeks", "Distributor", "week_start_date"])
        h = h.sort_values("week_start_date")
        first = h.groupby("movie_id").first()
        years = h.groupby("movie_id")["Year"].min().astype(int).to_dict()
        weeks = pd.to_numeric(first["Weeks"], errors="coerce").fillna(0).astype(int).to_dict()
        dist = h.groupby("movie_id")["Distributor"].agg(
            lambda x: x.dropna().iloc[0] if x.notna().any() else "").to_dict()
        return years, weeks, dist
    except Exception as e:  # noqa: BLE001
        print(f"::warning::release info unavailable ({e}); year check disabled")
        return {}, {}, {}


def year_check(c: dict, rel: int | None, rerelease: bool = False) -> str:
    """'ok' | 'fail' | 'unknown'"""
    if not rel:
        return "unknown"
    if rerelease:  # re-release of an older film: any script from before the re-release year
        y = c["year"] or (str(int(c["award_year"]) - 1) if c["award_year"] else "")
        if not y:
            return "unknown"
        return "ok" if int(y) <= rel else "fail"
    if c["award_year"]:
        return "ok" if rel - 1 <= int(c["award_year"]) - 1 <= rel + 1 else "fail"
    if c["year"]:
        y = int(c["year"])
        return "ok" if rel - 8 <= y <= rel + 1 else "fail"
    return "unknown"


def candidates_for(title: str, rel, by_norm: dict, keys: list, fuzzy: int,
                   by_compact: dict | None = None, deadline_idx: list | None = None,
                   rerelease: bool = False) -> list[dict]:
    n = norm(title)
    found = [dict(c, score=100.0) for c in by_norm.get(n, [])]
    seen = {(c["source"], c["url"]) for c in found}
    for c in (by_compact or {}).get(compact(title), []):  # punctuation-insensitive (Script Slug slugs)
        if (c["source"], c["url"]) not in seen:
            found.append(dict(c, score=100.0))
            seen.add((c["source"], c["url"]))
    for c in ms.deadline_candidates(title, deadline_idx or []):
        c["norm"], c["compact"] = n, compact(title)
        found.append(dict(c, score=100.0))
    if n:
        for key, score, _ in process.extract(n, keys, scorer=fuzz.ratio, limit=5):
            if key != n and score >= fuzzy:
                found += [dict(c, score=float(score)) for c in by_norm[key]]
    out = []
    for c in found:
        c["year_check"] = year_check(c, rel, rerelease)
        if c["score"] < 100 and (c["year_check"] != "ok" or not _wordwise_close(n, c["norm"])):
            continue  # fuzzy matches must be year-verified and differ only by typos
        out.append(c)
    # year-verified first, then unknown, then mismatched (kept: box-office re-releases of old
    # films such as Jaws/Shrek legitimately use the original script; filter on year_check later)
    order = {"ok": 0, "unknown": 1, "fail": 2}
    out.sort(key=lambda c: (order[c["year_check"]], SOURCE_RANK[c["source"]], -c["score"]))
    return out


def _wordwise_close(a: str, b: str) -> bool:
    wa, wb = a.split(), b.split()
    return len(wa) == len(wb) and all(x == y or fuzz.ratio(x, y) >= 80 for x, y in zip(wa, wb))


# ----------------------------------------------------------------- fetching
LANDING_HINT = re.compile(r"documentcloud\.org/documents/\d+|\.pdf(\?|$|#)", re.I)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get_once(url: str):
    """Default UA first (some CDNs, e.g. DocumentCloud, 403 a spoofed browser UA);
    retry with a browser UA only if the default one is refused."""
    r = session.get(url, timeout=(20, 90), allow_redirects=True)
    if r.status_code == 403:
        r2 = session.get(url, timeout=(20, 90), allow_redirects=True, headers={"User-Agent": BROWSER_UA})
        if r2.ok:
            return r2
    r.raise_for_status()
    return r


_last_archive = [0.0]
ARCHIVE_STATS = {"ok": 0, "miss": 0, "429": 0}


def _archive_get(url: str):
    """GET from web.archive.org: >=5s between requests, one short retry on 429."""
    for attempt in range(2):
        wait = 5 - (time.time() - _last_archive[0])
        if wait > 0:
            time.sleep(wait)
        _last_archive[0] = time.time()
        r = session.get(url, timeout=(20, 90), allow_redirects=True)
        if r.status_code == 429:
            ARCHIVE_STATS["429"] += 1
            if attempt == 0:
                time.sleep(min(int(r.headers.get("Retry-After", 0) or 0), 60) or 20)
                continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def wayback_get(url: str):
    """Closest archived capture of url as raw content (id_ mode), or None."""
    if "web.archive.org" in url:
        return None
    try:
        r = _archive_get(f"https://web.archive.org/web/2026id_/{url}")
        r.wayback = True
        ARCHIVE_STATS["ok"] += 1
        print(f"    wayback ok: {url}")
        return r
    except Exception as e:  # noqa: BLE001
        ARCHIVE_STATS["miss"] += 1
        print(f"    wayback miss: {url} ({type(e).__name__})")
        return None


def _get(url: str):
    """GET with Wayback Machine fallback for dead links (403/404/410/429/5xx/connection errors)."""
    try:
        if "web.archive.org" in url:
            return _archive_get(url)
        return _get_once(url)
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code is not None and code not in (403, 404, 410, 429) and code < 500:
            raise
        r = wayback_get(url)
        if r is None:
            raise
        return r


OCR_MAX_PAGES = 250
OCR_USED: list[str] = []


def _pdf_text(content: bytes) -> str:
    """pypdf -> PyMuPDF -> Tesseract OCR (for scanned / image-only PDFs)."""
    text = ""
    try:
        from pypdf import PdfReader
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(content)).pages)
    except Exception:  # noqa: BLE001
        pass
    if len(text.strip()) >= MIN_CHARS:
        return text
    try:
        import pymupdf as fitz
        doc = fitz.open(stream=content, filetype="pdf")
        t2 = "\n".join(page.get_text() for page in doc)
        if len(t2.strip()) >= MIN_CHARS:
            return t2
        import os
        from concurrent.futures import ThreadPoolExecutor
        import pytesseract
        from PIL import Image
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # one core per tesseract, pages in parallel
        imgs = []
        for i, page in enumerate(doc):
            if i >= OCR_MAX_PAGES:
                break
            pix = page.get_pixmap(dpi=150, colorspace=fitz.csGRAY)
            imgs.append(Image.frombytes("L", (pix.width, pix.height), pix.samples))
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
            parts = list(ex.map(lambda im: pytesseract.image_to_string(im, config="--psm 6"), imgs))
        t3 = "\n".join(parts)
        if len(t3.strip()) > len(text.strip()):
            OCR_USED.append(f"{len(doc)}p")
            return "[OCR]\n" + t3
    except Exception as e:  # noqa: BLE001
        print(f"    pdf fallback failed: {type(e).__name__}: {str(e)[:120]}")
    return text


def _is_pdf(r) -> bool:
    return r.content[:5] == b"%PDF-" or "application/pdf" in r.headers.get("content-type", "").lower()


def _clean(text: str) -> str:
    text = "\n".join(l.rstrip() for l in text.replace("\r", "").splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_text(r) -> str:
    html = r.content.decode(r.apparent_encoding or "utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("td.scrtext pre") or soup.select_one("td.scrtext")
    if node is None:
        pres = soup.find_all("pre")
        node = max(pres, key=lambda p: len(p.get_text())) if pres else None
        if node is not None and len(node.get_text()) < MIN_CHARS:
            node = None
    if node is None:
        for t in soup(["script", "style", "noscript", "nav", "header", "footer"]):
            t.decompose()
        node = soup.body or soup
    return node.get_text("\n")


def _landing_links(r, base: str, title: str) -> list[str]:
    """PDF / DocumentCloud links on an article or landing page, best guesses first."""
    soup = BeautifulSoup(r.content, "html.parser")
    urls = []
    for tag in soup.find_all(["a", "iframe", "embed", "object"]):
        u = tag.get("href") or tag.get("src") or tag.get("data") or ""
        if u and LANDING_HINT.search(u):
            urls.append(urljoin(base, u))
    for m in re.finditer(r"https?://[^\s\"'<>]*documentcloud\.org/documents/\d+-[\w-]+", r.text):
        urls.append(m.group(0))
    slug = norm(title).replace(" ", "")
    seen, out = set(), []
    for u in sorted(urls, key=lambda u: slug[:12] not in re.sub(r"[^a-z0-9]", "", u.lower())):
        u = ss.resolve_download_url(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:4]


def fetch_script(c: dict, title: str) -> tuple[str, bytes, str, str]:
    """Return (text, raw_bytes, raw_ext, final_url). Raises on failure."""
    url = c["url"]
    if c["source"] == "imsdb":
        url = im.find_script_url(url)
        if not url:
            raise ValueError("IMSDb page has no script link")
        r = im.get(url)
        return _clean(im.page_to_text(r)), r.content, ".pdf" if _is_pdf(r) else ".html", url

    if c["source"] == "scriptslug":
        return _fetch_scriptslug(c)
    url = ss.resolve_download_url(url)
    if "drive.google.com" in url:
        return _fetch_drive(url)
    if ".box.com/s/" in url:
        return _fetch_box(url)
    r = _get(url)
    if getattr(r, "wayback", False):
        url = r.url
    if _is_pdf(r):
        t = _clean(_pdf_text(r.content))
        if len(t) < MIN_CHARS and not getattr(r, "wayback", False):
            wb = wayback_get(url)  # e.g. protected/broken PDF -> archived copy
            if wb is not None and _is_pdf(wb):
                t2 = _clean(_pdf_text(wb.content))
                if len(t2) > len(t):
                    return t2, wb.content, ".pdf", wb.url
        return t, r.content, ".pdf", url
    ctype = r.headers.get("content-type", "").lower()
    path = urlparse(url).path.lower()
    if r.content[:2] == b"PK" and ("wordprocessingml" in ctype or path.endswith(".docx")):
        import zipfile
        xml = zipfile.ZipFile(io.BytesIO(r.content)).read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return _clean(re.sub(r"<[^>]+>", "", xml)), r.content, ".docx", url
    if path.endswith((".doc", ".rtf")) or "msword" in ctype or "rtf" in ctype:
        txt = r.content.decode("latin-1", "replace")
        if path.endswith(".rtf") or "rtf" in ctype:
            txt = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", txt)
        return _clean(txt) if path.endswith(".rtf") else "", r.content, ".rtf" if "rtf" in path else ".doc", url
    if "text/plain" in ctype or path.endswith(".txt"):
        r.encoding = r.apparent_encoding or r.encoding
        return _clean(r.text), r.content, ".txt", url
    text = _clean(_html_text(r))
    if len(text) >= MIN_CHARS:
        return text, r.content, ".html", url
    # article / landing page -> follow embedded PDF or DocumentCloud links
    for link in _landing_links(r, url, title):
        try:
            r2 = _get(link)
            if _is_pdf(r2):
                t2 = _clean(_pdf_text(r2.content))
                if len(t2) >= MIN_CHARS:
                    return t2, r2.content, ".pdf", link
        except Exception:  # noqa: BLE001
            continue
        time.sleep(1)
    # page served something other than the script (redirect to a home page, viewer shell...)
    if not getattr(r, "wayback", False):
        wb = wayback_get(url)
        if wb is not None and _is_pdf(wb):
            t3 = _clean(_pdf_text(wb.content))
            if len(t3) >= MIN_CHARS:
                return t3, wb.content, ".pdf", wb.url
    return text, r.content, ".html", url


_last_slug = [0.0]


def _fetch_scriptslug(c: dict):
    """Script Slug PDFs sit behind a CDN that 403s rapid / referer-less requests: pace + retry."""
    hdrs = {"Referer": c.get("page") or "https://www.scriptslug.com/", "User-Agent": BROWSER_UA,
            "Accept": "application/pdf,*/*"}
    last = None
    for attempt in range(3):
        wait = 3 - (time.time() - _last_slug[0])
        if wait > 0:
            time.sleep(wait)
        _last_slug[0] = time.time()
        r = session.get(c["url"], timeout=(20, 90), headers=hdrs if attempt else {"Referer": hdrs["Referer"]})
        if r.ok and _is_pdf(r):
            return _clean(_pdf_text(r.content)), r.content, ".pdf", c["url"]
        last = r
        if r.status_code in (403, 429):
            time.sleep(15 * (attempt + 1))
            continue
        break
    if c.get("page"):  # PDF link with cache-busting ?v= taken from the script page
        try:
            pg = session.get(c["page"], timeout=(20, 60), headers={"User-Agent": BROWSER_UA})
            m = re.search(r'data-pdf-url="([^"]+)"', pg.text) or re.search(r'href="([^"]+\.pdf[^"]*)"', pg.text)
            if m:
                r = session.get(m.group(1).replace("&amp;", "&"), timeout=(20, 90), headers=hdrs)
                if r.ok and _is_pdf(r):
                    return _clean(_pdf_text(r.content)), r.content, ".pdf", m.group(1)
                last = r
        except Exception:  # noqa: BLE001
            pass
    last.raise_for_status()
    raise ValueError(f"Script Slug: no PDF (status {last.status_code})")


def _fetch_drive(url: str):
    fid = re.search(r"id=([\w-]+)", url).group(1)
    import tempfile
    import gdown
    with tempfile.TemporaryDirectory() as td:
        out = gdown.download(id=fid, output=f"{td}/f", quiet=True, timeout=120)
        if not out:
            raise ValueError("Google Drive download refused (not public?)")
        raw = Path(out).read_bytes()
    if raw[:5] != b"%PDF-":
        raise ValueError(f"Google Drive returned non-PDF ({raw[:60]!r})")
    return _clean(_pdf_text(raw)), raw, ".pdf", f"https://drive.google.com/uc?id={fid}"


def _fetch_box(url: str):
    """Box shared link: try shared/static, else parse file id from the page."""
    m = re.match(r"(https://[\w.-]*box\.com)/s/(\w+)", url)
    root, shared = m.group(1), m.group(2)
    tries = [f"{root}/shared/static/{shared}.pdf"]
    try:
        page = _get_once(url).text
        fm = re.search(r'"typedID"\s*:\s*"f_(\d+)"', page) or re.search(r'"itemID"\s*:\s*(\d+)', page) \
            or re.search(r"/file/(\d+)", page)
        if fm:
            tries.append(f"{root}/index.php?rm=box_download_shared_file&shared_name={shared}&file_id=f_{fm.group(1)}")
    except Exception:  # noqa: BLE001
        pass
    for t in tries:
        try:
            r = _get_once(t)
            if _is_pdf(r):
                return _clean(_pdf_text(r.content)), r.content, ".pdf", t
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("Box shared link: no direct PDF download - open in a browser and download manually")


# ----------------------------------------------------------------- main
def _alarm(signum, frame):
    raise TimeoutError("per-candidate time limit")


def main():
    signal.signal(signal.SIGALRM, _alarm)
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/movie_list.xlsx")
    ap.add_argument("--release-years", default="data/hollywood.xlsx")
    ap.add_argument("--output", default="data/script_collection.xlsx")
    ap.add_argument("--txt-dir", default="data/scripts_txt")
    ap.add_argument("--raw-dir", default="data/scripts_raw")
    ap.add_argument("--fuzzy", type=int, default=92)
    ap.add_argument("--max-tries", type=int, default=4, help="candidates tried per movie")
    ap.add_argument("--match-only", action="store_true")
    ap.add_argument("--per-try-timeout", type=int, default=480, help="seconds per candidate")
    ap.add_argument("--titles", default="", help="only these movie titles (separated by |), for debugging")
    ap.add_argument("--previous", default="", help="earlier script_collection.xlsx/.csv: skip movies already ok")
    a = ap.parse_args()

    movies = pd.read_excel(a.input)
    if a.titles:
        want = {t.strip() for t in a.titles.split("|") if t.strip()}
        movies = movies[movies["movie_title"].astype(str).str.strip().isin(want)]
    rel, first_weeks, dist = release_info(a.release_years)

    prev_ok = pd.DataFrame()
    if a.previous and Path(a.previous).exists():
        prev = pd.read_csv(a.previous) if a.previous.endswith(".csv") else pd.read_excel(a.previous)
        prev_ok = prev[prev["status"].astype(str).str.startswith("ok")].copy()
        prev_ok["round"] = prev_ok.get("round", pd.Series("previous", index=prev_ok.index)).fillna("previous")
        print(f"Previous results: {len(prev_ok)} movies already collected -> skipped")
    done_ids = set(prev_ok["movie_id"]) if len(prev_ok) else set()

    catalog = load_catalog()
    by_norm: dict[str, list[dict]] = {}
    by_compact: dict[str, list[dict]] = {}
    for c in catalog:
        by_norm.setdefault(c["norm"], []).append(c)
        if c.get("compact"):
            by_compact.setdefault(c["compact"], []).append(c)
    keys = list(by_norm)
    print("Deadline...")
    try:
        deadline_idx = ms.deadline_index(session)
    except Exception as e:  # noqa: BLE001
        print(f"::warning::Deadline index failed: {e}")
        deadline_idx = []

    txt_dir, raw_dir = Path(a.txt_dir), Path(a.raw_dir)
    txt_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    partial = Path(a.output).with_suffix(".partial.csv")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    round_name = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d %H:%M")
    for _, mv in movies.iterrows():
        mid, title = mv["movie_id"], str(mv["movie_title"]).strip()
        if mid in done_ids:
            continue
        ry = rel.get(mid)
        search_title, suffix = tu.clean_title(title)
        rerelease = suffix or first_weeks.get(mid, 0) >= 52
        nf = tu.non_film_type(title, dist.get(mid, ""))
        cands = [] if nf else candidates_for(search_title, ry, by_norm, keys, a.fuzzy,
                                            by_compact, deadline_idx, rerelease)
        row = {"movie_id": mid, "movie_title": title, "search_title": search_title,
               "release_year": ry, "rerelease": rerelease, "non_film_type": nf,
               "distributor": dist.get(mid, ""), "round": round_name,
               "n_candidates": len(cands),
               "status": "non_film" if nf else ("no_match" if not cands else "matched"),
               "source": "", "matched_title": "", "match_score": "", "year_check": "",
               "script_url": "", "via": "", "chars": 0, "script_info": "", "tried": "",
               "all_candidates": " || ".join(f"{c['source']}:{c['title']}:{c['url']}" for c in cands)}
        if cands and not a.match_only:
            row["status"] = "download_failed"
            tried = []
            raw_fallback = None  # first usable original file without extractable text
            for c in cands[: a.max_tries]:
                try:
                    signal.alarm(a.per_try_timeout)  # hard cap per candidate (hung server / huge OCR)
                    try:
                        text, raw, ext, final = fetch_script(c, title)
                    finally:
                        signal.alarm(0)
                    ok = len(text) >= MIN_CHARS
                    tried.append(f"{c['source']}:{'ok' if ok else f'short({len(text)})'}")
                    if not ok and raw_fallback is None and ext in RAW_OK_EXT and len(raw) >= RAW_MIN_BYTES:
                        raw_fallback = (c, raw, ext, final, len(text))
                    if ok:
                        safe = re.sub(r'[\\/:*?"<>|]+', "_", f"{mid}_{title}").strip(" ._")[:150]
                        (txt_dir / f"{safe}.txt").write_text(text, encoding="utf-8")
                        (raw_dir / f"{safe}{ext}").write_bytes(raw)
                        row["via"] = ("ocr " if text.startswith("[OCR]") else "") + ("wayback" if "web.archive.org" in final else "")
                        row.update(status="ok", source=c["source"], matched_title=c["title"],
                                   match_score=c["score"], year_check=c["year_check"],
                                   script_url=final, chars=len(text), script_info=c["info"][:300])
                        break
                except Exception as e:  # noqa: BLE001
                    tried.append(f"{c['source']}:err({type(e).__name__}: {str(e)[:60]})")
                time.sleep(1.5)
            if row["status"] == "download_failed" and raw_fallback is not None:
                c, raw, ext, final, n = raw_fallback  # keep the original file anyway (any format is fine)
                safe = re.sub(r'[\\/:*?"<>|]+', "_", f"{mid}_{title}").strip(" ._")[:150]
                (raw_dir / f"{safe}{ext}").write_bytes(raw)
                row.update(status="ok_raw_only", source=c["source"], matched_title=c["title"],
                           match_score=c["score"], year_check=c["year_check"], script_url=final,
                           chars=n, script_info=c["info"][:300],
                           via="wayback" if "web.archive.org" in final else "")
            row["tried"] = " ; ".join(tried)
            print(f"  [{row['status']}] {title} ({ry}) <- {row['source'] or '-'} | {row['tried']}", flush=True)
        elif cands:
            c = cands[0]
            row.update(source=c["source"], matched_title=c["title"], match_score=c["score"],
                       year_check=c["year_check"], script_url=c["url"], script_info=c["info"][:300])
        rows.append(row)
        if row["status"] != "no_match":  # keep progress on disk in case the job is cut off
            pd.DataFrame([row]).to_csv(partial, mode="a", header=not partial.exists(),
                                       index=False, encoding="utf-8-sig")

    new = pd.DataFrame(rows)
    res = pd.concat([prev_ok, new], ignore_index=True) if len(prev_ok) else new
    if "movie_id" in res:
        order = {m: i for i, m in enumerate(movies["movie_id"])}
        res = res.sort_values("movie_id", key=lambda s: s.map(order).fillna(1e9))
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    res.to_excel(a.output, index=False)
    st = res["status"].value_counts().to_dict()
    src = res.loc[res["status"].astype(str).str.startswith("ok"), "source"].value_counts().to_dict()
    gained = new["status"].astype(str).str.startswith("ok").sum() if len(new) else 0
    print(f"::notice::this round: +{gained} new scripts | non_film={int((new['status'] == 'non_film').sum())}")
    print(f"::notice::movies={len(res)} | status={st} | ok by source={src} | OCR used={len(OCR_USED)} | wayback={ARCHIVE_STATS}")
    failed = new[new["status"] == "download_failed"] if len(new) else new
    if len(failed):
        print(f"::notice::download_failed ({len(failed)}): {failed['movie_title'].tolist()[:60]}")
        lines = [f"{r.movie_title} => {r.tried} :: {r.all_candidates[:160]}" for r in failed.itertuples()]
        for i in range(0, len(lines), 12):  # annotations are size-limited; chunk them
            print("::notice::FAILED DETAIL " + " #### ".join(lines[i:i + 12]))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::error::{type(e).__name__}: {str(e)[:500]}")
        raise
