#!/usr/bin/env bash
# Systems integrator demo driver.
#
#   ./demo.sh up            start Keycloak, mock model, MCP servers and the gateway
#   ./demo.sh seed [args]   stop the gateway, backfill 30 days of history, start it again
#   ./demo.sh live [args]   continuous traffic through the gateway (Ctrl+C to stop)
#   ./demo.sh status        budget counters
#   ./demo.sh moment NAME   a scripted moment: tokens | maria | block-tom | wrong-app | service-app | real
#   ./demo.sh reset-tom     clear tom.vega's daily counter so the block moment can run again
#   ./demo.sh logs          follow the gateway log
#   ./demo.sh colima-net    local workaround if image pulls time out under Colima
#   ./demo.sh down          stop everything (keeps the agw-data volume; add -v to docker compose down to wipe it)
set -euo pipefail
cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(docker-compose); fi
READY=http://localhost:15321/healthz/ready

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# A new Docker volume is owned by root; the gateway and the seeder run as uid 65532.
own_volume() { "${DC[@]}" run --rm --no-deps --user 0 seeder chown 65532:65532 /data >/dev/null; }

wait_ready() {
  printf 'Waiting for the gateway'
  for _ in $(seq 1 60); do
    if curl -fs -o /dev/null "$READY"; then echo " ready."; return 0; fi
    printf '.'; sleep 2
  done
  echo; "${DC[@]}" logs --tail 20 agentgateway; die "gateway did not become ready"
}

check_env() {
  [ -f .env ] || die "no .env. Run: cp .env.example .env, then set ENTERPRISE_AGENTGATEWAY_LICENSE_KEY"
  grep -qE '^ENTERPRISE_AGENTGATEWAY_LICENSE_KEY=.+' .env || die "set ENTERPRISE_AGENTGATEWAY_LICENSE_KEY in .env"
  # Without SESSION_KEY the gateway writes a generated key into gateway/config.yaml.
  if ! grep -qE '^SESSION_KEY=.+' .env; then
    sed -i.bak '/^SESSION_KEY=/d' .env && rm -f .env.bak
    echo "SESSION_KEY=$(openssl rand -hex 32)" >> .env
  fi
}

# Clear budget_usage rows matching a SQL LIKE pattern. The gateway is stopped first:
# it keeps counters in memory and writes them back every few seconds.
clear_counter() {
  "${DC[@]}" stop agentgateway >/dev/null
  "${DC[@]}" run --rm --no-deps seeder python -c '
import sqlite3, sys
db = sqlite3.connect("/data/agentgateway.db")
n = db.execute("DELETE FROM budget_usage WHERE budget_id LIKE ?", (sys.argv[1],)).rowcount
db.commit(); print(f"cleared {n} counter(s)")' "$1"
  "${DC[@]}" start agentgateway >/dev/null
  wait_ready
}

case "${1:-}" in
  up)
    check_env
    "${DC[@]}" up -d keycloak
    own_volume
    "${DC[@]}" up -d --build
    wait_ready
    echo "UI:       http://localhost:15300/ui"
    echo "Gateway:  http://localhost:4300  (LLM /v1/messages, MCP /mcp)"
    echo "Keycloak: http://localhost:8380  (admin / admin)"
    ;;
  seed)
    shift
    check_env
    own_volume
    # The gateway creates and migrates the schema; start it once if the volume is new.
    if ! "${DC[@]}" run --rm --no-deps seeder test -f /data/agentgateway.db; then
      "${DC[@]}" up -d agentgateway; wait_ready
    fi
    "${DC[@]}" stop agentgateway >/dev/null 2>&1 || true
    "${DC[@]}" run --rm --no-deps seeder python backfill.py --db /data/agentgateway.db "$@"
    "${DC[@]}" up -d agentgateway
    wait_ready
    ;;
  live)    shift; exec python3 scripts/live_traffic.py "$@" ;;
  status)  exec python3 scripts/moments.py status ;;
  moment)  shift; exec python3 scripts/moments.py "$@" ;;
  reset-tom) clear_counter '%tom-vega-daily' ;;
  colima-net)
    # Local workaround: the Colima VM's default route (vmnet) times out to quay.io and
    # Docker Hub. Prefer the user-mode interface. Lasts until the VM restarts.
    colima ssh -- sh -c 'ip route | grep -q "metric 50" || sudo ip route add default via 192.168.5.2 dev eth0 metric 50'
    echo "route set" ;;
  logs)    exec "${DC[@]}" logs -f agentgateway ;;
  down)    "${DC[@]}" down ;;
  *)       sed -n '2,13p' "$0"; exit 1 ;;
esac
