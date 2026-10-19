#!/usr/bin/env python3
"""TMDB 제작비(budget)·수익(revenue) 수집. 제목+연도 검증(collect_tmdb_overviews.find) 후 /movie/{id}.
TMDB는 값이 없으면 0을 반환 -> 빈칸(None)으로 저장. 단위 USD, 숫자만.
Attribution: This product uses the TMDB API but is not endorsed or certified by TMDB."""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(__file__))
import pandas as pd
import collect_tmdb_overviews as T

COLS = ["work_id","title","year","tmdb_id","tmdb_title","tmdb_year","tmdb_release_date","budget","revenue","status","debug"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    df = pd.read_excel(a.targets)
    if a.limit: df = df.head(a.limit)
    rows = []
    for i, r in df.iterrows():
        title, year = r["title"], r.get("year")
        try:
            m, my, dbg = T.find(title, year)
            if m:
                d = T.get(f"/movie/{m['id']}", language="en-US")
                b, rv = d.get("budget") or None, d.get("revenue") or None
                st = "ok" if (b or rv) else "no_financials"
                rows.append([r["work_id"], title, year, m["id"], m["title"], my, d.get("release_date"), b, rv, st, " | ".join(dbg)])
            else:
                rows.append([r["work_id"], title, year, None, "", None, None, None, None, "not found", " | ".join(dbg)])
        except Exception as e:
            rows.append([r["work_id"], title, year, None, "", None, None, None, None, "error", str(e)[:150]])
        print(f"[{i+1}/{len(df)}] {title} -> {rows[-1][9]}", flush=True)
        if (i + 1) % 100 == 0: pd.DataFrame(rows, columns=COLS).to_excel(a.out, index=False)
    pd.DataFrame(rows, columns=COLS).to_excel(a.out, index=False)
    print(pd.DataFrame(rows, columns=COLS).status.value_counts().to_dict())

if __name__ == "__main__":
    main()
