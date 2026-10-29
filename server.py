"""
Remote Control Server — финальная версия
HTTP (aiohttp) + WebSocket + Telegram + REST API.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rc")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SECRET_KEY = os.getenv("SECRET_KEY", "")  # НЕ хардкодить! Обязателен через переменную окружения
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://remote-server-mr8v.onrender.com")
PORT = int(os.getenv("PORT", 8080))
UPDATE_VERSION = int(os.getenv("UPDATE_VERSION", "1"))
UPDATE_EXE_URL = os.getenv("UPDATE_EXE_URL", "")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")  # separate from SECRET_KEY; falls back if unset
PANEL_URL = PUBLIC_URL.rstrip("/") + "/admin"
GROUPS_FILE = Path(os.getenv("GROUPS_FILE", "remote_groups.json"))
AUDIT_FILE = Path(os.getenv("AUDIT_FILE", "audit.log"))
MODERATOR_ALLOWED_COMMANDS = {
    c.strip().lower()
    for c in os.getenv(
        "MODERATOR_ALLOWED_COMMANDS",
        "status,sysinfo,cpu,ram,disk,uptime,battery,screenshot,screen_size,processes,help,notify",
    ).split(",")
    if c.strip()
}


def audit(actor: str, action: str, target: str = "", detail: str = "", result: str = "") -> None:
    """Append one JSON-line audit record. Never pass secrets into detail/result."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail[:200],
        "result": str(result)[:200],
    }
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("audit write failed: %s", e)


def _first_token(cmd: str) -> str:
    cmd = cmd.strip()
    return cmd.split(None, 1)[0].lower() if cmd else ""


class ClientRegistry:
    """Реестр клиентов. Активность = живой WebSocket."""

    def __init__(self):
        self.clients: dict[str, web.WebSocketResponse | None] = {}
        self.last_seen: dict[str, str] = {}
        self.history: list[dict] = []

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def is_alive(ws: web.WebSocketResponse | None) -> bool:
        if ws is None:
            return False
        try:
            return not ws.closed
        except Exception:
            return False

    async def try_send(self, name: str, payload: dict, wait_secs: float = 5.0) -> bool:
        ws = self.clients.get(name)
        if self.is_alive(ws):
            try:
                await ws.send_json(payload)
                return True
            except Exception as e:
                log.warning(f"Send to '{name}' failed: {e}")

        deadline = asyncio.get_event_loop().time() + wait_secs
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.25)
            ws = self.clients.get(name)
            if self.is_alive(ws):
                try:
                    await ws.send_json(payload)
                    return True
                except Exception:
                    break
        return False

    def register(self, name: str, ws: web.WebSocketResponse) -> None:
        self.clients[name] = ws
        self.last_seen[name] = self._now()
        active_sessions[name] = {
            "pc_name": name,
            "ws": ws,
            "tg_user_id": active_sessions.get(name, {}).get("tg_user_id"),
            "connected": True,
            "connected_at": datetime.now().isoformat(timespec="seconds"),
            "last_seen": self.last_seen[name],
        }

    def disconnect(self, name: str, ws: web.WebSocketResponse) -> None:
        if self.clients.get(name) is ws:
            self.clients[name] = None
        if active_sessions.get(name, {}).get("ws") is ws:
            active_sessions[name]["ws"] = None
            active_sessions[name]["connected"] = False

    def touch(self, name: str) -> None:
        self.last_seen[name] = self._now()
        if name in active_sessions:
            active_sessions[name]["last_seen"] = self.last_seen[name]

    def add_result(self, script: str, command: str, result: str) -> None:
        short = result[:200] if len(result) < 1000 else "[Screenshot / Binary]"
        self.history.append({
            "time": self._now(),
            "script": script,
            "command": command[:80],
            "result": short,
        })
        if len(self.history) > 200:
            self.history.pop(0)

    def all_names(self) -> set[str]:
        return set(self.clients.keys()) | set(self.last_seen.keys())

    def online_names(self) -> list[str]:
        return [n for n, w in self.clients.items() if self.is_alive(w)]


registry = ClientRegistry()
tg_app: Application | None = None

USER_COMMANDS = {
    "Power off": "shutdown",
    "Lock screen": "lock",
    "Screenshot": "screenshot",
    "Restart": "restart",
    "System info": "sysinfo",
    "Battery": "battery",
}

USER_MENU = ReplyKeyboardMarkup(
    [
        ["Screenshot", "Lock screen"],
        ["System info", "Battery"],
        ["Restart", "Power off"],
    ],
    resize_keyboard=True,
)

# pc_name -> session metadata. The WebSocket itself is still owned by ClientRegistry.
active_sessions: dict[str, dict] = {}

# tg_user_id -> pc_name
user_bindings: dict[int, str] = {}
# group_name -> set of computer names; moderator codes are one-time and expire after use.
computer_groups: dict[str, set[str]] = {}
moderator_codes: dict[str, dict] = {}
# Telegram user id -> set of group names
group_moderators: dict[int, set[str]] = {}

