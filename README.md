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
- хранит секреты отдельно в `secrets/runtime.toml`, а Docker-образ их не забирает.

## Что внутри

- `aiogram` для Telegram-бота;
- `SQLAlchemy + SQLite` для хранения пользователей, инвойсов и подписок;
- HTTP-клиент к `3x-ui`, который создаёт клиента и читает статистику трафика;
- полуавтоматическая схема оплаты без эквайринга: уникальная сумма + ручное подтверждение админом.

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

## Как устроена оплата без платёжной системы

Сейчас бот работает по безопасной для MVP схеме:

1. Пользователь выбирает тариф.
2. Бот выдаёт перевод на карту/телефон с уникальной суммой, например `299.17 ₽`.
3. Пользователь нажимает кнопку "Я оплатил".
4. Админу приходит карточка на подтверждение.
5. После подтверждения бот создаёт клиента в `3x-ui` и отправляет ссылку.

Telegram Stars работают параллельно:

1. Пользователь выбирает тариф.
2. Нажимает оплату Stars.
3. Telegram присылает `successful_payment`.
4. Бот сам создаёт клиента в `3x-ui` и отправляет ссылку.

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

## Основные команды

Пользователь:

- `/start`
- `/buy`
- `/my`

Админ:

- `/admin`
- `/admin help`
- `/admin users [username|id]`
- `/users [username|id]`
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

Подробный выбор стека и протокола описан в [docs/vpn-stack.md](docs/vpn-stack.md).
Как готовить перенос на другие серверы: [docs/scaling.md](docs/scaling.md).

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

И systemd-сервис:

```bash
sudo systemctl status vpn-bot --no-pager
```

Чтобы runner мог перезапускать только сервис бота без пароля:

```bash
sudo visudo -f /etc/sudoers.d/github-runner-vpn-bot
```

```text
github-runner ALL=(root) NOPASSWD: /usr/bin/systemctl restart vpn-bot, /usr/bin/systemctl status vpn-bot --no-pager, /bin/systemctl restart vpn-bot, /bin/systemctl status vpn-bot --no-pager
```
