"""
clean_broadway_columns.py
=============================
data/broadway.csv의 데드 컬럼 정리 + 시상식별 컬럼(동적 스키마)을
"토니상"과 "그 외 시상식 합산" 두 그룹으로 축약.

*** 왜 토니상만 따로 두는가 ***
토니상은 전국 TV 중계, 일반 대중 인지도, 박스오피스 직결 효과가 압도적으로 큰
반면 Drama Desk/Outer Critics Circle/Drama League/Theatre World/The Hewes 등은
업계·평단 내부 상 성격이 강함. 전부 합쳐버리면 "토니상 1개 수상"과 "덜 알려진
상 1개 수상"이 회귀분석에서 동일한 가중치를 갖게 되어 award prestige 통제라는
원래 목적이 사라짐. 그래서 토니상은 독립 변수로 유지하고, 나머지는
"그 외 시상식(권위가 상대적으로 낮거나 업계 내부용)"으로 합산.

*** 컬럼 매칭이 하드코딩이 아니라 정규식인 이유 ***
award_bonus 컬럼(fetch_broadwayworld_full.py의 _parse_awards_section)은 시상식
이름에서 자동 생성되는 동적 스키마라(새 시상식이 생기면 새 컬럼이 자동으로 생김)
'*_nominations'/'*_wins' 패턴으로 전부 찾아서 tony 여부만 분기함. 새 시상식이
앞으로 생겨도 스크립트 수정 없이 자동으로 other_awards에 합산됨.

*** 결측 처리 ***
시상식별 컬럼의 NaN은 "그 시상식에서 데이터를 못 모았다"가 아니라 "그 시상식
후보에 오른 적이 없다"는 뜻(award_bonus는 실제로 후보/수상 기록이 있을 때만
값이 채워지는 구조)이라 합산 전에 0으로 채움.

*** 삭제하는 데드 컬럼들 (이유는 대화에서 확인한 내용) ***
  - tony_nominations / tony_wins: 예전 버전 컬럼. 지금은 award_bonus가 만드는
    tony_awards_nominations / tony_awards_wins가 정식 버전이라 중복임.
  - awards_detail: fetch_tony_awards.py의 표 파싱이 현재 깨져 있어서(요약
    승/노미 숫자는 되는데 상세 표는 못 뽑음, tony_awards.csv 자체에서도 전부
    빈 값으로 확인됨) 항상 빈 값. award_bodies_detail(creative.php에서 파싱,
    정상 동작)로 이미 대체됨.
  - genre_corgis: 현재 어떤 스크립트도 이 컬럼을 채우지 않음 - 예전 실험의
    잔재로 추정, 전부 결측.
  - genre_mismatch: 예전엔 두 소스(Playbill/BroadwayWorld) 장르를 비교해서
    불일치를 표시했는데, 지금 fetch_broadwayworld_full.py엔 이 비교 로직이
    아예 없어서(genre_map/genre_guess 둘 중 하나를 그냥 씀) 신규 쇼는 전부
    결측, 예전에 채워진 값도 전부 False(불일치 없음)라 사실상 죽은 컬럼.
  - has_pulitzer / has_drama_desk_win: 지금 파이프라인이 호출하지 않는 예전
    fetch_ibdb_awards.py(IBDB 소스, 스크립트 자체 주석에 "Awards 탭이
    자바스크립트라 불안정해서 폐기"라고 적혀 있음)가 테스트로 남긴 값.
    10개 쇼에만 값이 있음.

*** producer -> production 이름 변경 ***
원래 있던 'producer' 컬럼(BroadwayWorld creative.php에서 개인 이름을 모으려던
컬럼, 항상 결측)은 fetch_ibdb_production.py가 새로 만드는 'production'
컬럼(IBDB의 "Produced by ..." 원문 그대로 - 개인/회사 구분 없이 통째로)으로
대체됨. 이 스크립트는 그 새 컬럼이 이미 merge된 상태라고 가정하고 그대로
둠 - 혹시 구버전 'producer' 컬럼이 아직 남아있으면 삭제함.

slug / based_on 은 아직 결정이 안 끝나서 이 스크립트에서 건드리지
않음 (대화에서 별도로 설명).

Usage:
  python clean_broadway_columns.py --broadway data/broadway.csv --out data/broadway.csv
"""
import argparse
import re

