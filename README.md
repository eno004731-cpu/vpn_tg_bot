# VPN subscription bot

## Мини-описание

Простой Telegram-бот для продажи VPN-подписок. Пользователь выбирает тариф, оплачивает переводом или Telegram Stars, а бот выдаёт VPN-ссылку и показывает остаток трафика. Админ может подтверждать переводы, смотреть активные подписки и видеть, кто сколько трафика использует.

Бот умеет:

- принимать оплату переводом на карту или через СБП;
- принимать оплату Telegram Stars;
- автоматически выдавать `VLESS + REALITY` доступ после оплаты;
- показывать пользователю срок подписки и расход трафика;
- показывать админу список пользователей и их трафик;
- снижать скорость после дневного лимита;
- хранить секреты отдельно от кода.

Telegram-бот для продажи VPN-подписок с интеграцией в `3x-ui`:

- показывает пользователю его трафик и срок действия;
- показывает админу список пользователей и использование трафика;
- создаёт инвойс на перевод на карту с уникальной суммой;
- принимает оплату через Telegram Stars;
- снижает скорость после дневного fair-use лимита;
- умеет выдавать `VLESS + REALITY` ссылку, которая импортируется в Hiddify и v2RayTun;
- умеет готовиться к нескольким VPN-нодам и выбирать менее загруженную ноду для новой подписки;
- хранит секреты отдельно в `secrets/runtime.toml`, а Docker-образ их не забирает.

## Что внутри

- `aiogram` для Telegram-бота;
- `SQLAlchemy + SQLite/Postgres` для хранения пользователей, инвойсов, подписок и очереди выдачи;
- HTTP-клиент к `3x-ui`, который создаёт клиента и читает статистику трафика;
- полуавтоматическая схема оплаты без эквайринга: уникальная сумма + ручное подтверждение админом;
- webhook-режим для Telegram, healthcheck endpoints и worker с retry-очередью.

## Быстрый старт

