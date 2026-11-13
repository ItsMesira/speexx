#!/usr/bin/env bash
# multi_run.sh — Supervisor for N simultaneous Speexx AutoCycle sessions
# (one per account). Each session gets its own device fingerprint profile so
# Speexx sees distinct devices despite the shared IP.
# Usage: ./multi_run.sh <runtime_seconds> [accounts_file]
# Accounts file: one "email password" pair per line (# comments allowed)
set -uo pipefail

RUNTIME="${1:-12600}"
ACCOUNTS="${2:-Accounts.md}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python"
LOGDIR="$HOME/speexx_autocycle_multi"
MASTER_LOG="$HOME/speexx_autocycle_multi_run.log"
STAGGER=15          # seconds between session launches (avoid simultaneous logins)
CYCLE_CAP=1800      # max seconds per speexx_autocycle.py cycle
REST_OK=15          # pause after a clean cycle
REST_ERR=30         # pause after a failed cycle

mkdir -p "$LOGDIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$MASTER_LOG"; }

# ---- parse accounts --------------------------------------------------------
# Accepts two formats:
#   "email password" on one line, or
#   "email" line followed by "password" on the next line
declare -a EMAILS=()
declare -a PASSWORDS=()
while IFS= read -r line; do
    case "$line" in ''|\#*) continue;; esac
    set -- $line
    if [ "$#" -ge 2 ]; then
        EMAILS+=("$1")
        PASSWORDS+=("$2")
    else
        email="$line"
        if ! IFS= read -r pwline; then break; fi
        EMAILS+=("$email")
        PASSWORDS+=("$pwline")
    fi
done < "$ACCOUNTS"

N=${#EMAILS[@]}
if [ "$N" -eq 0 ]; then
    log "ERROR: no accounts found in $ACCOUNTS"
    exit 1
fi
log "Found $N account(s) in $ACCOUNTS. Runtime ${RUNTIME}s, stagger ${STAGGER}s."

# ---- per-session supervisor -----------------------------------------------
run_session() {
    local email="$1" password="$2" name="$3" logfile="$4" profile="$5"
    (
        elapsed=0
        while [ "$elapsed" -lt "$RUNTIME" ]; do
            remaining=$((RUNTIME - elapsed))
            cycle_runtime=$(( remaining < CYCLE_CAP ? remaining : CYCLE_CAP ))
            echo "$(date '+%Y-%m-%d %H:%M:%S') starting cycle ${cycle_runtime}s (${elapsed}s elapsed)" >> "$logfile"
            "$PY" "$SCRIPT_DIR/speexx_autocycle.py" "$email" "$password" \
                --runtime "$cycle_runtime" --device-profile "$profile" \
                > "${logfile}.console" 2>&1
            status=$?
            elapsed=$((elapsed + cycle_runtime))
            [ "$elapsed" -ge "$RUNTIME" ] && break
            if [ "$status" -ne 0 ]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') cycle exited $status, resting ${REST_ERR}s" >> "$logfile"
                sleep "$REST_ERR"
            else
                echo "$(date '+%Y-%m-%d %H:%M:%S') cycle ok, resting ${REST_OK}s" >> "$logfile"
                sleep "$REST_OK"
            fi
        done
        echo "$(date '+%Y-%m-%d %H:%M:%S') session finished" >> "$logfile"
    ) &
}

# ---- launch all sessions, staggered, one device profile each -----------------
declare -a PIDS=()
for ((i = 0; i < N; i++)); do
    name="session$((i + 1))_${EMAILS[$i]%%@*}"
    logfile="$LOGDIR/${name}.log"
    profile=$((i + 1))   # profile 1..N (each session = distinct device fingerprint)
    log "Launching session $((i + 1))/$N: ${EMAILS[$i]} (profile $profile, log: $logfile)"
    run_session "${EMAILS[$i]}" "${PASSWORDS[$i]}" "$name" "$logfile" "$profile"
    PIDS+=($!)
    if [ $((i + 1)) -lt "$N" ]; then
        sleep "$STAGGER"
    fi
done

log "All $N sessions running (pids: ${PIDS[*]})"

# ---- shutdown handler -------------------------------------------------------
stop_all() {
    log "Shutting down all sessions..."
    for p in "${PIDS[@]}"; do
        pkill -TERM -P "$p" 2>/dev/null || true
        kill "$p" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    log "All sessions stopped"
}
trap stop_all INT TERM

wait
log "All sessions finished (${RUNTIME}s window complete)"
