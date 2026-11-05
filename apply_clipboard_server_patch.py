"""Патч server.py: фото/файлы из буфера в Telegram, /clip между ПК, фото из Telegram -> буфер ПК."""
import ast, re, shutil, sys
from pathlib import Path


class Patcher:
    """Точные замены. Ничего не пишет на диск, если хоть один якорь не найден или результат не компилируется."""

    def __init__(self, src: str):
        self.src, self.errors, self.done = src, [], []

    def sub(self, label, old, new):
        if new in self.src:
            self.done.append(f"= {label} (уже применено)"); return
        n = self.src.count(old)
        if n != 1:
            self.errors.append(f"x {label}: якорь найден {n} раз (нужен ровно 1)"); return
        self.src = self.src.replace(old, new); self.done.append(f"+ {label}")

    def before(self, label, anchor, block):
        self.sub(label, anchor, block + anchor)

    def rx(self, label, pattern, new, flags=re.M, done_marker=None):
        if (done_marker or new.strip()) in self.src:
            self.done.append(f"= {label} (уже применено)"); return
        out, n = re.subn(pattern, lambda m: new, self.src, count=1, flags=flags)
        if n != 1:
            self.errors.append(f"x {label}: шаблон не найден"); return
        self.src = out; self.done.append(f"+ {label}")

    def func(self, name, new_code):
        self.rx(f"функция {name}", rf"^(?:async )?def {name}\(.*?\n(?:(?:[ \t].*)?\n)*", new_code.rstrip() + "\n\n\n",
                done_marker=new_code.strip())


def run(apply):
    if len(sys.argv) != 2:
        print(f"Использование: python {Path(sys.argv[0]).name} <путь к файлу>"); sys.exit(2)
    path = Path(sys.argv[1])
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    p = Patcher(raw.replace("\r\n", "\n"))
    apply(p)
    print("\n".join(p.done + p.errors))
    if p.errors:
        print("\nНИЧЕГО НЕ ЗАПИСАНО: часть якорей не найдена (файл отличается от ожидаемого). Пришлите вывод выше."); sys.exit(1)
    try:
        ast.parse(p.src)
    except SyntaxError as e:
        print(f"\nНИЧЕГО НЕ ЗАПИСАНО: результат не компилируется: {e}"); sys.exit(1)
    shutil.copy2(path, str(path) + ".bak")
    path.write_bytes((p.src.replace("\n", "\r\n") if crlf else p.src).encode("utf-8"))
    print(f"\nГотово. Резервная копия: {path}.bak")


SEND_TG_BLOCK = r'''        # Свои одноразовые ссылки (скриншот / буфер / файл) отдаём в чат САМИМ содержимым:
        # картинка приходит фото, файл - документом, а ссылку не успевает "сжечь" предпросмотр Telegram.
        stripped = text.strip()
        m_img = _OWN_IMG_RE.match(stripped)
        m_file = None if m_img else _OWN_FILE_RE.match(stripped)
        if m_img or m_file:
            got = _take_upload(m_img.group(3) if m_img else m_file.group(2))
            if got:
                data, fname = got
                bio = io.BytesIO(data)
                bio.name = fname or "file.bin"
                if m_img:
                    cap = f"{m_img.group(4) or 'Screenshot'} - {client_name}"[:200]
                    try:
                        await tg_app.bot.send_photo(chat_id=chat_id, photo=bio, caption=cap)
                    except Exception:   # напр. слишком большое фото для Telegram: отправляем оригинал файлом
                        bio.seek(0)
                        await tg_app.bot.send_document(chat_id=chat_id, document=bio, filename=bio.name, caption=cap)
                else:
                    await tg_app.bot.send_document(chat_id=chat_id, document=bio, filename=bio.name,
                                                   caption=f"{fname} - {client_name}"[:200])
                return

'''

WS_BLOCK = r'''                    if registered and isinstance(result, str) and result.startswith("[CLIPSEND]"):
                        await handle_clip_send(client_name or "?", chat_id, result)
                        continue
'''

