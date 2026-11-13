# github-upgrade-helper

Мини-приложение для деплоя GitHub-репозиториев **на свой сервер** без GitHub Actions
(Actions для приватных репо лимитирован бюджетом/минутами; self-hosted-раннеры при
нулевом бюджете тоже не получают задания).

Делает то же, что ручной Deploy-ворклоу, но из браузера:

```
git fetch → checkout <ref> → docker compose build <service> → up -d --force-recreate → health-check
```

- **Zero-dependency**: чистый Python 3.9+ (стандартная библиотека), ничего ставить не нужно.
- Несколько репозиториев; приватные — по PAT-токену или через SSH-ключ сервера.
- Логи каждого запуска, статусы, история, dry-run, откат повторным деплоем старого ref.
- Инструкция продублирована прямо в UI (`/`), пароль (Basic auth) — опционально.

## Установка на сервере

```bash
git clone https://github.com/WlfromB/github-upgrade-helper.git /opt/github-upgrade-helper
cd /opt/github-upgrade-helper
python3 server.py          # поднимется на http://127.0.0.1:8790
```

Автозапуск (systemd):

```bash
cp deploy/github-upgrade-helper.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now github-upgrade-helper
journalctl -u github-upgrade-helper -f
```

Доступ с локальной машины через SSH-туннель (рекомендуется):

```bash
ssh -L 8790:127.0.0.1:8790 root@<сервер>
# затем открывайте http://127.0.0.1:8790
```

## Настройка

Всё в `config.json` (создаётся при первом запуске, редактируется и из UI):

| Поле | Назначение |
|---|---|
| `settings.bind` / `port` | По умолчанию `127.0.0.1:8790` — снаружи не видно |
| `settings.password` | Если задан — HTTP Basic auth (`admin` / пароль) |
| `settings.workspaceRoot` | Куда клонировать новые репозитарии (по умолчанию `/opt`) |
| `settings.composeDir` | Каталог umbrella docker-compose (по умолчанию `/opt/accumulate-queue`) |
| `settings.githubToken` | PAT по умолчанию для приватных репо (scope: `repo`) |
| `repos[]` | Список репозиториев: url, cloneDir, service, healthUrl, defaultRef, token |

Токен берётся один раз здесь: GitHub → Settings → Developer settings →
Personal access tokens → **classic** → галочка **repo**.

## Как деплоить

1. Добавьте репозиторий (ссылка в любом виде; для приватного — токен).
2. Введите ref (ветка/тег/SHA) → **Деплой**. `dry-run` — только обновить исходники.
3. Смотрите лог: fetch, коммит, сборка, health-check.
4. **Откат** — деплой предыдущего ref (история с коммитами в разделе «Запуски»).

## Состав

```
server.py            # приложение (HTTP + API + запуск деплоев), только stdlib
static/index.html    # UI с инструкцией
lib/deploy.sh        # универсальный деплой-скрипт (параметры через env)
deploy/*.service     # systemd-юнит
runs/                # логи запусков (создаётся автоматически)
config.json          # настройки (gitignore, создаётся при первом старте)
```

## Безопасность

Приложение исполняет `git`/`docker` на сервере от имени пользователя, запустившего
процесс. По умолчанию слушает только localhost — открывайте наружу лишь за паролем
(`settings.password`) и HTTPS/VPN. Токены в UI не отображаются.

## Пара к скриптам в репозиториях

В каждом репозитории проекта лежит `scripts/deploy.sh` (ветка `chore/deploy-scripts`) —
та же логика для ручного запуска по SSH: `./scripts/deploy.sh main`. Хелпер использует
собственный `lib/deploy.sh`, чтобы работать с любым ref независимо от содержимого репо.
