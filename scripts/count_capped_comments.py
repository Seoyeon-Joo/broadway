"""
count_capped_comments.py
=========================
상한(capped)에 걸려 일부만 수집된 영상들의 "실제 댓글 수"를 YouTube Data API
videos.list(part=statistics)로 조회한다. 50개 ID를 1유닛에 조회하므로 거의 공짜.
댓글 본문은 받지 않는다.

입력: --checkpoint-dir 안의 shard_*.processed.txt (status=capped인 video_id)
출력: --out CSV (video_id, comment_count, 이미 수집한 행 수 n_collected) + 요약 출력

Usage:
    python scripts/count_capped_comments.py --checkpoint-dir data/condensed_comments \
        --out data/capped_comment_counts.csv
"""
import argparse
import csv
import glob
import os
import sys

import requests

API = "https://www.googleapis.com/youtube/v3/videos"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint-dir", default="data/condensed_comments")
    ap.add_argument("--out", default="data/capped_comment_counts.csv")
    args = ap.parse_args()

    keys = [k.strip() for k in os.environ.get("YOUTUBE_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        print("YOUTUBE_API_KEYS 가 없어요.")
        sys.exit(1)

    capped = {}
    for f in glob.glob(os.path.join(args.checkpoint_dir, "shard_*.processed.txt")):
        with open(f, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "capped":
                    capped[row["video_id"]] = int(row.get("n_collected") or 0)
    ids = sorted(capped)
    print(f"상한에 걸린 영상 {len(ids)}개 조회")

    counts, ki = {}, 0
    s = requests.Session()
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        for _ in range(len(keys)):
            r = s.get(API, params={"part": "statistics", "id": ",".join(chunk),
                                    "key": keys[ki % len(keys)]}, timeout=30)
            if r.status_code == 200:
                for it in r.json().get("items", []):
                    counts[it["id"]] = int(it.get("statistics", {}).get("commentCount", 0))
                break
            ki += 1
        else:
            print("모든 키 실패:", r.status_code, r.text[:200])
            sys.exit(1)

    rows = [(v, counts.get(v, ""), capped[v]) for v in ids]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["video_id", "comment_count", "n_collected"])
        w.writerows(rows)

    known = sorted(c for c in counts.values())
    print(f"댓글 수 조회 성공 {len(known)}개 / 못 찾음 {len(ids) - len(known)}개")
    if known:
        tot = sum(known)
        print(f"전체 댓글 수 합계 {tot:,} (이미 수집한 행 {sum(capped.values()):,})")
        for th in (1000, 5000, 10000, 50000, 100000, 500000):
            print(f"  {th:>7,}개 이상: {sum(1 for c in known if c >= th)}개 영상")
        print("상위 10개:", [(v, counts[v]) for v in sorted(counts, key=counts.get, reverse=True)[:10]])


if __name__ == "__main__":
    main()
