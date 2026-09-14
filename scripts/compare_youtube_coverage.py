#!/usr/bin/env python3
"""
compare_youtube_coverage.py

broadway_shows_list.csv 에 있는 공연들이 broadway_youtube_merged_cleaned_v2.csv
(유튜브 데이터)에도 있는지 확인한다.
- 유튜브 데이터 쪽에 더 많은 쇼가 있는 건 문제 없음 (유튜브 수집을 더 넓게 했을 수 있으니까).
- 반대로 shows_list에 있는데 유튜브 데이터에 없는 쇼는 "누락"으로 보고한다.
- 이름 표기가 살짝 다른 경우(아포스트로피, 대소문자, en/em dash 등)는
  정규화해서 같은 쇼로 인식하도록 처리했다.

사용법:
    python compare_youtube_coverage.py \
        --shows data/broadway_shows_list.csv \
        --youtube data/broadway_youtube_merged_cleaned_v2.csv \
        --output data/show_coverage_report.csv
"""

from __future__ import annotations

import argparse
import csv
import difflib
import re
from pathlib import Path


def normalize(name: str) -> str:
    s = name.strip().lower()
    s = s.replace("’", "'").replace("—", "-").replace("–", "-")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def load_shows_list(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    cleaned = []
    for r in rows:
        show = (r.get("show") or "").strip()
        if not show or show.lower() == "show":
            continue
        cleaned.append(r)
    return cleaned


def load_youtube_show_names(path: Path) -> set[str]:
    names = set()
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            show = (row.get("show") or "").strip()
            if show:
                names.add(show)
    return names


def main():
    parser = argparse.ArgumentParser(description="shows_list <-> youtube 데이터 커버리지 비교")
    parser.add_argument("--shows", default="data/broadway_shows_list.csv")
    parser.add_argument("--youtube", default="data/broadway_youtube_merged_cleaned_v2.csv")
    parser.add_argument("--output", default="data/show_coverage_report.csv")
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=0.6,
        help="정규화 후에도 매칭 안 될 때 유사도 기반으로 후보를 보여줄 임계값",
    )
    args = parser.parse_args()

    shows_rows = load_shows_list(Path(args.shows))
    yt_names = load_youtube_show_names(Path(args.youtube))

    yt_norm_map: dict[str, list[str]] = {}
    for name in yt_names:
        yt_norm_map.setdefault(normalize(name), []).append(name)

    report_rows = []
    missing = []

    for row in shows_rows:
        show = row["show"].strip()
        key = normalize(show)
        if key in yt_norm_map:
            report_rows.append(
                {
                    "show": show,
                    "in_youtube_data": "yes",
                    "matched_youtube_name": "; ".join(yt_norm_map[key]),
                    "close_match_suggestion": "",
                }
            )
            continue

        close = difflib.get_close_matches(key, yt_norm_map.keys(), n=3, cutoff=args.fuzzy_threshold)
        suggestion = "; ".join(sorted({n for c in close for n in yt_norm_map[c]}))
        report_rows.append(
            {
                "show": show,
                "in_youtube_data": "no",
                "matched_youtube_name": "",
                "close_match_suggestion": suggestion,
            }
        )
        missing.append(show)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["show", "in_youtube_data", "matched_youtube_name", "close_match_suggestion"]
        )
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"총 {len(shows_rows)}개 공연 중 유튜브 데이터에 없는 공연: {len(missing)}개")
    for m in missing:
        print(f"  - {m}")
    print(f"\n전체 리포트: {output_path}")
    print(f"(참고) shows_list 자체에 없는데 유튜브 데이터에만 있는 쇼는 {len(yt_names) - len(shows_rows) + len(missing)}개 이상 더 있을 수 있음 - 그건 문제 없음.")


if __name__ == "__main__":
    main()
