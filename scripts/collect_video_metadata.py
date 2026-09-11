"""
collect_video_metadata.py

video_url 컬럼이 있는 CSV를 읽어서, 각 영상의 title / description / channel_title 등을
YouTube Data API v3 (videos.list, part=snippet)로 수집해 CSV로 저장하는 스크립트.

특징
- 한 번에 최대 50개 video_id를 묶어서 요청 (API 쿼터를 아낌: 요청 1건 = 1 unit)
- API 키 여러 개를 콤마/줄바꿈으로 구분해서 넣으면, quota 초과(403) 시 자동으로 다음 키로 전환
- 중간에 quota가 다 떨어지거나 GitHub Actions 실행 시간이 끝나도, checkpoint_csv에 지금까지
  수집한 결과가 남아있어서 다음 실행에서 이어서 진행됨 (이미 수집된 video_id는 다시 요청하지 않음)
- 삭제/비공개 등으로 조회가 안 되는 영상은 status="not_found"로 표시하고 넘어감

사용법 (로컬 테스트):
    export YOUTUBE_API_KEYS="key1,key2,key3"
    python scripts/collect_video_metadata.py --config config.yml
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

FIELDNAMES = [
    "video_url",
    "video_id",
    "status",  # ok / not_found / error
    "title",
    "description",
    "channel_title",
    "channel_id",
    "published_at",
]


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_api_keys() -> list[str]:
    """
    YOUTUBE_API_KEYS 환경변수에서 API 키 목록을 읽어온다.
    콤마(,) 또는 줄바꿈으로 구분된 여러 키를 지원 (기존 broadway 파이프라인과 동일한 방식).
    """
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
        # video_url 컬럼명이 다를 경우를 대비해 첫 컬럼도 fallback으로 사용
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
    """API 키 목록을 순환하며, quota 초과 시 다음 키로 넘어간다."""

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


def fetch_batch(video_ids: list[str], key_pool: KeyPool) -> tuple[dict, bool]:
    """
    최대 50개 video_id에 대해 videos.list 호출.
    반환: (video_id -> snippet dict, 모든 키 소진 여부)
    """
    params_ids = ",".join(video_ids)
    while True:
        key = key_pool.current()
        if key is None:
            return {}, True  # 모든 키의 quota가 소진됨

        resp = requests.get(
            API_URL,
            params={"part": "snippet", "id": params_ids, "key": key},
            timeout=30,
        )

        if resp.status_code == 200:
            data = resp.json()
            found = {}
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                found[item["id"]] = {
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "published_at": snippet.get("publishedAt", ""),
                }
            return found, False

        if resp.status_code in (403, 429):
            # quota 초과로 추정 -> 다음 키로 전환
            print(f"  키 quota 초과/거부(status={resp.status_code}), 다음 키로 전환")
            new_key = key_pool.rotate()
            if new_key is None:
                return {}, True
            continue

        # 그 외 에러(일시적 네트워크 오류 등)는 한 번 재시도
        print(f"  요청 실패(status={resp.status_code}): {resp.text[:200]}")
        time.sleep(2)
        resp2 = requests.get(
            API_URL,
            params={"part": "snippet", "id": params_ids, "key": key},
            timeout=30,
        )
        if resp2.status_code == 200:
            data = resp2.json()
            found = {}
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                found[item["id"]] = {
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "published_at": snippet.get("publishedAt", ""),
                }
            return found, False
        # 두 번째도 실패하면 이 배치는 건너뜀 (개별 video는 아래에서 not_found 처리)
        return {}, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    keys = load_api_keys()
    key_pool = KeyPool(keys)

    urls = read_urls(cfg["input_csv"])
    print(f"입력 URL {len(urls)}개")

    # url -> video_id, video_id -> url 목록 (중복 url이 같은 video_id를 가리킬 수 있음)
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
            snippet = found.get(vid)
            for u in vid_to_urls[vid]:
                if snippet:
                    rows.append(
                        {
                            "video_url": u,
                            "video_id": vid,
                            "status": "ok",
                            **snippet,
                        }
                    )
                else:
                    rows.append(
                        {
                            "video_url": u,
                            "video_id": vid,
                            "status": "not_found",
                            "title": "",
                            "description": "",
                            "channel_title": "",
                            "channel_id": "",
                            "published_at": "",
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

    # video_id를 못 뽑아낸(URL 형식이 이상한) 행도 결과에 남기기
    novid_rows = []
    for u, vid in url_to_vid.items():
        if vid is None:
            novid_rows.append(
                {
                    "video_url": u,
                    "video_id": "",
                    "status": "invalid_url",
                    "title": "",
                    "description": "",
                    "channel_title": "",
                    "channel_id": "",
                    "published_at": "",
                }
            )
    if novid_rows:
        append_checkpoint(cfg["checkpoint_csv"], novid_rows)
        print(f"video_id를 추출할 수 없는 URL {len(novid_rows)}개 기록됨")

    # 체크포인트를 최종 output_csv로 복사 (그대로 최종 산출물로 사용)
    Path(cfg["output_csv"]).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(cfg["checkpoint_csv"]):
        with open(cfg["checkpoint_csv"], "r", encoding="utf-8") as src, open(
            cfg["output_csv"], "w", encoding="utf-8"
        ) as dst:
            dst.write(src.read())

    remaining = len(pending_vids) - batches_done * batch_size
    if quota_exhausted and remaining > 0:
        print(f"미완료 video_id 약 {max(remaining, 0)}개 남음 -> 다음 실행에서 재개")
        sys.exit(78)  # GitHub Actions에서 "부분 완료" 상태를 구분하기 위한 특수 종료 코드
    else:
        print("모든 video_id 처리 완료")


if __name__ == "__main__":
    main()
