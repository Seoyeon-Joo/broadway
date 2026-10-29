"""
build_condensed_comment_targets.py
===================================
condensed content로 지정된 영상(= 패널의 t2 영상, similarity/컷 수를 계산한 영상)만 골라
YouTube 댓글 수집 대상 파일(data/condensed_comment_targets.csv)을 만든다.

입력: data/similarity/panel_with_similarity/{broadway,hollywood}_final_v3_similarity.xlsx
      의 `video_metrics` 시트 (video_id, 작품, 컷 수 등)
출력: data/condensed_comment_targets.csv
      fetch_youtube_comments.py가 읽는 형식(video_id, show 열 필수)과 호환.
      show 열에는 Broadway는 쇼 이름, Hollywood는 영화 제목이 들어감.

Usage:
    python scripts/build_condensed_comment_targets.py
"""
import argparse

import pandas as pd

BW = "data/similarity/panel_with_similarity/broadway_final_v3_similarity.xlsx"
HW = "data/similarity/panel_with_similarity/hollywood_final_v3_similarity.xlsx"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default=BW)
    ap.add_argument("--hollywood", default=HW)
    ap.add_argument("--movies", default="data/hollywood_youtube_targets.csv")
    ap.add_argument("--stats", default="data/youtube_video_stats.csv")
    ap.add_argument("--out", default="data/condensed_comment_targets.csv")
    args = ap.parse_args()

    b = pd.read_excel(args.broadway, sheet_name="video_metrics", dtype=str)
    b["medium"] = "broadway"
    b["work_id"] = ""
    b = b.rename(columns={"show": "show"})

    h = pd.read_excel(args.hollywood, sheet_name="video_metrics", dtype=str)
    h["medium"] = "hollywood"
    movies = pd.read_csv(args.movies, dtype=str, encoding="utf-8-sig")[["movie_id", "movie_title"]]
    h = h.merge(movies, on="movie_id", how="left").rename(
        columns={"movie_title": "show", "movie_id": "work_id"})
    h["show"] = h["show"].fillna("")

    cols = ["video_id", "medium", "work_id", "show", "weeks_as_t2",
            "duration_sec", "n_shots", "status"]
    t = pd.concat([b[cols], h[cols]], ignore_index=True)
    t = t.drop_duplicates(subset=["video_id"], keep="first")

    # 참고용: 수집 시점 comment_count (있는 영상만). 0이면 API 호출 낭비라 제외.
    s = pd.read_csv(args.stats, dtype=str)[["video_id", "comment_count"]]
    s["comment_count"] = pd.to_numeric(s["comment_count"], errors="coerce")
    s = s.dropna(subset=["video_id"]).drop_duplicates("video_id")
    t = t.merge(s.rename(columns={"comment_count": "comment_count_estimate"}),
                on="video_id", how="left")
    n_zero = int((t["comment_count_estimate"] == 0).sum())
    t = t[t["comment_count_estimate"].fillna(1) > 0]

    t.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"대상 {len(t)}개 (broadway {int((t.medium == 'broadway').sum())} / "
          f"hollywood {int((t.medium == 'hollywood').sum())}), "
          f"comment_count=0 제외 {n_zero}개 -> {args.out}")
    known = t["comment_count_estimate"].notna().sum()
    print(f"comment_count 알려진 영상 {known}개, 합계 {int(t['comment_count_estimate'].sum()):,}")


if __name__ == "__main__":
    main()
