"""
youtube_collect_broadway.py
============================
data/broadway_youtube_targets.csv (run 단위)를 입력받아 YouTube Data API v3로
show별 관련 영상을 검색/수집한다. KOPIS 파이프라인과의 차이:

  - 시즌 그룹 로직 없음 (target = 프로덕션 run 하나)
  - 영어 쿼리, regionCode=US, relevanceLanguage=en
  - search.list로 후보만 찾고, videos.list로 처음부터 description 전문(최대 5,000자)을
    같이 받아옴 -> KOPIS 때처럼 나중에 별도 백필 스텝이 필요 없음
  - 종연(closing_date) 이후 올라온 영상도 배제하지 않고 그대로 수집
    (분석 단계에서 published_at 기준으로 필터링하도록 결정을 미룸)

수집 필드
---------
run_id, show, theatre, opening_date, closing_date, query_used,
video_id, video_title, description, channel_id, channel_name, published_at,
duration_sec, duration_min, is_shorts_guess,
view_count, like_count, comment_count,
channel_subscriber_count, channel_video_count, hidden_subscriber_count,
video_url, days_since_opening, is_post_closing

준비물
------
1. Google Cloud Console에서 YouTube Data API v3 활성화, API 키 발급
2. 여러 키를 콤마로 이어붙여 환경변수 YOUTUBE_API_KEYS 로 전달 (GitHub Actions
   secrets.YOUTUBE_API_KEYS 그대로 재사용 가능) 또는 --api-key 로 단일 키 직접 지정

사용 예시
--------
    python youtube_collect_broadway.py \
        --targets data/broadway_youtube_targets.csv \
        --out data/youtube_broadway/shard_0.csv \
        --shard-index 0 --num-shards 20 \
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

FIELDNAMES = [
    "run_id", "show", "theatre", "opening_date", "closing_date", "date_source", "query_used",
    "video_id", "video_title", "description", "channel_id", "channel_name",
    "published_at", "duration_sec", "duration_min", "is_shorts_guess",
    "view_count", "like_count", "comment_count",
    "channel_subscriber_count", "channel_video_count", "hidden_subscriber_count",
    "video_url", "days_since_opening", "is_post_closing",
]

# 영어권 브로드웨이 관행에 맞춘 쿼리 변형. {show} 자리에 쇼 제목이 들어감.
QUERY_TEMPLATES = [
    '"{show}" Broadway official trailer',
    '"{show}" Broadway highlights',
    '"{show}" Broadway musical numbers',
    '"{show}" Broadway sizzle reel',
    '"{show}" Broadway first look',
    '"{show}" Broadway behind the scenes',
    '"{show}" Broadway opening night',
    '"{show}" Broadway curtain call',
    '"{show}" Broadway cast interview',
    '"{show}" Broadway making of',
    '"{show}" Broadway rehearsal',
    '"{show}" Broadway press event',
    '"{show}" Broadway teaser',
    '"{show}" Broadway official clip',
    '"{show}" Broadway Playbill',
    '"{show}" Broadway.com',
    '"{show}" Broadway Today',
    '"{show}" Broadway review',
    '"{show}" Broadway performance clip',
    '"{show}" musical vlog',
]


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


class KeyPool:
    """콤마로 이어붙인 여러 API 키를 순환하며 429/quota 오류 시 다음 키로 넘어감.
    한 번 "API key not valid"로 확인된 키는 dead 세트에 넣고 이후 완전히 건너뜀
    (죽은 키에 idx가 멈춰서 로테이션이 정지하는 문제 방지)."""

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


def robust_get(session, url, params, key_pool, max_cycles=3):
    """429/quota 오류 시 키를 순환하며 재시도, 그 외 네트워크 오류는 지수 백오프."""
    cycles = 0
    backoff = 1.0
    while True:
        params = dict(params)
        params["key"] = key_pool.current()
        try:
            resp = session.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            print(f"    [네트워크 오류] {e} - {backoff:.0f}초 후 재시도")
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
            # 이 키는 죽었음이 확정됐으니 앞으로 완전히 건너뜀 (idx만 옮기고 마는
            # 게 아니라 dead 세트에 등록 -> 다음 current() 호출부터 자동 스킵)
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
    """search.list는 호출 1번에 100유닛 고정이고 maxResults를 50까지 올려도 비용이
    똑같음 - 그래서 항상 50으로 요청함(이전엔 15로 받아서 같은 비용에 결과를 1/3만
    받고 있었음).

    *** 적응형 페이지네이션 ***
    페이지가 꽉 찼을 때만(=results_per_page개를 그대로 다 채워서 받았을 때만) 다음
    페이지를 요청하고, 안 채워지면 그 자리에서 멈춤. 즉 인기 많은 쇼(검색 결과가
    풍부한 쇼)는 페이지가 계속 꽉 차니까 자연스럽게 max_pages까지 더 깊이 받고,
    비인기 쇼는 첫 페이지부터 안 차서 바로 멈춤.

    *** target_count: 이 run에 아직 몇 개 더 필요한지 ***
    process_target이 "이 run에서 limit_per_show까지 아직 몇 개 남았는지"를 넘겨주면,
    누적 결과가 그 개수에 도달하는 즉시 멈춤(설령 max_pages에 안 닿았고 페이지가
    계속 꽉 차더라도). 이게 없으면 max_pages를 크게 잡았을 때 초인기 쇼(Hamilton,
    Wicked 등)가 이미 충분히 모았는데도 같은 쿼리 안에서 계속 페이지를 넘겨서
    쿼터를 낭비함 - 이 체크 덕분에 max_pages는 사실상 "이론적 최대치"일 뿐이고
    실제 지출은 항상 needed 만큼으로 수렴함.
    max_pages는 그래도 남겨둠 - target_count가 None인 호출(테스트 등)이나,
    YouTube 자체가 정말 결과가 무한히 많다고 우길 때의 최후 안전판."""
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
            break  # 다음 페이지가 없거나(nextPageToken 없음), 이번 페이지가 안 찼으면(더 볼 게 없다는 신호) 멈춤
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
                "channel_name": sn.get("channelTitle", ""),
                "published_at": sn.get("publishedAt", ""),
                "duration_sec": duration_sec,
                "duration_min": round(duration_sec / 60, 2),
                "is_shorts_guess": duration_sec > 0 and duration_sec <= 60,
                "view_count": st.get("viewCount", ""),
                "like_count": st.get("likeCount", ""),
                "comment_count": st.get("commentCount", ""),
            }
        time.sleep(0.1)
    return results


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
    """체크포인트 파일에서 '완전히 재시도 불필요'한 run만 걸러서 반환.

    *** 왜 CSV 형식(run_id,n_collected,limit_used)으로 바꿨나 ***
    예전엔 run_id 한 줄만 기록해서 "처리 완료 = 영원히 스킵"이었는데, 그러면
    나중에 --limit-per-show를 올려도 이미 처리된 run은 그냥 넘어가버려서
    "예전엔 40개 캡에 걸려서 못 받은 나머지"를 절대 못 받아옴. 이제 그 run에서
    실제로 몇 개 모았는지(n_collected)와 그때 상한이 뭐였는지(limit_used)를 같이
    기록해서, n_collected >= limit_used(=캡에 걸려서 끊긴 것으로 추정)이고
    현재 --limit-per-show가 그때보다 크면 다시 시도 대상에 넣음. 반대로
    n_collected < limit_used(=검색 결과가 자연스럽게 바닥나서 끝난 것)면 상한을
    올려도 더 나올 게 없으니 여전히 스킵함 - 쓸데없는 재검색으로 쿼터 낭비하는 걸
    막음.

    예전 버전(plain-text, run_id 한 줄씩)으로 만들어진 체크포인트 파일도 그대로
    읽을 수 있게 폴백 처리함 - 그런 줄은 컬럼 정보가 없으니 보수적으로 "완료,
    재시도 안 함"으로 취급함(예전 동작과 동일하게 유지, 데이터 손실 없음)."""
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
                    continue  # 캡에 걸렸었고 이번엔 상한을 올렸으니 재시도 대상에 남김
                fully_done.add(row["run_id"])
        else:
            # 구버전 plain-text 체크포인트 - 줄마다 run_id 하나, 정보 없으니 완료로 취급
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


