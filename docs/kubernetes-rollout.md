# Kubernetes rollout

Этот путь не трогает текущий `x-ui` и VPN-порты. Сначала переносится бот, а 3x-ui остаётся на systemd до отдельного cutover.

## Перед переключением

1. Сделать backup SQLite:

```bash
cp data/bot.sqlite3 data/bot.sqlite3.backup
```

2. Поднять k3s, ingress-nginx и cert-manager.
3. Проверить DNS `panel.swift-log.ru`.
4. Подготовить Vault secret, который рендерит `/vault/secrets/runtime.toml`.
5. Указать в runtime:

```toml
[app]
database_url = "postgresql://vpn_bot:change-me@postgres.vpn-prod.svc.cluster.local:5432/vpn_bot"
webhook_path_secret = "telegram-webhook-path"
webhook_secret_token = "telegram-secret-token"
field_encryption_key = "replace-with-vault-rendered-key"
public_webhook_base_url = "https://panel.swift-log.ru"
```

## Запуск рядом со старым ботом

```bash
kubectl apply -k k8s
kubectl rollout status statefulset/postgres -n vpn-prod
kubectl rollout status statefulset/vault -n vpn-prod
```

После готовности Postgres:

```bash
vpn-bot migrate-sqlite-to-postgres --sqlite data/bot.sqlite3 --database-url "$VPN_BOT_DATABASE_URL"
```

Потом:

```bash
kubectl rollout status deployment/vpn-bot-web -n vpn-prod
kubectl rollout status deployment/vpn-bot-worker -n vpn-prod
curl https://panel.swift-log.ru/healthz
curl https://panel.swift-log.ru/readyz
```

## Переключение Telegram

1. Установить webhook на `https://panel.swift-log.ru/telegram/<webhook_path_secret>` с `secret_token`.
2. Остановить старый systemd `vpn-bot`.
3. Проверить `/start`, `/buy`, тест Stars за 1 звезду, manual approve, `/my`, `/admin nodes`.

## Rollback

Если бот в Kubernetes сломался:

```bash
kubectl scale deployment/vpn-bot-web -n vpn-prod --replicas=0
kubectl scale deployment/vpn-bot-worker -n vpn-prod --replicas=0
```

Дальше удалить Telegram webhook и вернуть systemd `vpn-bot run`. VPN/3x-ui при этом остаются как были.

## 3x-ui

`k8s/xui-template.yaml` не включён в `kustomization.yaml`. Он нужен только для тестового запуска 3x-ui в Kubernetes. Production cutover делается отдельно: backup базы 3x-ui, остановка systemd `x-ui`, переключение production-портов и быстрый rollback-план.
