"""
debug_creative_team.py
=============================
fetch_broadwayworld_full.py의 producer 파싱이 왜 항상 빈 값인지 원인을 찾기
위한 진단 스크립트. creative.php 페이지를 열어서:
  1) PERSON_RE에 매칭되는 <a> 태그들을 전부 나열하고
  2) 각 태그에 대해 실제 프로덕션 코드가 쓰는 _text_after()가 뭘 반환하는지
  3) 그 사람 이름 근처(부모 요소 포함) 원본 HTML이 어떻게 생겼는지
를 그대로 출력함. Wicked(showid=11291)로 실제 페이지를 열어보니 "John Frost -
Producer", "David Stone - Producer" 등 Producer 크레딧이 분명히 있는데
broadway.csv에는 producer 컬럼이 전부 비어있어서, 실제 HTML 구조와
_text_after()의 가정이 어디서 어긋나는지 이 스크립트로 확인하려는 거예요.

Usage:
  python debug_creative_team.py --showid 11291
  python debug_creative_team.py --showid 11291 --raw-html   # 원본 HTML도 같이 출력
"""
import argparse
import re

import requests
from bs4 import BeautifulSoup

BASE = "https://www.broadwayworld.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
PERSON_RE = re.compile(r"^/people/(?!character/)[^/]+/?$")


def _text_after(tag):
    """fetch_broadwayworld_full.py와 동일한 로직 (비교용으로 그대로 복사)."""
    sib = tag.next_sibling
    while sib is not None and not str(sib).strip():
        sib = sib.next_sibling
    if sib is None:
        return ""
    if isinstance(sib, str):
        return sib.strip()
    return sib.get_text(" ", strip=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--showid", required=True)
    ap.add_argument("--raw-html", action="store_true",
                     help="이름 태그 주변의 원본 HTML도 같이 출력 (구조 파악용)")
    args = ap.parse_args()

    resp = requests.get(f"{BASE}/shows/creative.php", params={"showid": args.showid},
                         headers=HEADERS, timeout=30)
    print(f"status_code={resp.status_code}, 응답 길이={len(resp.text)}자\n")

    soup = BeautifulSoup(resp.text, "html.parser")
    tags = soup.find_all("a", href=PERSON_RE)
    print(f"PERSON_RE에 매칭되는 <a> 태그 수: {len(tags)}\n")

    for i, tag in enumerate(tags, 1):
        name = tag.get_text(strip=True)
        if not name:
            continue
        role_via_text_after = _text_after(tag)
        is_producer_hit = "producer" in role_via_text_after.lower()
        print(f"[{i}] name={name!r}")
        print(f"    _text_after() 결과 = {role_via_text_after[:80]!r}")
        print(f"    'producer' 매칭 여부 = {is_producer_hit}")
        if args.raw_html:
            # 이름 태그 기준 부모 요소의 HTML을 그대로 보여줌 - 실제 구조가
            # _text_after()의 가정(다음 형제 노드에 역할이 있음)과 맞는지 확인용
            parent_html = str(tag.parent)[:500]
            print(f"    부모 요소 HTML 앞부분: {parent_html}")
        print()


if __name__ == "__main__":
    main()
