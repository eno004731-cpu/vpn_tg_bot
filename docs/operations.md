# Operations

## Monitoring

Создать Telegram secret для Alertmanager вне git:

```bash
cd /opt/vpn-bot
read -s ALERTMANAGER_TELEGRAM_BOT_TOKEN
read ALERTMANAGER_TELEGRAM_CHAT_ID
sudo env ALERTMANAGER_TELEGRAM_BOT_TOKEN="$ALERTMANAGER_TELEGRAM_BOT_TOKEN" \
  ALERTMANAGER_TELEGRAM_CHAT_ID="$ALERTMANAGER_TELEGRAM_CHAT_ID" \
  ./ops/k3s/create_alertmanager_telegram_secret.sh
unset ALERTMANAGER_TELEGRAM_BOT_TOKEN ALERTMANAGER_TELEGRAM_CHAT_ID
```

Установить `kube-prometheus-stack` и применить правила:

```bash
sudo ./ops/k3s/install_monitoring_stack.sh
```

Telegram получает только allowlist из actionable alerts:

- `vpn-bot` web/worker недоступны;
- provisioning/job queue реально сломались;
- Postgres/Vault недоступны;
- backup/restore-check упал;
- pod в `CrashLoopBackOff`;
- k3s node не ready;
- CPU/memory/disk держатся в опасной зоне достаточно долго.

Служебные и шумные kube-prometheus алерты вроде `Watchdog`, `InfoInhibitor`,
`KubeProxyDown`, `KubeControllerManagerDown`, `KubeSchedulerDown` не отправляются в Telegram.
Если secret уже существует, его можно пересоздать без повторного ввода токена: скрипт возьмёт
`bot_token` и `chat_id` из текущего Kubernetes Secret и обновит только routing config.

```bash
sudo ./ops/k3s/create_alertmanager_telegram_secret.sh
sudo ./ops/k3s/install_monitoring_stack.sh
```

Проверки:

```bash
sudo k3s kubectl get pods -n monitoring
sudo k3s kubectl get servicemonitor,prometheusrule -n vpn-prod
curl https://panel.swift-log.ru/metrics
sudo k3s kubectl port-forward -n vpn-prod service/vpn-bot-worker-metrics 9091:9091
curl http://127.0.0.1:9091/metrics
```

Проверить доставку в Telegram тестовым алёртом:

```bash
sudo ./ops/k3s/send_test_alert.sh
```

После теста в Telegram должен прийти `VpnBotTestAlert`, а затем resolved-сообщение.

## Host-level nightly backup

Сделать скрипты исполняемыми и скопировать systemd units:

```bash
chmod +x scripts/nightly_backup.sh scripts/restore_check.sh
sudo cp ops/systemd/vpn-bot-nightly-backup.service /etc/systemd/system/
sudo cp ops/systemd/vpn-bot-nightly-backup.timer /etc/systemd/system/
sudo cp ops/systemd/vpn-bot-backup.env.example /etc/default/vpn-bot-backup
sudo systemctl daemon-reload
sudo systemctl enable --now vpn-bot-nightly-backup.timer
```

Nightly backup запускается в `03:30 Europe/Moscow`, пишет архивы под `/srv/backups/vpn-bot/` и делает встроенный restore-check для SQLite backup.

Полезные команды:

```bash
sudo systemctl status vpn-bot-nightly-backup.timer --no-pager
sudo systemctl start vpn-bot-nightly-backup.service
sudo journalctl -u vpn-bot-nightly-backup.service -n 100 --no-pager
```

## Kubernetes backups

После `sudo k3s kubectl apply -k k8s` будут доступны:

- `postgres-nightly-backup`
- `postgres-weekly-restore-check`

Проверки:

```bash
sudo k3s kubectl get cronjobs -n vpn-prod
sudo k3s kubectl create job --from=cronjob/postgres-nightly-backup manual-postgres-backup -n vpn-prod
sudo k3s kubectl create job --from=cronjob/postgres-weekly-restore-check manual-postgres-restore-check -n vpn-prod
```

Все backup jobs пишут данные под `/srv/backups/vpn-bot/` на этом же сервере:

- `host/`
- `postgres/`
- `logs/`

Vault backup вынесен в `k8s/optional/vault-backups.yaml` и подключается только когда Vault реально вводится в бой.

## First-cutover without Vault

Базовый k8s rollout бота теперь не зависит от Vault. Вместо него используется обычный Kubernetes Secret с `runtime.toml`:

```bash
chmod +x ops/k3s/create_runtime_secret.sh ops/k3s/build_and_import_image.sh
sudo RUNTIME_TOML_PATH=/opt/vpn-bot/secrets/runtime.toml ./ops/k3s/create_runtime_secret.sh
```

Если образ в GHCR приватный, загрузить его локально в k3s:

```bash
sudo APP_DIR=/opt/vpn-bot ./ops/k3s/build_and_import_image.sh
```

Потом:

```bash
sudo k3s kubectl apply -k k8s
sudo k3s kubectl get secret -n vpn-prod vpn-bot-runtime
sudo k3s kubectl get pods -n vpn-prod
```