# 개막 며칠 전부터 프레스콜/티저 영상이 올라올 수 있어 허용하는 버퍼.
# date_source(build_broadway_youtube_targets.py가 매긴 신뢰도)에 따라 다르게 적용:
#   history(show_history 날짜 겹침 매칭, 가장 정확) -> 좁게
#   show_level(title 단위 병합, 리바이벌엔 부정확할 수 있음) -> 넉넉하게
#   week_range(관측 주차로 근사, 가장 부정확) -> 아주 넉넉하게 (거의 배제 안 함)
PRE_OPENING_BUFFER_DAYS = {
    "history": 120,
    "show_level": 270,
    "show_level_ambiguous": 500,  # 동일 제목 리바이벌이 여럿이라 opening_date 자체가
                                   # 다른 프로덕션 것일 수 있음 - 가장 넉넉하게 잡음
    "week_range": 400,
}
DEFAULT_BUFFER_DAYS = 270


def build_queries_for_target(target):
    show = target["show"]
    templates = list(QUERY_TEMPLATES)
    is_revival = str(target.get("is_revival", "")).lower() == "true"
    title_ambiguous = str(target.get("title_ambiguous", "")).lower() == "true"
    opening = parse_date(target.get("opening_date", ""))
    lead_cast = target.get("lead_cast", "") or ""
    lead_actor = lead_cast.split(";")[0].strip() if lead_cast else ""

    if (is_revival or title_ambiguous) and opening:
        # 리바이벌은 연도를 붙여 검색 자체를 그 시기로 편향시킴 (동명 프로덕션 간 뒤섞임 방지)
        year = opening.year
        templates = [
            f'"{{show}}" Broadway {year} revival',
            f'"{{show}}" Broadway {year} trailer',
            f'"{{show}}" Broadway {year} cast',
        ] + templates
        if lead_actor:
            # 주연 배우 이름까지 넣으면 같은 제목의 다른 시기 프로덕션과 훨씬
            # 확실하게 구분됨 (예: "Cabaret" "Eddie Redmayne" 는 2024년 리바이벌만 나옴)
            templates = [f'"{{show}}" "{lead_actor}"'] + templates
    if target.get("theatre"):
        templates.insert(0, '"{show}" "%s"' % target["theatre"])
    return [t.format(show=show) for t in templates]


