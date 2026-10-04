"""Helpers for Box Office Mojo titles: strip re-release suffixes, flag non-film items."""
from __future__ import annotations

import re

# "Star Trek II: The Wrath of Khan2022 Re-release", "Coraline15th Anniversary",
# "PonyoStudio Ghibli Fest 2024", "The Big Lebowski2023 Re-release (25th Anniversary)"
RERELEASE_SUFFIX = re.compile(
    r"\s*(?:(?:19|20)\d{2}\s*(?:3D\s*)?Re-?release.*|(?:19|20)\d{2}\s*3D Release.*|"
    r"\d+(?:½)?(?:st|nd|rd|th)\s+Anniversary.*|Studio Ghibli Fest\s*\d{4}.*)$", re.I)


def clean_title(title: str) -> tuple[str, bool]:
    """Return (title without re-release suffix, suffix_found)."""
    t = str(title or "").strip()
    m = RERELEASE_SUFFIX.search(t)
    if m and m.start() > 0:
        return t[: m.start()].strip(" -–:"), True
    return t, False


NON_FILM_RULES = [
    ("sports", re.compile(r"\bUFC\b|\bWWE\b|\bAEW\b|\bboxing\b|\bfight night\b|\bNFL\b|\bNBA\b|World Cup", re.I)),
    ("tv_episodes", re.compile(r"\bS\d+\s*:?\s*Episodes?\b|\bSeason\s*\d+\b|\bEpisodes?\s*\d+(\s*[-&]\s*\d+)?\b|"
                               r"Season \d+ Finale|\bThe Chosen\b.*(Finale|Episode)", re.I)),
    ("stage_broadcast", re.compile(r"Met(ropolitan)? Opera|National Theatre|\bNT Live\b|Bolshoi|Royal Ballet|"
                                   r"Royal Opera|\bOpera\b|\bBallet\b|Libretto|Sight & Sound Presents|"
                                   r"Broadway HD|Live from (the )?(Met|Lincoln|London)", re.I)),
    ("shorts_program", re.compile(r"Oscar Nominated Short|Short Films\b|Animation Show of Shows", re.I)),
    ("concert_live", re.compile(r"Live Viewing|Live Broadcast|\bin Cinemas\b|\bWorld Tour\b|\bTour\b.*\b(the Movie|Live)\b|"
                                r"\bConcert\b|Music Special|\bat the Symphony\b|\bon Stage\b|"
                                r"[:\-–]\s*Live (at|in|from|on)\b|\bLive at\b|- Live\b|Musical Live|"
                                r"Eras Tour|Renaissance: A Film", re.I)),
    ("concert_live", re.compile(r"\bLIVE\b|LiVE")),  # all-caps LIVE in event titles (case-sensitive)
]
NON_FILM_DISTRIBUTORS = {"Trafalgar Releasing": "concert_live", "CinemaLive": "stage_broadcast",
                         "Piece of Magic Entertainment": "concert_live"}


def non_film_type(title: str, distributor: str = "") -> str:
    title = clean_title(title)[0]
    for name, rx in NON_FILM_RULES:
        if rx.search(str(title)):
            return name
    return NON_FILM_DISTRIBUTORS.get(str(distributor or "").strip(), "")
