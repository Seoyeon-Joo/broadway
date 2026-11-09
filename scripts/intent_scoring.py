"""
intent_scoring.py
==================
condensed 영상 댓글에서 "관람/구매 의사" 댓글을 찾기 위한 1단계: 마스킹 -> 임베딩(저장) -> 시드 문장과의
유사도 점수 -> 기준선 확정용 구간별 샘플 출력.

*** 왜 이렇게 하나 ***
전체 댓글을 한꺼번에 토픽 모델링하면 작품·배우 이야기가 판을 지배하고, 관람 의사 댓글은 소수라 묻힌다.
그래서 (1) 작품명·인물명을 마스킹해서 임베딩이 "누구/무슨 작품"이 아니라 "무슨 말을 하는가"를 반영하게
하고, (2) 관람 의사를 나타내는 시드 문장과의 의미 유사도로 후보 댓글을 먼저 고른다. 유사도는 상대값이라
기준선(threshold)은 사람이 샘플을 읽고 정해야 하므로, 이 스크립트는 점수와 구간별 샘플까지만 만든다.

마스킹(텍스트를 "it"/"someone"으로 치환):
  - 댓글이 달린 영상 작품의 제목 단어
  - 매체 안에서 한 작품에 60%+ 몰린 단어(인물명·작품 고유어)
  - spaCy NER의 PERSON, WORK_OF_ART
  관람 의사 판단에 필요한 단어(ticket, booked, tomorrow, streaming 등)는 보호 목록으로 지우지 않음.

시드 그룹:
  purchase     이미 표를 샀거나 일정을 잡음
  anticipation 곧 볼 예정/기대
  desire       영상을 보고 더 보고 싶어짐(Spillover 신호)
  substitute   이제 안 봐도 됨/스트리밍으로 기다림(Substitution 신호)
  barrier      보고 싶지만 가격·거리 때문에 못 감

출력(--out-dir): comments_masked.csv.gz, scores.csv.gz, samples.csv, stats.json,
                 embeddings_unique.npy(float16), emb_inverse.npy
"""
import argparse
import json
import os
import re
import time

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS as SK_STOP

SEEDS = {
    "purchase": [
        "I just bought tickets to see it", "I got my tickets for next week", "tickets booked for this weekend",
        "I have tickets to see it tomorrow", "I'm seeing it this weekend", "just booked my seats",
        "got front row tickets", "I already bought tickets and I can't wait",
    ],
    "anticipation": [
        "I can't wait to see it", "so excited to watch this in theaters", "counting down the days until I see it",
        "can't wait for opening night", "I'm definitely going to see it in the cinema",
        "I'm going to see this for sure", "this looks amazing, I'm going opening weekend",
    ],
    "desire": [
        "Now I really want to see the whole thing", "this makes me want to watch the full movie",
        "I need to see this in person", "I'm going to go see it now", "I have to watch the original now",
        "this made me want to buy tickets", "after watching this I want to go see it live",
    ],
    "substitute": [
        "Now I don't need to watch the movie", "I don't have to see it now, thanks for the summary",
        "I'll just wait until it's on streaming", "no need to pay for a ticket now",
        "this summary is enough, I got the whole story", "I'll skip the theater and wait for it to be free",
        "thanks for the recap, I don't need to watch it anymore",
    ],
    "barrier": [
        "I wish I could see this live", "I can't afford the tickets", "tickets are way too expensive",
        "I wish it came to my city", "I want to go but it's too far", "sold out, I couldn't get tickets",
    ],
}
KW_TICKET = re.compile(r"\b(tickets?|booked|booking|box office|opening night|opening weekend|in theat(?:er|re)s?|"
                       r"in the cinema|front row|presale|pre-sale)\b", re.I)

PROTECT = set("""ticket tickets book booked booking buy bought seat seats theater theaters theatre theatres cinema
opening night weekend tomorrow tonight today excited wait watch watching stream streaming free netflix price expensive
cheap show play musical movie film live broadway imax premiere sold tour""".split())