def video_belongs_to_run(published, opening, date_source):
    """published_at이 run의 opening_date보다 크게(버퍼 이상) 앞서면 다른 시기의
    프로덕션(예: 같은 쇼의 다른 리바이벌) 영상일 가능성이 높다고 보고 배제.
    종연 이후 영상은 배제하지 않음 (요청사항). 버퍼는 date_source(날짜 출처의
    신뢰도)에 따라 다르게 적용 - 근사치 날짜일수록 버퍼를 넉넉하게 잡아
    오탐으로 배제되는 걸 방지."""
    if not published or not opening:
        return True  # 날짜 정보 부족하면 일단 포함, 나중에 QA에서 검토
    buffer_days = PRE_OPENING_BUFFER_DAYS.get(date_source, DEFAULT_BUFFER_DAYS)
    return (published - opening).days >= -buffer_days


def process_target(session, key_pool, target, limit_per_show, csv_writer,
                    fetched_details_cache, written_pairs, max_pages_per_query=2):
    show = target["show"]
    run_id = target["run_id"]
    opening = parse_date(target.get("opening_date", ""))
    closing = parse_date(target.get("closing_date", ""))

    all_ids_this_run = {}  # video_id -> query_used (first hit)
    for query in build_queries_for_target(target):
        remaining = limit_per_show - len(all_ids_this_run)
        if remaining <= 0:
            break
        ids = search_videos(session, key_pool, query, max_pages=max_pages_per_query,
                             target_count=remaining)
        for vid in ids:
            if vid not in all_ids_this_run:
                all_ids_this_run[vid] = query
        time.sleep(0.1)

    # videos.list 호출은 아직 상세정보 캐시에 없는 영상만 (같은 run 내 API 절약용,
    # 다른 run과는 공유하지 않음 -- 리바이벌 간 오귀속 방지가 우선이라 재사용 안 함)
    ids_to_fetch = [v for v in all_ids_this_run if v not in fetched_details_cache]
    if ids_to_fetch:
        fetched_details_cache.update(get_video_details(session, key_pool, ids_to_fetch))

    channel_ids = [
        fetched_details_cache[v]["channel_id"]
        for v in all_ids_this_run
        if v in fetched_details_cache and fetched_details_cache[v].get("channel_id")
    ]
    channel_details = get_channel_details(session, key_pool, channel_ids)

    n_written = 0
    for vid, query_used in all_ids_this_run.items():
        if (run_id, vid) in written_pairs:
            continue
        d = fetched_details_cache.get(vid)
        if not d:
            continue
        published = parse_date(d.get("published_at", ""))
        if not video_belongs_to_run(published, opening, target.get("date_source", "")):
            continue  # 이 run의 시기로 보기 어려움 -> 배제

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
            "closing_date": target.get("closing_date", ""),
            "date_source": target.get("date_source", ""),
            "query_used": query_used,
            "video_id": vid,
            "video_url": f"https://www.youtube.com/watch?v={vid}",
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
    ap.add_argument("--out", default="data/youtube_broadway/shard_0.csv")
    ap.add_argument("--checkpoint", default=None,
                     help="기본값: --out 경로에서 확장자만 .processed.txt 로 변경")
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=200,
                     help="이번 실행에서 처리할 최대 run 개수 (GitHub Actions 시간 제한 대비)")
    ap.add_argument("--limit-per-show", type=int, default=60,
                     help="run 하나당 최대 수집 영상 개수")
    ap.add_argument("--max-pages-per-query", type=int, default=20,
                     help="쿼리 하나당 최대 몇 페이지까지 갈 수 있는지의 이론적 상한 "
                          "(페이지당 100유닛). --limit-per-show에 도달하면 그 전에 "
                          "항상 먼저 멈추니(target_count 체크), 이 값을 크게 잡아도 "
                          "실제 지출은 늘지 않음 - YouTube가 정말 무한히 결과를 준다고 "
                          "우기는 극단적 상황에서만 의미 있는 최후 안전판")
    ap.add_argument("--api-key", default=None, help="단일 키 직접 지정 (테스트용)")
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
            for row in csv.DictReader(f):
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
            # n_collected는 '이번에 새로 쓴 개수'가 아니라 '이 run에서 지금까지 누적
            # 수집된 총 개수' (재시도로 몇 번을 거쳤든 written_pairs에 다 쌓여있음) -
            # limit_used와 비교해서 다음에 상한을 올렸을 때 재시도할지 판단하는 데 씀
            n_total_for_run = sum(1 for (rid, _) in written_pairs if rid == target["run_id"])
            print(f"  [{n_done+1}/{min(len(remaining), args.limit)}] "
                  f"'{target['show']}' ({target['run_id']}) -> {n_rows}건 신규 수집 "
                  f"(누적 {n_total_for_run}건)")
            ckpt_writer.writerow([target["run_id"], n_total_for_run, args.limit_per_show])
            ckpt_f.flush()
            n_done += 1
            if len(fetched_details_cache) > 5000:
                fetched_details_cache.clear()  # 메모리 상한 (긴 실행 대비)

    print(f"완료: {n_done}개 run 처리, 결과 -> {args.out}")


if __name__ == "__main__":
    main()
