"""
add_new_condensed_targets.py
============================
최신 패널(Hollywood v13, Broadway current)의 t2/s2 영상 중 기존 수집 목록
(data/condensed_comment_targets.csv)에 없는 것만 목록 *끝에* 추가한다.
기존 행의 순서는 그대로 두므로 이미 수집한 shard 배정이 바뀌지 않는다(이어받기 안전).
또 모든 행에 in_current_panel(1/0) 열을 붙여, 현재 패널에서 빠진 영상(오매칭·비영어 정리 등)은
댓글을 지우지 않고 분석에서만 거를 수 있게 한다.
"""
import argparse
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--targets", default="data/condensed_comment_targets.csv")
ap.add_argument("--hollywood", default="data/similarity/panel_current/hollywood_v13.xlsx")
ap.add_argument("--broadway", default="data/similarity/panel_current/broadway.xlsx")
ap.add_argument("--movies", default="data/hollywood_youtube_targets.csv")
a = ap.parse_args()

t = pd.read_csv(a.targets, dtype=str, encoding="utf-8-sig")
t = t.drop(columns=[c for c in ["in_current_panel"] if c in t.columns])
movies = pd.read_csv(a.movies, dtype=str, encoding="utf-8-sig")[["movie_id", "movie_title"]].drop_duplicates("movie_id")
title = dict(zip(movies.movie_id, movies.movie_title))

rows = []
h = pd.read_excel(a.hollywood, sheet_name="Sheet1", dtype=str)
for col, src in (("t2_video_id", "t2"), ("s2_video_id", "s2")):
    x = h.dropna(subset=[col]).copy()
    x["video_id"], x["source"] = x[col], src
    x["medium"], x["work_id"] = "hollywood", x["movie_id"]
    x["show"] = x["movie_id"].map(title).fillna("")
    cols = ["video_id", "medium", "work_id", "show", "source"] + (["duration_sec", "n_shots"] if src == "t2" else [])
    rows.append(x[cols])
b = pd.read_excel(a.broadway, sheet_name="Sheet1", dtype=str)
x = b.dropna(subset=["t2_video_id"]).copy()
x["video_id"], x["source"], x["medium"], x["work_id"] = x["t2_video_id"], "t2", "broadway", ""
rows.append(x[["video_id", "medium", "work_id", "show", "source", "duration_sec", "n_shots"]])
cur = pd.concat(rows, ignore_index=True)
src_all = cur.groupby("video_id")["source"].agg(lambda v: "both" if len(set(v)) > 1 else v.iloc[0])
cur = cur.drop_duplicates("video_id", keep="first")
cur["source"] = cur["video_id"].map(src_all)

new = cur[~cur["video_id"].isin(t["video_id"])].copy()
t["in_current_panel"] = t["video_id"].isin(cur["video_id"]).astype(int).astype(str)
new["in_current_panel"] = "1"
out = pd.concat([t, new], ignore_index=True)
out.to_csv(a.targets, index=False, encoding="utf-8-sig")
print(f"기존 {len(t)}개 (현재 패널에 있음 {int((t.in_current_panel=='1').sum())} / 빠짐 {int((t.in_current_panel=='0').sum())})")
print(f"신규 추가 {len(new)}개: " + str(new.groupby(['medium', 'source']).size().to_dict()))
print(f"최종 {len(out)}개")
