"""
Remote Control Server — STABLE
================================
Fix: clients only marked offline when the WebSocket actually closes.
No timers, no guessing. If the socket is open → client is online.
"""

import asyncio, json, logging, os
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rc_server")

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_IDS  = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip())
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")
PORT       = int(os.getenv("PORT", 8080))

# KEY FIX: connected=True only while the ws coroutine is actively running.
# We flip it to False ONLY in the finally block of ws_handler.
clients = {}   # {name: {"ws": ws, "connected": bool, "last_seen": str}}
history = []   # [{time, script, command, result}]

# ── Telegram app (set later) ──
tg_app = None

async def send_telegram(chat_id: int, text: str):
    """Send result back to Telegram."""
    if tg_app and chat_id:
        try:
            # Split long messages
            for i in range(0, min(len(text), 4000), 4000):
                await tg_app.bot.send_message(chat_id=chat_id, text=text[i:i+4000])
        except Exception as e:
            log.error(f"Telegram send failed: {e}")

# =============================================================================
#  WEBSOCKET HANDLER
# =============================================================================
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)   # aiohttp sends WS ping every 30s
    await ws.prepare(request)
    client_name = None

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            try:
                data     = json.loads(msg.data)
                msg_type = data.get("type", "")

                if msg_type == "register":
                    name   = data.get("name","").strip()
                    secret = data.get("secret","")
                    if secret != SECRET_KEY:
                        await ws.send_json({"type":"error","msg":"Invalid secret key"})
                        await ws.close()
                        return
                    client_name = name
                    clients[name] = {"ws": ws, "connected": True, "last_seen": _now()}
                    log.info(f"+ REGISTERED: {name}")
                    await ws.send_json({"type":"ok"})

                elif msg_type == "result":
                    cmd     = data.get("command","")
                    result  = data.get("result","")
                    chat_id = data.get("reply_chat_id")
                    history.append({"time": _now(), "script": client_name or "?",
                                    "command": cmd[:80], "result": result[:200]})
                    if len(history) > 200:
                        history.pop(0)
                    log.info(f"  result from {client_name}: {cmd[:50]}")
                    if chat_id:
                        await send_telegram(int(chat_id), f"[{client_name}]\n{result}")

                elif msg_type == "ping":
                    if client_name and client_name in clients:
                        clients[client_name]["last_seen"] = _now()

            except Exception as e:
                log.warning(f"msg error: {e}")

    except Exception as e:
        log.warning(f"ws_handler error ({client_name}): {e}")
    finally:
        # ── Mark offline ONLY when the socket actually closes ──
        if client_name and client_name in clients:
            clients[client_name]["connected"] = False
            log.info(f"- DISCONNECTED: {client_name}")

    return ws

def _now(): return datetime.now().strftime("%H:%M:%S")

# =============================================================================
#  HTTP API
# =============================================================================
async def h_status(request):
    return web.json_response({
        "status":  "ok",
        "online":  [n for n,c in clients.items() if c["connected"]],
        "offline": [n for n,c in clients.items() if not c["connected"]],
    })

async def h_scripts(request):
    if request.headers.get("X-Admin-Key","") != SECRET_KEY:
        return web.json_response({"error":"Unauthorized"}, status=401)

    scripts = [{"name":n,"connected":c["connected"],"last_seen":c["last_seen"]}
               for n,c in clients.items()]
    scripts.sort(key=lambda x: (not x["connected"], x["name"]))
    return web.json_response({"scripts": scripts, "history": history[-30:]})

async def h_send(request):
    if request.headers.get("X-Admin-Key","") != SECRET_KEY:
        return web.json_response({"error":"Unauthorized"}, status=401)
    try: data = await request.json()
    except: return web.json_response({"error":"Bad JSON"}, status=400)

    name = data.get("script","").strip()
    cmd  = data.get("command","").strip()
    if not name or not cmd:
        return web.json_response({"error":"script and command required"}, status=400)
    if name not in clients:
        return web.json_response({"error":f"'{name}' not found"}, status=404)
    if not clients[name]["connected"]:
        return web.json_response({"error":f"'{name}' is offline"}, status=503)

    try:
        await clients[name]["ws"].send_json({
            "type":"command","command":cmd,
            "reply_chat_id": data.get("reply_chat_id"),
        })
        return web.json_response({"status":"sent"})
    except Exception as e:
        return web.json_response({"error":str(e)}, status=500)

