"""
fetch_tony_awards.py
=============================
BroadwayWorld의 쇼별 토니상 전용 페이지에서 수상/후보 이력을 수집.

  https://www.broadwayworld.com/tonyawardsshowinfo.php?showname=<쇼이름>

실제로 이 URL을 fetch해서 확인함 (Wicked 예시):
  "Wicked Tony Awards Stats" 아래 "3 wins, 16 nominations in our Tony Awards
  database." 요약 문장이 있고, 그 아래 표에 연도/부문/후보자별로 "Award winner"
  표시가 붙어있음. IBDB의 자바스크립트 Awards 탭보다 훨씬 안정적이라 이쪽으로
  전환함 (이전 fetch_ibdb_awards.py는 폐기).

이전 실행에서 크래시가 났던 두 가지를 고쳤음:
  1. 네트워크 타임아웃/일시적 오류로 전체 job이 죽지 않도록, requests에
     urllib3 Retry(재시도)를 붙이고 timeout을 20초 -> 30초로 늘림
  2. 쇼 하나 처리 중 예외가 나도 그 쇼만 스킵하고 나머지는 계속 진행하도록
     루프 본문 전체를 try/except로 감쌈

Usage:
  python fetch_tony_awards.py --raw data/broadway.csv \
      --existing data/tony_awards.csv --out-dir data --sleep 1.0
  python fetch_tony_awards.py --shows "Wicked" "42nd Street" --out-dir . --limit 2  # 테스트용
"""
import argparse
import os
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.broadwayworld.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

SUMMARY_RE = re.compile(
    r"(\d+)\s+wins?,\s*(\d+)\s+nominations?\s+in\s+our\s+Tony\s+Awards\s+database",
    re.IGNORECASE,
)


def make_session():
    session = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_tony_page(title, session, timeout=30):
    url = f"{BASE}/tonyawardsshowinfo.php"
    resp = session.get(url, params={"showname": title}, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_tony_stats(html):
    """summary 문장에서 총 wins/nominations 숫자를 뽑고,
    아래 표(<table>)에서 연도/부문/후보자/수상여부를 행 단위로 파싱.
    표 구조(실제 fetch로 확인함): 각 행이 년도 링크, 부문 링크, 후보자 링크,
    그리고 수상한 행에만 'Award winner' 텍스트가 마지막 셀에 들어있음."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    m = SUMMARY_RE.search(text)
    if not m:
        return 0, 0, []
    wins, noms = int(m.group(1)), int(m.group(2))

    detail = []
    table = soup.find("table")
    if table:
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue
            row_text = [c.get_text(" ", strip=True) for c in cells]
            year = row_text[0]
            if not re.match(r"^\d{4}$", year):
                continue
            category = row_text[1] if len(row_text) > 1 else ""
            nominee = row_text[2] if len(row_text) > 2 else ""
            is_winner = any("award winner" in c.lower() for c in row_text)
            detail.append({
                "year": year,
                "category": category,
                "nominee": nominee,
                "result": "Winner" if is_winner else "Nominee",
            })
    return wins, noms, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="broadway.csv 경로 - 'show' 컬럼에서 고유 쇼 이름을 뽑음")
    ap.add_argument("--shows", nargs="+", default=None, help="테스트용: 쇼 이름을 직접 나열")
    ap.add_argument("--existing", default=None,
                     help="기존 tony_awards.csv 경로. 있으면 그 안에 이미 있는 title은 건너뜀")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    session = make_session()

    if args.shows:
        titles = args.shows
    elif args.raw:
        df = pd.read_csv(args.raw, sep=None, engine="python", encoding="utf-8-sig")
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        if "show" not in df.columns:
            print(f"'show' 컬럼이 없어요. 실제 컬럼: {list(df.columns)}")
            raise SystemExit(1)
        titles = df["show"].dropna().drop_duplicates().tolist()
    else:
        ap.error("--raw 또는 --shows 중 하나는 필요해요")

    existing_df = None
    if args.existing and os.path.isfile(args.existing):
        existing_df = pd.read_csv(args.existing, sep=None, engine="python", encoding="utf-8-sig")
        already_done = set(existing_df["title"])
        before = len(titles)
        titles = [t for t in titles if t not in already_done]
        print(f"기존 {len(already_done)}개 쇼는 건너뜀 ({before} -> {len(titles)}개 신규 처리 대상)")

    if args.limit:
        titles = titles[: args.limit]

    print(f"총 {len(titles)}개 쇼 처리 예정")

    rows = []
    n_errors = 0
    for i, title in enumerate(titles, 1):
        try:
            html = fetch_tony_page(title, session)
            wins, noms, detail = parse_tony_stats(html)
            detail_str = "; ".join(f"{d['year']} {d['category']} - {d['nominee']} ({d['result']})" for d in detail)
            rows.append({
                "title": title,
                "tony_wins": wins,
                "tony_nominations": noms,
                "awards_detail": detail_str,
            })
            print(f"[{i}/{len(titles)}] '{title}' -> {wins}승 {noms}노미네이션")
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(titles)}] '{title}' -> 오류 발생, 스킵: {e}")
            rows.append({"title": title, "tony_wins": pd.NA, "tony_nominations": pd.NA, "awards_detail": ""})
        time.sleep(args.sleep)

    print(f"\n처리 중 오류 {n_errors}건 (스킵하고 계속 진행함)")

    if not rows:
        print("수집된 데이터가 없어요.")
        return

    new_df = pd.DataFrame(rows)
    if existing_df is not None:
        out_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(subset=["title"], keep="last")
    else:
        out_df = new_df

    out_path = os.path.join(args.out_dir, "tony_awards.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"저장 완료: 신규 {len(new_df)}개 + 기존 -> 총 {len(out_df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
