#!/usr/bin/env python3
"""
작품(영화/공연)별 시놉시스 수집기 -> Excel(.xlsx) 출력

입력 : data/synopsis_targets.xlsx
        columns: work_id, title, type(film|musical|play), year(선택), wikipedia_title(선택, 알면 정확도 올라감)
출력 : data/synopses.xlsx
        sheet "synopses"  : 코딩 시트에 붙여넣을 시놉시스(앞 N문장) + 출처/검토 표시
        sheet "full_plot" : 위키 줄거리 섹션 전문 (참고용)

출처는 영화/공연 모두 영어 Wikipedia (MediaWiki API, 공식 API 사용).
본문은 CC BY-SA 4.0 이므로 사용 시 출처(URL)를 표기해야 한다.
"""
import argparse
import re
import sys
import time

import pandas as pd
import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

API = "https://en.wikipedia.org/w/api.php"
UA = "broadway-synopsis-collector/1.0 (academic thesis; https://github.com/Seoyeon-Joo/broadway)"
TARGETS = "data/synopsis_targets.xlsx"
OUT = "data/synopses.xlsx"
N_SENTENCES = None       # None = 줄거리 섹션 전체 (문장 수 제한 없음)
MAX_CHARS = 32000        # 엑셀 셀 한도(32,767자) 안에서 전체를 담음
PLOT_HEADINGS = ["plot", "synopsis", "plot summary", "premise", "story", "summary", "plot synopsis"]
SKIP_TITLE = re.compile(r"^(list of|category:|soundtrack|.*\bsoundtrack\b|.*\(album\)|.*\(song\))", re.I)

session = requests.Session()
session.headers["User-Agent"] = UA


