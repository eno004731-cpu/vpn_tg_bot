# Kubernetes rollout

Этот путь не трогает текущий `x-ui` и VPN-порты. Сначала переносится бот, а 3x-ui остаётся на systemd до отдельного cutover.

## Перед переключением

1. Сделать backup SQLite:

```bash
cp data/bot.sqlite3 data/bot.sqlite3.backup
```

2. Поднять k3s. Для первого cutover текущий Caddy может проксировать в k8s `NodePort` `127.0.0.1:30080`, поэтому ingress-nginx/cert-manager можно подключить позже.
   `ops/k3s/rollout_bot.sh` автоматически ставит firewall-правило, которое закрывает `30080` для всех интерфейсов кроме loopback.
3. Проверить DNS `panel.swift-log.ru`.
4. Подготовить Kubernetes Secret `vpn-bot-runtime` из `/opt/vpn-bot/secrets/runtime.toml`.
5. Подготовить Kubernetes Secret `postgres-secret` вне git:

```bash
sudo k3s kubectl create namespace vpn-prod --dry-run=client -o yaml | sudo k3s kubectl apply -f -
sudo k3s kubectl create secret generic postgres-secret \
  -n vpn-prod \
  --from-literal=POSTGRES_DB=vpn_bot \
  --from-literal=POSTGRES_USER=vpn_bot \
  --from-literal=POSTGRES_PASSWORD='replace-with-real-password' \
  --dry-run=client -o yaml | sudo k3s kubectl apply -f -
```

6. Указать в runtime:

```toml
[app]
database_url = "postgresql+asyncpg://vpn_bot:replace-with-real-password@postgres.vpn-prod.svc.cluster.local:5432/vpn_bot"
webhook_path_secret = "telegram-webhook-path"
webhook_secret_token = "telegram-secret-token"
field_encryption_key = "replace-with-vault-rendered-key"
public_webhook_base_url = "https://panel.swift-log.ru"
```

## Запуск рядом со старым ботом

```bash
chmod +x ops/k3s/build_and_import_image.sh ops/k3s/create_runtime_secret.sh
sudo APP_DIR=/opt/vpn-bot ./ops/k3s/build_and_import_image.sh
sudo RUNTIME_TOML_PATH=/opt/vpn-bot/secrets/runtime.toml ./ops/k3s/create_runtime_secret.sh
sudo k3s kubectl apply -k k8s
sudo k3s kubectl rollout status statefulset/postgres -n vpn-prod
```

Для автоматического push-deploy в k3s workflow теперь умеет отдельный режим `k8s`: он вызывает `ops/k3s/rollout_bot.sh`, собирает образ с tag = commit SHA и делает rollout `vpn-bot-web`/`vpn-bot-worker` через `kubectl set image` + `rollout status`.

Для мониторинга отдельно поставить `kube-prometheus-stack` и применить CRD-ресурсы из `k8s/monitoring/`.

После готовности Postgres:

```bash
vpn-bot migrate-sqlite-to-postgres --sqlite data/bot.sqlite3 --database-url "$VPN_BOT_DATABASE_URL"
```

Потом:

```bash
sudo k3s kubectl rollout status deployment/vpn-bot-web -n vpn-prod
sudo k3s kubectl rollout status deployment/vpn-bot-worker -n vpn-prod
curl http://127.0.0.1:30080/healthz
curl http://127.0.0.1:30080/readyz
curl http://127.0.0.1:30080/metrics
```

`vpn-bot-web` в Kubernetes использует `RollingUpdate` с `maxUnavailable: 0`, поэтому новые pod'ы проходят readiness до удаления старых. `vpn-bot-worker` использует `Recreate`, чтобы в момент деплоя не запускать две рабочие реплики одновременно.

## Переключение Telegram

1. Переключить Caddy с `127.0.0.1:8080` на `127.0.0.1:30080` и выполнить `sudo systemctl reload caddy`.
2. Проверить `curl https://panel.swift-log.ru/healthz` и `curl https://panel.swift-log.ru/readyz`.
3. Установить webhook на `https://panel.swift-log.ru/telegram/<webhook_path_secret>` с `secret_token`.
4. Остановить старые systemd-сервисы `vpn-bot-web` и `vpn-bot-worker`. При deploy mode `k8s` это делает workflow после успешного rollout через `ops/systemd/restart_bot_services.sh k8s`.
5. Проверить `/start`, `/buy`, тест Stars за 1 звезду, manual approve, `/my`, `/admin nodes`.

## Rollback

Если бот в Kubernetes сломался:

```bash
sudo k3s kubectl scale deployment/vpn-bot-web -n vpn-prod --replicas=0
sudo k3s kubectl scale deployment/vpn-bot-worker -n vpn-prod --replicas=0
```

Дальше вернуть Caddy на `127.0.0.1:8080`, запустить `vpn-bot-web` и `vpn-bot-worker` через systemd. VPN/3x-ui при этом остаются как были.

## 3x-ui

`k8s/xui-template.yaml` не включён в `kustomization.yaml`. Он нужен только для тестового запуска 3x-ui в Kubernetes. Production cutover делается отдельно: backup базы 3x-ui, остановка systemd `x-ui`, переключение production-портов и быстрый rollback-план.

## Nightly backups

- Host-level backup для текущего production ставится через `ops/systemd/vpn-bot-nightly-backup.service` и `ops/systemd/vpn-bot-nightly-backup.timer`.
- K8s backup jobs для Postgres уже лежат в `k8s/backups.yaml`.
- Vault backup вынесен в `k8s/optional/vault-backups.yaml` и подключается позже, вместе с отдельным Vault rollout.
- Расписание: nightly в `03:30 Europe/Moscow`.
- Retention: `14 daily + 8 weekly`.
- Ручной restore-check для host backup: `scripts/restore_check.sh`.

## Vault

Vault сознательно не входит в базовый first-cutover. Для первой рабочей версии webhook-бота в k8s достаточно:

- `postgres` в кластере;
- `vpn-bot-runtime` как обычный Kubernetes Secret;
- локально загруженного образа бота в k3s, если GHCR-пакет приватный.

Если Vault понадобится позже, использовать `k8s/optional/vault.yaml` и подключать его уже вместе с Vault injector/auth setup.
