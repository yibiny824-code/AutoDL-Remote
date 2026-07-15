#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${AUTODL_REMOTE_TEST_CLI:-$ROOT_DIR/plugins/autodl-remote/bin/autodl-remote}"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/autodl-remote-test.XXXXXX")"
cleanup() {
  if [ "${KEEP_TEST_TMP:-}" = "1" ]; then
    printf 'Kept test directory: %s\n' "$TMP_ROOT" >&2
  else
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT

HOME_DIR="$TMP_ROOT/home"
PROJECT_DIR="$TMP_ROOT/project"
FAKE_BIN="$TMP_ROOT/bin"
FAKE_REMOTE_BIN="$TMP_ROOT/remote-bin"
REMOTE_DIR="$TMP_ROOT/remote"
mkdir -p "$HOME_DIR/.autodl-remote/accounts" "$PROJECT_DIR/.autodl-remote/jobs" "$FAKE_BIN" "$FAKE_REMOTE_BIN" "$REMOTE_DIR"

cat > "$HOME_DIR/.autodl-remote/accounts/test.conf" <<EOF
TARGET="test@example.invalid"
PORT="22"
KEY=""
AUTH="prompt"
DEFAULT_REMOTE="$REMOTE_DIR"
EOF

cat > "$PROJECT_DIR/.autodl-remote.conf" <<EOF
ACCOUNT="test"
REMOTE_ROOT="$REMOTE_DIR"
EOF

cat > "$FAKE_BIN/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ -n "${FAKE_SSH_ARGS_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_SSH_ARGS_LOG"
fi
if [ -n "${FAKE_SSH_OUTPUT_FILE:-}" ]; then
  exec /bin/cat "$FAKE_SSH_OUTPUT_FILE"
fi
if [ -n "${FAKE_SSH_STATE_DIR:-}" ]; then
  state_dir="${FAKE_SSH_STATE_DIR:?}"
  mkdir -p "$state_dir"
  control_master="auto"
  operation=""
  previous=""
  master_start=false
  for argument in "$@"; do
    if [ "$previous" = "-O" ]; then
      operation="$argument"
    elif [ "$previous" = "-o" ]; then
      case "$argument" in
        ControlMaster=*) control_master="${argument#ControlMaster=}" ;;
      esac
    fi
    case "$argument" in
      -*M*N*f*|-*M*f*N*|-*N*M*f*|-*N*f*M*|-*f*M*N*|-*f*N*M*) master_start=true ;;
    esac
    previous="$argument"
  done

  if [ "$operation" = "check" ]; then
    [ -f "$state_dir/master" ] && exit 0
    exit 255
  fi

  create_master() {
    if [ "${FAKE_SSH_FAIL_MASTER_ONCE:-}" = "1" ] && [ ! -f "$state_dir/failed-once" ]; then
      : > "$state_dir/failed-once"
      return 255
    fi
    if mkdir "$state_dir/creating" 2>/dev/null; then
      if [ "${FAKE_SSH_CONTROL_RACE:-}" = "1" ]; then
        sleep 0.2
      fi
      : > "$state_dir/master"
      rmdir "$state_dir/creating"
      return 0
    fi
    sleep 5
    return 255
  }

  if [ "$master_start" = "true" ] || [ "$control_master" = "yes" ]; then
    if [ "${FAKE_SSH_HANG_MASTER:-}" = "1" ]; then
      exec sleep 5
    fi
    create_master
    exit $?
  fi
  if [ "$control_master" = "auto" ] && [ ! -f "$state_dir/master" ]; then
    create_master || exit $?
  fi
fi
if [ "${FAKE_SSH_HANG_SESSION:-}" = "1" ]; then
  exec sleep 5
