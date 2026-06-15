#!/usr/bin/env bash
# Evaluate bot-policy.yaml (first-match-wins, fail-closed on errors).
set -euo pipefail

POLICY_FILE="${1:?policy file}"
ACTOR="${2:?actor}"
PR_NUMBER="${3:?pr number}"
DRAFT="${4:-false}"
DEP_UPDATE_TYPE="${5:-}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"

emit() {
  local key="$1"
  local value="$2"
  echo "${key}=${value}" >> "${GITHUB_OUTPUT}"
}

fail_closed() {
  local reason="$1"
  emit decision block
  emit reason "${reason}"
  emit actions "$(printf '%s\n' 'label:needs-human-review' 'comment:blocked-by-policy' | jq -R . | jq -s -c .)"
  emit matched-policy "error"
  exit 0
}

actor_in_list() {
  local actor="$1"
  local list_key="$2"
  yq -e ".${list_key}[] | select(. == \"${actor}\")" "${POLICY_FILE}" >/dev/null 2>&1
}

resolve_actor_in() {
  local actor="$1"
  local spec="$2"
  case "${spec}" in
    trusted_actors) actor_in_list "${actor}" trusted_actors ;;
    shim_actors) actor_in_list "${actor}" shim_actors ;;
    union*)
      actor_in_list "${actor}" trusted_actors || actor_in_list "${actor}" shim_actors
      ;;
    *) return 1 ;;
  esac
}

when_matches() {
  local actor="$1"
  local index="$2"
  local when_json
  when_json="$(yq -o=json ".policies[${index}].when" "${POLICY_FILE}")"

  if [[ "${when_json}" == "null" ]]; then
    return 1
  fi

  if [[ "$(echo "${when_json}" | jq -r 'has("always")')" == "true" ]]; then
    if [[ "$(echo "${when_json}" | jq -r '.always')" != "true" ]]; then
      return 1
    fi
  fi

  if [[ "$(echo "${when_json}" | jq -r 'has("draft")')" == "true" ]]; then
    local want_draft
    want_draft="$(echo "${when_json}" | jq -r '.draft')"
    if [[ "${want_draft}" == "false" && "${DRAFT}" == "true" ]]; then
      return 1
    fi
    if [[ "${want_draft}" == "true" && "${DRAFT}" != "true" ]]; then
      return 1
    fi
  fi

  if [[ "$(echo "${when_json}" | jq -r 'has("actor_in")')" == "true" ]]; then
    local actor_spec
    actor_spec="$(echo "${when_json}" | jq -r '.actor_in')"
    if ! resolve_actor_in "${actor}" "${actor_spec}"; then
      return 1
    fi
  fi

  return 0
}

