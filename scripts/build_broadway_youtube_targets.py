"""
build_broadway_youtube_targets.py
==================================
data/broadway.csv (주간 패널) -> data/broadway_youtube_targets.csv (프로덕션 run 단위)

*** 업데이트: run_number/lead_cast/title_ambiguous 컬럼을 우선 활용하도록 재작성함 ***
*** run_theatre/run_opening_date/run_closing_date 경로(history)는 fetch_show_history.py가
    실제로는 작동 불가능한 페이지 구조를 가정하고 있었던 게 확인되어 파이프라인에서
    제거됨 - 코드 경로는 남겨뒀지만(누군가 수동으로 더 나은 소스를 채워 넣을 경우 대비),
    실제로는 항상 show_level/week_range 둘 중 하나로 결정됨 ***

우선순위:
  1. run_number 컬럼이 있으면(calculate_revival_flags.py 실행 결과) 그걸 그대로
     run 구분 키로 사용 - opening_date만으로 추측해서 나누던 이전 방식보다 훨씬 안정적
     (week_ending 공백 기반이라 opening_date가 결측이어도 정확히 나뉨).
  2. run_theatre/run_opening_date/run_closing_date/run_performance_id 컬럼이 있으면
     (지금은 실질적으로 발생 안 함, 위 설명 참고) 그 값을 최우선으로 씀.
  3. 없으면 show-level opening_date/closing_date/theatre(--meta 병합분)로 대체.
     단, title_ambiguous=True(동일 제목의 다른 프로덕션이 BroadwayWorld에 여럿 있어서
     이 메타가 어느 쪽 건지 불확실)면 date_source를 show_level_ambiguous로 낮춰 표시함
     -> youtube_collect_broadway.py가 이 표시를 보고 날짜 필터 버퍼를 더 넉넉하게 잡음.
  4. 그것도 없으면 해당 run의 week_ending min/max로 근사.
  각 run마다 실제 쓰인 소스를 date_source 컬럼에 남겨서 나중에 신뢰도 판단할 수 있게 함.

run_number 컬럼이 아예 없는 broadway.csv(구버전)라면 예전처럼 opening_date 기준
그룹핑으로 자동 폴백함.

Usage:
    python build_broadway_youtube_targets.py \
        --broadway data/broadway.csv \
        --out data/broadway_youtube_targets.csv
"""
import argparse
import re

import pandas as pd

OUT_COLUMNS = [
    "run_id", "show", "theatre", "opening_date", "closing_date", "date_source",
    "is_revival", "revival_rank", "genre", "based_on", "lead_cast",
    "title_ambiguous", "n_weeks_observed",
]


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(text)).strip("-").lower()
    return text[:40] if text else "show"


def most_common(s):
    s = s.dropna()
    return s.mode().iloc[0] if not s.empty else pd.NA


