"""
fetch_broadwayworld_full.py
=============================
data/broadway.csv의 고유 show 이름을 기준으로 BroadwayWorld에서 아래 정보를 수집.

  - 장르(genre): Musical/Play
  - 개막일(opening_date) / 폐막일(closing_date) / 첫 프리뷰(first_preview)
  - 캐스트(cast): 배우 이름 + 배역
  - 창작진(creative_team): 연출/안무/무대디자인 등 Production Team 크레딧 전체
  - producer: creative_team 중 역할이 "Producer"인 사람만 따로 뽑아 별도 컬럼으로 분리
    (creative_team 문자열 안에는 그대로 다 남아있고, producer는 거기서 필터링만 한 것)

기존에 이미 수집한 쇼는 다시 긁지 않음 — --existing으로 지정한 기존
broadwayworld_full.csv를 읽어서 이미 있는 title은 스킵하고, 신규 쇼만 수집한 뒤
기존 데이터 + 신규 데이터를 합쳐서 같은 파일에 다시 저장함 (주간 자동화용).

*** 주의: cast/creative_team 파싱은 BroadwayWorld cast.php 페이지의 실제 구조를
한 개 쇼(Wicked)로 확인한 뒤 만든 로직이에요. 전체 쇼가 동일 구조라는 보장은 없어서,
처음 --limit 10 정도로 테스트해서 결과를 눈으로 확인하는 걸 권장해요.

Usage:
  python fetch_broadwayworld_full.py --raw data/broadway.csv \
      --existing data/broadwayworld_full.csv --out-dir data --sleep 1.0
  python fetch_broadwayworld_full.py --shows "Wicked" "Hamilton" --out-dir . --limit 2  # 테스트용
"""
import argparse
import os
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.broadwayworld.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

PERSON_RE = re.compile(r"^/people/(?!character/)[^/]+/?$")
CHARACTER_RE = re.compile(r"^/people/character/([^/]+)-(\d+)/?$")
SHOWID_RE = re.compile(r"showid=(\d+)")

ROLE_KEYWORDS = [
    "Director", "Choreographer", "Music Director", "Musical Director", "Orchestrator",
    "Composer", "Lyricist", "Book", "Producer", "Scenic Designer", "Set Designer",
    "Costume Designer", "Lighting Designer", "Sound Designer", "Projection Designer",
    "Hair", "Wig Designer", "Make-Up Designer", "Special Effects Designer",
    "Flying Effects", "Musical Staging", "Casting", "General Manager",
]

