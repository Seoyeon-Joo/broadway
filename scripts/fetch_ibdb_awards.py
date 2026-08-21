"""
fetch_ibdb_awards.py
=============================
IBDB(Internet Broadway Database, The Broadway League 공식 운영)에서
쇼별 수상 이력(Tony Award, Pulitzer Prize, Theatre World, Drama Desk Award)을 수집.

흐름:
  1. https://www.ibdb.com/Search/QuickSearchInfo?TextBoxQuery=<쇼이름>&Category=all
     -> 검색 결과 HTML에서 /broadway-show/<slug>-<id> 링크를 찾음
     (실제로 브라우저에서 확인함: "Wicked" 검색 -> www.ibdb.com/broadway-show/wicked-11169)
  2. 그 show 페이지에서 "Original Broadway Production" 등 /broadway-production/<slug>-<id>
     링크를 찾아 따라감 (Awards 정보는 production 페이지에 있음, Wicked로 확인됨:
     https://www.ibdb.com/broadway-production/wicked-13485 의 "Awards" 섹션)
  3. production 페이지의 Awards 섹션을 파싱해서 award_name / category / year / result
     (Winner/Nominee) 목록을 뽑음

*** 주의: 1~2단계 URL 패턴은 실제 브라우저 캡처로 확인했지만, show 페이지 안에서
"Original Broadway Production" 링크를 정확히 어떤 텍스트/구조로 찾는지는 Wicked
사례로 추정한 것. 처음 --limit 5~10으로 테스트해서 award 컬럼이 실제로 채워지는지
확인 필요. ***

Usage:
  python fetch_ibdb_awards.py --raw data/broadway.csv \
      --existing data/ibdb_awards.csv --out-dir data --sleep 1.0
  python fetch_ibdb_awards.py --shows "Wicked" "Hamilton" --out-dir . --limit 2  # 테스트용
"""
import argparse
import os
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.ibdb.com"
SEARCH_URL = BASE + "/Search/QuickSearchInfo"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

SHOW_LINK_RE = re.compile(r"/broadway-show/([a-z0-9-]+)")
PRODUCTION_LINK_RE = re.compile(r"/broadway-production/([a-z0-9-]+)")
AWARD_HEADER_RE = re.compile(r"^(Tony Award®?|Pulitzer Prize|Theatre World|Drama Desk Award)$")
AWARD_RESULT_RE = re.compile(r"(\d{4})\s*(Winner|Nominee)", re.IGNORECASE)


def find_show_link(title, session):
    resp = session.get(SEARCH_URL, params={"TextBoxQuery": title, "Category": "all"},
                        headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    # "Shows" 섹션 안에서 첫 번째 /broadway-show/ 링크를 취함
    for a in soup.find_all("a", href=SHOW_LINK_RE):
        return a["href"] if a["href"].startswith("http") else BASE + a["href"]
    return None


def find_production_link(show_url, session):
    resp = session.get(show_url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=PRODUCTION_LINK_RE):
        return a["href"] if a["href"].startswith("http") else BASE + a["href"]
    return None


def parse_awards(production_url, session):
    """production 페이지의 Awards 섹션 파싱.
    구조(Wicked로 확인됨): 'Tony Award®' 같은 시상식명 헤더 다음에
    '#### Best Musical \n 2004 Nominee' 형태로 부문/연도/결과가 반복됨."""
    resp = session.get(production_url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    awards_section = None
    for heading in soup.find_all(["h2", "h3", "a"]):
        if heading.get_text(strip=True).lower() == "awards":
            awards_section = heading.find_parent(["section", "div"])
            break
    if awards_section is None:
        return []

    results = []
    current_award_name = None
    for el in awards_section.find_all(["h3", "h4", "p", "div"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        m_name = AWARD_HEADER_RE.match(text)
        if m_name:
            current_award_name = m_name.group(1).replace("®", "").strip()
            continue
        m_result = AWARD_RESULT_RE.search(text)
        if m_result and current_award_name:
            year, result = m_result.group(1), m_result.group(2).title()
            category = text[: m_result.start()].strip(" -\u2013")
            results.append({
                "award_name": current_award_name,
                "category": category,
                "year": year,
                "result": result,
            })
    return results


def summarize_awards(award_rows):
    if not award_rows:
        return {
            "tony_nominations": 0, "tony_wins": 0,
            "has_pulitzer": 0, "has_drama_desk_win": 0,
            "awards_detail": "",
        }
    tony = [r for r in award_rows if r["award_name"] == "Tony Award"]
    pulitzer = [r for r in award_rows if r["award_name"] == "Pulitzer Prize"]
    drama_desk_wins = [r for r in award_rows if r["award_name"] == "Drama Desk Award" and r["result"] == "Winner"]
    detail = "; ".join(f"{r['award_name']} {r['year']} {r['category']} ({r['result']})" for r in award_rows)
    return {
        "tony_nominations": len(tony),
        "tony_wins": sum(1 for r in tony if r["result"] == "Winner"),
        "has_pulitzer": int(len(pulitzer) > 0),
        "has_drama_desk_win": int(len(drama_desk_wins) > 0),
        "awards_detail": detail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="broadway.csv 경로 - 'show' 컬럼에서 고유 쇼 이름을 뽑음")
    ap.add_argument("--shows", nargs="+", default=None, help="테스트용: 쇼 이름을 직접 나열")
    ap.add_argument("--existing", default=None,
                     help="기존 ibdb_awards.csv 경로. 있으면 그 안에 이미 있는 title은 건너뜀")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    session = requests.Session()

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
    for i, title in enumerate(titles, 1):
        show_url = find_show_link(title, session)
        time.sleep(args.sleep)
        if not show_url:
            print(f"[{i}/{len(titles)}] '{title}' -> IBDB에서 못 찾음, 스킵")
            rows.append({"title": title, **summarize_awards([])})
            continue

        production_url = find_production_link(show_url, session)
        time.sleep(args.sleep)
        if not production_url:
            print(f"[{i}/{len(titles)}] '{title}' -> production 페이지 링크 못 찾음, 스킵")
            rows.append({"title": title, **summarize_awards([])})
            continue

        award_rows = parse_awards(production_url, session)
        time.sleep(args.sleep)
        summary = summarize_awards(award_rows)
        rows.append({"title": title, **summary})
        print(f"[{i}/{len(titles)}] '{title}' -> 토니 후보 {summary['tony_nominations']}회, "
              f"수상 {summary['tony_wins']}회, 퓰리처={'Y' if summary['has_pulitzer'] else 'N'}, "
              f"드라마데스크 수상={'Y' if summary['has_drama_desk_win'] else 'N'}")

    if not rows:
        print("\n수집된 데이터가 없어요.")
        return

    new_df = pd.DataFrame(rows)
    if existing_df is not None:
        out_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(subset=["title"], keep="last")
    else:
        out_df = new_df

    out_path = os.path.join(args.out_dir, "ibdb_awards.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n저장 완료: 신규 {len(new_df)}개 + 기존 -> 총 {len(out_df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
