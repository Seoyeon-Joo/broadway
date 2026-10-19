#!/usr/bin/env python3
"""
기존 synopses_*.xlsx 에서 (A) 페이지 없음 (B) 연도·목록·상이 페이지가 붙음 (C) 제목 불일치 의심 (D) 도입 문단만 있음
작품의 시놉시스를 다시 찾는다.
순서: ① source_title(원작 페이지 등 수동 지정) ② Wikipedia 제목 후보 직접 조회 ③ Wikipedia 검색(제목·연도 엄격 검증)
      ④ Rotten Tomatoes(영화) / AllMusicals(뮤지컬)  → 모두 collect_fallback_synopses.py 의 함수를 재사용
검증: 후보 페이지 제목에서 괄호를 뗀 토큰이 작품 제목과 80% 이상 겹치고, 괄호 속 연도가 작품 연도와 ±1 이내여야 채택.
출력 열: work_id,title,year,synopsis,source_kind,source_title_or_url,plot_words,status,debug
사용: python scripts/rematch_synopses.py --market hollywood --targets data/rematch_targets_hollywood.xlsx --out data/synopses_rematch_hollywood.xlsx
"""
import argparse, html, re, sys, time
import pandas as pd, requests
sys.path.insert(0, __file__.rsplit("/", 1)[0])
import collect_fallback_synopses as fb

API = "https://en.wikipedia.org/w/api.php"
S = requests.Session(); S.headers.update({"User-Agent": fb.UA})
STOP = {"film", "the", "a", "an", "of", "and", "movie", "in", "to"}
PLOT_H = ("plot", "plot summary", "synopsis", "premise", "summary", "story", "storyline", "overview", "plot synopsis")
GENERIC = re.compile(r"^\d{4}( in |–)|^List of|Award|Festival|film series|\(human activity\)", re.I)


def tok(s):
    return set(re.findall(r"[a-z0-9]+", re.sub(r"\([^)]*\)", "", html.unescape(str(s)).lower()))) - STOP


def api(**p):
    time.sleep(0.4)
    p.update(format="json", formatversion=2)
    r = S.get(API, params=p, timeout=30)
    return r.json() if r.status_code == 200 else {}


def wp_text(title):
    d = api(action="query", prop="extracts", explaintext=1, redirects=1, titles=title)
    pg = (d.get("query", {}).get("pages") or [{}])[0]
    if pg.get("missing") or not pg.get("extract"):
        return None, None
    return pg["title"], pg["extract"]


def split_plot(text):
    parts = re.split(r"\n==+\s*(.*?)\s*==+\n", "\n" + text)
    lead = parts[0].strip()
    for i in range(1, len(parts) - 1, 2):
        if parts[i].strip().lower() in PLOT_H:
            body = parts[i + 1].strip()
            if len(body.split()) >= 40:
                return body, lead
    return None, lead


def title_ok(work_title, year, wp_title):
    if GENERIC.search(wp_title):
        return False
    a, b = tok(work_title), tok(wp_title)
    if not a or not b or len(a & b) / len(a) < 0.8 or len(a & b) / len(b) < 0.8:
        return False
    m = re.search(r"\((\d{4})\b", wp_title)
    return not (m and pd.notna(year) and abs(int(m.group(1)) - int(year)) > 1)


def candidates(title, year, kind):
    t = html.unescape(str(title)); y = int(year) if pd.notna(year) else None
    if kind == "film":
        c = ([f"{t} ({y} film)", f"{t} ({y - 1} film)", f"{t} ({y + 1} film)"] if y else []) + [f"{t} (film)", t]
    else:
        c = [f"{t} (musical)", f"{t} (play)", t]
    return c


def wikipedia(title, year, kind, hint=""):
    dbg = []
    for c in candidates(title, year, kind):
        n, txt = wp_text(c); dbg.append(f"{c}:{'Y' if txt else 'N'}")
        if txt and title_ok(title, year, n):
            plot, lead = split_plot(txt)
            if plot:
                return plot, "wikipedia_plot", n, dbg
    q = f"{title} {hint or (str(int(year)) + ' film' if kind == 'film' and pd.notna(year) else '')}".strip()
    for h in api(action="query", list="search", srsearch=q, srlimit=5).get("query", {}).get("search", []):
        if not title_ok(title, year, h["title"]):
            continue
        n, txt = wp_text(h["title"])
        if txt:
            plot, _ = split_plot(txt)
            dbg.append(f"search:{h['title']}")
            if plot:
                return plot, "wikipedia_plot(search)", n, dbg
    return None, "", "", dbg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["hollywood", "broadway"])
    ap.add_argument("--targets", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    df = pd.read_excel(a.targets)
    if a.limit: df = df.head(a.limit)
    kind = "film" if a.market == "hollywood" else "stage"
    rows = []
    for i, r in df.iterrows():
        title, year, dbg = r["title"], r.get("year"), []
        syn = src = ref = ""
        try:
            if isinstance(r.get("source_title"), str) and r["source_title"].strip():   # ① 원작 등 수동 지정
                n, txt = wp_text(r["source_title"])
                p, lead = split_plot(txt) if txt else (None, None)
                if p: syn, src, ref = p, "source_material_plot", n
            if not syn:                                                                  # ②③
                p, src, ref, d = wikipedia(title, year, kind, str(r.get("search_hint") or ""))
                syn, dbg = p or "", d
            if not syn:                                                                  # ④
                f = fb.do_film if kind == "film" else fb.do_stage
                p, url, st, d = f(title, year)
                if p: syn, src, ref = p, "rottentomatoes" if kind == "film" else "allmusicals", url
                dbg.append(d)
            status = "ok" if syn else "not found"
        except Exception as e:
            status = "error"; dbg.append(str(e)[:150])
        rows.append([r["work_id"], title, year, syn[:32000], src, ref, len(syn.split()), status, " | ".join(map(str, dbg))[:400]])
        print(f"[{i+1}/{len(df)}] {title} -> {status} {src}", flush=True)
        if (i + 1) % 25 == 0: save(rows, a.out)
    save(rows, a.out)
    print("ok:", sum(1 for x in rows if x[7] == "ok"), "/", len(rows))


def save(rows, path):
    pd.DataFrame(rows, columns=["work_id", "title", "year", "synopsis", "source_kind", "source_title_or_url", "plot_words", "status", "debug"]).to_excel(path, index=False)


if __name__ == "__main__":
    main()
