# Operations

## Monitoring

Установить `kube-prometheus-stack`:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
sudo k3s kubectl create namespace monitoring --dry-run=client -o yaml | sudo k3s kubectl apply -f -
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring \
  -f k8s/monitoring/kube-prometheus-stack-values.yaml
```

Потом создать Alertmanager secret вне git и применить monitoring CRD-ресурсы:

```bash
sudo k3s kubectl apply -f /path/to/alertmanager-vpn-bot-secret.yaml
sudo k3s kubectl apply -k k8s/monitoring
```

Проверки:

```bash
sudo k3s kubectl get servicemonitor,prometheusrule -n vpn-prod
curl https://panel.swift-log.ru/metrics
sudo k3s kubectl port-forward -n vpn-prod service/vpn-bot-worker-metrics 9091:9091
curl http://127.0.0.1:9091/metrics
```

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
