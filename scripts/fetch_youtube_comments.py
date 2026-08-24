"""
fetch_youtube_comments.py
==========================
data/youtube_comment_targets.csv (고유 video_id 목록)을 입력받아 YouTube Data API v3
commentThreads.list로 영상별 댓글을 수집한다. youtube_collect_broadway.py와 최대한
동일한 패턴(KeyPool로 여러 키 순환, run_id 대신 video_id 단위 체크포인트, 20-shard
병렬 구조)을 따름 - 파이프라인 전체의 일관성을 위해 KeyPool/robust_get을 그대로
복붙함(다른 스크립트를 import하지 않고 파일 하나로 독립 실행 가능하게 하는 이 repo의
기존 관행을 따름).

*** commentThreads.list 하나로 top-level 댓글 + 답글(최대 5개)까지 한 번에 ***
part=snippet,replies로 요청하면 각 top-level 댓글 스레드에 대해 최신/관련도 높은
답글을 최대 5개까지 추가 API 호출 없이 같이 내려줌(호출당 비용은 snippet과 동일하게
1유닛). 답글이 5개보다 많은 스레드는 total_reply_count로 표시만 하고, 나머지 답글을
전부 받아오는 comments.list(parentId=...) 호출은 하지 않음 - 영상 하나에 답글이
수천 개씩 달리는 경우도 있어서(예: 유명 트레일러) 무제한으로 받으면 유닛을 감당할 수
없음. 5개면 대부분의 분석 목적(작성자/시간/좋아요 패턴 파악)엔 충분하다고 보고
의도적으로 타협함.

*** order="time"을 쓰는 이유 ***
기본값(relevance)은 좋아요/답글이 많은 댓글을 앞에 주는데, 이러면 인기 없는(그러나
분석에는 필요할 수 있는) 댓글이 --max-comments-per-video 상한에 걸려 아예 못 들어올
수 있음. "댓글 작성 시간"을 수집 목적으로 명시했으므로, 시간순(최신순)으로 받아서
표본이 특정 시점에 쏠리지 않게 함.

*** --max-comments-per-video로 상한을 두는 이유 ***
comment_count가 79만 건이 넘는 영상도 실제로 있음(멀티 쇼 걸친 초인기 트레일러 추정).
전체를 다 받으면 그 영상 하나가 shard 하나의 시간/유닛 예산을 통째로 잡아먹으므로,
run 단위 --limit-per-show와 동일한 취지로 영상 하나당 상한을 둠. 상한에 걸려 끊긴
영상은 체크포인트에 status=capped로 남아서, 나중에 상한을 올리면 그 영상만 다시
이어서 받을 수 있음(youtube_collect_broadway.py의 n_collected/limit_used 재시도
로직과 동일한 패턴).

수집 필드
---------
video_id, show, comment_id, is_reply, parent_comment_id,
author_display_name, author_channel_id, text, like_count,
published_at, updated_at, total_reply_count

준비물
------
1. Google Cloud Console에서 YouTube Data API v3 활성화, API 키 발급 (기존
   youtube_collect_broadway.py와 같은 키 재사용 가능 - commentThreads.list도
   같은 quota 풀을 씀)
2. 여러 키를 콤마로 이어붙여 환경변수 YOUTUBE_API_KEYS로 전달, 또는 --api-key로
   단일 키 지정 (테스트용)

사용 예시
--------
    python fetch_youtube_comments.py \
        --targets data/youtube_comment_targets.csv \
        --out data/youtube_comments/shard_0.csv \
        --shard-index 0 --num-shards 20 \
        --limit 300 --max-comments-per-video 200
    python fetch_youtube_comments.py \
        --shows "dQw4w9WgXcQ:some show" --out . --limit 1  # 테스트용
"""
import argparse
import csv
import os
import sys
import time

import requests

API_BASE = "https://www.googleapis.com/youtube/v3"

FIELDNAMES = [
    "video_id", "show", "comment_id", "is_reply", "parent_comment_id",
    "author_display_name", "author_channel_id", "text", "like_count",
    "published_at", "updated_at", "total_reply_count",
]


class QuotaExceededError(Exception):
    pass


class KeyPool:
    """콤마로 이어붙인 여러 API 키를 순환하며 429/quota 오류 시 다음 키로 넘어감.
    (youtube_collect_broadway.py와 동일 - 이 repo의 표준 패턴)"""

    def __init__(self, keys):
        self.keys = keys
        self.idx = 0

    def current(self):
        return self.keys[self.idx % len(self.keys)]

    def rotate(self):
        self.idx += 1
        return self.current()


