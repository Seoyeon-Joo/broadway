#!/usr/bin/env bash
# checkpoint_release.sh <tag> <path1> [path2 ...]
#
# 왜 git commit 대신 Release 업로드로 바꿨나:
#   data/broadway.csv가 136MB로 커져서 GitHub의 git push 파일 크기 제한(100MB)에
#   걸림. 그래서 전 단계 파이프라인은 8단계가 전부 "성공"으로 표시돼도 실제로는
#   커밋이 하나도 안 됐음(push가 매번 조용히 실패). Release 자산은 파일당 2GB까지
#   허용되고 git 히스토리에도 안 쌓여서 이 문제를 근본적으로 피할 수 있음.
#
# 동작 방식:
#   같은 태그(예: broadway-data)에 매번 같은 파일명으로 덮어써서(--clobber)
#   "최신 데이터 = 이 Release" 상태를 유지함. Release가 없으면 새로 만듦.
#   업로드 실패 시 최대 3회 재시도, 다 실패하면 ::error:: 로 표시하고 exit 1
#   (예전 git push 재시도 버그처럼 조용히 넘어가지 않게 함).
#
# *** 0바이트 파일은 업로드 대상에서 제외 ***
#   실제로 겪은 버그: 이번 실행에서 처리한 게 하나도 없는 shard의
#   checkpoint(.processed.txt)가 0바이트로 생성되는데, 이걸 그대로 업로드하면
#   `gh release upload`가 "HTTP 400: Bad Content-Length"로 실패함(빈 파일의
#   멀티파트 업로드를 gh/GitHub API가 제대로 처리 못 하는 것으로 보임). 한
#   호출에 여러 파일을 같이 올리면 그 중 하나만 0바이트여도 전체 호출이
#   실패해서, 정상 파일(예: shard_N.csv)까지 덩달아 안 올라감 - 그래서 파일
#   존재 여부뿐 아니라 크기도 확인해서 0바이트면 조용히 건너뜀.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "사용법: checkpoint_release.sh <tag> <path1> [path2 ...]" >&2
  exit 1
fi

tag="$1"
shift
files=("$@")

existing_files=()
for f in "${files[@]}"; do
  if [ ! -f "$f" ]; then
    echo "경고: '$f' 파일이 없어서 이번 업로드에서 제외함"
  elif [ ! -s "$f" ]; then
    echo "경고: '$f' 파일이 0바이트라서 이번 업로드에서 제외함 (gh release upload가 " \
         "빈 파일에서 Bad Content-Length 오류를 내서 아예 안 올림)"
  else
    existing_files+=("$f")
  fi
done

if [ "${#existing_files[@]}" -eq 0 ]; then
  echo "업로드할 파일이 하나도 없어서 건너뜀"
  exit 0
fi

if ! gh release view "$tag" >/dev/null 2>&1; then
  gh release create "$tag" \
    --title "$tag" \
    --notes "Broadway 파이프라인이 자동으로 생성하는 최신 데이터 파일들. 매주 월요일(UTC) 갱신됨. 같은 파일명은 매번 덮어쓰기 됨(과거 버전은 남지 않음)."
fi

for attempt in 1 2 3; do
  if gh release upload "$tag" "${existing_files[@]}" --clobber; then
    echo "release 업로드 성공 (시도 $attempt/3): ${existing_files[*]}"
    exit 0
  fi
  echo "release 업로드 실패 (시도 $attempt/3)"
  if [ "$attempt" = "3" ]; then
    echo "::error::checkpoint_release.sh - 3회 재시도 후에도 release 업로드 실패: ${existing_files[*]}"
    exit 1
  fi
  sleep $((5 * attempt))
done