SERVER_BLOCK = r'''# ── Clipboard hub: ПК <-> ПК и Telegram <-> ПК (только для админов) ──────────
_OWN_IMG_RE  = re.compile(r"^OK (\d+)x(\d+)\n(\S+)(?:\n([^\n]*))?$")
_OWN_FILE_RE = re.compile(r"^Ready!\n(.+) \([^\n]*\)\n(\S+)$")
_DL_URL_RE   = re.compile(r"^https?://[\w.\-:]+(?:/[\w.\-]+)*/dl/([\w\-]+)$")
_clip_targets: dict[int, str] = {}   # id админа -> компьютер по умолчанию для фото/файлов, отправленных боту


def _take_upload(url: str):
    """Если url - одна из НАШИХ одноразовых ссылок: забрать данные из памяти. -> (data, filename) | None"""
    prefix = PUBLIC_URL + "/dl/"
    if not url.startswith(prefix):
        return None
    entry = _uploads.pop(url[len(prefix):], None)
    if entry is None or entry["expires"] < _time_mod.time():
        return None
    return entry["data"], entry["filename"]


def _store_upload(data: bytes, filename: str) -> str:
    if len(_uploads) >= _UPLOAD_MAX_SLOTS:
        _uploads.pop(min(_uploads, key=lambda t: _uploads[t]["expires"]), None)
    token = _secrets.token_urlsafe(16)
    _uploads[token] = {"data": data, "filename": filename,
                       "expires": _time_mod.time() + _UPLOAD_TTL_SECS, "size": len(data)}
    return token


async def handle_clip_send(src: str, chat_id, result: str) -> None:
    """Компьютер `src` ответил на `clip_send <dst>` ссылкой на загрузку: передаём её на `dst`.
    Выполняется только если команда пришла из админского чата."""
    body = result[len("[CLIPSEND]"):]
    dst, kind, url, summary = (body.split("\n", 3) + ["", "", "", ""])[:4]
    dst, kind, url = dst.strip(), kind.strip(), url.strip()
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = 0
    m = _DL_URL_RE.match(url)
    token = m.group(1) if m else ""
    if cid not in ADMIN_IDS or not m or not url.startswith(PUBLIC_URL + "/dl/") or kind not in ("text", "image", "file"):
        audit("client", "clip_send_denied", f"{src}->{dst}", result="not started by an admin chat / bad link")
        _uploads.pop(token, None)
        return
    if dst not in registry.online_names():
        _uploads.pop(token, None)
        await send_telegram(cid, f"{dst} is offline - nothing was copied.", "PCRC")
        return
    ok = await registry.try_send(dst, {"type": "command", "command": f"clip_put {url} {kind}",
                                       "reply_chat_id": cid}, wait_secs=0.5)
    audit(f"tg:{cid}", "clip_send", f"{src}->{dst}", summary[:80], "sent" if ok else "offline")
    if not ok:
        _uploads.pop(token, None)
        await send_telegram(cid, f"Could not reach {dst}.", "PCRC")


async def _clip_transfer(chat_id: int, src: str, dst: str) -> str:
    online = set(registry.online_names())
    if src == dst:
        return "Choose two different computers."
    for n in (src, dst):
        if n not in online:
            return f"'{n}' is offline or unknown."
    ok = await registry.try_send(src, {"type": "command", "command": f"clip_send {dst}", "reply_chat_id": chat_id})
    audit(f"tg:{chat_id}", "clip_transfer", f"{src}->{dst}", result="sent" if ok else "offline")
    return f"Copying clipboard {src} -> {dst} ..." if ok else f"{src} went offline."


async def tg_clip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/clip - выбрать два компьютера кнопками; /clip <from> <to> - скопировать буфер <from> на <to>."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) == 2:
        await update.message.reply_text(await _clip_transfer(update.effective_chat.id, ctx.args[0], ctx.args[1]))
        return
    online = sorted(registry.online_names())
    if len(online) < 2:
        await update.message.reply_text("Need at least two online computers. Usage: /clip <from> <to>")
        return
    ctx.user_data["clip_online"] = online
    ctx.user_data.pop("clip_src", None)
    keyboard = [[InlineKeyboardButton(n, callback_data=f"cs:{i}")] for i, n in enumerate(online)]
    await update.message.reply_text("Copy the clipboard FROM:", reply_markup=InlineKeyboardMarkup(keyboard))


async def tg_clip_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Admin only", show_alert=True)
        return
    await q.answer()
    online = ctx.user_data.get("clip_online") or []
    kind, _, rest = (q.data or "").partition(":")
    try:
        if kind == "cs":
            src = online[int(rest)]
            ctx.user_data["clip_src"] = src
            keyboard = [[InlineKeyboardButton(n, callback_data=f"cd:{j}")] for j, n in enumerate(online) if n != src]
            await q.edit_message_text(f"From {src}\nCopy TO:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif kind == "cd":
            dst = online[int(rest)]
            src = ctx.user_data.get("clip_src")
            if not src:
                raise IndexError
            await q.edit_message_text(await _clip_transfer(q.message.chat_id, src, dst))
    except (ValueError, IndexError):
        await q.edit_message_text("Selection expired. Send /clip again.")


async def tg_clipto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/clipto <computer> - фото и файлы, которые вы отправите боту, попадут в буфер этого компьютера."""
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if not ctx.args:
        cur = _clip_targets.get(uid)
        await update.message.reply_text(
            (f"Photos and files you send me go to: {cur}\n" if cur else "No default computer set.\n")
            + "Usage: /clipto <computer> | /clipto off\n"
              "Or add the caption  /clip <computer>  to a single photo/file.")
        return
    arg = ctx.args[0]
    if arg.lower() in ("off", "none", "-"):
        _clip_targets.pop(uid, None)
        await update.message.reply_text("Default clipboard computer cleared.")
    elif arg not in registry.all_names():
        await update.message.reply_text(f"Computer '{arg}' was not found.")
    else:
        _clip_targets[uid] = arg
        await update.message.reply_text(
            f"Photos and files you send me now go to the clipboard of {arg}.\n"
            "Send them as File (not Photo) to keep the original quality. /clipto off to stop.")


async def tg_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото/файл от админа -> в буфер компьютера (по подписи '/clip <pc>' или по /clipto)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or user.id not in ADMIN_IDS:
        return
    m = re.match(r"^/?clip(?:@\w+)?\s+(\S+)", (msg.caption or "").strip(), re.I)
    target = m.group(1) if m else _clip_targets.get(user.id)
    if not target:
        return
    if target not in registry.online_names():
        await msg.reply_text(f"'{target}' is offline or unknown.")
        return
    media = msg.photo[-1] if msg.photo else msg.document
    if media is None:
        return
    fname = "telegram_photo.jpg" if msg.photo else (getattr(media, "file_name", None) or "file.bin")
    fname = re.sub(r"[^\w.\- ]", "_", Path(fname).name).lstrip(". ")
    fname = re.sub(r"\.{2,}", "_", fname)[:100] or "file.bin"
    if (media.file_size or 0) > _UPLOAD_MAX_BYTES:
        await msg.reply_text(f"Too large (max {_UPLOAD_MAX_BYTES // 1048576} MB).")
        return
    tg_file = await ctx.bot.get_file(media.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    if len(data) > _UPLOAD_MAX_BYTES:
        await msg.reply_text(f"Too large (max {_UPLOAD_MAX_BYTES // 1048576} MB).")
        return
    token = _store_upload(data, fname)
    ok = await registry.try_send(target, {"type": "command",
                                          "command": f"clip_put {PUBLIC_URL}/dl/{token} auto",
                                          "reply_chat_id": msg.chat_id})
    audit(f"tg:{user.id}", "clip_media", target, f"{fname} {len(data)} bytes", "sent" if ok else "offline")
    if not ok:
        _uploads.pop(token, None)
        await msg.reply_text(f"{target} is offline.")


'''

