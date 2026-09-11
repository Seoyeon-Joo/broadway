"""
collect_video_stats.py

video_url 컬럼이 있는 CSV를 읽어서, 각 영상의 재생시간(duration) / 조회수(view_count) /
좋아요수(like_count) / 댓글수(comment_count)를
YouTube Data API v3 (videos.list, part=contentDetails,statistics)로 수집해 CSV로 저장하는 스크립트.

collect_video_metadata.py(title/description/channel 수집용)와 구조는 거의 동일하고,
요청하는 part와 저장하는 필드만 다릅니다. 두 스크립트를 각자 원하는 스케줄로 따로 돌리면 됩니다.

특징
- 한 번에 최대 50개 video_id를 묶어서 요청 (요청 1건 = 1 unit)
- API 키 여러 개를 콤마/줄바꿈으로 구분해서 넣으면, quota 초과(403) 시 자동으로 다음 키로 전환
- 중간에 멈춰도 checkpoint_csv 덕분에 다음 실행에서 이어서 진행 (이미 수집된 video_id는 재요청 안 함)
- 삭제/비공개 등으로 조회가 안 되는 영상은 status="not_found"로 표시
- duration은 API가 주는 ISO 8601 형식(예: PT1M4S)을 기존 데이터와 같은 "0:22", "1:04:09" 형식으로 변환

사용법 (로컬 테스트):
    export YOUTUBE_API_KEYS="key1,key2,key3"
    python scripts/collect_video_stats.py --config config_video_stats.yml
"""

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

API_URL = "https://www.googleapis.com/youtube/v3/videos"
VIDEO_ID_PATTERNS = [
    re.compile(r"(?:v=|/videos/|youtu\.be/|/v/|/embed/)([A-Za-z0-9_-]{11})"),
]

ISO8601_DURATION_PAT = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)

FIELDNAMES = [
    "video_url",
    "video_id",
    "status",  # ok / not_found / error
    "duration",
    "view_count",
    "like_count",
    "comment_count",
]