def build_targets_with_run_number(df):
    """run_number 컬럼이 있는 broadway.csv용 (권장 경로)."""
    has_history_cols = {"run_theatre", "run_opening_date", "run_closing_date"}.issubset(df.columns)

    agg_spec = {
        "week_min": ("week_ending", "min"),
        "week_max": ("week_ending", "max"),
        "n_weeks_observed": ("week_ending", "nunique"),
    }
    for col, out_name in [("theatre", "show_theatre"), ("genre", "genre"),
                           ("based_on", "based_on"), ("lead_cast", "lead_cast")]:
        if col in df.columns:
            agg_spec[out_name] = (col, most_common)
    if "title_ambiguous" in df.columns:
        # bool 컬럼은 most_common(dropna+mode)이 아니라 any로: 그 run에 걸린 주차 중
        # 단 하나라도 모호 표시가 있었으면 전체를 모호로 취급(더 안전한 쪽으로)
        agg_spec["title_ambiguous"] = ("title_ambiguous", lambda s: bool(s.fillna(False).any()))
    if has_history_cols:
        agg_spec["run_theatre"] = ("run_theatre", most_common)
        agg_spec["run_opening_date"] = ("run_opening_date", most_common)
        agg_spec["run_closing_date"] = ("run_closing_date", most_common)
    if "run_performance_id" in df.columns:
        agg_spec["run_performance_id"] = ("run_performance_id", most_common)
    if "opening_date" in df.columns:
        agg_spec["show_opening_date"] = ("opening_date", most_common)
    if "closing_date" in df.columns:
        agg_spec["show_closing_date"] = ("closing_date", most_common)

    agg = df.groupby(["show", "run_number"], dropna=False).agg(**agg_spec).reset_index()

    # 날짜/극장 우선순위: run_* (history 매칭, 지금은 실질적으로 안 씀) > show-level (title 병합) > week_ending 근사
    def pick(row):
        if has_history_cols and pd.notna(row.get("run_opening_date")):
            return (
                row.get("run_theatre") or row.get("show_theatre"),
                row["run_opening_date"],
                row.get("run_closing_date") or row["week_max"],
                "history",
            )
        if pd.notna(row.get("show_opening_date")):
            source = "show_level_ambiguous" if row.get("title_ambiguous") else "show_level"
            return (
                row.get("show_theatre"),
                row["show_opening_date"],
                row.get("show_closing_date") or row["week_max"],
                source,
            )
        return (row.get("show_theatre"), row["week_min"], row["week_max"], "week_range")

    picked = agg.apply(pick, axis=1, result_type="expand")
    picked.columns = ["theatre", "opening_date", "closing_date", "date_source"]
    agg = pd.concat([agg, picked], axis=1)

    agg["revival_rank"] = agg["run_number"]
    n_runs_per_show = agg.groupby("show")["show"].transform("size")
    agg["is_revival"] = n_runs_per_show > 1

    # run_performance_id가 있으면 그걸 run_id로(전역 유일), 없으면 show+run_number
    if "run_performance_id" in agg.columns:
        agg["run_id"] = agg.apply(
            lambda r: r["run_performance_id"] if pd.notna(r["run_performance_id"])
            else f"{slugify(r['show'])}-run{int(r['run_number'])}",
            axis=1,
        )
    else:
        agg["run_id"] = agg.apply(
            lambda r: f"{slugify(r['show'])}-run{int(r['run_number'])}", axis=1
        )

    for col in ("opening_date", "closing_date"):
        # format="mixed" 필수: run_opening_date는 BroadwayWorld 표기("March 19, 1998")고
        # show_opening_date/week_range 쪽은 ISO 표기("2003-10-30")라 같은 컬럼 안에 두 포맷이
        # 섞여 있음. format 지정 없이 pd.to_datetime을 쓰면 첫 값 기준으로 포맷을 고정해서
        # 나머지 형식이 전부 NaT가 되는 문제가 실제로 있었음 (테스트로 발견/수정).
        agg[col] = pd.to_datetime(agg[col], errors="coerce", format="mixed").dt.strftime("%Y-%m-%d")

    for col in OUT_COLUMNS:
        if col not in agg.columns:
            agg[col] = pd.NA
    return agg[OUT_COLUMNS]


def build_targets_legacy(df):
    """run_number가 없는 구버전 broadway.csv용 폴백 (opening_date 기준 그룹핑)."""
    for col in ("opening_date", "closing_date"):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_datetime(df[col], errors="coerce", format="mixed")

    group_keys = ["show", "opening_date", "closing_date"]
    agg_spec = {
        "theatre": ("theatre", most_common),
        "week_min": ("week_ending", "min"),
        "week_max": ("week_ending", "max"),
        "n_weeks_observed": ("week_ending", "nunique"),
    }
    for col in ("genre", "based_on", "lead_cast"):
        if col in df.columns:
            agg_spec[col] = (col, most_common)
    if "title_ambiguous" in df.columns:
        agg_spec["title_ambiguous"] = ("title_ambiguous", lambda s: bool(s.fillna(False).any()))
    agg = df.groupby(group_keys, dropna=False).agg(**agg_spec).reset_index()
    # 원본에 없던 컬럼(genre/based_on/lead_cast)은 개수 같은 엉뚱한 값이 아니라
    # 명시적으로 결측으로 채움 (OUT_COLUMNS 맞추는 아래 루프에서 처리됨)

    approx_mask = agg["opening_date"].isna()
    if "title_ambiguous" in agg.columns:
        agg["date_source"] = approx_mask.map({True: "week_range", False: None})
        agg.loc[~approx_mask, "date_source"] = agg.loc[~approx_mask, "title_ambiguous"].map(
            {True: "show_level_ambiguous", False: "show_level"}
        )
    else:
        agg["date_source"] = approx_mask.map({True: "week_range", False: "show_level"})
    agg.loc[approx_mask, "opening_date"] = agg.loc[approx_mask, "week_min"]
    agg.loc[approx_mask, "closing_date"] = agg.loc[approx_mask, "week_max"]
    closing_missing = agg["closing_date"].isna()
    agg.loc[closing_missing, "closing_date"] = agg.loc[closing_missing, "week_max"]

    agg = agg.sort_values(["show", "opening_date"])
    agg["revival_rank"] = agg.groupby("show").cumcount() + 1
    n_runs_per_show = agg.groupby("show")["show"].transform("size")
    agg["is_revival"] = n_runs_per_show > 1

    seen = set()

    def make_id(show, opening):
        base = f"{slugify(show)}-{str(opening)[:10] if pd.notna(opening) else 'na'}"
        rid, i = base, 2
        while rid in seen:
            rid = f"{base}-{i}"
            i += 1
        seen.add(rid)
        return rid

    agg["run_id"] = agg.apply(lambda r: make_id(r["show"], r["opening_date"]), axis=1)
    for col in ("opening_date", "closing_date"):
        agg[col] = agg[col].dt.strftime("%Y-%m-%d")

    for col in OUT_COLUMNS:
        if col not in agg.columns:
            agg[col] = pd.NA
    return agg[OUT_COLUMNS]


