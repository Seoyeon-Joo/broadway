"""
scrape_playbill_runtime.py
---------------------------
Broadway show 러닝타임(running time) 수집 스크립트.

*** 이 스크립트는 이 대화의 샌드박스 환경이 아니라, 본인 컴퓨터 또는
    GitHub Actions처럼 인터넷 제한이 없는 환경에서 실행해야 합니다. ***

입력: broadway_shows_list.csv (show, theatre, opening_date, closing_date, run_number, is_revival, genre)
출력: show_runtime_lookup.csv (show, theatre, opening_date, running_time_minutes, source, source_url, status)

동작 방식:
  1. Playbill 사이트 검색(searchpage/search)으로 쇼 이름을 검색해서 프로덕션(production) 페이지 URL을 찾음
  2. 후보 URL들 중 opening_date의 연도가 일치하는 것을 우선으로 고름 (Broadway 프로덕션 우선, touring/regional/London 등은 제외)
  3. 해당 production 페이지에서 "Running Time: X hours and Y minutes, ..." 텍스트를 정규식으로 추출
  4. Playbill에서 못 찾으면 Wikipedia REST API로 "{show} (musical)" 또는 "{show} (Broadway play)" 페이지의
     인포박스 텍스트에서 "Running time: N minutes"를 백업으로 시도
  5. 중간 결과를 매 행마다 CSV에 append + 이미 처리된 쇼는 스킵 (재실행 시 이어서 하기 가능)

필요 패키지: requests, beautifulsoup4
    pip install requests beautifulsoup4
"""

import csv
import os
import re
import sys
import time
import unicodedata
import urllib.parse

import requests
from bs4 import BeautifulSoup

INPUT_CSV = "data/broadway_shows_list.csv"
OUTPUT_CSV = "data/show_runtime_lookup.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (research script; contact: your_email@example.com)"
}

REQUEST_DELAY_SEC = 1.5  # 서버에 부담 안 주기 위한 요청 간 대기시간

RUNTIME_PATTERN = re.compile(
    r"running\s*time:\s*(?:(\d+)\s*hours?\s*(?:and)?\s*)?(\d+)\s*minutes?",
    re.IGNORECASE,
)


def parse_runtime_minutes(text):
    """텍스트에서 'Running Time: X hours and Y minutes' 패턴을 찾아 총 분(minute)으로 변환."""
    m = RUNTIME_PATTERN.search(text)
    if not m:
        return None
    hours = int(m.group(1)) if m.group(1) else 0
    minutes = int(m.group(2))
    return hours * 60 + minutes


def normalize_show_name(name):
    """검색 질의용으로 특수문자를 정리 (예: '& Juliet' -> '& Juliet' 그대로, 따옴표류 제거)."""
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("’", "'").replace("‘", "'")
    name = name.replace("“", '"').replace("”", '"')
    return name.strip()


def search_playbill_production_urls(show_name, session):
    """
    Playbill 사이트 검색에서 이 쇼의 production 페이지 후보 URL들을 반환.
    NOTE: playbill.com의 검색 페이지 마크업은 언제든 바뀔 수 있음 -
          이 함수가 결과를 못 찾으면 아래 SELECTOR 후보들을 실제 페이지 구조에
          맞춰 조정해야 함. (브라우저 개발자 도구로 직접 확인 권장)
    """
    query = urllib.parse.quote(show_name)
    search_url = f"https://playbill.com/searchpage/search?q={query}&productions=on&view=hideSearch"

    resp = session.get(search_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []
    for a in soup.select('a[href*="/production/"]'):
        href = a.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://playbill.com" + href
        candidates.append(href)

    # 중복 제거, 순서 유지
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)
    return unique_candidates


def pick_best_candidate(candidates, opening_year, theatre):
    """
    후보 URL 중 Broadway 프로덕션에 가장 맞는 것을 고름.
    - URL에 'touring', 'regional', 'shaftesbury' 같은 non-Broadway 키워드가 있으면 감점
    - opening_year가 slug에 포함되면 가점
    """
    if not candidates:
        return None

    def score(url):
        s = 0
        low = url.lower()
        if any(bad in low for bad in ["touring", "regional", "-london-", "shaftesbury", "west-end"]):
            s -= 5
        if opening_year and opening_year in low:
            s += 3
        theatre_slug_guess = re.sub(r"[^a-z0-9]+", "-", theatre.lower()).strip("-")
        if theatre_slug_guess and theatre_slug_guess in low:
            s += 2
        return s

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[0]


