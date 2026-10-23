"""
build_video_work_map.py
-----------------------
스크립트(transcript) 엑셀의 video_url을 작품(Hollywood work_id / Broadway show_id)에 연결하는
매핑 CSV(data/video_work_map.csv)를 만든다.

입력:
  --transcripts         youtube_transcripts_all.xlsx (video_url 열 필요)
  --hollywood-merged    hollywood_youtube_merged.csv  (Release: hollywood-youtube-data)
  --broadway-merged     youtube_broadway_merged.csv   (Release: broadway-youtube-data)
  --broadway-condensed  youtube_condensed_merged.csv  (Release: broadway-youtube-data)
  --synopses-broadway   synopses_broadway.xlsx   (show_id, show)

출력: data/video_work_map.csv
  columns: video_url, medium(hollywood|broadway), work_id, work_title
  * 한 영상이 여러 작품에 걸려 있으면 (video, work) 쌍마다 한 행.
  * Broadway는 run_id가 아니라 시놉시스 단위인 show_id로 정규화한 제목 매칭.

Release 파일이 크므로(수백 MB) 이 스크립트는 로컬/수동 1회 실행용이다.
유사도 계산 워크플로는 만들어진 data/video_work_map.csv만 읽는다.
"""
import argparse
import re
import sys

import pandas as pd


def norm(s: str) -> str:
    s = str(s).lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def read_urls(path, url_col, keep_cols, urls):
    out = []
    for chunk in pd.read_csv(path, encoding="utf-8-sig", usecols=lambda c: c.strip() in keep_cols,
                             chunksize=200_000, low_memory=False):
        chunk.columns = [c.strip() for c in chunk.columns]
        out.append(chunk[chunk[url_col].isin(urls)])
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", required=True)
    ap.add_argument("--hollywood-merged", required=True)
    ap.add_argument("--broadway-merged", required=True)
    ap.add_argument("--broadway-condensed", default=None)
    ap.add_argument("--synopses-broadway", required=True)
    ap.add_argument("--out", default="data/video_work_map.csv")
    a = ap.parse_args()

    urls = set(pd.read_excel(a.transcripts, usecols=["video_url"]).video_url.dropna())
    print(f"transcript urls: {len(urls)}")

    rows = []

    hw = read_urls(a.hollywood_merged, "vedio_url", {"movie_id", "movie_title", "vedio_url"}, urls)
    hw = hw.rename(columns={"vedio_url": "video_url", "movie_id": "work_id", "movie_title": "work_title"})
    hw["medium"] = "hollywood"
    rows.append(hw[["video_url", "medium", "work_id", "work_title"]].drop_duplicates())
    print(f"hollywood pairs: {len(rows[-1])}")

    syn = pd.read_excel(a.synopses_broadway, usecols=["show_id", "show"])
    syn["key"] = syn["show"].map(norm)
    key2id = syn.drop_duplicates("key").set_index("key")

    bw_parts = []
    bw = read_urls(a.broadway_merged, "video_url", {"show", "video_url"}, urls)
    bw_parts.append(bw[["video_url", "show"]])
    if a.broadway_condensed:
        bc = read_urls(a.broadway_condensed, "vedio_url", {"show", "vedio_url"}, urls)
        bc = bc.rename(columns={"vedio_url": "video_url"})
        bw_parts.append(bc[["video_url", "show"]])
    bw = pd.concat(bw_parts, ignore_index=True).drop_duplicates()
    bw["key"] = bw["show"].map(norm)
    bw = bw.merge(key2id[["show_id"]], left_on="key", right_index=True, how="left")
    unmatched = bw[bw.show_id.isna()]
    if len(unmatched):
        print(f"[warn] broadway pairs without synopsis (show not in synopses): {len(unmatched)} "
              f"({unmatched['show'].nunique()} shows)")
    bw = bw.dropna(subset=["show_id"])
    bw["work_id"] = bw["show_id"].astype(int)
    bw["work_title"] = bw["show"]
    bw["medium"] = "broadway"
    rows.append(bw[["video_url", "medium", "work_id", "work_title"]].drop_duplicates())
    print(f"broadway pairs: {len(rows[-1])}")

    out = pd.concat(rows, ignore_index=True).drop_duplicates(["video_url", "medium", "work_id"])
    out.to_csv(a.out, index=False, encoding="utf-8-sig")
    covered = out.video_url.nunique()
    print(f"mapped videos: {covered}/{len(urls)} | pairs: {len(out)} -> {a.out}")
    miss = urls - set(out.video_url)
    print(f"videos with no work mapping: {len(miss)}")


if __name__ == "__main__":
    sys.exit(main())
