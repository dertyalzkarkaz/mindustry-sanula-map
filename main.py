import json
import hashlib
import os
import re
import zlib
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request, Response

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["postgresql://neondb_owner:npg_aq4w1dbDVuog@ep-aged-morning-beg51ed1-pooler.c-9.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

API = "https://api.telegram.org/bot" + BOT_TOKEN
FILE_API = "https://api.telegram.org/file/bot" + BOT_TOKEN
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

MAX_MAP_SIZE = 5 * 1024 * 1024
MAX_PREVIEW_SIZE = 2 * 1024 * 1024
REPORT_LIMIT = 3
LIST_LIMIT = 60

pool = None
http = None


def clean_dsn(url):
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunparse(p._replace(query=urlencode(q)))


SCHEMA = """
create table if not exists maps (
    id serial primary key,
    name text not null,
    author text not null,
    author_id bigint not null,
    descr text not null default '',
    data bytea not null,
    preview bytea,
    size int not null,
    downloads int not null default 0,
    reports int not null default 0,
    status text not null default 'approved',
    created timestamptz not null default now()
);
create table if not exists map_reports (
    map_id int not null,
    ip text not null,
    primary key (map_id, ip)
);
"""


@asynccontextmanager
async def lifespan(app):
    global pool, http
    pool = await asyncpg.create_pool(clean_dsn(DATABASE_URL), min_size=1, max_size=4)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    http = httpx.AsyncClient(timeout=60)
    if BASE_URL:
        await tg(
            "setWebhook",
            url=BASE_URL + "/tg",
            secret_token=SECRET,
            allowed_updates=["message", "callback_query"],
        )
    yield
    await http.aclose()
    await pool.close()


app = FastAPI(lifespan=lifespan)


async def tg(method, **params):
    try:
        r = await http.post(API + "/" + method, json=params)
        return r.json()
    except Exception:
        return {"ok": False}


async def tg_download(file_id):
    info = await tg("getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    r = await http.get(FILE_API + "/" + info["result"]["file_path"])
    if r.status_code != 200:
        return None
    return r.content


async def send_text(chat_id, text):
    await tg("sendMessage", chat_id=chat_id, text=text)


def looks_like_map(data):
    try:
        zlib.decompressobj().decompress(data, 64)
        return True
    except Exception:
        return False


def parse_caption(caption, filename):
    caption = (caption or "").strip()
    default = re.sub(r"\.msav$", "", filename, flags=re.I)[:40] or "Map"
    if not caption:
        return default, ""
    if "|" in caption:
        name, desc = caption.split("|", 1)
    else:
        name, desc = caption, ""
    return (name.strip()[:40] or default), desc.strip()[:300]


STATUS_RU = {"approved": "опубликована", "hidden": "скрыта жалобами"}

HELP = (
    "Отправь мне файл карты .msav, в подписи можно написать: Название | описание\n"
    "После файла можно прислать картинку, она станет превью карты.\n"
    "Карта сразу появится в списке карт в игре.\n\n"
    "/my — мои карты\n"
    "/del ID — удалить свою карту"
)

ADMIN_HELP = "\n\nАдмин:\n/del ID — удалить любую карту\n/list — последние карты"


async def notify_admins_new(map_id, name, author, desc, data):
    caption = "Новая карта #%d\n%s\nАвтор: %s\n%s" % (map_id, name, author, desc)
    markup = {"inline_keyboard": [[{"text": "🗑 Удалить", "callback_data": "del:%d" % map_id}]]}
    for admin in ADMIN_IDS:
        try:
            await http.post(
                API + "/sendDocument",
                data={"chat_id": str(admin), "caption": caption[:1000], "reply_markup": json.dumps(markup)},
                files={"document": ("map_%d.msav" % map_id, data, "application/octet-stream")},
            )
        except Exception:
            pass


async def handle_document(msg):
    user = msg["from"]
    chat_id = msg["chat"]["id"]
    doc = msg["document"]
    filename = doc.get("file_name") or "map.msav"
    if not filename.lower().endswith(".msav"):
        await send_text(chat_id, "Нужен файл карты с расширением .msav")
        return
    if doc.get("file_size", 0) > MAX_MAP_SIZE:
        await send_text(chat_id, "Файл слишком большой (максимум 5 МБ)")
        return
    data = await tg_download(doc["file_id"])
    if data is None:
        await send_text(chat_id, "Не удалось скачать файл, попробуй ещё раз")
        return
    if not looks_like_map(data):
        await send_text(chat_id, "Это не похоже на файл карты Mindustry")
        return
    name, desc = parse_caption(msg.get("caption"), filename)
    author = user.get("username") or user.get("first_name") or str(user["id"])
    async with pool.acquire() as c:
        map_id = await c.fetchval(
            "insert into maps (name, author, author_id, descr, data, size) values ($1,$2,$3,$4,$5,$6) returning id",
            name, author, user["id"], desc, data, len(data),
        )
    await send_text(chat_id, "Карта «%s» (#%d) опубликована. Можешь прислать картинку для превью." % (name, map_id))
    await notify_admins_new(map_id, name, author, desc, data)


async def handle_photo(msg):
    user = msg["from"]
    chat_id = msg["chat"]["id"]
    photo = msg["photo"][-1]
    if photo.get("file_size", 0) > MAX_PREVIEW_SIZE:
        await send_text(chat_id, "Картинка слишком большая")
        return
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select id from maps where author_id=$1 and preview is null order by id desc limit 1", user["id"]
        )
        if row is None:
            await send_text(chat_id, "Сначала отправь файл карты .msav")
            return
        data = await tg_download(photo["file_id"])
        if data is None:
            await send_text(chat_id, "Не удалось скачать картинку")
            return
        await c.execute("update maps set preview=$1 where id=$2", data, row["id"])
    await send_text(chat_id, "Превью добавлено к карте #%d" % row["id"])


async def handle_command(msg):
    user_id = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    is_admin = user_id in ADMIN_IDS

    if cmd in ("/start", "/help"):
        await send_text(chat_id, HELP + (ADMIN_HELP if is_admin else ""))
    elif cmd == "/my":
        async with pool.acquire() as c:
            rows = await c.fetch("select id, name, status, downloads from maps where author_id=$1 order by id desc limit 30", user_id)
        if not rows:
            await send_text(chat_id, "У тебя пока нет карт")
            return
        await send_text(chat_id, "\n".join("#%d %s — %s, скачано: %d" % (r["id"], r["name"], STATUS_RU.get(r["status"], r["status"]), r["downloads"]) for r in rows))
    elif cmd == "/del":
        if not arg.strip().isdigit():
            await send_text(chat_id, "Формат: /del ID")
            return
        mid = int(arg.strip())
        async with pool.acquire() as c:
            if is_admin:
                res = await c.execute("delete from maps where id=$1", mid)
            else:
                res = await c.execute("delete from maps where id=$1 and author_id=$2", mid, user_id)
        await send_text(chat_id, "Удалено" if res.endswith("1") else "Карта не найдена или она не твоя")
    elif cmd == "/list" and is_admin:
        async with pool.acquire() as c:
            rows = await c.fetch("select id, name, author, status from maps order by id desc limit 30")
        if not rows:
            await send_text(chat_id, "Пусто")
            return
        await send_text(chat_id, "\n".join("#%d %s (%s)" % (r["id"], r["name"], r["author"]) for r in rows))
    else:
        await send_text(chat_id, HELP)


async def handle_callback(cb):
    user_id = cb["from"]["id"]
    if user_id not in ADMIN_IDS:
        await tg("answerCallbackQuery", callback_query_id=cb["id"], text="Нет доступа")
        return
    action, _, sid = cb.get("data", "").partition(":")
    if action != "del" or not sid.isdigit():
        await tg("answerCallbackQuery", callback_query_id=cb["id"])
        return
    mid = int(sid)
    async with pool.acquire() as c:
        res = await c.execute("delete from maps where id=$1", mid)
    await tg("answerCallbackQuery", callback_query_id=cb["id"], text="Удалено" if res.endswith("1") else "Карта не найдена")
    msg = cb.get("message")
    if msg:
        await tg("editMessageCaption", chat_id=msg["chat"]["id"], message_id=msg["message_id"], caption="#%d удалена" % mid)


@app.post("/tg")
async def telegram_webhook(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != SECRET:
        raise HTTPException(403)
    upd = await request.json()
    try:
        if "callback_query" in upd:
            await handle_callback(upd["callback_query"])
        elif "message" in upd:
            msg = upd["message"]
            if msg["chat"].get("type") != "private" or "from" not in msg:
                return {"ok": True}
            if "document" in msg:
                await handle_document(msg)
            elif "photo" in msg:
                await handle_photo(msg)
            elif msg.get("text"):
                await handle_command(msg)
    except Exception:
        pass
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/maps")
async def list_maps(sort: str = "new", q: str = ""):
    order = "downloads desc, id desc" if sort == "top" else "id desc"
    like = "%" + q.strip().replace("%", "").replace("_", "") + "%"
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select id, name, author, descr, size, downloads, (preview is not null) as has_preview "
            "from maps where status='approved' and (name ilike $1 or author ilike $1) "
            "order by " + order + " limit $2",
            like, LIST_LIMIT,
        )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "author": r["author"],
            "desc": r["descr"],
            "size": r["size"],
            "downloads": r["downloads"],
            "preview": r["has_preview"],
        }
        for r in rows
    ]


