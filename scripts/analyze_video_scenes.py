#!/usr/bin/env python3
"""
영상 화면 전환(shot/컷) 및 장면 다양성 분석기 (가벼운 버전: OpenCV + PySceneDetect, 색 히스토그램).
영상 1개씩 저화질(<=360p) 다운로드 -> 분석 -> 즉시 삭제 (디스크 사용 최소화). 중간에 멈춰도 --out 파일을 읽어 이어서 실행.

입력  : --input CSV (video_id 열)   예) data/transcript_targets_t2.csv
출력  : --out JSONL (한 줄 = 영상 1개)
  video_id,status,duration_s,n_shots,shots_per_min,mean_shot_s,median_shot_s,
  n_unique_scenes,unique_scene_ratio,scene_entropy,mean_consec_dist
status: ok | too_long | download_failed | unreadable | blocked | error
  - 장면 다양성: 샷별 대표 프레임의 HSV 히스토그램(Bhattacharyya 거리)을 군집(leader clustering, --cluster-th)해서
    서로 다른 장면 수(n_unique_scenes), 지속시간 가중 엔트로피(scene_entropy), 연속 샷 간 평균 거리를 계산.
사용 예)
  python scripts/analyze_video_scenes.py --input data/transcript_targets_t2.csv --out scenes_t2.jsonl --limit 5
  python scripts/analyze_video_scenes.py --input data/transcript_targets_t2.csv --out scenes_t2.jsonl
"""
import collections
import argparse, glob, json, math, os, random, shutil, sys, tempfile, time
import cv2, numpy as np, pandas as pd
from scenedetect import ContentDetector, detect


def hist(frame):
    hsv = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(h, h, 1, 0, cv2.NORM_L1).astype("float32")


def analyze(path, shot_th=27.0, cluster_th=0.35, min_shot_s=0.5):
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    if n <= 0:
        cap.release(); return None
    dur = n / fps
    scenes = detect(path, ContentDetector(threshold=shot_th, min_scene_len=max(1, int(min_shot_s * fps))))
    bounds = [(a_.frame_num, b_.frame_num) for a_, b_ in scenes] if scenes else []
    if not bounds:
        bounds = [(0, int(n))]
    lens, hs = [], []
    for a, b in bounds:
        mid = int((a + b) / 2)
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
        ok, fr = cap.read()
        if not ok:
            continue
        hs.append(hist(fr)); lens.append((b - a) / fps)
    cap.release()
    if not hs:
        return None
    # leader clustering
    cents, w = [], []
    for h, L in zip(hs, lens):
        for k, c in enumerate(cents):
            if cv2.compareHist(c, h, cv2.HISTCMP_BHATTACHARYYA) < cluster_th:
                w[k] += L; break
        else:
            cents.append(h); w.append(L)
    p = np.array(w) / sum(w)
    ent = float(-(p * np.log2(p)).sum())
    cons = [cv2.compareHist(hs[i], hs[i + 1], cv2.HISTCMP_BHATTACHARYYA) for i in range(len(hs) - 1)]
    ns = len(lens)
    return dict(duration_s=round(dur, 2), n_shots=ns, shots_per_min=round(ns / (dur / 60), 3) if dur else None,
                mean_shot_s=round(float(np.mean(lens)), 3), median_shot_s=round(float(np.median(lens)), 3),
                n_unique_scenes=len(cents), unique_scene_ratio=round(len(cents) / ns, 4),
                scene_entropy=round(ent, 4), mean_consec_dist=round(float(np.mean(cons)), 4) if cons else 0.0)


def select_ids(ids, skip_done, own_done, shard, num_shards):
    """ids 중 다른 파일에서 이미 끝난 것(skip_done)을 뺀 고정 목록을 만든 뒤, shard 번째 몫만 고르고 자기 파일에서 끝난 것(own_done)을 뺀다.
    (여러 창을 동시에 돌려도 서로 겹치지 않고, 이어하기를 해도 몫이 바뀌지 않는다.)"""
    base = [v for v in ids if v not in skip_done]
    mine = [v for k, v in enumerate(base) if k % num_shards == shard]
    return [v for v in mine if v not in own_done]


def read_done(path, retry_blocked=False):
    """결과 파일에 기록된 video_id 집합. retry_blocked=True 이면 status=blocked 인 줄은 '끝난 것'으로 치지 않는다(다시 시도)."""
    done = set()
    if path and os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            try:
                r = json.loads(ln)
                if retry_blocked and r.get("status") == "blocked":
                    continue
                done.add(r["video_id"])
            except Exception:
                pass
    return done


