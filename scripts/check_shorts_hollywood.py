"""
check_shorts_hollywood.py
=========================
Hollywood YouTube 영상의 실제 Shorts 여부(is_shorts 0/1)를 판별.
(Broadway용 check_shorts_existing_data.py와 같은 판별 로직)

판별: https://www.youtube.com/shorts/{video_id} 를 리다이렉트 없이 요청
  - 200 그대로            -> is_shorts=1
  - /watch로 3xx 리다이렉트 -> is_shorts=0
  - duration_sec > 180    -> 요청 없이 0 (Shorts 최대 길이 3분)
  - 끝내 판별 실패         -> 체크포인트에 안 남김(다음 실행 때 재시도)

입력: data/hollywood_shorts_targets.csv (video_id,duration_sec)
출력: --out (video_id,is_shorts) — 실행할 때마다 이어서 append (체크포인트 겸용)

    python scripts/check_shorts_hollywood.py --out data/shorts/shard_0.csv \
        --shard-index 0 --num-shards 10
"""
import argparse
import csv
import os
import time
import zlib

import pandas as pd
import requests

YT = "https://www.youtube.com/shorts/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}


def check(session, vid, timeout=10, retries=3):
    backoff = 1.0
    for _ in range(retries):
        try:
            for method in (session.head, session.get):
                r = method(YT + vid, allow_redirects=False, timeout=timeout)
                if r.status_code in (301, 302, 303, 307, 308):
                    return 0
                if r.status_code == 200:
                    return 1
                if r.status_code == 429:
                    break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default="data/hollywood_shorts_targets.csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--max-duration-sec", type=int, default=180)
    ap.add_argument("--sleep", type=float, default=0.1)
    ap.add_argument("--time-budget-min", type=float, default=330,
                    help="이 시간이 지나면 저장하고 종료(Actions 6시간 제한 대비)")
    a = ap.parse_args()

    df = pd.read_csv(a.in_path).drop_duplicates("video_id")
    df = df[df.video_id.apply(lambda v: zlib.crc32(str(v).encode()) % a.num_shards == a.shard_index)]

    done = set()
    if os.path.isfile(a.out):
        done = set(pd.read_csv(a.out).video_id.astype(str))
    new = not os.path.isfile(a.out)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    f = open(a.out, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(["video_id", "is_shorts"])

    todo = []
    for vid, dur in zip(df.video_id.astype(str), df.duration_sec):
        if vid in done:
            continue
        if pd.isna(dur) or dur <= 0 or dur > a.max_duration_sec:
            w.writerow([vid, 0])
        else:
            todo.append(vid)
    f.flush()
    print(f"shard {a.shard_index}: 대상 {len(df)} / 이미 완료 {len(done)} / 요청 필요 {len(todo)}")

    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("CONSENT", "YES+1", domain=".youtube.com")
    t0, fail = time.time(), 0
    for i, vid in enumerate(todo, 1):
        if (time.time() - t0) / 60 > a.time_budget_min:
            print("시간 예산 소진 - 다음 실행에서 이어서 진행")
            break
        v = check(s, vid)
        if v is None:
            fail += 1
        else:
            w.writerow([vid, v])
            f.flush()
        if i % 500 == 0:
            print(f"  {i}/{len(todo)} (실패 {fail})")
        time.sleep(a.sleep)
    f.close()
    print(f"완료: 실패(다음에 재시도) {fail}건")


if __name__ == "__main__":
    main()
