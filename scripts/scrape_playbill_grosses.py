"""
scrape_playbill_grosses.py
============================
playbill.com/grosses에서 주간 박스오피스 데이터를 수집해서 data/broadway.csv에
이어붙이는 스크래퍼.

URL 패턴 확인 완료: https://playbill.com/grosses?week=YYYY-MM-DD
(예: ?week=2026-07-26 -> Week 9, Week's Total $34,864,840.00 로 정확히 그 주
 데이터가 서버에서 렌더링되어 나오는 걸 실제로 확인함. JSON API 우회 필요 없음.)

각 쇼 이름은 playbill.com/production/gross?production=<UUID> 링크를 갖고 있어서,
이 UUID를 안정적인 perf_id로 바로 쓸 수 있음 (재공연도 UUID가 달라서 구분 가능).
단, broadway.csv에는 production_id 컬럼이 없어서 최종 저장 시에는 제외하고
show/week_ending 조합으로 중복을 관리함.

*** 주의: 실제 <table> 태그 구조(클래스명 등)는 raw HTML을 직접 못 받아온 상태에서
텍스트 레이아웃만 보고 추정한 파서예요. 처음 몇 주치 결과를 꼭 broadway.csv 형식과
비교해서 컬럼이 밀리지 않았는지 확인하세요. ***

Usage:
  # 처음부터 지정 기간 수집해서 새 파일로 저장
  python scrape_playbill_grosses.py --start 2021-08-08 --end 2026-08-09 \
      --out playbill_new.csv

  # 기존 broadway.csv에 이어서 자동 수집 + 스키마 맞춰 병합 (주간 자동화용)
  python scrape_playbill_grosses.py --append-to data/broadway.csv --sleep 1.0
"""
import argparse
import os
import re
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
import pandas as pd

URL_TEMPLATE = "https://playbill.com/grosses?week={week}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html",
}

PRODUCTION_LINK_RE = re.compile(r"/production/gross\?production=([0-9a-fA-F-]+)")
MONEY_RE = re.compile(r"-?\$[\d,]+\.\d{2}")
NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?%?")

# 페이지 상단 "Week 9" / "Week's Total $34,864,840.00" 헤더 텍스트에서 추출
WEEK_NUMBER_RE = re.compile(r"Week\s+(\d+)\b", re.IGNORECASE)
WEEK_TOTAL_RE = re.compile(r"Week'?s?\s+Total\s*\$?([\d,]+\.\d{2})", re.IGNORECASE)

# broadway.csv의 컬럼 순서 (기존 병합 스크립트와 동일한 스키마)
BROADWAY_CSV_COLUMNS = [
    "week_ending", "week_number", "weekly_gross_overall", "show", "theatre",
    "weekly_gross", "potential_gross", "avg_ticket_price", "top_ticket_price",
    "seats_sold", "seats_in_theatre", "pct_capacity", "performances", "previews",
]


def week_range(start_date, end_date):
    """start_date부터 end_date까지 7일 간격 주차(일요일 시작 가정)를 생성."""
    cur = start_date
    while cur <= end_date:
        yield cur
        cur += timedelta(days=7)


def _split_stacked(text):
    """'7,548 1,026' 처럼 공백으로 붙은 두 숫자를 분리.
    쇼 표는 This Week Gross/Potential Gross, Avg Ticket/Top Ticket,
    Seats Sold/Seats in Theatre, Perfs/Previews 가 한 셀에 두 줄로 쌓여있음."""
    parts = text.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return text, None


