"""
compute_similarity_alt.py
-------------------------
기존 compute_similarity.py(bge-m3)와 같은 (작품, t2 영상) 쌍에 대해 다른 방식으로 시놉시스-스크립트 유사도를 다시 잰다.
(기존 파일은 수정하지 않고 그 함수들을 가져다 쓴다.)

지표 그룹
  lex_*   어휘 기반 (모델 불필요)
    lex_tfidf_uni_cos    TF-IDF 코사인, 단어 1-gram (기존 tfidf_cosine과 같은 설정)
    lex_tfidf_bi_cos     TF-IDF 코사인, 1~2-gram
    lex_tfidf_cov_mean   시놉시스 각 문장 -> 스크립트 청크 최대 TF-IDF 코사인의 평균 (cov_mean의 어휘 버전)
    lex_tfidf_cov_ge_*   위 최대값이 임계값 이상인 문장 비율
    lex_word_recall      시놉시스 내용어(불용어 제외 서로 다른 단어) 중 스크립트에 등장하는 비율
    lex_entity_recall    시놉시스의 고유명사(인물·지명 등 대문자 단어) 중 스크립트에 등장하는 비율
  <tag>_* 임베딩 모델별 (config의 models: sbert_mpnet, sbert_minilm, bge_large_en ...)
    doc_cosine           시놉시스 문장 임베딩 평균 vs 스크립트 청크 임베딩 평균
    cov_mean             시놉시스 각 문장 -> 청크 최대 코사인의 평균 (기존 similarity와 같은 정의)
    cov_median           위 문장별 최대값의 중앙값 (일부 문장에 치우친 영향 확인용)
    cov_ge_<τ>           최대 코사인 >= τ 인 문장 비율 (플롯 포인트 커버리지)
    end_cov_mean         시놉시스 마지막 end_fraction 문장들의 커버리지 (결말 노출도)
    video_align_mean     스크립트 청크 -> 시놉시스 문장 최대 코사인의 평균

사용
  계산(샤드):  python scripts/compute_similarity_alt.py --config config_similarity_alt.yml --shard-count 12 --shard-index 0
  합치기:      python scripts/compute_similarity_alt.py --config config_similarity_alt.yml --merge-from <shards 폴더>
  스모크:      python scripts/compute_similarity_alt.py --config config_similarity_alt.yml --smoke --limit 30
--smoke 는 임베딩 모델 대신 해싱 벡터를 써서 파이프라인만 점검 (논문 결과에 쓰지 말 것).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from compute_similarity import (  # 같은 폴더의 기존 스크립트 재사용
    HashingEncoder, STEncoder, chunk_words, extract_video_id, l2norm,
    load_panel_pairs, load_synopses, load_transcripts, split_sentences,
)

TOKEN = r"(?u)\b[a-zA-Z][a-zA-Z']+\b"
ID_COLS = ["medium", "work_id", "work_title", "video_id", "video_url", "video_title", "language", "is_auto",
           "transcript_words", "synopsis_words", "n_sentences", "synopsis_is_lead", "match_confidence", "needs_review"]


# ----------------------------------------------------------------------------- pairs
def build_pairs(cfg, log):
    trans, excluded = load_transcripts(cfg["transcripts_xlsx"], cfg, log)
    syn = load_synopses(cfg.get("synopses_hollywood_xlsx"), cfg.get("synopses_broadway_xlsx"))
    vmap = load_panel_pairs(cfg)
    log.append(f"panel t2 pairs: {len(vmap)} ({vmap.groupby('medium').size().to_dict()})")
    syn["work_id"] = syn["work_id"].astype(int)

    pairs = vmap.merge(trans[["video_id", "video_url", "title", "language", "is_auto", "text_clean", "transcript_words"]]
                       .rename(columns={"title": "video_title"}), on="video_id", how="inner")
    n_tr = len(pairs)
    pairs = pairs.merge(syn.rename(columns={"title": "work_title_syn"}), on=["medium", "work_id"], how="left")
    bad = pairs["synopsis_words"].isna() | (pairs["synopsis_words"] < cfg["min_synopsis_words"])
    log.append(f"pairs with usable transcript: {n_tr}; dropped for missing/short synopsis: {int(bad.sum())}")
    pairs = pairs[~bad].reset_index(drop=True)
    pairs["sentences"] = pairs["synopsis"].map(lambda s: split_sentences(s, cfg["sentence_min_words"]))
    pairs["n_sentences"] = pairs["sentences"].map(len)
    log.append(f"pairs to compute: {len(pairs)} ({pairs.groupby('medium').size().to_dict()}) | "
               f"unique videos: {pairs.video_url.nunique()}")
    return pairs


# ----------------------------------------------------------------------------- lexical
def lexical_metrics(pairs, cfg, shard_mask):
    """TF-IDF는 전 쌍을 기준으로 적합해야 샤드 간 값이 같다. 점수는 shard_mask 쌍만 계산."""
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS as SW, TfidfVectorizer
    lc = cfg["lexical"]
    taus = lc["coverage_thresholds"]

    vids = pairs.drop_duplicates("video_url").set_index("video_url")["text_clean"]
    works = pairs.drop_duplicates(["medium", "work_id"]).set_index(["medium", "work_id"])

    # 문서 수준
    corpus = list(vids.values) + list(works["synopsis"].values)
    v_row = {u: i for i, u in enumerate(vids.index)}
    w_row = {k: len(vids) + i for i, k in enumerate(works.index)}
    X1 = TfidfVectorizer(stop_words="english", sublinear_tf=True, min_df=2, max_df=0.9, token_pattern=TOKEN).fit_transform(corpus)
    X2 = TfidfVectorizer(stop_words="english", sublinear_tf=True, min_df=2, max_df=0.9, ngram_range=(1, 2),
                         token_pattern=TOKEN).fit_transform(corpus)

    # 문장-청크 수준
    v_chunks = {u: chunk_words(t, lc["chunk_words"], lc["chunk_overlap"]) for u, t in vids.items()}
    w_sents = {k: r["sentences"] for k, r in works.iterrows()}
    all_txt = [c for cs in v_chunks.values() for c in cs] + [s for ss in w_sents.values() for s in ss]
    tfc = TfidfVectorizer(stop_words="english", sublinear_tf=True, min_df=2, max_df=0.9, token_pattern=TOKEN).fit(all_txt)
    v_mat = {u: tfc.transform(cs) for u, cs in v_chunks.items()}
    w_mat = {k: tfc.transform(ss) for k, ss in w_sents.items()}

    tok = re.compile(TOKEN)

    def content_words(text):
        return {w for w in (x.lower() for x in tok.findall(text)) if w not in SW and len(w) > 2}

    def entities(text):
        out = set()
        for s in split_sentences(text, 1):
            ws = tok.findall(s)
            for w in ws[1:]:  # 문장 첫 단어는 제외 (일반 단어가 대문자인 경우)
                if w[0].isupper() and len(w) > 2 and w.lower() not in SW:
                    out.add(w.lower())
        return out

    rows = []
    for i, r in enumerate(pairs.itertuples(index=False)):
        if not shard_mask[i]:
            continue
        key = (r.medium, r.work_id)
        sim = (w_mat[key] @ v_mat[r.video_url].T).toarray()
        cov = sim.max(axis=1)
        n_end = max(1, int(np.ceil(len(cov) * cfg["end_fraction"])))
        vt = {w.lower() for w in tok.findall(r.text_clean)}
        sw_, ent = content_words(r.synopsis), entities(r.synopsis)
        rec = dict(
            lex_tfidf_uni_cos=float(X1[v_row[r.video_url]].multiply(X1[w_row[key]]).sum()),
            lex_tfidf_bi_cos=float(X2[v_row[r.video_url]].multiply(X2[w_row[key]]).sum()),
            lex_tfidf_cov_mean=float(cov.mean()),
            lex_tfidf_end_cov_mean=float(cov[-n_end:].mean()),
            lex_word_recall=float(len(sw_ & vt) / len(sw_)) if sw_ else np.nan,
            lex_entity_recall=float(len(ent & vt) / len(ent)) if ent else np.nan,
            lex_n_entities=len(ent),
        )
        for tau in taus:
            rec[f"lex_tfidf_cov_ge_{str(tau).replace('.', '')}"] = float((cov >= tau).mean())
        rows.append(rec)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- embeddings
def embedding_metrics(pairs, mcfg, cfg, shard_mask, smoke):
    tag = mcfg["tag"]
    enc = HashingEncoder() if smoke else STEncoder(mcfg["name"], cfg["device"], cfg["batch_size"], mcfg["max_len"])
    sub = pairs[shard_mask].reset_index(drop=True)

    vids = sub.drop_duplicates("video_url").set_index("video_url")["text_clean"]
    chunks, owner = [], []
    for u, t in vids.items():
        cs = chunk_words(t, mcfg["chunk_words"], mcfg["chunk_overlap"])
        chunks.extend(cs)
        owner.extend([u] * len(cs))
    print(f"[{tag}] embedding {len(chunks)} transcript chunks", flush=True)
    emb = enc.encode(chunks)
    owner = np.array(owner)
    v_emb = {u: l2norm(emb[owner == u]) for u in vids.index}

    works = sub.drop_duplicates(["medium", "work_id"]).set_index(["medium", "work_id"])
    sents, s_owner = [], []
    for k, r in works.iterrows():
        sents.extend(r["sentences"])
        s_owner.extend([k] * len(r["sentences"]))
    print(f"[{tag}] embedding {len(sents)} synopsis sentences", flush=True)
    s_all = enc.encode(sents)
    s_owner_idx = {}
    for i, k in enumerate(s_owner):
        s_owner_idx.setdefault(k, []).append(i)
    w_emb = {k: l2norm(s_all[idx]) for k, idx in s_owner_idx.items()}

    taus = mcfg["coverage_thresholds"]
    rows = []
    for r in sub.itertuples(index=False):
        ce, se = v_emb[r.video_url], w_emb[(r.medium, r.work_id)]
        sim = se @ ce.T
        cov = sim.max(axis=1)
        n_end = max(1, int(np.ceil(len(cov) * cfg["end_fraction"])))
        rec = {
            f"{tag}_doc_cosine": float(l2norm(se.mean(axis=0)) @ l2norm(ce.mean(axis=0))),
            f"{tag}_cov_mean": float(cov.mean()),
            f"{tag}_cov_median": float(np.median(cov)),
            f"{tag}_end_cov_mean": float(cov[-n_end:].mean()),
            f"{tag}_video_align_mean": float(sim.max(axis=0).mean()),
        }
        for tau in taus:
            rec[f"{tag}_cov_ge_{str(tau).replace('.', '')}"] = float((cov >= tau).mean())
        rows.append(rec)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- merge
def merge_shards(cfg, shard_dir, out_dir, log):
    parts = sorted(Path(shard_dir).glob("shard_*.csv"))
    res = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in parts], ignore_index=True)
    res = res.sort_values(["medium", "work_id", "video_id"]).reset_index(drop=True)
    log.append(f"merged {len(parts)} shards -> {len(res)} pairs")

    # 기존 bge-m3 결과를 같은 쌍에 붙여 비교
    ex_path = cfg.get("existing_results_csv")
    if ex_path and Path(ex_path).exists():
        ex = pd.read_csv(ex_path, encoding="utf-8-sig")
        keep = ["medium", "work_id", "video_id", "doc_cosine", "cov_mean", "end_cov_mean", "video_align_mean", "tfidf_cosine"]
        ex = ex[keep].rename(columns={c: f"bgem3_{c}" for c in keep[3:]})
        res = res.merge(ex, on=["medium", "work_id", "video_id"], how="left")
        log.append(f"joined existing bge-m3 results: {int(res['bgem3_cov_mean'].notna().sum())} pairs matched")

    res.to_csv(out_dir / "similarity_alt_results.csv", index=False, encoding="utf-8-sig")

    main_cols = [c for c in res.columns if c.endswith(("_cov_mean", "_doc_cosine"))
                 or c in ("lex_tfidf_uni_cos", "lex_tfidf_bi_cos", "lex_word_recall", "lex_entity_recall", "bgem3_tfidf_cosine")]
    corr = res[main_cols].corr(method="spearman").round(3)
    corr.to_csv(out_dir / "similarity_alt_spearman.csv", encoding="utf-8-sig")
    for m, g in res.groupby("medium"):
        g[main_cols].corr(method="spearman").round(3).to_csv(out_dir / f"similarity_alt_spearman_{m}.csv", encoding="utf-8-sig")

    desc = res[[c for c in res.columns if c not in ID_COLS and res[c].dtype != object]].describe().round(4)
    summary = {
        "n_pairs": int(len(res)), "n_videos": int(res.video_url.nunique()),
        "by_medium": res.groupby("medium").size().to_dict(),
        "log": log,
        "spearman_vs_bgem3_cov_mean": (corr["bgem3_cov_mean"].drop("bgem3_cov_mean").to_dict()
                                       if "bgem3_cov_mean" in corr else None),
        "describe": desc.to_dict(),
    }
    (out_dir / "similarity_alt_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(log))
    if summary["spearman_vs_bgem3_cov_mean"]:
        print("Spearman vs bge-m3 cov_mean:")
        print(json.dumps(summary["spearman_vs_bgem3_cov_mean"], indent=1))


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_similarity_alt.yml")
    ap.add_argument("--only", default=None, help="콤마로 구분한 태그만 계산 (예: lex,sbert_mpnet). 기본=전부")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--smoke", action="store_true", help="임베딩 모델 대신 해싱 벡터 (파이프라인 점검용)")
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
    base = pairs[mask].reset_index(drop=True)
    base = base[ID_COLS].copy()
    parts = [base]
    if want is None or "lex" in want:
        parts.append(lexical_metrics(pairs, cfg, mask))
    for m in cfg["models"]:
        if want is None or m["tag"] in want:
            parts.append(embedding_metrics(pairs, m, cfg, mask, a.smoke))
    res = pd.concat(parts, axis=1)

    sd = out_dir / "shards"
    sd.mkdir(parents=True, exist_ok=True)
    res.to_csv(sd / f"shard_{a.shard_index}.csv", index=False, encoding="utf-8-sig")
    print("\n".join(log))
    print(f"shard {a.shard_index} done: {len(res)} pairs, {res.shape[1]} columns")


if __name__ == "__main__":
    sys.exit(main())
