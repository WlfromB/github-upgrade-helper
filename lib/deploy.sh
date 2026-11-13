#!/usr/bin/env bash
#
# Универсальный деплой-скрипт github-upgrade-helper.
# Параметры приходят переменными окружения (их выставляет server.py):
#
#   REF          — ветка/тег/SHA для выката (обязательно)
#   REPO_SLUG    — owner/name на GitHub (для fetch по токену)
#   CLONE_DIR    — каталог клона репо на этом сервере (обязательно)
#   COMPOSE_DIR  — каталог umbrella docker-compose (по умолч. /opt/accumulate-queue)
#   SERVICE      — compose-сервис(ы), через пробел; пусто только с MODE=infra-refs
#   MODE         — service (build + recreate) | infra (up -d --build)
#   HEALTH_URL   — если пусто, health-check пропускается
#   HEALTH_GREP  — подстрока/паттерн для grep в ответе health
#   GITHUB_TOKEN — PAT (scope: repo) для приватных репо; без него fetch идёт
#                  через настроенный remote (нужен SSH-ключ)
#   DRY_RUN=1    — только fetch + checkout, compose не запускать
#   HEALTH_TIMEOUT_SECS — сколько ждать health (по умолчанию 300)
#
set -euo pipefail

: "${REF:?REF не задан}"
: "${CLONE_DIR:?CLONE_DIR не задан}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/accumulate-queue}"
SERVICE="${SERVICE:-}"
MODE="${MODE:-service}"
HEALTH_URL="${HEALTH_URL:-}"
HEALTH_GREP="${HEALTH_GREP:-\"status\":\"UP\"}"
HEALTH_TIMEOUT_SECS="${HEALTH_TIMEOUT_SECS:-300}"
REPO_SLUG="${REPO_SLUG:-}"

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*"; exit 1; }
gitx() { git -c safe.directory='*' "$@"; }

[ -d "$CLONE_DIR/.git" ] || die "клон не найден: $CLONE_DIR — добавьте репозиторий заново (он будет склонирован)"

# ── 1) Исходники ────────────────────────────────────────────────────────────
say "==> обновляю исходники: $CLONE_DIR -> $REF"
cd "$CLONE_DIR"
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "$REPO_SLUG" ]; then
  gitx fetch --prune --tags \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO_SLUG}.git" \
    "+refs/heads/*:refs/remotes/origin/*" \
    || die "fetch по токену не удался (проверьте PAT/доступ к ${REPO_SLUG})"
else
  gitx fetch --all --prune --tags \
    || die "fetch через remote не удался: нужен SSH-ключ с доступом либо GITHUB_TOKEN"
fi
if gitx show-ref --verify --quiet "refs/remotes/origin/$REF"; then
  gitx checkout --force -B "$REF" "refs/remotes/origin/$REF"
else
  gitx checkout --force "$REF" || die "ref не найден: $REF (ветка/тег/SHA)"
fi
say "==> исходники: $(gitx rev-parse --short HEAD) ($(gitx symbolic-ref -q --short HEAD || echo 'detached'))"

if [ "${DRY_RUN:-0}" = "1" ]; then
  say "==> DRY_RUN: compose-шаг пропущен"
  exit 0
fi

# ── 2) Compose ──────────────────────────────────────────────────────────────
cd "$COMPOSE_DIR"
export DOCKER_BUILDKIT=1
if [ "$MODE" = "infra" ]; then
  say "==> docker compose up -d --build: ${SERVICE:-<по умолчанию из compose>}"
  # shellcheck disable=SC2086
  docker compose up -d --build $SERVICE
else
  [ -n "$SERVICE" ] || die "SERVICE не задан (для инфраструктурного репо укажите MODE=infra)"
  say "==> docker compose build + up: $SERVICE"
  # shellcheck disable=SC2086
  docker compose build $SERVICE
  # shellcheck disable=SC2086
  docker compose up -d --no-deps --force-recreate $SERVICE
fi

# ── 3) Health ───────────────────────────────────────────────────────────────
if [ -z "$HEALTH_URL" ]; then
  say "==> health-check пропущен (HEALTH_URL не задан); состав:"
  docker compose ps | head -30
  say "==> ДЕПЛОЙ ЗАВЕРШЁН"
  exit 0
fi
say "==> жду health: $HEALTH_URL (до ${HEALTH_TIMEOUT_SECS}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -sf "$HEALTH_URL" 2>/dev/null | grep -q $HEALTH_GREP; then
    say "==> OK: $SERVICE здоров — деплой успешен"
    # shellcheck disable=SC2086
    docker compose ps $SERVICE 2>/dev/null | head -10 || true
    exit 0
  fi
  sleep 5
done
say "==> HEALTH-CHECK ПРОВАЛЕН, последние логи:"
# shellcheck disable=SC2086
docker compose logs --tail=80 $SERVICE 2>/dev/null || true
exit 1
