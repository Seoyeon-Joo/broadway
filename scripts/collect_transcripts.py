"""
collect_transcripts.py
----------------------
패널의 t2 요약 영상 중 **컷수(shots_status == 'ok')까지 분석이 끝난 영상**의 영어 자막(transcript)을 수집한다.
이미 받은 영상은 건너뛰므로, 컷수가 새로 채워질 때마다 다시 돌리면 새로 생긴 영상만 수집된다.

대상 선정
  1) 패널(hollywood/broadway)에서 t2_video_id가 있고 shots_status == 'ok' 인 영상
  2) 기존 youtube_transcripts_all.xlsx에 이미 있으면 제외. 단 --retry 로 지정한 status(기본: error)는 다시 시도
       status: ok(받음) / no_transcript(영어 자막 없음) / unavailable(삭제·비공개·연령제한) / error

수집 규칙 (비영어 영상은 수집하지 않기로 한 방침에 맞춤)
  - 영어 수동 자막 우선, 없으면 영어 자동 생성 자막. 영어 자막이 없으면 status=no_transcript (번역 자막은 쓰지 않음)
  - 출력 열은 기존 파일과 같다: video_url, text, status, language, is_auto, title

IP 차단 대응
  - YouTube는 클라우드 IP를 막는 경우가 있다. 막히면 status='blocked'로 기록하고(최종 결과에는 반영 안 함)
    연속 차단이 많으면 중단한다. 환경변수 YT_PROXY_URL(예: http://user:pass@host:port)이 있으면 프록시를 쓴다.

사용:
  python scripts/collect_transcripts.py --out-dir transcripts_out [--shard-count 4 --shard-index 0] [--retry error,no_transcript] [--limit N]
"""
import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

EN = ["en", "en-US", "en-GB", "en-CA", "en-AU", "en-IN"]
OUT_COLS = ["video_url", "text", "status", "language", "is_auto", "title"]


def vid_of(url):
    return pd.Series([url]).astype(str).str.extract(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})")[0].iloc[0]


def build_targets(args):
    t = pd.read_excel(args.transcripts)
    t["video_id"] = t["video_url"].astype(str).str.extract(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})")[0]
    status = t.drop_duplicates("video_id").set_index("video_id")["status"]
    retry = {s.strip() for s in args.retry.split(",") if s.strip()}

    ids = []
    for path, sheet in [(args.panel_hollywood, args.hollywood_sheet), (args.panel_broadway, args.broadway_sheet)]:
        if not path:
            continue
        d = pd.read_excel(path, sheet_name=sheet, usecols=["t2_video_id", "shots_status"])
        d = d[d["t2_video_id"].notna() & (d["shots_status"] == "ok")]
        ids.extend(d["t2_video_id"].astype(str).str.strip().unique().tolist())
    ids = sorted(set(ids))
    have = status.reindex(ids)
    todo = [v for v, s in zip(ids, have) if pd.isna(s) or s in retry]
    print(f"t2 videos with cuts: {len(ids)} | already have transcript record: {int(have.notna().sum())} | "
          f"to collect (new + retry {sorted(retry)}): {len(todo)}")
    return todo


def titles_lookup(path):
    if not path or not Path(path).exists():
        return {}
    m = pd.read_csv(path, usecols=["video_id", "title"], low_memory=False).dropna(subset=["video_id"])
    return dict(zip(m["video_id"], m["title"]))


def make_api():
    from youtube_transcript_api import YouTubeTranscriptApi
    proxy = os.environ.get("YT_PROXY_URL", "").strip()
    if proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig
        return YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy))
    return YouTubeTranscriptApi()


