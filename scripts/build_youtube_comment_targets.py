"""
build_youtube_comment_targets.py
=================================
data/youtube_broadway_merged.csv (youtube_collect_broadway.py가 run 단위로 모은 영상
전체)를 입력받아, 댓글 수집 대상이 될 고유 video_id 목록(data/youtube_comment_targets.csv)을
만든다.

*** 왜 별도 target 빌더가 필요한가 ***
youtube_broadway_merged.csv는 (run_id, video_id) 기준으로 중복 제거돼 있어서, 같은
영상이 여러 run/쇼에 걸쳐 검색되면 여러 행으로 나타남(실제로 총 62,740행인데 고유
video_id는 54,796개 - 약 8천 개가 중복). 댓글은 영상 단위 데이터라 run과 무관하므로,
video_id 기준으로 한 번만 더 중복 제거해서 같은 영상 댓글을 두 번 긁는 낭비를 막는다.

*** comment_count == 0(또는 결측)인 영상은 아예 대상에서 제외 ***
commentThreads.list를 호출해봤자 빈 응답만 오고 API 유닛만 쓰므로, 영상 메타데이터에
이미 있는 comment_count(수집 시점 스냅샷, 정확한 실시간 값은 아니지만 "댓글이 아예
없다"는 판단에는 충분히 신뢰할 만함)가 0/결측이면 미리 걸러낸다.

Usage:
    python build_youtube_comment_targets.py \
        --merged data/youtube_broadway_merged.csv \
        --out data/youtube_comment_targets.csv
"""
import argparse

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged", default="data/youtube_broadway_merged.csv")
    ap.add_argument("--out", default="data/youtube_comment_targets.csv")
    args = ap.parse_args()

    df = pd.read_csv(args.merged, encoding="utf-8-sig", low_memory=False)
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]

    before = len(df)
    df["comment_count"] = pd.to_numeric(df.get("comment_count"), errors="coerce").fillna(0)

    # video_id 기준 중복 제거. 같은 영상이 여러 run에 걸려 있으면 comment_count가 더
    # 큰(=더 최근에 수집된, 또는 더 신뢰도 높은) 행을 대표로 남김.
    df = df.sort_values("comment_count", ascending=False).drop_duplicates(
        subset=["video_id"], keep="first"
    )
    n_dedup = len(df)

    has_comments = df["comment_count"] > 0
    targets = df.loc[has_comments, [
        "video_id", "show", "video_title", "channel_id", "channel_name",
        "published_at", "comment_count",
    ]].rename(columns={"comment_count": "comment_count_estimate"})
    n_skipped_zero = n_dedup - len(targets)

    targets.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"입력 {before}행 -> video_id 중복 제거 {n_dedup}개 -> "
          f"댓글 0개/결측 제외 {n_skipped_zero}개 스킵 -> "
          f"최종 수집 대상 {len(targets)}개 -> {args.out}")
    print(f"comment_count_estimate 합계(참고용, 실제 API 유닛 예산 감 잡기용): "
          f"{int(targets['comment_count_estimate'].sum()):,}")


if __name__ == "__main__":
    main()
