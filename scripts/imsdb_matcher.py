"""
IMSDb (imsdb.com) matcher + TXT exporter.

1) Reads data/movie_list.xlsx (movie_id, movie_title).
2) Reads IMSDb's full list (all-scripts.html): links like
   /Movie Scripts/Brutalist, The Script.html
3) Matches titles (", The" / ", A" suffixes are moved to the front before matching).
4) For matched movies, opens the movie page to find the script link
   (/scripts/Brutalist,-The.html). Some IMSDb entries have no script.
5) --download matched : saves script text as .txt (from <td class="scrtext"><pre>)
   and the original page/PDF into --raw-dir.

Outputs: data/IMSDb_results.xlsx, data/imsdb_txt/, data/imsdb_raw/
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import pandas as pd
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

from dailyscript_matcher import norm as _norm, session

BASE = "https://imsdb.com/"
LIST_URL = urljoin(BASE, "all-scripts.html")
ARTICLE_SUFFIX = re.compile(r"^(.*),\s*(the|a|an)$", re.I)


def norm(title: str) -> str:
    t = str(title or "").strip()
    t = re.sub(r"\s+Script$", "", t, flags=re.I)
    m = ARTICLE_SUFFIX.match(t)
    if m:
        t = f"{m.group(2)} {m.group(1)}"
    return _norm(t)


def get(url: str, timeout: int = 60):
    # IMSDb paths contain spaces/commas; quote everything except URL syntax.
    r = session.get(quote(url, safe=":/?=&%,#"), timeout=timeout)
    r.raise_for_status()
    return r


def build_catalog() -> list[dict]:
    r = get(LIST_URL)
    r.encoding = r.apparent_encoding or r.encoding
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/movie scripts/" not in href.lower():
            continue
        url = urljoin(BASE, href)
        if url in seen:
            continue
        seen.add(url)
        title = " ".join(a.get_text(" ", strip=True).split())
        after = a.next_sibling
        date = ""
        if isinstance(after, str):
            m = re.search(r"\(([^)]*)\)", after)
            date = m.group(1) if m else ""
        writers = ""
        p = a.find_parent(["p", "td", "li"])
        if p:
            i = p.find("i")
            if i:
                writers = " ".join(i.get_text(" ", strip=True).split())
        out.append({"imsdb_title": title, "title_norm": norm(title),
                    "movie_page": url, "script_date": date, "writers": writers})
    if not out:
        title = soup.title.get_text(strip=True) if soup.title else ""
        print(f"::warning::IMSDb list: 0 entries | len={len(r.text)} title={title!r}")
    return out


def find_script_url(movie_page: str) -> str:
    soup = BeautifulSoup(get(movie_page).text, "html.parser")
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.lower().startswith("/scripts/") or "imsdb.com/scripts/" in h.lower():
            return urljoin(BASE, h)
    return ""


def match(df: pd.DataFrame, catalog: list[dict], threshold: int) -> pd.DataFrame:
    groups: dict[str, list[dict]] = {}
    for c in catalog:
        if c["title_norm"]:
            groups.setdefault(c["title_norm"], []).append(c)
    keys = list(groups)
    rows = []
    for _, mv in df.iterrows():
        title = str(mv["movie_title"]).strip()
        n = norm(title)
        key, score = (n, 100.0) if n in groups else (None, 0.0)
        if key is None and n and keys:
            hit = process.extractOne(n, keys, scorer=fuzz.ratio)
            if hit:
                score = float(hit[1])
                key = hit[0] if hit[1] >= threshold else None
        best = groups[key][0] if key else None
        rows.append({
            "movie_id": mv.get("movie_id", ""), "movie_title": title,
            "matched": bool(best), "match_score": round(score, 1),
            "imsdb_title": best["imsdb_title"] if best else "",
            "script_date": best["script_date"] if best else "",
            "writers": best["writers"] if best else "",
            "movie_page": best["movie_page"] if best else "",
            "script_url": "",
        })
    res = pd.DataFrame(rows)
    for i in res.index[res["matched"]]:
        try:
            res.at[i, "script_url"] = find_script_url(res.at[i, "movie_page"])
        except Exception as e:  # noqa: BLE001
            print(f"  movie page error {res.at[i, 'movie_title']}: {e}")
        time.sleep(1)
    res["has_script"] = res["script_url"].astype(str).str.len() > 0
    return res


def page_to_text(resp) -> str:
    if resp.content[:4] == b"%PDF" or "pdf" in resp.headers.get("content-type", "").lower():
        import io
        from pypdf import PdfReader
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(resp.content)).pages)
    else:
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        node = soup.select_one("td.scrtext pre") or soup.select_one("td.scrtext") or soup.find("pre")
        if node is None:
            return ""
        for t in node(["script", "style", "table"]):
            t.decompose()
        text = node.get_text()
    text = "\n".join(l.rstrip() for l in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def download(res: pd.DataFrame, out_dir: Path, raw_dir: Path | None):
    sel = res[res["has_script"]]
    print(f"Downloading IMSDb scripts: {len(sel)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []
    for _, row in sel.iterrows():
        url, title = row["script_url"], str(row["movie_title"])
        safe = re.sub(r'[\\/:*?"<>|]+', "_", f"{row['movie_id']}_{title}").strip(" ._") or "script"
        try:
            resp = get(url)
            if raw_dir is not None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                ext = ".pdf" if resp.content[:4] == b"%PDF" else (Path(url).suffix or ".html")
                (raw_dir / f"{safe}{ext}").write_bytes(resp.content)
            text = page_to_text(resp)
            if len(text) > 500:
                (out_dir / f"{safe}.txt").write_text(text, encoding="utf-8")
                status = f"ok ({len(text):,} chars)"
            else:
                status = "skip: no script text on page"
        except Exception as e:  # noqa: BLE001
            status = f"error: {e}"
        print(f"  {title}: {status}")
        log.append({"movie_id": row["movie_id"], "movie_title": title, "url": url, "status": status})
        time.sleep(1.5)
    pd.DataFrame(log).to_csv(out_dir / "_download_log.csv", index=False, encoding="utf-8-sig")
    ok = sum(l["status"].startswith("ok") for l in log)
    bad = [f"{l['movie_title']}({l['status'][:40]})" for l in log if not l["status"].startswith("ok")]
    print(f"::notice::IMSDb download: txt ok={ok}/{len(log)} | not ok={bad[:40]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/movie_list.xlsx")
    ap.add_argument("--output", default="data/IMSDb_results.xlsx")
    ap.add_argument("--threshold", type=int, default=88)
    ap.add_argument("--download", choices=["none", "matched"], default="none")
    ap.add_argument("--download-dir", default="data/imsdb_txt")
    ap.add_argument("--raw-dir", default="data/imsdb_raw")
    a = ap.parse_args()

    df = pd.read_excel(a.input)
    print("Fetching IMSDb list...")
    catalog = build_catalog()
    print(f"IMSDb catalog entries: {len(catalog)}")
    if not catalog:
        raise RuntimeError("No IMSDb entries parsed — page layout may have changed.")
    res = match(df, catalog, a.threshold)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    res.to_excel(a.output, index=False)
    print(f"::notice::IMSDb catalog={len(catalog)} | matched={int(res['matched'].sum())}/{len(res)}"
          f" | with script page={int(res['has_script'].sum())}")
    if a.download == "matched":
        download(res, Path(a.download_dir), Path(a.raw_dir) if a.raw_dir else None)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::error::{type(e).__name__}: {str(e)[:500]}")
        raise
