"""
fetch_show_history.py
=============================
BroadwayWorld의 쇼별 History 페이지에서 "어느 극장에서 언제부터 언제까지" 공연했는지를
프로덕션(run) 단위로 긁어서 performance_id를 부여.

  https://www.broadwayworld.com/shows/<Title-Slug>-<id>/history  (추정 URL)

*** 중요 - 아직 미검증 ***
/cast 페이지는 실제 스크린샷으로 URL 패턴을 확인했지만(/shows/<Title-Slug>-<id>/cast),
/history 페이지는 사이드바에 버튼만 보였을 뿐 실제로 열어서 구조를 확인하지 못했음.
이 스크립트는:
  1. 위 추정 URL을 먼저 시도
  2. 404 등으로 실패하면 조용히 빈 결과로 넘어감 (전체 파이프라인이 죽지 않음)
  3. 페이지를 열었다면, "극장 이름 + 날짜 범위(예: 'Mar 24, 2016 - Jun 05, 2016')" 패턴이
     반복되는 걸 찾아서 프로덕션 목록으로 파싱 (테이블/리스트 어느 구조든 텍스트 레벨에서
     동작하도록 느슨하게 작성함)

처음 몇 개 쇼로 --limit 3~5 정도 테스트해서 productions 컬럼이 실제로 채워지는지, 값이
말이 되는지 꼭 확인해주세요. 안 맞으면 실제 페이지 스크린샷을 보내주시면 바로 고칠 수 있어요.

performance_id 규칙: "<showid>-R<순번>" (예: 331080-R1, 331080-R2, ...) - 날짜 오름차순.
같은 쇼가 여러 도시/시즌에서 공연했으면 그 각각이 별도 run으로 잡힘.

Usage:
  python fetch_show_history.py --raw data/broadway.csv --showids data/broadwayworld_full.csv \
      --existing data/show_history.csv --out-dir data --sleep 1.0
  python fetch_show_history.py --shows "American Psycho:331080" --out-dir . --limit 1  # 테스트용
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

# "Mar 24, 2016 - Jun 05, 2016" 또는 "Mar 24, 2016 – Jun 05, 2016" 같은 날짜 범위
DATE_RANGE_RE = re.compile(
    r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\s*[-\u2013\u2014]\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
)
# 단일 날짜만 있는 경우(현재도 상연 중이라 종료일이 없는 run 등)
SINGLE_DATE_RE = re.compile(r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})")


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


def fetch_history(title, showid, session):
    """History는 별도 URL이 아니라 '/shows/<Title-Slug>-<id>.html' 메인 페이지의
    '#history' 앵커였음 (42nd Street 사례로 실제 확인: nav의 History 링크가
    '/shows/42nd-Street-4633.html#history'를 가리킴 - 프래그먼트는 서버 응답에
    영향 없으니 그냥 .html 페이지를 그대로 받으면 됨). 페이지 안에서 파싱 실패하면
    조용히 빈 리스트 반환 (에러 아님)."""
    if not showid:
        return []
    url = f"{BASE}/shows/{title_slug(title)}-{showid}.html"
    resp = session.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    productions = []
    # 테이블/리스트 각 행 단위로 순회하면서, 그 행 텍스트 안에 날짜범위가 있으면
    # 하나의 production으로 취급. 태그 종류를 특정하지 않고 li/tr/div/p 다 시도.
    candidate_blocks = soup.find_all(["tr", "li", "p", "div"])
    seen_texts = set()
    for block in candidate_blocks:
        text = block.get_text(" ", strip=True)
        if not text or text in seen_texts:
            continue
        m = DATE_RANGE_RE.search(text)
        if not m:
            continue
        seen_texts.add(text)
        start_date, end_date = m.group(1), m.group(2)
        # 극장/도시 이름 후보: 날짜 범위 앞부분 텍스트
        venue_guess = text[: m.start()].strip(" -:\u2013\u2014")[:80]
        productions.append({
            "start_date": start_date,
            "end_date": end_date,
            "venue": venue_guess,
        })
    return productions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None)
    ap.add_argument("--showids", default=None,
                     help="broadwayworld_full.csv 경로 - title별 showid를 가져옴")
    ap.add_argument("--shows", nargs="+", default=None,
                     help="테스트용: 'Title:showid' 형태로 나열")
    ap.add_argument("--existing", default=None)
    ap.add_argument("--out-dir", default=".")
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

    rows = []
    n_found, n_errors = 0, 0
    for i, (title, showid) in enumerate(pairs, 1):
        try:
            productions = fetch_history(title, showid, session)
            if not productions:
                print(f"[{i}/{len(pairs)}] '{title}' -> history 못 찾음/파싱 실패, 스킵")
            else:
                n_found += 1
                # 시작일 기준 정렬 후 performance_id 부여
                for run_idx, p in enumerate(
                    sorted(productions, key=lambda x: x["start_date"]), start=1
                ):
                    rows.append({
                        "title": title,
                        "showid": showid,
                        "performance_id": f"{showid}-R{run_idx}",
                        "venue": p["venue"],
                        "start_date": p["start_date"],
                        "end_date": p["end_date"],
                    })
                print(f"[{i}/{len(pairs)}] '{title}' -> {len(productions)}개 run 발견")
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(pairs)}] '{title}' -> 오류 발생, 스킵: {e}")
        time.sleep(args.sleep)

    print(f"\nhistory 발견: {n_found}/{len(pairs)}개 쇼, 오류 {n_errors}건")

    if not rows:
        print("수집된 프로덕션 데이터가 없어요. URL 패턴이나 페이지 구조가 다를 수 있어요 - "
              "실제 /history 페이지를 열어서 구조를 확인해주세요.")
        return

    new_df = pd.DataFrame(rows)
    out_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(
        subset=["title", "performance_id"], keep="last"
    ) if existing_df is not None else new_df

    out_path = os.path.join(args.out_dir, "show_history.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"저장 완료: 총 {len(out_df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
