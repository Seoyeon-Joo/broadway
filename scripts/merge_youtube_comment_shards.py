"""
merge_youtube_comment_shards.py
================================
data/youtube_comments/shard_*.csv 를 하나로 합쳐 data/youtube_comments_merged.csv 생성.
comment_id 기준으로 중복 제거 (comment_id는 YouTube 전역에서 유일하므로
merge_broadway_youtube_shards.py처럼 별도 복합키가 필요 없음).

Usage:
    python merge_youtube_comment_shards.py \
        --shard-dir data/youtube_comments \
        --out data/youtube_comments_merged.csv
"""
import argparse
import glob
import os

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", default="data/youtube_comments")
    ap.add_argument("--out", default="data/youtube_comments_merged.csv")
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
    merged = merged.drop_duplicates(subset=["comment_id"], keep="first")
    after = len(merged)

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    n_replies = int(merged["is_reply"].astype(str).str.lower().eq("true").sum())
    print(f"{len(files)}개 shard 병합 -> {args.out}")
    print(f"  총 {before}행 -> 중복 제거 후 {after}행 ({before - after}건 제거)")
    print(f"  고유 video_id 수: {merged['video_id'].nunique()}")
    print(f"  top-level 댓글: {after - n_replies}건 / 답글: {n_replies}건")


if __name__ == "__main__":
    main()
