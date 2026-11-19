"""
run_h2_regressions.py
---------------------
유사도 지표를 합성하지 않고, **지표마다 따로** 패널에 붙여 H2(Synopsis Similarity 조절효과) 회귀를 돌린다.

설정 (기존 R 슬라이드 식(2)와 동일하게 맞춤)
  종속: log(총수입 + 1)   [Hollywood: Gross, Broadway: weekly_gross]
  처치: is_condensed (t2: 2주 전까지 요약콘텐츠 존재)
  통제: log(평균 티켓가격), log(규모) [Hollywood: Theaters, Broadway: seats_in_theatre], log(경과주차)
  고정효과: 작품 + 연 + 월 + 주(week_num), 표준오차는 작품 단위 클러스터
  조절변수 x = is_condensed * z(지표)   (영상이 없는 주는 0; z는 요약콘텐츠 주 표본에서 표준화 -> 계수 = 1 표준편차 효과)
  표본: 요약콘텐츠 주 중 해당 지표 값이 있는 주 + 요약콘텐츠가 없는 주 (기존 R의 spoiler 표본 처리와 같음)

지표마다 3개 모형
  (A) 처치만            : 같은 표본에서 처치 효과 (비교 기준)
  (B) + x                : 지표 조절효과  <- H2 검증
  (C) + x + 길이 통제    : x에 더해 is_condensed * z(log 자막 단어 수)  (자막 길이 편향 점검)

사용:  python scripts/run_h2_regressions.py [--out-dir data/similarity/regressions]
"""
import argparse
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

PANELS = {
    "hollywood": dict(path="data/similarity/panel_current/hollywood_v13.xlsx", sheet="Sheet1", work="movie_id",
                      gross="Gross", size="Theaters"),
    "broadway": dict(path="data/similarity/panel_current/broadway.xlsx", sheet=0, work="show_id",
                     gross="weekly_gross", size="seats_in_theatre"),
}
RESULT_FILES = [
    ("data/similarity/results_alt/similarity_alt_results.csv", None),
    ("data/similarity/results_extra/similarity_extra_results.csv", None),
]
SKIP = re.compile(r"(cov_ge|^n_|_n_|transcript_words|synopsis_words|n_sentences|work_id|^lex_n_entities)")


