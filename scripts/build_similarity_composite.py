"""
build_similarity_composite.py
-----------------------------
여러 방식으로 잰 시놉시스-스크립트 유사도(기존 bge-m3 + ALT + EXTRA)를 하나로 묶고, 자막 길이 편향을 점검·보정한다.

입력 (있는 것만 쓴다)
  data/similarity/results_alt/similarity_alt_results.csv      (lex_*, sbert_*, bge_large_en_*, bgem3_*)
  data/similarity/results_extra/similarity_extra_results.csv  (q3emb06_*, x_rerank_*, x_nli_*, 순서 지표)

합성에 쓰는 지표: '시놉시스 내용이 영상에 얼마나 담겼는가'를 재는 지표만 쓴다.
  *_cov_mean(결말부 end_ 제외), *_doc_cosine, lex_tfidf_uni_cos, lex_tfidf_bi_cos, lex_word_recall, lex_entity_recall,
  x_rerank_mean, x_nli_mean
  (bgem3_tfidf_cosine은 lex_tfidf_uni_cos와 같은 설정이라 이중 계산을 피하려 제외. 순서 지표 order_tau/mono_ratio/span은
   '얼마나'가 아니라 '어떻게'를 재므로 합성에서 제외하고 따로 쓴다.)

산출물 (--out-dir, 기본 data/similarity/results_extra)
  similarity_composite.csv         쌍별 합성 지표
    sim_z_mean            선택된 지표의 z점수 평균 (결측은 가능한 지표만 평균, 지표의 min_share 이상일 때만)
    sim_pc1               z점수의 1주성분 점수 (완전 관측 쌍만, 부호는 평균 적재가 +가 되게)
    sim_z_mean_lenadj     sim_z_mean에서 log(자막 단어 수)·log(시놉시스 단어 수)·log(문장 수)를 매체별 OLS로 제거한 잔차
    sim_pc1_lenadj        sim_pc1에 같은 보정
  similarity_composite_report.json  선택 지표, PC1 설명 비율·적재, Cronbach alpha, 지표 간 평균 Spearman
  similarity_length_bias.csv        각 지표와 log(자막 단어 수)의 Spearman (전체/매체별)
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.decomposition import PCA

KEYS = ["medium", "work_id", "video_id"]
LEN_COLS = ["transcript_words", "synopsis_words", "n_sentences"]
EXPLICIT = {"lex_tfidf_uni_cos", "lex_tfidf_bi_cos", "lex_word_recall", "lex_entity_recall", "x_rerank_mean", "x_nli_mean"}


def pick_columns(df, max_na):
    cand = []
    for c in df.columns:
        if df[c].dtype == object:
            continue
        ok = ((c.endswith("_cov_mean") and "_end_" not in c and not c.endswith("end_cov_mean") and "median" not in c)
              or c.endswith("_doc_cosine") or c in EXPLICIT)
        if ok and c != "bgem3_tfidf_cosine" and df[c].isna().mean() <= max_na:
            cand.append(c)
    return cand


def zscore(s):
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd > 0 else s * 0.0


def cronbach_alpha(z):
    k = z.shape[1]
    if k < 2:
        return float("nan")
    var_sum = z.var(axis=0, ddof=1).sum()
    tot = z.sum(axis=1).var(ddof=1)
    return float(k / (k - 1) * (1 - var_sum / tot)) if tot > 0 else float("nan")


def length_adjust(df, col):
    """매체별로 col을 log(자막 단어 수), log(시놉시스 단어 수), log(문장 수)에 OLS 회귀한 잔차를 매체 안에서 표준화."""
    out = pd.Series(np.nan, index=df.index)
    for _, g in df.groupby("medium"):
        ok = g[col].notna() & g[LEN_COLS].notna().all(axis=1) & (g[LEN_COLS] > 0).all(axis=1)
        if ok.sum() < 10:
            continue
        X = np.column_stack([np.ones(ok.sum())] + [np.log(g.loc[ok, c].astype(float)) for c in LEN_COLS])
        y = g.loc[ok, col].to_numpy(float)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        r = y - X @ beta
        out.loc[g.index[ok]] = (r - r.mean()) / (r.std(ddof=0) or 1.0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alt", default="data/similarity/results_alt/similarity_alt_results.csv")
    ap.add_argument("--extra", default="data/similarity/results_extra/similarity_extra_results.csv")
    ap.add_argument("--out-dir", default="data/similarity/results_extra")
    ap.add_argument("--max-na", type=float, default=0.15, help="이 비율보다 결측이 많은 지표는 합성에서 제외")
    ap.add_argument("--min-share", type=float, default=0.6, help="sim_z_mean은 선택 지표의 이 비율 이상이 관측된 쌍만 계산")
    a = ap.parse_args()

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(a.alt, encoding="utf-8-sig") if Path(a.alt).exists() else None
    if df is None:
        raise SystemExit(f"ALT 결과가 없음: {a.alt}")
    used = ["alt"]
    if Path(a.extra).exists():
        ex = pd.read_csv(a.extra, encoding="utf-8-sig")
        new = [c for c in ex.columns if c not in df.columns]
        df = df.merge(ex[KEYS + new], on=KEYS, how="left")
        used.append("extra")

    cols = pick_columns(df, a.max_na)
    if len(cols) < 2:
        raise SystemExit(f"합성할 지표가 2개 미만: {cols}")
    Z = pd.DataFrame({c: zscore(df[c]) for c in cols})

    k = len(cols)
    need = math.ceil(a.min_share * k)
    df["n_methods_used"] = Z.notna().sum(axis=1)
    df["sim_z_mean"] = Z.mean(axis=1).where(df["n_methods_used"] >= need)

    comp = Z.dropna()
    pca = PCA(n_components=1).fit(comp)
    load = pca.components_[0]
    if load.mean() < 0:
        load = -load
    df["sim_pc1"] = np.nan
    df.loc[comp.index, "sim_pc1"] = (comp.to_numpy() @ load) / np.sqrt((load ** 2).sum())

    df["sim_z_mean_lenadj"] = length_adjust(df, "sim_z_mean")
    df["sim_pc1_lenadj"] = length_adjust(df, "sim_pc1")

    # 길이 편향
    lw = np.log(df["transcript_words"].astype(float).where(df["transcript_words"] > 0))
    rows = []
    for c in cols + ["sim_z_mean", "sim_z_mean_lenadj"]:
        rec = {"metric": c}
        for name, g in [("all", df)] + list(df.groupby("medium")):
            m = g[c].notna() & lw.loc[g.index].notna()
            rec[f"rho_len_{name}"] = round(float(spearmanr(g.loc[m, c], lw.loc[g.index][m])[0]), 3) if m.sum() > 10 else np.nan
        rows.append(rec)
    pd.DataFrame(rows).to_csv(out / "similarity_length_bias.csv", index=False, encoding="utf-8-sig")

    rho = df[cols].corr(method="spearman")
    off = rho.to_numpy()[~np.eye(k, dtype=bool)]
    report = {
        "inputs": used,
        "n_pairs": int(len(df)),
        "by_medium": df.groupby("medium").size().to_dict(),
        "columns_used": cols,
        "n_complete_pairs_for_pc1": int(len(comp)),
        "n_pairs_with_sim_z_mean": int(df["sim_z_mean"].notna().sum()),
        "pc1_explained_variance_ratio": round(float(pca.explained_variance_ratio_[0]), 3),
        "pc1_loadings": {c: round(float(l), 3) for c, l in zip(cols, load)},
        "cronbach_alpha_z": round(cronbach_alpha(comp), 3),
        "mean_pairwise_spearman": round(float(np.nanmean(off)), 3),
        "min_pairwise_spearman": round(float(np.nanmin(off)), 3),
        "spearman_sim_z_mean_vs_pc1": round(float(df[["sim_z_mean", "sim_pc1"]].corr(method="spearman").iloc[0, 1]), 3),
        "spearman_sim_z_mean_vs_bgem3_cov_mean": (round(float(df[["sim_z_mean", "bgem3_cov_mean"]].corr(method="spearman").iloc[0, 1]), 3)
                                                   if "bgem3_cov_mean" in df else None),
    }
    keep = [c for c in df.columns if c in KEYS + ["work_title", "video_url", "language", "is_auto"] + LEN_COLS]
    df[keep + ["n_methods_used", "sim_z_mean", "sim_pc1", "sim_z_mean_lenadj", "sim_pc1_lenadj"]].to_csv(
        out / "similarity_composite.csv", index=False, encoding="utf-8-sig")
    (out / "similarity_composite_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