def fetch_one(api, vid):
    """-> (status, text, language, is_auto)"""
    from youtube_transcript_api import (AgeRestricted, CouldNotRetrieveTranscript, IpBlocked, NoTranscriptFound,
                                        RequestBlocked, TranscriptsDisabled, VideoUnavailable, VideoUnplayable)
    try:
        tl = api.list(vid)
        tr, is_auto = None, False
        try:
            tr = tl.find_manually_created_transcript(EN)
        except NoTranscriptFound:
            try:
                tr, is_auto = tl.find_generated_transcript(EN), True
            except NoTranscriptFound:
                return "no_transcript", "", "", ""
        snippets = tr.fetch()
        text = " ".join(s.text.replace("\n", " ").strip() for s in snippets if s.text).strip()
        if not text:
            return "no_transcript", "", tr.language_code, is_auto
        return "ok", text, tr.language_code, is_auto
    except TranscriptsDisabled:
        return "no_transcript", "", "", ""
    except (VideoUnavailable, VideoUnplayable, AgeRestricted):
        return "unavailable", "", "", ""
    except (IpBlocked, RequestBlocked) as e:
        return "blocked", f"{type(e).__name__}: {str(e)[:150]}".replace("\n", " "), "", ""
    except CouldNotRetrieveTranscript as e:
        return "error", str(e)[:200].replace("\n", " "), "", ""
    except Exception as e:  # noqa: BLE001
        return "error", f"{type(e).__name__}: {e}"[:200].replace("\n", " "), "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel-hollywood", default="data/similarity/panel_current/hollywood.xlsx")
    ap.add_argument("--panel-broadway", default="data/similarity/panel_current/broadway.xlsx")
    ap.add_argument("--hollywood-sheet", default="Sheet1")
    ap.add_argument("--broadway-sheet", default="Sheet1")
    ap.add_argument("--transcripts", default="data/similarity/youtube_transcripts_all.xlsx")
    ap.add_argument("--metadata", default="data/youtube_video_metadata.csv", help="제목 채우기용(선택)")
    ap.add_argument("--retry", default="error", help="다시 시도할 기존 status (쉼표 구분: error,no_transcript)")
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--max-consecutive-blocked", type=int, default=15)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"transcripts_shard_{a.shard_index}.csv"

    todo = build_targets(a)
    todo = todo[a.shard_index::a.shard_count]
    if a.limit:
        todo = todo[:a.limit]
    done = set()
    if out_csv.exists():  # 이어서 하기
        prev = pd.read_csv(out_csv, usecols=["video_url"])
        done = set(prev["video_url"].astype(str).str.extract(r"v=([\w-]{11})")[0])
    todo = [v for v in todo if v not in done]
    print(f"shard {a.shard_index}/{a.shard_count}: {len(todo)} videos to fetch")
    if not todo:
        if not out_csv.exists():
            pd.DataFrame(columns=OUT_COLS).to_csv(out_csv, index=False, encoding="utf-8-sig")
        return 0

    titles = titles_lookup(a.metadata)
    api = make_api()
    buf, blocked_run, counts, samples = [], 0, {}, []

    def flush():
        if buf:
            pd.DataFrame(buf, columns=OUT_COLS).to_csv(out_csv, mode="a", header=not out_csv.exists(),
                                                       index=False, encoding="utf-8-sig")
            buf.clear()

    for i, v in enumerate(todo, 1):
        status, text, lang, is_auto = fetch_one(api, v)
        counts[status] = counts.get(status, 0) + 1
        if status in ("blocked", "error") and len(samples) < 5:
            samples.append(f"{v}: {status}: {text}")
        if status == "blocked":
            blocked_run += 1
            if blocked_run >= a.max_consecutive_blocked:
                print(f"[stop] {blocked_run}회 연속 IP 차단 -> 중단. 프록시(YT_PROXY_URL)를 설정하거나 나중에 다시 실행하세요.")
                break
            time.sleep(min(60, 5 * blocked_run))
            continue  # 차단은 기록하지 않음 -> 다음 실행에서 다시 대상이 됨
        blocked_run = 0
        buf.append([f"https://www.youtube.com/watch?v={v}", text, status, lang, is_auto, titles.get(v, "")])
        if len(buf) >= 20:
            flush()
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} {counts}", flush=True)
        time.sleep(a.sleep)
    flush()
    print(f"done: {counts}")
    for m in samples:
        print("  sample:", m)
    import json
    (out_dir / f"diag_shard_{a.shard_index}.json").write_text(
        json.dumps({"shard": a.shard_index, "counts": counts, "samples": samples}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