fi
if [ "${FAKE_SSH_HANG_SESSION:-}" = "child" ]; then
  (
    sleep 5 &
    grandchild=$!
    while [ ! -f "${FAKE_SSH_CHILD_PID_FILE:?}" ]; do sleep 0.01; done
    printf '%s\n' "$grandchild" >> "${FAKE_SSH_CHILD_PID_FILE:?}"
    wait "$grandchild"
  ) &
  child=$!
  printf '%s\n' "$child" > "${FAKE_SSH_CHILD_PID_FILE:?}"
  wait "$child"
  exit $?
fi
command="${!#}"
if [ -n "${FAKE_SSH_REMOTE_BIN:-}" ]; then
  PATH="$FAKE_SSH_REMOTE_BIN:$PATH"
  export PATH
fi
exec bash -c "$command"
EOF
chmod +x "$FAKE_BIN/ssh"

cat > "$FAKE_BIN/scp" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
source_path="${@: -2:1}"
target_path="${!#}"
target_path="${target_path#*:}"
mkdir -p "$(dirname "$target_path")"
cp "$source_path" "$target_path"
EOF
chmod +x "$FAKE_BIN/scp"

cat > "$FAKE_REMOTE_BIN/setsid" <<'EOF'
#!/usr/bin/env python3
import os
import sys

os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
EOF
chmod +x "$FAKE_REMOTE_BIN/setsid"

cat > "$FAKE_BIN/security" <<'EOF'
#!/usr/bin/env bash
if [ "${FAKE_SECURITY_HANG:-}" = "1" ]; then
  exec sleep 5
fi
printf 'test-password\n'
EOF
chmod +x "$FAKE_BIN/security"

cat > "$FAKE_BIN/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" has-session "*) exit 0 ;;
  *" capture-pane "*) exec /bin/cat "${FAKE_TMUX_OUTPUT_FILE:?}" ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$FAKE_BIN/tmux"

write_job() {
  local name="$1" log="$2" backend="${3:-detach}"
  cat > "$PROJECT_DIR/.autodl-remote/jobs/$name.conf" <<EOF
NAME="$name"
PID="1234"
ACCOUNT="test"
REMOTE_ROOT="$REMOTE_DIR"
LOG_PATH="$log"
STATUS_PATH="$REMOTE_DIR/$name.status"
CWD="$REMOTE_DIR"
STARTED_AT="2026-07-13T00:00:00-0700"
COMMAND="python train.py"
BACKEND="$backend"
TMUX_SESSION="${name}-session"
TMUX_SOCKET="$REMOTE_DIR/tmux.sock"
EOF
}

run_cli() {
  (
    cd "$PROJECT_DIR"
    HOME="$HOME_DIR" PATH="$FAKE_BIN:$PATH" AUTODL_REMOTE_CONTROL_DIR="$TMP_ROOT/control" FAKE_SSH_REMOTE_BIN="$FAKE_REMOTE_BIN" FAKE_SSH_STATE_DIR="${FAKE_SSH_STATE_DIR:-$TMP_ROOT/default-ssh-state}" "$CLI" "$@"
  )
}

assert_eq() {
  local expected="$1" actual="$2" label="$3"
  if [ "$actual" != "$expected" ]; then
    printf 'FAIL: %s\nexpected:\n%s\nactual:\n%s\n' "$label" "$expected" "$actual" >&2
    return 1
  fi
}

assert_le() {
  local actual="$1" limit="$2" label="$3"
  if [ "$actual" -gt "$limit" ]; then
    printf 'FAIL: %s (actual=%s limit=%s)\n' "$label" "$actual" "$limit" >&2
    return 1
  fi
}

assert_contains() {
  local needle="$1" haystack="$2" label="$3"
  if ! printf '%s' "$haystack" | grep -Fq -- "$needle"; then
    printf 'FAIL: %s\nmissing: %s\nactual:\n%s\n' "$label" "$needle" "$haystack" >&2
    return 1
  fi
}

wait_for_file_content() {
  local file="$1" label="$2"
  for _ in $(seq 1 100); do
    [ -s "$file" ] && return 0
    sleep 0.05
  done
  printf 'FAIL: timed out waiting for %s (%s)\n' "$label" "$file" >&2
  return 1
}