def iso8601_to_duration_str(iso: str) -> str:
    """'PT1M4S' -> '1:04', 'PT1H2M3S' -> '1:02:03', 'PT45S' -> '0:45'"""
    if not iso:
        return ""
    m = ISO8601_DURATION_PAT.match(iso)
    if not m:
        return iso  # 형식이 예상과 다르면 원본 그대로 반환
    h = int(m.group("hours") or 0)
    mi = int(m.group("minutes") or 0)
    s = int(m.group("seconds") or 0)
    if h > 0:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_api_keys() -> list[str]:
    raw = os.environ.get("YOUTUBE_API_KEYS", "").strip()
    if not raw:
        print("ERROR: YOUTUBE_API_KEYS 환경변수가 비어있습니다.", file=sys.stderr)
        sys.exit(1)
    keys = [k.strip() for chunk in raw.split("\n") for k in chunk.split(",")]
    keys = [k for k in keys if k]
    if not keys:
        print("ERROR: 유효한 API 키를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)
    print(f"API 키 {len(keys)}개 로드됨")
    return keys


def extract_video_id(url: str) -> str | None:
    if not url:
        return None
    for pat in VIDEO_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def read_urls(input_csv: str) -> list[str]:
    urls = []
    with open(input_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        col = "video_url" if "video_url" in reader.fieldnames else reader.fieldnames[0]
        for row in reader:
            u = (row.get(col) or "").strip()
            if u:
                urls.append(u)
    return urls


def load_checkpoint(checkpoint_csv: str) -> dict[str, dict]:
    done = {}
    if not os.path.exists(checkpoint_csv):
        return done
    with open(checkpoint_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            vid = row.get("video_id")
            if vid:
                done[vid] = row
    print(f"체크포인트에서 이미 수집된 video_id {len(done)}개 로드됨")
    return done


def append_checkpoint(checkpoint_csv: str, rows: list[dict]) -> None:
    file_exists = os.path.exists(checkpoint_csv)
    Path(checkpoint_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(checkpoint_csv, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


class KeyPool:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.idx = 0
        self.dead = set()

    def current(self) -> str | None:
        if len(self.dead) >= len(self.keys):
            return None
        return self.keys[self.idx]

    def rotate(self) -> str | None:
        self.dead.add(self.keys[self.idx])
        for _ in range(len(self.keys)):
            self.idx = (self.idx + 1) % len(self.keys)
            if self.keys[self.idx] not in self.dead:
                return self.keys[self.idx]
        return None


def _parse_items(data: dict) -> dict:
    found = {}
    for item in data.get("items", []):
        content = item.get("contentDetails", {})
        stats = item.get("statistics", {})
        found[item["id"]] = {
            "duration": iso8601_to_duration_str(content.get("duration", "")),
            "view_count": stats.get("viewCount", ""),
            "like_count": stats.get("likeCount", ""),  # 좋아요 비공개 영상은 빈 값
            "comment_count": stats.get("commentCount", ""),  # 댓글 비활성 영상은 빈 값
        }
    return found


def fetch_batch(video_ids: list[str], key_pool: KeyPool) -> tuple[dict, bool]:
    params_ids = ",".join(video_ids)
    while True:
        key = key_pool.current()
        if key is None:
            return {}, True

        resp = requests.get(
            API_URL,
            params={"part": "contentDetails,statistics", "id": params_ids, "key": key},
            timeout=30,
        )

        if resp.status_code == 200:
            return _parse_items(resp.json()), False

        if resp.status_code in (403, 429):
            print(f"  키 quota 초과/거부(status={resp.status_code}), 다음 키로 전환")
            new_key = key_pool.rotate()
            if new_key is None:
                return {}, True
            continue

        print(f"  요청 실패(status={resp.status_code}): {resp.text[:200]}")
        time.sleep(2)
        resp2 = requests.get(
            API_URL,
            params={"part": "contentDetails,statistics", "id": params_ids, "key": key},
            timeout=30,
        )
        if resp2.status_code == 200:
            return _parse_items(resp2.json()), False
        return {}, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config_video_stats.yml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    keys = load_api_keys()
    key_pool = KeyPool(keys)

    urls = read_urls(cfg["input_csv"])
    print(f"입력 URL {len(urls)}개")

    url_to_vid = {}
    vid_to_urls: dict[str, list[str]] = {}
    for u in urls:
        vid = extract_video_id(u)
        url_to_vid[u] = vid
        if vid:
            vid_to_urls.setdefault(vid, []).append(u)

    done = load_checkpoint(cfg["checkpoint_csv"])
    pending_vids = [v for v in vid_to_urls.keys() if v not in done]
    print(f"이미 수집됨: {len(done)}개 / 남은 작업: {len(pending_vids)}개")

    batch_size = cfg.get("batch_size", 50)
    sleep_seconds = cfg.get("sleep_seconds", 0.5)
    max_batches = cfg.get("max_batches_per_run", 0)

    batches_done = 0
    quota_exhausted = False

    for i in range(0, len(pending_vids), batch_size):
        if max_batches and batches_done >= max_batches:
            print(f"max_batches_per_run({max_batches}) 도달, 이번 실행 종료")
            break

        batch_vids = pending_vids[i : i + batch_size]
        found, exhausted = fetch_batch(batch_vids, key_pool)

        rows = []
        for vid in batch_vids:
            stats = found.get(vid)
            for u in vid_to_urls[vid]:
                if stats:
                    rows.append({"video_url": u, "video_id": vid, "status": "ok", **stats})
                else:
                    rows.append(
                        {
                            "video_url": u,
                            "video_id": vid,
                            "status": "not_found",
                            "duration": "",
                            "view_count": "",
                            "like_count": "",
                            "comment_count": "",
                        }
                    )
        append_checkpoint(cfg["checkpoint_csv"], rows)
        batches_done += 1
        print(f"배치 {batches_done} 완료 ({len(batch_vids)}개 video_id, 누적 {i + len(batch_vids)}/{len(pending_vids)})")

        if exhausted:
            print("모든 API 키의 quota가 소진되었습니다. 다음 스케줄 실행에서 이어서 진행됩니다.")
            quota_exhausted = True
            break

        time.sleep(sleep_seconds)

    novid_rows = []
    for u, vid in url_to_vid.items():
        if vid is None:
            novid_rows.append(
                {
                    "video_url": u,
                    "video_id": "",
                    "status": "invalid_url",
                    "duration": "",
                    "view_count": "",
                    "like_count": "",
                    "comment_count": "",
                }
            )
    if novid_rows:
        append_checkpoint(cfg["checkpoint_csv"], novid_rows)
        print(f"video_id를 추출할 수 없는 URL {len(novid_rows)}개 기록됨")

    Path(cfg["output_csv"]).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(cfg["checkpoint_csv"]):
        with open(cfg["checkpoint_csv"], "r", encoding="utf-8") as src, open(
            cfg["output_csv"], "w", encoding="utf-8"
        ) as dst:
            dst.write(src.read())

    remaining = len(pending_vids) - batches_done * batch_size
    if quota_exhausted and remaining > 0:
        print(f"미완료 video_id 약 {max(remaining, 0)}개 남음 -> 다음 실행에서 재개")
        sys.exit(78)
    else:
        print("모든 video_id 처리 완료")


if __name__ == "__main__":
    main()
