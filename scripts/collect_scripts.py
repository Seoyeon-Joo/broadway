"""
Combined screenplay collector — one script per movie, sources in priority order.

  1. SimplyScripts award-season pages (studio FYC scripts, 2018-2026)
  2. SimplyScripts full movie list (links to many hosts)
  3. IMSDb
  4. Daily Script

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
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

import dailyscript_matcher as ds
import imsdb_matcher as im
import simplyscripts_source as ss
from imsdb_matcher import norm  # handles "Title, The" -> "the title"

session = ds.session
MIN_CHARS = 15000  # a feature screenplay is typically 100k+ characters
SOURCE_RANK = {"simplyscripts_award": 0, "simplyscripts": 1, "imsdb": 2, "dailyscript": 3}


# ----------------------------------------------------------------- catalogs
def load_catalog() -> list[dict]:
    cands: list[dict] = []
    print("SimplyScripts...")
    for e in ss.build_catalog(session):
        cands.append({"source": "simplyscripts_award" if e["award_year"] else "simplyscripts",
                      "title": e["title"], "url": e["url"], "year": e["script_year"],
                      "award_year": e["award_year"], "info": f"{e['host']} | {e['draft_info']}"})
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
    counts = pd.Series([c["source"] for c in cands]).value_counts().to_dict()
    print(f"Catalog: {len(cands)} entries {counts}")
    return [c for c in cands if c["norm"]]


def release_years(path: str) -> dict:
    try:
        h = pd.read_excel(path, usecols=["movie_id", "Year"])
        return h.groupby("movie_id")["Year"].min().astype(int).to_dict()
    except Exception as e:  # noqa: BLE001
        print(f"::warning::release years unavailable ({e}); year check disabled")
        return {}


def year_check(c: dict, rel: int | None) -> str:
    """'ok' | 'fail' | 'unknown'"""
    if not rel:
        return "unknown"
    if c["award_year"]:
        return "ok" if rel - 1 <= int(c["award_year"]) - 1 <= rel + 1 else "fail"
    if c["year"]:
        y = int(c["year"])
        return "ok" if rel - 8 <= y <= rel + 1 else "fail"
    return "unknown"


def candidates_for(title: str, rel, by_norm: dict, keys: list, fuzzy: int) -> list[dict]:
    n = norm(title)
    found = [dict(c, score=100.0) for c in by_norm.get(n, [])]
    if n:
        for key, score, _ in process.extract(n, keys, scorer=fuzz.ratio, limit=5):
            if key != n and score >= fuzzy:
                found += [dict(c, score=float(score)) for c in by_norm[key]]
    out = []
    for c in found:
        c["year_check"] = year_check(c, rel)
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
    r = session.get(url, timeout=90, allow_redirects=True, headers={"User-Agent": BROWSER_UA})
    r.raise_for_status()
    return r


def wayback_url(url: str) -> str:
    """Closest archived snapshot as raw content (id_), or ''."""
    if "web.archive.org" in url:
        return ""
    try:
        j = session.get("https://archive.org/wayback/available", params={"url": url}, timeout=40).json()
        snap = (j.get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available") and snap.get("url"):
            return re.sub(r"(/web/\d+)/", r"\1id_/", snap["url"].replace("http://", "https://", 1), count=1)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _get(url: str):
    """GET with Wayback Machine fallback for dead links (404/403/410/5xx/connection errors)."""
    try:
        return _get_once(url)
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code is not None and code not in (403, 404, 410) and code < 500:
            raise
        wb = wayback_url(url)
        if not wb:
            raise
        print(f"    wayback: {url} -> {wb}")
        r = _get_once(wb)
        r.wayback = True
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

    url = ss.resolve_download_url(url)
    if "drive.google.com" in url:
        return _fetch_drive(url)
    if ".box.com/s/" in url:
        return _fetch_box(url)
    r = _get(url)
    if getattr(r, "wayback", False):
        url = r.url
    if _is_pdf(r):
        return _clean(_pdf_text(r.content)), r.content, ".pdf", url
    ctype = r.headers.get("content-type", "").lower()
    if "text/plain" in ctype or urlparse(url).path.lower().endswith(".txt"):
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
    return text, r.content, ".html", url


def _fetch_drive(url: str):
    fid = re.search(r"id=([\w-]+)", url).group(1)
    import tempfile
    import gdown
    with tempfile.TemporaryDirectory() as td:
        out = gdown.download(id=fid, output=f"{td}/f", quiet=True, fuzzy=True)
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
    raise ValueError("Box shared link: no direct PDF download")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/movie_list.xlsx")
    ap.add_argument("--release-years", default="data/hollywood.xlsx")
    ap.add_argument("--output", default="data/script_collection.xlsx")
    ap.add_argument("--txt-dir", default="data/scripts_txt")
    ap.add_argument("--raw-dir", default="data/scripts_raw")
    ap.add_argument("--fuzzy", type=int, default=92)
    ap.add_argument("--max-tries", type=int, default=4, help="candidates tried per movie")
    ap.add_argument("--match-only", action="store_true")
    ap.add_argument("--titles", default="", help="only these movie titles (separated by |), for debugging")
    a = ap.parse_args()

    movies = pd.read_excel(a.input)
    if a.titles:
        want = {t.strip() for t in a.titles.split("|") if t.strip()}
        movies = movies[movies["movie_title"].astype(str).str.strip().isin(want)]
    rel = release_years(a.release_years)
    catalog = load_catalog()
    by_norm: dict[str, list[dict]] = {}
    for c in catalog:
        by_norm.setdefault(c["norm"], []).append(c)
    keys = list(by_norm)

    txt_dir, raw_dir = Path(a.txt_dir), Path(a.raw_dir)
    txt_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for _, mv in movies.iterrows():
        mid, title = mv["movie_id"], str(mv["movie_title"]).strip()
        ry = rel.get(mid)
        cands = candidates_for(title, ry, by_norm, keys, a.fuzzy)
        row = {"movie_id": mid, "movie_title": title, "release_year": ry,
               "n_candidates": len(cands), "status": "no_match" if not cands else "matched",
               "source": "", "matched_title": "", "match_score": "", "year_check": "",
               "script_url": "", "via": "", "chars": 0, "script_info": "", "tried": "",
               "all_candidates": " || ".join(f"{c['source']}:{c['title']}:{c['url']}" for c in cands)}
        if cands and not a.match_only:
            row["status"] = "download_failed"
            tried = []
            for c in cands[: a.max_tries]:
                try:
                    text, raw, ext, final = fetch_script(c, title)
                    ok = len(text) >= MIN_CHARS
                    tried.append(f"{c['source']}:{'ok' if ok else f'short({len(text)})'}")
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
            row["tried"] = " ; ".join(tried)
            print(f"  [{row['status']}] {title} ({ry}) <- {row['source'] or '-'} | {row['tried']}")
        elif cands:
            c = cands[0]
            row.update(source=c["source"], matched_title=c["title"], match_score=c["score"],
                       year_check=c["year_check"], script_url=c["url"], script_info=c["info"][:300])
        rows.append(row)

    res = pd.DataFrame(rows)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    res.to_excel(a.output, index=False)
    st = res["status"].value_counts().to_dict()
    src = res.loc[res["status"] == "ok", "source"].value_counts().to_dict()
    print(f"::notice::movies={len(res)} | status={st} | ok by source={src} | OCR used={len(OCR_USED)}")
    failed = res[res["status"] == "download_failed"]
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
