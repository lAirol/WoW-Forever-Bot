"""Discord server provisioner. Standard library only. No credentials in files."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://discord.com/api/v10"
HERE = Path(__file__).resolve().parent
VIEW, SEND, EMBED, HISTORY = 1 << 10, 1 << 11, 1 << 14, 1 << 16
CONNECT, SPEAK = 1 << 20, 1 << 21
MANAGE_CHANNELS, MANAGE_ROLES = 1 << 4, 1 << 28
APP_COMMANDS = 1 << 31
REQUIRED = VIEW | SEND | EMBED | HISTORY | MANAGE_CHANNELS | MANAGE_ROLES
READER = VIEW | HISTORY | APP_COMMANDS
WRITER = READER | SEND | EMBED | (1 << 15) | (1 << 6)
VOICE = VIEW | CONNECT | SPEAK


class SetupError(Exception):
    pass


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    role_keys = {r["key"] for r in config["roles"]}
    if len(role_keys) != len(config["roles"]):
        raise SetupError("Повторяющийся ключ роли.")
    category_keys, channel_keys = set(), set()
    for category in config["categories"]:
        if category["key"] in category_keys:
            raise SetupError("Повторяющийся ключ категории.")
        category_keys.add(category["key"])
        if not set(category["access"]) <= role_keys:
            raise SetupError("Неизвестная роль доступа.")
        for channel in category["channels"]:
            if channel["key"] in channel_keys:
                raise SetupError("Повторяющийся ключ канала.")
            channel_keys.add(channel["key"])
            if len(channel.get("message", "")) > 2000:
                raise SetupError("Стартовое сообщение длиннее лимита Discord.")
    return config


def overwrites(config_category, channel, guild_id, bot_id, role_ids):
    """Explicit per-channel ACL; no changes to existing @everyone or human roles."""
    voice = channel.get("type") == "voice"
    readonly = channel.get("readonly", False)
    public = config_category.get("public", False)
    everyone_deny = (0 if public else VIEW) | (1 << 35) | (1 << 36)
    if readonly:
        everyone_deny |= SEND | (1 << 6)
    items = [{"id": guild_id, "type": 0,
              "allow": str(READER if public else 0),
              "deny": str(everyone_deny)}]
    items.append({"id": bot_id, "type": 1,
                  "allow": str(WRITER | MANAGE_CHANNELS | VOICE), "deny": "0"})
    access = set(config_category["access"]) | {"owner", "operator"}
    for key in sorted(access):
        permission = VOICE if voice else (WRITER if key in {"owner", "operator"} or not readonly else READER)
        items.append({"id": role_ids[key], "type": 0,
                      "allow": str(permission), "deny": "0"})
    if public and not readonly:
        items[0]["allow"] = str(WRITER)
        items[0]["deny"] = str((1 << 35) | (1 << 36))
    return items


class Discord:
    def __init__(self, token):
        self.token = token

    def request(self, method, route, data=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        for attempt in range(5):
            request = urllib.request.Request(API + route, data=body, method=method,
                headers={"Authorization": "Bot " + self.token,
                         "Content-Type": "application/json",
                         "User-Agent": "DiscordBot (https://discord.com/developers/docs, 1.0)",
                         "X-Audit-Log-Reason": "Forever%20Team%20server%20setup"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    try:
                        pause = float(json.loads(exc.read()).get("retry_after", 2))
                    except (ValueError, TypeError):
                        pause = 2
                    if pause > 120:
                        raise SetupError("Discord просит длительную паузу. Повторите запуск позже.") from None
                    time.sleep(max(0.1, pause) + 0.2)
                    continue
                # Never print request headers, response bodies or token.
                raise SetupError(f"Discord HTTP {exc.code}: {method} {route}. Проверьте доступ и права бота.") from None
            except (urllib.error.URLError, TimeoutError):
                # A POST may have succeeded; never retry mutations blindly.
                raise SetupError("Сетевой сбой: результат последнего запроса неоднозначен. Проверьте сервер перед повтором.") from None
        raise SetupError("Лимит запросов Discord. Повторите запуск позже.")


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def provision(api, config, guild_id, expected_name, state_path):
    me = api.request("GET", "/users/@me")
    if not me.get("bot"):
        raise SetupError("Разрешён только токен официального бота.")
    guild = api.request("GET", f"/guilds/{guild_id}")
    if guild["name"] != expected_name:
        raise SetupError("Имя сервера не совпало с --expected-name. Изменения не выполнены.")
    member = api.request("GET", f"/guilds/{guild_id}/members/{me['id']}")
    roles = api.request("GET", f"/guilds/{guild_id}/roles")
    own = {guild_id, *member["roles"]}
    permissions = 0
    for role in roles:
        if role["id"] in own:
            permissions |= int(role["permissions"])
    if permissions & (1 << 3):
        raise SetupError("У бота Administrator. Уберите его и выдайте только права из README.")
    if permissions & REQUIRED != REQUIRED:
        raise SetupError("Боту не хватает прав. Требуемые права перечислены в README.")
    channels = api.request("GET", f"/guilds/{guild_id}/channels")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "guild_id": guild_id, "bot_id": me["id"], "roles": {}, "categories": {}, "channels": {}, "messages": {}}
    if state["guild_id"] != guild_id or state["bot_id"] != me["id"]:
        raise SetupError("Файл состояния относится к другому серверу или боту.")
    # Never adopt an existing object by name: it may contain different ACLs/data.
    for kind, specs, existing in (("roles", config["roles"], roles),
                                  ("categories", config["categories"], channels),
                                  ("channels", [c for cat in config["categories"] for c in cat["channels"]], channels)):
        ids = {item["id"] for item in existing}
        for spec in specs:
            known = state[kind].get(spec["key"])
            if known and known not in ids:
                raise SetupError(f"Ранее созданный объект отсутствует: {spec['name']}. Нужна проверка состояния.")
            if not known and any(item["name"] == spec["name"] for item in existing):
                raise SetupError(f"Совпадение имени: {spec['name']}. Не буду менять существующий объект.")
    save_state(state_path, state)
    for spec in config["roles"]:
        if spec["key"] not in state["roles"]:
            role = api.request("POST", f"/guilds/{guild_id}/roles", {
                "name": spec["name"], "permissions": "0", "color": spec["color"],
                "hoist": spec["key"] in {"owner", "operator", "leader", "worker"}, "mentionable": False})
            state["roles"][spec["key"]] = role["id"]
            save_state(state_path, state)
            print("Создана роль:", spec["name"])
    for category in config["categories"]:
        if category["key"] not in state["categories"]:
            acl = overwrites(category, {"readonly": True}, guild_id, me["id"], state["roles"])
            created = api.request("POST", f"/guilds/{guild_id}/channels", {
                "name": category["name"], "type": 4, "permission_overwrites": acl})
            state["categories"][category["key"]] = created["id"]
            save_state(state_path, state)
        for channel in category["channels"]:
            key = channel["key"]
            if key not in state["channels"]:
                voice = channel.get("type") == "voice"
                payload = {"name": channel["name"], "type": 2 if voice else 0,
                    "parent_id": state["categories"][category["key"]],
                    "permission_overwrites": overwrites(category, channel, guild_id, me["id"], state["roles"])}
                if voice:
                    payload["user_limit"] = channel.get("limit", 0)
                else:
                    payload["topic"] = channel.get("topic", "")
                    payload["rate_limit_per_user"] = 5
                created = api.request("POST", f"/guilds/{guild_id}/channels", payload)
                state["channels"][key] = created["id"]
                save_state(state_path, state)
                print("Создан канал:", channel["name"])
            if channel.get("message") and key not in state["messages"]:
                recent = api.request("GET", f"/channels/{state['channels'][key]}/messages?limit=50")
                already = next((m for m in recent if m.get("author", {}).get("id") == me["id"]
                                and m.get("content") == channel["message"]), None)
                sent = already or api.request("POST", f"/channels/{state['channels'][key]}/messages", {
                    "content": channel["message"], "allowed_mentions": {"parse": []},
                    "nonce": "ft:" + state["channels"][key], "enforce_nonce": True})
                state["messages"][key] = sent["id"]
                save_state(state_path, state)
    actual = {c["id"]: c for c in api.request("GET", f"/guilds/{guild_id}/channels")}
    for category in config["categories"]:
        for channel in category["channels"]:
            obj = actual.get(state["channels"][channel["key"]])
            expected = overwrites(category, channel, guild_id, me["id"], state["roles"])
            normalize = lambda entries: sorted((str(e["id"]), int(e["type"]), int(e["allow"]), int(e["deny"])) for e in entries)
            if obj is None or normalize(obj.get("permission_overwrites", [])) != normalize(expected):
                raise SetupError("Проверка прав не пройдена: " + channel["name"])
    print("Структура создана. Проверьте доступ тестовым участником и выдайте роли вручную.")
    print("Автоматизация заказов ещё не подключена. Существующие каналы/роли не изменялись.")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "server.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--guild-id")
    parser.add_argument("--expected-name")
    parser.add_argument("--invite", metavar="APPLICATION_ID")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.invite:
        if not args.invite.isdecimal():
            raise SetupError("Application ID должен состоять из цифр.")
        print("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
            "client_id": args.invite, "scope": "bot", "permissions": str(REQUIRED)}))
        return
    if not args.apply:
        print("ПЛАН БЕЗ ПОДКЛЮЧЕНИЯ К DISCORD:", config["name"])
        print("Роли:", ", ".join(role["name"] for role in config["roles"]))
        for category in config["categories"]:
            print(category["name"], "[входная зона]" if category.get("public") else "[закрыто]")
            for channel in category["channels"]:
                print("  -", channel["name"])
        print("Для применения: --apply --guild-id ID --expected-name 'Точное имя сервера'")
        return
    if not args.guild_id or not args.guild_id.isdecimal() or not args.expected_name:
        raise SetupError("Для применения обязательны --guild-id и --expected-name.")
    token = os.environ.get("DISCORD_BOT_TOKEN") or getpass.getpass("Токен БОТА (скрытый ввод, не сохраняется): ")
    if not token.strip():
        raise SetupError("Токен бота не задан.")
    provision(Discord(token.strip()), config, args.guild_id, args.expected_name,
              HERE / ("setup-state-" + args.guild_id + ".json"))


if __name__ == "__main__":
    try:
        main()
    except (SetupError, ValueError, KeyError, OSError) as exc:
        if isinstance(exc, SetupError):
            print("Остановлено:", str(exc), file=sys.stderr)
        else:
            print("Остановлено: проверьте конфигурацию и файл состояния. Секреты не выводятся.", file=sys.stderr)
        sys.exit(1)