wait_for_matching_file_content() {
  local file="$1" pattern="$2" label="$3"
  for _ in $(seq 1 100); do
    grep -Fq -- "$pattern" "$file" 2>/dev/null && return 0
    sleep 0.05
  done
  printf 'FAIL: timed out waiting for %s (%s)\n' "$label" "$file" >&2
  return 1
}

test_job_tail_counts_logical_lines() {
  local log="$REMOTE_DIR/tqdm.log" output
  printf '\033[31mepoch 1\033[0m\repoch 2\r\nepoch 3\n\033[32mepoch 4\033[0m\repoch 5' > "$log"
  write_job train "$log"

  output="$(run_cli job tail --lines 3 train)"
  assert_eq $'epoch 3\nepoch 4\nepoch 5' "$output" "job tail selects the last N normalized logical lines"
}

test_job_tail_caps_long_output() {
  local log="$REMOTE_DIR/long.log" output bytes
  awk 'BEGIN { printf "\033[35m"; for (i = 0; i < 70000; i++) printf "x"; printf "END\033[0m" }' > "$log"
  write_job long "$log"

  output="$(run_cli job tail --lines 1 long)"
  bytes="$(LC_ALL=C printf '%s' "$output" | wc -c | tr -d ' ')"
  assert_le "$bytes" 65536 "job tail output has a hard byte ceiling"
  assert_eq "END" "${output: -3}" "job tail keeps the newest bytes"
  if printf '%s' "$output" | LC_ALL=C grep -q $'\033'; then
    printf 'FAIL: job tail strips ANSI control sequences\n' >&2
    return 1
  fi
}

test_tmux_job_tail_uses_the_same_output_policy() {
  local fixture="$REMOTE_DIR/tmux-pane.log" output bytes
  awk 'BEGIN { printf "\033[36m"; for (i = 0; i < 70000; i++) printf "y"; printf "END\033[0m" }' > "$fixture"
  write_job tmux-run "$REMOTE_DIR/tmux-run.log" tmux

  output="$(FAKE_TMUX_OUTPUT_FILE="$fixture" run_cli job tail --lines 1 tmux-run)"
  bytes="$(LC_ALL=C printf '%s' "$output" | wc -c | tr -d ' ')"
  assert_le "$bytes" 65536 "tmux job tail output has the same byte ceiling"
  assert_eq "END" "${output: -3}" "tmux job tail keeps the newest bytes"
  if printf '%s' "$output" | LC_ALL=C grep -q $'\033'; then
    printf 'FAIL: tmux job tail strips ANSI control sequences\n' >&2
    return 1
  fi
}

test_invalid_tail_limit_fails_in_the_parent_process() {
  local output rc
  set +e
  output="$(AUTODL_REMOTE_TAIL_MAX_BYTES=invalid run_cli job tail --lines 3 train 2>&1)"
  rc=$?
  set -e
  assert_eq "2" "$rc" "invalid tail byte limit is rejected"
  if ! printf '%s' "$output" | grep -q 'AUTODL_REMOTE_TAIL_MAX_BYTES must be a positive integer'; then
    printf 'FAIL: invalid tail byte limit reports the expected diagnostic\n%s\n' "$output" >&2
    return 1
  fi
}

test_job_tail_accepts_options_after_name() {
  local log="$REMOTE_DIR/tail-position.log" output rc
  printf 'one\ntwo\nthree\n' > "$log"
  write_job tail-position "$log"

  output="$(run_cli job tail tail-position --lines 2)"
  assert_eq $'two\nthree' "$output" "job tail accepts --lines after the job name"

  set +e
  output="$(run_cli job tail tail-position --unexpected 2>&1)"
  rc=$?
  set -e
  assert_eq "2" "$rc" "job tail rejects unknown trailing arguments"
  assert_contains 'unknown job tail argument: --unexpected' "$output" "job tail explains an unknown trailing argument"
}