URL_RE = re.compile(r"https?://\S+|www\.\S+")
TS_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
MENTION_RE = re.compile(r"@\S+")
REPEAT_RE = re.compile(r"(.)\1{3,}")
TOKEN_RE = re.compile(r"[a-z']+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
CAP_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b(?!I\b|I')[A-Z][a-zA-Z]+")
EN_STOP = set("the a an and or but if of to in on at for with is are was were be been it its this that i you he "
              "she we they me my your his her our their do does did have has had not no so as by from about just "
              "very really can will would could should what which who when where why how there here all more".split())


def clean_text(s):
    s = URL_RE.sub(" ", s)
    s = TS_RE.sub(" ", s)
    s = MENTION_RE.sub(" ", s)
    s = REPEAT_RE.sub(r"\1\1\1", s)
    return re.sub(r"\s+", " ", s).strip()


def is_english(s):
    if not s:
        return False
    if sum(ch.isascii() for ch in s) / len(s) < (0.95 if len(s.split()) < 5 else 0.9):
        return False
    toks = TOKEN_RE.findall(s.lower())
    if len(toks) < 3:
        return False
    if len(toks) < 5:  # 짧은 댓글은 영어 불용어 비율 검사를 하지 않음("Tickets booked!")
        return True
    return sum(t in EN_STOP for t in toks) / len(toks) >= 0.12


def prep(df, min_words):
    st = {"input_rows": int(len(df))}
    df = df.copy()
    df["text"] = df["text"].fillna("").astype(str).map(clean_text)
    df = df[df["text"].str.split().str.len() >= min_words]
    st["after_min_words"] = int(len(df))
    df = df[df["text"].map(is_english)]
    st["after_english"] = int(len(df))
    key = df["text"].str.lower().str.replace(r"[^a-z0-9 ]", "", regex=True).str.strip()
    df = df[~pd.DataFrame({"v": df["video_id"], "k": key}).duplicated(keep="first")]  # 같은 영상 안 복붙만 제거
    st["after_dedup_within_video"] = int(len(df))
    return df.reset_index(drop=True), st


def entity_terms(df):
    """매체 안에서 한 작품에 60%+ 몰린 단어(인물명·작품 고유어)."""
    import scipy.sparse as sp
    from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS
    uv = CountVectorizer(min_df=15, token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z']{2,}\b")
    U = uv.fit_transform(df["text"])
    V = np.array(uv.get_feature_names_out())
    codes, uniq = pd.factorize(df["show"])
    W = sp.csr_matrix((np.ones(len(df)), (codes, np.arange(len(df)))), shape=(len(uniq), len(df)))
    G = (W @ U).tocsc()
    tot = np.asarray(G.sum(0)).ravel()
    mx = np.asarray(G.max(0).todense()).ravel()
    ent = (mx / tot >= 0.6) & (mx >= 15) & ~np.isin(V, list(ENGLISH_STOP_WORDS))
    return {w for w in V[ent] if w not in PROTECT}


def title_tokens(show):
    return {w.lower() for w in WORD_RE.findall(show or "") if len(w) >= 3} - PROTECT - EN_STOP - set(SK_STOP)


def mask_words(s, ents, own):
    def rep(m):
        w = m.group(0).lower()
        return "it" if (w in ents or w in own) else m.group(0)
    return re.sub(r"\b(?:it\s+)+it\b", "it", WORD_RE.sub(rep, s))


def mask_ner(texts, nlp, n_process):
    out = list(texts)
    idx = [i for i, s in enumerate(texts) if CAP_RE.search(s)]
    for i, doc in zip(idx, nlp.pipe([texts[i] for i in idx], batch_size=2000, n_process=n_process)):
        s = texts[i]
        for e in sorted(doc.ents, key=lambda e: -e.start_char):
            if e.label_ in ("PERSON", "WORK_OF_ART"):
                s = s[:e.start_char] + ("someone" if e.label_ == "PERSON" else "it") + s[e.end_char:]
        out[i] = s
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--comments", default="data/condensed_comments_merged.csv")
    ap.add_argument("--out-dir", default="data/intent")
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--embedder", default="st", choices=["st", "hash"])
    ap.add_argument("--min-words", type=int, default=3)
    ap.add_argument("--no-ner", action="store_true")
    ap.add_argument("--n-process", type=int, default=2)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--band-n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--only-current", action="store_true",
                    help="in_current_panel==1 인 영상의 댓글만 사용(최신 패널에서 빠진 영상 제외)")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    rng = np.random.RandomState(args.seed)

    df = pd.read_csv(args.comments, encoding="utf-8-sig", low_memory=False,
                     usecols=["video_id", "comment_id", "is_reply", "text", "like_count", "medium", "show", "source"]
                     + (["in_current_panel"] if args.only_current else []))
    if args.only_current:
        n0 = len(df)
        df = df[pd.to_numeric(df["in_current_panel"], errors="coerce").fillna(0) == 1].drop(columns=["in_current_panel"])
        print(f"현재 패널 영상만: {n0:,} -> {len(df):,}행")
    if args.max_rows:
        df = df.sample(min(args.max_rows, len(df)), random_state=args.seed)
    df["like_count"] = pd.to_numeric(df["like_count"], errors="coerce").fillna(0)
    parts, stats = [], {}
    for m in ["broadway", "hollywood"]:
        d, st = prep(df[df["medium"] == m], args.min_words)
        t0 = time.time()
        ents = entity_terms(d)
        own = d["show"].map(lambda s: title_tokens(s))
        d["text_masked"] = [mask_words(s, ents, o) for s, o in zip(d["text"], own)]
        st["entity_terms"] = len(ents)
        print(f"[{m}] {st} ({time.time() - t0:.0f}s)", flush=True)
        parts.append(d)
        stats[m] = st
    d = pd.concat(parts, ignore_index=True)

    if not args.no_ner:
        import spacy
        nlp = spacy.load("en_core_web_sm", disable=["tagger", "parser", "attribute_ruler", "lemmatizer"])
        t0 = time.time()
        d["text_masked"] = mask_ner(d["text_masked"].tolist(), nlp, args.n_process)
        print(f"NER 마스킹 완료 ({time.time() - t0:.0f}s)", flush=True)
    d["text_masked"] = d["text_masked"].str.replace(r"\s+", " ", regex=True).str.strip()

    # ---- 임베딩 (마스킹 텍스트 고유값만) ----
    uniq, inv = np.unique(d["text_masked"].values, return_inverse=True)
    print(f"고유 마스킹 텍스트 {len(uniq):,} / 전체 {len(d):,}", flush=True)
    if args.embedder == "hash":
        from sklearn.feature_extraction.text import HashingVectorizer
        from sklearn.preprocessing import normalize
        hv = HashingVectorizer(n_features=512, alternate_sign=False, ngram_range=(1, 2))
        enc = lambda xs: normalize(hv.transform(xs).toarray()).astype(np.float32)
        emb_u = enc(list(uniq))
    else:
        from sentence_transformers import SentenceTransformer
        st_model = SentenceTransformer(args.model, device="cpu")
        st_model.max_seq_length = 128
        enc = lambda xs: st_model.encode(list(xs), batch_size=256, show_progress_bar=False,
                                         normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)
        t0 = time.time()
        emb_u = enc(uniq)
        print(f"임베딩 완료 ({time.time() - t0:.0f}s)", flush=True)

    # ---- 시드 유사도 ----
    sc = {}
    for g, seeds in SEEDS.items():
        S = enc(seeds)
        sims_u = (emb_u @ S.T).max(1)
        sc[f"s_{g}"] = sims_u[inv]
    sc = pd.DataFrame(sc)
    sc.insert(0, "comment_id", d["comment_id"].values)
    cols = [c for c in sc.columns if c.startswith("s_")]
    sc["best_group"] = np.array([c[2:] for c in cols])[sc[cols].values.argmax(1)]
    sc["kw_ticket"] = d["text"].map(lambda s: bool(KW_TICKET.search(s))).values
    sc.to_csv(os.path.join(args.out_dir, "scores.csv.gz"), index=False, float_format="%.4f")

    d[["comment_id", "medium", "video_id", "show", "is_reply", "like_count", "source", "text", "text_masked"]].to_csv(
        os.path.join(args.out_dir, "comments_masked.csv.gz"), index=False, encoding="utf-8")
    np.save(os.path.join(args.out_dir, "embeddings_unique.npy"), emb_u.astype(np.float16))
    np.save(os.path.join(args.out_dir, "emb_inverse.npy"), inv.astype(np.int32))

    # ---- 기준선 확정용 구간별 샘플 ----
    N = len(sc)
    bands = [("top 0.2%", 0, .002), ("0.2~1%", .002, .01), ("1~3%", .01, .03), ("3~6%", .03, .06)]
    rows = []
    for g in SEEDS:
        order = np.argsort(-sc[f"s_{g}"].values)
        for name, lo, hi in bands:
            pool = order[int(lo * N):int(hi * N)]
            pick = rng.choice(pool, size=min(args.band_n, len(pool)), replace=False)
            for i in pick:
                rows.append({"group": g, "band": name, "score": round(float(sc[f"s_{g}"].values[i]), 3),
                             "medium": d["medium"].iat[i], "kw_ticket": bool(sc["kw_ticket"].iat[i]),
                             "text": d["text"].iat[i][:300], "text_masked": d["text_masked"].iat[i][:300]})
    pd.DataFrame(rows).to_csv(os.path.join(args.out_dir, "samples.csv"), index=False, encoding="utf-8-sig")
    stats["n_comments"] = int(N)
    stats["n_unique_masked"] = int(len(uniq))
    stats["kw_ticket_n"] = int(sc["kw_ticket"].sum())
    stats["score_quantiles"] = {g: {str(q): round(float(np.quantile(sc[f"s_{g}"], q)), 3)
                                    for q in (.5, .9, .95, .99, .995, .999)} for g in SEEDS}
    json.dump(stats, open(os.path.join(args.out_dir, "stats.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
