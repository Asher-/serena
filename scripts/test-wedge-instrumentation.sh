#!/bin/bash
# Smoke test: verify the serena daemon registers faulthandler on SIGUSR1
# and writes a 'Current thread' all-thread traceback to its operator log
# within a small window after the signal.
#
# Strategy: spawn a throwaway daemon under a private HOME so the operator
# log lands in a tmp directory (and never touches ~/Library/Logs/serena).
# Send SIGUSR1, tail-grep the log, clean up. Exit 0 on success, non-zero
# with diagnostic tail on failure.
#
# Optional environment overrides:
#   SERENA_BIN              path to serena CLI (default: project venv)
#   PORT                    listening port (default: 19102 -- non-default
#                           to avoid conflict with the live launchd-managed
#                           daemon on 9102)
#   STARTUP_WAIT_S          max seconds to wait for the log to appear (default 15)
#   SIGUSR1_DUMP_WAIT_S     max seconds to wait for 'Current thread' (default 5)

set -euo pipefail

SERENA_BIN="${SERENA_BIN:-/Users/asher/Dropbox/Projects/claude/serena/.venv/bin/serena}"
PORT="${PORT:-19102}"
STARTUP_WAIT_S="${STARTUP_WAIT_S:-15}"
SIGUSR1_DUMP_WAIT_S="${SIGUSR1_DUMP_WAIT_S:-5}"

if [ ! -x "${SERENA_BIN}" ] ; then
  echo "FAIL: serena binary not executable: ${SERENA_BIN}"
  exit 1
fi

TMPHOME="$(mktemp -d -t serena-wedge-smoke)"
LOG_DIR="${TMPHOME}/Library/Logs/serena"
mkdir -p "${LOG_DIR}"
SERENA_LOG="${LOG_DIR}/serena.log"
DAEMON_PID=""

cleanup() {
  if [ -n "${DAEMON_PID}" ] ; then
    kill -TERM "${DAEMON_PID}" 2>/dev/null || true
    sleep 1
    kill -KILL "${DAEMON_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMPHOME}"
}
trap cleanup EXIT

echo "Starting throwaway serena daemon: port=${PORT} HOME=${TMPHOME}"
# Mirror the launchd plist's stdout/stderr capture: redirect the daemon's
# stderr into serena.log so faulthandler.register(file=sys.stderr) dumps
# end up in the same file the launchd-managed daemon writes to. stdout is
# silenced (the protocol does not use it for streamable-http transport).
HOME="${TMPHOME}" "${SERENA_BIN}" \
  start-mcp-server \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port "${PORT}" \
  >/dev/null 2>>"${SERENA_LOG}" &
DAEMON_PID=$!
echo "Daemon PID=${DAEMON_PID}; waiting up to ${STARTUP_WAIT_S}s for operator log"

start_deadline=$(( $(date +%s) + STARTUP_WAIT_S ))
while [ "$(date +%s)" -lt "${start_deadline}" ] ; do
  if [ -f "${SERENA_LOG}" ] && grep -q "Initializing Serena MCP server" "${SERENA_LOG}" 2>/dev/null ; then
    break
  fi
  if ! kill -0 "${DAEMON_PID}" 2>/dev/null ; then
    echo "FAIL: daemon PID=${DAEMON_PID} exited before logging started"
    exit 1
  fi
  sleep 0.5
done

if [ ! -f "${SERENA_LOG}" ] ; then
  echo "FAIL: operator log never appeared at ${SERENA_LOG}"
  exit 1
fi

log_bytes_before="$(stat -f %z "${SERENA_LOG}")"
echo "Operator log: ${SERENA_LOG} (size before signal: ${log_bytes_before} bytes)"
echo "Sending SIGUSR1 to PID ${DAEMON_PID}"
kill -USR1 "${DAEMON_PID}"

echo "Waiting up to ${SIGUSR1_DUMP_WAIT_S}s for 'Current thread' marker"
dump_deadline=$(( $(date +%s) + SIGUSR1_DUMP_WAIT_S ))
while [ "$(date +%s)" -lt "${dump_deadline}" ] ; do
  log_bytes_now="$(stat -f %z "${SERENA_LOG}")"
  new_bytes=$(( log_bytes_now - log_bytes_before ))
  if [ "${new_bytes}" -gt 0 ] ; then
    if tail -c "${new_bytes}" "${SERENA_LOG}" 2>/dev/null | grep -q 'Current thread' ; then
      echo "PASS: 'Current thread' marker observed in ${SERENA_LOG} after SIGUSR1"
      exit 0
    fi
  fi
  sleep 0.5
done

echo "FAIL: 'Current thread' marker did not appear within ${SIGUSR1_DUMP_WAIT_S}s"
echo "--- last 80 lines of ${SERENA_LOG} ---"
tail -n 80 "${SERENA_LOG}" || true
echo "--- end ---"
exit 1