def download(video_id, tmp, height, max_seconds, cookies_browser=None):
    import yt_dlp
    # 분석에는 소리가 필요 없으므로 '영상만' 스트림(bv*)을 받는다 (합쳐진 파일이 없어도 됨, ffmpeg 불필요)
    opts = {"format": f"bv*[height<={height}][ext=mp4]/bv*[height<={height}]/bv*[height<=480]/wv*", "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "retries": 2, "socket_timeout": 30}
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        d = info.get("duration") or 0
        if max_seconds and d > max_seconds:
            return None, d
        y.process_ie_result(info, download=True)
    f = glob.glob(os.path.join(tmp, video_id + ".*"))
    return (f[0] if f else None), d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--height", type=int, default=360)
    ap.add_argument("--max-seconds", type=int, default=1800, help="이보다 긴 영상은 too_long 으로 기록하고 건너뜀 (0=제한 없음)")
    ap.add_argument("--min-sleep", type=float, default=1.5); ap.add_argument("--max-sleep", type=float, default=3.5)
    ap.add_argument("--shot-th", type=float, default=27.0); ap.add_argument("--cluster-th", type=float, default=0.35)
    ap.add_argument("--shard", type=int, default=0, help="나눠서 실행할 때 내 번호 (0부터)")
    ap.add_argument("--num-shards", type=int, default=1, help="전체를 몇 조각으로 나누는지")
    ap.add_argument("--skip-jsonl", nargs="*", default=[], help="이미 끝난 결과 파일(들): 여기에 있는 영상은 건너뜀")
    ap.add_argument("--tmp-dir", default=None, help="임시 다운로드 폴더 (기본: 시스템 임시폴더)")
    ap.add_argument("--wait-on-block-min", type=float, default=0,
                    help="3회 연속 차단되면 중단하는 대신 이만큼(분) 쉬었다가 같은 영상부터 자동으로 이어감 (0=중단)")
    ap.add_argument("--max-waits", type=int, default=12, help="자동 대기를 최대 몇 번까지 할지")
    ap.add_argument("--cookies-from-browser", default=None, help="예: firefox (로그인한 브라우저의 쿠키 사용; 차단 완화용)")
    a = ap.parse_args()
    ids = pd.read_csv(a.input)["video_id"].dropna().astype(str).tolist()
    own_done = read_done(a.out, retry_blocked=True)
    skip_done = set()
    for f_ in a.skip_jsonl:
        skip_done |= read_done(f_)
    todo = select_ids(ids, skip_done, own_done, a.shard, a.num_shards)
    done = own_done | skip_done
    if a.limit:
        todo = todo[:a.limit]
    print(f"전체 {len(ids)}, 완료(다른 파일 포함) {len(done)}, 내 몫 {a.shard + 1}/{a.num_shards}, 이번 처리 {len(todo)}", flush=True)
    fails = 0
    errs = 0
    waits = 0
    queue = collections.deque(todo)
    total = len(todo)
    n = 0
    with open(a.out, "a", encoding="utf-8") as fo:
        while queue:
            v = queue.popleft()
            tmp = tempfile.mkdtemp(dir=a.tmp_dir)
            rec = {"video_id": v}
            t0 = time.time()
            try:
                path, d = download(v, tmp, a.height, a.max_seconds, a.cookies_from_browser)
                if path is None and d and a.max_seconds and d > a.max_seconds:
                    rec.update(status="too_long", duration_s=d)
                elif path is None:
                    rec["status"] = "download_failed"
                else:
                    r = analyze(path, a.shot_th, a.cluster_th)
                    rec.update(r or {}, status="ok" if r else "unreadable")
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if "sign in to confirm" in low or "429" in low or "too many requests" in low or "bot" in low:
                    rec.update(status="blocked", error=msg[:150])
                elif any(k in low for k in ("not available", "unavailable", "private", "removed", "terminated", "deleted", "no longer available")):
                    rec.update(status="unavailable", error=msg[:150])   # 삭제/비공개 영상: 분석 대상에서 제외할 목록
                else:
                    rec.update(status="error", error=msg[:150])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            rec["elapsed_s"] = round(time.time() - t0, 1)
            n += 1
            print(f"[{n}/{total}] {v} {rec['status']} shots={rec.get('n_shots')} uniq={rec.get('n_unique_scenes')}", flush=True)
            if rec["status"] == "blocked":
                # 차단된 영상은 저장하지 않는다(다음에 다시 시도). 맨 앞에 되돌려 놓는다.
                queue.appendleft(v); n -= 1
                fails += 1
                if fails >= 3:
                    if a.wait_on_block_min > 0 and waits < a.max_waits:
                        waits += 1
                        print(f"YouTube 차단 의심: 3회 연속. {a.wait_on_block_min:g}분 쉬었다가 자동으로 이어갑니다 ({waits}/{a.max_waits}회째 대기). 현재 시각 {time.strftime('%H:%M:%S')}", flush=True)
                        time.sleep(a.wait_on_block_min * 60)
                        fails = 0
                        continue
                    print("YouTube 차단 의심: 3회 연속 blocked. 중단합니다. 1~2시간 뒤 같은 명령으로 이어서 실행하세요."); break
                time.sleep(random.uniform(a.min_sleep, a.max_sleep) * 3)
                continue
            fails = 0
            if rec["status"] == "error":
                # 오류는 저장하지 않아서 다음 실행 때 다시 시도됨. 연속 5번이면 중단.
                errs += 1
                print("   오류:", rec.get("error"))
                if errs >= 5:
                    print("연속 5번 오류로 중단합니다. 위 오류 메시지를 확인하세요."); break
                continue
            errs = 0
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n"); fo.flush()
            time.sleep(random.uniform(a.min_sleep, a.max_sleep))


if __name__ == "__main__":
    main()
