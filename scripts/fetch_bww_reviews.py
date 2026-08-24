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
    """예전엔 이 로컬 함수로 슬러그를 새로 추측했는데, BroadwayWorld의 실제 규칙과
    미묘하게 달라서(예: '!' 같은 문장부호를 그냥 삭제하는데, 실제 사이트는 하이픈으로
    바꿈 - 'Mark Twain Tonight!' -> 실제 URL은 'Mark-Twain-Tonight-'인데 이 함수는
    'Mark-Twain-Tonight'를 만들어서 404가 났었음) 조용히 404가 나면 예외 없이 빈
    결과({}, [])를 반환하고 그 행이 그대로 저장돼서, 다음 실행에서 '이미 처리함'으로
    스킵되어 영원히 평점이 안 채워지는 문제가 있었음 (실제로 49개 쇼에서 확인됨).
    이제 이 함수는 fetch_broadwayworld_full.py가 실제 사이트 인덱스에서 검증한
    slug가 없을 때(구버전 broadwayworld_full.csv 등)만 쓰는 폴백으로 남겨둠."""
    s = re.sub(r"[^\w\s-]", "", title).strip()
    s = re.sub(r"\s+", "-", s)
    return s


def make_session():
    session = requests.Session()
    # *** 2026-08-24 수정: 429가 이어질 때 재시도 예산을 늘림 ***
    # 실제로 리뷰 페이지에서 리다이렉트 실패가 이어지다가 이후 거의 모든 요청이
    # 429를 맞기 시작한 사례가 있었음(로그로 확인: Bring It On부터 Riverdance까지
    # 연속 429). backoff_factor=1.5/total=3으로는 한 번 트립된 레이트리밋에서
    # 회복이 안 됐음 - total과 backoff를 늘려서 서버가 풀어줄 시간을 더 줌.
    # urllib3 Retry는 기본으로 429 응답의 Retry-After 헤더를 존중하므로, 서버가
    # 값을 보내주면 그 시간만큼은 자동으로 기다림.
    retry = Retry(total=6, backoff_factor=3, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    # 리다이렉트 루프(실제 확인됨: 특정 slug로 요청하면 서버가 계속 다른
    # article= 파라미터를 붙이며 자기 자신으로 되돌아오는 응답을 반복함)에 빠졌을 때
    # 기본값 30홉까지 기다리지 않고 빨리 실패해서 폴백 slug 시도로 넘어가게 함
    session.max_redirects = 8
    return session


def fetch_ratings(title, showid, session, slug=None):
    """slug가 주어지면(broadwayworld_full.csv에서 이미 검증된 값) 그걸 우선 쓰고,
    없을 때만 title_slug()로 추측함.

    *** 2026-08-24 수정: 예외가 나도 폴백 slug를 반드시 시도하게 함 ***
    예전엔 상태코드가 200이 아닌 "정상적으로 응답은 왔지만 실패"인 경우에만
    폴백을 시도했음. 근데 실제로는 검증된 slug로 요청했을 때 TooManyRedirects
    (리다이렉트 루프 - BWW 서버가 article= 파라미터를 계속 덧붙이며 자기 자신으로
    되돌아가는 응답을 반복하는 걸로 보임) 같은 예외가 session.get() 단계에서
    바로 터져서 상태코드 체크까지 가지도 못하고 함수 전체가 죽는 사례가 있었음
    (실제 로그: 'A Beautiful Noise...', 'Sweeney Todd' 등 다수가 이걸로 통째로
    스킵됨). 그래서 이제 첫 번째 요청 자체를 try/except로 감싸서, 예외가 나도
    폴백 slug 시도로 넘어가게 하고, 폴백까지 실패하면 그때 조용히 빈 결과를
    반환함(호출부에서 예외로 죽지 않고, 이 쇼는 n_critic_reviews가 비어서
    다음 실행에서 자동 재시도됨 - 기존 재시도 로직과 동일).

    반환값에 rate_limited를 추가함: urllib3 Retry가 429로 재시도를 전부
    소진하면 예외 메시지에 '429'가 남는데, 이걸로 "이 쇼가 실패한 게 아니라
    BWW가 지금 이 IP 자체를 막고 있다"는 신호를 호출부(main)에 전달해서,
    남은 쇼들을 같은 벽에 계속 부딪히게 두지 않고 잠깐 쉬었다 가게 함."""
    def try_get(url_slug):
        try:
            r = session.get(f"{BASE}/reviews/{url_slug}", params={"id": showid},
                             headers=HEADERS, timeout=30)
        except requests.exceptions.RequestException as e:
            is_rate_limited = "429" in str(e)
            return None, is_rate_limited
        return (r if r.status_code == 200 else None), (r.status_code == 429)

    resp = None
    rate_limited = False
    if slug:
        resp, rl = try_get(slug)
        rate_limited = rate_limited or rl
    if resp is None:
        fallback_slug = title_slug(title)
        if fallback_slug != slug:
            resp, rl = try_get(fallback_slug)
            rate_limited = rate_limited or rl

    if resp is None:
        return {}, [], rate_limited
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
    return ratings, review_rows, False


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
            pairs.append((title, showid, None))  # 테스트 경로는 slug 없이 title_slug() 폴백만 씀
    elif args.raw and args.showids:
        raw = pd.read_csv(args.raw, sep=None, engine="python", encoding="utf-8-sig")
        raw.columns = [c.strip().lstrip("\ufeff") for c in raw.columns]
        titles = raw["show"].dropna().drop_duplicates().tolist()

        meta = pd.read_csv(args.showids, sep=None, engine="python", encoding="utf-8-sig")
        meta.columns = [c.strip().lstrip("\ufeff") for c in meta.columns]
        title_to_id = dict(zip(meta["title"], meta["showid"]))
        # broadwayworld_full.csv에 이미 검증된 slug가 있으면 그걸 씀 (로컬에서 다시
        # 추측하다가 실제 사이트 규칙과 안 맞아서 404 나던 문제 방지 - 아래 설명 참고)
        title_to_slug = dict(zip(meta["title"], meta.get("slug", pd.Series(dtype=object))))

        pairs = [(t, title_to_id.get(t), title_to_slug.get(t))
                 for t in titles if title_to_id.get(t) and pd.notna(title_to_id.get(t))]
        print(f"showid가 있는 쇼 {len(pairs)}/{len(titles)}개만 처리 가능 (나머지는 먼저 "
              f"fetch_broadwayworld_full.py로 showid부터 확보해야 함)")
    else:
        ap.error("--shows 또는 (--raw와 --showids) 조합이 필요해요")

    existing_df = None
    if args.existing and os.path.isfile(args.existing):
        existing_df = pd.read_csv(args.existing, sep=None, engine="python", encoding="utf-8-sig")
        # *** 중요: '이미 시도한 title'이 아니라 '진짜로 값을 얻은 title'만 완료로 침 ***
        # 예전 버전은 title이 존재하기만 하면 무조건 스킵했는데, 그러면 슬러그가
        # 틀려서 404로 빈 값만 저장된 쇼(실제로 49개 확인됨)가 영원히 재시도 안 되고
        # 방치됨. n_critic_reviews가 실제 숫자(0 포함)로 채워진 것만 "완료"로 인정하고,
        # NaN인 title은 다시 시도 대상에 넣음 - 슬러그 폴백 로직과 합쳐지면 이번
        # 실행에서 자동으로 복구됨.
        if "n_critic_reviews" in existing_df.columns:
            done_mask = existing_df["n_critic_reviews"].notna()
        else:
            done_mask = pd.Series(True, index=existing_df.index)  # 구버전 파일 호환
        already_done = set(existing_df.loc[done_mask, "title"])
        n_retry = existing_df.loc[~done_mask, "title"].nunique() if "n_critic_reviews" in existing_df.columns else 0
        before = len(pairs)
        pairs = [(t, sid, slug) for t, sid, slug in pairs if t not in already_done]
        print(f"기존 {len(already_done)}개 쇼는 완료로 건너뜀"
              + (f" (이전에 실패해서 재시도 대상인 쇼 {n_retry}개는 포함됨)" if n_retry else "")
              + f" ({before} -> {len(pairs)}개 처리 대상)")

    if args.limit:
        pairs = pairs[: args.limit]

    print(f"총 {len(pairs)}개 쇼 처리 예정")

    reviews_detail_dir = args.reviews_detail_dir or os.path.join(args.out_dir, "reviews_by_show")
    os.makedirs(reviews_detail_dir, exist_ok=True)

    rows = []
    n_errors = 0
    n_detail_files = 0
    n_rate_limited = 0
    for i, (title, showid, slug) in enumerate(pairs, 1):
        try:
            ratings, review_rows, rate_limited = fetch_ratings(title, showid, session, slug=slug)
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
                  f"reviews={ratings.get('n_critic_reviews', 0)}건 (본문 {len(review_rows)}건 저장)"
                  + (" [429 감지 - 냉각 대기 후 계속]" if rate_limited else ""))

            if rate_limited:
                # *** 2026-08-24 추가: 429 폭주 냉각 로직 ***
                # 실제로 리뷰 페이지에서 한 번 429가 뜨기 시작하면 이후 거의 모든
                # 요청이 연달아 429를 맞는 패턴이 확인됨(로그: Bring It On부터
                # Riverdance까지 연속 실패). 그때마다 재시도 예산(Retry total=6)을
                # 매번 다 태우면서도 결과가 안 나오면 시간만 낭비하니까, 429가
                # 감지되면 남은 쇼로 넘어가기 전에 한 번 길게(60초) 쉬어서 서버
                # 레이트리밋이 풀릴 시간을 줌. 그래도 계속 429가 나면 매번 60초씩
                # 쉬면서 진행하되(무한정 기다리진 않음), 총 429 감지 횟수를 마지막
                # 요약에 남겨서 다음 실행 때 sleep을 더 늘려야 하는지 판단하게 함.
                n_rate_limited += 1
                print(f"    [429 냉각 대기 60초...]")
                time.sleep(60)
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(pairs)}] '{title}' -> 오류 발생, 스킵: {e}")
        time.sleep(args.sleep)

    print(f"\n처리 중 오류 {n_errors}건, 429 감지 {n_rate_limited}건, "
          f"리뷰 상세 파일 {n_detail_files}개 생성 ({reviews_detail_dir})")

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
