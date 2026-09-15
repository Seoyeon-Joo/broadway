"""
merge_condensed_youtube_shards.py
===================================
data/youtube_condensed/condensed_shard_*.csv 를 하나로 합쳐
data/youtube_condensed_merged.csv 생성.
merge_broadway_youtube_shards.py와 동일 로직(파일명 패턴만 다름) - 기존
스크립트를 건드리지 않고 별도 파일로 둬서 broadway 쪽 머지에 영향 없음.

Usage:
    python merge_condensed_youtube_shards.py \\
        --shard-dir data/youtube_condensed \\
        --out data/youtube_condensed_merged.csv
"""
import argparse
import glob
import os

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", default="data/youtube_condensed")
    ap.add_argument("--out", default="data/youtube_condensed_merged.csv")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.shard_dir, "condensed_shard_*.csv")))
    if not files:
        print(f"{args.shard_dir} 안에 condensed_shard_*.csv가 없어요.")
        return

    frames = []
    skipped = []
    for f in files:
        try:
            df = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            frames.append(df)
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
    merged = merged.drop_duplicates(subset=["run_id", "video_id"], keep="first")
    after = len(merged)

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    if skipped:
        print(f"\n주의: {len(skipped)}개 shard 파일을 건너뛰었어요 - 이 파일들은 병합 결과에 "
              f"안 들어갔으니 스키마 문제 해결 후 다시 수집해서 채워야 해요:")
        for f in skipped:
            print(f"  - {f}")
    print(f"{len(files)}개 shard 병합 -> {args.out}")
    print(f"  총 {before}행 -> 중복 제거 후 {after}행 ({before - after}건 제거)")
    print(f"  고유 run 수: {merged['run_id'].nunique()}")
    print(f"  고유 video_id 수: {merged['video_id'].nunique()}")
    if "query_category" in merged.columns:
        print(f"  카테고리별 건수:\n{merged['query_category'].value_counts().to_string()}")
    if "is_shorts_confirmed" in merged.columns:
        print(f"  Shorts 확인된 영상: {(merged['is_shorts_confirmed'] == True).sum()}건")


if __name__ == "__main__":
    main()