import pandas as pd

DEAD_COLUMNS = [
    "tony_nominations", "tony_wins",       # 구버전 중복 (award_bonus의 tony_awards_* 로 대체)
    "awards_detail",                        # 표 파싱 버그로 항상 빈 값 (award_bodies_detail로 대체됨)
    "genre_corgis",                         # 어떤 스크립트도 채우지 않는 죽은 컬럼
    "genre_mismatch",                       # 현재 스크립트에 비교 로직 자체가 없음, 죽은 컬럼
    "has_pulitzer", "has_drama_desk_win",   # 폐기된 fetch_ibdb_awards.py 테스트 잔재, 10건뿐
    "producer",                             # 항상 결측이던 구버전 컬럼 - production/production_company로 대체
]

TONY_NOM_COL = "tony_awards_nominations"
TONY_WIN_COL = "tony_awards_wins"

AWARD_NOM_RE = re.compile(r"^(.+)_nominations$")
AWARD_WIN_RE = re.compile(r"^(.+)_wins$")


def consolidate_awards(df):
    nom_cols = [c for c in df.columns if AWARD_NOM_RE.match(c)]
    win_cols = [c for c in df.columns if AWARD_WIN_RE.match(c)]

    other_nom_cols = [c for c in nom_cols if c != TONY_NOM_COL]
    other_win_cols = [c for c in win_cols if c != TONY_WIN_COL]

    print(f"토니상 노미네이트 원본 컬럼: {TONY_NOM_COL if TONY_NOM_COL in df.columns else '(없음)'}")
    print(f"토니상 수상 원본 컬럼: {TONY_WIN_COL if TONY_WIN_COL in df.columns else '(없음)'}")
    print(f"other_awards_nominations로 합산되는 컬럼({len(other_nom_cols)}개): {other_nom_cols}")
    print(f"other_awards_wins로 합산되는 컬럼({len(other_win_cols)}개): {other_win_cols}")

    if TONY_NOM_COL in df.columns:
        df["tony_nominations"] = df[TONY_NOM_COL].fillna(0).astype(int)
    if TONY_WIN_COL in df.columns:
        df["tony_wins"] = df[TONY_WIN_COL].fillna(0).astype(int)

    if other_nom_cols:
        df["other_awards_nominations"] = df[other_nom_cols].fillna(0).sum(axis=1).astype(int)
    if other_win_cols:
        df["other_awards_wins"] = df[other_win_cols].fillna(0).sum(axis=1).astype(int)

    # 원본 시상식별 컬럼(토니 포함)은 전부 지움 - tony_nominations/tony_wins라는
    # 새 이름으로 이미 값을 복사해뒀음
    drop_cols = [c for c in (nom_cols + win_cols) if c not in ("tony_nominations", "tony_wins")]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns], errors="ignore")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default="data/broadway.csv")
    ap.add_argument("--out", default="data/broadway.csv")
    args = ap.parse_args()

    df = pd.read_csv(args.broadway, sep=None, engine="python", encoding="utf-8-sig")
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]

    before_cols = len(df.columns)

    existing_dead = [c for c in DEAD_COLUMNS if c in df.columns]
    if existing_dead:
        print(f"삭제하는 데드 컬럼: {existing_dead}")
        df = df.drop(columns=existing_dead)

    df = consolidate_awards(df)

    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {args.out} ({len(df)}행, {before_cols} -> {len(df.columns)}컬럼)")


if __name__ == "__main__":
    main()
