"""
compute_similarity_extra.py
---------------------------
기존 bge-m3(compute_similarity.py)와 ALT(compute_similarity_alt.py: TF-IDF / SBERT / bge-large-en)가
이미 재는 방식과 겹치지 않는 시놉시스-스크립트 유사도 지표를 같은 (작품, t2 영상) 쌍에 대해 추가로 잰다.
(기존 파일은 수정하지 않고 그 함수들을 가져다 쓴다. 새 열은 모두 아래 접두어를 쓴다.)

지표 그룹
  <retriever tag>_*  Qwen3-Embedding (기본 0.6B) 커버리지 -- 기존과 다른 모델 계열, 질의 지시문(instruction) 사용
    doc_cosine / cov_mean / cov_median / cov_ge_<τ> / end_cov_mean / video_align_mean  (정의는 기존과 동일)
  <retriever tag>_order_*  서사 순서 일치도 (시놉시스 문장 순서 vs 영상 안에서 가장 비슷한 위치)
    order_tau   시놉시스 문장 번호와 '가장 비슷한 청크 위치'의 Kendall tau (1에 가까울수록 시놉시스 순서대로 진행)
    mono_ratio  순서를 지키는(비감소) 최적 정렬의 점수 합 / 문장별 최대 점수 합  (1이면 이미 순서대로)
    span        커버리지 상위 절반 문장이 영상 타임라인에서 차지하는 구간 폭 (0~1; 처음부터 끝까지 풀어 주면 1)
  x_rerank_*  리랭커(cross-encoder) 커버리지: 문장별 후보 청크 top-k에 대해 관련도 확률의 최댓값 -> 평균
  x_nli_*     NLI 함의 커버리지: 후보 청크(전제)가 시놉시스 문장(가설)을 함의할 확률의 최댓값 -> 평균

cross-encoder는 쌍마다 비용이 커서, 후보 청크는 retriever 유사도 상위 cross_top_k개만 본다.

사용
  계산(샤드):  python scripts/compute_similarity_extra.py --config config_similarity_extra.yml --shard-count 12 --shard-index 0
  합치기:      python scripts/compute_similarity_extra.py --config config_similarity_extra.yml --merge-from <shards 폴더>
  스모크:      python scripts/compute_similarity_extra.py --config config_similarity_extra.yml --smoke --limit 30
--smoke 는 모델 대신 해싱 벡터와 단어 겹침 점수를 써서 파이프라인만 점검 (논문 결과에 쓰지 말 것).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.stats import kendalltau

from compute_similarity import HashingEncoder, chunk_words, l2norm
from compute_similarity_alt import ID_COLS, build_pairs

TOK = re.compile(r"[a-z']+")


# ----------------------------------------------------------------------------- backends
class QwenRetriever:
    """Qwen3-Embedding: 질의(시놉시스 문장)에만 지시문을 붙이고 문서(자막 청크)에는 붙이지 않는다."""

    def __init__(self, name, device, batch_size, max_len, instruction):
        from sentence_transformers import SentenceTransformer
        try:
            self.m = SentenceTransformer(name, device=device, tokenizer_kwargs={"padding_side": "left"})
        except TypeError:
            self.m = SentenceTransformer(name, device=device)
        self.m.max_seq_length = max_len
        self.bs = batch_size
        self.prompt = f"Instruct: {instruction}\nQuery:"

    def _enc(self, texts, **kw):
        return self.m.encode(list(texts), batch_size=self.bs, normalize_embeddings=True, convert_to_numpy=True,
                             show_progress_bar=len(texts) > 200, **kw)

    def encode_queries(self, texts):
        return self._enc(texts, prompt=self.prompt)

    def encode_docs(self, texts):
        return self._enc(texts)


class SmokeRetriever:
    def __init__(self):
        self.h = HashingEncoder()

    def encode_queries(self, texts):
        return self.h.encode(texts)

    encode_docs = encode_queries


class PairScorer:
    """transformers 시퀀스 분류 모델로 (a, b) 쌍 점수를 낸다. sentence-transformers CrossEncoder API는 버전마다 달라서 직접 쓴다.
    kind='rerank': a=질의(문장), b=문서(청크) -> sigmoid(logit)
    kind='nli'   : a=전제(청크), b=가설(문장)  -> softmax의 entailment 확률"""

    def __init__(self, name, kind, device, max_len, batch_size):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        self.kind, self.max_len, self.bs, self.device = kind, max_len, batch_size, device
        self.tok = AutoTokenizer.from_pretrained(name)
        self.m = AutoModelForSequenceClassification.from_pretrained(name).to(device).eval()
        if kind == "nli":
            id2l = {int(k): str(v).lower() for k, v in self.m.config.id2label.items()}
            ent = [k for k, v in id2l.items() if v.startswith("entail")]
            if not ent:
                raise ValueError(f"{name}: id2label에 entailment가 없음: {id2l}")
            self.ent = ent[0]

    def score(self, a, b):
        torch = self.torch
        order = np.argsort([len(x) + len(y) for x, y in zip(a, b)])  # 길이순으로 묶어 패딩 낭비를 줄인다
        out = np.zeros(len(a), dtype=np.float32)
        for s in range(0, len(order), self.bs):
            idx = order[s:s + self.bs]
            enc = self.tok([a[i] for i in idx], [b[i] for i in idx], truncation="longest_first",
                           max_length=self.max_len, padding=True, return_tensors="pt").to(self.device)
            with torch.no_grad():
                lg = self.m(**enc).logits.float()
            if self.kind == "rerank":
                p = torch.sigmoid(lg[:, 0])
            else:
                p = torch.softmax(lg, dim=-1)[:, self.ent]
            out[idx] = p.cpu().numpy()
            if (s // self.bs) % 50 == 0:
                print(f"[{self.kind}] {min(s + self.bs, len(order))}/{len(order)}", flush=True)
        return out


class SmokeScorer:
    """스모크 전용: 단어 겹침 비율 (의미 점수가 아님)."""

    def score(self, a, b):
        res = []
        for x, y in zip(a, b):
            sx, sy = set(TOK.findall(x.lower())), set(TOK.findall(y.lower()))
            res.append(len(sx & sy) / max(1, len(sx | sy)))
        return np.asarray(res, dtype=np.float32)


# ----------------------------------------------------------------------------- order metrics
def monotone_best(sim):
    """문장 i를 청크 j(i)에 배정하되 j가 비감소가 되도록 하는 최대 점수 합 (동적계획법)."""
    f = sim[0].copy()
    for i in range(1, sim.shape[0]):
        f = sim[i] + np.maximum.accumulate(f)
    return float(f.max())


def order_metrics(sim, top_frac):
    n_s, n_c = sim.shape
    cov = sim.max(axis=1)
    out = dict(order_tau=np.nan, mono_ratio=np.nan, span=np.nan)
    if n_c < 2 or n_s < 4:
        return out
    pos = sim.argmax(axis=1)
    if len(set(pos.tolist())) > 1:
        tau = kendalltau(np.arange(n_s), pos).statistic
        out["order_tau"] = float(tau) if tau == tau else np.nan
    denom = float(cov.sum())
    if denom > 0:
        out["mono_ratio"] = monotone_best(sim) / denom
    k = max(2, int(np.ceil(n_s * top_frac)))
    top = np.argsort(-cov)[:k]
    out["span"] = float((pos[top].max() - pos[top].min()) / (n_c - 1))
    return out


# ----------------------------------------------------------------------------- compute
def compute_shard(pairs, mask, cfg, smoke, want):
    sub = pairs[mask].reset_index(drop=True)
    rc = cfg["retriever"]
    tag = rc["tag"]
    top_k = int(cfg["cross_top_k"])
    taus = rc["coverage_thresholds"]
    ef = cfg["end_fraction"]

    enc = SmokeRetriever() if smoke else QwenRetriever(rc["name"], cfg["device"], cfg["batch_size"], rc["max_len"],
                                                       rc["instruction"])
    vids = sub.drop_duplicates("video_url").set_index("video_url")["text_clean"]
    v_chunks = {u: chunk_words(t, rc["chunk_words"], rc["chunk_overlap"]) for u, t in vids.items()}
    flat = [c for u in vids.index for c in v_chunks[u]]
    print(f"[{tag}] embedding {len(flat)} transcript chunks", flush=True)
    emb = enc.encode_docs(flat)
    v_emb, o = {}, 0
    for u in vids.index:
        n = len(v_chunks[u])
        v_emb[u] = l2norm(emb[o:o + n])
        o += n

    works = sub.drop_duplicates(["medium", "work_id"]).set_index(["medium", "work_id"])
    sents = [s for _, r in works.iterrows() for s in r["sentences"]]
    print(f"[{tag}] embedding {len(sents)} synopsis sentences", flush=True)
    s_all = enc.encode_queries(sents)
    w_emb, o = {}, 0
    for k, r in works.iterrows():
        n = len(r["sentences"])
        w_emb[k] = l2norm(s_all[o:o + n])
        o += n

    rows, jobs = [], []  # jobs: (pair_idx, sent_idx, chunk_idx)
    sims = []
    for p, r in enumerate(sub.itertuples(index=False)):
        ce, se = v_emb[r.video_url], w_emb[(r.medium, r.work_id)]
        sim = se @ ce.T
        sims.append(sim)
        cov = sim.max(axis=1)
        n_end = max(1, int(np.ceil(len(cov) * ef)))
        rec = {
            f"{tag}_doc_cosine": float(l2norm(se.mean(axis=0)) @ l2norm(ce.mean(axis=0))),
            f"{tag}_cov_mean": float(cov.mean()),
            f"{tag}_cov_median": float(np.median(cov)),
            f"{tag}_end_cov_mean": float(cov[-n_end:].mean()),
            f"{tag}_video_align_mean": float(sim.max(axis=0).mean()),
            f"{tag}_n_chunks": int(ce.shape[0]),
        }
        for tau in taus:
            rec[f"{tag}_cov_ge_{str(tau).replace('.', '')}"] = float((cov >= tau).mean())
        for k_, v_ in order_metrics(sim, cfg["order"]["span_top_fraction"]).items():
            rec[f"{tag}_{k_}"] = v_
        rows.append(rec)
        k = min(top_k, sim.shape[1])
        top = np.argsort(-sim, axis=1)[:, :k]
        for i in range(sim.shape[0]):
            for j in top[i]:
                jobs.append((p, i, int(j)))
    res = pd.DataFrame(rows)

    # cross-encoder 계열: 후보 (문장, 청크) 쌍만 점수화
    def cross(kind_key, kind, prefix, name_for_log):
        cc = cfg[kind_key]
        if not cc.get("enabled", True) or (want and kind_key not in want):
            return
        scorer = SmokeScorer() if smoke else PairScorer(cc["name"], kind, cfg["device"], cc["max_len"], cc["batch_size"])
        sent_txt = [sub.at[p, "sentences"][i] for p, i, _ in jobs]
        chunk_txt = [v_chunks[sub.at[p, "video_url"]][j] for p, _, j in jobs]
        print(f"[{name_for_log}] scoring {len(jobs)} (sentence, chunk) candidates", flush=True)
        if kind == "rerank":
            sc = scorer.score(sent_txt, chunk_txt)
        else:
            sc = scorer.score(chunk_txt, sent_txt)
        best = {}
        for (p, i, _), s in zip(jobs, sc):
            best[(p, i)] = max(best.get((p, i), 0.0), float(s))
        col = {f"{prefix}_mean": [], f"{prefix}_median": [], f"{prefix}_ge_05": [], f"{prefix}_end_mean": []}
        for p in range(len(sub)):
            v = np.array([best[(p, i)] for i in range(len(sub.at[p, "sentences"]))])
            n_end = max(1, int(np.ceil(len(v) * ef)))
            col[f"{prefix}_mean"].append(float(v.mean()))
            col[f"{prefix}_median"].append(float(np.median(v)))
            col[f"{prefix}_ge_05"].append(float((v >= 0.5).mean()))
            col[f"{prefix}_end_mean"].append(float(v[-n_end:].mean()))
        for k_, v_ in col.items():
            res[k_] = v_

    cross("reranker", "rerank", "x_rerank", "rerank")
    cross("nli", "nli", "x_nli", "nli")
    return sub[ID_COLS].copy(), res


# ----------------------------------------------------------------------------- merge
def merge_shards(cfg, shard_dir, out_dir, log):
    parts = sorted(Path(shard_dir).glob("shard_*.csv"))
    res = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in parts], ignore_index=True)
    res = res.sort_values(["medium", "work_id", "video_id"]).reset_index(drop=True)
    log.append(f"merged {len(parts)} shards -> {len(res)} pairs")
    res.to_csv(out_dir / "similarity_extra_results.csv", index=False, encoding="utf-8-sig")

    # 상관 비교용으로 ALT(어휘·SBERT·bge-en)와 기존 bge-m3 결과를 같은 쌍에 붙인다 (출력 파일에는 넣지 않음)
    keys = ["medium", "work_id", "video_id"]
    cmp = res.copy()
    alt = cfg.get("alt_results_csv")
    if alt and Path(alt).exists():
        a = pd.read_csv(alt, encoding="utf-8-sig")
        keep = keys + [c for c in a.columns if c.endswith(("_cov_mean", "_doc_cosine")) or c in
                       ("lex_tfidf_uni_cos", "lex_word_recall", "lex_entity_recall", "bgem3_cov_mean")]
        cmp = cmp.merge(a[keep], on=keys, how="left")
        log.append(f"joined ALT results: {int(cmp.filter(like='lex_tfidf_cov_mean').notna().sum().sum())} pairs matched")
    ex = cfg.get("existing_results_csv")
    if ex and Path(ex).exists() and "bgem3_cov_mean" not in cmp:
        e = pd.read_csv(ex, encoding="utf-8-sig")[keys + ["cov_mean"]].rename(columns={"cov_mean": "bgem3_cov_mean"})
        cmp = cmp.merge(e, on=keys, how="left")

    tag = cfg["retriever"]["tag"]
    main_extra = [c for c in res.columns if c in (f"{tag}_cov_mean", f"{tag}_doc_cosine", f"{tag}_order_tau",
                                                  f"{tag}_mono_ratio", f"{tag}_span", "x_rerank_mean", "x_nli_mean")]
    others = [c for c in cmp.columns if c not in res.columns]
    cols = main_extra + others
    corr = cmp[cols].corr(method="spearman").round(3)
    corr.to_csv(out_dir / "similarity_extra_spearman.csv", encoding="utf-8-sig")
    for m, g in cmp.groupby("medium"):
        g[cols].corr(method="spearman").round(3).to_csv(out_dir / f"similarity_extra_spearman_{m}.csv", encoding="utf-8-sig")

    num = [c for c in res.columns if c not in ID_COLS and res[c].dtype != object]
    summary = {
        "n_pairs": int(len(res)), "n_videos": int(res.video_url.nunique()),
        "by_medium": res.groupby("medium").size().to_dict(),
        "log": log,
        "spearman_vs_bgem3_cov_mean": (corr["bgem3_cov_mean"].drop("bgem3_cov_mean").dropna().to_dict()
                                       if "bgem3_cov_mean" in corr else None),
        "describe": res[num].describe().round(4).to_dict(),
    }
    (out_dir / "similarity_extra_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
    print("\n".join(log))
    if summary["spearman_vs_bgem3_cov_mean"]:
        print("Spearman vs bge-m3 cov_mean:")
        print(json.dumps(summary["spearman_vs_bgem3_cov_mean"], indent=1))


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_similarity_extra.yml")
    ap.add_argument("--only", default=None, help="콤마로 구분: reranker,nli (retriever/order 지표는 항상 계산). 기본=설정대로 전부")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--merge-from", default=None)
    a = ap.parse_args()

    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    out_dir = Path(a.out_dir or cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []

    if a.merge_from:
        merge_shards(cfg, a.merge_from, out_dir, log)
        return

    pairs = build_pairs(cfg, log)
    if a.limit:
        pairs = pairs.head(a.limit).reset_index(drop=True)
    uv = sorted(pairs["video_url"].unique())
    mine = set(uv[a.shard_index::a.shard_count])
    mask = pairs["video_url"].isin(mine).to_numpy()
    log.append(f"shard {a.shard_index}/{a.shard_count}: {int(mask.sum())} pairs")

    want = set(a.only.split(",")) if a.only else None
    ids, res = compute_shard(pairs, mask, cfg, a.smoke, want)
    out = pd.concat([ids.reset_index(drop=True), res], axis=1)
    sd = out_dir / "shards"
    sd.mkdir(parents=True, exist_ok=True)
    out.to_csv(sd / f"shard_{a.shard_index}.csv", index=False, encoding="utf-8-sig")
    print("\n".join(log))
    print(f"shard {a.shard_index} done: {len(out)} pairs, {out.shape[1]} columns")


if __name__ == "__main__":
    sys.exit(main())