1. Установите зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install ".[dev]"
```

2. Скопируйте пример секретов и заполните его:

```bash
cp config/examples/secrets.example.toml secrets/runtime.toml
```

3. Проверьте тарифы в `config/plans.toml`.
   - `price_rub` отвечает за оплату переводом.
   - `price_stars` отвечает за оплату Telegram Stars.
   - `provision_access = false` оставляет оплату тестовой, без выдачи VPN.

4. Инициализируйте базу:

```bash
vpn-bot init-db
```

5. Запустите бота:

```bash
vpn-bot run
```

`vpn-bot run` остаётся polling-режимом для простого запуска и rollback. Для Kubernetes используются две роли:

```bash
vpn-bot web
vpn-bot worker
```

`web` принимает Telegram webhook и отвечает на `/healthz` и `/readyz`. `worker` выдаёт доступ в `3x-ui`, ретраит ошибки, синкает трафик и отправляет пользователю ссылку.
`web` также публикует `/metrics`, а `worker` поднимает отдельный metrics listener на `worker_metrics_host:worker_metrics_port`.

## Как устроена оплата без платёжной системы

Сейчас бот работает по безопасной для MVP схеме:

1. Пользователь выбирает тариф.
2. Бот выдаёт перевод на карту/телефон с уникальной суммой, например `299.17 ₽`.
3. Пользователь нажимает кнопку "Я оплатил".
4. Админу приходит карточка на подтверждение.
5. После подтверждения бот фиксирует оплату и создаёт job выдачи.
6. Worker создаёт клиента в `3x-ui` и отправляет ссылку. Если `3x-ui` временно недоступна, job ретраится.

Telegram Stars работают параллельно:

1. Пользователь выбирает тариф.
2. Нажимает оплату Stars.
3. Telegram присылает `successful_payment`.
4. Бот сохраняет платёж и создаёт job выдачи.
5. Worker выдаёт доступ. Повторный webhook от Telegram не создаёт дубль.

В `config/plans.toml` есть тестовый пункт `stars_test` за 1 звезду. Он нужен только для проверки оплаты и не выдаёт VPN-доступ.

## Fair-use лимит скорости

По умолчанию бот следит за активными клиентами в `3x-ui` и выставляет `speedLimit = 1250`, то есть примерно `10 Мбит/с`, после дневного лимита тарифа. На следующий день скорость снимается.

Текущие дневные лимиты:

- `30 дней / 310 ГБ`: `75 ГБ` в день;
- `90 дней / 1000 ГБ`: `150 ГБ` в день;
- `180 дней / 2200 ГБ`: `250 ГБ` в день.

Настройка лежит в `secrets/runtime.toml`:

```toml
[traffic_policy]
enabled = true
daily_limit_gb = 75
throttled_speed_kbytes_per_second = 1250
timezone = "Europe/Moscow"
```

`daily_limit_gb` в блоке секретов используется как запасное значение. Для конкретных тарифов дневной лимит задаётся в `config/plans.toml`.

Если ваша версия `3x-ui` не поддерживает `speedLimit` у клиентов, бот запишет ошибку в лог при синхронизации трафика.

Если в `secrets/runtime.toml` заполнен `phone`, бот показывает телефон для перевода через СБП. Если телефона нет, бот показывает полный номер карты.

Это реально работает без эквайринга. Для полной автоматизации позже можно подключить:

- `ЮKassa` для платёжных ссылок и автоплатежей;
- `CloudPayments` для ссылок и webhook-уведомлений;
- `T-Bank` API/СБП, если у вас бизнес-счёт и нужен более банковский сценарий.

Подробности и варианты лежат в [docs/payments.md](docs/payments.md).

## Секреты

- секреты лежат в `secrets/runtime.toml`;
- `secrets/` игнорируется git;
- `secrets/` исключён из Docker-контекста через `.dockerignore`;
- в прод контейнер монтирует секреты как read-only volume.
- в первом k8s cutover тот же файл монтируется в pod как Kubernetes Secret `vpn-bot-runtime`;
- Vault вынесен в `k8s/optional/` и подключается позже отдельным этапом;
- если задан `field_encryption_key`, новые `access_url`, `xui_client_id`, `xui_email` пишутся в БД как `enc:v1:...`.

Для Postgres можно указать URL через env или TOML:

```toml
[app]
database_url = "postgresql://vpn_bot:change-me@postgres.vpn-prod.svc.cluster.local:5432/vpn_bot"
```

Если `database_url` не задан, бот продолжит работать на SQLite по `database_path`.

Миграция SQLite в database URL:

```bash
vpn-bot migrate-sqlite-to-postgres --sqlite data/bot.sqlite3 --database-url "$VPN_BOT_DATABASE_URL"
```

## Основные команды

Пользователь:

- `/start`
- `/buy`
- `/my`

Админ:

- `/admin`
- `/admin help`
- `/admin nodes`
- `/nodes`
- `/admin users [username|id]`
- `/users [username|id]`
- `/admin invoices`
- `/invoices`
- `/traffic_admin`
- `/approve <invoice_id>`
- `/reject <invoice_id> [причина]`

## Что нужно настроить на сервере

1. Поднимите `3x-ui`.
2. Создайте inbound `VLESS + REALITY`.
3. В `secrets/runtime.toml` укажите:
   - URL панели,
   - логин/пароль,
   - `inbound_id`,
   - внешний домен/IP и порт.
   - `node_code`, например `main`, чтобы подписки было проще переносить между серверами.
   Если бот запущен на том же Ubuntu-сервере, можно указать локальный URL панели:
   `base_url = "https://127.0.0.1:8443/secret-path/"` и `verify_tls = false`.

Для одного сервера старый блок `[xui]` остаётся рабочим. Для нескольких серверов используйте `[[xui.nodes]]`:

```toml
[xui]
default_node_code = "main"

[[xui.nodes]]
code = "main"
name = "Netherlands main"
enabled = true
priority = 100
base_url = "https://panel-main.example.com/secret-path"
username = "admin"
password = "super-secret"
inbound_id = 1
public_host = "vpn-main.example.com"
public_port = 443
verify_tls = true
fingerprint = "chrome"
flow = "xtls-rprx-vision"
spider_x = "/"

