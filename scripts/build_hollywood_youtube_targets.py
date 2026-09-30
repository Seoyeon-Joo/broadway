"""
build_hollywood_youtube_targets.py
===================================
Box Office Mojo 주간 데이터(data/hollywood.xlsx, weekly_data 시트)에서 영화 단위
YouTube 검색 대상 리스트(data/hollywood_youtube_targets.csv)를 만든다.
Broadway의 build_broadway_youtube_targets.py에 해당하는 단계.

영화 1편 = 1행 (movie_id 기준, 2,854편)

만드는 컬럼
-----------
movie_id          : 원본 movie_id 그대로 (Broadway의 run_id 역할)
movie_title       : 원본 Release 열 그대로
search_title      : YouTube 검색용으로 정리한 제목
                    - "Elf2020 Re-release"            -> "Elf"
                    - "Shrek25th Anniversary"          -> "Shrek"
                    - "Terrifier 22023 Re-release"     -> "Terrifier 2"
                    - "PonyoStudio Ghibli Fest 2024"   -> "Ponyo"
is_rerelease      : 재개봉/기념 재상영 표기가 있었으면 True
distributor       : 첫 주 기준 배급사
first_chart_week  : 주간 차트에 처음 등장한 주의 시작일
release_date      : 추정 개봉일 = first_chart_week - 7 * (첫 등장 시 Weeks - 1)
                    (2021-01-01 이전 개봉작이 첫 주에 이미 n주차로 잡혀 있는 경우 보정)
release_year      : release_date의 연도
last_week_date    : 차트에 마지막으로 등장한 주의 종료일 (Broadway의 closing_date 역할)
n_chart_weeks     : 차트 등장 주 수
total_gross       : 마지막 주 기준 누적 흥행 (Total Gross 최대값)
title_ambiguous   : 제목이 2단어 이하라 동명 영상이 섞일 위험이 큰 경우 True
                    -> 수집 스크립트가 검색어 뒤에 개봉 연도를 붙임.
                    CSV를 열어서 직접 True/False를 고쳐도 됨(수동 보정 우선).

사용 예시
--------
    python scripts/build_hollywood_youtube_targets.py \\
        --xlsx data/hollywood.xlsx \\
        --out data/hollywood_youtube_targets.csv
"""
import argparse
import re

import pandas as pd

# 재개봉/기념 상영 꼬리표. 제목 뒤에 공백 없이 붙어 있는 경우가 많음
# (Box Office Mojo 표기: "Elf2020 Re-release", "Shrek25th Anniversary").
RERELEASE_PATTERNS = [
    # "Toy Story2025 Re-release", "The Piano2026 4K Re-release",
    # "Jaws2025 Re-release (50th Anniversary)"
    r"\s*\d{4}\s*(?:4K\s*)?Re-?release.*$",
    # "Ghost in the Shell2021 4K Remaster"
    r"\s*\d{4}\s*4K\s*Remaster.*$",
    # "Shrek25th Anniversary", "Hocus Pocus30th Anniversary re-release",
    # "Scott Pilgrim vs. the World10th Anniversary (2021 Re-release)"
    r"\s*\d{1,2}(?:st|nd|rd|th)\s+Anniversary.*$",  # 1~2자리만: "Mob Psycho 10010th" -> "Mob Psycho 100"
    # "Star Wars: Episode I - The Phantom Menace2012 3D Release"
    r"\s*\d{4}\s*3D\s*Release.*$",
    # "...Scarlet BondRe-Release 2026 Anime Nights Program"
    r"\s*Re-?release\s+\d{4}.*$",
    # "Titanic25 Year Anniversary"
    r"\s*\d+\s+Year\s+Anniversary.*$",
    # "Contempt4k Restoration - ..."
    r"\s*4K\s*Restoration.*$",
    # "PonyoStudio Ghibli Fest 2024"
    r"\s*Studio Ghibli Fest.*$",
    # 괄호로만 붙은 경우 "(Re-release)"
    r"\s*\((?:\d{4}\s*)?Re-?release\)\s*$",
]
RERELEASE_RE = [re.compile(p, re.IGNORECASE) for p in RERELEASE_PATTERNS]


def clean_title(title):
    """(search_title, is_rerelease) 반환."""
    t = str(title).strip()
    is_rerelease = False
    for rx in RERELEASE_RE:
        new_t = rx.sub("", t).strip()
        if new_t and new_t != t:
            t = new_t
            is_rerelease = True
    if is_rerelease:
        t = re.sub(r"[\s\-–:/]+$", "", t)  # 꼬리표 떼고 남은 구분기호 정리
    return t, is_rerelease


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default="data/hollywood.xlsx")
    ap.add_argument("--sheet", default="weekly_data")
    ap.add_argument("--out", default="data/hollywood_youtube_targets.csv")
    ap.add_argument("--ambiguous-max-words", type=int, default=2,
                    help="이 단어 수 이하 제목은 title_ambiguous=True (검색어에 연도 추가)")
    args = ap.parse_args()

    df = pd.read_excel(args.xlsx, sheet_name=args.sheet)
    df["week_start_date"] = pd.to_datetime(df["week_start_date"])
    df["week_end_date"] = pd.to_datetime(df["week_end_date"])
    df = df.sort_values(["movie_id", "week_start_date"])

    first = df.groupby("movie_id").first()
    agg = df.groupby("movie_id").agg(
        movie_title=("Release", "first"),
        first_chart_week=("week_start_date", "min"),
        last_week_date=("week_end_date", "max"),
        n_chart_weeks=("week_start_date", "nunique"),
        total_gross=("Total Gross", "max"),
    )
    agg["distributor"] = first["Distributor"]
    weeks_at_first = pd.to_numeric(first["Weeks"], errors="coerce").fillna(1).clip(lower=1)
    agg["release_date"] = agg["first_chart_week"] - pd.to_timedelta((weeks_at_first - 1) * 7, unit="D")
    agg["release_year"] = agg["release_date"].dt.year

    cleaned = agg["movie_title"].apply(clean_title)
    agg["search_title"] = cleaned.str[0]
    agg["is_rerelease"] = cleaned.str[1]
    agg["title_ambiguous"] = agg["search_title"].str.split().str.len() <= args.ambiguous_max_words

    out = agg.reset_index()[[
        "movie_id", "movie_title", "search_title", "is_rerelease", "distributor",
        "first_chart_week", "release_date", "release_year", "last_week_date",
        "n_chart_weeks", "total_gross", "title_ambiguous",
    ]]
    for c in ["first_chart_week", "release_date", "last_week_date"]:
        out[c] = out[c].dt.strftime("%Y-%m-%d")
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    print(f"영화 {len(out)}편 -> {args.out}")
    print(f"  재개봉/기념 상영: {out['is_rerelease'].sum()}편")
    print(f"  title_ambiguous(검색어에 연도 추가): {out['title_ambiguous'].sum()}편")
    print(f"  2021년 이전 개봉 추정(차트 첫 주에 이미 n주차): "
          f"{(out['release_date'] < '2021-01-01').sum()}편")


if __name__ == "__main__":
    main()
