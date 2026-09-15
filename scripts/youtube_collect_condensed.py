"""
youtube_collect_condensed.py
==============================
youtube_collect_broadway.py와 같은 targets CSV(data/broadway_youtube_targets.csv)를
입력받지만, 범용 쿼리(트레일러/하이라이트/비하인드 등 20개 템플릿) 대신
"condensed content"를 찾기 위한 쿼리로 검색한다.

기존 스크립트와의 차이
---------------------
  - QUERY_TEMPLATES를 5개 카테고리(condensed / review / trailer / full_uncut / highlights)로 교체
  - 각 행에 query_category 컬럼 추가 -> 나중에 title 정규식 없이도 어떤 의도로
    검색되어 걸린 영상인지 바로 알 수 있음 (단, 실제 콘텐츠가 검색 의도와
    다를 수 있으니 최종 판별은 여전히 title 정규식 재검증 필요 - QA 단계 참고)
  - is_shorts_guess(duration<=60초 추정)에 더해 is_shorts 추가:
    duration<=180초인 후보만 /shorts/{video_id} URL을 direct 요청해서
    리다이렉트 여부로 실제 Shorts인지 최종 확인 (Shorts는 2024년부터 최대 3분)
  - 출력 컬럼명/형식을 broadway_youtube_merged_cleaned_v5.csv와 처음부터 동일하게
    맞춤(losing_date, vedio_url, channel, published_date, duration(m:ss),
    comment_count 뒤 공백까지) -> 별도 변환 스크립트 없이 바로 v5.csv에 이어붙이거나
    align_and_merge_condensed.py로 dedup 병합 가능. query_category -> is_condensed
    (1~5)도 수집 시점에 바로 채워짐

수집 필드
---------
run_id, show, theatre, opening_date, losing_date, date_source,
query_used, query_category, is_condensed,
video_id, video_title, description, channel_id, channel,
published_date, duration, duration_sec, duration_min,
is_shorts_guess, is_shorts,
view_count, like_count, "comment_count " (trailing space, v5 표기 그대로),
channel_subscriber_count, channel_video_count, hidden_subscriber_count,
vedio_url, days_since_opening, is_post_closing

사용 예시
--------
    python youtube_collect_condensed.py \\
        --targets data/broadway_youtube_targets.csv \\
        --out data/youtube_condensed/shard_0.csv \\
        --shard-index 0 --num-shards 20 \\
        --limit 80
"""
import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

API_BASE = "https://www.googleapis.com/youtube/v3"
YT_BASE = "https://www.youtube.com"

FIELDNAMES = [
    "run_id", "show", "theatre", "opening_date", "losing_date", "date_source",
    "query_used", "query_category", "is_condensed",
    "video_id", "video_title", "description", "channel_id", "channel",
    "published_date", "duration", "duration_sec", "duration_min",
    "is_shorts_guess", "is_shorts",
    "view_count", "like_count", "comment_count ",
    "channel_subscriber_count", "channel_video_count", "hidden_subscriber_count",
    "vedio_url", "days_since_opening", "is_post_closing",
]

# query_category -> is_condensed 매핑 (v5.csv의 기존 라벨 체계 그대로 확장)
#   condensed(1)/review(2)/full_uncut(3)은 v5에서 쓰던 값 그대로,
#   trailer(4)/highlights(5)는 이번에 새로 추가된 카테고리
QUERY_CATEGORY_TO_IS_CONDENSED = {
    "condensed": "1",
    "review": "2",
    "full_uncut": "3",
    "trailer": "4",
    "highlights": "5",
}

