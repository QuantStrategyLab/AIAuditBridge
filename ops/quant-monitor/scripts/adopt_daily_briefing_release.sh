#!/usr/bin/env bash
# Install an exact immutable release and switch only codex-daily-briefing.service.
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: adopt_daily_briefing_release.sh --sha <40-hex-main-sha> \
  --expected-current-sha <40-hex-release-sha> --repo <git-root> \
  [--release-root <dir>] [--runtime-data <dir>] [--runtime-venv <dir>]

Installs the immutable code release, performs offline imports with the existing
runtime venv, and changes only the daily service drop-in. It never installs
packages, starts a service or timer, reads the ledger, or sends a message.
EOF
}

die() {
  echo "[daily-adoption] status=stopped reason=$1" >&2
  exit 2
}

SHA=""
EXPECTED_CURRENT_SHA=""
REPO_ROOT=""
RELEASE_ROOT="/opt/quant-monitor/releases"
RUNTIME_DATA=""
RUNTIME_VENV=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --expected-current-sha) EXPECTED_CURRENT_SHA="${2:-}"; shift 2 ;;
    --repo) REPO_ROOT="${2:-}"; shift 2 ;;
    --release-root) RELEASE_ROOT="${2:-}"; shift 2 ;;
    --runtime-data) RUNTIME_DATA="${2:-}"; shift 2 ;;
    --runtime-venv) RUNTIME_VENV="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unsupported_arguments" ;;
  esac
done

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "invalid_release_sha"
[[ "$EXPECTED_CURRENT_SHA" =~ ^[0-9a-f]{40}$ ]] || die "invalid_expected_current_sha"
[[ -n "$REPO_ROOT" && ( -d "$REPO_ROOT/.git" || -f "$REPO_ROOT/.git" ) ]] \
  || die "repository_unavailable"
TEST_MODE="${AAB_DAILY_ADOPTION_TEST_MODE:-false}"
UNIT_ROOT="/etc/systemd/system"
SYSTEMCTL="/usr/bin/systemctl"
if [[ "$TEST_MODE" == "true" ]]; then
  [[ "${EUID}" -ne 0 ]] || die "test_mode_forbidden_as_root"
  [[ -n "${AAB_DAILY_ADOPTION_TEST_UNIT_ROOT:-}" && -n "${AAB_DAILY_ADOPTION_TEST_SYSTEMCTL:-}" ]] \
    || die "test_mode_paths_required"
  UNIT_ROOT="$AAB_DAILY_ADOPTION_TEST_UNIT_ROOT"
  SYSTEMCTL="$AAB_DAILY_ADOPTION_TEST_SYSTEMCTL"
elif [[ "${EUID}" -ne 0 ]]; then
  die "root_required"
fi

if [[ -z "$RUNTIME_DATA" ]]; then
  RUNTIME_DATA="$REPO_ROOT/ops/quant-monitor/data"
fi
if [[ -z "$RUNTIME_VENV" ]]; then
  RUNTIME_VENV="$REPO_ROOT/ops/quant-monitor/.venv"
fi