def robust_get(session, url, params, key_pool, max_cycles=3):
    """429/quota 오류 시 키를 순환하며 재시도, 그 외 네트워크 오류는 지수 백오프.
    commentsDisabled/videoNotFound 같은 영상별 영구 오류는 quota 오류가 아니므로
    바로 {"error": ...}를 반환함 (키 순환 없이 즉시 호출부로 넘겨서 그 영상만
    스킵하게 함 - 다른 영상까지 재시도 예산을 낭비하지 않도록)."""
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
            err_json = resp.json().get("error", {})
        except Exception:
            err_json = {"message": resp.text[:200]}
        err_msg = err_json.get("message", "")
        reasons = [e.get("reason", "") for e in err_json.get("errors", [])]

        if resp.status_code in (403, 429) and (
            "quota" in err_msg.lower() or resp.status_code == 429
        ) and "commentsDisabled" not in reasons:
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

        return {"error": {"message": err_msg, "reasons": reasons, "status": resp.status_code}}


def fetch_comments_for_video(session, key_pool, video_id, max_comments, max_pages=200):
    """영상 하나의 댓글을 최대 max_comments(top-level 기준)까지 수집.

    반환값: (rows, status)
      - status: "ok"(자연 종료, 더 받을 게 없음) / "capped"(상한에 걸려 끊김) /
        "disabled"(댓글 비활성화) / "error"(그 외 오류)"""
    rows = []
    page_token = None
    for _ in range(max_pages):
        if len(rows) >= max_comments:
            return rows, "capped"
        params = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": 100,
            "order": "time",
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token
        data = robust_get(session, f"{API_BASE}/commentThreads", params, key_pool)

        if "error" in data:
            reasons = data["error"].get("reasons", [])
            if "commentsDisabled" in reasons:
                return rows, "disabled"
            if "videoNotFound" in reasons or "notFound" in reasons:
                return rows, "disabled"  # 삭제/비공개 처리된 영상 - 재시도해도 소용없음
            print(f"    [댓글 조회 오류] video={video_id} {data['error'].get('message', '')}")
            return rows, "error"

        for item in data.get("items", []):
            top = item.get("snippet", {}).get("topLevelComment", {})
            top_sn = top.get("snippet", {})
            top_id = top.get("id", "")
            rows.append({
                "video_id": video_id,
                "comment_id": top_id,
                "is_reply": False,
                "parent_comment_id": "",
                "author_display_name": top_sn.get("authorDisplayName", ""),
                "author_channel_id": top_sn.get("authorChannelId", {}).get("value", ""),
                "text": top_sn.get("textOriginal", ""),
                "like_count": top_sn.get("likeCount", ""),
                "published_at": top_sn.get("publishedAt", ""),
                "updated_at": top_sn.get("updatedAt", ""),
                "total_reply_count": item.get("snippet", {}).get("totalReplyCount", 0),
            })
            # part=replies로 같이 받아온 답글(스레드당 최대 5개, 추가 유닛 없음).
            # 5개보다 많으면 위 total_reply_count로만 표시하고 나머지는 안 받음
            # (파일 상단 "commentThreads.list 하나로..." 설명 참고).
            for reply in item.get("replies", {}).get("comments", []):
                r_sn = reply.get("snippet", {})
                rows.append({
                    "video_id": video_id,
                    "comment_id": reply.get("id", ""),
                    "is_reply": True,
                    "parent_comment_id": top_id,
                    "author_display_name": r_sn.get("authorDisplayName", ""),
                    "author_channel_id": r_sn.get("authorChannelId", {}).get("value", ""),
                    "text": r_sn.get("textOriginal", ""),
                    "like_count": r_sn.get("likeCount", ""),
                    "published_at": r_sn.get("publishedAt", ""),
                    "updated_at": r_sn.get("updatedAt", ""),
                    "total_reply_count": "",
                })
            if len(rows) >= max_comments:
                return rows, "capped"

        page_token = data.get("nextPageToken")
        if not page_token:
            return rows, "ok"
        time.sleep(0.05)
    return rows, "capped"  # max_pages 안전판에 걸림 (사실상 도달할 일 거의 없음)


def load_targets(path, shard_index, num_shards):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i % num_shards == shard_index:
                rows.append(row)
    return rows


