#!/usr/bin/env python3
"""
TMDB API로 영화 개요(overview)를 수집한다. 제목+연도로 검색하고, 제목 일치·개봉연도(±1)를 검증한 결과만 채택한다.
환경변수 TMDB_API_KEY: v3 API 키(32자리 hex) 또는 v4 Read Access Token(긴 JWT) 모두 가능.
입력 : --targets xlsx (work_id,title,year)   출력 : --out xlsx
열   : work_id,title,year,tmdb_id,tmdb_title,tmdb_year,overview_en,overview_ko,words_en,status,debug
Attribution: This product uses the TMDB API but is not endorsed or certified by TMDB.
"""
import argparse, html, os, re, sys, time
import pandas as pd, requests

KEY = os.environ.get("TMDB_API_KEY", "").strip()
if not KEY:
    sys.exit("TMDB_API_KEY 가 설정되지 않았습니다 (저장소 Settings > Secrets and variables > Actions).")
S = requests.Session()
if len(KEY) > 40:
    S.headers["Authorization"] = f"Bearer {KEY}"
BASE = "https://api.themoviedb.org/3"
STOP = {"the", "a", "an", "of", "and", "film", "movie"}


def get(path, **p):
    if len(KEY) <= 40:
        p["api_key"] = KEY
    time.sleep(0.12)
    for _ in range(3):
        r = S.get(BASE + path, params=p, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5"))); continue
        if r.status_code == 401:
            sys.exit("TMDB 인증 실패(401): 키가 올바르지 않습니다.")
        return r.json() if r.status_code == 200 else {}
    return {}


def tok(s):
    return set(re.findall(r"[a-z0-9]+", html.unescape(str(s)).lower())) - STOP


def same(a, b):
    A, B = tok(a), tok(b)
    return bool(A and B) and len(A & B) / len(A) >= 0.8 and len(A & B) / len(B) >= 0.8


def find(title, year):
    dbg = []
    yrs = [int(year)] if pd.notna(year) else [None]
    for q in dict.fromkeys([str(title), re.sub(r"\s*[:\-–].*$", "", str(title))]):
        if not q.strip():
            continue
        for y in yrs:
            p = {"query": q, "include_adult": "false", "language": "en-US"}
            if y:
                p["primary_release_year"] = y
            res = get("/search/movie", **p).get("results", [])
            dbg.append(f"{q}[{y}]:{len(res)}")
            for m in res[:8]:
                my = int(m["release_date"][:4]) if m.get("release_date") else None
                if same(title, m["title"]) or same(title, m.get("original_title", "")):
                    if y is None or my is None or abs(my - y) <= 1:
                        return m, my, dbg
        # 연도 필터 없이 재시도 (TMDB의 개봉일이 다를 수 있음)
        res = get("/search/movie", query=q, language="en-US").get("results", [])
        for m in res[:8]:
            my = int(m["release_date"][:4]) if m.get("release_date") else None
            if same(title, m["title"]) and (yrs[0] is None or my is None or abs(my - yrs[0]) <= 1):
                return m, my, dbg + ["noyearfilter"]
    return None, None, dbg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    df = pd.read_excel(a.targets)
    if a.limit:
        df = df.head(a.limit)
    rows = []
    for i, r in df.iterrows():
        title, year = r["title"], r.get("year")
        try:
            m, my, dbg = find(title, year)
            if m:
                en = (m.get("overview") or "").strip()
                if not en:
                    en = (get(f"/movie/{m['id']}", language="en-US").get("overview") or "").strip()
                ko = (get(f"/movie/{m['id']}", language="ko-KR").get("overview") or "").strip()
                status = "ok" if en else ("ko_only" if ko else "no_overview")
                rows.append([r["work_id"], title, year, m["id"], m["title"], my, en, ko, len(en.split()), status, " | ".join(dbg)])
            else:
                rows.append([r["work_id"], title, year, None, "", None, "", "", 0, "not found", " | ".join(dbg)])
        except Exception as e:
            rows.append([r["work_id"], title, year, None, "", None, "", "", 0, "error", str(e)[:150]])
        print(f"[{i+1}/{len(df)}] {title} -> {rows[-1][9]}", flush=True)
        if (i + 1) % 50 == 0:
            save(rows, a.out)
    save(rows, a.out)
    print("ok:", sum(1 for x in rows if x[9] == "ok"), "/", len(rows))


def save(rows, path):
    pd.DataFrame(rows, columns=["work_id", "title", "year", "tmdb_id", "tmdb_title", "tmdb_year", "overview_en", "overview_ko", "words_en", "status", "debug"]).to_excel(path, index=False)


if __name__ == "__main__":
    main()