for path in "$REPO_ROOT" "$RELEASE_ROOT" "$RUNTIME_DATA" "$RUNTIME_VENV" "$UNIT_ROOT"; do
  [[ "$path" == /* && "$path" != *".."* ]] || die "invalid_path_configuration"
done
[[ -d "$UNIT_ROOT" && ! -L "$UNIT_ROOT" ]] || die "systemd_unit_root_unavailable"
[[ -x "$SYSTEMCTL" ]] || die "systemctl_unavailable"

UNIT="codex-daily-briefing.service"
TIMER="codex-daily-briefing.timer"
DROPIN_DIR="$UNIT_ROOT/codex-daily-briefing.service.d"
DROPIN="$DROPIN_DIR/zzzzzzzzzz-aab-daily-release.conf"
BASE_UNIT="$UNIT_ROOT/$UNIT"
TIMER_UNIT="$UNIT_ROOT/$TIMER"
[[ -f "$BASE_UNIT" && ! -L "$BASE_UNIT" ]] || die "daily_unit_unavailable"
[[ -f "$TIMER_UNIT" && ! -L "$TIMER_UNIT" ]] || die "daily_timer_unavailable"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all -- \
  src scripts ops/quant-monitor/quant_monitor_domain ops/quant-monitor/scripts \
  ops/quant-monitor/systemd ops/quant-monitor/qpk-runtime.sha \
  ops/quant-monitor/requirements-linux-py312.lock)" ]]; then
  die "runtime_source_dirty"
fi
MAIN_SHA="$(git -C "$REPO_ROOT" rev-parse --verify refs/remotes/origin/main 2>/dev/null || true)"
[[ "$MAIN_SHA" == "$SHA" ]] || die "release_sha_not_current_origin_main"
git -C "$REPO_ROOT" cat-file -e "${SHA}^{commit}" 2>/dev/null || die "release_commit_unavailable"

load_properties() {
  local unit_name="$1" output="$2"; shift 2
  local raw line key value properties parsed_count=0 expected_count=$#
  properties="$(IFS=,; printf '%s' "$*")"
  raw="$("$SYSTEMCTL" --no-pager show "$unit_name" "--property=$properties" 2>/dev/null)" \
    || return 1
  (( ${#raw} <= 32768 )) || return 1
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$line" == *=* ]] || continue
    case "$output:$key" in
      service:WorkingDirectory|service:ExecStart|service:ExecStartPre|service:FragmentPath|service:DropInPaths|service:ActiveState|service:SubState|service:UnitFileState|\
      timer:FragmentPath|timer:DropInPaths|timer:Unit|timer:ActiveState|timer:SubState|timer:UnitFileState|\
      health:WorkingDirectory|health:ExecStart|health:ExecStartPre|health:FragmentPath|health:DropInPaths|health:ActiveState|health:SubState|health:UnitFileState)
        printf -v "${output}_${key}" '%s' "$value"
        parsed_count=$((parsed_count + 1)) ;;
      *) return 1 ;;
    esac
  done <<< "$raw"
  [[ "$parsed_count" -eq "$expected_count" ]]
}

read_service() {
  for key in WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState; do unset "service_$key"; done
  load_properties "$UNIT" service WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState \
    || return 1
}

read_timer() {
  for key in FragmentPath DropInPaths Unit ActiveState SubState UnitFileState; do unset "timer_$key"; done
  load_properties "$TIMER" timer FragmentPath DropInPaths Unit ActiveState SubState UnitFileState \
    || return 1
  [[ "$timer_Unit" == "$UNIT" ]]
}

read_health() {
  for key in WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState; do unset "health_$key"; done
  load_properties codex-quant.service health WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState \
    || return 1
}

exec_is_release() {
  local raw="$1" sha="$2" command="${3:-/bin/bash}" script="${4:-daily_briefing_pipeline.sh}"
  local expected_root="${RELEASE_ROOT}/${sha}"
  local expected_argv="${command} ${expected_root}/ops/quant-monitor/scripts/${script}"
  if [[ "$script" == "daily_briefing_pipeline.sh" && "$command" == "/usr/bin/env" ]]; then
    expected_argv="/usr/bin/env QUANT_MONITOR_ROOT=${expected_root}/ops/quant-monitor AIAUDIT_BRIDGE_ROOT=${expected_root} /bin/bash ${expected_root}/ops/quant-monitor/scripts/${script}"
  fi
  [[ "$raw" == "{ path=${command} ; argv[]=${expected_argv} ; "* ]]
}

exec_is_prior_release() {
  local raw="$1" sha="$2"
  exec_is_release "$raw" "$sha" /bin/bash daily_briefing_pipeline.sh \
    || exec_is_release "$raw" "$sha" /usr/bin/env daily_briefing_pipeline.sh
}

owned_dropin_matches_expected_release() {
  local sha="$1" previous_sha="$2" monitor_root
  monitor_root="$RELEASE_ROOT/$sha/ops/quant-monitor"
  local expected
  expected="$(printf '%s\n' \
    '# managed-by: AIAuditBridge daily-release-adoption.v1' \
    "# release-sha: $sha" \
    "# prior-release-sha: $previous_sha" \
    '[Service]' \
    "WorkingDirectory=$monitor_root" \
    'ExecStart=' \
    "ExecStart=/usr/bin/env QUANT_MONITOR_ROOT=$monitor_root AIAUDIT_BRIDGE_ROOT=$RELEASE_ROOT/$sha /bin/bash $monitor_root/scripts/daily_briefing_pipeline.sh")"
  [[ "$(cat -- "$DROPIN" 2>/dev/null)" == "$expected" ]]
}

normalize_dropin_paths() {
  local paths="$1" path
  for path in $paths; do
    [[ "$path" == /* && "$path" != *".."* && "$path" != *$'\n'* ]] || return 1
    printf '%s\n' "$path"
  done | LC_ALL=C sort
}

service_is_idle() {
  [[ "${service_ActiveState}" != "active" \
    && "${service_ActiveState}" != "activating" \
    && "${service_ActiveState}" != "deactivating" \
    && "${service_ActiveState}" != "reloading" ]]
}

read_service || die "daily_service_readback_unavailable"
read_timer || die "daily_timer_readback_unavailable"
read_health || die "health_service_readback_unavailable"
service_is_idle || die "daily_service_active"
[[ "${service_FragmentPath}" == "$BASE_UNIT" ]] || die "daily_unit_source_unexpected"
[[ "${service_WorkingDirectory}" == "$RELEASE_ROOT/$EXPECTED_CURRENT_SHA/ops/quant-monitor" ]] \
  || die "daily_working_directory_unexpected"
exec_is_prior_release "${service_ExecStart}" "$EXPECTED_CURRENT_SHA" \
  || die "daily_execstart_unexpected"
[[ -n "${service_ExecStartPre}" ]] || die "daily_execstartpre_unavailable"
SERVICE_PRE_BEFORE="${service_ExecStartPre}"
DAILY_STATE_BEFORE="${service_UnitFileState}:${service_ActiveState}:${service_SubState}"
TIMER_BEFORE="${timer_FragmentPath}|${timer_DropInPaths}|${timer_Unit}|${timer_UnitFileState}|${timer_ActiveState}|${timer_SubState}"
HEALTH_BEFORE="${health_FragmentPath}|${health_DropInPaths}|${health_WorkingDirectory}|${health_ExecStart}|${health_ExecStartPre}|${health_UnitFileState}|${health_ActiveState}|${health_SubState}"
DROPINS_BEFORE="$service_DropInPaths"

BASELINE_DROPINS=""
OWN_DROPIN_PRESENT=false
for existing_dropin in $service_DropInPaths; do
  [[ -f "$existing_dropin" && ! -L "$existing_dropin" ]] || die "daily_dropin_state_unexpected"
  if [[ "$existing_dropin" == "$DROPIN" ]]; then
    OWN_DROPIN_PRESENT=true
    continue
  fi
  BASELINE_DROPINS="$BASELINE_DROPINS $existing_dropin"
done
if [[ "$OWN_DROPIN_PRESENT" == true ]]; then
  [[ "$(wc -c < "$DROPIN")" -le 4096 ]] || die "daily_dropin_unowned_or_unexpected"
  PRIOR_DROPIN_SHA="$(sed -n 's/^# prior-release-sha: //p' "$DROPIN" 2>/dev/null)"
  [[ "$PRIOR_DROPIN_SHA" =~ ^[0-9a-f]{40}$ ]] \
    && owned_dropin_matches_expected_release "$EXPECTED_CURRENT_SHA" "$PRIOR_DROPIN_SHA" \
    || die "daily_dropin_unowned_or_unexpected"
else
  [[ ! -e "$DROPIN" && ! -L "$DROPIN" ]] || die "daily_dropin_state_unexpected"
fi

OLD_RELEASE="$RELEASE_ROOT/$EXPECTED_CURRENT_SHA/ops/quant-monitor"
[[ -d "$OLD_RELEASE" && ! -L "$OLD_RELEASE" ]] || die "current_release_unavailable"
[[ -L "$OLD_RELEASE/data" && -L "$OLD_RELEASE/.venv" ]] || die "current_release_links_unavailable"
PERSISTENT_DATA="$(readlink -f "$OLD_RELEASE/data" 2>/dev/null || true)"
RUNTIME_PYTHON="$(readlink -f "$OLD_RELEASE/.venv" 2>/dev/null || true)"
[[ -n "$PERSISTENT_DATA" && -n "$RUNTIME_PYTHON" \
  && -d "$PERSISTENT_DATA" && -d "$RUNTIME_PYTHON" ]] || die "current_release_links_unavailable"
[[ "$(readlink -f "$RUNTIME_DATA" 2>/dev/null || true)" == "$PERSISTENT_DATA" \
  && "$(readlink -f "$RUNTIME_VENV" 2>/dev/null || true)" == "$RUNTIME_PYTHON" ]] \
  || die "runtime_data_or_venv_mismatch"

INSTALLER="$REPO_ROOT/ops/quant-monitor/scripts/install_immutable_release.sh"
[[ -x "$INSTALLER" ]] || die "immutable_installer_unavailable"
if ! INSTALL_RESULT="$("$INSTALLER" --sha "$SHA" --repo "$REPO_ROOT" \
    --release-root "$RELEASE_ROOT" --runtime-data "$PERSISTENT_DATA" \
    --runtime-venv "$RUNTIME_PYTHON" 2>/dev/null)"; then
  die "immutable_release_install_failed"
fi
unset INSTALL_RESULT
NEW_ROOT="$RELEASE_ROOT/$SHA"
NEW_MONITOR="$NEW_ROOT/ops/quant-monitor"
[[ -d "$NEW_MONITOR" && -L "$NEW_MONITOR/data" && -L "$NEW_MONITOR/.venv" ]] \
  || die "immutable_release_install_unverified"
[[ "$(readlink -f "$NEW_MONITOR/data" 2>/dev/null || true)" == "$PERSISTENT_DATA" \
  && "$(readlink -f "$NEW_MONITOR/.venv" 2>/dev/null || true)" == "$RUNTIME_PYTHON" ]] \
  || die "immutable_release_link_mismatch"

file_uid() {
  local path="$1"
  stat -c '%u' -- "$path" 2>/dev/null || stat -f '%u' "$path" 2>/dev/null
}

EXPECTED_SOURCE_UID=0
if [[ "$TEST_MODE" == "true" ]]; then
  EXPECTED_SOURCE_UID="$(id -u)"
fi
for source_file in \
  "$NEW_ROOT" \
  "$NEW_MONITOR/scripts/daily_briefing_pipeline.sh" \
  "$NEW_MONITOR/scripts/daily_briefing_builder.py" \
  "$NEW_ROOT/scripts/consume_daily_briefing.py" \
  "$NEW_ROOT/ops/quant-monitor/quant_monitor_domain/runtime_digest.py" \
  "$NEW_ROOT/ops/quant-monitor/quant_monitor_domain/briefing_dispatch.py"; do
  [[ -e "$source_file" && ! -L "$source_file" ]] || die "candidate_source_unavailable"
  [[ "$(file_uid "$source_file")" == "$EXPECTED_SOURCE_UID" ]] || die "candidate_source_owner_unexpected"
done
UNOWNED_SOURCE="$(find "$NEW_ROOT" -xdev ! -uid "$EXPECTED_SOURCE_UID" -print -quit 2>/dev/null)"
[[ -z "$UNOWNED_SOURCE" ]] || die "candidate_source_owner_unexpected"
command -v gcloud >/dev/null 2>&1 || die "gcloud_unavailable"
[[ -x "$RUNTIME_PYTHON/bin/python" ]] || die "runtime_python_unavailable"

# This qualifies only the code paths used by the existing daily runtime and
# domain builder. It imports candidate source and the already-installed QPK;
# it does not read lifecycle data, access cloud services, send, or touch ledger.
if ! "$RUNTIME_PYTHON/bin/python" -I -B - "$NEW_ROOT" <<'PY' >/dev/null 2>&1
import importlib
import inspect
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))

importlib.import_module("scripts.consume_daily_briefing")
importlib.import_module("quant_monitor_domain.runtime_digest")
importlib.import_module("quant_monitor_domain.briefing_dispatch")

from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard
from quant_platform_kit.strategy_lifecycle.performance_monitor import run_monitor
from quant_platform_kit.strategy_lifecycle.strategy_health_score import compute_health_score

def accepts(signature, name):
    return name in signature.parameters or any(
        item.kind == inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )

drift_signature = inspect.signature(run_drift_detection)
dashboard_signature = inspect.signature(build_dashboard)
monitor_signature = inspect.signature(run_monitor)
score_signature = inspect.signature(compute_health_score)
assert "domain" in drift_signature.parameters
assert all(accepts(dashboard_signature, key) for key in ("output_dir", "output_format", "domains"))
assert "domain" in monitor_signature.parameters
assert all(accepts(monitor_signature, key) for key in ("strategy_profile", "live_stream_id", "source_revision"))
assert accepts(score_signature, "drift")
PY
then
  die "daily_runtime_qualification_failed"
fi

# Re-read source and timer state before changing the service drop-in.
read_service || die "daily_service_readback_unavailable"
read_timer || die "daily_timer_readback_unavailable"
read_health || die "health_service_readback_unavailable"
service_is_idle || die "daily_service_active"
[[ "${service_WorkingDirectory}" == "$RELEASE_ROOT/$EXPECTED_CURRENT_SHA/ops/quant-monitor" \
  && "${service_ExecStartPre}" == "$SERVICE_PRE_BEFORE" \
  && "${service_DropInPaths}" == "$DROPINS_BEFORE" \
  && "${service_UnitFileState}:${service_ActiveState}:${service_SubState}" == "$DAILY_STATE_BEFORE" ]] \
  || die "daily_service_changed_during_adoption"
exec_is_prior_release "${service_ExecStart}" "$EXPECTED_CURRENT_SHA" \
  || die "daily_service_changed_during_adoption"
[[ "${timer_FragmentPath}|${timer_DropInPaths}|${timer_Unit}|${timer_UnitFileState}|${timer_ActiveState}|${timer_SubState}" == "$TIMER_BEFORE" \
  && "${health_FragmentPath}|${health_DropInPaths}|${health_WorkingDirectory}|${health_ExecStart}|${health_ExecStartPre}|${health_UnitFileState}|${health_ActiveState}|${health_SubState}" == "$HEALTH_BEFORE" ]] \
  || die "runtime_state_changed_during_adoption"

mkdir -p "$DROPIN_DIR" 2>/dev/null || die "daily_dropin_directory_unavailable"
[[ ! -L "$DROPIN_DIR" && -d "$DROPIN_DIR" ]] || die "daily_dropin_directory_unavailable"
SNAPSHOT="$DROPIN_DIR/zzzzzzzzzz-aab-daily-release.rollback.${EXPECTED_CURRENT_SHA}.${SHA}"
[[ ! -e "$SNAPSHOT" && ! -L "$SNAPSHOT" ]] || die "recovery_snapshot_already_exists"
if [[ -f "$DROPIN" && ! -L "$DROPIN" ]]; then
  cp -p -- "$DROPIN" "$SNAPSHOT" 2>/dev/null || die "recovery_snapshot_failed"
else
  (umask 077; printf 'previous_dropin=absent\n' > "$SNAPSHOT") 2>/dev/null \
    || die "recovery_snapshot_failed"
fi
chmod 0600 "$SNAPSHOT" 2>/dev/null || die "recovery_snapshot_failed"

STAGED="$(mktemp "$DROPIN_DIR/.daily-release.XXXXXX" 2>/dev/null)" \
  || die "daily_dropin_stage_failed"
cleanup_stage() {
  if [[ -n "${STAGED:-}" && -f "$STAGED" ]]; then rm -f -- "$STAGED" 2>/dev/null; fi
  return 0
}
trap cleanup_stage EXIT
if ! (cat <<EOF
# managed-by: AIAuditBridge daily-release-adoption.v1
# release-sha: $SHA
# prior-release-sha: $EXPECTED_CURRENT_SHA
[Service]
WorkingDirectory=$NEW_MONITOR
ExecStart=
ExecStart=/usr/bin/env QUANT_MONITOR_ROOT=$NEW_MONITOR AIAUDIT_BRIDGE_ROOT=$NEW_ROOT /bin/bash $NEW_MONITOR/scripts/daily_briefing_pipeline.sh
EOF
) 2>/dev/null > "$STAGED"; then die "daily_dropin_stage_failed"; fi
chmod 0644 "$STAGED" 2>/dev/null || die "daily_dropin_stage_failed"
mv -f -- "$STAGED" "$DROPIN" 2>/dev/null || die "daily_dropin_write_failed"
STAGED=""

if ! "$SYSTEMCTL" daemon-reload >/dev/null 2>&1; then
  echo "[daily-adoption] status=unknown sha=$SHA recovery_snapshot=preserved action=manual_readback_no_retry" >&2
  exit 3
fi

if ! read_service || ! read_timer || ! read_health || ! service_is_idle \
  || [[ "${service_FragmentPath:-}" != "$BASE_UNIT" \
     || "${service_WorkingDirectory:-}" != "$NEW_MONITOR" \
     || "$(normalize_dropin_paths "$service_DropInPaths" 2>/dev/null || true)" != "$(normalize_dropin_paths "$BASELINE_DROPINS $DROPIN" 2>/dev/null || true)" \
     || "${service_ExecStartPre:-}" == "" \
     || "${service_ExecStartPre:-}" != "$SERVICE_PRE_BEFORE" \
     || "${service_UnitFileState:-}:${service_ActiveState:-}:${service_SubState:-}" != "$DAILY_STATE_BEFORE" ]]; then
  echo "[daily-adoption] status=unknown sha=$SHA recovery_snapshot=preserved action=manual_readback_no_retry" >&2
  exit 3
fi

if ! exec_is_release "${service_ExecStart}" "$SHA" /usr/bin/env daily_briefing_pipeline.sh \
  || [[ "$(normalize_dropin_paths "$service_DropInPaths" 2>/dev/null || true)" != "$(normalize_dropin_paths "$BASELINE_DROPINS $DROPIN" 2>/dev/null || true)" \
     || "${timer_Unit}" != "$UNIT" \
     || "${timer_FragmentPath}" != "$TIMER_UNIT" \
     || "${timer_FragmentPath}|${timer_DropInPaths}|${timer_Unit}|${timer_UnitFileState}|${timer_ActiveState}|${timer_SubState}" != "$TIMER_BEFORE" \
     || "${health_FragmentPath}|${health_DropInPaths}|${health_WorkingDirectory}|${health_ExecStart}|${health_ExecStartPre}|${health_UnitFileState}|${health_ActiveState}|${health_SubState}" != "$HEALTH_BEFORE" ]]; then
  echo "[daily-adoption] status=unknown sha=$SHA recovery_snapshot=preserved action=manual_readback_no_retry" >&2
  exit 3
fi

echo "[daily-adoption] status=prepared sha=$SHA daily_service=release timer=unchanged health=untouched send=not_run recovery_snapshot=preserved"