START_OLD = '            "/panel — web admin panel link"\n        )'
START_NEW = ('            "/panel — web admin panel link\\n\\n"\n'
             '            "Clipboard:\\n"\n'
             '            "/clip — pick two computers and copy the clipboard from one to the other\\n"\n'
             '            "/clip <from> <to> — the same in one command\\n"\n'
             '            "/clipto <computer> — photos and files you send me go to that computer\'s clipboard\\n"\n'
             '            "(or put the caption /clip <computer> on a single photo or file)"\n'
             '        )')


def apply(p):
    p.sub("импорт: inline-кнопки",
          "from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update",
          "from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update")
    p.sub("импорт: CallbackQueryHandler",
          "from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters",
          "from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters")
    p.before("send_telegram: фото и файлы из своих ссылок",
             "        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]\n", SEND_TG_BLOCK)
    p.before("ws: передача буфера на другой ПК",
             '                    registry.add_result(client_name or "?", cmd, result)\n', WS_BLOCK)
    p.before("команды /clip, /clipto, фото из Telegram",
             "async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:\n", SERVER_BLOCK)
    p.sub("регистрация /clip и /clipto",
          '            ("pair", tg_pair),\n        ]:',
          '            ("pair", tg_pair),\n            ("clip", tg_clip), ("clipto", tg_clipto),\n        ]:')
    p.before("регистрация кнопок и приёма фото/файлов",
             "        tg_app.add_handler(MessageHandler(filters.COMMAND, tg_group_command))\n",
             '        tg_app.add_handler(CallbackQueryHandler(tg_clip_cb, pattern=r"^c[sd]:"))\n'
             '        tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, tg_media))\n')
    p.sub("/start: подсказка про буфер", START_OLD, START_NEW)


if __name__ == "__main__":
    run(apply)
