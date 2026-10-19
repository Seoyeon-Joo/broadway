#!/usr/bin/env python3
"""
YouTube 영상 자막(영어) 수집기.  youtube-transcript-api (>=1.0) 사용, 영상 1개당 요청 1~2회.

입력  : --input CSV (video_url 또는 video_id 열)   예) data/youtube_video_urls.csv
출력  : --out-dir/transcripts_shard{I}.jsonl   (한 줄 = 영상 1개)
        {"video_id","status","language","is_generated","n_segments","text","segments":[[start,dur,text],...]}
status : ok | no_english_transcript | transcripts_disabled | unavailable | age_restricted | blocked | error
         (blocked = IP 차단. 그 지점에서 샤드를 멈추고 저장 → 나중에 --skip-jsonl 로 이어서 실행)
영어 우선순위: 수동 영어 자막(en, en-US, en-GB…) > 자동 생성 영어 자막.  번역 자막은 쓰지 않는다.

사용 예)
  python scripts/collect_video_transcripts.py --input data/youtube_video_urls.csv --out-dir out --shard 0 --num-shards 10
  python scripts/collect_video_transcripts.py --input ids.csv --out-dir out --skip-jsonl out/transcripts_shard0.jsonl
"""
import argparse, json, os, random, re, sys, time
import pandas as pd
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (AgeRestricted, IpBlocked, NoTranscriptFound, RequestBlocked,
                                            TranscriptsDisabled, VideoUnavailable, VideoUnplayable)

ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
EN = ("en", "en-US", "en-GB", "en-CA", "en-AU", "en-IN")


def vid(row):
    if "video_id" in row and isinstance(row["video_id"], str) and len(row["video_id"]) == 11:
        return row["video_id"]
    m = ID_RE.search(str(row.get("video_url", "")))
    return m.group(1) if m else None


def fetch_one(api, video_id):
    tl = api.list(video_id)
    manual = auto = None
    for t in tl:
        if t.language_code in EN or t.language_code.startswith("en"):
            if t.is_generated:
                auto = auto or t
            else:
                manual = manual or t
    t = manual or auto
    if t is None:
        return "no_english_transcript", None, None, []
    segs = t.fetch()
    out = [[round(s.start, 2), round(s.duration, 2), s.text.replace("\n", " ")] for s in segs]
    return "ok", t.language_code, bool(t.is_generated), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--out-dir", required=True)
    ap.add_argument("--shard", type=int, default=0); ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="이 샤드에서 처리할 최대 영상 수 (0=전부)")
    ap.add_argument("--skip-jsonl", action="append", default=[], help="이미 처리한 jsonl (video_id 중복 건너뜀)")
    ap.add_argument("--min-sleep", type=float, default=1.0); ap.add_argument("--max-sleep", type=float, default=2.5)
    ap.add_argument("--no-segments", action="store_true", help="타임스탬프 없이 text만 저장")
    a = ap.parse_args()

    df = pd.read_csv(a.input)
    ids = [v for v in (vid(r) for r in df.to_dict("records")) if v]
    ids = list(dict.fromkeys(ids))
    done = set()
    for p in a.skip_jsonl:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                        if j.get("status") != "blocked":
                            done.add(j["video_id"])
                    except Exception:
                        pass
    mine = [v for i, v in enumerate(ids) if i % a.num_shards == a.shard and v not in done]
    if a.limit:
        mine = mine[:a.limit]
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"transcripts_shard{a.shard}.jsonl")
    api = YouTubeTranscriptApi()
    n_ok = 0
    print(f"shard {a.shard}/{a.num_shards}: {len(mine)} videos to process ({len(done)} already done)", flush=True)
    with open(path, "a", encoding="utf-8") as out:
        for k, v in enumerate(mine, 1):
            rec = {"video_id": v, "status": "error", "language": None, "is_generated": None, "n_segments": 0, "text": "", "segments": []}
            try:
                st, lang, gen, segs = fetch_one(api, v)
                rec.update(status=st, language=lang, is_generated=gen, n_segments=len(segs), text=" ".join(s[2] for s in segs))
                if not a.no_segments:
                    rec["segments"] = segs
            except TranscriptsDisabled:
                rec["status"] = "transcripts_disabled"
            except (VideoUnavailable, VideoUnplayable):
                rec["status"] = "unavailable"
            except AgeRestricted:
                rec["status"] = "age_restricted"
            except NoTranscriptFound:
                rec["status"] = "no_english_transcript"
            except (IpBlocked, RequestBlocked):
                rec["status"] = "blocked"
                out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
                print(f"[{k}/{len(mine)}] BLOCKED at {v} - stopping shard; rerun later with --skip-jsonl", flush=True)
                break
            except Exception as e:
                rec["status"] = "error"; rec["text"] = str(e)[:200]
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += rec["status"] == "ok"
            if k % 25 == 0:
                out.flush(); print(f"[{k}/{len(mine)}] ok so far: {n_ok}", flush=True)
            time.sleep(random.uniform(a.min_sleep, a.max_sleep))
    print(f"done shard {a.shard}: ok={n_ok}/{len(mine)} -> {path}")


if __name__ == "__main__":
    main()
