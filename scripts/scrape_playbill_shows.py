#!/usr/bin/env python3
"""
scrape_playbill_shows.py

broadway_shows_list.csv (show, theatre, opening_date, closing_date, run_number,
is_revival, genre) 에 있는 공연들을 기준으로 Playbill production 페이지를 찾아서
SYNOPSIS + Running Time을 긁어오는 스크립트.

Playbill URL은 show/theatre 이름과 opening_date의 연도로부터 추정한 슬러그로
만들어지는데, 실제 Playbill 슬러그 규칙에는 예외가 많아서(예: 앰퍼샌드 처리,
em-dash 처리, 연도 범위 표기 등) 자동 추정이 항상 맞지는 않는다.
그래서 이 스크립트는:
  1) data/playbill_url_overrides.csv 에 수동으로 적어둔 URL이 있으면 그걸 최우선으로 쓰고,
  2) 없으면 여러 후보 URL을 만들어서 fetch 후 페이지 제목이 쇼 이름과 맞는지 확인하고,
  3) 끝내 못 찾은 건 data/playbill_unresolved.csv 에 남겨서 사람이 나중에
     override 파일에 URL을 채워 넣을 수 있게 한다.

사용법:
    python scrape_playbill_shows.py \
        --shows data/broadway_shows_list.csv \
        --overrides data/playbill_url_overrides.csv \
        --output data/playbill_scraped.csv \
        --unresolved data/playbill_unresolved.csv

overrides CSV 형식 (선택 사항, 없으면 빈 파일로 취급):
    show,theatre,url
    Hadestown,Walter Kerr Theatre,https://playbill.com/production/hadestown-broadway-walter-kerr-theatre-2019
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

from lib_playbill import (
    build_candidate_urls,
    extract_h1,
    extract_running_time,
    extract_synopsis,
    fetch_html,
    title_matches,
)


def load_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def clean_shows_rows(rows: list[dict]) -> list[dict]:
    """빈 행이나 헤더가 중복으로 섞여 들어간 행을 걸러낸다."""
    cleaned = []
    for r in rows:
        show = (r.get("show") or "").strip()
        if not show:
            continue
        if show.lower() == "show":  # 중복 헤더 행
            continue
        cleaned.append(r)
    return cleaned


def build_override_map(rows: list[dict]) -> dict[tuple[str, str], str]:
    result = {}
    for r in rows:
        show = (r.get("show") or "").strip()
        theatre = (r.get("theatre") or "").strip()
        url = (r.get("url") or "").strip()
        if show and url:
            result[(show, theatre)] = url
    return result


def resolve_and_scrape_one(show: str, theatre: str, opening_date: str, closing_date: str,
                            override_url: str | None) -> dict:
    result = {
        "show": show,
        "theatre": theatre,
        "url": "",
        "matched_title": "",
        "synopsis": "",
        "running_time": "",
        "status": "",
        "note": "",
    }

    urls_to_try = [override_url] if override_url else build_candidate_urls(
        show, theatre, opening_date, closing_date
    )

    for url in urls_to_try:
        if not url:
            continue
        try:
            html = fetch_html(url)
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"fetch error on {url}: {exc}"
            continue
        if html is None:
            continue  # 404 등 -> 다음 후보 시도

        soup = BeautifulSoup(html, "html.parser")
        page_title = extract_h1(soup) or ""

        # override URL은 사람이 직접 확인한 것이므로 제목 매칭 없이 신뢰한다.
        if override_url or title_matches(page_title, show):
            result["url"] = url
            result["matched_title"] = page_title
            result["synopsis"] = extract_synopsis(soup) or ""
            result["running_time"] = extract_running_time(soup) or ""
            result["status"] = "ok" if result["synopsis"] else "found_no_synopsis"
            return result

    result["status"] = "unresolved"
    result["note"] = f"{len(urls_to_try)}개 후보 URL 모두 실패 (override 필요)"
    return result


def main():
    parser = argparse.ArgumentParser(description="Broadway shows list 기준 Playbill 스크래퍼")
    parser.add_argument("--shows", default="data/broadway_shows_list.csv", help="공연 목록 CSV")
    parser.add_argument(
        "--overrides",
        default="data/playbill_url_overrides.csv",
        help="수동으로 지정한 show->url 매핑 CSV (없어도 됨)",
    )
    parser.add_argument("--output", default="data/playbill_scraped.csv", help="스크래핑 결과 CSV")
    parser.add_argument(
        "--unresolved",
        default="data/playbill_unresolved.csv",
        help="URL을 못 찾은 공연들을 저장할 CSV (override 파일에 채워넣기 위한 용도)",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="요청 사이 대기 시간(초)")
    args = parser.parse_args()

    shows_rows = clean_shows_rows(load_csv_rows(Path(args.shows)))
    if not shows_rows:
        print(f"입력 파일에 유효한 공연이 없습니다: {args.shows}", file=sys.stderr)
        sys.exit(1)

    override_map = build_override_map(load_csv_rows(Path(args.overrides)))

    results = []
    unresolved = []
    total = len(shows_rows)

    for i, row in enumerate(shows_rows, start=1):
        show = (row.get("show") or "").strip()
        theatre = (row.get("theatre") or "").strip()
        opening_date = (row.get("opening_date") or "").strip()
        closing_date = (row.get("closing_date") or "").strip()

        override_url = override_map.get((show, theatre))
        print(f"[{i}/{total}] {show} ({theatre}){' [override]' if override_url else ''}")

        r = resolve_and_scrape_one(show, theatre, opening_date, closing_date, override_url)
        # 원본 메타데이터도 같이 붙여서 저장
        r = {
            "show": show,
            "theatre": theatre,
            "opening_date": opening_date,
            "closing_date": closing_date,
            "run_number": row.get("run_number", ""),
            "is_revival": row.get("is_revival", ""),
            "genre": row.get("genre", ""),
            **{k: v for k, v in r.items() if k not in ("show", "theatre")},
        }
        results.append(r)
        if r["status"] == "unresolved":
            unresolved.append(
                {
                    "show": show,
                    "theatre": theatre,
                    "opening_date": opening_date,
                    "closing_date": closing_date,
                    "url": "",  # 여기에 사람이 직접 URL을 채워서 overrides 파일로 옮기면 됨
                    "note": r["note"],
                }
            )

        if i < total:
            time.sleep(args.delay)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(results[0].keys()) if results else []
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    unresolved_path = Path(args.unresolved)
    unresolved_path.parent.mkdir(parents=True, exist_ok=True)
    with unresolved_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames_u = ["show", "theatre", "opening_date", "closing_date", "url", "note"]
        writer = csv.DictWriter(f, fieldnames=fieldnames_u)
        writer.writeheader()
        writer.writerows(unresolved)

    ok = sum(1 for r in results if r["status"] == "ok")
    found_no_synopsis = sum(1 for r in results if r["status"] == "found_no_synopsis")
    unresolved_count = len(unresolved)
    print(
        f"\n완료: 총 {total}건 "
        f"(시놉시스까지 추출 {ok} / 페이지는 찾았지만 시놉시스 없음 {found_no_synopsis} / "
        f"URL 못 찾음 {unresolved_count})"
    )
    print(f"결과: {output_path}")
    if unresolved_count:
        print(
            f"URL을 못 찾은 {unresolved_count}건은 {unresolved_path} 에 저장했습니다. "
            f"직접 Playbill에서 검색해서 URL을 채운 뒤 "
            f"{args.overrides} 로 옮겨서 다시 실행하면 그 공연들만 override로 바로 처리됩니다."
        )


if __name__ == "__main__":
    main()
