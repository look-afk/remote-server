"""
Запусти один раз рядом с server.py (в папке remote-server):
  python server_patch.py

Добавляет в server.py два эндпоинта:
  POST /upload  — принимает файл от авторизованного клиента (X-Secret: SECRET_KEY),
                  хранит в памяти до 15 мин или до первого скачивания.
  GET  /dl/<token> — отдаёт файл один раз (single-use ссылка), без авторизации.

После этого клиент будет заливать скриншоты и файлы на твой сервер,
а не на file.io / 0x0.st / transfer.sh.
"""

from pathlib import Path
import sys

SERVER = Path("server.py")
if not SERVER.exists():
    print(f"ERROR: {SERVER} not found — run from the remote-server folder")
    sys.exit(1)

src = SERVER.read_text(encoding="utf-8")

def patch(s, old, new, label):
    if old not in s:
        print(f"  SKIP (already applied or not found): {label}")
        return s
    print(f"  OK: {label}")
    return s.replace(old, new, 1)

# 1. import time
src = patch(src,
    "from datetime import datetime",
    "import time as _time_mod\nfrom datetime import datetime",
    "add time import")

# 2. _uploads store after AUDIT_FILE
src = patch(src,
    'AUDIT_FILE = Path(os.getenv("AUDIT_FILE", "audit.log"))',
    '''AUDIT_FILE = Path(os.getenv("AUDIT_FILE", "audit.log"))

# ── Temporary file store for /upload ─────────────────────────────────────────
_UPLOAD_MAX_BYTES = 20 * 1024 * 1024   # 20 MB per file
_UPLOAD_MAX_SLOTS = 100                # max files in memory at once
_UPLOAD_TTL_SECS  = 15 * 60           # auto-delete after 15 min
_uploads: dict[str, dict] = {}        # token -> {data, filename, expires, size}''',
    "add _uploads store")

# 3. handler functions before h_admin
HANDLERS = '''async def _upload_cleanup() -> None:
    """Background task: remove expired uploads every 60 s."""
    while True:
        await asyncio.sleep(60)
        now = _time_mod.time()
        expired = [t for t, u in _uploads.items() if u["expires"] < now]
        for t in expired:
            _uploads.pop(t, None)
        if expired:
            log.debug(f"Upload cleanup: removed {len(expired)} expired slot(s)")


async def h_upload(request: web.Request) -> web.Response:
    """
    POST /upload  — authenticated with X-Secret: <SECRET_KEY>.
    Accepts multipart/form-data field named 'file'.
    Returns {"url": "https://server/dl/<token>", "expires_in": 900}
    Files live in memory until first download or 15 min (no Render disk needed).
    """
    if request.headers.get("X-Secret", "") != SECRET_KEY or not SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "Missing 'file' field"}, status=400)
        filename = field.filename or "file.bin"
        chunks, total = [], 0
        async for chunk in field:
            total += len(chunk)
            if total > _UPLOAD_MAX_BYTES:
                return web.json_response({"error": f"File too large (max {_UPLOAD_MAX_BYTES // 1048576} MB)"}, status=413)
            chunks.append(chunk)
        data = b"".join(chunks)
    except Exception as e:
        log.warning(f"/upload read error: {e}")
        return web.json_response({"error": "Bad request"}, status=400)
    if len(_uploads) >= _UPLOAD_MAX_SLOTS:
        oldest = min(_uploads, key=lambda t: _uploads[t]["expires"])
        _uploads.pop(oldest, None)
    token = _secrets.token_urlsafe(16)
    _uploads[token] = {"data": data, "filename": filename,
                       "expires": _time_mod.time() + _UPLOAD_TTL_SECS, "size": len(data)}
    url = f"{PUBLIC_URL.rstrip('/')}/dl/{token}"
    log.info(f"Upload: {filename} ({len(data)} bytes) -> {url}")
    return web.json_response({"url": url, "expires_in": _UPLOAD_TTL_SECS})


async def h_download(request: web.Request) -> web.Response:
    """GET /dl/{token} — single-use, no auth. Deletes on download."""
    token = request.match_info.get("token", "")
    entry = _uploads.pop(token, None)
    if entry is None or entry["expires"] < _time_mod.time():
        return web.json_response({"error": "Not found or expired"}, status=404)
    return web.Response(
        body=entry["data"],
        headers={
            "Content-Disposition": f'attachment; filename="{entry["filename"]}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(entry["size"]),
        },
    )


'''

src = patch(src,
    "async def h_admin(request: web.Request) -> web.Response:",
    HANDLERS + "async def h_admin(request: web.Request) -> web.Response:",
    "add h_upload and h_download handlers")

# 4. Register routes
src = patch(src,
    '    app.router.add_get("/update", h_update)',
    '    app.router.add_get("/update", h_update)\n    app.router.add_post("/upload", h_upload)\n    app.router.add_get("/dl/{token}", h_download)',
    "register /upload and /dl routes")

# 5. Start cleanup task
src = patch(src,
    "    runner = web.AppRunner(app)",
    "    asyncio.create_task(_upload_cleanup())\n    runner = web.AppRunner(app)",
    "start _upload_cleanup task")

SERVER.write_text(src, encoding="utf-8")
print(f"\nSaved {SERVER}")
print("\nDone. Now:")
print("  git add server.py")
print("  git commit -m 'feat: add /upload + /dl endpoints (own-server file storage)'")
print("  git push")