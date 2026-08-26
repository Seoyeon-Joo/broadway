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
import unicodedata

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.broadwayworld.com"
# User-Agent만 있으면 봇으로 더 쉽게 잡히는 것으로 보여서(실제로 유명 쇼들이
# 대거 빈 응답을 받은 정황), 실제 브라우저가 보내는 헤더 세트에 가깝게 보강함
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.broadwayworld.com/grosses.php",
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


MIN_EXPECTED_LINKS = 10  # 참고용 기준치일 뿐 차단 판정에는 안 씀(아래 주석 참고).
                          # 대부분의 글자 인덱스는 이보다 훨씬 많은 쇼가 있지만,
                          # 실제로 확인해보니 U(5개)/Y(6개)/Z(2개)처럼 원래 이
                          # 기준보다 적은 게 정상인 글자도 있음. 그래서 이 값보다
                          # 적다고 재시도/차단 취급하면 U/Y/Z로 시작하는 쇼가
                          # (Urinetown, You're a Good Man Charlie Brown,
                          # Young Frankenstein, Zoya's Apartment 등 실존하는 쇼
                          # 포함) 통째로 "슬러그 못 찾음"으로 스킵되는 오탐이 남.
                          # (2026-08-24, https://www.broadwayworld.com/grossesbyshow.php?letter=u
                          # 등을 직접 열어서 U=5/Y=6/Z=2가 실제 전체 목록임을 확인함)


