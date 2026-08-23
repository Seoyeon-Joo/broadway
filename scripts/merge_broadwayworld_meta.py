"""
merge_broadwayworld_meta.py
=============================
fetch_broadwayworld_full.py로 만든 broadwayworld_full.csv(장르/캐스트/창작진/
프로듀서/개막·폐막일/원작유무/기사수/시상식별 수상·후보), fetch_tony_awards.py로
만든 tony_awards.csv, fetch_bww_reviews.py로 만든 bww_reviews.csv(집계 평점만,
개별 리뷰 텍스트는 별도 파일)를 data/broadway.csv에 show 이름 기준으로 병합.

컬럼을 고정 목록으로 하드코딩하지 않고, 각 소스 파일에 있는 컬럼(title 제외)을
전부 자동으로 병합함. 시상식별 컬럼(tony_awards_wins, drama_desk_awards_nominations
등)이 쇼마다 다르게 생기는 동적 스키마라 고정 목록 방식은 한계가 있어서 이렇게
바꿈. 이미 있는 주간 흥행 컬럼(week_ending, weekly_gross, seats_sold 등)은 전혀
건드리지 않고, 이미 채워진 값은 유지함(덮어쓰지 않음, 새로 찾은 값이 있을 때만 채움).

*** --history 옵션 (신규) ***
show_history.csv(fetch_show_history.py 결과: title, showid, performance_id,
venue, start_date, end_date)를 병합할 땐 title만으로 합치면 안 됨 - 리바이벌이
있는 쇼는 BroadwayWorld showid 자체가 "쇼 하나당 하나"라서(genre/cast/opening_date
등 나머지 메타도 이 한계를 그대로 가짐), title 기준 병합은 여러 run 중 한 프로덕션의
값을 모든 run에 덮어씌우는 문제가 있음. 대신 이미 계산된 run_number(각 run이 실제로
관측된 week_ending 최소~최대 구간)와 show_history의 [start_date, end_date]가 가장 많이
겹치는 production을 골라 매칭함. 결과는 run_theatre/run_opening_date/run_closing_date/
run_performance_id로 별도 컬럼에 저장 - 기존 show-level opening_date/closing_date/
cast 등은 건드리지 않음 (그것들은 여전히 title 단위 근사치라는 한계가 있다는 점 유의).

broadway.csv에 run_number 컬럼이 없으면(calculate_revival_flags.py를 아직 안
돌렸으면) --history는 조용히 건너뜀 -> 파이프라인 순서(Step4 이후에 Step10)를
지켜야 함.

Usage:
  python merge_broadwayworld_meta.py \
      --broadway data/broadway.csv \
      --meta data/broadwayworld_full.csv \
      --awards data/tony_awards.csv \
      --reviews data/bww_reviews.csv \
      --history data/show_history.csv \
      --out data/broadway.csv
"""
import argparse

import pandas as pd


def _parse_bww_date(s):
    """BroadwayWorld/show_history 쪽 날짜 문자열('April 21, 2016' 등)을 파싱.
    실패하면 NaT. format="mixed" 필수 - 컬럼 안에 형식이 섞여 있으면(예: 일부는
    ISO 'YYYY-MM-DD', 일부는 'Month DD, YYYY') 지정 없이는 첫 값 기준 포맷을
    고정해버려서 나머지가 전부 NaT가 되는 pandas 동작이 있음 (실제로 겪은 버그)."""
    return pd.to_datetime(s, errors="coerce", format="mixed")


