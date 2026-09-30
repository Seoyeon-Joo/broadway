"""
youtube_collect_hollywood.py
==============================
Broadway의 youtube_collect_condensed.py를 영화(Box Office Mojo 주간 데이터)용으로
옮긴 스크립트. data/hollywood_youtube_targets.csv(영화 1편 = 1행)를 입력받아
condensed content 중심 5개 카테고리 쿼리로 YouTube를 검색한다.

Broadway 스크립트와 그대로 같은 부분
-----------------------------------
  - KeyPool(키 순환 + "API key not valid" 죽은 키 영구 스킵), robust_get(네트워크
    재시도 상한), 적응형 페이지네이션, /shorts/{id} 리다이렉트로 is_shorts 판별
  - 체크포인트(.processed.txt) CSV 형식: movie_id,n_collected,limit_used
    (limit_per_show를 올려서 다시 돌리면 상한에 걸렸던 영화만 재수집)
  - query_category -> is_condensed(1~5) 매핑
  - 영상/채널 컬럼명은 broadway_youtube_merged_cleaned_v5.csv 표기를 그대로 유지
    (vedio_url, published_date, duration(m:ss), "comment_count "(뒤 공백 포함))
    -> Broadway 결과와 행 단위로 바로 이어붙여 비교할 수 있게 함

영화용으로 바뀐 부분
-------------------
  - 검색 제목: search_title (재개봉 꼬리표 "2024 Re-release" 등을 뗀 제목)
  - 쿼리: "movie recap", "ending explained" 등 영화 요약 채널이 실제로 쓰는 표현
  - title_ambiguous=True인 영화는 검색어 뒤에 개봉 연도를 붙임 (예: "Nobody" 2021)
    단, 재개봉작은 원작 개봉 연도를 모르므로 연도를 붙이지 않음
  - 날짜 필터: 개봉 365일 전보다 이전에 올라온 영상은 동명 다른 영화로 보고 제외
    (영화 트레일러는 보통 개봉 6~12개월 전에 나옴). 재개봉작은 원작 영상이
    몇 년 전 것일 수 있으므로 날짜 필터를 적용하지 않음
  - Broadway의 run_id/show/opening_date/closing_date 대신
    movie_id/movie_title/release_date/last_week_date 사용

수집 필드
---------
movie_id, movie_title, search_title, is_rerelease, distributor,
release_date, last_week_date,
query_used, query_category, is_condensed,
video_id, video_title, description, channel_id, channel,
published_date, duration, duration_sec, duration_min,
is_shorts_guess, is_shorts,
view_count, like_count, "comment_count ",
channel_subscriber_count, channel_video_count, hidden_subscriber_count,
vedio_url, days_since_release, is_post_last_week

사용 예시
--------
    python scripts/youtube_collect_hollywood.py \\
        --targets data/hollywood_youtube_targets.csv \\
        --out data/youtube_hollywood/shard_0.csv \\
        --shard-index 0 --num-shards 20 \\
        --limit 50
"""
import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime

import requests

API_BASE = "https://www.googleapis.com/youtube/v3"
YT_BASE = "https://www.youtube.com"

FIELDNAMES = [
    "movie_id", "movie_title", "search_title", "is_rerelease", "distributor",
    "release_date", "last_week_date",
    "query_used", "query_category", "is_condensed",
    "video_id", "video_title", "description", "channel_id", "channel",
    "published_date", "duration", "duration_sec", "duration_min",
    "is_shorts_guess", "is_shorts",
    "view_count", "like_count", "comment_count ",
    "channel_subscriber_count", "channel_video_count", "hidden_subscriber_count",
    "vedio_url", "days_since_release", "is_post_last_week",
]

# Broadway와 같은 라벨 체계 (영화/공연 비교 시 같은 코드로 묶을 수 있게)
QUERY_CATEGORY_TO_IS_CONDENSED = {
    "condensed": "1",
    "review": "2",
    "full_uncut": "3",
    "trailer": "4",
    "highlights": "5",
}