test_detached_direct_argv_preserves_shell_metacharacters() {
  local name="quoted-argv" log output
  log="$REMOTE_DIR/.autodl-remote/logs/$name.log"
  output="$(run_cli exec --detach --name "$name" -- printf '%s\n' '<|endoftext|>')"
  wait_for_file_content "$log" "quoted detached command output"
  assert_eq '<|endoftext|>' "$(tr -d '\n' < "$log")" "detached direct argv preserves literal shell metacharacters"
  assert_contains 'exit_code=0' "$(run_cli job status "$name")" "quoted detached command exits successfully"
}

test_detached_stdin_preserves_literal_shell_metacharacters() {
  local name="quoted-stdin" log output
  log="$REMOTE_DIR/.autodl-remote/logs/$name.log"
  output="$(printf "%s\\n" "printf '%s\\n' '<|endoftext|>'" | run_cli exec --detach --stdin --name "$name" -- bash)"
  wait_for_file_content "$log" "quoted stdin detached command output"
  assert_eq '<|endoftext|>' "$(tr -d '\n' < "$log")" "detached stdin preserves literal shell metacharacters"
  assert_contains 'exit_code=0' "$(run_cli job status "$name")" "quoted stdin detached command exits successfully"
}

test_job_stop_terminates_the_whole_detached_process_group() {
  local name="stop-group" log child_pid output
  log="$REMOTE_DIR/.autodl-remote/logs/$name.log"
  output="$(run_cli exec --detach --name "$name" -- bash -c 'sleep 30 & child=$!; echo "$child"; wait "$child"')"
  wait_for_file_content "$log" "detached child pid"
  child_pid="$(head -n 1 "$log")"
  case "$child_pid" in
    ''|*[!0-9]*) printf 'FAIL: detached child pid is invalid: %s\n%s\n' "$child_pid" "$output" >&2; return 1 ;;
  esac

  output="$(run_cli job stop "$name")"
  assert_contains 'status=cancelled' "$output" "job stop reports cancellation"
  for _ in $(seq 1 40); do
    kill -0 "$child_pid" 2>/dev/null || break
    sleep 0.05
  done
  if kill -0 "$child_pid" 2>/dev/null; then
    printf 'FAIL: job stop left the detached child running (pid=%s)\n' "$child_pid" >&2
    return 1
  fi
  assert_contains 'status=cancelled' "$(run_cli job status "$name")" "runner records cancelled after job stop"
}

test_job_stop_preserves_completed_job_status() {
  local name="already-exited" status output
  status="$REMOTE_DIR/.autodl-remote/jobs/$name.status"
  run_cli exec --detach --name "$name" -- printf 'done\n' >/dev/null
  wait_for_matching_file_content "$status" 'status=exited' "completed detached job status"

  output="$(run_cli job stop "$name")"
  assert_contains 'status=exited' "$output" "stopping a completed job is a non-mutating no-op"
  output="$(run_cli job status "$name")"
  assert_contains 'status=exited' "$output" "completed job remains exited after stop"
  assert_contains 'exit_code=0' "$output" "completed job preserves its exit code after stop"
}

test_legacy_detached_job_stop_detects_its_process_group() {
  local name="legacy-stop" file output
  file="$PROJECT_DIR/.autodl-remote/jobs/$name.conf"
  run_cli exec --detach --name "$name" -- sleep 30 >/dev/null
  sed '/^PROCESS_GROUP=/d' "$file" > "$file.tmp"
  mv "$file.tmp" "$file"

  output="$(run_cli job stop "$name")"
  assert_contains 'status=cancelled' "$output" "legacy detach job is stopped after PGID detection"
}

