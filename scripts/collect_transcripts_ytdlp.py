#!/usr/bin/env python3
"""
yt-dlp 기반 영어 자막 수집기 (collect_video_transcripts.py 가 IP당 15~25개에서 막힐 때의 대안).
입력 : --input CSV (video_id 열)    출력 : --out JSONL  (한 줄 = 영상 1개; 이어하기 가능)
  {"video_id","status","language","is_generated","n_chars","text"}
status: ok | no_english_transcript | unavailable | blocked | error
영어 수동 자막 > 영어 자동 자막. 영상은 받지 않는다(skip_download).
--cookies-from-browser firefox|chrome|edge  : 로그인된 브라우저 쿠키 사용 (차단이 심할 때만; 해당 계정에 사용 흔적이 남음. Chrome/Edge 는 브라우저를 끄고 실행)
사용 예)
  python scripts/collect_transcripts_ytdlp.py --input data/transcript_targets_t2.csv --out transcripts_ytdlp_t2.jsonl --limit 10
"""
import argparse, glob, json, os, random, shutil, tempfile, time
import pandas as pd
import yt_dlp

BLOCK = ("sign in to confirm", "429", "too many requests", "not a bot", "rate-limit", "rate limit")


def parse_json3(path):
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for ev in d.get("events", []):
        t = "".join(s.get("utf8", "") for s in ev.get("segs", []) or [])
        t = t.replace("\n", " ").strip()
        if t:
            out.append(t)
    return " ".join(out)


def fetch(video_id, tmp, cookies):
    o = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["en", "en-US", "en-GB", "en.*"],
         "subtitlesformat": "json3", "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"), "quiet": True, "no_warnings": True,
         "retries": 1, "socket_timeout": 30}
    if cookies:
        o["cookiesfrombrowser"] = (cookies,)
    with yt_dlp.YoutubeDL(o) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    files = sorted(glob.glob(os.path.join(tmp, video_id + ".*.json3")))
    if not files:
        return None
    manual = set((info.get("subtitles") or {}).keys())
    # 수동 영어 자막 우선
    pick = None
    for f in files:
        lang = f[len(os.path.join(tmp, video_id)) + 1:-len(".json3")]
        if lang in manual:
            pick = (f, lang, False); break
    if pick is None:
        f = files[0]; pick = (f, f[len(os.path.join(tmp, video_id)) + 1:-len(".json3")], True)
    return pick[1], pick[2], parse_json3(pick[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-sleep", type=float, default=4); ap.add_argument("--max-sleep", type=float, default=9)
    ap.add_argument("--cookies-from-browser", default=None)
    a = ap.parse_args()
    ids = pd.read_csv(a.input)["video_id"].dropna().astype(str).tolist()
    done = set()
    if os.path.exists(a.out):
        for ln in open(a.out, encoding="utf-8"):
            try: done.add(json.loads(ln)["video_id"])
            except Exception: pass
    todo = [v for v in ids if v not in done]
    if a.limit: todo = todo[:a.limit]
    print(f"전체 {len(ids)}, 완료 {len(done)}, 이번 처리 {len(todo)}", flush=True)
    blocked = 0
    with open(a.out, "a", encoding="utf-8") as fo:
        for i, v in enumerate(todo, 1):
            tmp = tempfile.mkdtemp(); rec = {"video_id": v}
            try:
                r = fetch(v, tmp, a.cookies_from_browser)
                if r is None:
                    rec["status"] = "no_english_transcript"
                else:
                    rec.update(status="ok", language=r[0], is_generated=r[1], n_chars=len(r[2]), text=r[2])
            except Exception as e:
                m = str(e).lower()
                if any(k in m for k in BLOCK): rec.update(status="blocked", error=str(e)[:150])
                elif any(k in m for k in ("private", "unavailable", "removed", "terminated")): rec.update(status="unavailable", error=str(e)[:150])
                else: rec.update(status="error", error=str(e)[:150])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if rec["status"] == "blocked":
                blocked += 1
                print(f"[{i}/{len(todo)}] {v} BLOCKED ({blocked}/2)", flush=True)
                if blocked >= 2:
                    print("차단 의심: 중단합니다(결과는 저장됨). 나중에 같은 명령으로 이어서 실행하세요."); break
                continue  # blocked 는 저장하지 않아 나중에 다시 시도
            blocked = 0
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n"); fo.flush()
            print(f"[{i}/{len(todo)}] {v} {rec['status']}", flush=True)
            time.sleep(random.uniform(a.min_sleep, a.max_sleep))


if __name__ == "__main__":
    main()
