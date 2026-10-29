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
    ap.add_argument("--combined", default="data/combined_panel.xlsx",
                    help="combined 패널(t2_video_id/s2_video_id 열). 여기 있는 condensed "
                         "영상 중 위 목록에 없는 것을 뒤에 추가함(기존 순서 유지 = 이미 "
                         "수집한 shard 배정이 바뀌지 않음)")
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
    t["source"] = "t2"

    # combined 패널에 있는 condensed 영상 중 아직 없는 것(주로 Hollywood s2 영상)을 뒤에 추가
    if args.combined:
        d = pd.read_excel(args.combined, sheet_name="Sheet1", dtype=str)
        d["medium"] = d["market"].str.lower()
        extra = []
        for col, src in (("t2_video_id", "t2"), ("s2_video_id", "s2")):
            x = d.dropna(subset=[col])[[col, "medium", "orig_id", "title"]].drop_duplicates(col)
            x = x.rename(columns={col: "video_id", "orig_id": "work_id", "title": "show"})
            x["source"] = src
            extra.append(x)
        e = pd.concat(extra, ignore_index=True)
        both = e.groupby("video_id")["source"].agg(lambda v: "both" if len(set(v)) > 1 else v.iloc[0])
        e = e.drop_duplicates("video_id", keep="first")
        e["source"] = e["video_id"].map(both)
        # 기존 목록에 있는 영상은 source만 갱신, 없는 영상은 뒤에 추가
        t["source"] = t["video_id"].map(both).fillna("t2")
        new = e[~e["video_id"].isin(t["video_id"])]
        t = pd.concat([t, new], ignore_index=True)
        print(f"combined 패널 추가: {len(new)}개 (t2 {int((new.source=='t2').sum())} / "
              f"s2 {int((new.source=='s2').sum())} / both {int((new.source=='both').sum())})")

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
