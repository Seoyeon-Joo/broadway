"""
Daily Script matcher + TXT exporter.

1) Reads data/movie_list.xlsx (columns: movie_id, movie_title).
2) Reads Daily Script's public A-M and N-Z movie index pages.
3) Matches titles using normalized exact / fuzzy matching.
4) Writes data/DailyScript_results.xlsx (best match + every available version).
5) Optionally saves screenplays as .txt:
     --download approved : only URLs listed in data/approved_urls.txt
     --download matched  : every matched movie (best version per movie)
   HTML/TXT pages are converted to plain text; PDFs are text-extracted with pypdf.

Notes
- Daily Script is a legacy page: all entries sit in one big block separated by
  <br>, so metadata is read from the text that follows each script link up to
  the next script link (NOT from the link's parent element, which would be the
  whole page).
- Requests are throttled (1.5s between downloads).
"""

from __future__ import annotations

import argparse
import io
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from rapidfuzz import fuzz, process

BASES = ["https://www.dailyscript.com/", "http://www.dailyscript.com/"]
INDEX_PAGES = ["movie.html", "movie_n-z.html"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DailyScriptMatcher/1.1; academic research)"}
FORMAT_PRIORITY = {"txt": 0, "text": 0, "html": 1, "htm": 1, "pdf": 2}

session = requests.Session()
session.headers.update(HEADERS)


def norm(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def get(url: str, timeout: int = 30) -> requests.Response:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def fetch_index(page: str) -> tuple[str, str]:
    """Fetch an index page, falling back from https to http."""
    last = None
    for base in BASES:
        url = urljoin(base, page)
        try:
            r = get(url)
            r.encoding = r.apparent_encoding or r.encoding
            return r.text, url
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  fetch failed {url}: {e}")
            print(f"::warning::fetch failed {url}: {type(e).__name__}: {str(e)[:300]}")
    raise RuntimeError(f"Could not fetch {page}: {last}")


def is_script_link(a: Tag, base: str = "https://www.dailyscript.com/") -> bool:
    href = urljoin(base, a.get("href", "").strip()).lower()
    return "dailyscript.com/scripts/" in href


def text_after(a: Tag, limit: int = 400) -> str:
    """Text following the anchor until the next script link."""
    parts: list[str] = []
    for el in a.next_elements:
        if isinstance(el, Tag) and el.name == "a" and el is not a and is_script_link(el):
            break
        if isinstance(el, NavigableString) and a not in el.parents:
            parts.append(str(el))
            if sum(len(p) for p in parts) > limit:
                break
    return " ".join(" ".join(parts).split())


def parse_index(html: str, index_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = {}
    for a in soup.find_all("a", href=True):
        if not is_script_link(a):
            continue
        href = urljoin(index_url, a["href"])
        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            continue
        meta = text_after(a)

        m = re.search(r"\b(pdf|html|htm|txt|text|rtf|doc)\s+format\b", meta, re.I)
        fmt = m.group(1).lower() if m else ""
        if not fmt:  # fall back to file extension
            ext = Path(href.split("?")[0]).suffix.lower().lstrip(".")
            fmt = ext if ext in FORMAT_PRIORITY else ext
        ym = re.search(r"\b((?:19|20)\d{2})\b", meta)  # first year = release year

        records.setdefault(href, {
            "script_title": title,
            "script_title_norm": norm(title),
            "url": href,
            "format": fmt,
            "year": ym.group(1) if ym else "",
            "metadata": f"{title} {meta}".strip(),
        })
    return list(records.values())


def build_catalog() -> list[dict]:
    catalog, seen = [], set()
    for page in INDEX_PAGES:
        html, url = fetch_index(page)
        recs = parse_index(html, url)
        print(f"  {page}: {len(recs)} script links")
        if not recs:
            soup = BeautifulSoup(html, "html.parser")
            hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)][:15]
            title = soup.title.get_text(strip=True) if soup.title else ""
            print(f"::warning::{page}: 0 script links | len={len(html)} title={title!r} sample_hrefs={hrefs}")
        for r in recs:
            key = r["url"].split("://", 1)[-1]
            if key not in seen:
                seen.add(key)
                catalog.append(r)
    return catalog


