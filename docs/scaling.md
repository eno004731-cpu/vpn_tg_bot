# Расширение на другие серверы

Бот умеет работать с несколькими `3x-ui` нодами. Каждая подписка сохраняет `node_code` — короткое имя сервера, например `main`, `nl-2`, `de-1`.

Зачем это нужно:

- видно, на каком сервере выдан доступ;
- проще переносить пользователей на новую ноду;
- можно подготовить базу к нескольким серверам без смены схемы подписок.

## Один сервер

Старый формат остаётся рабочим:

```toml
[xui]
node_code = "main"
base_url = "https://127.0.0.1:8443/secret-path/"
username = "admin"
password = "secret"
inbound_id = 1
public_host = "vpn-main.example.com"
public_port = 443
verify_tls = false
```

## Несколько серверов

Для нескольких нод используйте `[[xui.nodes]]`:

```toml
[xui]
default_node_code = "main"

[[xui.nodes]]
code = "main"
name = "Netherlands main"
enabled = true
priority = 100
base_url = "https://127.0.0.1:8443/secret-path/"
username = "admin"
password = "secret"
inbound_id = 1
public_host = "vpn-main.example.com"
public_port = 443
verify_tls = false

[[xui.nodes]]
code = "nl-2"
name = "Netherlands 2"
enabled = true
priority = 90
base_url = "https://panel-nl2.example.com/secret-path/"
username = "admin"
password = "secret"
inbound_id = 1
public_host = "vpn-nl2.example.com"
public_port = 443
verify_tls = true

[[xui.nodes]]
code = "de-1"
name = "Germany 1"
enabled = false
priority = 80
base_url = "https://panel-de1.example.com/secret-path/"
username = "admin"
password = "secret"
inbound_id = 1
public_host = "vpn-de1.example.com"
public_port = 443
verify_tls = true
```

Новые подписки выдаются на включённую ноду с наименьшим числом активных подписок. Если количество одинаковое, бот выбирает ноду с большим `priority`, затем сортирует по `code`.

`enabled = false` запрещает новые выдачи на ноду. Старые подписки всё равно остаются привязанными к своему `node_code`, поэтому отзыв доступа и синхронизация трафика продолжают ходить на старый сервер.

Проверить состояние нод можно из Telegram:

```text
/admin nodes
/nodes
```

## Минимальный сценарий переноса

1. Поднимите `3x-ui` на новом сервере.
2. Создайте такой же inbound `VLESS + REALITY`.
3. Добавьте новый блок `[[xui.nodes]]` в `secrets/runtime.toml`:

```toml
[[xui.nodes]]
code = "nl-2"
name = "Netherlands 2"
enabled = true
priority = 90
base_url = "https://panel-nl2.example.com/secret-path/"
username = "admin"
password = "secret"
inbound_id = 1
public_host = "new-node.example.com"
public_port = 443
verify_tls = false
```

4. Перезапустите бота.
5. Новые подписки начнут выдаваться на менее загруженную включённую ноду и будут отмечены её `node_code`.

Следующий шаг для полноценного multi-server режима: добавить ручной выбор ноды при выдаче доступа и команду переноса пользователя между нодами.