DATE_LABEL_RE = {
    "first_preview": re.compile(r"First\s+Preview[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE),
    "opening_date": re.compile(r"Opening(?:\s+Date)?[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE),
    "closing_date": re.compile(r"Clos(?:ing|ed)(?:\s+Date)?[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE),
}

# "Based on the novel/film/album ..." 형태의 원작 표기 탐지
BASED_ON_RE = re.compile(
    r"[Bb]ased\s+on\s+the\s+(novel|film|movie|book|album|play|true\s+story|life\s+of)",
    re.IGNORECASE,
)


def slugify(title):
    s = title.upper().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def find_show_slug(title, session, grosses_html_cache):
    if grosses_html_cache["soup"] is None:
        resp = session.get(f"{BASE}/grosses.php", headers=HEADERS, timeout=20)
        grosses_html_cache["soup"] = BeautifulSoup(resp.text, "html.parser")
    soup = grosses_html_cache["soup"]
    target_norm = re.sub(r"[^A-Z0-9]", "", title.upper())
    for a in soup.select("a[href^='/grosses/']"):
        if re.sub(r"[^A-Z0-9]", "", a.get_text(strip=True).upper()) == target_norm:
            return a["href"].split("/grosses/")[-1]
    return None


def fetch_genre_map(session):
    """현재 상연작만 잡힘 - 종영작은 쇼 페이지 <title> 태그 휴리스틱으로 보조 판별."""
    resp = session.get(f"{BASE}/grosses.php", headers=HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    genre_map = {}
    for a in soup.select("a[href^='/grosses/']"):
        t = a.get("title", "")
        if t in ("Musical", "Play"):
            genre_map[a["href"].split("/grosses/")[-1]] = t
    return genre_map


def fetch_show_page(slug, session):
    """개별 쇼 그로스 페이지 -> showid + 개막/폐막/프리뷰 날짜 추출."""
    resp = session.get(f"{BASE}/grosses/{slug}", headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        return None, {}
    soup = BeautifulSoup(resp.text, "html.parser")

    showid = None
    for a in soup.find_all("a", href=True):
        m = SHOWID_RE.search(a["href"])
        if m:
            showid = m.group(1)
            break

    text = soup.get_text(" ", strip=True)
    meta = {}
    for key, pattern in DATE_LABEL_RE.items():
        m = pattern.search(text)
        meta[key] = m.group(1) if m else ""

    based_on_match = BASED_ON_RE.search(text)
    meta["based_on"] = based_on_match.group(1).title() if based_on_match else ""

    genre_guess = "Musical" if "musical" in (soup.title.get_text() if soup.title else "").lower() else (
        "Play" if "play" in (soup.title.get_text() if soup.title else "").lower() else ""
    )
    return showid, meta, genre_guess


def fetch_cast_and_creative(showid, session):
    """cast.php?showid=... 페이지에서 캐스트(사람+배역)와 창작진(사람+역할)을 분리 추출.
    페이지 구조(확인됨, Wicked showid=7848 기준): 배우 링크는 /people/<slug>/ 형태고 바로
    옆/근처에 배역명이 붙음. 하단 'Production Team' 헤더 이후로는 ROLE_KEYWORDS에 매칭되는
    역할명이 사람 이름 옆에 붙는 구조."""
    if not showid:
        return [], [], ""
    resp = session.get(f"{BASE}/shows/cast.php?showid={showid}", headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        return [], [], ""
    soup = BeautifulSoup(resp.text, "html.parser")

    cast_entries = []
    creative_entries = []

    # "Production Team" 헤더를 기준으로 앞은 캐스트, 뒤는 창작진으로 구간을 나눔
    full_text_nodes = soup.find_all(["a", "h1", "h2", "h3", "b", "strong"])
    in_production_team = False

    for tag in full_text_nodes:
        tag_text = tag.get_text(strip=True)
        if not in_production_team and "production team" in tag_text.lower():
            in_production_team = True
            continue

        if tag.name == "a" and PERSON_RE.match(tag.get("href", "")):
            name = tag.get_text(strip=True)
            if not name:
                continue
            # 이름 근처 텍스트에서 역할/배역 후보를 찾음 (형제 텍스트 노드 활용)
            context = ""
            nxt = tag.find_next(string=True)
            if nxt:
                context = str(nxt).strip()

            if in_production_team:
                matched_role = next((r for r in ROLE_KEYWORDS if r.lower() in context.lower()), None)
                creative_entries.append((name, matched_role or context[:40] or "Creative Team"))
            else:
                role = context[:60] if context else ""
                cast_entries.append((name, [role] if role else []))

    genre_guess = ""
    return cast_entries, creative_entries, genre_guess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="broadway.csv 경로 - 'show' 컬럼에서 고유 쇼 이름을 뽑음")
    ap.add_argument("--shows", nargs="+", default=None, help="테스트용: 쇼 이름을 직접 나열")
    ap.add_argument("--existing", default=None,
                     help="기존 broadwayworld_full.csv 경로. 지정하면 그 안에 이미 있는 title은 건너뜀")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--limit", type=int, default=None, help="테스트용 처리 개수 제한")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    session = requests.Session()

    if args.shows:
        titles = args.shows
    elif args.raw:
        df = pd.read_csv(args.raw, sep=None, engine="python", encoding="utf-8-sig")
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        if "show" not in df.columns:
            print(f"'show' 컬럼이 없어요. 실제 컬럼: {list(df.columns)}")
            raise SystemExit(1)
        titles = df["show"].dropna().drop_duplicates().tolist()
    else:
        ap.error("--raw 또는 --shows 중 하나는 필요해요")

    existing_df = None
    if args.existing and os.path.isfile(args.existing):
        existing_df = pd.read_csv(args.existing, sep=None, engine="python", encoding="utf-8-sig")
        already_done = set(existing_df["title"])
        before = len(titles)
        titles = [t for t in titles if t not in already_done]
        print(f"기존 {len(already_done)}개 쇼는 건너뜀 ({before} -> {len(titles)}개 신규 처리 대상)")

    if args.limit:
        titles = titles[: args.limit]

    print(f"총 {len(titles)}개 쇼 처리 예정")
    genre_map = fetch_genre_map(session)
    grosses_cache = {"soup": None}
    time.sleep(args.sleep)

    rows = []
    for i, title in enumerate(titles, 1):
        slug = slugify(title)
        test = session.head(f"{BASE}/grosses/{slug}", headers=HEADERS, timeout=15)
        if test.status_code != 200:
            found = find_show_slug(title, session, grosses_cache)
            if found:
                slug = found
            else:
                print(f"[{i}/{len(titles)}] '{title}' -> 슬러그 못 찾음, 스킵")
                continue

        showid, meta, genre_guess = fetch_show_page(slug, session)
        time.sleep(args.sleep)

        cast_entries, creative_entries, _ = fetch_cast_and_creative(showid, session)
        time.sleep(args.sleep)

        genre = genre_map.get(slug, "") or genre_guess

        cast_str = "; ".join(f"{name} as {', '.join(roles)}" if roles else name for name, roles in cast_entries)
        creative_str = "; ".join(f"{name} ({role})" for name, role in creative_entries)
        producer_str = "; ".join(name for name, role in creative_entries if "producer" in role.lower())

        row = {
            "title": title,
            "slug": slug,
            "showid": showid,
            "genre": genre,
            "first_preview": meta.get("first_preview", ""),
            "opening_date": meta.get("opening_date", ""),
            "closing_date": meta.get("closing_date", ""),
            "based_on": meta.get("based_on", ""),
            "cast": cast_str,
            "creative_team": creative_str,
            "producer": producer_str,
        }
        rows.append(row)
        print(f"[{i}/{len(titles)}] '{title}' -> slug={slug}, showid={showid}, "
              f"genre={genre}, cast={len(cast_entries)}명, creative={len(creative_entries)}명, "
              f"producer={'있음' if producer_str else '없음'}, "
              f"based_on={meta.get('based_on') or '원작 없음/오리지널'}")

    if not rows:
        print("\n신규로 수집된 데이터가 없어요.")
        return

    new_df = pd.DataFrame(rows)
    if existing_df is not None:
        out_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(subset=["title"], keep="last")
    else:
        out_df = new_df

    out_path = os.path.join(args.out_dir, "broadwayworld_full.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n저장 완료: 신규 {len(new_df)}개 + 기존 -> 총 {len(out_df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