[[xui.nodes]]
code = "nl-2"
name = "Netherlands 2"
enabled = true
priority = 90
base_url = "https://panel-nl2.example.com/secret-path"
username = "admin"
password = "super-secret"
inbound_id = 1
public_host = "vpn-nl2.example.com"
public_port = 443
```

Новые подписки выдаются на включённую ноду с наименьшим числом активных подписок. Если `enabled = false`, новые пользователи на эту ноду не попадут, но старые подписки останутся привязанными к своему `node_code`.

Подробный выбор стека и протокола описан в [docs/vpn-stack.md](docs/vpn-stack.md).
Как готовить перенос на другие серверы: [docs/scaling.md](docs/scaling.md).

## Kubernetes/Webhook

Манифесты лежат в `k8s/`:

- `vpn-bot-web` — `Deployment` на 2 реплики, принимает Telegram webhook;
- `vpn-bot-worker` — `Deployment` на 1 реплику, обрабатывает outbox/jobs;
- `postgres` — `StatefulSet`;
- `backups.yaml` — `CronJob` для nightly backup Postgres и weekly restore-check;
- `optional/vault.yaml` — Vault, если он понадобится позже;
- `optional/vault-backups.yaml` — nightly Vault backup для отдельного vault rollout;
- `vpn-bot-web` — `Service`;
- `Ingress` для `panel.swift-log.ru`;
- `monitoring/` — values и custom resources для `kube-prometheus-stack`;
- `xui-template.yaml` — подготовка 3x-ui на тестовых портах, не включена в `kustomization.yaml`.

Базовая проверка:

```bash
chmod +x ops/k3s/build_and_import_image.sh ops/k3s/create_runtime_secret.sh
sudo APP_DIR=/opt/vpn-bot ./ops/k3s/build_and_import_image.sh
sudo RUNTIME_TOML_PATH=/opt/vpn-bot/secrets/runtime.toml ./ops/k3s/create_runtime_secret.sh
sudo k3s kubectl apply -k k8s
sudo k3s kubectl rollout status deployment/vpn-bot-web -n vpn-prod
sudo k3s kubectl rollout status deployment/vpn-bot-worker -n vpn-prod
sudo k3s kubectl rollout status statefulset/postgres -n vpn-prod
curl https://panel.swift-log.ru/healthz
curl https://panel.swift-log.ru/readyz
curl https://panel.swift-log.ru/metrics
```

Текущий `x-ui`/VPN на systemd эти манифесты не трогают. Переключать Telegram webhook можно только после миграции базы и проверки `web/worker`. Подробный порядок: [docs/kubernetes-rollout.md](docs/kubernetes-rollout.md).

Мониторинг и nightly backup-операции описаны в [docs/operations.md](docs/operations.md).

## Перед публикацией

Проверьте, что в коммит не попадают локальные секреты и база:

```bash
git status --short
git check-ignore -v secrets/runtime.toml data/bot.sqlite3 .env
```

В публичный репозиторий должны попадать только примеры из `config/examples/`, без настоящих токенов, паролей, карт и доменов.

## CI/CD

CI запускается на `push` и `pull_request`:

```bash
python scripts/check_secrets.py
ruff format --check .
ruff check .
pytest -q
```

Локально можно повторить те же проверки:

```bash
python -m pip install ".[dev]"
python scripts/check_secrets.py
ruff format --check .
ruff check .
pytest -q
```

CD запускается только при `push` в `main` или вручную через `workflow_dispatch`. Для деплоя нужен self-hosted runner на сервере с labels:

```text
vpn-bot,prod
```

Есть два режима деплоя:

- `webhook` — текущий production через systemd `vpn-bot-web` + `vpn-bot-worker`;
- `k8s` — rollout в `k3s`, где `Deployment` сам меняет старые pod'ы на новые и поднимает их заново при падении.

По умолчанию workflow остаётся в `webhook`. Чтобы переключить push-деплой на Kubernetes, достаточно завести GitHub Actions variable:

```text
VPN_BOT_DEPLOY_MODE=k8s
```

Опционально можно задать:

```text
VPN_BOT_K8S_NAMESPACE=vpn-prod
VPN_BOT_K8S_RUNTIME_SECRET=vpn-bot-runtime
VPN_BOT_K8S_IMAGE_REPO=ghcr.io/eno004731-cpu/vpn_tg_bot
```

Текущие systemd-сервисы:

```bash
sudo systemctl status vpn-bot-web --no-pager
sudo systemctl status vpn-bot-worker --no-pager
```

CD в `webhook`-режиме:

- ставит unit-файлы через `ops/systemd/install_bot_units.sh`;
- останавливает старые сервисы и stray Python-процессы;
- отключает polling fallback `vpn-bot`;
- включает и перезапускает `vpn-bot-web` и `vpn-bot-worker` через `ops/systemd/restart_bot_services.sh`.

CD в `k8s`-режиме:

- собирает локальный образ с tag = commit SHA;
- импортирует его в `k3s` containerd;
- обновляет secret `vpn-bot-runtime`;
- делает `kubectl apply -k k8s`;
- меняет image в `vpn-bot-web` и `vpn-bot-worker`;
- ждёт `rollout status`, пока старые pod'ы заменятся новыми.

Чтобы runner мог делать это без пароля:

```bash
sudo visudo -f /etc/sudoers.d/github-runner-vpn-bot
```

```text
Cmnd_Alias VPN_BOT_DEPLOY = \
  /opt/vpn-bot/ops/systemd/install_bot_units.sh, \
  /opt/vpn-bot/ops/systemd/restart_bot_services.sh, \
  /opt/vpn-bot/ops/k3s/rollout_bot.sh

github-runner ALL=(root) NOPASSWD: VPN_BOT_DEPLOY
```

Если нужен временный rollback в polling, workflow тоже поддерживает режим `polling`.