def load_processed(path, current_limit_per_video):
    """체크포인트에서 재시도 불필요한 video_id만 걸러서 반환.
    (youtube_collect_broadway.py의 load_processed와 동일한 설계 - status 컬럼만 추가)

    - status=ok/disabled: 항상 스킵 (자연 종료 또는 영구 오류)
    - status=capped: 이번 --max-comments-per-video가 그때보다 커야만 재시도
    - status=error: 항상 재시도 대상에 남김 (일시적 오류로 가정)"""
    if not os.path.isfile(path):
        return set()
    fully_done = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status = row.get("status", "")
            try:
                n_collected = int(row.get("n_collected", 0))
                limit_used = int(row.get("limit_used", 0))
            except (TypeError, ValueError):
                n_collected, limit_used = 0, 0
            if status in ("ok", "disabled"):
                fully_done.add(row["video_id"])
            elif status == "capped":
                if current_limit_per_video <= limit_used:
                    fully_done.add(row["video_id"])
            # status == "error" 또는 알 수 없는 값 -> 재시도 대상에 남김 (추가 안 함)
    return fully_done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="data/youtube_comment_targets.csv")
    ap.add_argument("--out", default="data/youtube_comments/shard_0.csv")
    ap.add_argument("--checkpoint", default=None,
                     help="기본값: --out 경로에서 확장자만 .processed.txt 로 변경")
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=300,
                     help="이번 실행에서 처리할 최대 영상 개수 (GitHub Actions 시간 제한 대비)")
    ap.add_argument("--max-comments-per-video", type=int, default=200,
                     help="영상 하나당 최대 수집 댓글 개수(top-level 기준, 답글은 별도)")
    ap.add_argument("--api-key", default=None, help="단일 키 직접 지정 (테스트용)")
    ap.add_argument("--shows", nargs="+", default=None,
                     help="테스트용: 'video_id:show' 형태로 나열")
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

    if args.shows:
        targets = []
        for s in args.shows:
            vid, _, show = s.partition(":")
            targets.append({"video_id": vid, "show": show})
    else:
        targets = load_targets(args.targets, args.shard_index, args.num_shards)

    checkpoint_path = args.checkpoint or (
        os.path.splitext(args.out)[0] + ".processed.txt"
    )
    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)

    processed = load_processed(checkpoint_path, args.max_comments_per_video)
    remaining = [t for t in targets if t["video_id"] not in processed]
    print(f"[shard {args.shard_index}/{args.num_shards}] 총 {len(targets)}개 중 "
          f"{len(remaining)}개 미처리(재시도 대상 포함), 이번 실행 한도 {args.limit}개")

    out_exists = os.path.isfile(args.out)
    mode = "a" if out_exists else "w"
    checkpoint_is_new = not os.path.isfile(checkpoint_path)
    with open(args.out, mode, newline="", encoding="utf-8-sig") as out_f, \
         open(checkpoint_path, "a", newline="", encoding="utf-8") as ckpt_f:
        writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES)
        if not out_exists:
            writer.writeheader()

        ckpt_writer = csv.writer(ckpt_f)
        if checkpoint_is_new:
            ckpt_writer.writerow(["video_id", "n_collected", "limit_used", "status"])

        n_done = 0
        n_errors = 0
        for target in remaining:
            if n_done >= args.limit:
                break
            video_id = target["video_id"]
            show = target.get("show", "")
            try:
                rows, status = fetch_comments_for_video(
                    session, key_pool, video_id, args.max_comments_per_video
                )
            except QuotaExceededError as e:
                print(f"  중단: {e}")
                break

            for row in rows:
                row["show"] = show
                writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

            status_note = {"ok": "", "capped": " [상한 도달]",
                            "disabled": " [댓글 비활성화/영상 없음]",
                            "error": " [오류 - 다음 실행에 재시도]"}.get(status, "")
            if status == "error":
                n_errors += 1
            print(f"  [{n_done+1}/{min(len(remaining), args.limit)}] "
                  f"video={video_id} ('{show}') -> {len(rows)}건 수집{status_note}")
            ckpt_writer.writerow([video_id, len(rows), args.max_comments_per_video, status])
            ckpt_f.flush()
            n_done += 1

    print(f"완료: {n_done}개 영상 처리(오류 {n_errors}건), 결과 -> {args.out}")


if __name__ == "__main__":
    main()