def load_group_state() -> None:
    global computer_groups, group_moderators
    try:
        data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        computer_groups = {str(k): set(v) for k, v in data.get("groups", {}).items()}
        group_moderators = {int(k): set(v) for k, v in data.get("moderators", {}).items()}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not load group state: %s", e)

def save_group_state() -> None:
    try:
        payload = {"groups": {k: sorted(v) for k, v in computer_groups.items()},
                   "moderators": {str(k): sorted(v) for k, v in group_moderators.items()}}
        GROUPS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("Could not save group state: %s", e)

def can_manage_group(user_id: int, group_name: str) -> bool:
    return user_id in ADMIN_IDS or group_name in group_moderators.get(user_id, set())

def user_groups(user_id: int) -> set[str]:
    if user_id in ADMIN_IDS:
        return set(computer_groups)
    return set(group_moderators.get(user_id, set()))

def group_computers(group_name: str) -> set[str]:
    return computer_groups.get(group_name, set())


def do_bind(user_id: int, pc_name: str) -> tuple[bool, str]:
    """Admin-forced bind: link a Telegram user to a computer (add/replace)."""
    if pc_name not in registry.all_names():
        return False, f"Computer '{pc_name}' not found."

    prev_pc = user_bindings.get(user_id)
    if prev_pc and prev_pc != pc_name and active_sessions.get(prev_pc, {}).get("tg_user_id") == user_id:
        active_sessions[prev_pc]["tg_user_id"] = None

    user_bindings[user_id] = pc_name
    active_sessions.setdefault(pc_name, {
        "pc_name": pc_name,
        "ws": registry.clients.get(pc_name),
        "connected": registry.is_alive(registry.clients.get(pc_name)),
        "last_seen": registry.last_seen.get(pc_name, "never"),
    })
    active_sessions[pc_name]["tg_user_id"] = user_id
    return True, f"Bound user {user_id} to {pc_name}."


def do_unbind(target: str) -> tuple[bool, str]:
    """Admin-forced unbind. `target` can be a Telegram user id or a computer name."""
    target = target.strip()
    if target.isdigit():
        uid = int(target)
        pc = user_bindings.pop(uid, None)
        if pc and active_sessions.get(pc, {}).get("tg_user_id") == uid:
            active_sessions[pc]["tg_user_id"] = None
        if pc:
            return True, f"Unbound user {uid} from {pc}."
        return False, f"User {uid} was not bound to anything."
    else:
        pc = target
        uid = active_sessions.get(pc, {}).get("tg_user_id")
        if uid:
            user_bindings.pop(uid, None)
            active_sessions[pc]["tg_user_id"] = None
            return True, f"Unbound {pc} from user {uid}."
        return False, f"{pc} had no user bound."


def do_remove(pc_name: str) -> tuple[bool, str]:
    """Remove a stale offline computer from the server registry and related bindings."""
    pc_name = pc_name.strip()
    if not pc_name:
        return False, "Computer name is required."

    if pc_name not in registry.all_names() and pc_name not in active_sessions:
        return False, f"Computer '{pc_name}' was not found."

    if registry.is_alive(registry.clients.get(pc_name)):
        return False, f"'{pc_name}' is online. /remove only removes offline entries."

    registry.clients.pop(pc_name, None)
    registry.last_seen.pop(pc_name, None)
    active_sessions.pop(pc_name, None)

    removed_users = [uid for uid, bound_pc in user_bindings.items() if bound_pc == pc_name]
    for uid in removed_users:
        user_bindings.pop(uid, None)

    return True, f"Removed '{pc_name}' from the server list. Local files were not deleted."