def load_panel(medium):
    c = PANELS[medium]
    d = pd.read_excel(c["path"], sheet_name=c["sheet"])
    d["work"] = d[c["work"]].astype(int)
    d["lgross"] = np.log(d[c["gross"]].astype(float) + 1)
    d["lprice"] = np.log(d["avg_ticket_price"].where(d["avg_ticket_price"] > 0))
    d["lsize"] = np.log(pd.to_numeric(d[c["size"]], errors="coerce").where(lambda s: s > 0))
    if medium == "broadway":
        # 첫 프리뷰 주 = 1, 2, 3 ... (음수 없음)
        we, fp = pd.to_datetime(d["week_ending"]), pd.to_datetime(d["first_preview"], errors="coerce")
        w = ((we - fp).dt.days // 7 + 1).clip(lower=1)
        d["weeks"] = w.where(fp.notna(), d["weeks_open"].where(d["weeks_open"] >= 1))
    else:
        d["weeks"] = d["weeks_open"].where(d["weeks_open"] >= 1)
    d["lweeks"] = np.log(d["weeks"])
    d["cc"] = d["is_condensed"].astype(int)
    d["year"], d["month"], d["week"] = d["year_num"], d["month_num"], d["week_num"]
    d["video_id"] = d["t2_video_id"].astype(str)
    return d


def load_measures():
    frames = []
    for path, _ in RESULT_FILES:
        if Path(path).exists():
            r = pd.read_csv(path, encoding="utf-8-sig")
            frames.append(r)
    base = frames[0]
    for r in frames[1:]:
        extra = [c for c in r.columns if c not in base.columns] + ["medium", "work_id", "video_id"]
        base = base.merge(r[extra], on=["medium", "work_id", "video_id"], how="outer")
    cols = [c for c in base.columns
            if base[c].dtype.kind == "f" and not SKIP.search(c)
            and c not in ("language",)]
    return base, cols


def fit(df, rhs, extra_terms):
    import pyfixest as pf
    f = f"lgross ~ cc + lprice + lsize + lweeks{extra_terms} | work + year + month + week"
    return pf.feols(f, data=df, vcov={"CRV1": "work"})


def row(model, term):
    t = model.tidy()
    if term not in t.index:
        return (np.nan, np.nan, np.nan)
    r = t.loc[term]
    return float(r["Estimate"]), float(r["Std. Error"]), float(r["Pr(>|t|)"])


def stars(p):
    return "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/similarity/regressions")
    ap.add_argument("--min-words", type=int, default=0, help="요약콘텐츠 주 중 자막 단어 수가 이 값 미만인 쌍은 제외")
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    res, mcols = load_measures()
    print(f"measures: {len(mcols)}")
    rows = []
    for medium in PANELS:
        panel = load_panel(medium)
        r = res[res["medium"] == medium].drop_duplicates(["work_id", "video_id"])
        wcols = ["work_id", "video_id", "transcript_words"] + [c for c in mcols if c in r.columns]
        m = panel.merge(r[wcols], left_on=["work", "video_id"], right_on=["work_id", "video_id"], how="left")
        # 패널의 기존 similarity 열(기존 bge-m3 결과)도 같은 틀로 하나 더 돌려 재현 확인
        m["panel_similarity"] = m["similarity"]
        measures = ["panel_similarity"] + [c for c in mcols if c in m.columns and m[c].notna().any()]
        for meas in measures:
            d = m.copy()
            if a.min_words:
                d.loc[(d.cc == 1) & (d["transcript_words"] < a.min_words), meas] = np.nan
            d.loc[d.cc == 0, meas] = np.nan
            ok = (d.cc == 0) | d[meas].notna()
            d = d[ok & d[["lgross", "lprice", "lsize", "lweeks"]].notna().all(axis=1)].copy()
            tv = d.loc[d.cc == 1, meas]
            if tv.nunique() < 3 or d.loc[d.cc == 1, "work"].nunique() < 5:
                continue
            d["x"] = np.where(d.cc == 1, (d[meas] - tv.mean()) / tv.std(), 0.0)
            lw = np.log(d["transcript_words"].where(d["transcript_words"] > 0))
            lwz = (lw - lw[d.cc == 1].mean()) / lw[d.cc == 1].std()
            d["len_x"] = np.where(d.cc == 1, lwz, 0.0).astype(float)
            d["len_x"] = d["len_x"].fillna(0.0)
            try:
                mA = fit(d, "", "")
                mB = fit(d, "", " + x")
                mC = fit(d, "", " + x + len_x")
            except Exception as e:  # noqa
                print(f"skip {medium} {meas}: {e}")
                continue
            cA, cB, cC = row(mA, "cc"), row(mB, "cc"), row(mC, "cc")
            xB, xC, lC = row(mB, "x"), row(mC, "x"), row(mC, "len_x")
            rows.append(dict(
                medium=medium, measure=meas, n_rows=int(mB._N), n_cc_rows=int((d.cc == 1).sum()),
                n_pairs=int(d.loc[d.cc == 1, ["work", "video_id"]].drop_duplicates().shape[0]),
                cc_A=cA[0], cc_A_p=cA[2],
                cc_B=cB[0], cc_B_p=cB[2], x_B=xB[0], x_B_se=xB[1], x_B_p=xB[2],
                x_C=xC[0], x_C_se=xC[1], x_C_p=xC[2], len_C=lC[0], len_C_p=lC[2],
            ))
            print(f"{medium:9s} {meas:32s} x={xB[0]:+.4f}{stars(xB[2]):3s} (p={xB[2]:.3f})  "
                  f"x|len={xC[0]:+.4f}{stars(xC[2]):3s}  n_pairs={rows[-1]['n_pairs']}", flush=True)

    R = pd.DataFrame(rows)
    R.to_csv(out / "h2_similarity_by_measure.csv", index=False, encoding="utf-8-sig")

    # 보기 쉬운 표
    def fmt(c, p):
        return "" if pd.isna(c) else f"{c:+.3f}{stars(p)}"
    T = R.copy()
    T["x (B)"] = [fmt(c, p) for c, p in zip(R.x_B, R.x_B_p)]
    T["x (C: 길이통제)"] = [fmt(c, p) for c, p in zip(R.x_C, R.x_C_p)]
    T["길이항 (C)"] = [fmt(c, p) for c, p in zip(R.len_C, R.len_C_p)]
    T["처치 cc (B)"] = [fmt(c, p) for c, p in zip(R.cc_B, R.cc_B_p)]
    cols = ["measure", "n_pairs", "n_rows", "처치 cc (B)", "x (B)", "x (C: 길이통제)", "길이항 (C)"]
    with open(out / "h2_similarity_by_measure.md", "w", encoding="utf-8") as f:
        f.write("# H2 (Synopsis Similarity) 회귀: 지표별 (합성 없음)\n\n")
        f.write("종속 log(Gross+1) · 통제 log(가격), log(규모), log(경과주차) · FE 작품+연+월+주 · 작품 클러스터. "
                "x = is_condensed × z(지표); 계수는 지표 1 SD 변화당 효과. *** p<0.01, ** p<0.05, * p<0.1\n\n")
        for medium in PANELS:
            t = T[T.medium == medium][cols]
            f.write(f"## {medium}\n\n{t.to_markdown(index=False)}\n\n")
    print("saved", out)


if __name__ == "__main__":
    sys.exit(main())
