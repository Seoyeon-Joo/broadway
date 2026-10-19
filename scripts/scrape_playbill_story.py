#!/usr/bin/env python3
"""
Playbill production 페이지의 SYNOPSIS 또는 STORY 문단을 가져온다 (콘서트·마술·스탠드업도 Playbill에 story 문구가 있는 경우가 많음).
입력 : --targets (work_id,title,...)  --shows data/broadway_shows_list.csv (show,theatre,opening_date,closing_date)
출력 : --out xlsx  (work_id,title,synopsis,source_url,label_used,plot_words,status,debug)
lib_playbill 의 URL 후보 생성·제목 검증을 재사용한다. URL 슬러그 예외는 data/playbill_url_overrides.csv(show,theatre,url)로 보정 가능.
"""
import argparse, os, re, sys
import pandas as pd
from bs4 import BeautifulSoup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_playbill as L

LABELS = ("SYNOPSIS", "STORY", "ABOUT THE SHOW", "ABOUT")


def extract_story(soup):
    for tag in soup.find_all(["strong", "b", "h2", "h3", "h4"]):
        lab = tag.get_text(strip=True).upper().rstrip(":").strip()
        if lab not in LABELS:
            continue
        pieces = []
        for el in tag.find_all_next():
            t = el.get_text(strip=True) if hasattr(el, "get_text") else ""
            if not t:
                continue
            if el.name in ("strong", "b", "h2", "h3", "h4"):
                up = t.upper().rstrip(":").strip()
                if up in L.STOP_LABELS or (up in LABELS and up != lab):
                    break
                if up == lab:
                    continue
            if el.name == "p":
                pieces.append(el.get_text(" ", strip=True))
        if pieces:
            return "\n\n".join(pieces).strip(), lab
    # 대체: 페이지 전체 텍스트에서 "SYNOPSIS:" 같은 라벨 뒤 ~ 다음 라벨 앞까지
    full = soup.get_text("\n")
    for lab in LABELS:
        m = re.search(r"(?i)\b" + re.escape(lab) + r"\s*:\s*\n*(.*?)(?=\n\s*(?:" + L.STOP_LABELS_PATTERN + r")\s*:|\Z)", full, re.S)
        if m:
            txt = re.sub(r"\n{2,}", "\n\n", m.group(1).strip())
            if len(txt.split()) >= 5:
                return txt[:6000], lab + " (text)"
    return None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True); ap.add_argument("--shows", required=True)
    ap.add_argument("--overrides", default="data/playbill_url_overrides.csv"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t = pd.read_excel(a.targets); shows = pd.read_csv(a.shows).fillna("")
    ov = pd.read_csv(a.overrides) if os.path.exists(a.overrides) else pd.DataFrame(columns=["show", "theatre", "url"])
    rows = []
    for i, r in t.iterrows():
        sh = shows[shows.show == r["title"]]
        syn, url, lab, dbg, status = "", "", "", [], "not found"
        for _, s in sh.iterrows():
            urls = list(ov[ov.show == s.show].url) + L.build_candidate_urls(s.show, s.theatre, str(s.opening_date), str(s.closing_date))
            for u in urls:
                html = L.fetch_html(u)
                if not html:
                    dbg.append(f"{u.rsplit('/',1)[-1]}:x"); continue
                soup = BeautifulSoup(html, "html.parser")
                if not L.title_matches(L.extract_h1(soup) or "", s.show):
                    dbg.append(f"{u.rsplit('/',1)[-1]}:title"); continue
                text, lab = extract_story(soup)
                if text:
                    syn, url, status = text, u, "ok"; break
                dbg.append(f"{u.rsplit('/',1)[-1]}:nolabel")
            if syn: break
        rows.append([r["work_id"], r["title"], syn, url, lab, len(syn.split()), status, " | ".join(dbg)[:400]])
        print(f"[{i+1}/{len(t)}] {r['title']} -> {status} {lab}", flush=True)
    pd.DataFrame(rows, columns=["work_id", "title", "synopsis", "source_url", "label_used", "plot_words", "status", "debug"]).to_excel(a.out, index=False)
    print("ok:", sum(1 for x in rows if x[6] == "ok"), "/", len(rows))


if __name__ == "__main__":
    main()
