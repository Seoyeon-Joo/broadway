"""
merge_broadwayworld_meta.py
=============================
fetch_broadwayworld_full.py로 만든 broadwayworld_full.csv(장르/캐스트/창작진/
프로듀서/개막·폐막일/원작유무/기사수/시상식별 수상·후보), fetch_tony_awards.py로
만든 tony_awards.csv, fetch_bww_reviews.py로 만든 bww_reviews.csv(집계 평점만,
개별 리뷰 텍스트는 별도 파일)를 data/broadway.csv에 show 이름 기준으로 병합.

컬럼을 고정 목록으로 하드코딩하지 않고, 각 소스 파일에 있는 컬럼(title 제외)을
전부 자동으로 병합함. 시상식별 컬럼(tony_awards_wins, drama_desk_awards_nominations
등)이 쇼마다 다르게 생기는 동적 스키마라 고정 목록 방식은 한계가 있어서 이렇게
바꿈. 이미 있는 주간 흥행 컬럼(week_ending, weekly_gross, seats_sold 등)은 전혀
건드리지 않고, 이미 채워진 값은 유지함(덮어쓰지 않음, 새로 찾은 값이 있을 때만 채움).

Usage:
  python merge_broadwayworld_meta.py \
      --broadway data/broadway.csv \
      --meta data/broadwayworld_full.csv \
      --awards data/tony_awards.csv \
      --reviews data/bww_reviews.csv \
      --out data/broadway.csv
"""
import argparse

import pandas as pd


def merge_source(broadway, source_path, source_label):
    if not source_path:
        return broadway
    src = pd.read_csv(source_path, sep=None, engine="python", encoding="utf-8-sig")
    src.columns = [c.strip().lstrip("\ufeff") for c in src.columns]
    if "title" not in src.columns:
        print(f"[{source_label}] 'title' 컬럼이 없어서 건너뜀. 실제 컬럼: {list(src.columns)}")
        return broadway

    # title/showid를 제외한 모든 컬럼을 자동으로 병합 대상으로 삼음
    columns = [c for c in src.columns if c not in ("title", "showid")]
    if not columns:
        print(f"[{source_label}] 병합할 컬럼이 없어서 건너뜀")
        return broadway

    src_small = src.drop_duplicates(subset=["title"], keep="last")[["title"] + columns]

    merged = broadway.merge(
        src_small, left_on="show", right_on="title", how="left", suffixes=("", "_new")
    )

    for col in columns:
        new_col = f"{col}_new"
        if new_col not in merged.columns:
            continue
        if col in merged.columns:
            # 숫자 컬럼(예: _wins/_nominations)은 0도 유효한 값이라 notna만으로 판단
            existing = merged[col]
            is_missing = existing.isna()
            if existing.dtype == object:
                is_missing = is_missing | (existing == "")
            merged[col] = existing.where(~is_missing, merged[new_col])
        else:
            merged[col] = merged[new_col]
        merged = merged.drop(columns=[new_col])

    if "title" in merged.columns and "title" not in broadway.columns:
        merged = merged.drop(columns=["title"])

    n_matched = merged[columns[0]].notna().sum() if columns else 0
    print(f"[{source_label}] 매칭된 행: {n_matched}/{len(merged)}, 추가/갱신된 컬럼: {columns}")
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default="data/broadway.csv")
    ap.add_argument("--meta", default=None, help="broadwayworld_full.csv 경로")
    ap.add_argument("--awards", default=None, help="tony_awards.csv 경로")
    ap.add_argument("--reviews", default=None, help="bww_reviews.csv 경로 (집계 평점만)")
    ap.add_argument("--out", default="data/broadway.csv")
    args = ap.parse_args()

    broadway = pd.read_csv(args.broadway, sep=None, engine="python", encoding="utf-8-sig")
    broadway.columns = [c.strip().lstrip("\ufeff") for c in broadway.columns]

    if "show" not in broadway.columns:
        raise SystemExit(f"broadway.csv에 'show' 컬럼이 없어요. 실제 컬럼: {list(broadway.columns)}")

    merged = merge_source(broadway, args.meta, "BroadwayWorld meta")
    merged = merge_source(merged, args.awards, "Tony Awards")
    merged = merge_source(merged, args.reviews, "BroadwayWorld reviews")

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {args.out} ({len(merged)}행, {len(merged.columns)}컬럼)")


if __name__ == "__main__":
    main()