test_active_name_collision_requires_replace_and_stops_the_old_group() {
  local name="collision" file first_pid second_pid output rc
  file="$PROJECT_DIR/.autodl-remote/jobs/$name.conf"
  run_cli exec --detach --name "$name" -- sleep 30 >/dev/null
  first_pid="$(awk -F= '$1=="PID" { gsub(/"/, "", $2); print $2; exit }' "$file")"

  set +e
  output="$(run_cli exec --detach --name "$name" -- sleep 30 2>&1)"
  rc=$?
  set -e
  assert_eq "2" "$rc" "active job name collision is rejected"
  assert_contains 'already active' "$output" "active job name collision explains the rejection"
  assert_eq "$first_pid" "$(awk -F= '$1=="PID" { gsub(/"/, "", $2); print $2; exit }' "$file")" "collision does not overwrite the existing metadata"

  run_cli exec --detach --replace --name "$name" -- sleep 30 >/dev/null
  second_pid="$(awk -F= '$1=="PID" { gsub(/"/, "", $2); print $2; exit }' "$file")"
  if [ "$first_pid" = "$second_pid" ]; then
    printf 'FAIL: --replace did not record a new runner pid\n' >&2
    return 1
  fi
  for _ in $(seq 1 40); do
    kill -0 -- "-$first_pid" 2>/dev/null || break
    sleep 0.05
  done
  if kill -0 -- "-$first_pid" 2>/dev/null; then
    printf 'FAIL: --replace left the old process group running (pgid=%s)\n' "$first_pid" >&2
    return 1
  fi
  run_cli job stop "$name" >/dev/null
}

test_running_status_with_dead_runner_is_lost() {
  local name="lost-runner" file status output
  file="$PROJECT_DIR/.autodl-remote/jobs/$name.conf"
  status="$REMOTE_DIR/$name.status"
  write_job "$name" "$REMOTE_DIR/lost.log"
  sed 's/^PID="1234"/PID="999999"/' "$file" > "$file.tmp"
  mv "$file.tmp" "$file"
  printf 'status=running\npid=999999\n' > "$status"

  output="$(run_cli job status "$name")"
  assert_contains 'status=lost' "$output" "job status marks a dead runner as lost"
  assert_contains 'hint=runner pid is not alive' "$output" "job status includes a recovery hint for a lost job"
  assert_contains 'lost' "$(run_cli run list)" "run list uses the same lost-state normalization"
  assert_contains 'status=lost' "$(run_cli run status "$name")" "run status uses the same lost-state normalization"
}

test_captured_ssh_output_preserves_carriage_returns() {
  local fixture="$TMP_ROOT/ssh-output.bin" output
  printf 'alpha\rbeta\n' > "$fixture"
  output="$(FAKE_SSH_OUTPUT_FILE="$fixture" run_cli account test test)"
  assert_eq $'Testing test -> test@example.invalid:22\nalpha\rbeta' "$output" "generic captured SSH output round-trips carriage returns"
}

test_parallel_cold_start_finishes_before_deadline() {
  local state_dir="$TMP_ROOT/ssh-state" output_dir="$TMP_ROOT/concurrent-output" pid alive rc
  local pids=()
  mkdir -p "$state_dir" "$output_dir"
  rm -f "$state_dir/master"

  for index in 1 2 3 4 5 6 7 8; do
    (
      FAKE_SSH_CONTROL_RACE=1 FAKE_SSH_STATE_DIR="$state_dir" run_cli account test test
    ) > "$output_dir/$index" 2>&1 &
    pids+=("$!")
  done

  for _ in $(seq 1 60); do
    if [ -n "$(jobs -pr)" ]; then alive=true; else alive=false; fi
    [ "$alive" = "true" ] || break
    sleep 0.05
  done

  if [ "$alive" = "true" ]; then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
    printf 'FAIL: parallel cold-start calls exceeded the three-second deadline\n' >&2
    return 1
  fi

  rc=0
  for pid in "${pids[@]}"; do
    wait "$pid" || rc=$?
  done
  assert_eq "0" "$rc" "parallel cold-start calls all succeed"
}

