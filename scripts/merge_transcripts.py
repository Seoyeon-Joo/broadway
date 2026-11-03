"""
merge_transcripts.py
--------------------
collect_transcripts.py 가 만든 shard CSV를 data/similarity/youtube_transcripts_all.xlsx 에 합친다.
같은 video_url이 있으면 새 결과로 교체(재시도 결과 반영), 없으면 추가. 열 구성은 기존 파일과 같다.

사용:
  python scripts/merge_transcripts.py --shards transcripts_in [--transcripts data/similarity/youtube_transcripts_all.xlsx]
"""
import argparse
from pathlib import Path

import pandas as pd

COLS = ["video_url", "text", "status", "language", "is_auto", "title"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", required=True)
    ap.add_argument("--transcripts", default="data/similarity/youtube_transcripts_all.xlsx")
    a = ap.parse_args()

    old = pd.read_excel(a.transcripts)
    parts = [pd.read_csv(f, encoding="utf-8-sig") for f in sorted(Path(a.shards).rglob("transcripts_shard_*.csv"))]
    new = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
    new = new.reindex(columns=COLS).drop_duplicates("video_url", keep="last")

    keep = old[~old["video_url"].isin(new["video_url"])]
    out = pd.concat([keep.reindex(columns=COLS), new], ignore_index=True)
    out.to_excel(a.transcripts, index=False)

    gained = new["status"].value_counts().to_dict()
    print(f"existing {len(old)} | new/updated {len(new)} {gained} | total now {len(out)}")
    print("total by status:", out["status"].value_counts().to_dict())


if __name__ == "__main__":
    main()
