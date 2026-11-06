"""패널 t2 영상의 길이를 YouTube API로 다시 가져와 기존 duration_sec과 비교한다."""
import os, re, requests, pandas as pd
keys = [k.strip() for k in os.environ["YOUTUBE_API_KEYS"].replace("\n", ",").split(",") if k.strip()]
d = pd.read_csv("data/condensed_comment_targets.csv", encoding="utf-8-sig")
ids = d.video_id.dropna().unique().tolist()
def secs(s):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m: return None
    dd, h, mm, ss = (int(x) if x else 0 for x in m.groups()); return dd*86400+h*3600+mm*60+ss
rows = []; ki = 0
for i in range(0, len(ids), 50):
    chunk = ids[i:i+50]
    for _ in range(len(keys)):
        r = requests.get("https://www.googleapis.com/youtube/v3/videos", params=dict(part="contentDetails,snippet,liveStreamingDetails", id=",".join(chunk), key=keys[ki % len(keys)]), timeout=60)
        if r.status_code == 200: break
        ki += 1
    for it in r.json().get("items", []):
        rows.append(dict(video_id=it["id"], iso=it["contentDetails"]["duration"], api_sec=secs(it["contentDetails"]["duration"]),
                         title=it["snippet"]["title"], live=it["snippet"].get("liveBroadcastContent"), has_live_details="liveStreamingDetails" in it))
a = pd.DataFrame(rows).merge(d[["video_id", "medium", "show", "duration_sec", "n_shots"]].drop_duplicates("video_id"), on="video_id", how="right")
a["diff"] = a.api_sec - a.duration_sec
a.to_csv("data/duration_check.csv", index=False, encoding="utf-8-sig")
print("requested", len(ids), "returned", len(rows)); print("mismatch:", (a["diff"].abs() > 2).sum())
print(a[a["diff"].abs() > 2][["video_id", "medium", "show", "iso", "api_sec", "duration_sec", "n_shots"]].to_string())
