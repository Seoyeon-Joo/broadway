"""
calculate_revival_flags.py
=============================
data/broadway.csv 안에서 같은 show 이름이라도 시간 간격이 크게 벌어지면
별개의 run(오리지널 vs 재공연)으로 간주해서 run_number, is_revival 컬럼을 계산.

새로 크롤링할 필요 없이 이미 있는 week_ending만으로 계산 가능함.
기준: 같은 쇼의 연속된 두 주간 데이터 사이 간격이 GAP_DAYS(기본 180일)를
넘으면 새 run으로 취급. 코로나 셧다운(518일 공백)처럼 전체 브로드웨이가
멈춘 기간은 모든 쇼에 공통으로 걸리므로, 이 기준으로는 셧다운 자체가
"재공연"으로 오분류되지 않도록 별도 예외 처리는 하지 않음 -
(셧다운 이후 재개한 공연은 같은 run의 연장으로 보는 게 자연스러워서)
GAP_DAYS를 518일보다 크게 잡거나, 필요하면 --exclude-shutdown-gap 옵션으로
2020-03-08~2021-08-08 사이 공백은 run 분리 기준에서 제외할 수 있음.

Usage:
  python calculate_revival_flags.py --broadway data/broadway.csv --out data/broadway.csv
"""
import argparse

import pandas as pd

SHUTDOWN_GAP_START = pd.Timestamp("2020-03-08")
SHUTDOWN_GAP_END = pd.Timestamp("2021-08-08")


def assign_run(g, gap_days, exclude_shutdown_gap):
    g = g.sort_values("week_ending")
    diffs = g["week_ending"].diff()
    gap_flag = diffs.dt.days.fillna(0) > gap_days

    if exclude_shutdown_gap:
        prev_week = g["week_ending"].shift(1)
        # 버그 수정: 예전엔 "prev_week가 셧다운 시작 이전"이고 "week_ending이 셧다운
        # 종료 이후"라는 한쪽 방향 조건이라, 셧다운 기간을 통째로 감싸버리는 훨씬 큰
        # 공백(예: 2015년 종연 -> 2024년 재개막)까지 전부 "셧다운 공백"으로 오분류됨
        # (Cabaret에서 실제로 발견 - 2014~2015 Studio 54 공연과 2024~2025 August
        # Wilson Theatre 공연이 run_number 하나로 잘못 합쳐졌었음). 진짜 셧다운
        # 공백이려면 공백의 양쪽 끝이 전부 셧다운 시작/종료 시점에 바짝 붙어있어야
        # 함 - 그래서 양방향 범위 체크로 바꿈.
        is_shutdown_gap = (
            (prev_week >= SHUTDOWN_GAP_START - pd.Timedelta(days=14))
            & (prev_week <= SHUTDOWN_GAP_START + pd.Timedelta(days=14))
            & (g["week_ending"] >= SHUTDOWN_GAP_END - pd.Timedelta(days=14))
            & (g["week_ending"] <= SHUTDOWN_GAP_END + pd.Timedelta(days=14))
        )
        gap_flag = gap_flag & ~is_shutdown_gap.fillna(False)

    g = g.copy()
    g["run_number"] = gap_flag.cumsum() + 1
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadway", default="data/broadway.csv")
    ap.add_argument("--out", default="data/broadway.csv")
    ap.add_argument("--gap-days", type=int, default=180,
                     help="이 일수보다 크게 비면 새 run(재공연)으로 간주 (기본 180일)")
    ap.add_argument("--exclude-shutdown-gap", action="store_true",
                     help="코로나 셧다운 공백(2020-03-08~2021-08-08)은 run 분리 기준에서 제외")
    args = ap.parse_args()

    df = pd.read_csv(args.broadway, sep=None, engine="python", encoding="utf-8-sig")
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    df["week_ending"] = pd.to_datetime(df["week_ending"])

    if "show" not in df.columns:
        raise SystemExit(f"'show' 컬럼이 없어요. 실제 컬럼: {list(df.columns)}")

    df = df.sort_values(["show", "week_ending"]).reset_index(drop=True)
    cols = df.columns.tolist()
    out = df.groupby("show", group_keys=False)[cols].apply(
        lambda g: assign_run(g, args.gap_days, args.exclude_shutdown_gap)
    )
    out["is_revival"] = (out["run_number"] > 1).astype(int)

    n_multi = out.groupby("show")["run_number"].max()
    n_multi = (n_multi > 1).sum()
    print(f"재공연으로 감지된 쇼 수: {n_multi}개 (gap_days={args.gap_days})")

    out = out.sort_values(["show", "week_ending"]).reset_index(drop=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {args.out} ({len(out)}행, run_number/is_revival 컬럼 추가됨)")


if __name__ == "__main__":
    main()