def fetch_letter_index(session, letter, max_retries=3):
    """grossesbyshow.php?letter=<X> 인덱스 페이지를 받아옴.

    *** 왜 상태코드 200만으로는 부족한가 ***
    실제로 유명 쇼(Oklahoma!, SIX: The Musical, Nine 등)가 대거 "슬러그 못 찾음"으로
    나온 적이 있어서 원인을 추적한 결과: BroadwayWorld가 요청이 몰리면 429가 아니라
    "200 OK + 속도를 늦춰달라는 안내/차단 페이지"로 응답하는 것으로 보임. 예전
    코드는 resp.status_code == 200이면 무조건 정상 응답으로 캐싱했는데, 그러면
    이 빈 안내 페이지가 letter_cache에 그대로 박제되어서 그 글자로 시작하는
    나머지 쇼 전부가 이후 계속 실패함.

    *** 링크 개수가 적다고 차단으로 판정하면 안 되는 이유 (2026-08-24 수정) ***
    이전 버전은 "링크가 MIN_EXPECTED_LINKS(10개) 미만이면 차단/안내 페이지"로 보고
    최대 3회 재시도 후 포기(None 반환, 캐싱 안 함)했음. 근데 U/Y/Z 인덱스 페이지를
    직접 열어서 확인해보니 이건 오탐이었음 - U=5개, Y=6개, Z=2개가 그 글자로
    시작하는 쇼의 "실제 전체 개수"였고, 3번 재시도해도 매번 똑같은(정상) 응답이
    돌아온 것뿐이었음. 그 결과 이 세 글자로 시작하는 모든 쇼(Urinetown, You're a
    Good Man Charlie Brown, Young Frankenstein, Zoya's Apartment 등 실존 쇼 포함)가
    letter_cache에 아예 못 들어가서 매번 새로 요청하며 "슬러그 못 찾음"으로 스킵됐음.

    그래서 이제는 링크 개수 대신 아래 두 가지만 확인함:
      1. 상태코드 200
      2. <title> 태그가 요청한 letter와 실제로 일치 (다른 글자 페이지가 캐시/프록시
         문제로 잘못 오는 경우를 걸러내기 위함 - 이 문제도 실제로 관찰된 적 있음:
         letter=U로 요청했는데 letter=A 페이지 내용이 돌아온 사례)
    링크가 0개인 경우만 "진짜로 이상한 응답"으로 보고 재시도하고, 그 외엔 링크
    개수와 무관하게(1개든 50개든) 정상 응답으로 받아들여 캐싱함. 링크 수가 적을
    땐 그냥 참고용으로 로그만 남김 - 그 글자가 원래 적은 건지 나중에 데이터로
    확인할 수 있게.

    *** letter="1"의 <title> 표시가 "1"이 아니라 "#"인 문제 (2026-08-24 추가 수정) ***
    숫자/기호로 시작하는 쇼는 letter=1로 요청하는데, 실제 페이지 <title>은
    "Broadway Grosses by Show: 1"이 아니라 "Broadway Grosses by Show: # — A-Z
    Index"로 옴(실제 파이프라인 로그로 확인됨: '"Master Harold"...and the Boys',
    '& Juliet', '3 from Brooklyn' 등 letter=1에 해당하는 쇼가 전부 "다른 글자
    페이지로 의심"에 걸려 매번 3회 재시도 후 포기했음 - 위 title 일치 검사가 "1"과
    "#"를 비교해서 항상 실패했던 것). 그래서 letter="1"일 때만 기대 표시를 "#"로
    바꿔서 비교함."""
    display_letter = "#" if letter == "1" else letter.upper()
    expected_title_frag = f"Broadway Grosses by Show: {display_letter}"
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
                  f"시도 {attempt}/{max_retries}] 응답 앞부분: {resp.text[:300]!r}")
            time.sleep(2 * attempt)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.find("title")
        page_title = title_tag.get_text(strip=True) if title_tag else "(title 태그 없음)"

        if expected_title_frag not in page_title:
            # 요청한 글자와 실제로 받은 페이지가 다름 - 캐시/프록시 쪽 문제로
            # 보이므로 캐싱하지 않고 재시도
            print(f"    [letter={letter} 요청했는데 실제 응답 <title>={page_title!r} - "
                  f"다른 글자 페이지로 의심, 시도 {attempt}/{max_retries}]")
            time.sleep(2 * attempt)
            continue

        n_links = len(soup.select("a[href*='/grosses/']"))
        if n_links == 0:
            # <title>은 맞는데 쇼 링크가 정말 하나도 없는 건 여전히 이상한 상황
            # (모든 글자에 최소 1개는 있는 게 정상) - 이 경우만 진짜로 재시도
            raw_substring_count = resp.text.count("/grosses/")
            print(f"    [letter={letter} 인덱스에 쇼 링크가 0개 - 이상 응답 의심, "
                  f"시도 {attempt}/{max_retries}] 응답 길이={len(resp.text)}자, "
                  f"<title>={page_title!r}, 원본 '/grosses/' 등장 횟수={raw_substring_count}, "
                  f"본문 앞부분: {resp.text[:300]!r}")
            time.sleep(3 * attempt)
            continue

        if n_links < MIN_EXPECTED_LINKS:
            # 차단 취급하지 않고 그냥 참고 로그만 남김 - U/Y/Z처럼 원래 적을 수 있음
            print(f"    [letter={letter} 인덱스 링크 {n_links}개 - 참고: 이 글자는 "
                  f"원래 쇼가 적어서 정상일 수 있음(차단 아님, 재시도 안 함)]")

        if letter == "1":
            # *** 2026-08-24 진단용 추가 ***
            # letter=1("#" 페이지)로 라우팅한 게 맞는지, 그리고 실제로 어떤 쇼들이
            # 여기 들어있는지 아직 실제 파이프라인 실행 환경에서 확인된 적이 없음
            # (제가 가진 웹 조회 도구로는 이 URL만 캐시가 꼬여서 매번 다른 글자의
            # 페이지가 섞여 나옴 - 실제 GitHub Actions 러너의 요청과는 무관한
            # 도구 쪽 문제로 보임). 그래서 여기서 실제로 받은 링크 텍스트 전체를
            # 한 번 찍어서, 다음 실행 로그에서 우리가 세운 라우팅 가정
            # ("따옴표/&/숫자로 시작하는 제목은 전부 여기로 온다")이 실제 사이트
            # 분류와 맞는지 눈으로 바로 확인할 수 있게 함. "#" 페이지는 보통
            # 항목 수가 많지 않을 것으로 예상되니 로그 부담은 적음.
            all_link_texts = [a.get_text(strip=True) for a in soup.select("a[href*='/grosses/']")]
            print(f"    [letter=1(#) 페이지 실제 쇼 목록 {len(all_link_texts)}개 (진단용): "
                  f"{all_link_texts}]")
        return soup

    print(f"    [letter={letter} 인덱스 {max_retries}회 재시도 후에도 비정상 - "
          f"이번엔 포기(캐싱 안 함, 다음 쇼가 다시 시도함)]")
    return None


