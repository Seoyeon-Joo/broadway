"""
transcribe_whisper.py
---------------------
영어 자막이 없는 t2 영상(data/whisper_targets.csv)의 오디오를 받아 Whisper(faster-whisper)로 받아쓴다.
결과는 collect_transcripts.py 와 같은 열(video_url,text,status,language,is_auto,title)의 CSV로 저장되며,
merge_transcripts.py 로 youtube_transcripts_all.xlsx 에 합친다 (is_auto 열에는 'whisper' 표시).
Whisper가 감지한 언어가 영어가 아니면 status='no_transcript'(영어 기반 분석 방침).

필요: pip install faster-whisper yt-dlp   (+ ffmpeg 가 PATH 에 있어야 함; 컷수 분석 때 이미 쓰셨다면 있음)
사용:
  py -3.13 scripts/transcribe_whisper.py --out transcripts_whisper/transcripts_shard_0.csv --limit 3   # 시험
  py -3.13 scripts/transcribe_whisper.py --out transcripts_whisper/transcripts_shard_0.csv             # 전체 (이어서 하기 가능)
  옵션: --model small(기본)|base|medium  --device auto|cpu|cuda  --cookies-from-browser chrome
"""
import argparse
import sys
import tempfile
from pathlib import Path

import pandas as pd

OUT_COLS = ["video_url", "text", "status", "language", "is_auto", "title"]


def download_audio(vid, tmp, cookies):
    import yt_dlp
    opts = {"format": "bestaudio/best", "outtmpl": str(Path(tmp) / f"{vid}.%(ext)s"), "quiet": True,
            "no_warnings": True, "noplaylist": True, "retries": 3}
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
    files = [f for f in Path(tmp).glob(f"{vid}.*")]
    return (files[0] if files else None), (info or {}).get("title", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="data/whisper_targets.csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--cookies-from-browser", default="")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    from faster_whisper import WhisperModel
    t = pd.read_csv(a.targets)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = set(pd.read_csv(out, usecols=["video_url"])["video_url"].str.extract(r"v=([\w-]{11})")[0])
    todo = [v for v in t["video_id"] if v not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"targets {len(t)} | already done {len(done)} | to do {len(todo)} | model {a.model}")
    if not todo:
        return 0
    model = WhisperModel(a.model, device=a.device, compute_type="int8" if a.device in ("auto", "cpu") else "float16")

    for i, v in enumerate(todo, 1):
        status, text, lang, title = "error", "", "", ""
        try:
            with tempfile.TemporaryDirectory() as tmp:
                f, title = download_audio(v, tmp, a.cookies_from_browser)
                if f is None:
                    raise RuntimeError("audio file missing")
                segs, info = model.transcribe(str(f), vad_filter=True, beam_size=1)
                lang = info.language
                text = " ".join(s.text.strip() for s in segs).strip()
            if lang != "en" or not text:
                status, text = "no_transcript", ""
            else:
                status = "ok"
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] {v} error: {type(e).__name__}: {str(e)[:120]}")
            continue  # 오류는 기록하지 않음 -> 다시 실행하면 재시도
        pd.DataFrame([[f"https://www.youtube.com/watch?v={v}", text, status, lang, "whisper", title]],
                     columns=OUT_COLS).to_csv(out, mode="a", header=not out.exists(), index=False, encoding="utf-8-sig")
        print(f"[{i}/{len(todo)}] {v} {status} lang={lang} words={len(text.split())}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
