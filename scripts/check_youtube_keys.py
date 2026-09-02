#!/usr/bin/env python3
"""
YouTube Data API v3 키 목록 일괄 유효성 검사 스크립트.
사용법:
    python check_youtube_keys.py keys.txt

keys.txt: 쉼표 또는 줄바꿈으로 구분된 API 키 목록 파일

결과:
    - dead_keys.txt   : "API key not valid" (400) 등 키 자체가 죽은 것
    - alive_keys.txt  : 정상 응답한 키
    - quota_keys.txt  : quotaExceeded(403) — 죽은 게 아니라 오늘 할당량 소진
    - forbidden_other_keys.txt / other_keys.txt / network_error_keys.txt : 그 외 케이스

주의: 콘솔 로그에는 키 내용을 출력하지 않음 (CI 마스킹 우회 방지).
      상세 결과는 결과 파일에만 기록됨.
"""

import sys
import time
import requests

TEST_VIDEO_ID = "dQw4w9WgXcQ"  # 가벼운 단일 비디오 조회로 최소 쿼터만 소모
URL = "https://www.googleapis.com/youtube/v3/videos"

def load_keys(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = raw.replace("\n", ",")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    seen = set()
    unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique

def check_key(key, timeout=10):
    params = {"part": "id", "id": TEST_VIDEO_ID, "key": key}
    try:
        resp = requests.get(URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        return "network_error", str(e)

    if resp.status_code == 200:
        return "alive", resp.status_code

    try:
        err = resp.json().get("error", {})
        reason = ""
        for d in err.get("errors", []):
            reason = d.get("reason", "")
            break
        message = err.get("message", "")
    except Exception:
        reason, message = "", resp.text[:200]

    if resp.status_code == 400 and ("API key not valid" in message or reason in ("badRequest", "keyInvalid")):
        return "dead", message
    if resp.status_code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return "quota", message
    if resp.status_code == 403:
        return "forbidden_other", f"{reason}: {message}"
    return "other", f"{resp.status_code}: {message}"

def main():
    if len(sys.argv) < 2:
        print("사용법: python check_youtube_keys.py keys.txt")
        sys.exit(1)

    keys = load_keys(sys.argv[1])
    print(f"총 {len(keys)}개 키 검사 시작")

    buckets = {"alive": [], "dead": [], "quota": [], "forbidden_other": [], "other": [], "network_error": []}

    for i, key in enumerate(keys, 1):
        status, detail = check_key(key)
        buckets.setdefault(status, []).append((key, detail))
        if i % 20 == 0 or i == len(keys):
            print(f"[{i}/{len(keys)}] 진행 중...")
        time.sleep(0.05)

    print("\n=== 요약 ===")
    for k, v in buckets.items():
        print(f"{k}: {len(v)}개")

    for name in ["dead", "alive", "quota", "forbidden_other", "other", "network_error"]:
        with open(f"{name}_keys.txt", "w", encoding="utf-8") as f:
            for key, detail in buckets.get(name, []):
                f.write(f"{key}\t{detail}\n")

    print("\n결과 파일: dead_keys.txt, alive_keys.txt, quota_keys.txt, forbidden_other_keys.txt, other_keys.txt, network_error_keys.txt")

if __name__ == "__main__":
    main()
