"""
topic_model_comments.py
========================
condensed 영상 댓글(data/condensed_comments_merged.csv)에서 BERTopic으로 주요 토픽을 뽑는다.

*** 설계 ***
- 매체(Broadway / Hollywood)별로 모델을 따로 만든다. 규모(7천 vs 97만)와 말투가 달라서
  한 모델로 합치면 Hollywood 토픽이 Broadway를 덮어버림.
- 전처리: URL/타임스탬프/멘션 제거 -> 5단어 이상 -> 영어(ASCII 비율 + 영어 불용어 비율
  휴리스틱, 로마자 힌디어 등 제외) -> 매체 내 완전 동일 텍스트 중복 제거.
- 임베딩은 전체 댓글에 대해 한 번 계산(SentenceTransformer all-MiniLM-L6-v2).
  UMAP+HDBSCAN은 비용이 커서 "표본"으로만 학습(영상당 --cap-per-video 개로 제한해
  초대형 영상 하나가 토픽을 독점하지 않게 한 뒤 --fit-n 개 무작위 추출)하고, 나머지는
  학습된 모델로 transform해서 토픽을 배정한다.
- 영화/공연 단위로 뭉치는 토픽(특정 작품 이야기)과 작품을 가로지르는 일반 토픽을
  구분하도록 토픽별 작품 집중도(top_work_share, n_works)를 같이 계산한다.

출력(--out-dir): topics_<medium>.csv, assignments_<medium>.csv, summary_<medium>.md,
                 prep_stats_<medium>.json

Usage:
    python scripts/topic_model_comments.py --medium broadway --fit-n 100000 \
        --min-topic-size 15 --nr-topics 12
    python scripts/topic_model_comments.py --medium hollywood --fit-n 150000 \
        --min-topic-size 150 --nr-topics 40
"""
import argparse
import json
import os
import re
import time

import numpy as np
import pandas as pd

EN_STOP = set(
    "the a an and or but if of to in on at for with is are was were be been being it its this "
    "that these those i you he she we they me my your his her our their them him do does did "
    "have has had not no so as by from about just very really can will would could should what "
    "which who when where why how there here all more most some any too than then also".split()
)
EXTRA_STOP = [
    "movie", "movies", "film", "films", "like", "just", "really", "know", "think", "don", "didn",
    "doesn", "ve", "ll", "im", "ive", "thats", "dont", "cant", "youtube", "video", "videos",
    "watch", "watching", "watched", "scene", "scenes", "thing", "things", "ever", "got", "get",
    "gonna", "one", "even", "much", "make", "made", "still", "see", "going", "way", "lot",
]

URL_RE = re.compile(r"https?://\S+|www\.\S+")
TS_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
MENTION_RE = re.compile(r"@\S+")
REPEAT_RE = re.compile(r"(.)\1{3,}")
TOKEN_RE = re.compile(r"[a-z']+")


def clean_text(s):
    s = URL_RE.sub(" ", s)
    s = TS_RE.sub(" ", s)
    s = MENTION_RE.sub(" ", s)
    s = REPEAT_RE.sub(r"\1\1\1", s)
    return re.sub(r"\s+", " ", s).strip()


def is_english(s):
    if not s:
        return False
    ascii_ratio = sum(ch.isascii() for ch in s) / len(s)
    if ascii_ratio < 0.9:
        return False
    toks = TOKEN_RE.findall(s.lower())
    if len(toks) < 5:
        return False
    return sum(t in EN_STOP for t in toks) / len(toks) >= 0.12


def prep(df, min_words=5):
    stats = {"input_rows": int(len(df))}
    df = df.copy()
    df["text"] = df["text"].fillna("").astype(str).map(clean_text)
    df = df[df["text"].str.split().str.len() >= min_words]
    stats["after_min_words"] = int(len(df))
    df = df[df["text"].map(is_english)]
    stats["after_english"] = int(len(df))
    key = df["text"].str.lower().str.replace(r"[^a-z0-9 ]", "", regex=True).str.strip()
    df = df[~key.duplicated(keep="first")]
    stats["after_dedup"] = int(len(df))
    return df.reset_index(drop=True), stats


