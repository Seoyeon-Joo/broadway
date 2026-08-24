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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
    "first_preview": re.compile(r"(?:began performances|first preview)", re.IGNORECASE),
    "opening_date": re.compile(r"Opening(?:\s+Night)?(?:\s+is)?", re.IGNORECASE),
    "closing_date": re.compile(r"clos(?:ing|ed)(?:\s+on)?", re.IGNORECASE),
}
# 위 라벨 뒤 근처에 오는 실제 날짜 표현: 요일(선택) + 월 일, 연도
# 예: "Thursday, April 21, 2016" 또는 "April 21, 2016"
DATE_VALUE_RE = re.compile(r"(?:[A-Za-z]+,\s*)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})")


def _find_date_near_label(text, label_re, window=110):
    r"""label_re가 매치된 지점 바로 뒤 window자 안에서 실제 날짜 표현을 찾음.
    예전엔 라벨 바로 뒤에 날짜가 붙어있다고 가정했는데(:\s]*), 실제 문장은
    'Opening Night is Thursday, April 21, 2016'처럼 라벨과 날짜 사이에
    단어가 더 끼어있어서 못 잡았음 - 이번에 고침. first_preview 쪽은 극장 이름까지
    끼어들어서 window를 넉넉하게 잡음."""
    m = label_re.search(text)
    if not m:
        return ""
    snippet = text[m.end(): m.end() + window]
    dm = DATE_VALUE_RE.search(snippet)
    return dm.group(1) if dm else ""

# "Based on the novel/film/album ..." 형태의 원작 표기 탐지
BASED_ON_RE = re.compile(
    r"[Bb]ased\s+on\s+the\s+(novel|film|movie|book|album|play|true\s+story|life\s+of)",
    re.IGNORECASE,
)
# "1 - 25 of 63" 같은 기사 목록 페이지네이션 문구에서 총 기사 수(63)를 뽑음
# (실제로 American Psycho .html 페이지에서 확인함 - 뉴스 커버리지 volume을
# 화제성/버즈 프록시 변수로 쓸 수 있음)
ARTICLE_COUNT_RE = re.compile(r"\d+\s*-\s*\d+\s+of\s+(\d+)")


def slugify(title):
    s = title.upper().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def make_session():
    session = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


MIN_EXPECTED_LINKS = 10  # 정상적인 글자 인덱스 페이지라면 이보다 훨씬 많은 쇼가 있어야 함
                          # (한 글자로 시작하는 브로드웨이 역대 쇼가 10개 미만일 리 없음 -
                          # 이보다 적으면 진짜 빈 페이지가 아니라 차단/안내 페이지로 간주)


