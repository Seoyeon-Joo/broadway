"""
Box Office Mojo 주간(Weekly) 박스오피스 데이터 크롤러
=====================================================
https://www.boxofficemojo.com/weekly/{YEAR}W{WEEK:02d}/ 형태의 페이지에서
Rank, LW, Release, Gross, %±LW, Theaters, Change, Average, Total Gross, Weeks, Distributor
표를 긁어 하나의 CSV로 합칩니다.

사용법
------
1. 아래 라이브러리 설치 (터미널에서):
   pip install requests pandas lxml beautifulsoup4

2. 이 파일을 원하는 위치에 저장 후 실행:
   python boxofficemojo_weekly_scraper.py

3. 실행이 끝나면 같은 폴더에 boxofficemojo_weekly_YYYY_YYYY.csv 가 생성됩니다.

주의사항
--------
- Box Office Mojo는 robots.txt로 자동 접근을 막아두고 있습니다. 이 스크립트는
  '공개된 페이지를 사람이 브라우저로 보는 속도와 비슷하게' 접근하도록 요청 사이에
  지연 시간을 두었지만, 그래도 해당 사이트의 이용약관(ToS)을 학술적 목적에 맞게
  확인해보시길 권장합니다.
- 요청이 너무 잦으면 IP가 일시 차단될 수 있으니 DELAY_RANGE를 줄이지 마세요.
- 2026년처럼 아직 다 지나지 않은 해는 존재하지 않는 주차에서 자동으로 실패하고
  넘어가도록 처리되어 있습니다 (에러 로그만 남고 스크립트는 계속 진행됩니다).
"""

import time
import random
import re
import sys
from datetime import datetime

import requests
import pandas as pd

# ------------------------- 설정 -------------------------
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]  # 원하는 연도 범위로 수정하세요
MAX_WEEKS = 53                  # ISO 주차 최대치 (53주까지 있는 해도 있음)
DELAY_RANGE = (2.5, 5.0)        # 요청 사이 랜덤 지연(초) - 너무 줄이지 마세요
OUTPUT_CSV = f"boxofficemojo_weekly_{min(YEARS)}_{max(YEARS)}.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

MONEY_COLS = ["Gross", "Average", "Total Gross"]
PCT_COLS = ["%± LW"]
INT_COLS = ["Rank", "LW", "Theaters", "Change", "Weeks"]


def clean_money(val):
    if pd.isna(val):
        return None
    s = str(val).replace("$", "").replace(",", "").strip()
    if s in ("-", "", "n/a", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_pct(val):
    if pd.isna(val):
        return None
    s = str(val).replace("%", "").replace(",", "").strip()
    if s in ("-", "", "n/a", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_int(val):
    if pd.isna(val):
        return None
    s = str(val).replace(",", "").strip()
    if s in ("-", "", "n/a", "N/A"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def fetch_week_table(year: int, week: int) -> pd.DataFrame | None:
    """한 주차 페이지를 가져와 표를 DataFrame으로 반환. 실패 시 None."""
    url = f"https://www.boxofficemojo.com/weekly/{year}W{week:02d}/"
    resp = requests.get(url, headers=HEADERS, timeout=20)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    # pandas.read_html이 페이지 내 모든 <table>을 파싱해줌
    tables = pd.read_html(resp.text)
    if not tables:
        raise RuntimeError("표를 찾지 못함")

    # 메인 주간 차트 표는 보통 'Rank'와 'Release' 컬럼을 가진 첫 번째 큰 표
    target = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if any("Rank" in c for c in cols) and any("Release" in c for c in cols):
            target = t
            break
    if target is None:
        target = tables[0]

    target = target.copy()
    target["Year"] = year
    target["Week"] = week
    target["SourceURL"] = url
    return target


def main():
    all_frames = []
    total_ok, total_fail = 0, 0

    for year in YEARS:
        for week in range(1, MAX_WEEKS + 1):
            # 미래 주차는 건너뛰기 (오늘 날짜 기준)
            try:
                df = fetch_week_table(year, week)
                if df is not None and len(df) > 0:
                    all_frames.append(df)
                    total_ok += 1
                    print(f"[OK]   {year}W{week:02d} - {len(df)}행")
                else:
                    print(f"[SKIP] {year}W{week:02d} - 빈 표")
            except Exception as e:
                total_fail += 1
                print(f"[FAIL] {year}W{week:02d} - {e}")

            time.sleep(random.uniform(*DELAY_RANGE))

    if not all_frames:
        print("수집된 데이터가 없습니다. 종료합니다.")
        sys.exit(1)

    result = pd.concat(all_frames, ignore_index=True)

    # 컬럼명이 페이지마다 살짝 다를 수 있어 존재하는 것만 정제
    for col in MONEY_COLS:
        if col in result.columns:
            result[col] = result[col].apply(clean_money)
    for col in PCT_COLS:
        if col in result.columns:
            result[col] = result[col].apply(clean_pct)
    for col in INT_COLS:
        if col in result.columns:
            result[col] = result[col].apply(clean_int)

    result.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n완료: 성공 {total_ok}주 / 실패 {total_fail}주")
    print(f"저장 위치: {OUTPUT_CSV} (총 {len(result)}행)")


if __name__ == "__main__":
    main()