def embed_all(texts, model_name, embedder, batch_size=256):
    if embedder == "tfidf":  # 로컬 코드 점검용(품질 무관)
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import normalize
        x = TfidfVectorizer(max_features=20000, stop_words="english").fit_transform(texts)
        z = TruncatedSVD(n_components=64, random_state=0).fit_transform(x)
        return normalize(z).astype(np.float32), None
    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer(model_name, device="cpu")
    st.max_seq_length = 128
    t0 = time.time()
    emb = st.encode(texts, batch_size=batch_size, show_progress_bar=False,
                    normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)
    print(f"  임베딩 {len(texts):,}개 완료 ({time.time() - t0:.0f}초)", flush=True)
    return emb, st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--comments", default="data/condensed_comments_merged.csv")
    ap.add_argument("--medium", required=True, choices=["broadway", "hollywood"])
    ap.add_argument("--out-dir", default="data/topics")
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--embedder", default="st", choices=["st", "tfidf"])
    ap.add_argument("--fit-n", type=int, default=150000)
    ap.add_argument("--cap-per-video", type=int, default=300)
    ap.add_argument("--min-topic-size", type=int, default=100)
    ap.add_argument("--nr-topics", type=int, default=40)
    ap.add_argument("--n-neighbors", type=int, default=15)
    ap.add_argument("--max-rows", type=int, default=0, help="점검용: 입력 행 수 제한")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    from bertopic import BERTopic
    from bertopic.vectorizers import ClassTfidfTransformer
    from hdbscan import HDBSCAN
    from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS
    from umap import UMAP

    os.makedirs(args.out_dir, exist_ok=True)
    rng = np.random.RandomState(args.seed)

    df = pd.read_csv(args.comments, encoding="utf-8-sig", low_memory=False,
                     usecols=["video_id", "comment_id", "is_reply", "text", "like_count",
                              "medium", "show", "source"])
    df = df[df["medium"] == args.medium]
    if args.max_rows:
        df = df.sample(min(args.max_rows, len(df)), random_state=args.seed)
    df["like_count"] = pd.to_numeric(df["like_count"], errors="coerce").fillna(0)
    df, stats = prep(df)
    print(f"[{args.medium}] 전처리: {stats}", flush=True)

    # 표본 선택: 영상당 cap 후 fit_n개
    pool = df.groupby("video_id", group_keys=False).apply(
        lambda g: g.sample(min(len(g), args.cap_per_video), random_state=args.seed))
    fit_idx = pool.index.to_numpy()
    if len(fit_idx) > args.fit_n:
        fit_idx = rng.choice(fit_idx, size=args.fit_n, replace=False)
    fit_mask = np.zeros(len(df), dtype=bool)
    fit_mask[fit_idx] = True
    stats.update(fit_rows=int(fit_mask.sum()))
    print(f"  학습 표본 {fit_mask.sum():,} / 전체 {len(df):,}", flush=True)

    texts = df["text"].tolist()
    emb, st = embed_all(texts, args.model, args.embedder)

    umap_model = UMAP(n_neighbors=args.n_neighbors, n_components=5, min_dist=0.0,
                      metric="cosine", random_state=args.seed, low_memory=True)
    hdb = HDBSCAN(min_cluster_size=args.min_topic_size, min_samples=10, metric="euclidean",
                  cluster_selection_method="eom", prediction_data=True)
    stop = list(ENGLISH_STOP_WORDS.union(EXTRA_STOP))
    vec = CountVectorizer(stop_words=stop, ngram_range=(1, 2), min_df=10,
                          token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z']+\b")
    rep = None
    if st is not None:
        from bertopic.representation import MaximalMarginalRelevance
        rep = MaximalMarginalRelevance(diversity=0.3)
    tm = BERTopic(embedding_model=st, umap_model=umap_model, hdbscan_model=hdb,
                  vectorizer_model=vec, ctfidf_model=ClassTfidfTransformer(reduce_frequent_words=True),
                  representation_model=rep, nr_topics=args.nr_topics,
                  calculate_probabilities=False, verbose=True)

    t0 = time.time()
    fit_docs = [texts[i] for i in np.where(fit_mask)[0]]
    topics_fit, _ = tm.fit_transform(fit_docs, embeddings=emb[fit_mask])
    print(f"  BERTopic 학습 완료 ({time.time() - t0:.0f}초)", flush=True)

    topics = np.full(len(df), -1, dtype=int)
    topics[np.where(fit_mask)[0]] = topics_fit
    rest = np.where(~fit_mask)[0]
    t0 = time.time()
    for s in range(0, len(rest), 50000):
        part = rest[s:s + 50000]
        t_part, _ = tm.transform([texts[i] for i in part], embeddings=emb[part])
        topics[part] = t_part
        print(f"  나머지 배정 {min(s + 50000, len(rest)):,}/{len(rest):,} "
              f"({time.time() - t0:.0f}초)", flush=True)
    df["topic"] = topics
    df["in_fit_sample"] = fit_mask

    # ---- 토픽 표 ----
    info = tm.get_topic_info().set_index("Topic")
    rows = []
    n_total = len(df)
    for tid, g in df.groupby("topic"):
        kws = [w for w, _ in (tm.get_topic(tid) or [])][:10]
        work = g["show"].value_counts()
        reps = [d[:220] for d in (tm.get_representative_docs(tid) or [])[:3]] if tid != -1 else []
        liked = g.sort_values("like_count", ascending=False).head(3)["text"].str[:220].tolist()
        rows.append({
            "topic": tid, "n_comments": len(g), "share_pct": round(100 * len(g) / n_total, 2),
            "name": info.loc[tid, "Name"] if tid in info.index else "",
            "keywords": ", ".join(kws), "n_videos": g["video_id"].nunique(),
            "n_works": work.size, "top_work": work.index[0],
            "top_work_share": round(work.iloc[0] / len(g), 3),
            "mean_likes": round(g["like_count"].mean(), 2),
            "rep_comments": " || ".join(reps), "top_liked_comments": " || ".join(liked),
        })
    tdf = pd.DataFrame(rows).sort_values("n_comments", ascending=False)
    tdf["type"] = np.where(tdf["topic"] == -1, "outlier",
                  np.where((tdf["top_work_share"] < 0.25) & (tdf["n_works"] >= 5), "generic", "work-specific"))
    tdf.to_csv(os.path.join(args.out_dir, f"topics_{args.medium}.csv"), index=False, encoding="utf-8-sig")
    df[["comment_id", "video_id", "show", "topic", "in_fit_sample"]].to_csv(
        os.path.join(args.out_dir, f"assignments_{args.medium}.csv"), index=False, encoding="utf-8-sig")

    stats.update(n_topics=int((tdf["topic"] != -1).sum()),
                 outlier_pct=round(float(100 * (df["topic"] == -1).mean()), 1),
                 generic_topics=int((tdf["type"] == "generic").sum()),
                 work_specific_topics=int((tdf["type"] == "work-specific").sum()))
    json.dump(stats, open(os.path.join(args.out_dir, f"prep_stats_{args.medium}.json"), "w"), indent=1)

    with open(os.path.join(args.out_dir, f"summary_{args.medium}.md"), "w", encoding="utf-8") as f:
        f.write(f"# {args.medium} 댓글 토픽 ({stats['n_topics']}개, 이상치 {stats['outlier_pct']}%)\n\n")
        for r in tdf[tdf["topic"] != -1].itertuples():
            f.write(f"## 토픽 {r.topic} [{r.type}] {r.n_comments:,}개 ({r.share_pct}%)\n"
                    f"- 키워드: {r.keywords}\n- 작품 집중도: {r.top_work} {r.top_work_share:.0%} "
                    f"(작품 {r.n_works}개)\n- 대표 댓글: {r.rep_comments}\n\n")
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
