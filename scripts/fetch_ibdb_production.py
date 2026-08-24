"""
fetch_ibdb_production.py
=============================
IBDB(ibdb.com)의 "Produced by ..." 한 줄을 그대로 가져와서
data/broadway.csv의 (비어있던) producer 컬럼을 production 컬럼으로 대체.

*** 배경: 왜 BroadwayWorld creative.php 대신 IBDB인가 ***
BroadwayWorld의 creative.php 페이지에도 실제로 Producer 크레딧이 있긴
함(예: Wicked 페이지에서 John Frost, David Stone 등이 "Producer"로 표시됨)
- 다만 이 스크립트를 만들 당시 진단 결과를 아직 못 받아서 그쪽 파싱 버그를
못 고침. 그래서 우선 IBDB 쪽으로 감. IBDB 프로덕션 페이지(예:
https://www.ibdb.com/broadway-production/wicked-13485)는 "ABOUT THIS
PRODUCTION" 섹션 맨 위에

  Produced by Marc Platt, Universal Pictures, The Araca Group,
  Jon B. Platt and David Stone

처럼 프로듀서/제작사를 한 줄로 모아놓음. 이 스크립트는 이 한 줄을 그대로
가져옴 - 개인/회사를 나누지 않고 원문 그대로 저장함.

Playbill에는 이 정보 자체가 없음 (흥행 데이터만 수집하는 소스라서).

*** 쇼 제목 -> IBDB 프로덕션 URL/ID: 검색 API 대신 사이트맵 사용 ***
IBDB의 자체 검색창은 자바스크립트 기반이라(Network 탭에서 실제로 확인해봤는데
검색 API 요청 자체가 안 잡힘 - 페이지 로드시 콘텐츠가 이미 서버에서 렌더링된
상태로 오는 걸로 보임) 검색 API를 역공학하는 대신, IBDB가 공개해둔 사이트맵
파일을 통째로 받아서 제목으로 매칭하는 방식으로 바꿈:

  https://www.ibdb.com/productions-sitemap.xml

이 파일 안에 'https://www.ibdb.com/broadway-production/<slug>-<id>' 형태로
IBDB에 있는 프로덕션 전체(1920년대 것까지)가 나열돼 있음(실제로 열어봐서
확인함). 매 쇼마다 검색 요청을 보낼 필요 없이, 이 파일 하나를 딱 한 번
받아서 slug -> id 매핑 테이블을 로컬에 만든 뒤 조회만 하면 됨 - 훨씬
빠르고 안정적임.

*** 한계 ***
  - slug 생성 규칙이 IBDB와 100% 똑같지 않을 수 있음(예: '& Juliet'처럼
    특수문자로 시작하는 제목은 슬러그가 앞에 하이픈이 붙는 식('-juliet')이라
    지금 slugify()가 못 잡음 - 이런 특이 케이스는 나중에 손볼 것).
  - 같은 slug에 id가 여러 개 매칭되면(동명 리바이벌) 가장 큰 id(최근 프로덕션으로
    추정)를 골라서 씀(2026-08-24 수정, 아래 "모호한 매칭 처리" 참고). 완전히
    확신할 순 없어서 production_match_type 컬럼에 "ambiguous_latest_pid"로
    표시해 둠 - broadway.csv의 title_ambiguous 컬럼과 비슷한 개념.

*** 모호한 매칭(동일 slug, id 여러 개) 처리 (2026-08-24 수정) ***
처음엔 후보가 2개 이상이면 아예 스킵했는데, 실제로 돌려보니 830개 성공 대비
373개(약 31%)가 이 이유로 통째로 날아가는 문제가 있었음 - Hamlet(67개),
Macbeth(49개), The Merchant of Venice(50개)처럼 셰익스피어/고전극이나 여러 번
재상연된 유명작일수록 후보가 많아서 오히려 더 잘 스킵되는 역설적인 상황이었음.

원인을 보면: IBDB 사이트맵엔 1920년대 프로덕션까지 전부 있는데, broadway.csv는
"1985 ~ 현재" 데이터만 다룸(broadway_pipeline.yml 상단 주석 참고). 즉 후보
대부분은 broadway.csv에 있는 공연과 무관한 훨씬 오래된 프로덕션이고, 우리가
실제로 매칭하고 싶은 건 보통 그중 가장 최근 것 하나뿐임.

IBDB의 프로덕션 id는 페이지가 만들어진 순서라서 완벽하게 개막일 순서와
일치한다는 보장은 없지만, 대체로 시간이 지날수록 커지는 경향이 있음(실제로
후보 목록을 보면 id가 작은 쪽에 1000~5000대, 큰 쪽에 40만~50만대가 몰려있어서
이 경향이 뚜렷이 보임). 그래서 완전 매칭이 아니면 버리는 대신, 후보 중 id가
가장 큰 것(=가장 최근으로 추정)을 골라 쓰고 production_match_type 컬럼에
"ambiguous_latest_pid"라고 표시해서 신뢰도가 완전 매칭보다 낮다는 걸 남김.
로그에는 계속 전체 후보 목록을 다 찍어서, 고른 게 실제로 원하는 프로덕션이
맞는지 나중에 검토할 수 있게 함.

Usage:
  python fetch_ibdb_production.py --raw data/broadway.csv --out-dir data --sleep 1.0
Usage (테스트용, 사이트맵 없이 ID를 직접 아는 경우):
  python fetch_ibdb_production.py --shows "Wicked:13485" "Hamilton:499521" --out-dir .
"""
import argparse
import os
import re
import time
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.ibdb.com"
SITEMAP_URL = f"{BASE}/productions-sitemap.xml"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