# *** 영화용 condensed content 쿼리 - 5개 카테고리 ***
# {title}은 큰따옴표로 감싸서 제목 전체가 포함된 영상 위주로 걸리게 함.
# 검색 의도별로 편향시킨 것일 뿐, 실제 콘텐츠 유형은 title 정규식/분류 단계에서 재검증 필요.
QUERY_CATEGORIES = {
    "condensed": [
        '"{title}" movie recap',
        '"{title}" recap',
        '"{title}" in minutes',
        '"{title}" in 10 minutes',
        '"{title}" movie summary',
        '"{title}" plot summary',
        '"{title}" story explained',
        '"{title}" movie explained',
        '"{title}" ending explained',
        '"{title}" full movie recap',
    ],
    "review": [
        '"{title}" movie review',
        '"{title}" review',
        '"{title}" non spoiler review',
        '"{title}" spoiler review',
    ],
    "trailer": [
        '"{title}" official trailer',
        '"{title}" teaser trailer',
        '"{title}" final trailer',
    ],
    "full_uncut": [
        '"{title}" full movie',
        '"{title}" full movie english',
        '"{title}" full film',
    ],
    "highlights": [
        # 줄거리 전체 압축(condensed)이 아니라 일부 장면만 발췌한 클립 -
        # Broadway의 highlights와 같은 역할 (스튜디오 공식 클립 채널이 많음)
        '"{title}" movie clip',
        '"{title}" clip',
        '"{title}" scene',
        '"{title}" best scenes',
    ],
}

PRE_RELEASE_BUFFER_DAYS = 365

SHORTS_CANDIDATE_MAX_SEC = 180  # Shorts 최대 길이(2024~ 3분) 이하만 URL로 최종 확인


class QuotaExceededError(Exception):
    pass


def iso8601_duration_to_seconds(duration):
    if not duration:
        return 0
    m = re.match(
        r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration
    )
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def seconds_to_mmss(sec):
    """v5.csv의 duration 표기(m:ss, 1시간 넘으면 h:mm:ss)와 동일한 형식으로 변환."""
    if not sec or sec <= 0:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class KeyPool:
    """콤마로 이어붙인 여러 API 키를 순환하며 429/quota 오류 시 다음 키로 넘어감.
    한 번 "API key not valid"로 확인된 키는 dead 세트에 넣고 이후 완전히 건너뜀."""

    def __init__(self, keys):
        self.keys = keys
        self.idx = 0
        self.dead = set()

    def current(self):
        n = len(self.keys)
        checked = 0
        while checked < n:
            k = self.keys[self.idx % n]
            if k not in self.dead:
                return k
            self.idx += 1
            checked += 1
        raise QuotaExceededError("모든 키가 죽었거나(dead) 소진됨")

    def rotate(self):
        self.idx += 1
        return self.current()

    def mark_dead(self, key):
        self.dead.add(key)


def robust_get(session, url, params, key_pool, max_cycles=3, max_network_retries=8):
    """429/quota 오류 시 키를 순환하며 재시도, 그 외 네트워크 오류는 지수 백오프
    (최대 max_network_retries회 - 예전엔 상한이 없어서 네트워크가 불안정하면
    조용히 계속 도는 버그가 있었음, 실행 로그가 안 찍혀서 '멈춘 것처럼' 보일 수 있음)."""
    cycles = 0
    backoff = 1.0
    network_retries = 0
    while True:
        params = dict(params)
        params["key"] = key_pool.current()
        try:
            resp = session.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            network_retries += 1
            if network_retries > max_network_retries:
                print(f"    [네트워크 오류 {max_network_retries}회 초과, 이 쿼리 포기] {e}")
                return {"error": {"message": f"network retries exceeded: {e}"}}
            print(f"    [네트워크 오류 {network_retries}/{max_network_retries}] {e} - "
                  f"{backoff:.0f}초 후 재시도")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if resp.status_code == 200:
            return resp.json()

        try:
            err_msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            err_msg = resp.text[:200]

        is_key_invalid = resp.status_code == 400 and "API key not valid" in err_msg

        if is_key_invalid:
            dead_key = params["key"]
            key_pool.mark_dead(dead_key)
            print(f"    [죽은 키 감지, 영구 스킵] index={key_pool.idx % len(key_pool.keys)} "
                  f"(현재 dead 키 {len(key_pool.dead)}개)")
            key_pool.rotate()
            cycles += 1
            if cycles > max_cycles * len(key_pool.keys):
                raise QuotaExceededError(f"모든 키 무효/소진 추정: {err_msg}")
            continue

        if resp.status_code in (403, 429) and (
            "quota" in err_msg.lower() or resp.status_code == 429
        ):
            key_pool.rotate()
            cycles += 1
            if cycles % len(key_pool.keys) == 0:
                print(f"    [전체 키 소진 {cycles // len(key_pool.keys)}회차] "
                      f"{backoff:.0f}초 대기 후 계속")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            if cycles > max_cycles * len(key_pool.keys):
                raise QuotaExceededError(f"모든 키 quota 소진 추정: {err_msg}")
            continue

        print(f"    [API 오류 {resp.status_code}] {err_msg}")
        return {"error": {"message": err_msg}}


