"""
youtube_collect_metadata.py

config.yml에서 지정한 CSV(video_url 컬럼 포함)를 읽어,
YouTube Data API v3 (videos.list)로 아래 항목을 수집한다:
    video_title, description(전체), channel_name, published_at,
    duration_sec/duration_min, view_count, like_count, comment_count

사용법:
    export YOUTUBE_API_KEYS="key1,key2,key3"   # 콤마로 여러 키 가능(쿼터 초과 시 자동 교체)
    python youtube_collect_metadata.py --config config.yml

특징:
    - videos.list는 한 번에 최대 50개 video_id를 조회 가능 -> batch_size(기본 50)만큼씩 배치 처리
    - 여러 API 키를 등록해두면 쿼터 초과(403 quotaExceeded) 시 자동으로 다음 키로 교체
    - 중간에 중단되어도 checkpoint_csv를 읽어 이미 수집한 video_id는 다시 요청하지 않음
    - 네트워크/서버 오류는 지정한 횟수만큼 재시도
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime

import pandas as pd
import requests
import yaml

YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

VIDEO_ID_PATTERNS = [
    re.compile(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})"),
]


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_video_id(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    for pattern in VIDEO_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    # 이미 11자리 video_id 그 자체로 들어온 경우
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    return None


def parse_iso8601_duration(duration: str) -> int:
    """'PT1H2M10S' 형태의 ISO 8601 duration -> 총 초(sec)."""
    if not duration:
        return 0
    m = re.fullmatch(
        r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration
    )
    if not m:
        return 0
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


class KeyPool:
    """여러 YouTube API 키를 순환 사용하고, 죽은(쿼터초과/무효) 키는 건너뛴다."""

    def __init__(self, keys: list[str]):
        if not keys:
            raise ValueError(
                "YouTube API 키가 없습니다. 환경변수(예: YOUTUBE_API_KEYS)를 설정하세요."
            )
        self.keys = keys
        self.dead = set()
        self.idx = 0

    def current(self) -> str:
        for _ in range(len(self.keys)):
            key = self.keys[self.idx]
            if key not in self.dead:
                return key
            self.idx = (self.idx + 1) % len(self.keys)
        raise RuntimeError("사용 가능한 YouTube API 키가 모두 소진되었습니다.")

    def mark_dead_and_rotate(self):
        print(f"  [key] 키 index={self.idx} 쿼터 초과/무효 처리 -> 다음 키로 교체")
        self.dead.add(self.keys[self.idx])
        self.idx = (self.idx + 1) % len(self.keys)


def fetch_batch(video_ids: list[str], key_pool: KeyPool, cfg: dict) -> dict:
    """video_id 리스트(최대 50개) -> {video_id: item_dict} 반환."""
    params = {
        "part": "snippet,contentDetails,statistics",
        "id": ",".join(video_ids),
        "maxResults": 50,
    }
    retries = cfg.get("max_retries", 3)
    backoff = cfg.get("retry_backoff_sec", 5)

    for attempt in range(retries + 1):
        params["key"] = key_pool.current()
        try:
            resp = requests.get(YOUTUBE_VIDEOS_ENDPOINT, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  [warn] 네트워크 오류: {e} (attempt {attempt + 1}/{retries + 1})")
            time.sleep(backoff)
            continue

        if resp.status_code == 200:
            data = resp.json()
            return {item["id"]: item for item in data.get("items", [])}

        if resp.status_code in (403, 400):
            # 쿼터 초과 또는 API 키 문제로 추정 -> 키 교체 후 같은 배치 재시도
            print(f"  [warn] status={resp.status_code} body={resp.text[:200]}")
            key_pool.mark_dead_and_rotate()
            continue

        # 5xx 등 일시적 오류
        print(f"  [warn] status={resp.status_code}, {backoff}초 후 재시도")
        time.sleep(backoff)

    print(f"  [error] 배치 수집 실패, video_ids 일부 스킵: {video_ids[:3]}...")
    return {}


def build_record(video_id: str, url: str, item: dict | None) -> dict:
    if item is None:
        # API에서 못 찾은 경우(비공개/삭제된 영상 등) — 빈 값으로라도 기록
        return {
            "video_id": video_id,
            "video_url": url,
            "video_title": None,
            "description": None,
            "channel_name": None,
            "published_at": None,
            "duration_sec": None,
            "duration_min": None,
            "view_count": None,
            "like_count": None,
            "comment_count": None,
        }

    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content = item.get("contentDetails", {})

    duration_sec = parse_iso8601_duration(content.get("duration", ""))

    return {
        "video_id": video_id,
        "video_url": url,
        "video_title": snippet.get("title"),
        "description": snippet.get("description"),  # 축약 없이 전체 저장
        "channel_name": snippet.get("channelTitle"),
        "published_at": snippet.get("publishedAt"),
        "duration_sec": duration_sec,
        "duration_min": round(duration_sec / 60, 2) if duration_sec else 0,
        "view_count": stats.get("viewCount"),
        "like_count": stats.get("likeCount"),
        "comment_count": stats.get("commentCount"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    api_keys_raw = os.environ.get(cfg["api_key_env"], "")
    api_keys = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
    key_pool = KeyPool(api_keys)

    df_in = pd.read_csv(cfg["input_csv"])
    if "video_url" not in df_in.columns:
        sys.exit("입력 CSV에 'video_url' 컬럼이 없습니다.")

    df_in["video_id"] = df_in["video_url"].apply(extract_video_id)
    df_in = df_in.dropna(subset=["video_id"]).drop_duplicates(subset=["video_id"])
    url_by_id = dict(zip(df_in["video_id"], df_in["video_url"]))
    all_ids = list(url_by_id.keys())
    print(f"[info] 수집 대상 영상 수: {len(all_ids)}")

    # 체크포인트에서 이미 수집한 video_id는 건너뛰기
    checkpoint_path = cfg["checkpoint_csv"]
    collected_rows = []
    already_done = set()
    if os.path.exists(checkpoint_path):
        df_ckpt = pd.read_csv(checkpoint_path)
        collected_rows = df_ckpt.to_dict("records")
        already_done = set(df_ckpt["video_id"].astype(str))
        print(f"[info] 체크포인트에서 {len(already_done)}개 이미 수집됨 -> 건너뜀")

    todo_ids = [vid for vid in all_ids if vid not in already_done]
    print(f"[info] 이번에 수집할 영상 수: {len(todo_ids)}")

    batch_size = cfg.get("batch_size", 50)
    delay = cfg.get("request_delay_sec", 0.2)
    save_every = cfg.get("save_every_n_batches", 5)

    batches = [todo_ids[i : i + batch_size] for i in range(0, len(todo_ids), batch_size)]

    for b_idx, batch in enumerate(batches, start=1):
        print(f"[info] 배치 {b_idx}/{len(batches)} ({len(batch)}개) 수집 중...")
        items_by_id = fetch_batch(batch, key_pool, cfg)

        for vid in batch:
            record = build_record(vid, url_by_id[vid], items_by_id.get(vid))
            collected_rows.append(record)

        time.sleep(delay)

        if b_idx % save_every == 0 or b_idx == len(batches):
            pd.DataFrame(collected_rows).to_csv(checkpoint_path, index=False)
            print(f"  [checkpoint] {len(collected_rows)}건 저장 -> {checkpoint_path}")

    # 최종 결과 저장
    df_out = pd.DataFrame(collected_rows)
    df_out.to_csv(cfg["output_csv"], index=False)
    print(f"[done] 총 {len(df_out)}건 -> {cfg['output_csv']}")
    print(f"[done] 완료 시각: {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