def match_movies(df: pd.DataFrame, catalog: list[dict], threshold: int = 88) -> pd.DataFrame:
    groups: dict[str, list[dict]] = {}
    for r in catalog:
        if r["script_title_norm"]:
            groups.setdefault(r["script_title_norm"], []).append(r)
    for versions in groups.values():  # prefer txt > html > pdf
        versions.sort(key=lambda r: FORMAT_PRIORITY.get(r["format"], 9))
    keys = list(groups)

    rows = []
    for _, movie in df.iterrows():
        title = str(movie["movie_title"]).strip()
        n = norm(title)
        key, score = (n, 100) if n in groups else (None, 0)
        if key is None and keys and n:
            hit = process.extractOne(n, keys, scorer=fuzz.ratio)
            if hit and hit[1] >= threshold:
                key, score = hit[0], hit[1]
            elif hit:
                score = hit[1]
        versions = groups.get(key, []) if key else []
        best = versions[0] if versions else None
        rows.append({
            "movie_id": movie.get("movie_id", ""),
            "movie_title": title,
            "matched": bool(best),
            "match_score": round(float(score), 1),
            "script_title": best["script_title"] if best else "",
            "script_year": best["year"] if best else "",
            "script_format": best["format"] if best else "",
            "script_url": best["url"] if best else "",
            "n_versions": len(versions),
            "all_urls": " | ".join(v["url"] for v in versions),
            "metadata": best["metadata"] if best else "",
        })
    return pd.DataFrame(rows)


def to_text(resp: requests.Response, fmt: str) -> str | None:
    ctype = resp.headers.get("content-type", "").lower()
    if "pdf" in ctype or fmt == "pdf" or resp.content[:4] == b"%PDF":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(resp.content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    elif "html" in ctype or fmt in {"html", "htm"}:
        soup = BeautifulSoup(resp.content, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text("\n")
    elif "text" in ctype or fmt in {"txt", "text"}:
        resp.encoding = resp.apparent_encoding or resp.encoding
        text = resp.text
    else:
        return None
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def download(results: pd.DataFrame, mode: str, approved_file: Path, out_dir: Path):
    matched = results[results["matched"]]
    if mode == "approved":
        if not approved_file.exists():
            print(f"No approved list found: {approved_file}")
            return
        approved = {
            l.strip() for l in approved_file.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")
        }
        selected = matched[matched["script_url"].isin(approved)]
        print(f"Approved URLs: {len(approved)} | matched & approved: {len(selected)}")
    else:
        selected = matched
        print(f"Downloading all matched scripts: {len(selected)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    log = []
    for _, row in selected.iterrows():
        url, title = row["script_url"], str(row["movie_title"])
        safe = re.sub(r'[\\/:*?"<>|]+', "_", f"{row['movie_id']}_{title}").strip(" ._") or "script"
        status = ""
        try:
            text = to_text(get(url, timeout=60), row["script_format"])
            if text and len(text) > 500:
                (out_dir / f"{safe}.txt").write_text(text, encoding="utf-8")
                status = f"ok ({len(text):,} chars)"
            else:
                status = "skip: unsupported format or empty text (scanned PDF?)"
        except Exception as e:  # noqa: BLE001
            status = f"error: {e}"
        print(f"  {title}: {status}")
        log.append({"movie_id": row["movie_id"], "movie_title": title, "url": url, "status": status})
        time.sleep(1.5)
    pd.DataFrame(log).to_csv(out_dir / "_download_log.csv", index=False, encoding="utf-8-sig")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/movie_list.xlsx")
    ap.add_argument("--output", default="data/DailyScript_results.xlsx")
    ap.add_argument("--threshold", type=int, default=88)
    ap.add_argument("--download", choices=["none", "approved", "matched"], default="none")
    ap.add_argument("--approved", default="data/approved_urls.txt")
    ap.add_argument("--download-dir", default="data/dailyscript_txt")
    args = ap.parse_args()

    df = pd.read_excel(args.input)
    if "movie_title" not in df.columns:
        raise ValueError("Input Excel must contain a 'movie_title' column.")

    print("Fetching Daily Script public indexes...")
    catalog = build_catalog()
    print(f"Catalog entries found: {len(catalog)}")
    if not catalog:
        raise RuntimeError("No script links parsed — page layout may have changed.")

    results = match_movies(df, catalog, args.threshold)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    results.to_excel(args.output, index=False)
    print(f"Saved: {args.output}")
    print(f"Matched: {int(results['matched'].sum())}/{len(results)}")
    fmts = results.loc[results["matched"], "script_format"].value_counts().to_dict()
    print(f"::notice::Daily Script catalog={len(catalog)} | matched={int(results['matched'].sum())}/{len(results)} | formats={fmts}")

    if args.download != "none":
        download(results, args.download, Path(args.approved), Path(args.download_dir))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface the cause as a GitHub Actions annotation
        print(f"::error::{type(e).__name__}: {str(e)[:500]}")
        raise
