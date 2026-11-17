#!/usr/bin/env bash
# Runs inside the public racing-board repo (GitHub Actions). Polls every
# INTERVAL seconds for DURATION seconds and pushes schedule.json when the
# schedule changes, or at least every HEARTBEAT seconds so "Updated" stays fresh.
# Each pass pulls first, so code synced from the private repo is picked up.
set -u
DURATION=${DURATION:-20400}   # 5 h 40 min; job timeout is 6 h
INTERVAL=${INTERVAL:-300}
HEARTBEAT=${HEARTBEAT:-1800}
PY=${PY:-python3}

end=$((SECONDS + DURATION))
last_push=$SECONDS

changed() {
  # True if series ids or start times differ from the committed schedule.json.
  git show HEAD:schedule.json > .prev.json 2>/dev/null || return 0
  "$PY" - <<'EOF'
import json, sys
def key(p):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    return sorted((s.get("id"), tuple(s.get("starts", []))) for s in d.get("series", []))
sys.exit(0 if key(".prev.json") != key("schedule.json") else 1)
EOF
}

push() {
  git add schedule.json state.json
  git diff --cached --quiet && return 0
  git commit -q -m "Update schedule $(date -u +%Y-%m-%dT%H:%MZ)"
  for i in 1 2 3; do
    git push -q && return 0
    git pull -q --rebase || git rebase --abort
  done
  echo "push failed"
}

while [ $SECONDS -lt $end ]; do
  git pull -q --rebase --autostash || echo "pull failed"
  "$PY" poller/poll.py --out . || echo "poll failed"
  if changed; then
    echo "$(date -u +%H:%M) schedule changed"
    push; last_push=$SECONDS
  elif [ $((SECONDS - last_push)) -ge $HEARTBEAT ]; then
    echo "$(date -u +%H:%M) heartbeat"
    push; last_push=$SECONDS
  fi
  rm -f .prev.json
  [ $((SECONDS + INTERVAL)) -lt $end ] || break
  sleep "$INTERVAL"
done
push   # keep the latest state for the next run
