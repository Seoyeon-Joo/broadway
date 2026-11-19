# 유사도 추가 측정 (EXTRA): Qwen3-Embedding / 리랭커 / NLI / 서사 순서 + 통합 지표

기존 `similarity`(bge-m3, `SIMILARITY.md`)와 ALT(TF-IDF·SBERT·bge-en, `SIMILARITY_ALT.md`)가 이미 재는 방식과 **겹치지 않는** 방식으로, **같은 (작품, t2 영상) 쌍·같은 필터·같은 시놉시스**에 대해 다시 잰다. Broadway와 Hollywood만 쓴다 (KOPIS 제외).

## 실행
GitHub → Actions → **Compute Similarity EXTRA (Qwen3-Embedding / reranker / NLI / order)** → Run workflow
- `limit`에 `40`을 넣으면 시험 실행 (결과 커밋 안 함). 모델 다운로드와 열 구성이 정상인지 먼저 확인할 때 쓴다.
- `only`: `reranker`, `nli` 중 일부만 (비우면 config대로 전부). retriever(Qwen3)와 순서 지표는 항상 계산한다.
- 영상 단위 12조각으로 병렬 실행 → 합치기 → 통합 지표 계산. 결과는 `data/similarity/results_extra/`에 커밋되고 Artifacts로도 올라간다.

## 지표
| 접두어 | 방식 | 무엇을 보나 |
|---|---|---|
| `q3emb06_*` | Qwen3-Embedding-0.6B (시놉시스 문장에만 지시문을 붙이는 질의-문서 방식) | `cov_mean` 등 기존과 같은 정의의 커버리지. 모델 계열이 달라 로버스트니스용 |
| `q3emb06_order_tau` | 시놉시스 문장 번호 vs 영상에서 가장 비슷한 청크 위치의 Kendall tau | 영상이 줄거리를 시놉시스 순서대로 따라가는가 (1 = 순서대로, -1 = 거꾸로) |
| `q3emb06_mono_ratio` | 순서를 지키는 최적 정렬 점수 합 / 문장별 최대 점수 합 | 위와 같은 질문의 다른 정의 (1 = 이미 순서대로) |
| `q3emb06_span` | 커버리지 상위 절반 문장이 영상 타임라인에서 차지하는 폭 (0~1) | 한 장면만 요약한 영상(0)과 처음부터 끝까지 풀어 주는 영상(1) 구분 |
| `x_rerank_*` | BAAI/bge-reranker-v2-m3 | 문장-청크를 한 쌍으로 넣은 관련도 확률. 후보 청크는 Qwen3 유사도 상위 `cross_top_k`(3)개만 |
| `x_nli_*` | MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli | 자막 청크(전제)가 시놉시스 문장(가설)을 함의할 확률. 주제만 비슷한 영상과 실제로 그 사건을 말하는 영상을 구분 |

`x_*_mean`은 문장별 최댓값의 평균, `_median`, `_ge_05`(확률 0.5 이상인 문장 비율), `_end_mean`(결말부 문장)도 함께 나온다.

주의
- `mono_ratio`는 영상이 한 장면에만 몰려 있어도 1이 된다. 반드시 `span`과 같이 해석한다.
- 자동자막은 구두점이 없어서 NLI 점수가 전반적으로 낮게 나올 수 있다. 절대값보다 순위상관, 회귀 계수의 부호·유의성으로 비교한다.
- 모델마다 값의 척도가 달라 절대값 비교는 의미가 없다.

## 통합 지표와 길이 편향 (`scripts/build_similarity_composite.py`)
워크플로 마지막 단계에서 자동 실행된다. ALT가 먼저 끝나 있으면 EXTRA 없이도 단독 실행할 수 있다.
```
python scripts/build_similarity_composite.py --out-dir data/similarity/results_extra
```
| 파일 | 내용 |
|---|---|
| `similarity_composite.csv` | `sim_z_mean`(지표 z점수 평균), `sim_pc1`(1주성분), 각각의 길이 보정판 `*_lenadj`, `n_methods_used` |
| `similarity_composite_report.json` | 선택된 지표, PC1 설명 비율·적재, Cronbach alpha, 지표 간 평균 Spearman |
| `similarity_length_bias.csv` | 각 지표와 log(자막 단어 수)의 Spearman (전체·매체별) |

`*_lenadj`는 매체별로 log(자막 단어 수), log(시놉시스 단어 수), log(문장 수)를 OLS로 제거한 잔차를 매체 안에서 표준화한 값이다.

## 로컬 점검
```
python scripts/compute_similarity_extra.py --config config_similarity_extra.yml --smoke --limit 40 --out-dir /tmp/extra_smoke
```
`--smoke`는 모델 대신 해싱 벡터와 단어 겹침 점수를 써서 파이프라인만 점검한다 (논문 결과에 쓰지 말 것).

## GPU로 더 큰 모델 (직접 실행)
`config_similarity_extra.yml`의 `retriever`를 `Qwen/Qwen3-Embedding-4B`(또는 8B)로, `tag`를 `q3emb4b`로 바꾸고 `device: cuda`로 두면 Colab/로컬 GPU에서 같은 스크립트로 돌릴 수 있다. 출력 열 접두어가 태그를 따라가므로 0.6B 결과와 겹치지 않는다.