# *** condensed content 전용 쿼리 - 4개 카테고리 ***
# 카테고리별로 쿼리를 나눠서 query_category 컬럼에 기록. 검색 자체가 목적별로
# 편향되어 있을 뿐, 실제 콘텐츠가 그 카테고리인지는 title 정규식으로 재검증 필요.
QUERY_CATEGORIES = {
    "condensed": [
        '"{show}" in minutes',
        '"{show}" in 5 minutes',
        '"{show}" in 3 minutes',
        '"{show}" in 10 minutes',
        '"{show}" plot summary',
        '"{show}" summarized',
        '"{show}" summary',
        '"{show}" in a nutshell',
        '"{show}" condensed',
        '"{show}" TLDR',
        '"{show}" story explained',
        '"{show}" recap in minutes',
    ],
    "review": [
        '"{show}" Broadway review',
        '"{show}" musical review',
        '"{show}" review reaction',
        '"{show}" non spoiler review',
    ],
    "trailer": [
        '"{show}" official trailer',
        '"{show}" Broadway teaser trailer',
        '"{show}" first look trailer',
    ],
    "full_uncut": [
        '"{show}" full show',
        '"{show}" full musical',
        '"{show}" full performance',
        '"{show}" proshot',
        '"{show}" pro-shot',
        '"{show}" bootleg',
        '"{show}" complete performance',
    ],
    "highlights": [
        # condensed(전체 줄거리 압축)와는 다르게 일부 넘버/장면만 발췌한
        # 프로모션성 콘텐츠 - trailer에 가까운 성격이라 별도 카테고리로 분리.
        # 나중에 query_category로 condensed/trailer 어느 쪽에 합칠지 결정 가능.
        '"{show}" highlights',
        '"{show}" musical highlights',
        '"{show}" song highlights',
        '"{show}" play highlights',
    ],
}

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
    if not os.path.isfile(path):
        return set()

    fully_done = set()
    with open(path, newline="", encoding="utf-8") as f:
        first_line = f.readline()
        f.seek(0)
        is_csv = first_line.startswith("run_id,")
        if is_csv:
            for row in csv.DictReader(f):
                try:
                    n_collected = int(row.get("n_collected", 0))
                    limit_used = int(row.get("limit_used", 0))
                except (TypeError, ValueError):
                    fully_done.add(row["run_id"])
                    continue
                was_capped = n_collected >= limit_used and limit_used > 0
                if was_capped and current_limit_per_show > limit_used:
                    continue
                fully_done.add(row["run_id"])
        else:
            for line in f:
                line = line.strip()
                if line:
                    fully_done.add(line)
    return fully_done


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


PRE_OPENING_BUFFER_DAYS = {
    "history": 120,
    "show_level": 270,
    "show_level_ambiguous": 500,
    "week_range": 400,
}
DEFAULT_BUFFER_DAYS = 270


def build_queries_for_target(target):
    """(query_string, category) 튜플 리스트 반환. 리바이벌/동명 프로덕션 구분을
    위한 연도/주연배우 보강은 원본 스크립트와 동일하게 유지."""
    show = target["show"]
    is_revival = str(target.get("is_revival", "")).lower() == "true"
    title_ambiguous = str(target.get("title_ambiguous", "")).lower() == "true"
    opening = parse_date(target.get("opening_date", ""))
    lead_cast = target.get("lead_cast", "") or ""
    lead_actor = lead_cast.split(";")[0].strip() if lead_cast else ""

    year_suffix = ""
    if (is_revival or title_ambiguous) and opening:
        year_suffix = f" {opening.year}"

    queries = []
    for category, templates in QUERY_CATEGORIES.items():
        for t in templates:
            q = t.format(show=show)
            if year_suffix:
                q = q.replace(f'"{show}"', f'"{show}"{year_suffix}', 1)
            queries.append((q, category))
        if year_suffix and lead_actor and category == "condensed":
            # 리바이벌 condensed 검색은 주연 배우 이름도 하나 추가해 동명 프로덕션 오귀속 방지
            queries.append((f'"{show}" "{lead_actor}" in minutes', category))
    if target.get("theatre"):
        queries.insert(0, (f'"{show}" "{target["theatre"]}" condensed', "condensed"))
    return queries


def video_belongs_to_run(published, opening, date_source):
    if not published or not opening:
        return True
    buffer_days = PRE_OPENING_BUFFER_DAYS.get(date_source, DEFAULT_BUFFER_DAYS)
    return (published - opening).days >= -buffer_days