@app.get("/maps/{map_id}/file")
async def map_file(map_id: int):
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "update maps set downloads=downloads+1 where id=$1 and status='approved' returning data", map_id
        )
    if row is None:
        raise HTTPException(404)
    return Response(bytes(row["data"]), media_type="application/octet-stream")


@app.get("/maps/{map_id}/preview")
async def map_preview(map_id: int):
    async with pool.acquire() as c:
        row = await c.fetchrow("select preview from maps where id=$1 and status='approved'", map_id)
    if row is None or row["preview"] is None:
        raise HTTPException(404)
    return Response(bytes(row["preview"]), media_type="image/jpeg")


@app.post("/maps/{map_id}/report")
async def map_report(map_id: int, request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    async with pool.acquire() as c:
        exists = await c.fetchval("select 1 from maps where id=$1 and status='approved'", map_id)
        if not exists:
            raise HTTPException(404)
        added = await c.execute("insert into map_reports (map_id, ip) values ($1,$2) on conflict do nothing", map_id, ip)
        if added.endswith("1"):
            row = await c.fetchrow("update maps set reports=reports+1 where id=$1 returning reports, name", map_id)
            if row["reports"] >= REPORT_LIMIT:
                await c.execute("update maps set status='hidden' where id=$1", map_id)
                for admin in ADMIN_IDS:
                    await send_text(admin, "Карта #%d «%s» скрыта: %d жалоб. Удалить: /del %d" % (map_id, row["name"], row["reports"], map_id))
    return {"ok": True}
