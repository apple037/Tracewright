#!/usr/bin/env bash
# Start the whole demo: database, API, background worker, sample data.
#
#   ./run.sh          start everything and print the console URL
#   ./run.sh stop     stop it
#   ./run.sh logs     follow the logs
#   ./run.sh reset    stop and delete the database (starts fresh next time)
#
# Every failure below is meant to tell you what to do about it, not just what
# went wrong.

set -euo pipefail
cd "$(dirname "$0")"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"; fi
}

case "${1:-start}" in
  stop)  compose down; ok "Stopped."; exit 0 ;;
  logs)  compose logs -f app worker; exit 0 ;;
  reset) compose down -v; ok "Stopped and database deleted."; exit 0 ;;
  start) ;;
  *) die "Unknown command '$1'. Use: start | stop | logs | reset" ;;
esac

# --- 1. Docker ---------------------------------------------------------------

command -v docker >/dev/null 2>&1 \
  || die "Docker is not installed. Install Docker Desktop: https://docker.com/get-started"
docker info >/dev/null 2>&1 \
  || die "Docker is installed but not running. Start Docker Desktop and try again."
ok "Docker is running."

# --- 2. .env -----------------------------------------------------------------

if [ ! -f .env ]; then
  cp .env.example .env
  warn "Created .env for you."
  say ""
  say "Open .env and set these two lines to any random text, 16+ characters:"
  say "    ${DIM}DEMO_CUSTOMER_TOKEN=...${OFF}"
  say "    ${DIM}DEMO_ADMIN_TOKEN=...${OFF}"
  say ""
  say "Then run ./run.sh again."
  exit 1
fi

set -a; . ./.env; set +a

for name in DEMO_CUSTOMER_TOKEN DEMO_ADMIN_TOKEN; do
  value="${!name:-}"
  [ -n "$value" ] || die ".env is missing $name. Set it to any random text, 16+ characters."
  [ "${#value}" -ge 16 ] || die "$name in .env is too short (${#value} characters). It needs 16 or more."
done
ok "Login tokens are set."

# --- 3. The model server -----------------------------------------------------

MODEL_URL="${REMOTE_MODEL_BASE_URL:-}"
[ -n "$MODEL_URL" ] || die ".env is missing REMOTE_MODEL_BASE_URL — the address of your AI model server."

# Both /v1/models (OpenAI-style) and /api/tags (Ollama) are worth trying; the
# URL usually ends in /v1 but the Ollama-native path sits at the root.
MODEL_ROOT="${MODEL_URL%/}"; MODEL_ROOT="${MODEL_ROOT%/v1}"
if curl -fsS -m 5 "${MODEL_ROOT}/v1/models" >/dev/null 2>&1 \
   || curl -fsS -m 5 "${MODEL_ROOT}/api/tags" >/dev/null 2>&1; then
  ok "Model server is reachable at ${MODEL_ROOT}"
else
  warn "Cannot reach the model server at ${MODEL_ROOT}"
  say "  The app will still start, but every reply will fail until it responds."
  say "  ${DIM}Check REMOTE_MODEL_BASE_URL in .env. If the model runs on this"
  say "  machine, use http://host.docker.internal:11434/v1 so the container"
  say "  can see it — 'localhost' inside a container means the container.${OFF}"
fi

# --- 4. Start ----------------------------------------------------------------

say ""
say "Starting (the first run builds images and takes a few minutes)…"
compose up --build -d

printf 'Waiting for the API'
for _ in $(seq 1 60); do
  if curl -fsS -m 2 http://localhost:8080/health/live >/dev/null 2>&1; then
    printf '\n'; ok "API is up."; break
  fi
  printf '.'; sleep 2
done
curl -fsS -m 2 http://localhost:8080/health/live >/dev/null 2>&1 || {
  printf '\n'
  die "The API did not come up. See what went wrong with: ./run.sh logs"
}

say "Loading sample data…"
compose --profile demo run --rm demo-seed >/dev/null && ok "Sample data loaded."

say ""
ok "Ready."
say ""
say "  Console   http://localhost:8080/console/     ${DIM}(use your admin token)${OFF}"
say "  Chat only http://localhost:8080/console/chat.html"
say ""
say "  ${DIM}Logs: ./run.sh logs    Stop: ./run.sh stop${OFF}"