async def h_broadcast(request):
    if request.headers.get("X-Admin-Key","") != SECRET_KEY:
        return web.json_response({"error":"Unauthorized"}, status=401)
    try: data = await request.json()
    except: return web.json_response({"error":"Bad JSON"}, status=400)

    cmd  = data.get("command","").strip()
    if not cmd: return web.json_response({"error":"command required"}, status=400)

    sent = []
    for name, info in clients.items():
        if info["connected"]:
            try:
                await info["ws"].send_json({
                    "type":"command","command":cmd,
                    "reply_chat_id": data.get("reply_chat_id"),
                })
                sent.append(name)
            except Exception as e:
                log.warning(f"broadcast to {name} failed: {e}")

    return web.json_response({"sent_to": sent, "count": len(sent)})

# =============================================================================
#  TELEGRAM BOT
# =============================================================================
async def tg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Remote Control\n\n"
        "/send <script> <cmd> — run command\n"
        "/broadcast <cmd>     — run on all\n"
        "/scripts             — list scripts\n"
        "/panel               — admin panel URL"
    )

async def tg_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only"); return

    args = " ".join(ctx.args)
    if not args:
        await update.message.reply_text("Usage: /send <script> <command>"); return

    parts = args.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /send <script> <command>"); return

    name, cmd = parts[0], parts[1]

    if name not in clients:
        await update.message.reply_text(f"Script '{name}' not found\n\nConnected: {', '.join(n for n,c in clients.items() if c['connected']) or 'none'}"); return
    if not clients[name]["connected"]:
        await update.message.reply_text(f"'{name}' is offline"); return

    try:
        await clients[name]["ws"].send_json({
            "type":          "command",
            "command":       cmd,
            "reply_chat_id": update.effective_chat.id,
        })
        await update.message.reply_text(f"Sent to {name}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def tg_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only"); return
    cmd = " ".join(ctx.args)
    if not cmd:
        await update.message.reply_text("Usage: /broadcast <command>"); return

    sent = []
    for name, info in clients.items():
        if info["connected"]:
            try:
                await info["ws"].send_json({
                    "type":"command","command":cmd,
                    "reply_chat_id": update.effective_chat.id,
                })
                sent.append(name)
            except: pass
    await update.message.reply_text(f"Broadcast to {len(sent)}: {', '.join(sent) or 'none'}")

async def tg_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not clients:
        await update.message.reply_text("No scripts have ever connected"); return
    msg = "Scripts:\n\n"
    for name, info in sorted(clients.items()):
        icon = "🟢" if info["connected"] else "🔴"
        msg += f"{icon} {name}  (last seen: {info['last_seen']})\n"
    await update.message.reply_text(msg)

async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only"); return
    await update.message.reply_text(
        f"Admin Panel:\n{PUBLIC_URL}/admin\n\nKey: {SECRET_KEY}"
    )

# =============================================================================
#  MAIN
# =============================================================================
async def main():
    global tg_app

    # Telegram bot
    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start",     tg_start))
        tg_app.add_handler(CommandHandler("send",      tg_send))
        tg_app.add_handler(CommandHandler("broadcast", tg_broadcast))
        tg_app.add_handler(CommandHandler("scripts",   tg_scripts))
        tg_app.add_handler(CommandHandler("panel",     tg_panel))
        await tg_app.initialize()
        await tg_app.start()
        asyncio.create_task(tg_app.updater.start_polling())
        log.info("Telegram bot started")
    else:
        log.warning("BOT_TOKEN not set — bot disabled")

    # Web server
    web_app = web.Application()
    web_app.router.add_get( "/ws",            ws_handler)
    web_app.router.add_get( "/api/status",    h_status)
    web_app.router.add_get( "/api/scripts",   h_scripts)
    web_app.router.add_post("/api/send",      h_send)
    web_app.router.add_post("/api/broadcast", h_broadcast)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    log.info(f"Server on port {PORT}")
    log.info(f"WS endpoint: {PUBLIC_URL.replace('http','ws')}/ws")

    try:
        await asyncio.Future()   # run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