def api(params, retries=4):
    p = {"format": "json", "formatversion": "2", **params}
    for i in range(retries):
        try:
            r = session.get(API, params=p, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
    return {}


def norm(s):
    s = re.sub(r"\(.*?\)", " ", str(s))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def is_disambiguation(title):
    d = api({"action": "query", "prop": "pageprops", "ppprop": "disambiguation", "titles": title, "redirects": 1})
    pages = d.get("query", {}).get("pages", [])
    return bool(pages and "disambiguation" in pages[0].get("pageprops", {}))


def find_page(title, typ, year, override):
    """(page_title, confidence, note)"""
    if isinstance(override, str) and override.strip():
        return override.strip(), "high", "wikipedia_title provided"
    hint = {"film": "film", "musical": "musical", "play": "play", "broadway": "Broadway"}.get(str(typ).lower(), "")
    q = f"{title} {hint} {int(year) if pd.notna(year) else ''}".strip()
    d = api({"action": "query", "list": "search", "srsearch": q, "srlimit": 8, "srnamespace": 0})
    cands = d.get("query", {}).get("search", [])
    best, best_score = None, -1
    for c in cands:
        t = c["title"]
        if SKIP_TITLE.search(t):
            continue
        score = 0
        if norm(t) == norm(title):
            score += 3
        elif norm(t).startswith(norm(title)):
            score += 2
        if hint and hint.lower() in t.lower():
            score += 1
        if pd.notna(year) and str(int(year)) in t:
            score += 2
        if hint and hint.lower() in re.sub(r"<.*?>", "", c.get("snippet", "")).lower():
            score += 1
        if score > best_score:
            best, best_score = t, score
    if best is None:
        return None, "none", "no search result"
    if is_disambiguation(best):
        return best, "low", "disambiguation page"
    return best, ("high" if best_score >= 4 else "medium" if best_score >= 3 else "low"), f"search score {best_score}"


def get_text(page_title):
    d = api({"action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "wiki",
             "titles": page_title, "redirects": 1})
    pages = d.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None, None
    return pages[0].get("title"), pages[0].get("extract", "")


def split_sections(text):
    """[(level, heading, body)] ; 첫 항목은 lead(heading='')"""
    parts = re.split(r"^(={2,6})\s*(.+?)\s*\1\s*$", text, flags=re.M)
    out = [(1, "", parts[0].strip())]
    for i in range(1, len(parts), 3):
        out.append((len(parts[i]), parts[i + 1].strip(), parts[i + 2].strip()))
    return out


def plot_section(text):
    """줄거리 섹션(하위 Act 섹션 포함) 본문과 사용한 제목. 없으면 (None, None)."""
    secs = split_sections(text)
    for i, (lvl, head, body) in enumerate(secs):
        if lvl == 2 and head.lower() in PLOT_HEADINGS:
            chunks = [body]
            for lvl2, head2, body2 in secs[i + 1:]:
                if lvl2 <= 2:
                    break
                chunks.append(body2)
            return "\n\n".join(c for c in chunks if c), head
    return None, None


ABBR = r"(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|No|Mt|Lt|Gen|Col|Prof|U\.S|U\.K|a\.m|p\.m)"


def first_sentences(text, n=N_SENTENCES, max_chars=MAX_CHARS):
    text = re.sub(r"\s+", " ", text).strip()
    sents, buf = [], ""
    for tok in re.split(r"(?<=[.!?])\s+(?=[\"'“(]?[A-Z0-9])", text):
        buf = f"{buf} {tok}".strip() if buf else tok
        if re.search(rf"\b{ABBR}\.$", buf):
            continue
        sents.append(buf)
        buf = ""
    if buf:
        sents.append(buf)
    out = ""
    for s in (sents if n is None else sents[:n]):
        if out and len(out) + 1 + len(s) > max_chars:
            break
        out = f"{out} {s}".strip()
    return out[:max_chars] if len(out) > max_chars else out


def process(row):
    title, typ, year = row["title"], row.get("type", ""), row.get("year")
    rec = {"work_id": row.get("work_id"), "title": title, "type": typ, "year": year,
           "wikipedia_title": "", "url": "", "confidence": "", "section_used": "",
           "synopsis": "", "plot_words": 0, "needs_review": "", "review_reason": "", "full_plot": ""}
    reasons = []
    page, conf, note = find_page(title, typ, year, row.get("wikipedia_title"))
    rec["confidence"] = conf
    if page is None:
        rec["needs_review"] = "Y"
        rec["review_reason"] = "page not found"
        return rec
    real, text = get_text(page)
    if text is None:
        rec["needs_review"] = "Y"
        rec["review_reason"] = f"page missing: {page}"
        return rec
    rec["wikipedia_title"] = real
    rec["url"] = "https://en.wikipedia.org/wiki/" + real.replace(" ", "_")
    plot, head = plot_section(text)
    if plot:
        rec["section_used"] = head
        rec["synopsis"] = plot[:MAX_CHARS] if N_SENTENCES is None else first_sentences(plot, N_SENTENCES)
        rec["plot_words"] = len(plot.split())
        rec["full_plot"] = plot[:32000]
    else:
        lead = split_sections(text)[0][2]
        rec["section_used"] = "(no plot section; lead paragraph)"
        rec["synopsis"] = first_sentences(lead, 4)
        rec["full_plot"] = ""
        reasons.append("no Plot/Synopsis section - lead used")
    if conf in ("low", "none"):
        reasons.append(f"page match uncertain ({note})")
    if not rec["synopsis"]:
        reasons.append("empty synopsis")
    if rec["plot_words"] and rec["plot_words"] < 80:
        reasons.append("very short plot section")
    rec["needs_review"] = "Y" if reasons else ""
    rec["review_reason"] = "; ".join(reasons)
    return rec


def write_xlsx(recs, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "synopses"
    head = ["work_id", "title", "type", "year", "synopsis (full plot / synopsis section)", "source (Wikipedia, CC BY-SA 4.0)",
            "section_used", "plot_words (full)", "match_confidence", "needs_review", "review_reason", "wikipedia_title"]
    ws.append(head)
    for r in recs:
        ws.append([r["work_id"], r["title"], r["type"], r["year"], r["synopsis"], r["url"], r["section_used"],
                   r["plot_words"], r["confidence"], r["needs_review"], r["review_reason"], r["wikipedia_title"]])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in zip("ABCDEFGHIJKL", [8, 34, 9, 7, 90, 50, 18, 12, 12, 11, 36, 30]):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if row[9].value == "Y":
            for c in row[:5]:
                c.fill = PatternFill("solid", fgColor="FFF2CC")
    ws.freeze_panes = "C2"
    ws2 = wb.create_sheet("full_plot")
    ws2.append(["work_id", "title", "full plot section (reference)"])
    for r in recs:
        ws2.append([r["work_id"], r["title"], r["full_plot"]])
    ws2.column_dimensions["B"].width = 34
    ws2.column_dimensions["C"].width = 120
    for row in ws2.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=TARGETS)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--shard", default="1/1", help="i/N : 전체 목록을 N등분해 i번째만 처리")
    args = ap.parse_args()
    df = pd.read_excel(args.targets)
    i0, n0 = (int(x) for x in args.shard.split("/"))
    if n0 > 1:
        df = df.iloc[(i0 - 1)::n0].reset_index(drop=True)
    recs = []
    for i, row in df.iterrows():
        try:
            rec = process(row.to_dict())
        except Exception as e:  # 한 작품 오류가 전체를 멈추지 않게
            rec = {"work_id": row.get("work_id"), "title": row["title"], "type": row.get("type"), "year": row.get("year"),
                   "wikipedia_title": "", "url": "", "confidence": "", "section_used": "", "synopsis": "",
                   "plot_words": 0, "needs_review": "Y", "review_reason": f"error: {e}", "full_plot": ""}
        recs.append(rec)
        print(f"[{i + 1}/{len(df)}] {rec['title']} -> {rec['wikipedia_title']} | {rec['confidence']} | {rec['review_reason']}", flush=True)
        time.sleep(0.4)
        if (i + 1) % 100 == 0:
            write_xlsx(recs, args.out)  # 중간 저장
    write_xlsx(recs, args.out)
    print(f"saved {args.out}: {len(recs)} works, {sum(1 for r in recs if r['needs_review']=='Y')} need review")


if __name__ == "__main__":
    sys.exit(main())