def process_target(session, key_pool, target, limit_per_show, csv_writer,
                    fetched_details_cache, written_pairs, max_pages_per_query=2):
    show = target["show"]
    run_id = target["run_id"]
    opening = parse_date(target.get("opening_date", ""))
    closing = parse_date(target.get("closing_date", ""))

    all_hits_this_run = {}  # video_id -> (query_used, category)  (첫 매칭 우선)
    for query, category in build_queries_for_target(target):
        remaining = limit_per_show - len(all_hits_this_run)
        if remaining <= 0:
            break
        ids = search_videos(session, key_pool, query, max_pages=max_pages_per_query,
                             target_count=remaining)
        for vid in ids:
            if vid not in all_hits_this_run:
                all_hits_this_run[vid] = (query, category)
        time.sleep(0.1)

    ids_to_fetch = [v for v in all_hits_this_run if v not in fetched_details_cache]
    if ids_to_fetch:
        fetched_details_cache.update(get_video_details(session, key_pool, ids_to_fetch))

    # Shorts 후보(duration<=180초)만 /shorts/ URL로 최종 확인 (API 유닛 안 씀, HTTP 요청만)
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
        for v in all_hits_this_run
        if v in fetched_details_cache and fetched_details_cache[v].get("channel_id")
    ]
    channel_details = get_channel_details(session, key_pool, channel_ids)

    n_written = 0
    for vid, (query_used, category) in all_hits_this_run.items():
        if (run_id, vid) in written_pairs:
            continue
        d = fetched_details_cache.get(vid)
        if not d:
            continue
        published = parse_date(d.get("published_date", ""))
        if not video_belongs_to_run(published, opening, target.get("date_source", "")):
            continue

        days_since_opening = (
            (published - opening).days if published and opening else ""
        )
        is_post_closing = (
            bool(published and closing and published > closing)
            if published and closing else ""
        )
        ch = channel_details.get(d["channel_id"], {})
        row = {
            "run_id": run_id,
            "show": show,
            "theatre": target.get("theatre", ""),
            "opening_date": target.get("opening_date", ""),
            "losing_date": target.get("closing_date", ""),
            "date_source": target.get("date_source", ""),
            "query_used": query_used,
            "query_category": category,
            "is_condensed": QUERY_CATEGORY_TO_IS_CONDENSED.get(category, ""),
            "video_id": vid,
            "vedio_url": f"https://www.youtube.com/watch?v={vid}",
            "days_since_opening": days_since_opening,
            "is_post_closing": is_post_closing,
            **d,
            **ch,
        }
        csv_writer.writerow({k: row.get(k, "") for k in FIELDNAMES})
        written_pairs.add((run_id, vid))
        n_written += 1
    return n_written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="data/broadway_youtube_targets.csv")
    ap.add_argument("--out", default="data/youtube_condensed/shard_0.csv")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--limit-per-show", type=int, default=100000)
    ap.add_argument("--max-pages-per-query", type=int, default=2)
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

    checkpoint_path = args.checkpoint or (
        os.path.splitext(args.out)[0] + ".processed.txt"
    )
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    targets = load_targets(args.targets, args.shard_index, args.num_shards)
    processed_run_ids = load_processed(checkpoint_path, args.limit_per_show)
    remaining = [t for t in targets if t["run_id"] not in processed_run_ids]
    print(f"[shard {args.shard_index}/{args.num_shards}] 총 {len(targets)}개 중 "
          f"{len(remaining)}개 미처리(재시도 대상 포함), 이번 실행 한도 {args.limit}개")

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
                    "  -> 스키마가 바뀌기 전에 만들어진 구버전 파일로 보여요. 이 상태로 계속 "
                    "append하면 컬럼 수가 안 맞아 파일이 깨져요(pandas ParserError).\n"
                    "  -> Release/로컬에서 이 파일과 대응하는 .processed.txt 체크포인트를 "
                    "지우고 처음부터 다시 수집하세요."
                )
                sys.exit(1)
            for row in reader:
                written_pairs.add((row.get("run_id", ""), row.get("video_id", "")))

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
            ckpt_writer.writerow(["run_id", "n_collected", "limit_used"])

        n_done = 0
        for target in remaining:
            if n_done >= args.limit:
                break
            try:
                n_rows = process_target(
                    session, key_pool, target, args.limit_per_show, writer,
                    fetched_details_cache, written_pairs,
                    max_pages_per_query=args.max_pages_per_query
                )
            except QuotaExceededError as e:
                print(f"  중단: {e}")
                break
            n_total_for_run = sum(1 for (rid, _) in written_pairs if rid == target["run_id"])
            print(f"  [{n_done+1}/{min(len(remaining), args.limit)}] "
                  f"'{target['show']}' ({target['run_id']}) -> {n_rows}건 신규 수집 "
                  f"(누적 {n_total_for_run}건)")
            ckpt_writer.writerow([target["run_id"], n_total_for_run, args.limit_per_show])
            ckpt_f.flush()
            n_done += 1
            if len(fetched_details_cache) > 5000:
                fetched_details_cache.clear()

    print(f"완료: {n_done}개 run 처리, 결과 -> {args.out}")


if __name__ == "__main__":
    main()
