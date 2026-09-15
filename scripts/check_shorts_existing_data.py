"""
check_shorts_existing_data.py
===============================
이미 수집된 CSV(예: broadway_youtube_merged_cleaned_v4.csv)에 is_shorts(0/1)
컬럼을 실제로 채워 넣는 독립 실행 스크립트.

*** 반드시 youtube.com에 접속 가능한 환경(본인 PC, GitHub Actions 등)에서
실행해야 함 *** - Claude가 작업한 샌드박스 컨테이너는 youtube.com 접속이
네트워크 정책으로 막혀 있어서 여기서는 실행할 수 없었음.

판별 방법: https://www.youtube.com/shorts/{video_id} 를 리다이렉트 없이 요청.
  - 진짜 Shorts면 200 그대로 (리다이렉트 없음) -> is_shorts=1
  - 일반 영상이면 /watch?v={video_id}로 301/302/303/307/308 리다이렉트 -> is_shorts=0
  duration(초) <= 180초인 행만 확인 대상으로 삼아 요청 수를 줄임(Shorts 최대
  길이가 2024년부터 3분으로 늘어났으므로 그 이상은 확인할 필요 없이 0으로 둠).
  video_id가 없는 행(YouTube API가 아니라 title/URL만 있는 행 등)은 스킵.

사용 예시
--------
    pip install requests pandas
    python check_shorts_existing_data.py \\
        --in broadway_youtube_merged_cleaned_v4.csv \\
        --out broadway_youtube_merged_cleaned_v5.csv \\
        --url-col vedio_url --id-col video_id --duration-col duration

진행 상황은 --checkpoint 파일(기본: <out>.shorts_checkpoint.csv)에 video_id별로
저장되므로, 중간에 끊겨도 다시 실행하면 이어서 진행됨(--out 파일이 아직 없어도
체크포인트만 있으면 재개 가능).
"""
import argparse
import csv
import os
import time

import pandas as pd
import requests

YT_BASE = "https://www.youtube.com"


def to_seconds(duration_str):
    if not duration_str or pd.isna(duration_str):
        return None
    parts = str(duration_str).split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    return None


def check_is_shorts(session, video_id, timeout=10, max_retries=3):
    """/shorts/{video_id}를 리다이렉트 없이 요청해서 실제 Shorts인지 확인.
    네트워크 오류는 지수 백오프로 재시도, 끝내 실패하면 None(판별 불가) 반환."""
    backoff = 1.0
    for _ in range(max_retries):
        try:
            resp = session.head(
                f"{YT_BASE}/shorts/{video_id}", allow_redirects=False, timeout=timeout
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                return 0
            if resp.status_code == 200:
                return 1
            # HEAD가 405 등으로 막히면 GET으로 재시도 (redirect는 여전히 추적 안 함)
            resp = session.get(
                f"{YT_BASE}/shorts/{video_id}", allow_redirects=False, timeout=timeout
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                return 0
            if resp.status_code == 200:
                return 1
            return None
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return None


def load_checkpoint(path):
    done = {}
    if not os.path.isfile(path):
        return done
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            vid = row.get("video_id", "")
            val = row.get("is_shorts", "")
            if vid and val != "":
                done[vid] = int(val)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--id-col", default="video_id")
    ap.add_argument("--duration-col", default="duration")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--max-duration-sec", type=int, default=180,
                     help="이 초를 넘는 영상은 확인 없이 is_shorts=0으로 둠")
    ap.add_argument("--sleep", type=float, default=0.1,
                     help="요청 사이 대기(초) - too many requests 방지")
    args = ap.parse_args()

    checkpoint_path = args.checkpoint or (
        os.path.splitext(args.out_path)[0] + ".shorts_checkpoint.csv"
    )

    df = pd.read_csv(args.in_path)
    df["_duration_sec"] = df[args.duration_col].apply(to_seconds)

    done = load_checkpoint(checkpoint_path)
    checkpoint_is_new = not os.path.isfile(checkpoint_path)
    ckpt_f = open(checkpoint_path, "a", newline="", encoding="utf-8")
    ckpt_writer = csv.writer(ckpt_f)
    if checkpoint_is_new:
        ckpt_writer.writerow(["video_id", "is_shorts"])

    session = requests.Session()
    results = {}
    to_check = []
    for _, row in df.iterrows():
        vid = row.get(args.id_col)
        dur = row.get("_duration_sec")
        if pd.isna(vid) or not vid:
            continue
        if vid in done:
            results[vid] = done[vid]
            continue
        if dur is None or dur <= 0 or dur > args.max_duration_sec:
            results[vid] = 0
            continue
        to_check.append(vid)

    print(f"총 {len(df)}행 / 확인 필요한 후보(duration<={args.max_duration_sec}s, "
          f"미확인): {len(to_check)}개 (이미 체크포인트에 {len(done)}개 있음)")

    for i, vid in enumerate(to_check):
        val = check_is_shorts(session, vid)
        if val is None:
            # 확인 실패 -> 보수적으로 0 처리하되 체크포인트엔 남기지 않아 다음 실행 때 재시도
            results[vid] = 0
            continue
        results[vid] = val
        ckpt_writer.writerow([vid, val])
        ckpt_f.flush()
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(to_check)} 확인 완료")
        time.sleep(args.sleep)

    ckpt_f.close()

    df["is_shorts"] = df[args.id_col].map(results).fillna(0).astype(int)
    df = df.drop(columns=["_duration_sec"])
    df.to_csv(args.out_path, index=False)
    print(f"완료: is_shorts=1 인 영상 {df['is_shorts'].sum()}건 / 전체 {len(df)}건")
    print(f"저장 -> {args.out_path}")


if __name__ == "__main__":
    main()
