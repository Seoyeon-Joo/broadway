"""
compute_similarity.py
---------------------
영상 스크립트(transcript)와 작품 시놉시스의 유사도(Spoiler Intensity의 similarity 구성요소) 계산.

(video, work) 쌍마다 계산하는 지표
  doc_cosine        시놉시스 전체 임베딩 vs 스크립트 청크 임베딩 평균 (문서 수준 코사인)
  cov_mean          시놉시스 각 문장 -> 스크립트 청크 최대 코사인의 평균   (영상이 줄거리를 얼마나 담는가)
  cov_ge_<τ>        최대 코사인 >= τ 인 시놉시스 문장 비율 (τ는 config의 coverage_thresholds)
  end_cov_mean      시놉시스 마지막 end_fraction 구간(결말부) 문장들의 커버리지 평균 (결말 노출도)
  video_align_mean  스크립트 각 청크 -> 시놉시스 문장 최대 코사인의 평균   (영상 내용이 줄거리에 해당하는 정도)
  tfidf_cosine      TF-IDF 코사인 (어휘 기반 보조 지표 / 로버스트니스)

사용:
  python scripts/compute_similarity.py --config config_similarity.yml [--limit N] [--model NAME]

--model hashing 은 임베딩 모델 없이 파이프라인을 점검하는 스모크 테스트 전용 (논문 결과에 쓰지 말 것).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


# ----------------------------------------------------------------------------- text utils
_NOISE = re.compile(
    r"\[(?:music|applause|laughter|laughs|cheering|singing|sings|inaudible|__|\s)*\]|"  # [Music], [ __ ]
    r"♪+|\(\s*(?:music|applause|laughter)\s*\)|>>+|&gt;&gt;|"
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b",  # 타임스탬프
    flags=re.I,
)
_WS = re.compile(r"\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


def clean_transcript(t: str) -> str:
    t = _NOISE.sub(" ", str(t))
    return _WS.sub(" ", t).strip()


def split_sentences(text: str, min_words: int = 6):
    """간단한 문장 분리 후, 너무 짧은 문장은 다음 문장과 합친다."""
    text = _WS.sub(" ", str(text)).strip()
    if not text:
        return []
    sents = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    out, buf = [], ""
    for s in sents:
        buf = f"{buf} {s}".strip()
        if len(buf.split()) >= min_words:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] = f"{out[-1]} {buf}"
        else:
            out.append(buf)
    return out


def chunk_words(text: str, size: int, overlap: int):
    w = text.split()
    if not w:
        return []
    step = max(1, size - overlap)
    chunks = []
    for i in range(0, len(w), step):
        chunks.append(" ".join(w[i:i + size]))
        if i + size >= len(w):
            break
    return chunks


def looks_english(text: str, min_ascii=0.9, min_stop=0.25) -> bool:
    """의존성 없는 영어 판별: (1) 알파벳 중 ASCII 비율, (2) 토큰 중 영어 불용어 비율.
    메타데이터 language가 'en'이어도 실제로는 힌디어 등인 자동자막을 걸러내기 위한 용도."""
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS as SW
    sample = text[:5000]
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return False
    if sum(c.isascii() for c in letters) / len(letters) < min_ascii:
        return False
    toks = re.findall(r"[a-zA-Z']+", sample.lower())
    if not toks:
        return False
    return sum(w in SW for w in toks) / len(toks) >= min_stop


# ----------------------------------------------------------------------------- embedding backends
class HashingEncoder:
    """스모크 테스트용 (모델 다운로드 불필요). 의미 임베딩이 아님."""

    def __init__(self):
        from sklearn.feature_extraction.text import HashingVectorizer
        self.v = HashingVectorizer(n_features=1024, alternate_sign=False, norm="l2",
                                   ngram_range=(1, 2), stop_words="english")

    def encode(self, texts, **_):
        return self.v.transform(texts).toarray().astype(np.float32)


class STEncoder:
    def __init__(self, name, device, batch_size, max_len):
        from sentence_transformers import SentenceTransformer
        self.m = SentenceTransformer(name, device=device)
        self.batch_size = batch_size
        self.max_len = max_len

    def encode(self, texts, max_seq_length=None, **_):
        self.m.max_seq_length = max_seq_length or self.max_len
        return self.m.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True,
                             show_progress_bar=len(texts) > 200, convert_to_numpy=True)


def l2norm(x):
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


# ----------------------------------------------------------------------------- loading
def extract_video_id(series):
    return series.astype(str).str.extract(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})")[0]


def load_panel_pairs(cfg):
    """패널(final_v2)의 t2 처치 영상 -> (작품, 영상) 쌍. 패널에 남아 있는 작품만 분석 대상이 된다."""
    parts = []
    if cfg.get("panel_hollywood_xlsx"):
        h = pd.read_excel(cfg["panel_hollywood_xlsx"], sheet_name=cfg.get("panel_hollywood_sheet", "weekly_data"),
                          usecols=["movie_id", "Release", "t2_video_id"])
        h = h.rename(columns={"movie_id": "work_id", "Release": "work_title", "t2_video_id": "video_id"})
        h["medium"] = "hollywood"
        parts.append(h)
    if cfg.get("panel_broadway_xlsx"):
        b = pd.read_excel(cfg["panel_broadway_xlsx"], usecols=["show_id", "show", "t2_video_id"])
        b = b.rename(columns={"show_id": "work_id", "show": "work_title", "t2_video_id": "video_id"})
        b["medium"] = "broadway"
        parts.append(b)
    v = pd.concat(parts, ignore_index=True).dropna(subset=["video_id"])
    v["video_id"] = v["video_id"].astype(str).str.strip()
    v["work_id"] = v["work_id"].astype(int)
    return v.drop_duplicates(["medium", "work_id", "video_id"]).reset_index(drop=True)


def load_transcripts(path, cfg, log):
    t = pd.read_excel(path)
    n0 = len(t)
    t["video_url"] = t["video_url"].astype(str).str.strip()
    t["video_id"] = extract_video_id(t["video_url"])
    excl = []

    def drop(mask, reason):
        if mask.any():
            d = t[mask][["video_id", "video_url", "title", "status", "language"]].copy()
            d["reason"] = reason
            excl.append(d)
        return t[~mask]

    t = drop(t["status"].astype(str) != "ok", "status_not_ok")
    t = drop(t["text"].isna() | (t["text"].astype(str).str.strip() == ""), "empty_text")
    t["text_clean"] = t["text"].map(clean_transcript)
    t["transcript_words"] = t["text_clean"].str.split().str.len()
    t = drop(t["transcript_words"] < cfg["min_transcript_words"], "too_short")

    langs = tuple(l.lower() for l in cfg["languages"])
    if langs:
        lang_ok = t["language"].astype(str).str.lower().str.split("-").str[0].isin(langs)
        t = drop(~lang_ok, "language_metadata_not_allowed")
        if cfg.get("detect_language", True):
            ok = t["text_clean"].map(looks_english)
            t = drop(~ok, "language_text_not_english")
    excluded = pd.concat(excl, ignore_index=True) if excl else pd.DataFrame(
        columns=["video_id", "video_url", "title", "status", "language", "reason"])
    log.append(f"transcripts: {n0} rows -> kept {len(t)}; excluded {len(excluded)} "
               f"({excluded['reason'].value_counts().to_dict()})")
    return t.reset_index(drop=True), excluded


def load_synopses(hw_path, bw_path):
    def ren(df):
        c = [x for x in df.columns if str(x).startswith("synopsis")][0]
        return df.rename(columns={c: "synopsis"})

    frames = []
    if hw_path and Path(hw_path).exists():
        h = ren(pd.read_excel(hw_path, sheet_name="synopses"))
        h = h.rename(columns={"work_id": "work_id", "title": "title"})
        h["medium"] = "hollywood"
        frames.append(h)
    if bw_path and Path(bw_path).exists():
        b = ren(pd.read_excel(bw_path))
        b = b.rename(columns={"show_id": "work_id", "show": "title"})
        b["medium"] = "broadway"
        frames.append(b)
    s = pd.concat(frames, ignore_index=True)
    for col in ["section_used", "match_confidence", "needs_review", "plot_words (full)"]:
        if col not in s.columns:
            s[col] = np.nan
    s["synopsis"] = s["synopsis"].fillna("").astype(str)
    s["synopsis_words"] = s["synopsis"].str.split().str.len()
    s["synopsis_is_lead"] = (s["section_used"].astype(str).str.contains("no plot", case=False)
                             | (pd.to_numeric(s["plot_words (full)"], errors="coerce").fillna(0) == 0))
    return s[["medium", "work_id", "title", "synopsis", "synopsis_words", "synopsis_is_lead",
              "section_used", "match_confidence", "needs_review"]]


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_similarity.yml")
    ap.add_argument("--limit", type=int, default=0, help="디버그용: 처리할 (video,work) 쌍 수 제한")
    ap.add_argument("--model", default=None, help="config의 model 덮어쓰기 ('hashing' = 스모크 테스트)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--shard-count", type=int, default=1, help="영상 단위로 작업을 N개로 나눠 병렬 실행 (GitHub Actions matrix)")
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--merge-from", default=None, help="shard_*.csv가 있는 폴더: 계산 없이 합쳐서 최종 산출물만 생성")
    a = ap.parse_args()

    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    if a.model:
        cfg["model"] = a.model
    out_dir = Path(a.out_dir or cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []

    trans, excluded = load_transcripts(cfg["transcripts_xlsx"], cfg, log)
    syn = load_synopses(cfg.get("synopses_hollywood_xlsx"), cfg.get("synopses_broadway_xlsx"))
    if cfg.get("panel_hollywood_xlsx") or cfg.get("panel_broadway_xlsx"):
        vmap = load_panel_pairs(cfg)
        log.append(f"panel t2 pairs: {len(vmap)} ({vmap.groupby('medium').size().to_dict()})")
    else:  # 패널 없이 video_work_map.csv로 매핑하는 구버전 모드
        vmap = pd.read_csv(cfg["video_work_map_csv"], encoding="utf-8-sig")
        vmap["video_id"] = extract_video_id(vmap["video_url"])
        vmap["work_id"] = vmap["work_id"].astype(int)
    syn["work_id"] = syn["work_id"].astype(int)

    # 쌍별 계산 가능 여부 (커버리지 리포트)
    excl_reason = excluded.drop_duplicates("video_id").set_index("video_id")["reason"]
    pair_cov = vmap.copy()
    pair_cov["reason"] = np.where(pair_cov["video_id"].isin(trans["video_id"]), "ok",
                                  pair_cov["video_id"].map(excl_reason).fillna("not_in_transcript_file"))
    pair_cov = pair_cov.merge(syn[["medium", "work_id", "synopsis_words"]], on=["medium", "work_id"], how="left")
    bad_syn = (pair_cov["reason"] == "ok") & (pair_cov["synopsis_words"].isna() | (pair_cov["synopsis_words"] < cfg["min_synopsis_words"]))
    pair_cov.loc[bad_syn, "reason"] = "synopsis_missing_or_short"

    pairs = vmap.merge(trans[["video_id", "video_url", "title", "language", "is_auto", "text_clean", "transcript_words"]]
                       .rename(columns={"title": "video_title"}), on="video_id", how="inner")
    n_pairs_en = len(pairs)
    pairs = pairs.merge(syn.rename(columns={"title": "work_title_syn"}), on=["medium", "work_id"], how="left")
    no_syn = pairs["synopsis_words"].isna() | (pairs["synopsis_words"] < cfg["min_synopsis_words"])
    log.append(f"pairs after language/transcript filter: {n_pairs_en}; "
               f"dropped for missing/short synopsis: {int(no_syn.sum())}")
    skipped = pairs[no_syn][["video_id", "medium", "work_id", "work_title", "synopsis_words"]]
    pairs = pairs[~no_syn].reset_index(drop=True)
    if a.limit:
        pairs = pairs.head(a.limit)
    log.append(f"pairs to compute: {len(pairs)} | unique videos: {pairs.video_url.nunique()} | "
               f"unique works: {pairs[['medium', 'work_id']].drop_duplicates().shape[0]}")

    vid_text = pairs.drop_duplicates("video_url").set_index("video_url")["text_clean"]
    wk = pairs.drop_duplicates(["medium", "work_id"]).set_index(["medium", "work_id"])
    # ---------------- TF-IDF (전체 쌍 기준 - 샤드 간 동일해야 함)
    from sklearn.feature_extraction.text import TfidfVectorizer
    corpus_v = vid_text.reset_index()
    corpus = list(corpus_v["text_clean"]) + list(wk["synopsis"])
    tf = TfidfVectorizer(stop_words="english", sublinear_tf=True, min_df=2, max_df=0.9,
                         lowercase=True, token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z']+\b")
    X = tf.fit_transform(corpus)
    vid_row = {u: i for i, u in enumerate(corpus_v["video_url"])}
    wk_row = {k: len(corpus_v) + i for i, k in enumerate(wk.index)}

    # ---------------- 샤드 선택 (영상 단위)
    if a.shard_count > 1:
        uv = sorted(pairs["video_url"].unique())
        mine = set(uv[a.shard_index::a.shard_count])
        pairs = pairs[pairs["video_url"].isin(mine)].reset_index(drop=True)
        log.append(f"shard {a.shard_index}/{a.shard_count}: {len(pairs)} pairs, {pairs.video_url.nunique()} videos")

    if a.merge_from:
        parts = sorted(Path(a.merge_from).glob("shard_*.csv"))
        res = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in parts], ignore_index=True)
        log.append(f"merged {len(parts)} shards -> {len(res)} pairs")
        res = res.sort_values(["medium", "work_id", "video_id"]).reset_index(drop=True)
    else:
        # ---------------- embeddings
        if cfg["model"] == "hashing":
            enc = HashingEncoder()
        else:
            enc = STEncoder(cfg["model"], cfg["device"], cfg["batch_size"], cfg["chunk_max_tokens"])

        # 영상별 청크 임베딩
        vid_text = pairs.drop_duplicates("video_url").set_index("video_url")["text_clean"]
        vid_chunks, all_chunks, owner = {}, [], []
        for url, txt in vid_text.items():
            ch = chunk_words(txt, cfg["chunk_words"], cfg["chunk_overlap"])
            vid_chunks[url] = ch
            all_chunks.extend(ch)
            owner.extend([url] * len(ch))
        print(f"embedding {len(all_chunks)} transcript chunks ...", flush=True)
        emb = enc.encode(all_chunks) if all_chunks else np.zeros((0, 1))
        vid_emb, owner = {}, np.array(owner)
        for url in vid_chunks:
            vid_emb[url] = l2norm(emb[owner == url])

        # 작품별 문장 + 전체 임베딩
        wk = pairs.drop_duplicates(["medium", "work_id"]).set_index(["medium", "work_id"])
        work_sents, sent_all, sent_owner, full_texts, full_keys = {}, [], [], [], []
        for key, row in wk.iterrows():
            ss = split_sentences(row["synopsis"], cfg["sentence_min_words"])
            work_sents[key] = ss
            sent_all.extend(ss)
            sent_owner.extend([key] * len(ss))
            full_texts.append(row["synopsis"])
            full_keys.append(key)
        print(f"embedding {len(sent_all)} synopsis sentences, {len(full_texts)} full synopses ...", flush=True)
        s_emb = enc.encode(sent_all)
        full_emb = l2norm(enc.encode(full_texts, max_seq_length=cfg["doc_max_tokens"]))
        work_sent_emb = {}
        for key in work_sents:
            idx = [i for i, k in enumerate(sent_owner) if k == key]
            work_sent_emb[key] = s_emb[idx]
        work_full_emb = dict(zip(full_keys, full_emb))

        # ---------------- metrics
        taus = cfg["coverage_thresholds"]
        rows = []
        for r in pairs.itertuples(index=False):
            key = (r.medium, r.work_id)
            ce = vid_emb[r.video_url]
            se = work_sent_emb[key]
            sim = se @ ce.T                              # (n_sent, n_chunk)
            cov = sim.max(axis=1)                        # 문장별 최대 코사인
            n_end = max(1, int(np.ceil(len(cov) * cfg["end_fraction"])))
            rec = dict(
                medium=r.medium, work_id=r.work_id, work_title=r.work_title, video_id=r.video_id, video_url=r.video_url,
                video_title=r.video_title, language=r.language, is_auto=r.is_auto,
                transcript_words=r.transcript_words, n_chunks=len(ce),
                synopsis_words=r.synopsis_words, n_sentences=len(cov),
                synopsis_section=r.section_used, synopsis_is_lead=r.synopsis_is_lead,
                match_confidence=r.match_confidence, needs_review=r.needs_review,
                doc_cosine=float(work_full_emb[key] @ l2norm(ce.mean(axis=0))),
                cov_mean=float(cov.mean()),
                end_cov_mean=float(cov[-n_end:].mean()),
                video_align_mean=float(sim.max(axis=0).mean()),
                tfidf_cosine=float(X[vid_row[r.video_url]].multiply(X[wk_row[key]]).sum()),
            )
            for tau in taus:
                rec[f"cov_ge_{str(tau).replace('.', '')}"] = float((cov >= tau).mean())
            rows.append(rec)
        res = pd.DataFrame(rows)
        if a.shard_count > 1:
            sd = out_dir / "shards"
            sd.mkdir(parents=True, exist_ok=True)
            res.to_csv(sd / f"shard_{a.shard_index}.csv", index=False, encoding="utf-8-sig")
            print(f"shard {a.shard_index} done: {len(res)} pairs")
            return

    res.to_csv(out_dir / "similarity_results.csv", index=False, encoding="utf-8-sig")
    excluded.to_csv(out_dir / "similarity_excluded_transcripts.csv", index=False, encoding="utf-8-sig")
    skipped.to_csv(out_dir / "similarity_skipped_pairs.csv", index=False, encoding="utf-8-sig")
    pair_cov["computed"] = pair_cov.set_index(["medium", "work_id", "video_id"]).index.isin(
        res.set_index(["medium", "work_id", "video_id"]).index) if len(res) else False
    pair_cov.to_csv(out_dir / "similarity_pair_coverage.csv", index=False, encoding="utf-8-sig")
    cov_tbl = pair_cov.groupby(["medium", "reason"]).size().unstack(0).fillna(0).astype(int)
    log.append("pair coverage by reason:\n" + cov_tbl.to_string())
    summary = {
        "model": cfg["model"], "n_pairs": int(len(res)),
        "n_videos": int(res.video_url.nunique()),
        "by_medium": res.groupby("medium").size().to_dict() if len(res) else {},
        "coverage_by_reason": {m: pair_cov[pair_cov.medium == m]["reason"].value_counts().to_dict() for m in pair_cov.medium.unique()},
        "log": log,
        "describe": res[["doc_cosine", "cov_mean", "end_cov_mean", "video_align_mean", "tfidf_cosine"]]
        .describe().round(4).to_dict() if len(res) else {},
        "corr_embedding_vs_tfidf": float(res[["doc_cosine", "tfidf_cosine"]].corr().iloc[0, 1]) if len(res) > 2 else None,
    }
    (out_dir / "similarity_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    print("\n".join(log))
    print(json.dumps({k: summary[k] for k in ("model", "n_pairs", "n_videos", "by_medium",
                                               "corr_embedding_vs_tfidf")}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