def merge_show_history(broadway, history_path):
    if not history_path:
        return broadway
    if "run_number" not in broadway.columns:
        print("[show_history] broadway.csv에 run_number 컬럼이 없어서 건너뜀 "
              "(calculate_revival_flags.py를 먼저 돌려야 함)")
        return broadway

    hist = pd.read_csv(history_path, sep=None, engine="python", encoding="utf-8-sig")
    hist.columns = [c.strip().lstrip("\ufeff") for c in hist.columns]
    required = {"title", "performance_id", "venue", "start_date", "end_date"}
    if not required.issubset(hist.columns):
        print(f"[show_history] 필요한 컬럼이 없어서 건너뜀. 실제 컬럼: {list(hist.columns)}")
        return broadway

    hist = hist.copy()
    hist["_start"] = _parse_bww_date(hist["start_date"])
    hist["_end"] = _parse_bww_date(hist["end_date"])
    # end_date가 없으면(현재도 공연 중) 아주 먼 미래로 취급해서 겹침 계산이 안 깨지게 함
    hist["_end"] = hist["_end"].fillna(pd.Timestamp("2100-01-01"))

    broadway = broadway.copy()
    broadway["week_ending"] = pd.to_datetime(broadway["week_ending"], errors="coerce")
    run_windows = (
        broadway.groupby(["show", "run_number"])["week_ending"]
        .agg(_run_start="min", _run_end="max")
        .reset_index()
    )

    matches = []
    for _, run in run_windows.iterrows():
        candidates = hist[hist["title"] == run["show"]]
        if candidates.empty:
            continue
        best_row, best_overlap = None, pd.Timedelta(0)
        for _, cand in candidates.iterrows():
            overlap_start = max(run["_run_start"], cand["_start"]) if pd.notna(cand["_start"]) else None
            overlap_end = min(run["_run_end"], cand["_end"]) if pd.notna(cand["_end"]) else None
            if overlap_start is None or overlap_end is None or overlap_end < overlap_start:
                continue
            overlap = overlap_end - overlap_start
            if best_row is None or overlap > best_overlap:
                best_row, best_overlap = cand, overlap
        if best_row is not None:
            matches.append({
                "show": run["show"],
                "run_number": run["run_number"],
                "run_theatre": best_row["venue"],
                "run_opening_date": best_row["start_date"],
                "run_closing_date": best_row["end_date"],
                "run_performance_id": best_row["performance_id"],
            })

    if not matches:
        print("[show_history] 겹치는 production을 하나도 못 찾아서 건너뜀 "
              "(show_history.csv 날짜 형식이나 커버리지를 확인해보세요)")
        return broadway

    match_df = pd.DataFrame(matches)
    merged = broadway.merge(match_df, on=["show", "run_number"], how="left")
    n_runs_total = len(run_windows)
    n_runs_matched = len(match_df)
    print(f"[show_history] run {n_runs_total}개 중 {n_runs_matched}개에 극장/기간 매칭 완료 "
          f"(run_theatre/run_opening_date/run_closing_date/run_performance_id 컬럼 추가)")
    return merged


def merge_source(broadway, source_path, source_label):
    if not source_path:
        return broadway
    src = pd.read_csv(source_path, sep=None, engine="python", encoding="utf-8-sig")
    src.columns = [c.strip().lstrip("\ufeff") for c in src.columns]
    if "title" not in src.columns:
        print(f"[{source_label}] 'title' 컬럼이 없어서 건너뜀. 실제 컬럼: {list(src.columns)}")
        return broadway

    # title/showid를 제외한 모든 컬럼을 자동으로 병합 대상으로 삼음
    columns = [c for c in src.columns if c not in ("title", "showid")]
    if not columns:
        print(f"[{source_label}] 병합할 컬럼이 없어서 건너뜀")
        return broadway

    src_small = src.drop_duplicates(subset=["title"], keep="last")[["title"] + columns]

    merged = broadway.merge(
        src_small, left_on="show", right_on="title", how="left", suffixes=("", "_new")
    )

    for col in columns:
        new_col = f"{col}_new"
        if new_col not in merged.columns:
            continue
        if col in merged.columns:
            # 숫자 컬럼(예: _wins/_nominations)은 0도 유효한 값이라 notna만으로 판단
            existing = merged[col]
            is_missing = existing.isna()
            if existing.dtype == object:
                is_missing = is_missing | (existing == "")
            merged[col] = existing.where(~is_missing, merged[new_col])
        else:
            merged[col] = merged[new_col]
        merged = merged.drop(columns=[new_col])

    if "title" in merged.columns and "title" not in broadway.columns:
        merged = merged.drop(columns=["title"])

    n_matched = merged[columns[0]].notna().sum() if columns else 0
    print(f"[{source_label}] 매칭된 행: {n_matched}/{len(merged)}, 추가/갱신된 컬럼: {columns}")
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default="data/broadway.csv")
    ap.add_argument("--meta", default=None, help="broadwayworld_full.csv 경로")
    ap.add_argument("--awards", default=None, help="tony_awards.csv 경로")
    ap.add_argument("--reviews", default=None, help="bww_reviews.csv 경로 (집계 평점만)")
    ap.add_argument("--history", default=None,
                     help="show_history.csv 경로 - run_number 기준 날짜 겹침 매칭으로 "
                          "run_theatre/run_opening_date/run_closing_date/run_performance_id 추가")
    ap.add_argument("--out", default="data/broadway.csv")
    args = ap.parse_args()

    broadway = pd.read_csv(args.broadway, sep=None, engine="python", encoding="utf-8-sig")
    broadway.columns = [c.strip().lstrip("\ufeff") for c in broadway.columns]

    if "show" not in broadway.columns:
        raise SystemExit(f"broadway.csv에 'show' 컬럼이 없어요. 실제 컬럼: {list(broadway.columns)}")

    merged = merge_source(broadway, args.meta, "BroadwayWorld meta")
    merged = merge_source(merged, args.awards, "Tony Awards")
    merged = merge_source(merged, args.reviews, "BroadwayWorld reviews")
    merged = merge_show_history(merged, args.history)

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {args.out} ({len(merged)}행, {len(merged.columns)}컬럼)")


if __name__ == "__main__":
    main()