def search_videos(session, key_pool, query, max_pages=6, results_per_page=50, target_count=None):
    """search.list는 호출 1번에 100유닛 고정. 적응형 페이지네이션 +
    target_count 도달 시 즉시 중단 (youtube_collect_broadway.py와 동일 로직)."""
    video_ids = []
    page_token = None
    for _ in range(max_pages):
        if target_count is not None and len(video_ids) >= target_count:
            break
        params = {
            "q": query,
            "part": "snippet",
            "type": "video",
            "maxResults": min(results_per_page, 50),
            "relevanceLanguage": "en",
            "regionCode": "US",
            "order": "relevance",
        }
        if page_token:
            params["pageToken"] = page_token
        data = robust_get(session, f"{API_BASE}/search", params, key_pool)
        if "error" in data:
            break
        items = data.get("items", [])
        for item in items:
            vid = item.get("id", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = data.get("nextPageToken")
        page_was_full = len(items) >= min(results_per_page, 50)
        if not page_token or not page_was_full:
            break
    return video_ids


def get_video_details(session, key_pool, video_ids):
    results = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        params = {
            "id": ",".join(batch),
            "part": "snippet,contentDetails,statistics",
        }
        data = robust_get(session, f"{API_BASE}/videos", params, key_pool)
        if "error" in data:
            continue
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            cd = item.get("contentDetails", {})
            st = item.get("statistics", {})
            duration_sec = iso8601_duration_to_seconds(cd.get("duration", ""))
            results[item["id"]] = {
                "video_title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "channel_id": sn.get("channelId", ""),
                "channel": sn.get("channelTitle", ""),
                "published_date": sn.get("publishedAt", ""),
                "duration": seconds_to_mmss(duration_sec),
                "duration_sec": duration_sec,
                "duration_min": round(duration_sec / 60, 2),
                "is_shorts_guess": 1 if (duration_sec > 0 and duration_sec <= 60) else 0,
                "view_count": st.get("viewCount", ""),
                "like_count": st.get("likeCount", ""),
                "comment_count ": st.get("commentCount", ""),
            }
        time.sleep(0.1)
    return results


def check_is_shorts(session, video_id, timeout=10):
    """/shorts/{video_id}를 리다이렉트 없이 요청해서 실제 Shorts인지 확인.
    Shorts면 200 그대로, 일반 영상이면 /watch?v=로 301/302 리다이렉트됨.
    네트워크 오류/타임아웃 시에는 판별 불가로 빈 문자열 반환(추정치인
    is_shorts_guess로 대체 사용하도록 QA 단계에서 처리)."""
    try:
        resp = session.head(
            f"{YT_BASE}/shorts/{video_id}", allow_redirects=False, timeout=timeout
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            return False
        if resp.status_code == 200:
            return True
        # HEAD가 막힌 서버면 GET으로 한 번 더 (redirect 추적 안 함)
        resp = session.get(
            f"{YT_BASE}/shorts/{video_id}", allow_redirects=False, timeout=timeout
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            return False
        return resp.status_code == 200
    except requests.RequestException:
        return ""


def get_channel_details(session, key_pool, channel_ids):
    results = {}
    unique_ids = list(dict.fromkeys(channel_ids))
    for i in range(0, len(unique_ids), 50):
        batch = unique_ids[i:i + 50]
        params = {"id": ",".join(batch), "part": "statistics"}
        data = robust_get(session, f"{API_BASE}/channels", params, key_pool)
        if "error" in data:
            continue
        for item in data.get("items", []):
            st = item.get("statistics", {})
            results[item["id"]] = {
                "channel_subscriber_count": st.get("subscriberCount", ""),
                "channel_video_count": st.get("videoCount", ""),
                "hidden_subscriber_count": st.get("hiddenSubscriberCount", ""),
            }
        time.sleep(0.1)
    return results


def load_targets(path, shard_index, num_shards):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i % num_shards == shard_index:
                rows.append(row)
    return rows


def load_processed(path, current_limit_per_show):
    """체크포인트에서 '다 끝난' movie_id 집합을 읽음. 이전 실행이 limit_per_show
    상한에 걸려서 멈췄고 이번 상한이 더 크면 재수집 대상으로 남겨둠 (Broadway와 동일)."""
    if not os.path.isfile(path):
        return set()

    fully_done = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                n_collected = int(row.get("n_collected", 0))
                limit_used = int(row.get("limit_used", 0))
            except (TypeError, ValueError):
                fully_done.add(row["movie_id"])
                continue
            was_capped = n_collected >= limit_used and limit_used > 0
            if was_capped and current_limit_per_show > limit_used:
                continue
            fully_done.add(row["movie_id"])
    return fully_done


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def is_true(v):
    return str(v).strip().lower() in ("true", "1", "yes")


def build_queries_for_target(target):
    """(query_string, category) 튜플 리스트 반환.
    title_ambiguous이고 재개봉작이 아니면 따옴표 뒤에 개봉 연도를 붙여 동명 영화 혼입을 줄임."""
    title = target["search_title"]
    year_suffix = ""
    if is_true(target.get("title_ambiguous")) and not is_true(target.get("is_rerelease")):
        year = str(target.get("release_year", "")).strip()
        if year and year.lower() != "nan":
            year_suffix = f" {year}"

    queries = []
    for category, templates in QUERY_CATEGORIES.items():
        for t in templates:
            q = t.format(title=title.replace('"', ""))
            if year_suffix:
                q = q.replace('" ', f'"{year_suffix} ', 1)
            queries.append((q, category))
    return queries


def video_belongs_to_movie(published, release, is_rerelease):
    if is_rerelease or not published or not release:
        return True
    return (published - release).days >= -PRE_RELEASE_BUFFER_DAYS


def process_target(session, key_pool, target, limit_per_show, csv_writer,
                   fetched_details_cache, written_pairs, max_pages_per_query=2):
    movie_id = target["movie_id"]
    release = parse_date(target.get("release_date", ""))
    last_week = parse_date(target.get("last_week_date", ""))
    rerelease = is_true(target.get("is_rerelease"))

    all_hits = {}  # video_id -> (query_used, category)  (첫 매칭 우선)
    for query, category in build_queries_for_target(target):
        remaining = limit_per_show - len(all_hits)
        if remaining <= 0:
            break
        ids = search_videos(session, key_pool, query, max_pages=max_pages_per_query,
                            target_count=remaining)
        for vid in ids:
            if vid not in all_hits:
                all_hits[vid] = (query, category)
        time.sleep(0.1)

    ids_to_fetch = [v for v in all_hits if v not in fetched_details_cache]
    if ids_to_fetch:
        fetched_details_cache.update(get_video_details(session, key_pool, ids_to_fetch))

    # Shorts 후보(duration<=180초)만 /shorts/ URL로 최종 확인 (API 유닛 안 씀)
    for vid in ids_to_fetch:
        d = fetched_details_cache.get(vid)
        if d and 0 < d.get("duration_sec", 0) <= SHORTS_CANDIDATE_MAX_SEC:
            shorts_result = check_is_shorts(session, vid)
            d["is_shorts"] = 1 if shorts_result is True else 0
            time.sleep(0.05)
        elif d:
            d["is_shorts"] = 0

    channel_ids = [
        fetched_details_cache[v]["channel_id"]
        for v in all_hits
        if v in fetched_details_cache and fetched_details_cache[v].get("channel_id")
    ]
    channel_details = get_channel_details(session, key_pool, channel_ids)

    n_written = 0
    for vid, (query_used, category) in all_hits.items():
        if (movie_id, vid) in written_pairs:
            continue
        d = fetched_details_cache.get(vid)
        if not d:
            continue
        published = parse_date(d.get("published_date", ""))
        if not video_belongs_to_movie(published, release, rerelease):
            continue

        days_since_release = (published - release).days if published and release else ""
        is_post_last_week = (
            bool(published > last_week) if published and last_week else ""
        )
        ch = channel_details.get(d["channel_id"], {})
        row = {
            "movie_id": movie_id,
            "movie_title": target.get("movie_title", ""),
            "search_title": target.get("search_title", ""),
            "is_rerelease": target.get("is_rerelease", ""),
            "distributor": target.get("distributor", ""),
            "release_date": target.get("release_date", ""),
            "last_week_date": target.get("last_week_date", ""),
            "query_used": query_used,
            "query_category": category,
            "is_condensed": QUERY_CATEGORY_TO_IS_CONDENSED.get(category, ""),
            "video_id": vid,
            "vedio_url": f"https://www.youtube.com/watch?v={vid}",
            "days_since_release": days_since_release,
            "is_post_last_week": is_post_last_week,
            **d,
            **ch,
        }
        csv_writer.writerow({k: row.get(k, "") for k in FIELDNAMES})
        written_pairs.add((movie_id, vid))
        n_written += 1
    return n_written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="data/hollywood_youtube_targets.csv")
    ap.add_argument("--out", default="data/youtube_hollywood/shard_0.csv")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=50, help="이번 실행에서 처리할 최대 영화 수")
    ap.add_argument("--limit-per-show", type=int, default=200,
                    help="영화 1편당 최대 수집 영상 수 (Broadway 옵션명 그대로 유지)")
    ap.add_argument("--max-pages-per-query", type=int, default=2)
    ap.add_argument("--movie-ids", default="",
                    help="테스트용: 콤마로 구분한 movie_id만 수집 (예: 1,14,576)")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    api_keys_env = os.environ.get("YOUTUBE_API_KEYS", "")
    if args.api_key:
        keys = [args.api_key]
    elif api_keys_env:
        keys = [k.strip() for k in api_keys_env.split(",") if k.strip()]
    else:
        print("API 키가 없어요. --api-key 또는 환경변수 YOUTUBE_API_KEYS를 설정하세요.")
        sys.exit(1)

    key_pool = KeyPool(keys)
    session = requests.Session()

    checkpoint_path = args.checkpoint or (os.path.splitext(args.out)[0] + ".processed.txt")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    targets = load_targets(args.targets, args.shard_index, args.num_shards)
    if args.movie_ids:
        wanted = {x.strip() for x in args.movie_ids.split(",") if x.strip()}
        targets = [t for t in targets if t["movie_id"] in wanted]
    processed = load_processed(checkpoint_path, args.limit_per_show)
    remaining = [t for t in targets if t["movie_id"] not in processed]
    print(f"[shard {args.shard_index}/{args.num_shards}] 총 {len(targets)}편 중 "
          f"{len(remaining)}편 미처리(재시도 대상 포함), 이번 실행 한도 {args.limit}편")

    if not remaining:
        print("이 shard는 이미 다 수집됐어요.")
        return

    out_exists = os.path.isfile(args.out)
    written_pairs = set()
    if out_exists:
        with open(args.out, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            existing_header = reader.fieldnames or []
            if existing_header != FIELDNAMES:
                print(
                    "오류: 기존 출력 파일의 헤더가 지금 스크립트의 FIELDNAMES와 달라요.\n"
                    f"  파일: {args.out}\n"
                    f"  파일 헤더 ({len(existing_header)}개): {existing_header}\n"
                    f"  현재 FIELDNAMES ({len(FIELDNAMES)}개): {FIELDNAMES}\n"
                    "  -> Release에서 이 shard 파일과 .processed.txt를 지우고 다시 수집하세요."
                )
                sys.exit(1)
            for row in reader:
                written_pairs.add((row.get("movie_id", ""), row.get("video_id", "")))

    fetched_details_cache = {}
    mode = "a" if out_exists else "w"
    checkpoint_is_new = not os.path.isfile(checkpoint_path)
    with open(args.out, mode, newline="", encoding="utf-8-sig") as out_f, \
         open(checkpoint_path, "a", newline="", encoding="utf-8") as ckpt_f:
        writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES)
        if not out_exists:
            writer.writeheader()
        ckpt_writer = csv.writer(ckpt_f)
        if checkpoint_is_new:
            ckpt_writer.writerow(["movie_id", "n_collected", "limit_used"])

        n_done = 0
        for target in remaining:
            if n_done >= args.limit:
                break
            try:
                n_rows = process_target(
                    session, key_pool, target, args.limit_per_show, writer,
                    fetched_details_cache, written_pairs,
                    max_pages_per_query=args.max_pages_per_query,
                )
            except QuotaExceededError as e:
                print(f"  중단: {e}")
                break
            out_f.flush()
            n_total = sum(1 for (mid, _) in written_pairs if mid == target["movie_id"])
            print(f"  [{n_done+1}/{min(len(remaining), args.limit)}] "
                  f"'{target['search_title']}' (movie_id={target['movie_id']}) -> "
                  f"{n_rows}건 신규 (누적 {n_total}건)")
            ckpt_writer.writerow([target["movie_id"], n_total, args.limit_per_show])
            ckpt_f.flush()
            n_done += 1
            if len(fetched_details_cache) > 5000:
                fetched_details_cache.clear()

    print(f"완료: {n_done}편 처리, 결과 -> {args.out}")


if __name__ == "__main__":
    main()