check_ci_status() {
  local required_json="$1"
  local rollup
  rollup="$(gh pr view "${PR_NUMBER}" --repo "${REPO}" --json statusCheckRollup -q '.statusCheckRollup' 2>/dev/null)" || {
    fail_closed "ci_status: unable to fetch statusCheckRollup"
  }

  local missing=()
  local failing=()
  local pending=()

  while IFS= read -r check_name; do
    [[ -z "${check_name}" ]] && continue
    local entry
    entry="$(echo "${rollup}" | jq -c --arg n "${check_name}" '[.[] | select(.name == $n)][0]')"
    if [[ "${entry}" == "null" ]]; then
      missing+=("${check_name}")
      continue
    fi
    local status conclusion
    status="$(echo "${entry}" | jq -r '.status // empty')"
    conclusion="$(echo "${entry}" | jq -r '.conclusion // empty')"
    if [[ "${status}" == "QUEUED" || "${status}" == "IN_PROGRESS" || "${status}" == "PENDING" || "${conclusion}" == "" ]]; then
      pending+=("${check_name}")
    elif [[ "${conclusion}" != "SUCCESS" && "${conclusion}" != "NEUTRAL" && "${conclusion}" != "SKIPPED" ]]; then
      failing+=("${check_name}")
    fi
  done < <(echo "${required_json}" | jq -r '.[]')

  if ((${#missing[@]} > 0)); then
    echo "ci_status missing: ${missing[*]}"
    return 1
  fi
  if ((${#pending[@]} > 0)); then
    echo "ci_status pending: ${pending[*]}"
    return 1
  fi
  if ((${#failing[@]} > 0)); then
    echo "ci_status failing: ${failing[*]}"
    return 1
  fi
  return 0
}

path_matches_glob() {
  local path="$1"
  local pattern="$2"
  python3 - "${path}" "${pattern}" <<'PY'
import fnmatch, sys
print("yes" if fnmatch.fnmatch(sys.argv[1], sys.argv[2]) else "no")
PY
}

check_paths_deny() {
  local deny_json="$1"
  local files
  files="$(gh api "repos/${REPO}/pulls/${PR_NUMBER}/files" --paginate -q '.[].filename' 2>/dev/null)" || {
    fail_closed "paths: unable to list PR files"
  }

  while IFS= read -r pattern; do
    [[ -z "${pattern}" ]] && continue
    while IFS= read -r file; do
      [[ -z "${file}" ]] && continue
      if [[ "$(path_matches_glob "${file}" "${pattern}")" == "yes" ]]; then
        echo "paths deny matched: ${file} ~ ${pattern}"
        return 1
      fi
    done <<< "${files}"
  done < <(echo "${deny_json}" | jq -r '.[]')

  return 0
}

check_semver() {
  local block_level="$1"
  if [[ "${block_level}" == "major" && "${DEP_UPDATE_TYPE}" == "version-update:semver-major" ]]; then
    echo "semver block: major update"
    return 1
  fi
  return 0
}

gates_pass() {
  local index="$1"
  local gates_json
  gates_json="$(yq -o=json ".policies[${index}].gates // []" "${POLICY_FILE}")"
  if [[ "${gates_json}" == "[]" || "${gates_json}" == "null" ]]; then
    return 0
  fi

  local gate_count
  gate_count="$(echo "${gates_json}" | jq 'length')"
  local i reason
  for ((i = 0; i < gate_count; i++)); do
    local gate
    gate="$(echo "${gates_json}" | jq -c ".[${i}]")"
    if [[ "$(echo "${gate}" | jq -r 'has("ci_status")')" == "true" ]]; then
      if ! reason="$(check_ci_status "$(echo "${gate}" | jq -c '.ci_status.required')")"; then
        echo "${reason}"
        return 1
      fi
    elif [[ "$(echo "${gate}" | jq -r 'has("paths")')" == "true" ]]; then
      if ! reason="$(check_paths_deny "$(echo "${gate}" | jq -c '.paths.deny')")"; then
        echo "${reason}"
        return 1
      fi
    elif [[ "$(echo "${gate}" | jq -r 'has("semver")')" == "true" ]]; then
      if ! reason="$(check_semver "$(echo "${gate}" | jq -r '.semver.block')")"; then
        echo "${reason}"
        return 1
      fi
    else
      fail_closed "unknown gate type in policy"
    fi
  done
  return 0
}

# Fail-closed on invalid policy file.
if ! yq -e '.version == 1' "${POLICY_FILE}" >/dev/null 2>&1; then
  fail_closed "policy version must be 1"
fi

if ! actor_in_list "${ACTOR}" trusted_actors && ! actor_in_list "${ACTOR}" shim_actors; then
  emit decision skip
  emit reason "actor not in trusted_actors or shim_actors"
  emit actions "$(jq -n -c '[]')"
  emit matched-policy ""
  exit 0
fi

policy_count="$(yq '.policies | length' "${POLICY_FILE}")"
for ((idx = 0; idx < policy_count; idx++)); do
  if ! when_matches "${ACTOR}" "${idx}"; then
    continue
  fi

  policy_name="$(yq -r ".policies[${idx}].name" "${POLICY_FILE}")"
  then_json="$(yq -o=json ".policies[${idx}].then" "${POLICY_FILE}")"
  has_gates="$(yq ".policies[${idx}] | has(\"gates\")" "${POLICY_FILE}")"

  if [[ "${has_gates}" == "true" ]]; then
    gate_reason=""
    if ! gate_reason="$(gates_pass "${idx}")"; then
      echo "policy ${policy_name} gates failed: ${gate_reason}" >&2
      continue
    fi
    emit decision approve
    emit reason "${policy_name}: all gates passed"
    emit actions "${then_json}"
    emit matched-policy "${policy_name}"
    exit 0
  fi

  emit decision block
  emit reason "${policy_name}: matched catch-all policy"
  emit actions "${then_json}"
  emit matched-policy "${policy_name}"
  exit 0
done

emit decision block
emit reason "trusted/shim actor but no policy produced a decision"
emit actions "$(printf '%s\n' 'label:needs-human-review' 'comment:blocked-by-policy' | jq -R . | jq -s -c .)"
emit matched-policy "fallback"
