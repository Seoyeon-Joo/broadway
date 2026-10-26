"""
add_similarity_to_panel.py
--------------------------
compute_similarity.py 결과(similarity_results.csv)를 패널 xlsx에 `similarity` 열로 붙인다.

  similarity = 시놉시스 커버리지(cov_mean): 시놉시스 각 문장에 대해 영상 스크립트 청크와의 최대 코사인 유사도의 평균
  행 단위 키: (작품 ID, t2_video_id). 계산하지 못한 행(자막 없음·비영어·시놉시스 없음 등)과
  t2_video_id가 없는 행은 빈칸(NaN)으로 남긴다.

사용:
  python scripts/add_similarity_to_panel.py --results data/similarity/results/similarity_results.csv \
      --hollywood data/similarity/hollywood_final_v2.xlsx --broadway data/similarity/broadway_final_v2.xlsx \
      --out-dir data/similarity/panel_with_similarity [--metric cov_mean]
"""
import argparse
from pathlib import Path

import pandas as pd


def attach(panel_path, res, medium, id_col, out_path, metric, sheet=None):
    sheets = pd.read_excel(panel_path, sheet_name=None)
    main_name = sheet or list(sheets)[0]
    df = sheets[main_name]
    r = res[res["medium"] == medium][["work_id", "video_id", metric]].rename(
        columns={"work_id": id_col, "video_id": "t2_video_id", metric: "similarity"})
    r = r.drop_duplicates([id_col, "t2_video_id"])
    if "similarity" in df.columns:
        df = df.drop(columns="similarity")
    df = df.merge(r, on=[id_col, "t2_video_id"], how="left")
    assert len(df) == len(sheets[main_name]), "행 수가 달라졌다 (매칭 키 중복 확인)"
    sheets[main_name] = df
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        for name, d in sheets.items():
            d.to_excel(w, sheet_name=name, index=False)
    n_t2 = int(df["t2_video_id"].notna().sum())
    n_ok = int(df["similarity"].notna().sum())
    print(f"{medium}: rows {len(df)} | rows with t2 video {n_t2} | rows with similarity {n_ok} "
          f"({n_ok / max(n_t2, 1):.1%} of t2 rows) -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--hollywood", required=True)
    ap.add_argument("--broadway", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--metric", default="cov_mean")
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = pd.read_csv(a.results, encoding="utf-8-sig")
    attach(a.hollywood, res, "hollywood", "movie_id", out / "hollywood_final_v2_similarity.xlsx", a.metric,
           sheet="weekly_data")
    attach(a.broadway, res, "broadway", "show_id", out / "broadway_final_v2_similarity.xlsx", a.metric)


if __name__ == "__main__":
    main()
