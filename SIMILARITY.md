# 시놉시스 ↔ 영상 스크립트 유사도 (Spoiler Intensity의 similarity)

## 실행
GitHub → Actions → **Compute Synopsis-Transcript Similarity** → Run workflow
(`model` 비우면 `config_similarity.yml`의 `BAAI/bge-m3`, `limit`에 숫자를 넣으면 일부만 시험 실행).
결과는 `data/similarity/results/`에 커밋되고 Artifacts로도 올라간다.

## 입력
| 파일 | 설명 |
|---|---|
| `data/similarity/youtube_transcripts_all.xlsx` | 영상 스크립트 (video_url, text, status, language, is_auto, title) |
| `data/similarity/synopses_hollywood.xlsx`, `synopses_broadway.xlsx` | Wikipedia 시놉시스 |
| `data/video_work_map.csv` | video_url → 작품(work_id) 매핑. `scripts/build_video_work_map.py`로 생성 (Release의 병합 CSV 필요) |

새 스크립트를 추가하면 xlsx를 교체하고 매핑을 다시 만든 뒤 워크플로를 돌리면 된다.

## 분석 단위와 필터
- 단위: (영상, 작품) 쌍. 한 영상이 여러 작품에 매칭되면 쌍마다 한 행.
- 영어만 사용 (`languages: [en]`): 메타데이터 language가 en 계열이 아니거나, 텍스트가 영어로 보이지 않으면 제외.
- 제외: status≠ok, 50단어 미만 스크립트, 30단어 미만 시놉시스. 제외 목록은 `similarity_excluded_transcripts.csv`, `similarity_skipped_pairs.csv`.
- `synopsis_is_lead=True`는 Plot 섹션이 없어 리드 문단을 쓴 시놉시스 → 분석 시 제외/통제 권장. `needs_review`, `match_confidence`도 함께 출력.

## 지표 (`similarity_results.csv`)
`doc_cosine`, `cov_mean`, `cov_ge_06/07`, `end_cov_mean`, `video_align_mean`, `tfidf_cosine` — 정의는 `scripts/compute_similarity.py` 상단 주석 참고.
`cov_ge_*`의 임계값은 모델 분포를 본 뒤 `coverage_thresholds`에서 조정할 것.