def _extract_image_bytes(text: str) -> bytes | None:
    """Извлечь PNG/JPEG из ответа клиента (включая [SCREENSHOT_B64])."""
    raw = text.strip()

    # Формат клиента: [SCREENSHOT_B64]1920x1080\n<base64>
    m = re.match(r"^\[SCREENSHOT_B64\][^\n]*\n(.+)$", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()

    if raw.startswith("data:image"):
        raw = raw.split(",", 1)[-1].strip()

    # OK 1920x1080\n<link> — не картинка
    if raw.startswith("OK ") and "\nhttp" in raw:
        return None

    # Чистый base64 или с префиксом PNG/JPEG
    b64 = raw
    if not re.match(r"^[A-Za-z0-9+/=]+$", b64[:80].replace("\n", "")):
        # Попробуем найти base64 после первой строки
        lines = raw.split("\n")
        for line in reversed(lines):
            line = line.strip()
            if len(line) > 200 and re.match(r"^[A-Za-z0-9+/=]+$", line[:80]):
                b64 = line
                break
        else:
            return None

    try:
        data = base64.b64decode(b64, validate=False)
        if len(data) > 100 and data[:4] in (b"\x89PNG", b"\xff\xd8\xff", b"GIF8"):
            return data
    except Exception:
        pass
    return None


async def send_telegram(chat_id: int, text: str, client_name: str = "Script") -> None:
    if not tg_app or not chat_id:
        return
    try:
        img_bytes = _extract_image_bytes(text)
        if img_bytes:
            image_file = io.BytesIO(img_bytes)
            image_file.name = f"screenshot_{client_name}.png"
            await tg_app.bot.send_photo(
                chat_id=chat_id,
                photo=image_file,
                caption=f"Screenshot from {client_name}",
            )
            return

        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks[:3]:
            await tg_app.bot.send_message(chat_id=chat_id, text=f"[{client_name}]\n{chunk}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


async def notify_admins(text: str) -> None:
    if not tg_app:
        return
    for admin_id in ADMIN_IDS:
        try:
            await tg_app.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            log.warning(f"Admin notify failed for {admin_id}: {e}")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    client_name = None
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
                mtype = data.get("type", "")

                if mtype == "register":
                    name = (data.get("name") or data.get("pc_name") or data.get("script_name") or "").strip()
                    if data.get("secret", "") != SECRET_KEY:
                        audit("client", "register", name, result="rejected: invalid secret")
                        await ws.send_json({"type": "error", "msg": "Invalid secret key"})
                        await ws.close()
                        return ws
                    if not name:
                        await ws.send_json({"type": "error", "msg": "Empty client name"})
                        await ws.close()
                        return ws
                    client_name = name
                    registry.register(name, ws)
                    audit("client", "register", name, result="ok")
                    log.info(f"+ {name} registered")
                    await ws.send_json({"type": "ok"})

                elif mtype == "result":
                    cmd = data.get("command", "")
                    result = data.get("result", "")
                    chat_id = data.get("reply_chat_id")
                    registry.add_result(client_name or "?", cmd, result)
                    if chat_id:
                        await send_telegram(int(chat_id), result, client_name or "Script")

                elif mtype == "ping" and client_name:
                    registry.touch(client_name)
            except Exception as e:
                log.warning(f"msg parse error: {e}")
    except Exception as e:
        log.warning(f"ws error ({client_name}): {e}")
    finally:
        if client_name:
            registry.disconnect(client_name, ws)
    return ws


async def tg_start_v2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(
            "Remote Control Admin\n\n"
            "Use /scripts to see connected computers (grouped online/offline).\n"
            "Use /offline or /online to see just one group.\n"
            "Use /remove <computer> to remove a stale offline entry.\n"
            "Use /send <computer> <command> to run one command.\n"
            "Use /broadcast <command> to run a command on every online computer.\n"
            "Use /group <name> <computer...> to create a group.\n"
            "Use /groups to list groups.\n"
            "Use /moderator <group> to create a one-time moderator code.\n\n"
            "Users:\n"
            "/users — list Telegram users bound to computers\n"
            "/bind <telegram_user_id> <computer> — link a user to a computer\n"
            "/unbind <telegram_user_id | computer> — remove a link\n\n"
            "Other:\n"
            "/remove <computer> — remove an offline computer from the lists\n"
            "/sessions\n"
            "/panel — web admin panel link"
        )
        return

    await update.message.reply_text(
        "Send your computer name to connect. Ask the admin if you do not know it.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def tg_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Create/update a group: /group <name> <computer1> <computer2> ..."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only. Only the main admin can create or edit groups.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: /group <group_name> <computer1> <computer2> ...\n"
            "Example: /group group1 PC-Home PC-Laptop\n"
            "Use /groups to list groups. Computer names must already have connected at least once."
        )
        return
    name = ctx.args[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        await update.message.reply_text("Group name must be 1–40 characters: letters, numbers, _ or -.")
        return
    computers = set(ctx.args[1:])
    known = registry.all_names()
    missing = sorted(computers - known)
    if missing:
        await update.message.reply_text("Unknown computer(s): " + ", ".join(missing))
        return
    computer_groups[name] = computers
    save_group_state()
    await update.message.reply_text(f"Group '{name}' saved with {len(computers)} computer(s): " + ", ".join(sorted(computers)))


async def tg_renamegroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /renamegroup <old_name> <new_name>")
        return
    old, new = ctx.args[0].strip(), ctx.args[1].strip()
    if old not in computer_groups:
        await update.message.reply_text("Group not found.")
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", new) or new in computer_groups:
        await update.message.reply_text("New name must be unique and contain 1–32 letters, numbers, _ or -.")
        return
    computer_groups[new] = computer_groups.pop(old)
    for groups in group_moderators.values():
        if old in groups:
            groups.remove(old)
            groups.add(new)
    save_group_state()
    await update.message.reply_text(f"Renamed group '{old}' to '{new}'.")


async def tg_moderators(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: /moderators <group_name>")
        return
    group_name = ctx.args[0]
    if group_name not in computer_groups:
        await update.message.reply_text("Group not found.")
        return
    members = sorted(uid for uid, groups in group_moderators.items() if group_name in groups)
    await update.message.reply_text(
        f"Moderators for '{group_name}': " + (", ".join(map(str, members)) if members else "none")
    )


async def tg_modremove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /modremove <group_name> <telegram_user_id>")
        return
    group_name = ctx.args[0]
    try:
        uid = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("Telegram user ID must be a number.")
        return
    if group_name not in computer_groups:
        await update.message.reply_text("Group not found.")
        return
    groups = group_moderators.get(uid, set())
    if group_name not in groups:
        await update.message.reply_text("That user is not a moderator of this group.")
        return
    groups.remove(group_name)
    if not groups:
        group_moderators.pop(uid, None)
    save_group_state()
    await update.message.reply_text(f"Removed moderator {uid} from '{group_name}'.")


async def tg_group_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /<group_name> <client-command>, e.g. /glicey1 shutdown."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    match = re.match(r"^/([A-Za-z0-9_-]{1,32})(?:@\w+)?(?:\s+(.+))?$", message.text.strip(), re.DOTALL)
    if not match:
        return
    group_name, command = match.group(1), (match.group(2) or "").strip()
    if group_name not in computer_groups:
        return
    if not command:
        await message.reply_text(f"Usage: /{group_name} <supported_command>")
        return
    if not can_manage_group(user.id, group_name):
        await message.reply_text("You do not have permission to control this group.")
        return
    if user.id not in ADMIN_IDS and _first_token(command) not in MODERATOR_ALLOWED_COMMANDS:
        audit(f"tg:{user.id}", "group_command", group_name, command, "denied: not in moderator allowlist")
        await message.reply_text(
            "Command is not in the moderator allowlist. Allowed: "
            + ", ".join(sorted(MODERATOR_ALLOWED_COMMANDS))
        )
        return
    # Only send a command string to clients; the client must explicitly implement it.
    targets = group_computers(group_name) & set(registry.online_names())
    sent = []
    for name in sorted(targets):
        if await registry.try_send(name, {"type": "command", "command": command,
                                          "reply_chat_id": message.chat_id}):
            sent.append(name)
    audit(f"tg:{user.id}", "group_command", group_name, command, f"sent={len(sent)}/{len(targets)}")
    await message.reply_text(
        f"Sent '{command}' to {len(sent)}/{len(targets)} online computer(s) in '{group_name}'."
        if targets else f"No computers from '{group_name}' are online."
    )


async def tg_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    allowed = user_groups(uid)
    if not allowed:
        await update.message.reply_text("No groups available. Admin: create one with /group.")
        return
    lines = ["Groups you can manage:", ""]
    for name in sorted(allowed):
        members = sorted(group_computers(name))
        lines.append(f"{name} ({len(members)} computers): " + (", ".join(members) if members else "empty"))
    await update.message.reply_text("\n".join(lines))


async def tg_moderator(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a one-time activation code for a group moderator."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: /moderator <group_name>")
        return
    group_name = ctx.args[0]
    if group_name not in computer_groups:
        await update.message.reply_text("Group not found. Create it first with /group.")
        return
    import secrets
    code = secrets.token_urlsafe(8)
    moderator_codes[code] = {"group": group_name, "created_by": update.effective_user.id}
    await update.message.reply_text(
        f"Moderator activation code for '{group_name}':\n{code}\n\n"
        "Send this code to the intended account owner. They must open this bot and send /claim CODE. "
        "The code works once; do not post it publicly."
    )


async def tg_claim(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or len(ctx.args) != 1:
        await update.message.reply_text("Usage: /claim <activation_code>")
        return
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await update.message.reply_text("You are already a main admin.")
        return
    code = ctx.args[0]
    entry = moderator_codes.pop(code, None)
    if not entry:
        await update.message.reply_text("Invalid or already-used activation code.")
        return
    group_name = entry["group"]
    if group_name not in computer_groups:
        await update.message.reply_text("That group no longer exists.")
        return
    group_moderators.setdefault(uid, set()).add(group_name)
    save_group_state()
    audit(f"tg:{uid}", "claim_moderator", group_name, result="activated")
    await update.message.reply_text(
        f"Moderator access activated for '{group_name}'. Use /groups, /scripts, /online, /offline, "
        "/send and /broadcast with this group."
    )


async def tg_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not user_groups(uid):
        await update.message.reply_text("Admin or group moderator only")
        return

    if not active_sessions:
        await update.message.reply_text("No active sessions.")
        return

    lines = ["Active sessions", ""]
    allowed_groups = user_groups(uid)
    allowed_computers = set().union(*(group_computers(g) for g in allowed_groups)) if allowed_groups else set()
    for name in sorted(active_sessions):
        if uid not in ADMIN_IDS and name not in allowed_computers:
            continue
        session = active_sessions[name]
        connected = "Online" if session.get("connected") else "Offline"
        user_id = session.get("tg_user_id") or "not bound"
        lines.append(
            f"{name}\n"
            f"Status: {connected}\n"
            f"User: {user_id}\n"
            f"Last seen: {session.get('last_seen', 'never')}\n"
        )
    await update.message.reply_text("\n".join(lines))


async def tg_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if not user_bindings:
        await update.message.reply_text("No users bound yet. Use /bind <user_id> <computer>.")
        return
    lines = ["Bound users", ""]
    for uid, pc in sorted(user_bindings.items()):
        status = "Online" if registry.is_alive(registry.clients.get(pc)) else "Offline"
        lines.append(f"{uid} → {pc} ({status})")
    await update.message.reply_text("\n".join(lines))


async def tg_bind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /bind <telegram_user_id> <computer>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("telegram_user_id must be a number.")
        return
    ok, msg = do_bind(uid, ctx.args[1])
    audit(f"tg:{update.effective_user.id}", "bind", ctx.args[1], f"user_id={uid}", msg)
    await update.message.reply_text(msg)


async def tg_unbind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /unbind <telegram_user_id | computer>")
        return
    ok, msg = do_unbind(ctx.args[0])
    audit(f"tg:{update.effective_user.id}", "unbind", ctx.args[0], result=msg)
    await update.message.reply_text(msg)


async def tg_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    pc_name = " ".join(ctx.args).strip()
    if not pc_name:
        await update.message.reply_text("Usage: /remove <computer name>")
        return
    ok, msg = do_remove(pc_name)
    audit(f"tg:{update.effective_user.id}", "remove_pc", pc_name, result=msg)
    await update.message.reply_text(msg)


async def tg_offline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not user_groups(uid):
        await update.message.reply_text("Admin or group moderator only")
        return
    online = set(registry.online_names())
    names = registry.all_names()
    if uid not in ADMIN_IDS:
        names &= set().union(*(group_computers(g) for g in user_groups(uid)))
    offline = sorted(n for n in names if n not in online)
    if not offline:
        await update.message.reply_text("All computers are online.")
        return
    lines = [f"Offline ({len(offline)})", ""]
    for n in offline:
        lines.append(f"{n} — last seen {registry.last_seen.get(n, 'never')}")
    await update.message.reply_text("\n".join(lines))


async def tg_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not user_groups(uid):
        await update.message.reply_text("Admin or group moderator only")
        return
    online_names = set(registry.online_names())
    if uid not in ADMIN_IDS:
        online_names &= set().union(*(group_computers(g) for g in user_groups(uid)))
    online = sorted(online_names)
    if not online:
        await update.message.reply_text("No computers online right now.")
        return
    lines = [f"Online ({len(online)})", ""]
    for n in online:
        uid = active_sessions.get(n, {}).get("tg_user_id")
        lines.append(f"{n}" + (f" (user {uid})" if uid else ""))
    await update.message.reply_text("\n".join(lines))


async def tg_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_id in ADMIN_IDS:
        await update.message.reply_text(
            "Admin commands: /scripts, /online, /offline, /remove, /sessions, /users, /bind, /unbind, /send, /broadcast, /panel"
        )
        return

    if user_groups(user_id):
        await update.message.reply_text(
            "Moderator commands: /groups, /scripts, /online, /offline, "
            "/send <group> <computer> <command>, /broadcast <group> <command>."
        )
        return

    bound_pc = user_bindings.get(user_id)
    if bound_pc:
        if text not in USER_COMMANDS:
            await update.message.reply_text("Choose a command from the menu.", reply_markup=USER_MENU)
            return

        cmd = USER_COMMANDS[text]
        ok = await registry.try_send(bound_pc, {
            "type": "command",
            "command": cmd,
            "reply_chat_id": update.effective_chat.id,
        })
        if ok:
            await update.message.reply_text(f"Sent: {text}", reply_markup=USER_MENU)
        else:
            await update.message.reply_text(
                f"{bound_pc} is offline right now. The admin was notified.",
                reply_markup=USER_MENU,
            )
            await notify_admins(f"User {user_id} tried to use {bound_pc}, but it is offline.")
        return

    pc_name = text
    if pc_name not in registry.all_names():
        await update.message.reply_text("Computer not found. Check the name and try again.")
        return

    existing_user = active_sessions.get(pc_name, {}).get("tg_user_id")
    if existing_user and existing_user != user_id:
        await update.message.reply_text("This computer is already linked to another Telegram user.")
        return

    user_bindings[user_id] = pc_name
    active_sessions.setdefault(pc_name, {
        "pc_name": pc_name,
        "ws": registry.clients.get(pc_name),
        "connected": registry.is_alive(registry.clients.get(pc_name)),
        "last_seen": registry.last_seen.get(pc_name, "never"),
    })
    active_sessions[pc_name]["tg_user_id"] = user_id

    await update.message.reply_text(f"Connected to {pc_name}.", reply_markup=USER_MENU)
    await notify_admins(f"User {user_id} connected to {pc_name}.")


async def tg_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not user_groups(uid):
        await update.message.reply_text("Admin or group moderator only")
        return
    args = " ".join(ctx.args)
    if not args:
        await update.message.reply_text("Usage: /send <computer> <command> (admin) or /send <group> <computer> <command> (moderator)")
        return
    parts = args.split(None, 2)
    if uid in ADMIN_IDS:
        if len(parts) < 2:
            await update.message.reply_text("Usage: /send <computer> <command>")
            return
        name, cmd = parts[0], " ".join(parts[1:])
    else:
        if len(parts) < 3:
            await update.message.reply_text("Usage: /send <group> <computer> <command>")
            return
        group_name, name, cmd = parts
        if not can_manage_group(uid, group_name) or name not in group_computers(group_name):
            await update.message.reply_text("You do not have permission to control that computer in this group.")
            return
        if _first_token(cmd) not in MODERATOR_ALLOWED_COMMANDS:
            audit(f"tg:{uid}", "send", name, cmd, "denied: not in moderator allowlist")
            await update.message.reply_text(
                "Command is not in the moderator allowlist. Allowed: "
                + ", ".join(sorted(MODERATOR_ALLOWED_COMMANDS))
            )
            return
    if name not in registry.all_names():
        await update.message.reply_text(f"Computer '{name}' was not found.")
        return
    ok = await registry.try_send(name, {
        "type": "command", "command": cmd, "reply_chat_id": update.effective_chat.id,
    })
    audit(f"tg:{uid}", "send", name, cmd, "sent" if ok else "offline")
    await update.message.reply_text(f"Sent to {name}: {cmd}" if ok else f"{name} is offline. Last seen: {registry.last_seen.get(name, 'never')}")


async def tg_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    args = " ".join(ctx.args).strip()
    if not args:
        await update.message.reply_text("Usage: /broadcast <command> (admin) or /broadcast <group> <command> (moderator)")
        return
    if uid in ADMIN_IDS:
        cmd = args
        targets = set(registry.online_names())
    else:
        parts = args.split(None, 1)
        if len(parts) != 2:
            await update.message.reply_text("Usage: /broadcast <group> <command>")
            return
        group_name, cmd = parts
        if not can_manage_group(uid, group_name):
            await update.message.reply_text("You do not have permission to manage that group.")
            return
        if _first_token(cmd) not in MODERATOR_ALLOWED_COMMANDS:
            audit(f"tg:{uid}", "broadcast", group_name, cmd, "denied: not in moderator allowlist")
            await update.message.reply_text(
                "Command is not in the moderator allowlist. Allowed: "
                + ", ".join(sorted(MODERATOR_ALLOWED_COMMANDS))
            )
            return
        targets = group_computers(group_name) & set(registry.online_names())
    sent = []
    for name in sorted(targets):
        if await registry.try_send(name, {
            "type": "command", "command": cmd, "reply_chat_id": update.effective_chat.id,
        }):
            sent.append(name)
    audit(f"tg:{uid}", "broadcast", ",".join(sorted(sent)) or "-", cmd, f"count={len(sent)}")
    await update.message.reply_text(f"Command sent to {len(sent)} computer(s): " + (", ".join(sent) if sent else "none online"))

async def tg_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not user_groups(uid):
        await update.message.reply_text("Admin or group moderator only")
        return
    names = registry.all_names()
    if uid not in ADMIN_IDS:
        names &= set().union(*(group_computers(g) for g in user_groups(uid)))
    if not names:
        await update.message.reply_text("No computers have connected yet.")
        return

    online = sorted(registry.online_names())
    offline = sorted(n for n in names if n not in online)

    lines = [f"Computers — {len(online)} online / {len(offline)} offline", ""]
    if online:
        lines.append("Online:")
        for n in online:
            uid = active_sessions.get(n, {}).get("tg_user_id")
            lines.append(f"  {n}" + (f" (user {uid})" if uid else ""))
        lines.append("")
    if offline:
        lines.append("Offline:")
        for n in offline:
            lines.append(f"  {n} — last seen {registry.last_seen.get(n, 'never')}")
    await update.message.reply_text("\n".join(lines))


async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    await update.message.reply_text(
        f"Panel: {PANEL_URL}\n"
        "The panel will ask for the admin key (ADMIN_API_KEY) on first open. "
        "Do not share the key or a link containing it."
    )


async def h_keepalive_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def h_status(request: web.Request) -> web.Response:
    online = registry.online_names()
    return web.json_response({
        "status": "ok",
        "online": online,
        "offline": [n for n in registry.all_names() if n not in online],
    })


def _check_admin(request: web.Request) -> bool:
    key = request.headers.get("X-Admin-Key", "")
    expected = ADMIN_API_KEY or SECRET_KEY
    return bool(expected) and key == expected


async def h_scripts(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    scripts = sorted([
        {
            "name": n,
            "connected": registry.is_alive(registry.clients.get(n)),
            "last_seen": registry.last_seen.get(n, "never"),
        }
        for n in registry.all_names()
    ], key=lambda x: (not x["connected"], x["name"]))
    return web.json_response({"scripts": scripts, "history": registry.history[-30:]})


async def h_users(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    users = [
        {
            "user_id": uid,
            "pc_name": pc,
            "connected": registry.is_alive(registry.clients.get(pc)),
        }
        for uid, pc in sorted(user_bindings.items())
    ]
    return web.json_response({"users": users})


async def h_bind(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        uid = int(data.get("user_id"))
        pc_name = str(data.get("pc_name", "")).strip()
    except Exception:
        return web.json_response({"error": "Bad request — need user_id (int) and pc_name"}, status=400)
    ok, msg = do_bind(uid, pc_name)
    audit("api", "bind", pc_name, f"user_id={uid}", msg)
    return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 400)


async def h_unbind(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        target = str(data.get("target", "")).strip()
    except Exception:
        return web.json_response({"error": "Bad request"}, status=400)
    if not target:
        return web.json_response({"error": "target required"}, status=400)
    ok, msg = do_unbind(target)
    audit("api", "unbind", target, result=msg)
    return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 400)


async def h_send(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)
    name = data.get("script", "").strip()
    cmd = data.get("command", "").strip()
    if not name or not cmd:
        return web.json_response({"error": "script and command required"}, status=400)
    ok = await registry.try_send(name, {
        "type": "command",
        "command": cmd,
        "reply_chat_id": data.get("reply_chat_id"),
    })
    audit("api", "send", name, cmd, "sent" if ok else "not connected")
    return web.json_response({"status": "sent"} if ok else {"error": "not connected"}, status=200 if ok else 503)


async def h_broadcast(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        cmd = data.get("command", "").strip()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)
    if not cmd:
        return web.json_response({"error": "command required"}, status=400)
    sent = []
    for name, ws in registry.clients.items():
        if registry.is_alive(ws):
            try:
                await ws.send_json({"type": "command", "command": cmd})
                sent.append(name)
            except Exception:
                pass
    audit("api", "broadcast", ",".join(sorted(sent)) or "-", cmd, f"count={len(sent)}")
    return web.json_response({"sent_to": sent, "count": len(sent)})


async def h_version(request: web.Request) -> web.Response:
    return web.json_response({"version": UPDATE_VERSION, "url": UPDATE_EXE_URL})


async def h_update(request: web.Request) -> web.Response:
    local_exe = Path(__file__).parent / "RemoteControl.exe"
    if local_exe.exists():
        return web.FileResponse(local_exe, headers={
            "Content-Disposition": "attachment; filename=RemoteControl.exe"
        })
    if UPDATE_EXE_URL:
        raise web.HTTPFound(UPDATE_EXE_URL)
    return web.json_response({"error": "No update file"}, status=404)


ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Remote Control Admin</title>
<style>
 body{background:#0d1117;color:#e6edf3;font-family:"Segoe UI",sans-serif;padding:24px;max-width:900px;margin:auto}
 h1{font-size:20px} h2{font-size:14px;color:#8b949e;margin-top:26px;text-transform:uppercase;letter-spacing:.04em}
 #summary{color:#8b949e;margin-bottom:4px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:6px;font-size:14px}
 .online{border-left:3px solid #3fb950} .offline{border-left:3px solid #f85149}
 input{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 10px;margin-right:6px}
 button{background:#161b22;color:#58a6ff;border:1px solid #30363d;border-radius:6px;padding:7px 14px;cursor:pointer}
 button:hover{border-color:#58a6ff}
 .row{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
 small{color:#8b949e}
 pre{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;font-size:12px;white-space:pre-wrap}
</style></head>
<body>
<h1>Remote Control — Admin</h1>
<div id="summary"></div>

<h2>Online</h2><div id="online"></div>
<h2>Offline</h2><div id="offline"></div>

<h2>Users</h2><div id="users"></div>
<div class="row">
  <input id="bind-uid" placeholder="Telegram user id">
  <input id="bind-pc" placeholder="Computer name">
  <button onclick="doBind()">Bind</button>
  <button onclick="doUnbind()">Unbind</button>
</div>

<h2>Send command</h2>
<div class="row">
  <input id="cmd-pc" placeholder="Computer name">
  <input id="cmd-text" placeholder="command" style="width:260px">
  <button onclick="doSend()">Send</button>
</div>
<pre id="log">ready</pre>

<script>
const KEY = new URLSearchParams(location.search).get("key") || sessionStorage.getItem("admin_key") || prompt("Enter admin key:") || "";
if (KEY) sessionStorage.setItem("admin_key", KEY);
const H = {"X-Admin-Key": KEY, "Content-Type": "application/json"};

async function refresh() {
  const r = await fetch("/api/scripts", {headers: H});
  const d = await r.json();
  if (d.error) { log(d); return; }
  const online = d.scripts.filter(s => s.connected);
  const offline = d.scripts.filter(s => !s.connected);
  document.getElementById("summary").innerText = online.length + " online / " + offline.length + " offline";
  document.getElementById("online").innerHTML = online.map(s =>
    '<div class="card online">' + s.name + '</div>').join("") || "<small>none</small>";
  document.getElementById("offline").innerHTML = offline.map(s =>
    '<div class="card offline">' + s.name + ' — last seen ' + s.last_seen + '</div>').join("") || "<small>none</small>";

  const ur = await fetch("/api/users", {headers: H});
  const ud = await ur.json();
  document.getElementById("users").innerHTML = (ud.users || []).map(u =>
    '<div class="card">user ' + u.user_id + ' &rarr; ' + u.pc_name + ' (' + (u.connected ? "online" : "offline") + ')</div>'
  ).join("") || "<small>none</small>";
}

async function doBind() {
  const uid = document.getElementById("bind-uid").value.trim();
  const pc = document.getElementById("bind-pc").value.trim();
  const r = await fetch("/api/bind", {method:"POST", headers:H, body: JSON.stringify({user_id: uid, pc_name: pc})});
  log(await r.json()); refresh();
}
async function doUnbind() {
  const pc = document.getElementById("bind-pc").value.trim();
  const uid = document.getElementById("bind-uid").value.trim();
  const r = await fetch("/api/unbind", {method:"POST", headers:H, body: JSON.stringify({target: pc || uid})});
  log(await r.json()); refresh();
}
async function doSend() {
  const pc = document.getElementById("cmd-pc").value.trim();
  const cmd = document.getElementById("cmd-text").value.trim();
  const r = await fetch("/api/send", {method:"POST", headers:H, body: JSON.stringify({script: pc, command: cmd})});
  log(await r.json());
}
function log(x){ document.getElementById("log").innerText = JSON.stringify(x, null, 2); }

refresh();
setInterval(refresh, 4000);
</script>
</body></html>"""


async def h_admin(request: web.Request) -> web.Response:
    return web.Response(text=ADMIN_HTML, content_type="text/html")


async def main() -> None:
    load_group_state()
    if not SECRET_KEY:
        log.error("FATAL: SECRET_KEY env var is empty — refusing to start (would accept any client).")
        raise SystemExit(1)
    if not ADMIN_API_KEY:
        log.warning(
            "ADMIN_API_KEY is not set — admin API falls back to SECRET_KEY. "
            "Set a separate ADMIN_API_KEY so client and admin credentials differ."
        )
    elif ADMIN_API_KEY == SECRET_KEY:
        log.warning("ADMIN_API_KEY equals SECRET_KEY — use a separate value for the admin API.")

    global tg_app
    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        for cmd, handler in [
            ("start", tg_start_v2), ("send", tg_send), ("broadcast", tg_broadcast),
            ("scripts", tg_scripts), ("online", tg_online), ("offline", tg_offline),
            ("remove", tg_remove), ("sessions", tg_sessions), ("users", tg_users), ("bind", tg_bind),
            ("unbind", tg_unbind), ("panel", tg_panel),
            ("group", tg_group), ("groups", tg_groups), ("moderator", tg_moderator), ("claim", tg_claim),
            ("renamegroup", tg_renamegroup), ("moderators", tg_moderators), ("modremove", tg_modremove),
        ]:
            tg_app.add_handler(CommandHandler(cmd, handler))
        tg_app.add_handler(MessageHandler(filters.COMMAND, tg_group_command))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_text))
        await tg_app.initialize()
        await tg_app.start()
        asyncio.create_task(tg_app.updater.start_polling())
        log.info("Telegram bot started")
    else:
        log.warning("BOT_TOKEN not set")

    app = web.Application()
    app.router.add_get("/", h_keepalive_ping)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/admin", h_admin)
    app.router.add_get("/api/status", h_status)
    app.router.add_get("/api/scripts", h_scripts)
    app.router.add_get("/api/users", h_users)
    app.router.add_post("/api/bind", h_bind)
    app.router.add_post("/api/unbind", h_unbind)
    app.router.add_post("/api/send", h_send)
    app.router.add_post("/api/broadcast", h_broadcast)
    app.router.add_get("/version", h_version)
    app.router.add_get("/update", h_update)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Server on :{PORT}  WS: {PUBLIC_URL.replace('http', 'ws')}/ws")
    try:
        await asyncio.Future()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
