"""
fetch_bww_reviews.py
=============================
BroadwayWorld의 쇼별 리뷰 페이지에서 평론가/독자 평점을 수집.

  https://www.broadwayworld.com/reviews/<Title-Slug>?id=<showid>

실제로 fetch해서 확인함(American Psycho, showid=331080):
  "Critics' Rating  7.19 [icon] Mixed  [icon] 6 Positive  [icon] 15 Mixed
   [icon] 0 Negative  Readers' Rating  5.16 [icon] Mixed"
  개별 리뷰는 "**From:** Wall Street Journal | **By:** Terry Teachout |
  **Date:** 4/21/2016" 형태로 매체/평론가/날짜가 반복됨.

Usage:
  python fetch_bww_reviews.py --raw data/broadway.csv --showids data/broadwayworld_full.csv \
      --existing data/bww_reviews.csv --out-dir data --sleep 1.0
  python fetch_bww_reviews.py --shows "American Psycho:331080" --out-dir . --limit 1  # 테스트용
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

CRITICS_SCORE_RE = re.compile(r"Critics['\u2019]\s*Rating\s*([\d.]+)")
READERS_SCORE_RE = re.compile(r"Readers['\u2019]\s*Rating\s*([\d.]+)")
# 순서대로 "N Positive M Mixed K Negative"가 붙어서 나오는 걸 한 번에 매치
# (평점 숫자 자체에 있는 소수점과 혼동 안 되도록 트리오 전체를 하나의 패턴으로 잡음)
COUNTS_TRIO_RE = re.compile(r"(\d+)\s+Positive\s+(\d+)\s+Mixed\s+(\d+)\s+Negative")
# 리뷰 한 건마다 반복되는 "By: <critic> | Date: <m/d/yyyy>" 패턴으로 리뷰 개수를 셈
REVIEW_DATE_RE = re.compile(r"Date:\**\s*(\d{1,2}/\d{1,2}/\d{4})")
REVIEW_META_RE = re.compile(r"From:\s*([^|]+?)\s*\|\s*By:\s*([^|]+?)\s*\|\s*Date:\s*(\d{1,2}/\d{1,2}/\d{4})")
SCORE_NEAR_RE = re.compile(r"\b(\d{1,2})\b")


def title_slug(title):
    s = re.sub(r"[^\w\s-]", "", title).strip()
    s = re.sub(r"\s+", "-", s)
    return s


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_ratings(title, showid, session):
    url = f"{BASE}/reviews/{title_slug(title)}"
    resp = session.get(url, params={"id": showid}, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return {}, []
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    critics_m = CRITICS_SCORE_RE.search(text)
    readers_m = READERS_SCORE_RE.search(text)
    counts_m = COUNTS_TRIO_RE.search(text)
    n_reviews = len(REVIEW_DATE_RE.findall(text))

    ratings = {
        "critics_rating": critics_m.group(1) if critics_m else "",
        "readers_rating": readers_m.group(1) if readers_m else "",
        "critics_positive": counts_m.group(1) if counts_m else "",
        "critics_mixed": counts_m.group(2) if counts_m else "",
        "critics_negative": counts_m.group(3) if counts_m else "",
        "n_critic_reviews": n_reviews,
    }

    review_rows = []
    # "Critics' Reviews" 섹션 시작 지점부터만 리뷰 블록으로 파싱
    # (그 이전 Critics'/Readers' Rating 요약 숫자와 헷갈리지 않도록 - 실제로
    # 첫 리뷰 점수가 '7.19'의 '19'로 잘못 잡히던 버그가 있었음, 이번에 고침)
    reviews_start = text.find("Critics' Reviews")
    if reviews_start == -1:
        reviews_start = text.find("Critics\u2019 Reviews")
    reviews_text = text[reviews_start:] if reviews_start != -1 else text

    chunks = reviews_text.split("Read More")
    for chunk in chunks:
        meta_m = REVIEW_META_RE.search(chunk)
        if not meta_m:
            continue
        # From: 바로 앞 구간에서 가장 가까운 1~2자리 숫자를 점수로 취급
        before = chunk[: meta_m.start()]
        score_matches = list(SCORE_NEAR_RE.finditer(before))
        score = score_matches[-1].group(1) if score_matches else ""
        publication, critic, date = meta_m.group(1), meta_m.group(2), meta_m.group(3)
        snippet = chunk[meta_m.end():].strip()[:2000]
        review_rows.append({
            "score": score,
            "publication": publication.strip(),
            "critic": critic.strip(),
            "date": date,
            "snippet": snippet,
        })
    return ratings, review_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="broadway.csv 경로 (show 컬럼 사용)")
    ap.add_argument("--showids", default=None,
                     help="broadwayworld_full.csv 경로 - title별 showid를 여기서 가져옴 "
                          "(reviews 페이지도 showid가 필요해서)")
    ap.add_argument("--shows", nargs="+", default=None,
                     help="테스트용: 'Title:showid' 형태로 나열 (예: 'American Psycho:331080')")
    ap.add_argument("--existing", default=None)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--reviews-detail-dir", default=None,
                     help="쇼별 개별 리뷰 텍스트 CSV를 저장할 폴더 (performance_id=showid 기준으로 "
                          "파일 하나씩 생성). 지정 안 하면 <out-dir>/reviews_by_show 사용")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    session = make_session()

    if args.shows:
        pairs = []
        for s in args.shows:
            title, _, showid = s.rpartition(":")
            pairs.append((title, showid))
    elif args.raw and args.showids:
        raw = pd.read_csv(args.raw, sep=None, engine="python", encoding="utf-8-sig")
        raw.columns = [c.strip().lstrip("\ufeff") for c in raw.columns]
        titles = raw["show"].dropna().drop_duplicates().tolist()

        meta = pd.read_csv(args.showids, sep=None, engine="python", encoding="utf-8-sig")
        meta.columns = [c.strip().lstrip("\ufeff") for c in meta.columns]
        title_to_id = dict(zip(meta["title"], meta["showid"]))

        pairs = [(t, title_to_id.get(t)) for t in titles if title_to_id.get(t) and pd.notna(title_to_id.get(t))]
        print(f"showid가 있는 쇼 {len(pairs)}/{len(titles)}개만 처리 가능 (나머지는 먼저 "
              f"fetch_broadwayworld_full.py로 showid부터 확보해야 함)")
    else:
        ap.error("--shows 또는 (--raw와 --showids) 조합이 필요해요")

    existing_df = None
    if args.existing and os.path.isfile(args.existing):
        existing_df = pd.read_csv(args.existing, sep=None, engine="python", encoding="utf-8-sig")
        already_done = set(existing_df["title"])
        before = len(pairs)
        pairs = [(t, sid) for t, sid in pairs if t not in already_done]
        print(f"기존 {len(already_done)}개 쇼는 건너뜀 ({before} -> {len(pairs)}개 신규 처리 대상)")

    if args.limit:
        pairs = pairs[: args.limit]

    print(f"총 {len(pairs)}개 쇼 처리 예정")

    reviews_detail_dir = args.reviews_detail_dir or os.path.join(args.out_dir, "reviews_by_show")
    os.makedirs(reviews_detail_dir, exist_ok=True)

    rows = []
    n_errors = 0
    n_detail_files = 0
    for i, (title, showid) in enumerate(pairs, 1):
        try:
            ratings, review_rows = fetch_ratings(title, showid, session)
            row = {"title": title, "showid": showid, **ratings}
            rows.append(row)

            if review_rows:
                detail_df = pd.DataFrame(review_rows)
                detail_df.insert(0, "performance_id", showid)  # showid가 곧 이 프로덕션의 performance_id
                detail_df.insert(1, "title", title)
                # performance_id(=showid) 하나당 파일 하나 - 나중에 GitHub Release에 통째로 올림
                detail_path = os.path.join(reviews_detail_dir, f"{showid}.csv")
                detail_df.to_csv(detail_path, index=False, encoding="utf-8-sig")
                n_detail_files += 1

            print(f"[{i}/{len(pairs)}] '{title}' -> critics={ratings.get('critics_rating', '')}, "
                  f"readers={ratings.get('readers_rating', '')}, "
                  f"reviews={ratings.get('n_critic_reviews', 0)}건 (본문 {len(review_rows)}건 저장)")
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(pairs)}] '{title}' -> 오류 발생, 스킵: {e}")
        time.sleep(args.sleep)

    print(f"\n처리 중 오류 {n_errors}건, 리뷰 상세 파일 {n_detail_files}개 생성 ({reviews_detail_dir})")

    if not rows:
        print("수집된 데이터가 없어요.")
        return

    new_df = pd.DataFrame(rows)
    out_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(
        subset=["title"], keep="last"
    ) if existing_df is not None else new_df

    out_path = os.path.join(args.out_dir, "bww_reviews.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"저장 완료: 신규 {len(new_df)}개 + 기존 -> 총 {len(out_df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