test_monitor_call_has_total_timeout() {
  local account_file="$HOME_DIR/.autodl-remote/accounts/test.conf" child_pid_file="$TMP_ROOT/ssh-child.pid" child_pid output rc
  sed 's/^AUTH=.*/AUTH="ssh-key"/; s#^KEY=.*#KEY="/tmp/fake-key"#' "$account_file" > "$account_file.tmp"
  mv "$account_file.tmp" "$account_file"

  set +e
  output="$(FAKE_SSH_HANG_SESSION=child FAKE_SSH_CHILD_PID_FILE="$child_pid_file" AUTODL_REMOTE_MONITOR_TIMEOUT=1 run_cli account test test 2>&1)"
  rc=$?
  set -e

  sed 's/^AUTH=.*/AUTH="prompt"/; s#^KEY=.*#KEY=""#' "$account_file" > "$account_file.tmp"
  mv "$account_file.tmp" "$account_file"
  assert_eq "124" "$rc" "read-only monitoring call returns the timeout status"
  if ! printf '%s' "$output" | grep -q 'transport timed out after 1s'; then
    printf 'FAIL: monitor timeout reports a clear diagnostic\n%s\n' "$output" >&2
    return 1
  fi
  while IFS= read -r child_pid; do
    if kill -0 "$child_pid" 2>/dev/null; then
      printf 'FAIL: monitor timeout left a descendant process running (pid=%s)\n' "$child_pid" >&2
      return 1
    fi
  done < "$child_pid_file"
}

test_keychain_master_start_times_out() {
  local account_file="$HOME_DIR/.autodl-remote/accounts/test.conf" state_dir="$TMP_ROOT/keychain-state" output rc
  mkdir -p "$state_dir"
  sed 's/^AUTH=.*/AUTH="keychain"/' "$account_file" > "$account_file.tmp"
  mv "$account_file.tmp" "$account_file"

  set +e
  output="$(FAKE_SSH_HANG_MASTER=1 FAKE_SSH_STATE_DIR="$state_dir" AUTODL_REMOTE_CONNECT_TIMEOUT=1 run_cli account test test 2>&1)"
  rc=$?
  set -e

  sed 's/^AUTH=.*/AUTH="prompt"/' "$account_file" > "$account_file.tmp"
  mv "$account_file.tmp" "$account_file"
  assert_eq "124" "$rc" "keychain master startup returns the timeout status"
  if ! printf '%s' "$output" | grep -q 'SSH transport timed out'; then
    printf 'FAIL: keychain timeout reports a clear diagnostic\n%s\n' "$output" >&2
    return 1
  fi
}

test_keychain_lookup_is_inside_the_timeout() {
  local account_file="$HOME_DIR/.autodl-remote/accounts/test.conf" state_dir="$TMP_ROOT/keychain-lookup-state" output rc
  mkdir -p "$state_dir"
  sed 's/^AUTH=.*/AUTH="keychain"/' "$account_file" > "$account_file.tmp"
  mv "$account_file.tmp" "$account_file"

  set +e
  output="$(FAKE_SECURITY_HANG=1 FAKE_SSH_STATE_DIR="$state_dir" AUTODL_REMOTE_CONNECT_TIMEOUT=1 run_cli account test test 2>&1)"
  rc=$?
  set -e

  sed 's/^AUTH=.*/AUTH="prompt"/' "$account_file" > "$account_file.tmp"
  mv "$account_file.tmp" "$account_file"
  assert_eq "124" "$rc" "keychain lookup returns the timeout status"
  if ! printf '%s' "$output" | grep -q 'transport timed out after 1s'; then
    printf 'FAIL: keychain lookup timeout reports a clear diagnostic\n%s\n' "$output" >&2
    return 1
  fi
}

