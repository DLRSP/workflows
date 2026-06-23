#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVALUATE="${ROOT}/evaluate.sh"
FIXTURES="${ROOT}/tests/fixtures"
STUB_GH="${ROOT}/tests/bin/stub-gh"

failures=0

read_output() {
  local key="$1"
  local file="$2"
  if grep -q "^${key}<<EOF" "${file}"; then
    awk -v k="${key}" '$0 == k "<<EOF" { getline; print; exit }' "${file}"
  else
    grep "^${key}=" "${file}" | tail -1 | cut -d= -f2-
  fi
}

run_case() {
  local case_dir="$1"
  local name
  name="$(basename "${case_dir}")"
  # shellcheck disable=SC1090
  source "${case_dir}/env.sh"
  local policy_file="${ROOT}/../../../${POLICY_PATH:-policies/bot-policy.yaml}"

  export GITHUB_REPOSITORY="${GITHUB_REPOSITORY:?}"
  export GITHUB_OUTPUT
  GITHUB_OUTPUT="$(mktemp)"
  export GH_TOKEN=stub
  export PATH="${ROOT}/tests/bin:${PATH}"
  export STUB_GH_STATUS_ROLLUP="${case_dir}/status-rollup.json"
  export STUB_GH_PR_FILES="${case_dir}/pr-files.json"

  cp "${STUB_GH}" "${ROOT}/tests/bin/gh"
  chmod +x "${ROOT}/tests/bin/gh" "${EVALUATE}"

  : >"${GITHUB_OUTPUT}"
  "${EVALUATE}" "${policy_file}" "${ACTOR}" "${PR_NUMBER}" "${DRAFT:-false}" "${DEP_UPDATE_TYPE:-}"

  actual_decision="$(read_output decision "${GITHUB_OUTPUT}")"
  if [[ "${actual_decision}" != "${EXPECTED_DECISION}" ]]; then
    echo "FAIL ${name}: expected decision=${EXPECTED_DECISION} got ${actual_decision}"
    failures=$((failures + 1))
    return
  fi
  echo "PASS ${name}"
}

for case_dir in "${FIXTURES}"/*/; do
  run_case "${case_dir}"
done

if ((failures > 0)); then
  echo "${failures} fixture(s) failed"
  exit 1
fi

echo "All fixtures passed"