def parse_week_header(html):
    """페이지 상단의 'Week 9' / "Week's Total $34,864,840.00" 텍스트에서
    week_number, weekly_gross_overall을 추출. 원본 broadway.csv 스키마의
    두 컬럼을 채우기 위함. 못 찾으면 (None, None)."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    wn_match = WEEK_NUMBER_RE.search(text)
    wt_match = WEEK_TOTAL_RE.search(text)
    week_number = wn_match.group(1) if wn_match else None
    weekly_gross_overall = wt_match.group(1) if wt_match else None
    return week_number, weekly_gross_overall


def parse_html_table(html, week):
    """grosses?week=... 페이지의 표를 파싱.
    각 행 구조(확인됨): [Show+Theatre(링크 포함)] [This Week Gross\nPotential Gross]
    [Diff $] [Avg Ticket\nTop Ticket] [Seats Sold\nSeats in Theatre]
    [Perfs\nPreviews] [% Cap] [Diff % cap]"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    week_number, weekly_gross_overall = parse_week_header(html)

    table = soup.find("table")
    if not table:
        print(f"  [{week}] 테이블을 못 찾음 - 페이지 구조가 바뀌었을 수 있음")
        return rows

    body_rows = table.find_all("tr")
    for tr in body_rows:
        cells = tr.find_all("td")
        if not cells or len(cells) < 7:
            continue  # 헤더 행 등 스킵

        first_cell = cells[0]
        link = first_cell.find("a", href=PRODUCTION_LINK_RE)
        production_id = None
        show = None
        if link:
            m = PRODUCTION_LINK_RE.search(link.get("href", ""))
            production_id = m.group(1) if m else None
            show = link.get_text(strip=True)
        cell_text_lines = list(first_cell.stripped_strings)
        theatre = cell_text_lines[-1] if len(cell_text_lines) > 1 else None
        if show is None and cell_text_lines:
            show = cell_text_lines[0]

        gross_text = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        this_week_gross, potential_gross = _split_stacked(gross_text)

        diff_dollar = cells[2].get_text(strip=True) if len(cells) > 2 else None

        ticket_text = cells[3].get_text(" ", strip=True) if len(cells) > 3 else ""
        avg_ticket, top_ticket = _split_stacked(ticket_text)

        seats_text = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
        seats_sold, seats_in_theatre = _split_stacked(seats_text)

        perf_text = cells[5].get_text(" ", strip=True) if len(cells) > 5 else ""
        performances, previews = _split_stacked(perf_text)

        pct_cap = cells[6].get_text(strip=True) if len(cells) > 6 else None
        diff_pct_cap = cells[7].get_text(strip=True) if len(cells) > 7 else None

        if not show:
            continue

        rows.append({
            "week_ending": week,
            "week_number": week_number,
            "weekly_gross_overall": weekly_gross_overall,
            "production_id": production_id,
            "show": show,
            "theatre": theatre,
            "this_week_gross": this_week_gross,
            "potential_gross": potential_gross,
            "diff_dollar": diff_dollar,
            "avg_ticket_price": avg_ticket,
            "top_ticket_price": top_ticket,
            "seats_sold": seats_sold,
            "seats_in_theatre": seats_in_theatre,
            "performances": performances,
            "previews": previews,
            "pct_capacity": pct_cap,
            "diff_pct_cap": diff_pct_cap,
        })
    return rows


def fetch_week(week, session, retries=3):
    week_str = week.strftime("%Y-%m-%d")
    url = URL_TEMPLATE.format(week=week_str)

    for attempt in range(retries):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return parse_html_table(resp.text, week_str)
        except Exception as e:
            print(f"  [retry {attempt+1}] {week_str}: {e}")
            time.sleep(2)
    return []


def to_broadway_schema(df_scraped):
    """스크래퍼 원본 컬럼(this_week_gross, diff_dollar, production_id, diff_pct_cap 등)을
    broadway.csv 스키마(weekly_gross, week_number, weekly_gross_overall 등)로 맞춤.
    기존 gap-fill 병합 스크립트와 동일한 매핑 로직."""
    df = df_scraped.rename(columns={"this_week_gross": "weekly_gross"})
    for col in ["diff_dollar", "diff_pct_cap", "production_id"]:
        if col in df.columns:
            df = df.drop(columns=[col])
    for col in BROADWAY_CSV_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[BROADWAY_CSV_COLUMNS]


