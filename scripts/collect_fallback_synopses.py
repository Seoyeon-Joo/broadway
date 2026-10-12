#!/usr/bin/env python3
"""
Wikipedia에서 줄거리를 못 찾았거나 매칭이 불확실한 작품의 시놉시스를 다른 출처에서 채운다.
 - 영화(hollywood): Rotten Tomatoes 영화 페이지의 Synopsis (robots.txt 허용 경로 /m/ 만 사용)
 - 공연(broadway) : AllMusicals 의 synopsis 페이지 (뮤지컬만 해당)
입력 : --targets xlsx (work_id,title,type,year)   출력 : --out xlsx
각 작품마다 시도한 URL과 결과를 debug 열에 남긴다 (실패 원인 확인용).
"""
import argparse
import html
import re
import sys
import time

import pandas as pd
import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

UA = "broadway-synopsis-collector/1.0 (academic thesis; https://github.com/Seoyeon-Joo/broadway)"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
DELAY = 1.5
STOP_LINES = re.compile(r"^(director|producer|screenwriter|writer|genre|rating|rating \(|original language|release date|runtime|"
                        r"distributor|production co|sound mix|aspect ratio|view all|cast & crew|cast and crew|where to watch|"
                        r"what to know|critics consensus|tomatometer|audience score|photos|videos|related|reviews)\b", re.I)


def get(url):
    time.sleep(DELAY)
    try:
        r = S.get(url, timeout=30, allow_redirects=True)
        return r.status_code, r.text, r.url
    except requests.RequestException as e:
        return 0, "", f"ERR {e}"


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(str(s)).lower()).strip()


def slugify(t):
    t = html.unescape(str(t)).lower().replace("&", "and")
    t = re.sub(r"['’.]", "", t)
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")


def visible_lines(page):
    page = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?i)</?(p|div|br|li|h\d|section|tr|rt-text|span)[^>]*>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    lines = [re.sub(r"\s+", " ", html.unescape(x)).strip() for x in page.split("\n")]
    return [x for x in lines if x]


# ---------------- Rotten Tomatoes ----------------
def rt_parse(page):
    """(title, year, synopsis) ; 없으면 None 필드"""
    m = re.search(r"(?is)<title>(.*?)</title>", page)
    title = html.unescape(m.group(1)).strip() if m else ""
    year = None
    m = re.search(r'"dateCreated"\s*:\s*"(\d{4})', page) or re.search(r'"datePublished"\s*:\s*"(\d{4})', page)
    if m:
        year = int(m.group(1))
    syn = None
    m = re.search(r'(?is)data-qa="synopsis-value"[^>]*>(.*?)</', page)
    if m:
        syn = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
    if not syn:
        lines = visible_lines(page)
        for i, ln in enumerate(lines):
            if ln.lower() == "synopsis":
                buf = []
                for nxt in lines[i + 1:i + 12]:
                    if STOP_LINES.match(nxt):
                        break
                    buf.append(nxt)
                syn = " ".join(buf).strip() or None
                break
    return title, year, syn


def rt_candidates(title, year):
    s = slugify(title)
    c = []
    if pd.notna(year):
        y = int(year)
        c += [f"{s}_{y}", f"{s}_{y - 1}", f"{s}_{y + 1}"]
    c.append(s)
    return [f"https://www.rottentomatoes.com/m/{x}" for x in c]


def rt_search(title, year):
    code, page, _ = get("https://www.rottentomatoes.com/search?search=" + requests.utils.quote(str(title)))
    out = []
    for m in re.finditer(r'(?is)<search-page-media-row([^>]*)>(.*?)</search-page-media-row>', page):
        attrs, inner = m.group(1), m.group(2)
        ym = re.search(r'release-year="(\d{4})"', attrs)
        hm = re.search(r'href="(https://www\.rottentomatoes\.com/m/[^"]+)"', inner)
        tm = re.search(r'(?is)slot="title"[^>]*>\s*(.*?)\s*</a>', inner)
        if hm:
            out.append((hm.group(1), int(ym.group(1)) if ym else None, html.unescape(tm.group(1)) if tm else ""))
    return code, out


def do_film(title, year):
    dbg = []
    for url in rt_candidates(title, year):
        code, page, final = get(url)
        dbg.append(f"{url.split('/m/')[1]}:{code}")
        if code != 200:
            continue
        t, y, syn = rt_parse(page)
        ok_year = (pd.isna(year) or y is None or abs(y - int(year)) <= 1)
        ok_title = norm(title)[:20] in norm(t) or norm(t).startswith(norm(title)[:12])
        if syn and ok_year and ok_title:
            return syn, final, "ok (direct)", "; ".join(dbg)
        dbg.append(f"rejected(y={y},title_ok={ok_title},syn={'Y' if syn else 'N'})")
    code, hits = rt_search(title, year)
    dbg.append(f"search:{code}:{len(hits)}hits")
    for url, y, t in hits:
        if norm(t) == norm(title) and (pd.isna(year) or y is None or abs(y - int(year)) <= 1):
            c2, page, final = get(url)
            _, _, syn = rt_parse(page) if c2 == 200 else (None, None, None)
            if syn:
                return syn, final, "ok (search)", "; ".join(dbg)
            dbg.append(f"{url.split('/m/')[1]}:nosyn")
    return None, "", "not found", "; ".join(dbg)


# ---------------- AllMusicals ----------------
def do_stage(title, year):
    dbg = []
    s = re.sub(r"[^a-z0-9]", "", html.unescape(str(title)).lower())
    s2 = re.sub(r"^the", "", s)
    for slug in dict.fromkeys([s, s2]):
        for suffix in ("synopsis.htm", "synopsis.amp.htm"):
            url = f"https://www.allmusicals.com/lyrics/{slug}/{suffix}"
            code, page, final = get(url)
            dbg.append(f"{slug}/{suffix}:{code}")
            if code != 200:
                continue
            lines = visible_lines(page)
            body = [ln for ln in lines if len(ln) > 80 and not re.search(r"(?i)copyright|cookie|privacy|advert", ln)]
            if len(" ".join(body).split()) > 120:
                return "\n\n".join(body), final, "ok (allmusicals)", "; ".join(dbg)
    return None, "", "not found", "; ".join(dbg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["hollywood", "broadway"])
    ap.add_argument("--targets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    df = pd.read_excel(a.targets)
    if a.limit:
        df = df.head(a.limit)
    fn = do_film if a.market == "hollywood" else do_stage
    rows = []
    for i, r in df.iterrows():
        try:
            syn, url, status, dbg = fn(r["title"], r.get("year"))
        except Exception as e:
            syn, url, status, dbg = None, "", "error", str(e)[:200]
        rows.append([r["work_id"], r["title"], r.get("year"), (syn or "")[:32000], url, status, dbg])
        print(f"[{i + 1}/{len(df)}] {r['title']} -> {status}", flush=True)
        if (i + 1) % 50 == 0:
            save(rows, a.out)
    save(rows, a.out)
    print("saved", a.out, len(rows), "ok:", sum(1 for x in rows if x[5].startswith("ok")))


def save(rows, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "fallback"
    ws.append(["work_id", "title", "year", "synopsis", "source_url", "status", "debug"])
    for r in rows:
        ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
    for col, w in zip("ABCDEFG", [8, 34, 7, 100, 50, 18, 60]):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "C2"
    wb.save(path)


if __name__ == "__main__":
    sys.exit(main())
