"""
merge_hollywood_youtube_shards.py
==================================
data/youtube_hollywood/shard_*.csv 를 하나로 합쳐
data/hollywood_youtube_merged.csv 를 만든다.
(Broadway의 merge_condensed_youtube_shards.py와 같은 로직, 키만 movie_id)

Usage:
    python scripts/merge_hollywood_youtube_shards.py \\
        --shard-dir data/youtube_hollywood \\
        --out data/hollywood_youtube_merged.csv
"""
import argparse
import glob
import os

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", default="data/youtube_hollywood")
    ap.add_argument("--out", default="data/hollywood_youtube_merged.csv")
    ap.add_argument("--targets", default="data/hollywood_youtube_targets.csv",
                    help="수집 진행률(전체 영화 대비) 출력용")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.shard_dir, "shard_*.csv")))
    if not files:
        print(f"{args.shard_dir} 안에 shard_*.csv가 없어요.")
        return

    frames, skipped = [], []
    for f in files:
        try:
            frames.append(pd.read_csv(f, encoding="utf-8-sig", low_memory=False))
        except pd.errors.EmptyDataError:
            print(f"  [빈 파일 건너뜀] {f}")
        except pd.errors.ParserError as e:
            print(f"  [깨진 파일 건너뜀 - 컬럼 수 불일치, 확인 필요] {f}\n    {e}")
            skipped.append(f)

    if not frames:
        print("병합할 수 있는 shard가 하나도 없어요.")
        return

    merged = pd.concat(frames, ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["movie_id", "video_id"], keep="first")
    merged = merged.sort_values(["movie_id", "query_category"], kind="stable")
    merged.to_csv(args.out, index=False, encoding="utf-8-sig")

    if skipped:
        print(f"\n주의: {len(skipped)}개 shard를 건너뛰었어요 (병합 결과에 없음):")
        for f in skipped:
            print(f"  - {f}")
    print(f"{len(files)}개 shard 병합 -> {args.out}")
    print(f"  총 {before}행 -> 중복 제거 후 {len(merged)}행 ({before - len(merged)}건 제거)")
    n_movies = merged["movie_id"].nunique()
    if os.path.isfile(args.targets):
        n_total = len(pd.read_csv(args.targets, encoding="utf-8-sig"))
        print(f"  영상이 1건 이상 수집된 영화: {n_movies} / {n_total}편")
    else:
        print(f"  영상이 1건 이상 수집된 영화: {n_movies}편")
    print(f"  고유 video_id 수: {merged['video_id'].nunique()}")
    print(f"  카테고리별 건수:\n{merged['query_category'].value_counts().to_string()}")
    if "is_shorts" in merged.columns:
        print(f"  Shorts 확인된 영상: {(merged['is_shorts'] == 1).sum()}건")


if __name__ == "__main__":
    main()
