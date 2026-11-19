"""
qwen_vl_plot_coverage.py   (GPU 필요 - 로컬/Colab에서 직접 실행. GitHub Actions에서는 못 돌림)
-----------------------------------------------------------------------------------------------
Qwen3-VL-4B로 "영상이 시놉시스 줄거리를 얼마나 보여주는가"(플롯 포인트 커버리지)를 재는 2단계 스크립트.

  1단계  caption  영상 -> 구간별 장면 설명 (visual captions)         -> captions.jsonl
  2단계  judge    시놉시스 문장(=플롯 포인트)마다 영상 근거(자막+장면 설명)에서
                  "이 사건이 영상에 나오는가"를 Yes/No 확률로 판정    -> plot_coverage.csv
         결과: pc_mean (Yes 확률 평균), pc_ratio (Yes 확률>=0.5 비율), pc_end_mean (결말부 문장만)

입력
  --pairs      data/similarity/results_alt/similarity_alt_results.csv   (ALT 워크플로 결과; medium, work_id, video_id, video_url 필요)
  --synopses-hollywood / --synopses-broadway   synopses_*.xlsx (config_similarity_alt.yml과 같은 파일)
  --transcripts data/similarity/youtube_transcripts_all_v13.csv
  --video-dir  {video_id}.mp4 가 들어 있는 폴더 (1단계에서만 필요. 없는 영상은 건너뛰고 missing_videos.txt에 기록)

실행 예 (Colab/로컬, GPU):
  pip install -U "transformers>=4.57" accelerate opencv-python pandas openpyxl scikit-learn pyyaml
  python scripts/qwen_vl_plot_coverage.py caption --pairs ... --video-dir videos --out-dir out_qwen --limit 5
  python scripts/qwen_vl_plot_coverage.py judge   --pairs ... --out-dir out_qwen --limit 5
  (--limit 5로 먼저 확인한 뒤 --limit 없이 전체)

모델: Qwen/Qwen3-VL-4B-Instruct (bf16 약 10GB VRAM). 더 작은 GPU면 --model Qwen/Qwen3-VL-2B-Instruct.
주의: 이 스크립트는 GPU 환경이 없어 작성 환경에서 실행 검증을 못 했다. 첫 실행은 --limit 3으로 확인할 것.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_similarity import chunk_words, clean_transcript, split_sentences  # noqa: E402

MODEL = "Qwen/Qwen3-VL-4B-Instruct"


def load_model(name):
    import torch
    from transformers import AutoProcessor
    try:
        from transformers import Qwen3VLForConditionalGeneration as VL
    except ImportError:  # transformers가 너무 오래된 경우
        sys.exit("transformers>=4.57 필요: pip install -U 'transformers>=4.57'")
    model = VL.from_pretrained(name, torch_dtype=torch.bfloat16, device_map="auto")
    proc = AutoProcessor.from_pretrained(name)
    return model, proc


def load_pairs(path, limit):
    p = pd.read_csv(path, encoding="utf-8-sig")
    p = p.drop_duplicates(["medium", "work_id", "video_id"])
    return p.head(limit) if limit else p


# ----------------------------------------------------------------------------- 1단계: caption
def sample_frames(path, window_sec, frames_per_window, max_windows, size=448):
    import cv2
    from PIL import Image
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = n / fps
    n_win = int(min(max_windows, max(1, np.ceil(dur / window_sec))))
    # 영상이 길면 창 간격을 넓혀 전체를 고르게 덮는다
    step = dur / n_win
    out = []
    for w in range(n_win):
        t0 = w * step
        imgs = []
        for k in range(frames_per_window):
            t = t0 + (k + 0.5) * min(window_sec, step) / frames_per_window
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(min(t, dur - 0.1) * fps))
            ok, fr = cap.read()
            if ok:
                im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
                im.thumbnail((size, size))
                imgs.append(im)
        if imgs:
            out.append((t0, imgs))
    cap.release()
    return dur, out


def generate(model, proc, images, prompt, max_new_tokens):
    import torch
    content = [{"type": "image", "image": im} for im in images] + [{"type": "text", "text": prompt}]
    inputs = proc.apply_chat_template([{"role": "user", "content": content}], tokenize=True,
                                      add_generation_prompt=True, return_dict=True, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    return proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0].strip()


def run_caption(a):
    pairs = load_pairs(a.pairs, a.limit)
    vids = pairs.drop_duplicates("video_id")[["video_id", "video_url"]]
    out_path = Path(a.out_dir) / "captions.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(l)["video_id"] for l in open(out_path, encoding="utf-8")}
    model, proc = load_model(a.model)
    prompt = ("These are frames in order from one segment of a video about a movie or stage show. "
              "In 1-2 plain sentences, say what is happening in the story (who does what), like a plot summary. "
              "Do not describe camera, text overlays or the video format.")
    missing = []
    with open(out_path, "a", encoding="utf-8") as f:
        for r in vids.itertuples(index=False):
            if r.video_id in done:
                continue
            vp = Path(a.video_dir) / f"{r.video_id}.mp4"
            if not vp.exists():
                missing.append(r.video_id)
                continue
            dur, wins = sample_frames(vp, a.window_sec, a.frames_per_window, a.max_windows)
            segs = []
            for t0, imgs in wins:
                segs.append({"t": round(t0, 1), "caption": generate(model, proc, imgs, prompt, a.max_new_tokens)})
            f.write(json.dumps({"video_id": r.video_id, "duration_sec": round(dur, 1), "segments": segs}, ensure_ascii=False) + "\n")
            f.flush()
            print(f"captioned {r.video_id}: {len(segs)} segments", flush=True)
    (Path(a.out_dir) / "missing_videos.txt").write_text("\n".join(missing), encoding="utf-8")
    print(f"done. missing videos (no file in --video-dir): {len(missing)}")


# ----------------------------------------------------------------------------- 2단계: judge
def yes_prob(model, proc, prompt):
    """다음 토큰 로짓으로 Yes/No 확률을 구한다 (생성 없이 빠름)."""
    import torch
    inputs = proc.apply_chat_template([{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                                      tokenize=True, add_generation_prompt=True, return_dict=True,
                                      return_tensors="pt").to(model.device)
    with torch.no_grad():
        logits = model(**inputs).logits[0, -1]
    tok = proc.tokenizer
    yes = [tok.encode(x, add_special_tokens=False)[0] for x in ("Yes", "yes")]
    no = [tok.encode(x, add_special_tokens=False)[0] for x in ("No", "no")]
    ly = torch.logsumexp(logits[yes], 0)
    ln = torch.logsumexp(logits[no], 0)
    return float(torch.sigmoid(ly - ln))


def run_judge(a):
    from sklearn.feature_extraction.text import TfidfVectorizer
    pairs = load_pairs(a.pairs, a.limit)
    tr = pd.read_csv(a.transcripts)
    tr["video_id"] = tr["video_url"].astype(str).str.extract(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})")[0]
    tr = tr.drop_duplicates("video_id").set_index("video_id")
    caps = {}
    cp = Path(a.out_dir) / "captions.jsonl"
    if cp.exists():
        for l in open(cp, encoding="utf-8"):
            d = json.loads(l)
            caps[d["video_id"]] = d
    syn_frames = []
    for path, med, col in ((a.synopses_hollywood, "hollywood", None), (a.synopses_broadway, "broadway", None)):
        if path and Path(path).exists():
            s = pd.read_excel(path, sheet_name="synopses") if med == "hollywood" else pd.read_excel(path)
            c = [x for x in s.columns if str(x).startswith("synopsis")][0]
            s = s.rename(columns={c: "synopsis", "show_id": "work_id"})
            s["medium"] = med
            syn_frames.append(s[["medium", "work_id", "synopsis"]])
    syn = pd.concat(syn_frames).set_index(["medium", "work_id"])["synopsis"].to_dict()

    model, proc = load_model(a.model)
    rows = []
    for r in pairs.itertuples(index=False):
        if r.video_id not in tr.index or (r.medium, r.work_id) not in syn:
            continue
        # 영상 근거 = 자막 청크 + (있으면) 장면 설명
        ev = chunk_words(clean_transcript(tr.loc[r.video_id, "text"]), 120, 20)
        if r.video_id in caps:
            ev += [s["caption"] for s in caps[r.video_id]["segments"]]
        if not ev:
            continue
        sents = split_sentences(syn[(r.medium, r.work_id)], 6)
        tf = TfidfVectorizer(stop_words="english", sublinear_tf=True).fit(ev + sents)
        E, S = tf.transform(ev), tf.transform(sents)
        sim = (S @ E.T).toarray()
        probs = []
        for i, sent in enumerate(sents):
            top = np.argsort(-sim[i])[: a.top_k]
            evidence = "\n".join(f"- {ev[j]}" for j in sorted(top))
            prompt = (f"Plot event from the official synopsis:\n\"{sent}\"\n\n"
                      f"Excerpts from a video (speech transcript and scene descriptions):\n{evidence}\n\n"
                      "Does the video reveal or clearly describe this plot event? Answer only Yes or No.")
            probs.append(yes_prob(model, proc, prompt))
        probs = np.array(probs)
        n_end = max(1, int(np.ceil(len(probs) * 0.2)))
        rows.append(dict(medium=r.medium, work_id=r.work_id, video_id=r.video_id, n_plot_points=len(probs),
                         has_visual_captions=int(r.video_id in caps),
                         pc_mean=float(probs.mean()), pc_ratio=float((probs >= 0.5).mean()),
                         pc_end_mean=float(probs[-n_end:].mean())))
        print(f"judged {r.video_id}: pc_mean={rows[-1]['pc_mean']:.3f}", flush=True)
    pd.DataFrame(rows).to_csv(Path(a.out_dir) / "plot_coverage.csv", index=False, encoding="utf-8-sig")
    print(f"saved {len(rows)} rows -> {Path(a.out_dir) / 'plot_coverage.csv'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["caption", "judge"])
    ap.add_argument("--pairs", default="data/similarity/results_alt/similarity_alt_results.csv")
    ap.add_argument("--transcripts", default="data/similarity/youtube_transcripts_all_v13.csv")
    ap.add_argument("--synopses-hollywood", default="data/similarity/synopses_hollywood.xlsx")
    ap.add_argument("--synopses-broadway", default="data/similarity/synopses_broadway.xlsx")
    ap.add_argument("--video-dir", default="videos")
    ap.add_argument("--out-dir", default="out_qwen")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--window-sec", type=float, default=30.0)
    ap.add_argument("--frames-per-window", type=int, default=4)
    ap.add_argument("--max-windows", type=int, default=40, help="영상당 최대 구간 수 (긴 영상은 간격을 넓혀 전체를 덮음)")
    ap.add_argument("--max-new-tokens", type=int, default=80)
    ap.add_argument("--top-k", type=int, default=3, help="판정 시 문장마다 붙이는 근거 조각 수")
    a = ap.parse_args()
    Path(a.out_dir).mkdir(parents=True, exist_ok=True)
    run_caption(a) if a.stage == "caption" else run_judge(a)


if __name__ == "__main__":
    main()