# 유튜브 첫 영상("Me at the zoo")이 올라온 날짜. 이보다 먼저 폐연한 공연은
# 당대 유튜브 영상이 존재할 수 없으니(유튜브 자체가 없었으니) target에서 제외함.
# still-running(closing_date 없음)인 run은 당연히 안 걸러짐.
YOUTUBE_LAUNCH_DATE = pd.Timestamp("2005-04-23")


def filter_pre_youtube_runs(targets_df):
    closing_ts = pd.to_datetime(targets_df["closing_date"], errors="coerce")
    keep_mask = closing_ts.isna() | (closing_ts >= YOUTUBE_LAUNCH_DATE)
    n_excluded = int((~keep_mask).sum())
    return targets_df[keep_mask].reset_index(drop=True), n_excluded


def build_targets(broadway_path):
    df = pd.read_csv(broadway_path, sep=None, engine="python", encoding="utf-8-sig")
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    df["week_ending"] = pd.to_datetime(df["week_ending"], errors="coerce")

    if "run_number" in df.columns:
        return build_targets_with_run_number(df)
    print("[경고] broadway.csv에 run_number 컬럼이 없어요 - calculate_revival_flags.py를 "
          "먼저 돌리는 걸 권장해요. 지금은 opening_date 기준 추정 방식으로 폴백해요.")
    return build_targets_legacy(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default="data/broadway.csv")
    ap.add_argument("--out", default="data/broadway_youtube_targets.csv")
    ap.add_argument("--include-pre-youtube", action="store_true",
                     help="유튜브 개설(2005-04-23) 이전에 폐연한 공연도 포함 (기본은 제외)")
    args = ap.parse_args()

    targets = build_targets(args.broadway)
    n_before_date_filter = len(targets)

    if not args.include_pre_youtube:
        targets, n_excluded = filter_pre_youtube_runs(targets)
        if n_excluded:
            print(f"유튜브 개설 이전 폐연 공연 {n_excluded}개 제외 "
                  f"({n_before_date_filter} -> {len(targets)}개 run)")

    targets.to_csv(args.out, index=False, encoding="utf-8-sig")

    n_revival_shows = targets[targets["is_revival"]]["show"].nunique()
    by_source = targets["date_source"].value_counts(dropna=False).to_dict()
    print(f"총 {len(targets)}개 run 생성 -> {args.out}")
    print(f"  리바이벌로 표시된 show: {n_revival_shows}개")
    print(f"  날짜/극장 출처별 run 수: {by_source}")
    if by_source.get("week_range", 0):
        print(f"  -- week_range({by_source['week_range']}건)는 근사치예요, "
              f"BroadwayWorld 보강 권장")
    if by_source.get("show_level_ambiguous", 0):
        print(f"  -- show_level_ambiguous({by_source['show_level_ambiguous']}건)는 동일 제목의 "
              f"다른 프로덕션이 BroadwayWorld에 여럿 있어서 opening_date/극장 값이 어느 쪽 "
              f"것인지 불확실해요 - youtube_collect_broadway.py가 이 표시로 날짜 필터 버퍼를 "
              f"넉넉하게 잡아요")


if __name__ == "__main__":
    main()
