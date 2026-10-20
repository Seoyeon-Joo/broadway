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
    bounds = [(s.get_frames()[0] if hasattr(s, "get_frames") else s[0].get_frames(), s[1].get_frames()) for s in scenes] if scenes else []
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


def download(video_id, tmp, height, max_seconds):
    import yt_dlp
    opts = {"format": f"b[height<={height}][ext=mp4]/b[height<={height}]/worst", "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "retries": 2, "socket_timeout": 30}
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
    ap.add_argument("--tmp-dir", default=None, help="임시 다운로드 폴더 (기본: 시스템 임시폴더)")
    a = ap.parse_args()
    ids = pd.read_csv(a.input)["video_id"].dropna().astype(str).tolist()
    done = set()
    if os.path.exists(a.out):
        for ln in open(a.out, encoding="utf-8"):
            try:
                done.add(json.loads(ln)["video_id"])
            except Exception:
                pass
    todo = [v for v in ids if v not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"전체 {len(ids)}, 완료 {len(done)}, 이번 처리 {len(todo)}", flush=True)
    fails = 0
    with open(a.out, "a", encoding="utf-8") as fo:
        for i, v in enumerate(todo, 1):
            tmp = tempfile.mkdtemp(dir=a.tmp_dir)
            rec = {"video_id": v}
            try:
                path, d = download(v, tmp, a.height, a.max_seconds)
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
                    rec.update(status="blocked", error=msg[:150]); fails += 1
                elif "private" in low or "unavailable" in low or "removed" in low:
                    rec.update(status="download_failed", error=msg[:150])
                else:
                    rec.update(status="error", error=msg[:150])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n"); fo.flush()
            print(f"[{i}/{len(todo)}] {v} {rec['status']} shots={rec.get('n_shots')} uniq={rec.get('n_unique_scenes')}", flush=True)
            if rec["status"] == "blocked":
                if fails >= 3:
                    print("YouTube 차단 의심: 3회 연속 blocked. 중단합니다. 1~2시간 뒤 같은 명령으로 이어서 실행하세요."); break
            else:
                fails = 0
            time.sleep(random.uniform(a.min_sleep, a.max_sleep))


if __name__ == "__main__":
    main()
