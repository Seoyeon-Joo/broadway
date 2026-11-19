# 유사도 재측정 (ALT): TF-IDF / SBERT / BGE-en 등으로 다시 재기

기존 `similarity`(bge-m3, `SIMILARITY.md`)와 **같은 (작품, t2 영상) 쌍·같은 필터·같은 시놉시스**에 대해 다른 방식으로 다시 잰 값이다. 같은 결과가 다른 측정법에서도 유지되는지(로버스트니스) 보려는 용도.

## 실행
GitHub → Actions → **Compute Similarity ALT (TF-IDF / SBERT / BGE-en)** → Run workflow
- `only` 비우면 전부 계산. 일부만: `lex`, `sbert_mpnet`, `sbert_minilm`, `bge_large_en` 중 콤마로 (예: `lex,sbert_mpnet`)
- `limit`에 숫자를 넣으면 시험 실행 (결과 커밋 안 함)
- 영상 단위 12조각으로 병렬 실행 → 합치기. 결과는 `data/similarity/results_alt/`에 커밋되고 Artifacts로도 올라간다.

## 산출물 (`data/similarity/results_alt/`)
| 파일 | 내용 |
|---|---|
| `similarity_alt_results.csv` | (작품, 영상) 쌍별 모든 지표 + 기존 bge-m3 지표(`bgem3_*`) |
| `similarity_alt_spearman.csv` (+ `_hollywood`, `_broadway`) | 지표 간 Spearman 상관 |
| `similarity_alt_summary.json` | 쌍 수, 기술통계, 기존 `cov_mean`과의 상관 |

## 지표
- `lex_*` (어휘 기반): `lex_tfidf_uni_cos`, `lex_tfidf_bi_cos`, `lex_tfidf_cov_mean`, `lex_tfidf_cov_ge_*`, `lex_word_recall`(시놉시스 내용어 중 영상에 나오는 비율), `lex_entity_recall`(시놉시스 고유명사 중 영상에 나오는 비율)
- `<태그>_*` (임베딩): `doc_cosine`, `cov_mean`(기존 similarity와 같은 정의), `cov_median`, `cov_ge_*`(플롯 포인트 커버리지), `end_cov_mean`, `video_align_mean`
  - 태그: `sbert_mpnet`(all-mpnet-base-v2), `sbert_minilm`(all-MiniLM-L12-v2), `bge_large_en`(bge-large-en-v1.5)
- 임베딩 모델마다 값의 척도가 달라서 **모델 간 절대값을 비교하지 말고** 순위상관이나 회귀 계수의 부호·유의성으로 비교할 것.

## 패널에 붙이기
결과 CSV에서 쓰려는 열을 골라 (`medium`, `work_id`, `video_id`) 기준으로 패널의 (`media`, 작품 id, `t2_video_id`)에 병합하면 된다. 기존 `scripts/add_similarity_to_panel.py` 참고.

## 로컬 점검
```
python scripts/compute_similarity_alt.py --smoke --limit 60 --out-dir /tmp/alt_smoke    # 해싱 벡터로 파이프라인만 확인
```

## Qwen3-VL-4B 플롯 포인트 커버리지 (GPU, 직접 실행)
`scripts/qwen_vl_plot_coverage.py` — GitHub Actions에서는 못 돌린다 (무료 러너에 GPU 없음 + 영상 다운로드가 YouTube IP 차단).
1. `caption`: 영상 파일(`videos/{video_id}.mp4`)에서 30초 구간마다 4프레임을 뽑아 Qwen3-VL-4B로 장면 설명 생성
2. `judge`: 시놉시스 문장마다 영상 근거(자막 + 장면 설명)를 붙여 "이 사건이 영상에 나오는가" Yes 확률 → `pc_mean`, `pc_ratio`, `pc_end_mean`
`judge`는 영상 파일 없이 자막만으로도 돌아간다(그 경우 `has_visual_captions=0`). 사용법은 스크립트 상단 주석 참고. GPU 환경이 없어 작성 환경에서 실행 검증을 못 했으므로 `--limit 3`으로 먼저 확인할 것.
