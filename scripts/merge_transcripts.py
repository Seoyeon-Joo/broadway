"""
merge_transcripts.py
--------------------
collect_transcripts.py 가 만든 shard CSV를 data/similarity/youtube_transcripts_all.xlsx 에 합친다.
같은 video_url이 있으면 새 결과로 교체(재시도 결과 반영), 없으면 추가. 열 구성은 기존 파일과 같다.
단, 새 결과가 상태 ok가 아니거나 기존 텍스트보다 단어 수가 적으면 기존 행을 유지한다
(예: Whisper가 빈 결과를 내도 기존 자막이 사라지지 않음). 기존에 텍스트가 없던 영상은 항상 새 결과로 교체.

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
    ap.add_argument("--prefer-new", action="store_true",
                    help="새 결과가 ok이면 기존 텍스트보다 짧아도 항상 새 결과로 교체 (한 방법으로 통일하고 싶을 때, 예: Whisper 전체). "
                         "새 결과가 ok가 아니면 이 옵션을 써도 기존을 유지")
    a = ap.parse_args()

    old = pd.read_excel(a.transcripts)
    parts = [pd.read_csv(f, encoding="utf-8-sig") for f in sorted(Path(a.shards).rglob("transcripts_shard_*.csv"))]
    new = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
    new = new.reindex(columns=COLS).drop_duplicates("video_url", keep="last")

    # 새 결과가 기존 텍스트보다 나쁘면(ok가 아니거나 단어 수가 적음) 기존 행을 유지
    nwords = lambda x: x.fillna("").astype(str).str.split().str.len()
    old_w = old.drop_duplicates("video_url", keep="last").set_index("video_url")["text"]
    old_w = nwords(old_w)
    new_w = nwords(new.set_index("video_url")["text"])
    worse = [u for u in new["video_url"]
             if u in old_w.index and old_w[u] > 0
             and (new.set_index("video_url").at[u, "status"] != "ok"
                  or (not a.prefer_new and new_w[u] < old_w[u]))]
    if worse:
        print(f"기존 텍스트가 더 좋아 유지: {len(worse)}개")
        new = new[~new["video_url"].isin(worse)]

    keep = old[~old["video_url"].isin(new["video_url"])]
    out = pd.concat([keep.reindex(columns=COLS), new], ignore_index=True)
    out.to_excel(a.transcripts, index=False)

    gained = new["status"].value_counts().to_dict()
    import json
    for f in sorted(Path(a.shards).rglob("diag_shard_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"shard {d['shard']}: {d['counts']}")
        for sm in d["samples"][:2]:
            print("   ", sm)
    print(f"existing {len(old)} | new/updated {len(new)} {gained} | total now {len(out)}")
    print("total by status:", out["status"].value_counts().to_dict())


if __name__ == "__main__":
    main()
