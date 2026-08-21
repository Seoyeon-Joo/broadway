"""broadway_pipeline.yml의 'Step 1 result summary'에서 호출하는 헬퍼.
data/broadway.csv의 최근 week_ending 5개를 출력해서 이어붙이기가 잘 됐는지 눈으로 확인용."""
import pandas as pd

df = pd.read_csv("data/broadway.csv", sep=None, engine="python", encoding="utf-8-sig")
weeks = sorted(pd.to_datetime(df["week_ending"]).unique())
print(weeks[-5:])
