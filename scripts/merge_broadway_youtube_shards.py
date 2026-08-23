"""
merge_broadway_youtube_shards.py
==================================
data/youtube_broadway/shard_*.csv 를 하나로 합쳐 data/youtube_broadway_merged.csv 생성.
(run_id, video_id) 조합 기준으로 중복 제거 (리바이벌 간 같은 영상이 각자의 run에
독립적으로 걸려도 정상적으로 각각 남도록 run_id를 키에 포함).

Usage:
    python merge_broadway_youtube_shards.py \
        --shard-dir data/youtube_broadway \
        --out data/youtube_broadway_merged.csv
"""
import argparse
import glob
import os

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", default="data/youtube_broadway")
    ap.add_argument("--out", default="data/youtube_broadway_merged.csv")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.shard_dir, "shard_*.csv")))
    if not files:
        print(f"{args.shard_dir} 안에 shard_*.csv가 없어요.")
        return

    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            frames.append(df)
        except pd.errors.EmptyDataError:
            print(f"  [빈 파일 건너뜀] {f}")

    merged = pd.concat(frames, ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["run_id", "video_id"], keep="first")
    after = len(merged)

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"{len(files)}개 shard 병합 -> {args.out}")
    print(f"  총 {before}행 -> 중복 제거 후 {after}행 ({before - after}건 제거)")
    print(f"  고유 run 수: {merged['run_id'].nunique()}")
    print(f"  고유 video_id 수: {merged['video_id'].nunique()}")


if __name__ == "__main__":
    main()