test_connection_options_and_master_retry() {
  local state_dir="$TMP_ROOT/retry-state" args_log="$TMP_ROOT/ssh-args.log" output master_line
  mkdir -p "$state_dir"
  output="$(FAKE_SSH_FAIL_MASTER_ONCE=1 FAKE_SSH_STATE_DIR="$state_dir" FAKE_SSH_ARGS_LOG="$args_log" AUTODL_REMOTE_CONNECT_TIMEOUT=7 AUTODL_REMOTE_CONNECTION_ATTEMPTS=4 run_cli account test test)"
  if ! printf '%s' "$output" | grep -q '^host='; then
    printf 'FAIL: command did not recover after the first master start failure\n' >&2
    return 1
  fi
  if ! grep -q 'ConnectTimeout=7' "$args_log" || ! grep -q 'ConnectionAttempts=4' "$args_log"; then
    printf 'FAIL: explicit SSH connection bounds were not forwarded\n' >&2
    return 1
  fi
  master_line="$(grep -- '-MNf' "$args_log" | tail -n 1)"
  if ! printf '%s' "$master_line" | grep -q 'ControlMaster=yes' || printf '%s' "$master_line" | grep -q 'ControlMaster=no'; then
    printf 'FAIL: master startup must contain only ControlMaster=yes\n%s\n' "$master_line" >&2
    return 1
  fi
}

test_stale_control_state_is_recovered() {
  local state_dir account_id connection_id control_path output kind
  mkdir -p "$TMP_ROOT/control"
  account_id="$(printf '%s\n' 'test' | cksum | awk '{print $1}')"
  connection_id="$(printf '%s\n' 'test@example.invalid:22' | cksum | awk '{print $1}')"
  control_path="$TMP_ROOT/control/ssh-$account_id-$connection_id"
  for kind in dead-pid missing-pid malformed-pid; do
    state_dir="$TMP_ROOT/stale-$kind"
    mkdir -p "$state_dir"
    : > "$control_path"
    mkdir "$control_path.lock"
    case "$kind" in
      dead-pid) printf '999999\n' > "$control_path.lock/pid" ;;
      malformed-pid) printf 'not-a-pid\n' > "$control_path.lock/pid" ;;
    esac

    if [ "$kind" = "dead-pid" ]; then
      output="$(FAKE_SSH_STATE_DIR="$state_dir" run_cli account test test)"
    else
      output="$(FAKE_SSH_STATE_DIR="$state_dir" AUTODL_REMOTE_MASTER_LOCK_TIMEOUT=1 run_cli account test test)"
    fi
    if ! printf '%s' "$output" | grep -q '^host='; then
      printf 'FAIL: stale control state was not recovered (%s)\n' "$kind" >&2
      return 1
    fi
    if [ -e "$control_path" ] || [ -e "$control_path.lock" ]; then
      printf 'FAIL: stale socket or lock remains after recovery (%s)\n' "$kind" >&2
      return 1
    fi
  done
}

run_test() {
  local name="$1"
  [ -z "${AUTODL_REMOTE_TEST_FILTER:-}" ] || [ "$AUTODL_REMOTE_TEST_FILTER" = "$name" ] || return 0
  "$name"
  printf 'PASS: %s\n' "$name"
}

run_test test_job_tail_counts_logical_lines
run_test test_job_tail_caps_long_output
run_test test_tmux_job_tail_uses_the_same_output_policy
run_test test_invalid_tail_limit_fails_in_the_parent_process
run_test test_job_tail_accepts_options_after_name
run_test test_detached_direct_argv_preserves_shell_metacharacters
run_test test_detached_stdin_preserves_literal_shell_metacharacters
run_test test_job_stop_terminates_the_whole_detached_process_group
run_test test_job_stop_preserves_completed_job_status
run_test test_legacy_detached_job_stop_detects_its_process_group
run_test test_active_name_collision_requires_replace_and_stops_the_old_group
run_test test_running_status_with_dead_runner_is_lost
run_test test_captured_ssh_output_preserves_carriage_returns
run_test test_parallel_cold_start_finishes_before_deadline
run_test test_monitor_call_has_total_timeout
run_test test_keychain_master_start_times_out
run_test test_keychain_lookup_is_inside_the_timeout
run_test test_connection_options_and_master_retry
run_test test_stale_control_state_is_recovered
