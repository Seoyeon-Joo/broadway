"""
transcribe_whisper.py
---------------------
영어 자막이 없는 t2 영상(data/whisper_targets.csv)의 오디오를 받아 Whisper(faster-whisper)로 받아쓴다.
결과는 collect_transcripts.py 와 같은 열(video_url,text,status,language,is_auto,title)의 CSV로 저장되며,
merge_transcripts.py 로 youtube_transcripts_all.xlsx 에 합친다 (is_auto 열에는 'whisper' 표시).
Whisper가 감지한 언어가 영어가 아니면 status='no_transcript'(영어 기반 분석 방침).

노래(뮤지컬 넘버)가 중심인 영상도 가사를 받아쓸 수 있게 설정을 바꿈:
  - VAD(음성 구간 필터) 기본 꺼짐: 켜면 노래를 음악으로 보고 통째로 버려서 텍스트가 비는 경우가 많았음 (--vad 로 켤 수 있음)
  - 언어를 영어로 고정(--language en, 기본). 노래는 언어 자동 감지가 자주 틀림. 'auto'로 두면 감지 결과를 씀
  - 모델 medium, beam_size 5 (가사는 반주 때문에 small/beam 1보다 훨씬 정확)
  - no_speech / log_prob 임계값을 꺼서 노래 구간이 "말 없음"으로 건너뛰어지지 않게 함
  - condition_on_previous_text=False: 음악 구간에서 같은 문장이 반복되는 환각을 줄임
  - --separate-vocals: demucs로 보컬만 분리한 뒤 받아쓰기 (반주가 센 영상용, 느림; pip install demucs)

필요: pip install faster-whisper yt-dlp   (+ ffmpeg 가 PATH 에 있어야 함; 컷수 분석 때 이미 쓰셨다면 있음)
사용:
  # 브로드웨이 유사도가 비어 있는 영상 63개 (data/whisper_targets_broadway.csv)
  py -3.13 scripts/transcribe_whisper.py --targets data/whisper_targets_broadway.csv --out transcripts_whisper_v2/transcripts_shard_0.csv --limit 5   # 시험
  py -3.13 scripts/transcribe_whisper.py --targets data/whisper_targets_broadway.csv --out transcripts_whisper_v2/transcripts_shard_0.csv             # 전체 (이어서 하기 가능)
  옵션: --model medium(기본)|small|large-v3  --device auto|cpu|cuda  --cookies-from-browser chrome  --vad  --language auto  --separate-vocals
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
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


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        raise RuntimeError("ffmpeg를 찾을 수 없음: py -3.13 -m pip install imageio-ffmpeg 후 다시 실행")


def load_audio(path):
    """PyAV 대신 ffmpeg로 16kHz 모노 float32 배열을 만든다."""
    r = subprocess.run([ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                        "-ar", "16000", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg 변환 실패: " + r.stderr.decode(errors="ignore")[:150])
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def separate_vocals(f, tmp):
    """demucs로 보컬 트랙만 뽑는다. 실패하면 원본 오디오를 그대로 쓴다."""
    try:
        r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-o", str(Path(tmp) / "sep"), str(f)],
                           capture_output=True)
        hit = list((Path(tmp) / "sep").rglob("vocals.*"))
        if r.returncode == 0 and hit:
            return hit[0]
        print("    (demucs 실패, 원본 오디오 사용):", r.stderr.decode(errors="ignore")[-120:])
    except Exception as e:  # noqa: BLE001
        print("    (demucs 사용 불가, 원본 오디오 사용):", str(e)[:100])
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="data/whisper_targets.csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="medium")
    ap.add_argument("--language", default="en", help="'en' (기본, 노래 영상에 안전) 또는 'auto'(언어 감지)")
    ap.add_argument("--vad", action="store_true", help="VAD 필터 켜기 (기본 꺼짐: 노래가 버려짐)")
    ap.add_argument("--separate-vocals", action="store_true", help="demucs로 보컬만 분리해서 받아쓰기")
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
                if a.separate_vocals:
                    f = separate_vocals(f, tmp)
                segs, info = model.transcribe(
                    load_audio(f), vad_filter=a.vad, beam_size=5,
                    language=None if a.language == "auto" else a.language,
                    no_speech_threshold=None, log_prob_threshold=None,
                    condition_on_previous_text=False)
                lang = info.language
                text = " ".join(s.text.strip() for s in segs).strip()
            if lang != "en" or not text:
                status, text = "no_transcript", ""
            else:
                status = "ok"
        except Exception as e:  # noqa: BLE001
            tb = traceback.extract_tb(e.__traceback__)[-1]
            print(f"  [{i}/{len(todo)}] {v} error: {type(e).__name__}: {str(e)[:120]} (at {Path(tb.filename).name}:{tb.lineno})")
            continue  # 오류는 기록하지 않음 -> 다시 실행하면 재시도
        pd.DataFrame([[f"https://www.youtube.com/watch?v={v}", text, status, lang, "whisper", title]],
                     columns=OUT_COLS).to_csv(out, mode="a", header=not out.exists(), index=False, encoding="utf-8-sig")
        print(f"[{i}/{len(todo)}] {v} {status} lang={lang} words={len(text.split())}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
