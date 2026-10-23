"""영상 삭제/비공개 여부를 가볍게 확인한다 (영상 다운로드 없음, YouTube oEmbed 사용).
입력 CSV에 video_id 열이 있어야 한다.
결과 status: ok(공개) / not_found(404, 삭제 가능성 큼) / restricted(401·403, 비공개·임베드 금지·연령 제한 등 애매함) / error(네트워크 등, 다시 시도)
이미 확인한 id는 --out 파일에서 건너뛴다(중단 후 다시 실행 가능).
"""
import argparse, csv, os, time, random, urllib.request, urllib.error, urllib.parse

def check(vid):
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return "ok" if r.status == 200 else f"http_{r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 404: return "not_found"
        if e.code in (401, 403): return "restricted"
        return f"http_{e.code}"
    except Exception as e:
        return "error"

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--shard", type=int, default=0); ap.add_argument("--num-shards", type=int, default=1)
ap.add_argument("--min-sleep", type=float, default=0.3); ap.add_argument("--max-sleep", type=float, default=0.8)
a = ap.parse_args()
ids = []
with open(a.input, encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        v = (r.get("video_id") or "").strip()
        if v and v not in ids: ids.append(v)
done = {}
if os.path.exists(a.out):
    with open(a.out, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["status"] != "error": done[r["video_id"]] = r["status"]
ids = [v for k, v in enumerate(ids) if k % a.num_shards == a.shard]
todo = [v for v in ids if v not in done]
print(f"전체 {len(ids)}, 완료 {len(done)}, 이번 처리 {len(todo)}")
new = not os.path.exists(a.out)
with open(a.out, "a", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    if new: w.writerow(["video_id", "status"])
    err = 0
    for i, v in enumerate(todo, 1):
        s = check(v); w.writerow([v, s]); f.flush()
        err = err + 1 if s == "error" else 0
        if i % 50 == 0 or s != "ok": print(f"[{i}/{len(todo)}] {v} {s}")
        if err >= 10: print("연속 오류 10번, 중단합니다. 인터넷 연결을 확인하세요."); break
        time.sleep(random.uniform(a.min_sleep, a.max_sleep))