def find_show_slug(title, session, letter_cache):
    """title이 뭐든(폐막작 포함) 찾을 수 있도록 grossesbyshow.php?letter=<A-Z> A-Z
    인덱스 페이지를 사용. (이전 버전은 grosses.php를 썼는데, 거기는 '현재 상연 중'인
    쇼만 나와서 대부분의 과거/폐막 쇼를 못 찾는 문제가 있었음 - 실제 확인됨)
    letter_cache: {letter: soup} 형태로 한 번 받아온 글자 페이지는 재사용.

    매칭 4단계 (숫자가 커질수록 근사 매칭이라 신뢰도가 낮아짐):
      1. 완전 일치 (영숫자만 남기고 비교)
      2. 콜론(:) 앞부분만으로 재시도. 실제로 확인된 사례: Playbill/broadway.csv엔
         "Danny Gans on Broadway: The Man of Many Voices"로 풀네임이 들어있는데,
         BroadwayWorld grosses 인덱스엔 부제 없이 "Danny Gans On Broadway"로만
         등재돼 있어서 완전일치가 실패했음.
      3. 쉼표(,) 앞부분만으로 재시도. 2와 같은 패턴인데 구분자가 쉼표인 경우
         (콜론 폴백만으로는 못 잡음).
      4. 접두어 포함 매칭. 2/3은 구분자(:,)가 있을 때만 동작하는데, 구분자 없이
         그냥 단어가 붙어서 생략/추가되는 경우도 실제로 있었음:
           - "Urinetown The Musical" -> BWW엔 "URINETOWN"으로만 등재(우리 제목이
             BWW쪽보다 긺 - BWW 텍스트가 우리 제목의 접두어)
           - "You're Welcome America" -> BWW엔 "YOU'RE WELCOME AMERICA. A FINAL
             NIGHT WITH GEORGE W. BUSH"로 등재(반대로 BWW쪽이 더 긺 - 우리 제목이
             BWW 텍스트의 접두어)
         그래서 정규화한 문자열끼리 둘 중 하나가 다른 하나로 시작하면 후보로 인정.
         전혀 다른 짧은 제목이 우연히 접두어로 걸리는 걸 막기 위해 최소 길이(8자)와
         길이 비율(짧은 쪽이 긴 쪽의 30% 이상) 조건을 둠. 비율 기준은 실제 사례로
         보정함 - "URINETOWN"(9자) vs "URINETOWNTHEMUSICAL"(20자)는 비율 0.45,
         "YOUREWELCOMEAMERICA"(19자) vs BWW의 긴 표기(46자)는 비율 0.41이라 0.5
         기준을 쓰면 이 두 실제 사례가 모두 걸러져버려서 0.3으로 낮춤. 이 단계는
         오매칭 가능성이 가장 높아서 title_ambiguous/match_type을 통해 신뢰도를
         낮춰 표시함.

    반환값: (slug, is_ambiguous, match_type) 튜플.
      - is_ambiguous: 동일 제목으로 매치가 여러 개 있으면 True (=완전히 같은 이름으로
        재상연된 리바이벌 방어 로직 - BroadwayWorld가 "Cabaret at the Kit Kat Club"처럼
        리바이벌을 아예 개명해서 따로 등재하는 경우가 많지만, 개명 없는 경우도 있음).
        첫 번째 매치를 쓰되 이 플래그로 표시해서 target 빌더가 신뢰도를 낮춰 잡게 함.
      - match_type: "exact" / "colon_stripped" / "comma_stripped" / "prefix_fallback".
        exact가 아닐수록 완전히 다른 쇼를 잘못 골랐을 여지가 커짐(특히
        prefix_fallback은 반드시 사람이 한 번 눈으로 검토하는 걸 권장)."""
    first_char = title.strip()[0].upper() if title.strip() else ""
    letter = first_char if first_char.isalpha() else "1"  # 숫자/기호로 시작하면 '#' 페이지(letter=1)
    # *** 2026-08-24 수정: 기호로 시작하는 제목의 실제 분류 규칙 ***
    # letter=1("#") 페이지를 실제로 열어보니 '110 IN THE SHADE', '13', '1776',
    # '1984', '33 VARIATIONS', '42ND STREET'(x2), '45 SECONDS FROM BROADWAY',
    # '700 SUNDAYS', '9 TO 5' 딱 10개뿐이었고, 전부 진짜 숫자로 시작하는 제목이었음.
    # '"Master Harold"...and the Boys'나 '& Juliet'처럼 따옴표/앰퍼샌드로 시작하는
    # 제목은 여기 전혀 없었음 - BWW는 이런 제목을 앞의 기호를 무시하고 그 다음
    # 나오는 진짜 알파벳으로 분류함('"Master Harold"...' -> M, '& Juliet' -> J).
    # 그래서 "알파벳이 아니면 무조건 letter=1"이 아니라, 앞에서부터 기호를 건너뛰고
    # 처음 나오는 알파벳/숫자를 찾아서 그걸로 라우팅함. 끝까지 기호만 있거나 그
    # 다음이 숫자면(예: 진짜 '3 from Brooklyn'처럼 숫자로 시작) letter=1로 감.
    if not first_char.isalpha():
        stripped_lead = re.sub(r"^[^A-Za-z0-9]+", "", title.strip())
        next_char = stripped_lead[0].upper() if stripped_lead else ""
        letter = next_char if next_char.isalpha() else "1"

    if letter not in letter_cache:
        result = fetch_letter_index(session, letter)
        if result is not None:
            letter_cache[letter] = result
        # None이면 캐싱 안 함 - 다음 쇼가 같은 글자를 다시 시도하게 둠

    soup = letter_cache.get(letter)
    if soup is None:
        return None, False, ""

    def norm(s):
        # *** 2026-08-24 수정: 악센트 문자(É, È 등) 처리 버그 ***
        # 예전엔 정규식 [^A-Z0-9]로 영숫자만 남겼는데, 이러면 악센트 붙은 문자가
        # "다른 문자로 치환"이 아니라 "통째로 삭제"돼버림. 예를 들어 "MISÉRABLES"
        # -> "MISRABLES"(E가 통째로 사라짐)가 되는데, 우리 쪽 제목("Les
        # Miserables", 악센트 없는 영어 표기)은 "LESMISERABLES"로 정규화되어 서로
        # 안 맞았음. 실제로 BWW 카탈로그 전체를 훑어서 확인한 사례:
        # 'Les Miserables' vs 'LES MISÉRABLES', 'Amelie' vs 'AMÉLIE',
        # 'La Boheme' vs 'LA BOHÈME', 'Therese Raquin' vs 'THÉRÈSE RAQUIN' -
        # 전부 이 버그로 매칭이 깨졌었음. unicodedata.normalize("NFKD", ...)로
        # 악센트 문자를 "기본 문자 + 결합 분음 부호"로 분해한 뒤, 분음 부호만
        # (유니코드 카테고리 Mn) 제거하면 É -> E처럼 원하는 치환이 됨.
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^A-Z0-9]", "", s.upper())

    links = soup.select("a[href*='/grosses/']")

    def find_all(target_norm):
        return [a["href"].split("/grosses/")[-1] for a in links if norm(a.get_text(strip=True)) == target_norm]

    matches = find_all(norm(title))
    match_type = "exact"

    if not matches and ":" in title:
        stripped = title.split(":")[0].strip()
        if stripped:
            matches = find_all(norm(stripped))
            match_type = "colon_stripped"

    if not matches and "," in title:
        stripped = title.split(",")[0].strip()
        if stripped:
            matches = find_all(norm(stripped))
            match_type = "comma_stripped"

    if not matches and "&" in title:
        # *** 2026-08-24 추가: '&' vs 'AND' 표기 차이 ***
        # 'Bonnie & Clyde'류 제목이 정상적인 글자(B)로 라우팅됐는데도 매칭이 안 되는
        # 사례가 있었음. norm()이 '&'를 통째로 제거하니 "Bonnie & Clyde"는
        # "BONNIECLYDE"가 되는데, BWW가 이 제목을 실제로 "Bonnie And Clyde"라고
        # 풀어 쓰면 "BONNIEANDCLYDE"가 되어 안 맞음. '&'를 'AND'로 바꿔서 한 번 더
        # 시도함.
        and_variant = title.replace("&", "AND")
        matches = find_all(norm(and_variant))
        match_type = "ampersand_as_and"

    if not matches:
        title_norm = norm(title)
        if len(title_norm) >= 8:
            MIN_PREFIX_LEN = 8
            MIN_PREFIX_RATIO = 0.3
            candidates = []
            for a in links:
                link_norm = norm(a.get_text(strip=True))
                if not link_norm:
                    continue
                shorter, longer = (link_norm, title_norm) if len(link_norm) <= len(title_norm) \
                    else (title_norm, link_norm)
                if len(shorter) < MIN_PREFIX_LEN:
                    continue
                if longer.startswith(shorter) and len(shorter) / len(longer) >= MIN_PREFIX_RATIO:
                    candidates.append(a["href"].split("/grosses/")[-1])
            if candidates:
                matches = candidates
                match_type = "prefix_fallback"

    if not matches and not first_char.isalpha() and session is not None:
        # *** 2026-08-24 추가: '&'/따옴표로 시작하는 제목은 letter 인덱스에 아예
        # 없을 수 있음 ***
        # 실제로 grossesbyshow.php?letter=j 페이지를 통째로 열어서 확인해보니,
        # "JUDGMENT AT NUREMBERG" 바로 다음이 "JULIUS CAESAR"로 이어지고
        # "& JULIET"은 아예 없었음. 즉 BWW의 A-Z 브라우징 인덱스 자체가 '&'로
        # 시작하는 제목을 (문자를 무시하고 재분류하는 게 아니라) 통째로 안 실어줌 -
        # "Compare shows" 검색창의 자동완성 목록엔 있었지만, 그건 이 브라우징
        # 인덱스와는 다른 별도의 검색 시스템이었던 것으로 보임. 반면 실제 개별
        # 페이지(https://www.broadwayworld.com/grosses/JULIET)는 존재함이 직접
        # 확인됨. 그래서 letter 인덱스로는 원천적으로 못 찾는 이런 케이스를 위해,
        # 앞의 기호를 지운 제목으로 slug를 직접 만들어서 /grosses/<slug> 페이지에
        # 바로 접속해보는 최후 수단을 추가함. fetch_bww_reviews.py의 슬러그 추측
        # 폴백과 같은 발상.
        stripped_for_probe = re.sub(r"^[^A-Za-z0-9]+", "", title.strip())
        guess_slug = re.sub(r"[^A-Za-z0-9]+", "-", stripped_for_probe).strip("-").upper()
        if guess_slug:
            try:
                probe_resp = session.get(f"{BASE}/grosses/{guess_slug}", headers=HEADERS, timeout=20)
            except requests.RequestException:
                probe_resp = None
            if probe_resp is not None and probe_resp.status_code == 200:
                probe_soup = BeautifulSoup(probe_resp.text, "html.parser")
                probe_title_tag = probe_soup.find("title")
                probe_title = probe_title_tag.get_text(strip=True) if probe_title_tag else ""
                # 엉뚱한 페이지(홈으로 리다이렉트 등)로 빠진 게 아닌지 최소 확인:
                # <title>이 실제로 "Broadway Grosses" 문구를 포함하는지만 체크
                # (완전 일치까진 요구 안 함 - 페이지 제목 표기가 조금씩 다를 수 있어서)
                if "Broadway Grosses" in probe_title:
                    matches = [guess_slug]
                    match_type = "direct_url_probe"

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
    for a in soup.select("a[href*='/grosses/']"):
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
    ap.add_argument("--sleep", type=float, default=2.0,
                     help="요청 사이 대기 시간(초). 실제로 짧은 sleep(1.0)으로 296개 "
                          "연달아 돌렸더니 BroadwayWorld가 빈/차단 응답을 대거 준 정황이 "
                          "있어서 기본값을 늘림")
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
                "title_match_type": match_type,  # direct/exact/colon_stripped/comma_stripped/
                                                   # prefix_fallback - 뒤로 갈수록 근사 매칭이라
                                                   # 덜 확실함(특히 prefix_fallback은 검토 권장)
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
                  + (" [부제 생략 매칭]" if match_type in ("colon_stripped", "comma_stripped") else "")
                  + (" [& -> AND 표기 차이 매칭]" if match_type == "ampersand_as_and" else "")
                  + (" [접두어 근사 매칭 - 검토 권장]" if match_type == "prefix_fallback" else "")
                  + (" [직접 URL 확인 매칭]" if match_type == "direct_url_probe" else ""))
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
