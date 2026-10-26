"""
add_spoiler_intensity.py
------------------------
패널(final_v3)에 두 열을 추가한다.
  similarity         시놉시스 커버리지(cov_mean) - compute_similarity.py 결과
  spoiler_intensity  (영상 길이, 컷수, 유사도)를 PCA로 합친 1개 점수 (PC1)

처리 순서
  1) 분석 단위: (작품, t2 영상) 쌍. 세 값이 모두 있는 쌍만 PCA에 사용 (complete case).
  2) 변환: log(길이), log(컷수)  <- 오른쪽 꼬리가 길어서 극단값이 PCA를 지배하지 않게 함. 유사도는 그대로.
  3) 표준화(z-score) 후 PCA. 영화/공연은 분포가 달라 각각 따로 적합 (--pooled 로 합쳐서도 가능).
  4) PC1 점수를 spoiler_intensity 로 사용. 부호는 "세 변수 적재값의 합이 +"가 되도록 맞춤 (클수록 스포일러 강함).
  5) 세 값 중 하나라도 없는 행은 빈칸.
적재값, 설명 분산 비율, 상관행렬은 pca_report.json / pca_report.md 로 저장.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

FEATS = ["log_duration", "log_shots", "similarity"]


def prep(df, id_col):
    """패널에서 (작품, t2 영상) 쌍 단위 특징 테이블."""
    p = df.dropna(subset=["t2_video_id"]).drop_duplicates([id_col, "t2_video_id"])[
        [id_col, "t2_video_id", "t2_duration_sec", "t2_n_shots", "similarity"]].copy()
    p["log_duration"] = np.log(p["t2_duration_sec"].where(p["t2_duration_sec"] > 0))
    p["log_shots"] = np.log(p["t2_n_shots"].where(p["t2_n_shots"] > 0))
    return p


def fit_pca(p):
    cc = p.dropna(subset=FEATS)
    sc = StandardScaler().fit(cc[FEATS])
    pca = PCA(n_components=len(FEATS)).fit(sc.transform(cc[FEATS]))
    sign = 1.0 if pca.components_[0].sum() >= 0 else -1.0
    return sc, pca, sign, cc


def score(sc, pca, sign, p):
    out = pd.Series(np.nan, index=p.index)
    ok = p[FEATS].notna().all(axis=1)
    out[ok] = sign * pca.transform(sc.transform(p.loc[ok, FEATS]))[:, 0]
    return out


def report_block(name, sc, pca, sign, cc):
    ld = sign * pca.components_[0]
    return {
        "group": name,
        "n_pairs_used": int(len(cc)),
        "explained_variance_ratio": [round(float(x), 4) for x in pca.explained_variance_ratio_],
        "eigenvalues": [round(float(x), 4) for x in pca.explained_variance_],
        "pc1_loadings": {f: round(float(l), 4) for f, l in zip(FEATS, ld)},
        "corr_matrix": cc[FEATS].corr().round(3).to_dict(),
        "feature_mean": {f: round(float(m), 4) for f, m in zip(FEATS, sc.mean_)},
        "feature_std": {f: round(float(s), 4) for f, s in zip(FEATS, sc.scale_)},
    }


def insert_after(df, anchor, cols):
    """cols(DataFrame)를 anchor 열 바로 뒤에 끼워 넣는다."""
    base = [c for c in df.columns if c not in cols.columns]
    i = base.index(anchor) + 1 if anchor in base else len(base)
    order = base[:i] + list(cols.columns) + base[i:]
    return pd.concat([df.drop(columns=[c for c in cols.columns if c in df.columns], errors="ignore"),
                      cols], axis=1)[order]


def process(panel_path, res, medium, id_col, sheet, out_path, metric, pooled_store):
    sheets = pd.read_excel(panel_path, sheet_name=None)
    sheet = sheet or list(sheets)[0]
    df = sheets[sheet].drop(columns=["similarity", "spoiler_intensity"], errors="ignore")
    r = res[res["medium"] == medium][["work_id", "video_id", metric]].rename(
        columns={"work_id": id_col, "video_id": "t2_video_id", metric: "similarity"}).drop_duplicates(
        [id_col, "t2_video_id"])
    n0 = len(df)
    df = df.merge(r, on=[id_col, "t2_video_id"], how="left")
    assert len(df) == n0, "행 수가 달라짐"
    pooled_store[medium] = (df, id_col, sheet, sheets, out_path)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--hollywood", required=True)
    ap.add_argument("--broadway", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--metric", default="cov_mean")
    ap.add_argument("--pooled", action="store_true", help="영화+공연을 합쳐 한 번에 PCA (기본: 각각)")
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = pd.read_csv(a.results, encoding="utf-8-sig")

    store = {}
    process(a.hollywood, res, "hollywood", "movie_id", "weekly_data", out / "hollywood_final_v3_similarity.xlsx",
            a.metric, store)
    process(a.broadway, res, "broadway", "show_id", None, out / "broadway_final_v3_similarity.xlsx",
            a.metric, store)

    pairs = {m: prep(v[0], v[1]) for m, v in store.items()}
    reports, scores = [], {}
    if a.pooled:
        allp = pd.concat([p.assign(_m=m) for m, p in pairs.items()], ignore_index=True)
        sc, pca, sign, cc = fit_pca(allp)
        reports.append(report_block("pooled", sc, pca, sign, cc))
        allp["si"] = score(sc, pca, sign, allp)
        for m in pairs:
            scores[m] = allp[allp["_m"] == m].set_index([store[m][1], "t2_video_id"])["si"]
    else:
        for m, p in pairs.items():
            if len(p.dropna(subset=FEATS)) < 5:
                print(f"[warn] {m}: 세 값이 모두 있는 쌍이 5개 미만이라 PCA 생략")
                scores[m] = pd.Series(np.nan, index=pd.MultiIndex.from_frame(p[[store[m][1], "t2_video_id"]]))
                continue
            sc, pca, sign, cc = fit_pca(p)
            reports.append(report_block(m, sc, pca, sign, cc))
            p["si"] = score(sc, pca, sign, p)
            scores[m] = p.set_index([store[m][1], "t2_video_id"])["si"]

    for m, (df, id_col, sheet, sheets, path) in store.items():
        key = pd.MultiIndex.from_frame(df[[id_col, "t2_video_id"]])
        df["spoiler_intensity"] = scores[m].reindex(key).to_numpy()
        df.loc[df["t2_video_id"].isna(), "spoiler_intensity"] = np.nan
        new = df[["similarity", "spoiler_intensity"]]
        sheets[sheet] = insert_after(df.drop(columns=["similarity", "spoiler_intensity"]), "t2_shots_status", new)
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            for name, d in sheets.items():
                d.to_excel(w, sheet_name=name, index=False)
        n_t2 = int(df["t2_video_id"].notna().sum())
        print(f"{m}: rows {len(df)} | t2 rows {n_t2} | similarity {int(df['similarity'].notna().sum())} "
              f"| spoiler_intensity {int(df['spoiler_intensity'].notna().sum())} -> {path}")

    (out / "pca_report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Spoiler Intensity PCA 리포트\n"]
    for r_ in reports:
        md.append(f"## {r_['group']} (사용한 쌍 {r_['n_pairs_used']}개)\n")
        md.append(f"- 설명 분산 비율 PC1/PC2/PC3: {r_['explained_variance_ratio']}")
        md.append(f"- PC1 적재값: {r_['pc1_loadings']}")
        md.append(f"- 상관행렬: {r_['corr_matrix']}\n")
    (out / "pca_report.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(reports, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