def determine_start(existing_path, fallback_start):
    """기존 CSV가 있으면 그 파일의 최대 week_ending 다음 주부터 시작.
    (fetch_perfoby_weekly.py의 determine_start_date()와 동일한 설계)"""
    if not existing_path or not os.path.isfile(existing_path):
        return fallback_start
    df = pd.read_csv(existing_path, usecols=["week_ending"])
    if df.empty:
        return fallback_start
    max_week = pd.to_datetime(df["week_ending"]).max()
    return max_week + timedelta(days=7)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (--append-to 사용 시 생략 가능)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (생략하면 오늘 기준 최신 완료 주)")
    ap.add_argument("--out", default="playbill_new.csv",
                     help="--append-to 없이 쓸 때의 출력 파일 (스크래퍼 원본 스키마 그대로 저장)")
    ap.add_argument("--append-to", default="data/broadway.csv",
                     help="이어붙일 기존 CSV 경로 (기본값 data/broadway.csv). 지정하면 그 파일의"
                          " 마지막 주 다음부터 자동으로 이어서 수집하고, broadway.csv 스키마로"
                          " 맞춰서 합친 뒤 같은 경로에 다시 씀.")
    ap.add_argument("--no-append", action="store_true",
                     help="이 플래그를 주면 --append-to를 무시하고 --out에 원본 스키마로 새로 저장")
    ap.add_argument("--sleep", type=float, default=1.0, help="요청 사이 대기 시간(초) - 서버 부담 줄이기용")
    args = ap.parse_args()

    append_target = None if args.no_append else args.append_to

    if append_target:
        fallback = datetime.strptime(args.start, "%Y-%m-%d") if args.start else datetime(2021, 8, 8)
        start = determine_start(append_target, fallback)
        out_path = append_target
    else:
        if not args.start:
            ap.error("--start가 필요해요 (또는 --append-to 사용)")
        start = datetime.strptime(args.start, "%Y-%m-%d")
        out_path = args.out

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.today() - timedelta(days=1)

    if start > end:
        print(f"수집할 신규 주 없음 (다음 시작일 {start.date()} > 종료일 {end.date()})")
        return

    session = requests.Session()
    all_rows = []
    weeks = list(week_range(start, end))
    print(f"{len(weeks)}개 주차 수집 시작 ({start.date()} ~ {end.date()})")

    for i, week in enumerate(weeks, 1):
        rows = fetch_week(week, session)
        all_rows.extend(rows)
        print(f"[{i}/{len(weeks)}] {week.strftime('%Y-%m-%d')}: {len(rows)}건")
        time.sleep(args.sleep)

    if not all_rows:
        print("\n수집된 신규 데이터가 없어요. URL 패턴/파싱 로직을 다시 확인해주세요.")
        return

    df_new = pd.DataFrame(all_rows)

    if append_target and os.path.isfile(append_target):
        df_new = to_broadway_schema(df_new)
        df_existing = pd.read_csv(append_target, sep=None, engine="python", encoding="utf-8-sig")
        df_existing.columns = [c.strip().lstrip("\ufeff") for c in df_existing.columns]
        df = pd.concat([df_existing, df_new], ignore_index=True)
        df["week_ending"] = pd.to_datetime(df["week_ending"])
        df = df.sort_values(["show", "week_ending"]).drop_duplicates(
            subset=["show", "week_ending"], keep="last"
        )
    elif append_target:
        # 첫 실행이라 broadway.csv가 아직 없는 경우: 스키마만 맞춰서 새로 저장
        df = to_broadway_schema(df_new)
        df["week_ending"] = pd.to_datetime(df["week_ending"])
        df = df.sort_values(["show", "week_ending"])
    else:
        df = df_new  # --no-append: 원본 스키마 그대로

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n완료: 신규 {len(df_new)}행 수집, 총 {len(df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
