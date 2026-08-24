"""broadway_pipeline.yml의 'Step 2 result summary'에서 호출하는 헬퍼.
data/broadwayworld_full.csv의 title_match_type 분포를 출력해서 슬러그 매칭이
근사 매칭(colon_stripped/comma_stripped/prefix_fallback)에 얼마나 의존했는지
매 실행마다 눈으로 확인용. 특히 prefix_fallback은 오매칭 가능성이 가장 높은
단계라 목록을 같이 찍어서 검토하기 쉽게 함(2026-08-24 매칭 로직 보강과 함께 추가)."""
import pandas as pd

df = pd.read_csv("data/broadwayworld_full.csv", sep=None, engine="python", encoding="utf-8-sig")

if "title_match_type" not in df.columns:
    print("title_match_type 컬럼 없음 (기존 broadwayworld_full.csv에 신규 컬럼 반영 전)")
else:
    print(df["title_match_type"].value_counts(dropna=False).to_string())
    prefix_rows = df[df["title_match_type"] == "prefix_fallback"]
    if len(prefix_rows):
        print(f"\nprefix_fallback로 매칭된 {len(prefix_rows)}개 쇼 (title -> slug) - 검토 권장:")
        for _, r in prefix_rows.iterrows():
            print(f"  {r['title']!r} -> {r['slug']!r}")