# "Produced by ... " 뒤에서 다음 문단(Book by / Music by / 세계 초연 문구 등) 전까지만
# 뽑기 위한 경계. IBDB 페이지에서 실제로 확인된 패턴들.
PRODUCED_BY_RE = re.compile(r"Produced by\s+(.+?)(?=\s*(?:Book by|Music by|The world premiere|$))",
                             re.IGNORECASE | re.DOTALL)

LOC_RE = re.compile(r"/broadway-production/(.+)-(\d+)$")


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def slugify(title):
    """IBDB가 쓰는 것으로 보이는 슬러그 규칙을 최대한 흉내냄: 영숫자/공백/하이픈만
    남기고 소문자로, 공백은 하이픈으로. '& Juliet' 같은 특수문자 시작 제목은
    안 맞을 수 있음(위 docstring '한계' 참고)."""
    s = re.sub(r"[^\w\s-]", "", title).strip().lower()
    return re.sub(r"\s+", "-", s)


def build_sitemap_index(session):
    """productions-sitemap.xml을 통째로 받아서 slug -> [(slug, id), ...] 매핑을
    만듦. 매 쇼마다 네트워크 요청을 보내는 대신 이 파일 하나만 받으면 됨."""
    resp = session.get(SITEMAP_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    index = {}
    for url_tag in root.findall("sm:url", SITEMAP_NS):
        loc = url_tag.find("sm:loc", SITEMAP_NS)
        if loc is None or not loc.text:
            continue
        m = LOC_RE.search(loc.text.strip())
        if not m:
            continue
        slug, pid = m.group(1), m.group(2)
        index.setdefault(slug, []).append((slug, pid))
    return index


def resolve_ibdb_id(title, sitemap_index):
    """제목 -> (slug, id, match_type).

    매칭 안 되면 (None, None, "not_found").
    정확히 하나면 (slug, id, "ok").
    여러 개면(동명 리바이벌 등) id가 가장 큰(=가장 최근으로 추정) 후보를 골라서
    (slug, id, "ambiguous_latest_pid")로 반환함 - 왜 완전히 버리지 않고 이렇게
    고르는지는 파일 상단 "모호한 매칭 처리" 주석 참고."""
    key = slugify(title)
    candidates = sitemap_index.get(key)
    if not candidates:
        return None, None, "not_found"
    if len(candidates) == 1:
        slug, pid = candidates[0]
        return slug, pid, "ok"
    best_slug, best_pid = max(candidates, key=lambda sp: int(sp[1]))
    return best_slug, best_pid, "ambiguous_latest_pid"


def parse_production_page(html):
    """IBDB 프로덕션 페이지 HTML에서 'Produced by ...' 한 줄을 원문 그대로
    (개인/회사 구분 없이) 추출.

    *** 태그 구조가 아니라 텍스트 기반으로 파싱하는 이유 ***
    처음엔 'Produced by'로 시작하는 <p>/<div> 태그를 찾아서 그 안의 <a> 태그만
    뽑는 방식으로 짰는데, 실제 페이지 구조를 몰라서(제가 raw HTML에 직접
    접근을 못 함, 마크다운으로 변환된 내용만 봤음) 부모 태그가 다음 문단까지
    통째로 포함해버리는 버그가 있었음. 그래서 태그 경계에 의존하지 않고,
    페이지 전체 텍스트에서 'Produced by ~ Book by/Music by/The world premiere'
    사이 구간만 정규식으로 잘라내는 방식으로 바꿈 - 실제 HTML 태그 이름이
    무엇이든 상관없이 안정적으로 동작함.

    괄호 안 부가 설명(예: 'The Public Theater (Oskar Eustis, Artistic
    Director; Patrick Willingham, Executive Director)')은 원문 느낌을 살리되
    너무 길어지는 걸 막기 위해 제거함 -> 'The Public Theater'만 남음."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    m = PRODUCED_BY_RE.search(text)
    if not m:
        return ""

    raw = m.group(1)
    raw = re.sub(r"\([^)]*\)", "", raw)  # 괄호 안 부가 설명 제거
    raw = re.sub(r"\s+,", ",", raw)       # <a> 태그 경계에서 생기는 "이름 , 이름" 같은 공백 정리
    raw = re.sub(r"\s+", " ", raw).strip().rstrip(",")
    return raw


def fetch_production_page(slug, pid, session, timeout=30):
    url = f"{BASE}/broadway-production/{slug}-{pid}"
    resp = session.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="broadway.csv 경로 - 'show' 컬럼에서 고유 쇼 이름을 뽑음")
    ap.add_argument("--shows", nargs="+", default=None,
                     help="테스트용: 'Title:ibdb_id' 형태로 나열 (예: 'Wicked:13485' 'Hamilton:499521')")
    ap.add_argument("--existing", default=None,
                     help="기존 ibdb_production.csv 경로. 있으면 이미 있는 title은 건너뜀")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    session = make_session()

    if args.shows:
        pairs = []
        for s in args.shows:
            title, _, pid = s.rpartition(":")
            pairs.append((title, slugify(title), pid, "ok"))
    elif args.raw:
        df = pd.read_csv(args.raw, sep=None, engine="python", encoding="utf-8-sig")
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        titles = df["show"].dropna().drop_duplicates().tolist()

        print("productions-sitemap.xml 받는 중 (한 번만)...")
        sitemap_index = build_sitemap_index(session)
        print(f"사이트맵에서 {len(sitemap_index)}개 고유 slug 확인")

        pairs = []
        n_not_found = 0
        n_ambiguous = 0
        for t in titles:
            slug, pid, status = resolve_ibdb_id(t, sitemap_index)
            if status == "not_found":
                n_not_found += 1
                continue
            if status == "ambiguous_latest_pid":
                n_ambiguous += 1
                all_candidates = sitemap_index[slugify(t)]
                print(f"  [모호함, 최근 id로 근사 매칭] '{t}' -> 선택={slug}-{pid}, "
                      f"후보 {len(all_candidates)}개: "
                      f"{[f'{s}-{p}' for s, p in all_candidates]}")
            pairs.append((t, slug, pid, status))
        print(f"매칭 결과: 완전 매칭 {len(pairs) - n_ambiguous}개 / "
              f"근사 매칭(동명 리바이벌 등 -> 최근 id 선택) {n_ambiguous}개 / "
              f"못 찾음 {n_not_found}개")
    else:
        ap.error("--raw 또는 --shows 중 하나는 필요해요")

    existing_df = None
    if args.existing and os.path.isfile(args.existing):
        existing_df = pd.read_csv(args.existing, sep=None, engine="python", encoding="utf-8-sig")
        already_done = set(existing_df["title"])
        before = len(pairs)
        pairs = [(t, s, p, mt) for t, s, p, mt in pairs if t not in already_done]
        print(f"기존 {len(already_done)}개 쇼는 건너뜀 ({before} -> {len(pairs)}개 신규 처리 대상)")

    if args.limit:
        pairs = pairs[: args.limit]

    print(f"총 {len(pairs)}개 쇼 처리 예정")

    rows = []
    n_errors = 0
    for i, (title, slug, pid, match_type) in enumerate(pairs, 1):
        try:
            html = fetch_production_page(slug, pid, session)
            production = parse_production_page(html)
            rows.append({"title": title, "production": production,
                         "production_match_type": match_type})  # ok/ambiguous_latest_pid -
                                                                   # 후자는 동명 리바이벌 중 id가
                                                                   # 가장 큰 것을 고른 근사 매칭
            print(f"[{i}/{len(pairs)}] '{title}' -> {production}"
                  + (" [동명 리바이벌 중 최근 id 근사 매칭]" if match_type == "ambiguous_latest_pid" else ""))
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(pairs)}] '{title}' -> 오류 발생, 스킵: {e}")
            rows.append({"title": title, "production": "", "production_match_type": match_type})
        time.sleep(args.sleep)

    print(f"\n처리 중 오류 {n_errors}건 (스킵하고 계속 진행함)")

    if not rows:
        print("수집된 데이터가 없어요.")
        return

    new_df = pd.DataFrame(rows)
    out_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(
        subset=["title"], keep="last"
    ) if existing_df is not None else new_df

    out_path = os.path.join(args.out_dir, "ibdb_production.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"저장 완료: 신규 {len(new_df)}개 + 기존 -> 총 {len(out_df)}행 -> {out_path}")


if __name__ == "__main__":
    main()
