"""
lib_playbill.py

Playbill production 페이지 스크래핑에서 공통으로 쓰는 함수들.
- URL 슬러그 후보 생성 (show/theatre 이름 -> playbill.com/production/... 슬러그)
- HTML fetch (재시도 포함)
- SYNOPSIS / Running Time 추출
- 페이지 제목과 쇼 이름이 실제로 일치하는지 확인 (오탐 방지)
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

STOP_LABELS = [
    "SCHEDULE",
    "MUSIC",
    "LYRICS",
    "BOOK",
    "DIRECTOR",
    "CHOREOGRAPHER",
    "ORCHESTRATIONS",
    "MUSICAL SUPERVISION",
    "SCENIC DESIGN",
    "COSTUME DESIGN",
    "LIGHTING DESIGN",
    "SOUND DESIGN",
    "CASTING",
    "RUNNING TIME",
    "PRODUCER",
    "GENERAL MANAGER",
    "PRO INFO",
]
STOP_LABELS_PATTERN = "|".join(re.escape(label) for label in STOP_LABELS)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: int = 20, retries: int = 3, backoff: float = 2.0) -> Optional[str]:
    """페이지 HTML을 가져온다. 404 등 클라이언트 에러면 즉시 None 반환, 그 외 에러는 재시도."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    if last_exc:
        raise RuntimeError(f"Failed to fetch {url}: {last_exc}")
    return None


# ---------------------------------------------------------------------------
# Slug 생성
# ---------------------------------------------------------------------------

def slugify(text: str, ampersand: str = "drop") -> str:
    """
    문자열을 Playbill 스타일 URL 슬러그로 변환한다.
    ampersand: '&' 문자를 'drop'(삭제) 하거나 'and'로 바꿀지 선택.
    (Playbill 실제 슬러그 규칙은 케이스마다 달라서 100% 재현은 불가능 -
     여러 후보 중 하나로 사용하고, 최종적으로는 fetch 후 제목 매칭으로 검증한다.)
    """
    s = text.strip().lower()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"['’]", "", s)
    if ampersand == "drop":
        s = s.replace("&", "")
    else:
        s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\-]+", " ", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def extract_year(date_str: str) -> Optional[str]:
    """
    'DD-Mon-YY' (예: '10-Oct-21'), 'DD-Mon-YYYY', 'YYYY-MM-DD' 등 흔한 날짜 표기에서
    4자리 연도를 뽑아낸다. 2자리 연도는 21세기(20xx)로 가정한다 (이 데이터셋이
    2021년 이후 브로드웨이 시즌을 다루고 있어서).
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    # 이미 4자리 연도가 있는 경우
    m = re.search(r"(19|20)\d{2}", s)
    if m:
        return m.group(0)
    # 'DD-Mon-YY' 형태의 2자리 연도
    m = re.search(r"-(\d{2})$", s)
    if m:
        yy = int(m.group(1))
        return f"20{yy:02d}" if yy <= 79 else f"19{yy:02d}"
    return None


def build_candidate_urls(show: str, theatre: str, opening_date: str = "", closing_date: str = "") -> list[str]:
    """
    show/theatre 이름과 (있으면) 날짜로부터 가능성 있는 Playbill production URL 후보들을 생성한다.
    가장 그럴듯한 후보부터 순서대로 반환하며, 호출하는 쪽에서 fetch + 제목 매칭으로
    실제로 맞는지 검증해야 한다.
    """
    years = []
    for d in (opening_date, closing_date):
        year = extract_year(d)
        if year and year not in years:
            years.append(year)
    if not years:
        years = [""]

    theatre_slug = slugify(theatre)

    candidates = []
    for amp_mode in ("drop", "and"):
        show_slug = slugify(show, ampersand=amp_mode)
        for year in years:
            for suffix in ("", "-2"):
                parts = [show_slug, "broadway", theatre_slug]
                if year:
                    parts.append(year)
                url = "https://playbill.com/production/" + "-".join(p for p in parts if p) + suffix
                if url not in candidates:
                    candidates.append(url)
    return candidates


# ---------------------------------------------------------------------------
# 제목 매칭 (오탐 방지)
# ---------------------------------------------------------------------------

def _normalize_for_match(s: str) -> str:
    s = s.lower()
    s = s.replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def title_matches(page_title: str, show_name: str, threshold: float = 0.6) -> bool:
    """페이지 h1과 우리가 찾는 쇼 이름이 충분히 비슷한지 확인."""
    if not page_title:
        return False
    a = _normalize_for_match(page_title)
    b = _normalize_for_match(show_name)
    if not a or not b:
        return False
    if b in a or a in b:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= threshold


def extract_h1(soup: BeautifulSoup) -> Optional[str]:
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            return text
    if soup.title:
        return soup.title.get_text(strip=True)
    return None


# ---------------------------------------------------------------------------
# SYNOPSIS 추출
# ---------------------------------------------------------------------------

def extract_synopsis(soup: BeautifulSoup) -> Optional[str]:
    label_tag = None
    for tag in soup.find_all(["strong", "b", "h2", "h3", "h4"]):
        text = tag.get_text(strip=True).upper().rstrip(":").strip()
        if text == "SYNOPSIS":
            label_tag = tag
            break

    if label_tag is not None:
        pieces = []
        for el in label_tag.find_all_next():
            if not hasattr(el, "get_text"):
                continue
            el_text = el.get_text(strip=True)
            if not el_text:
                continue
            if el.name in ("strong", "b", "h2", "h3", "h4"):
                upper = el_text.upper().rstrip(":").strip()
                if upper in STOP_LABELS:
                    break
                if upper == "SYNOPSIS":
                    continue
            if el.name == "p":
                pieces.append(el.get_text(" ", strip=True))
        if pieces:
            return "\n\n".join(pieces).strip()

    full_text = soup.get_text("\n")
    pattern = re.compile(
        r"SYNOPSIS\s*:?\s*\n+(.*?)(?=\n\s*(?:" + STOP_LABELS_PATTERN + r")\s*:)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(full_text)
    if match:
        cleaned = re.sub(r"\n{2,}", "\n\n", match.group(1).strip())
        if cleaned:
            return cleaned
    return None


# ---------------------------------------------------------------------------
# Running Time 추출
# ---------------------------------------------------------------------------

RUNNING_TIME_RE = re.compile(r"Running Time\s*:\s*(.+)", re.IGNORECASE)


def extract_running_time(soup: BeautifulSoup) -> Optional[str]:
    """
    'Running Time: 2 hours and 30 minutes, including one intermission' 같은
    문구를 <li>/<p>/텍스트에서 찾아온다.
    """
    # 1) li/p 태그에서 직접 찾기 (가장 흔한 패턴)
    for tag in soup.find_all(["li", "p", "span", "div"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 300:
            continue
        m = RUNNING_TIME_RE.match(text.strip())
        if m:
            return m.group(1).strip()

    # 2) fallback: 전체 텍스트에서 정규식
    full_text = soup.get_text("\n")
    m = re.search(r"Running Time\s*:\s*([^\n]+)", full_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None
