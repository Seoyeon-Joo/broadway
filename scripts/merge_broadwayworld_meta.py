"""
merge_broadwayworld_meta.py
=============================
fetch_broadwayworld_full.py로 만든 broadwayworld_full.csv(장르/캐스트/창작진/
프로듀서/개막·폐막일/원작유무)와, fetch_ibdb_awards.py로 만든 ibdb_awards.csv
(토니/퓰리처/드라마데스크 수상 이력)를 data/broadway.csv에 show 이름 기준으로 병합.

이미 있는 주간 흥행 컬럼(week_ending, weekly_gross, seats_sold 등)은 전혀
건드리지 않고, 아래 META_COLUMNS + AWARD_COLUMNS만 새로 추가함. 이미 병합된
적이 있어서 해당 컬럼이 이미 존재하면, 새로 찾은 값이 있을 때만 채우고
기존 값은 유지함(덮어쓰지 않음).

Usage:
  python merge_broadwayworld_meta.py \
      --broadway data/broadway.csv \
      --meta data/broadwayworld_full.csv \
      --awards data/ibdb_awards.csv \
      --out data/broadway.csv
"""
import argparse

import pandas as pd

META_COLUMNS = [
    "genre", "cast", "creative_team", "producer",
    "first_preview", "opening_date", "closing_date", "based_on",
]

AWARD_COLUMNS = [
    "tony_nominations", "tony_wins", "has_pulitzer", "has_drama_desk_win", "awards_detail",
]


def merge_source(broadway, source_path, columns, source_label):
    if not source_path:
        return broadway
    src = pd.read_csv(source_path, sep=None, engine="python", encoding="utf-8-sig")
    src.columns = [c.strip().lstrip("\ufeff") for c in src.columns]
    if "title" not in src.columns:
        print(f"[{source_label}] 'title' 컬럼이 없어서 건너뜀. 실제 컬럼: {list(src.columns)}")
        return broadway

    available_cols = [c for c in columns if c in src.columns]
    src_small = src.drop_duplicates(subset=["title"], keep="last")[["title"] + available_cols]

    merged = broadway.merge(
        src_small, left_on="show", right_on="title", how="left", suffixes=("", "_new")
    )

    for col in columns:
        new_col = f"{col}_new"
        if new_col not in merged.columns:
            continue
        if col in merged.columns:
            merged[col] = merged[col].where(merged[col].notna() & (merged[col] != ""), merged[new_col])
        else:
            merged[col] = merged[new_col]
        merged = merged.drop(columns=[new_col])

    if "title" in merged.columns and "title" not in broadway.columns:
        merged = merged.drop(columns=["title"])

    n_matched = merged[available_cols[0]].notna().sum() if available_cols else 0
    print(f"[{source_label}] 매칭된 행: {n_matched}/{len(merged)}")
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default="data/broadway.csv")
    ap.add_argument("--meta", default=None, help="broadwayworld_full.csv 경로 (genre/cast/creative_team 등)")
    ap.add_argument("--awards", default=None, help="ibdb_awards.csv 경로 (tony_wins 등)")
    ap.add_argument("--out", default="data/broadway.csv")
    args = ap.parse_args()

    broadway = pd.read_csv(args.broadway, sep=None, engine="python", encoding="utf-8-sig")
    broadway.columns = [c.strip().lstrip("\ufeff") for c in broadway.columns]

    if "show" not in broadway.columns:
        raise SystemExit(f"broadway.csv에 'show' 컬럼이 없어요. 실제 컬럼: {list(broadway.columns)}")

    merged = merge_source(broadway, args.meta, META_COLUMNS, "BroadwayWorld meta")
    merged = merge_source(merged, args.awards, AWARD_COLUMNS, "IBDB awards")

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {args.out} ({len(merged)}행, {len(merged.columns)}컬럼)")


if __name__ == "__main__":
    main()
