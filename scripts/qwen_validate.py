"""
qwen_validate.py   (GPU 필요: Colab T4)
--------------------------------------
Qwen 플롯포인트 판정기가 믿을 만한지 확인하는 검증 스크립트.
쌍(작품, 영상)을 자막 길이별로 골고루 N개 뽑아 세 조건을 판정하고, 문장별 상세 기록을 남긴다.

  real : 실제 자막 근거 (기존 judge와 동일한 프롬프트·근거 선택)
  pos  : 양성 대조 - 근거를 '그 문장 자신'으로 준다.  Yes 확률이 1에 가까워야 정상
  neg  : 음성 대조 - 근거를 다른 작품 영상의 자막에서 뽑는다.  0에 가까워야 정상

산출물 (out-dir)
  validate_summary.csv   쌍별 pc_mean (real/pos/neg) + 자막 단어 수
  validate_detail.jsonl  문장별 확률과 실제로 준 근거 (1단계 원인 분석용)

사용:  python scripts/qwen_validate.py --n 50 --out-dir out_val
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_similarity import chunk_words, clean_transcript, split_sentences  # noqa: E402
from qwen_vl_plot_coverage import MODEL, load_model, yes_prob  # noqa: E402


def build_prompt(sent, evidence):
    return (f"Plot event from the official synopsis:\n\"{sent}\"\n\n"
            f"Excerpts from a video (speech transcript and scene descriptions):\n{evidence}\n\n"
            "Does the video reveal or clearly describe this plot event? Answer only Yes or No.")


def top_evidence(sents, ev, k):
    from sklearn.feature_extraction.text import TfidfVectorizer
    tf = TfidfVectorizer(stop_words="english", sublinear_tf=True).fit(ev + sents)
    sim = (tf.transform(sents) @ tf.transform(ev).T).toarray()
    return ["\n".join(f"- {ev[j]}" for j in sorted(np.argsort(-sim[i])[:k])) for i in range(len(sents))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="data/similarity/results_alt/similarity_alt_results.csv")
    ap.add_argument("--transcripts", default="data/similarity/youtube_transcripts_all_v13.csv")
    ap.add_argument("--synopses-hollywood", default="data/similarity/synopses_hollywood.xlsx")
    ap.add_argument("--synopses-broadway", default="data/similarity/synopses_broadway.xlsx")
    ap.add_argument("--out-dir", default="out_val")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--max-sents", type=int, default=25, help="쌍당 최대 문장 수 (시간 제한용; 앞/뒤 균등 추출)")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    pairs = pd.read_csv(a.pairs, encoding="utf-8-sig").drop_duplicates(["medium", "work_id", "video_id"])
    tr = pd.read_csv(a.transcripts)
    tr["video_id"] = tr["video_url"].astype(str).str.extract(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})")[0]
    tr = tr.drop_duplicates("video_id").set_index("video_id")
    syn = {}
    for path, med in ((a.synopses_hollywood, "hollywood"), (a.synopses_broadway, "broadway")):
        if Path(path).exists():
            s = pd.read_excel(path, sheet_name="synopses") if med == "hollywood" else pd.read_excel(path)
            c = [x for x in s.columns if str(x).startswith("synopsis")][0]
            s = s.rename(columns={c: "synopsis", "show_id": "work_id"})
            for r in s.itertuples(index=False):
                syn[(med, r.work_id)] = r.synopsis
    pairs = pairs[pairs.video_id.isin(tr.index) & pairs.apply(lambda r: (r.medium, r.work_id) in syn, axis=1)]

    # 자막 길이 5분위에서 균등 추출 (Broadway/Hollywood 섞어서)
    rng = np.random.default_rng(a.seed)
    pairs = pairs.assign(q=pd.qcut(pairs.transcript_words.rank(method="first"), 5, labels=False))
    pick = pd.concat([g.sample(min(len(g), a.n // 5), random_state=a.seed) for _, g in pairs.groupby("q")])
    print(f"selected {len(pick)} pairs; words quantiles:", pick.transcript_words.describe().round(0).to_dict(), flush=True)

    model, proc = load_model(a.model)
    all_ids = list(tr.index)
    rows, det = [], open(out / "validate_detail.jsonl", "w", encoding="utf-8")
    for n, r in enumerate(pick.itertuples(index=False), 1):
        sents = split_sentences(syn[(r.medium, r.work_id)], 6)
        if len(sents) > a.max_sents:
            sents = [sents[i] for i in np.unique(np.linspace(0, len(sents) - 1, a.max_sents).astype(int))]
        ev = chunk_words(clean_transcript(tr.loc[r.video_id, "text"]), 120, 20)
        if not ev or not sents:
            continue
        # 음성 대조용: 다른 작품의 영상 자막 (자막이 충분히 긴 것)
        other = None
        for oid in rng.permutation(all_ids)[:200]:
            o = pairs[pairs.video_id == oid]
            if len(o) and (o.iloc[0].medium, o.iloc[0].work_id) != (r.medium, r.work_id) and o.iloc[0].transcript_words >= 150:
                other = chunk_words(clean_transcript(tr.loc[oid, "text"]), 120, 20)
                break
        real_ev = top_evidence(sents, ev, a.top_k)
        neg_ev = top_evidence(sents, other, a.top_k) if other else [None] * len(sents)
        res = {"real": [], "pos": [], "neg": []}
        for i, s in enumerate(sents):
            p_real = yes_prob(model, proc, build_prompt(s, real_ev[i]))
            p_pos = yes_prob(model, proc, build_prompt(s, f"- {s}"))
            p_neg = yes_prob(model, proc, build_prompt(s, neg_ev[i])) if neg_ev[i] else np.nan
            res["real"].append(p_real); res["pos"].append(p_pos); res["neg"].append(p_neg)
            det.write(json.dumps(dict(medium=r.medium, work_id=int(r.work_id), video_id=r.video_id, sent=s,
                                      evidence=real_ev[i], p_real=p_real, p_pos=p_pos, p_neg=p_neg), ensure_ascii=False) + "\n")
        det.flush()
        rows.append(dict(medium=r.medium, work_id=r.work_id, video_id=r.video_id, transcript_words=r.transcript_words,
                         n_sents=len(sents), real=np.mean(res["real"]), pos=np.mean(res["pos"]), neg=np.nanmean(res["neg"])))
        print(f"[{n}/{len(pick)}] {r.medium} words={r.transcript_words:.0f} real={rows[-1]['real']:.3f} "
              f"pos={rows[-1]['pos']:.3f} neg={rows[-1]['neg']:.3f}", flush=True)
        pd.DataFrame(rows).to_csv(out / "validate_summary.csv", index=False, encoding="utf-8-sig")
    det.close()
    print("done ->", out)


if __name__ == "__main__":
    main()