def fetch_letter_index(session, letter, max_retries=3):
    """grossesbyshow.php?letter=<X> 인덱스 페이지를 받아옴.

    *** 왜 상태코드 200만으로는 부족한가 ***
    실제로 유명 쇼(Oklahoma!, SIX: The Musical, Nine 등)가 대거 "슬러그 못 찾음"으로
    나온 걸 보고 원인을 추적한 결과: BroadwayWorld가 요청이 몰리면 429가 아니라
    "200 OK + 속도를 늦춰달라는 안내/차단 페이지"로 응답하는 것으로 보임. 예전
    코드는 resp.status_code == 200이면 무조건 정상 응답으로 캐싱했는데, 그러면
    이 빈 안내 페이지가 letter_cache에 그대로 박제되어서 그 글자로 시작하는
    나머지 쇼 전부가 이후 계속 실패함(실제로 O/S/T로 시작하는 쇼들이 무더기로
    몰려서 실패한 게 이 패턴과 일치).

    그래서 실제 쇼 링크 개수(MIN_EXPECTED_LINKS 미만이면 의심)까지 확인하고,
    부족하면 캐싱하지 않고 잠깐 대기 후 재시도함. 그래도 계속 부족하면 어쩔 수
    없이 None을 반환(캐싱은 안 함 - 다음 쇼가 같은 글자를 다시 시도할 때 새로
    받아오게 해서, 언젠가 정상 응답이 오면 그때부터라도 살아나게 함)."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(f"{BASE}/grossesbyshow.php", params={"letter": letter},
                                headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"    [letter={letter} 인덱스 요청 실패, 시도 {attempt}/{max_retries}] {e}")
            time.sleep(2 * attempt)
            continue

        if resp.status_code != 200:
            print(f"    [letter={letter} 인덱스 상태코드 {resp.status_code}, "
                  f"시도 {attempt}/{max_retries}]")
            time.sleep(2 * attempt)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        n_links = len(soup.select("a[href^='/grosses/']"))
        if n_links >= MIN_EXPECTED_LINKS:
            return soup

        print(f"    [letter={letter} 인덱스에 쇼 링크가 {n_links}개뿐 - 차단/안내 "
              f"페이지로 의심, 시도 {attempt}/{max_retries}]")
        time.sleep(3 * attempt)  # 429 KOPIS 경험상 백오프 필요

    print(f"    [letter={letter} 인덱스 {max_retries}회 재시도 후에도 비정상 - "
          f"이번엔 포기(캐싱 안 함, 다음 쇼가 다시 시도함)]")
    return None


def find_show_slug(title, session, letter_cache):
    """title이 뭐든(폐막작 포함) 찾을 수 있도록 grossesbyshow.php?letter=<A-Z> A-Z
    인덱스 페이지를 사용. (이전 버전은 grosses.php를 썼는데, 거기는 '현재 상연 중'인
    쇼만 나와서 대부분의 과거/폐막 쇼를 못 찾는 문제가 있었음 - 실제 확인됨)
    letter_cache: {letter: soup} 형태로 한 번 받아온 글자 페이지는 재사용.

    매칭 2단계:
      1. 완전 일치 (영숫자만 남기고 비교)
      2. 그래도 못 찾으면, 제목에 콜론(:)이 있는 경우 콜론 앞부분만으로 재시도.
         실제로 확인된 사례: Playbill/broadway.csv엔 "Danny Gans on Broadway: The
         Man of Many Voices"로 풀네임이 들어있는데, BroadwayWorld grosses 인덱스엔
         부제 없이 "Danny Gans On Broadway"로만 등재돼 있어서 완전일치가 실패했음.
         이런 축약 등재가 흔해서 콜론 기준 폴백을 추가함.

    반환값: (slug, is_ambiguous, match_type) 튜플.
      - is_ambiguous: 동일 제목으로 매치가 여러 개 있으면 True (=완전히 같은 이름으로
        재상연된 리바이벌 방어 로직 - BroadwayWorld가 "Cabaret at the Kit Kat Club"처럼
        리바이벌을 아예 개명해서 따로 등재하는 경우가 많지만, 개명 없는 경우도 있음).
        첫 번째 매치를 쓰되 이 플래그로 표시해서 target 빌더가 신뢰도를 낮춰 잡게 함.
      - match_type: "exact" 또는 "colon_stripped" - 후자는 부제를 뗀 근사 매칭이라
        완전히 다른 쇼를 잘못 골랐을 여지가 exact보다 약간 더 있음을 표시."""
    first_char = title.strip()[0].upper() if title.strip() else ""
    letter = first_char if first_char.isalpha() else "1"  # 숫자/기호로 시작하면 '#' 페이지(letter=1)

    if letter not in letter_cache:
        result = fetch_letter_index(session, letter)
        if result is not None:
            letter_cache[letter] = result
        # None이면 캐싱 안 함 - 다음 쇼가 같은 글자를 다시 시도하게 둠

    soup = letter_cache.get(letter)
    if soup is None:
        return None, False, ""

    def norm(s):
        return re.sub(r"[^A-Z0-9]", "", s.upper())

    links = soup.select("a[href^='/grosses/']")

    def find_all(target_norm):
        return [a["href"].split("/grosses/")[-1] for a in links if norm(a.get_text(strip=True)) == target_norm]

    matches = find_all(norm(title))
    match_type = "exact"

    if not matches and ":" in title:
        stripped = title.split(":")[0].strip()
        if stripped:
            matches = find_all(norm(stripped))
            match_type = "colon_stripped"

    if not matches:
        return None, False, ""
    is_ambiguous = len(set(matches)) > 1
    return matches[0], is_ambiguous, match_type




def title_slug(title):
    """'/shows/<Title-Slug>-<id>.html' 형태의 URL에 쓰는 슬러그.
    grosses/<slug>의 전부-대문자 슬러그와는 다른, 원래 대소문자를 유지한 하이픈 연결
    (실제 확인됨: 'American Psycho' -> 'American-Psycho')."""
    s = re.sub(r"[^\w\s-]", "", title).strip()
    s = re.sub(r"\s+", "-", s)
    return s


def fetch_genre_map(session):
    """현재 상연작만 잡힘 - 종영작은 쇼 페이지 <title> 태그 휴리스틱으로 보조 판별."""
    resp = session.get(f"{BASE}/grosses.php", headers=HEADERS, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    genre_map = {}
    for a in soup.select("a[href^='/grosses/']"):
        t = a.get("title", "")
        if t in ("Musical", "Play"):
            genre_map[a["href"].split("/grosses/")[-1]] = t
    return genre_map


def fetch_show_page(title, showid, session):
    """'/shows/<Title-Slug>-<id>.html' 메인 쇼 페이지 -> 장르/개막·폐막·프리뷰 날짜/원작 추출.
    실제 확인됨(American Psycho showid=331080): 이 페이지의 시놉시스에 'Based on the
    best-selling novel by...' 문구가 직접 들어있고, <title> 태그에 'Musical'/'Play'가
    포함됨 (예: 'American Psycho - 2016 Broadway Musical: Tickets & Info | Broadway World').
    이전 버전은 /grosses/<slug> 페이지(흥행표만 있고 시놉시스 없음)를 봐서 genre/based_on이
    항상 비어있었음 - 이번에 수정."""
    if not showid:
        return {}, ""
    url = f"{BASE}/shows/{title_slug(title)}-{showid}.html"
    resp = session.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return {}, ""
    soup = BeautifulSoup(resp.text, "html.parser")

    text = soup.get_text(" ", strip=True)
    meta = {}
    for key, label_re in DATE_LABEL_RE.items():
        meta[key] = _find_date_near_label(text, label_re)

    based_on_match = BASED_ON_RE.search(text)
    meta["based_on"] = based_on_match.group(1).title() if based_on_match else ""

    article_count_match = ARTICLE_COUNT_RE.search(text)
    meta["n_articles_total"] = article_count_match.group(1) if article_count_match else ""

    title_tag_text = (soup.title.get_text() if soup.title else "").lower()
    genre_guess = "Musical" if "musical" in title_tag_text else ("Play" if "play" in title_tag_text else "")
    return meta, genre_guess


def fetch_showid(slug, session):
    """/grosses/<slug> 페이지에서 showid만 추출 (title_slug 페이지 URL을 만들려면
    showid가 먼저 필요해서, 이 단계는 여전히 grosses 페이지를 거쳐야 함)."""
    resp = session.get(f"{BASE}/grosses/{slug}", headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        m = SHOWID_RE.search(a["href"])
        if m:
            return m.group(1)
    return None


def _text_after(tag):
    """태그 바로 다음에 오는 형제 노드의 텍스트를 가져옴 (역할/배역명 추출용).
    주의: tag.find_next(string=True)는 태그 자기 자신의 텍스트를 반환하는 버그가 있어서
    반드시 next_sibling으로 순회해야 함 (실제 확인된 버그, 처음엔 몰랐음)."""
    sib = tag.next_sibling
    while sib is not None and not str(sib).strip():
        sib = sib.next_sibling
    if sib is None:
        return ""
    if isinstance(sib, str):
        return sib.strip()
    return sib.get_text(" ", strip=True)


def fetch_cast(title, showid, session):
    """'/shows/<Title-Slug>-<id>/cast' 페이지에서 캐스트(사람+배역) 추출.
    실제 확인됨(American Psycho showid=331080, 사용자가 스크린샷으로 확인해줌):
    이전 버전의 'shows/cast.php?showid=...' 쿼리스트링 형식이 아니라, 경로형
    '/shows/<Title-Slug>-<id>/cast'가 맞는 최신 URL임. 배우 이름(빨간 볼드) 아래
    배역명, 그 아래 프로필 문단이 붙는 카드 구조."""
    if not showid:
        return []
    url = f"{BASE}/shows/{title_slug(title)}-{showid}/cast"
    resp = session.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    cast_entries = []
    for tag in soup.find_all("a", href=PERSON_RE):
        name = tag.get_text(strip=True)
        if not name:
            continue
        role = _text_after(tag)[:60]
        cast_entries.append((name, [role] if role else []))
    return cast_entries


def fetch_creative_team(title, showid, session):
    """'/shows/creative.php?showid=<id>' 페이지(예전 쿼리스트링 URL, 실제로 확인됨 -
    /shows/<Title>-<id>/creative 가 아니라 이 옛날 URL이 지금도 살아있음, 42nd Street/
    Peerless 두 쇼로 검증)에서 창작진(Production Staff) + 보너스로 이 페이지 안에 같이
    있는 "Awards and Nominations" 섹션(Drama Desk/Outer Critics Circle/Tony/Hewes 등
    여러 시상식 통합)까지 한 번에 뽑음. 페이지 구조: 사람 이름(볼드 링크) 바로 뒤에
    역할(볼드 텍스트)이 붙음."""
    if not showid:
        return [], {}
    resp = session.get(f"{BASE}/shows/creative.php", params={"showid": showid}, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return [], {}
    soup = BeautifulSoup(resp.text, "html.parser")

    creative_entries = []
    for tag in soup.find_all("a", href=PERSON_RE):
        name = tag.get_text(strip=True)
        if not name:
            continue
        context = _text_after(tag)
        # 실제 페이지에서 역할명이 이미 깔끔한 텍스트로 나와서(Director/Choreographer/
        # Bookwriter 등) 키워드 부분매칭보다 원문 그대로 쓰는 게 더 정확함
        # (이전엔 ROLE_KEYWORDS 부분일치 때문에 'Bookwriter'가 'Book'으로 잘리는 문제가 있었음)
        creative_entries.append((name, context[:40] or "Creative Team"))

    award_counts, award_detail = _parse_awards_section(soup)
    award_counts["award_bodies_detail"] = award_detail
    return creative_entries, award_counts


def _parse_awards_section(soup):
    """creative.php 페이지 하단의 'Awards and Nominations' 섹션을 시상식(연도+이름)
    단위로 나누고, 그 안의 각 줄(부문: 후보자 won./was nominated but did not win.)을
    구조화해서 시상식별 수상/후보 횟수를 따로 셈. 시상식 이름을 하드코딩하지 않아서
    Drama Desk/Drama League/Outer Critics Circle/Tony 등 그 쇼에 실제로 걸린 모든
    시상식이 자동으로 다 잡힘. '후보 올랐지만 못 탄 것'과 '실제 수상'을 구분해야
    통제변수로서 의미가 달라서(수상이 더 강한 품질 신호) 텍스트로 명확히 나눔."""
    text = soup.get_text("\n", strip=True)
    idx = text.lower().find("awards and nominations")
    if idx == -1:
        return {}, ""
    awards_text = text[idx + len("awards and nominations"): idx + len("awards and nominations") + 6000]

    header_re = re.compile(r"^(\d{4})\s+((?:[A-Z][a-zA-Z]*\s*)+Awards?)\s*$", re.MULTILINE)
    line_re = re.compile(r"^(.+?):\s*(.+?)\s+(won|was nominated but did not win)\.?\s*$", re.MULTILINE)

    headers = list(header_re.finditer(awards_text))
    records = []
    for i, h in enumerate(headers):
        ceremony = h.group(2).strip()
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(awards_text)
        section = awards_text[start:end]
        for lm in line_re.finditer(section):
            category, nominee, result = lm.groups()
            records.append({
                "ceremony": ceremony,
                "category": category.strip(),
                "nominee": nominee.strip(),
                "result": "Won" if result == "won" else "Nominated",
            })

    counts = {}
    detail_lines = []
    for r in records:
        key_base = re.sub(r"[^a-z0-9]+", "_", r["ceremony"].lower()).strip("_")
        counts[f"{key_base}_nominations"] = counts.get(f"{key_base}_nominations", 0) + 1
        if r["result"] == "Won":
            counts[f"{key_base}_wins"] = counts.get(f"{key_base}_wins", 0) + 1
        detail_lines.append(f"{r['ceremony']} | {r['category']} | {r['nominee']} | {r['result']}")

    return counts, "; ".join(detail_lines)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="broadway.csv 경로 - 'show' 컬럼에서 고유 쇼 이름을 뽑음")
    ap.add_argument("--shows", nargs="+", default=None, help="테스트용: 쇼 이름을 직접 나열")
    ap.add_argument("--existing", default=None,
                     help="기존 broadwayworld_full.csv 경로. 지정하면 그 안에 이미 있는 title은 건너뜀")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--limit", type=int, default=None, help="테스트용 처리 개수 제한")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    session = make_session()

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
    letter_cache = {}
    time.sleep(args.sleep)

    rows = []
    n_errors = 0
    for i, title in enumerate(titles, 1):
        try:
            slug = slugify(title)
            title_ambiguous = False
            match_type = "direct"
            test = session.head(f"{BASE}/grosses/{slug}", headers=HEADERS, timeout=30)
            if test.status_code != 200:
                found, title_ambiguous, match_type = find_show_slug(title, session, letter_cache)
                if found:
                    slug = found
                else:
                    print(f"[{i}/{len(titles)}] '{title}' -> 슬러그 못 찾음, 스킵")
                    continue

            showid = fetch_showid(slug, session)
            time.sleep(args.sleep)

            meta, genre_guess = fetch_show_page(title, showid, session)
            time.sleep(args.sleep)

            cast_entries = fetch_cast(title, showid, session)
            time.sleep(args.sleep)

            creative_entries, award_bonus = fetch_creative_team(title, showid, session)
            time.sleep(args.sleep)

            genre = genre_map.get(slug, "") or genre_guess

            cast_str = "; ".join(f"{name} as {', '.join(roles)}" if roles else name for name, roles in cast_entries)
            # 상위 1~2명만 별도로 뽑음 - 리바이벌 유튜브 검색 쿼리에 배우 이름을 넣어
            # 같은 제목의 다른 시기 프로덕션과 구분하는 정확도를 높이기 위함
            # (cast.php 페이지는 보통 주연부터 나열되는 걸로 확인해서 순서 그대로 앞에서 자름)
            lead_cast_str = "; ".join(name for name, _roles in cast_entries[:2])
            creative_str = "; ".join(f"{name} ({role})" for name, role in creative_entries)
            producer_str = "; ".join(name for name, role in creative_entries if "producer" in role.lower())

            row = {
                "title": title,
                "slug": slug,
                "showid": showid,
                "title_ambiguous": title_ambiguous,  # True면 동일 제목 리바이벌이 여러 개 있어서
                                                       # 이 메타(opening_date/cast 등)가 정확히
                                                       # 어느 프로덕션 것인지 확신할 수 없음
                "title_match_type": match_type,  # direct/exact/colon_stripped - colon_stripped는
                                                   # 부제를 뗀 근사 매칭이라 살짝 덜 확실함
                "genre": genre,
                "first_preview": meta.get("first_preview", ""),
                "opening_date": meta.get("opening_date", ""),
                "closing_date": meta.get("closing_date", ""),
                "based_on": meta.get("based_on", ""),
                "n_articles_total": meta.get("n_articles_total", ""),
                "cast": cast_str,
                "lead_cast": lead_cast_str,
                "creative_team": creative_str,
                "producer": producer_str,
                **award_bonus,  # 시상식별 동적 컬럼(예: tony_awards_wins, drama_desk_awards_nominations 등) + award_bodies_detail
            }
            rows.append(row)
            n_award_wins = sum(v for k, v in award_bonus.items() if k.endswith("_wins") and isinstance(v, int))
            n_award_noms = sum(v for k, v in award_bonus.items() if k.endswith("_nominations") and isinstance(v, int))
            print(f"[{i}/{len(titles)}] '{title}' -> slug={slug}, showid={showid}, "
                  f"genre={genre}, cast={len(cast_entries)}명, creative={len(creative_entries)}명, "
                  f"producer={'있음' if producer_str else '없음'}, "
                  f"awards={n_award_wins}승/{n_award_noms}노미, "
                  f"based_on={meta.get('based_on') or '원작 없음/오리지널'}"
                  + (" [주의: 동일 제목 리바이벌 다수 -> 메타 신뢰도 낮음]" if title_ambiguous else "")
                  + (" [부제 생략 매칭]" if match_type == "colon_stripped" else ""))
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(titles)}] '{title}' -> 오류 발생, 스킵: {e}")
        time.sleep(args.sleep)

    print(f"\n처리 중 오류 {n_errors}건 (스킵하고 계속 진행함)")

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