def fetch_runtime_from_playbill(show_name, theatre, opening_date, session):
    opening_year = ""
    if opening_date:
        m = re.search(r"(\d{4})", opening_date)
        if m:
            opening_year = m.group(1)

    candidates = search_playbill_production_urls(show_name, session)
    best_url = pick_best_candidate(candidates, opening_year, theatre)
    if not best_url:
        return None, None

    resp = session.get(best_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator=" ")
    minutes = parse_runtime_minutes(text)
    return minutes, best_url


def fetch_runtime_from_wikipedia(show_name, session):
    """
    백업 소스: Wikipedia REST API에서 '{show} (musical)' 또는 '{show} (Broadway play)' 페이지를 시도.
    """
    candidates_titles = [
        f"{show_name} (musical)",
        f"{show_name} (Broadway musical)",
        f"{show_name} (play)",
        show_name,
    ]
    for title in candidates_titles:
        api_url = (
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(title.replace(" ", "_"))
        )
        try:
            resp = session.get(api_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            page_title = data.get("title")
            if not page_title:
                continue
            # summary만으로는 running time이 안 나오므로 실제 문서를 fetch해서 infobox 텍스트 검색
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page")
            if not page_url:
                continue
            page_resp = session.get(page_url, headers=HEADERS, timeout=15)
            if page_resp.status_code != 200:
                continue
            page_soup = BeautifulSoup(page_resp.text, "html.parser")
            infobox = page_soup.find("table", class_=re.compile("infobox"))
            search_text = infobox.get_text(separator=" ") if infobox else page_soup.get_text(separator=" ")
            m = re.search(r"running\s*time[:\s]*([\d]+)\s*(?:minutes|min)", search_text, re.IGNORECASE)
            if m:
                return int(m.group(1)), page_url
        except Exception:
            continue
        time.sleep(0.5)
    return None, None


def load_already_done(output_path):
    done = set()
    if os.path.exists(output_path):
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row["show"], row["theatre"], row["opening_date"])
                done.add(key)
    return done


def main():
    if not os.path.exists(INPUT_CSV):
        print(f"입력 파일이 없습니다: {INPUT_CSV}")
        sys.exit(1)

    already_done = load_already_done(OUTPUT_CSV)
    write_header = not os.path.exists(OUTPUT_CSV)

    session = requests.Session()

    with open(INPUT_CSV, newline="", encoding="utf-8") as fin, \
         open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as fout:

        reader = csv.DictReader(fin)
        writer = csv.DictWriter(
            fout,
            fieldnames=[
                "show", "theatre", "opening_date",
                "running_time_minutes", "source", "source_url", "status",
            ],
        )
        if write_header:
            writer.writeheader()

        for row in reader:
            show = row["show"]
            theatre = row["theatre"]
            opening_date = row["opening_date"]
            key = (show, theatre, opening_date)

            if key in already_done:
                continue

            show_query = normalize_show_name(show)
            minutes, url, source, status = None, None, None, "not_found"

            try:
                minutes, url = fetch_runtime_from_playbill(show_query, theatre, opening_date, session)
                if minutes:
                    source, status = "playbill", "ok"
            except Exception as e:
                print(f"[playbill error] {show}: {e}")

            if minutes is None:
                try:
                    minutes, url = fetch_runtime_from_wikipedia(show_query, session)
                    if minutes:
                        source, status = "wikipedia", "ok"
                except Exception as e:
                    print(f"[wikipedia error] {show}: {e}")

            writer.writerow({
                "show": show,
                "theatre": theatre,
                "opening_date": opening_date,
                "running_time_minutes": minutes or "",
                "source": source or "",
                "source_url": url or "",
                "status": status,
            })
            fout.flush()

            print(f"{show:40s} -> {minutes} min  [{status}]")
            time.sleep(REQUEST_DELAY_SEC)

    print("완료. 결과:", OUTPUT_CSV)


if __name__ == "__main__":
    main()
