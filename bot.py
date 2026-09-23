"""
Chek Bot — Telegram kanal/guruhdagi to'lov cheklarini AI bilan o'qib,
Notion "Cheklar" bazasiga yozadi.

Oqim:
  chek tushadi -> fayl yuklanadi -> AI to'liq o'qiydi -> JSON ajratiladi
  -> dublikat tekshiriladi -> Notion'ga yoziladi -> botning javobi
"""

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# Sozlamalar (Railway environment variables)
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

TZ = timezone(timedelta(hours=5))  # Asia/Tashkent
MAX_LIST_ROWS = 30

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("chek-bot")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

create_lock = asyncio.Lock()
pending_fix: dict[int, str] = {}  # admin user_id -> notion page_id


# ----------------------------------------------------------------------------
# Yordamchi funksiyalar
# ----------------------------------------------------------------------------

def money(value) -> str:
    """450000 -> '450 000'"""
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def short_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m")
    except ValueError:
        return iso[:10]


def txt(prop: dict) -> str:
    """Notion rich_text propertydan matn oladi."""
    parts = prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts)


# ----------------------------------------------------------------------------
# AI: chekni o'qish
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """You read payment receipts from Uzbekistan (bank apps, payment
services, PDF receipts). Receipts come from many different banks and apps, in
Uzbek, Russian or English, in any layout.

Work in two steps and output both.

STEP 1 — Read everything. Inside <transkript></transkript>, transcribe every
piece of text you can see on the receipt, line by line, exactly as written,
including labels, amounts, names, card numbers, button captions, status text and
any timestamps. Do not interpret yet, just read.

STEP 2 — Extract. Inside <json></json>, output a single JSON object with exactly
these keys:

{
  "chek_emas": boolean,        // true if this is not a payment receipt at all
  "holat": string,             // "muvaffaqiyatli" | "tasdiqlanmagan" | "muvaffaqiyatsiz"
  "summa": number|null,        // amount that reached the RECIPIENT
  "komissiya": number|null,    // commission amount in money (not percent)
  "bank": string|null,         // app or bank name as shown
  "sana": string|null,         // "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" if no time
  "yuboruvchi": string|null,   // sender full name
  "yuboruvchi_kartasi": string|null,  // sender card, as shown
  "qabul_kartasi": string|null,       // recipient card, as shown
  "tranzaksiya_id": string|null,      // transaction / operation / payment code
  "ishonch": number,           // 0.0-1.0 your confidence in these fields
  "izoh": string|null          // short note in Uzbek if something is unclear
}

Rules:
- "summa" is the amount the recipient receives. If the receipt shows both an
  amount and a larger "with commission" / "total debited" figure, take the
  smaller one (the transfer amount) and put the difference in "komissiya".
- If commission is shown only as a percent, compute the money amount if the
  base amount is visible; otherwise use null.
- Amounts are plain numbers: "450 000,00 UZS" -> 450000, "58 290,00 so'm" -> 58290.
- If the screen is a pre-transfer confirmation form (a button like "O'tkazish",
  "Перевести", "Confirm" is still waiting to be pressed, or the text asks to
  check the data), set "holat" to "tasdiqlanmagan" — money may not have moved.
- If a field is not visible, use null. Never invent, never guess a year that is
  not shown.
- "ishonch" below 0.8 means a human should check it. Be honest: blurry photos,
  cropped amounts and tiny text lower confidence.
- Output nothing outside the two tags."""


def call_claude(file_bytes: bytes, media_type: str) -> dict:
    """Bitta so'rovda: to'liq o'qish + JSON ajratish."""
    b64 = base64.standard_b64encode(file_bytes).decode()

    if media_type == "application/pdf":
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }
    else:
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [block, {"type": "text", "text": "Read this receipt."}],
            }
        ],
    }

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))

    match = re.search(r"<json>(.*?)</json>", text, re.S)
    if not match:
        raise ValueError(f"JSON topilmadi. AI javobi: {text[:400]}")

    data = json.loads(match.group(1).strip())
    transcript = re.search(r"<transkript>(.*?)</transkript>", text, re.S)
    data["_transkript"] = transcript.group(1).strip() if transcript else ""
    return data


# ----------------------------------------------------------------------------
# Notion
# ----------------------------------------------------------------------------

def notion_query(filter_: dict | None = None, sorts: list | None = None, limit: int = 100):
    body: dict = {"page_size": min(limit, 100)}
    if filter_:
        body["filter"] = filter_
    if sorts:
        body["sorts"] = sorts

    results, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=NOTION_HEADERS,
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results.extend(data["results"])
        if not data.get("has_more") or len(results) >= limit:
            return results[:limit]
        cursor = data["next_cursor"]


def notion_create(props: dict) -> dict:
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={
            "parent": {"type": "database_id", "database_id": NOTION_DB_ID},
            "properties": props,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def notion_update(page_id: str, props: dict) -> dict:
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"properties": props},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def notion_upload_file(file_bytes: bytes, filename: str, content_type: str) -> str | None:
    """Faylni Notion'ga yuklaydi, file_upload id qaytaradi. Xato bo'lsa None."""
    try:
        r = requests.post(
            "https://api.notion.com/v1/file_uploads",
            headers=NOTION_HEADERS,
            json={"filename": filename, "content_type": content_type},
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
        send_url = info.get("upload_url") or (
            f"https://api.notion.com/v1/file_uploads/{info['id']}/send"
        )

        r2 = requests.post(
            send_url,
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
            },
            files={"file": (filename, file_bytes, content_type)},
            timeout=120,
        )
        r2.raise_for_status()
        return info["id"]
    except Exception as exc:  # fayl biriktirilmasa ham qator yozilaveradi
        log.warning("Notion fayl yuklash xatosi: %s", exc)
        return None


def next_tartib() -> int:
    rows = notion_query(
        sorts=[{"property": "Tartib", "direction": "descending"}], limit=1
    )
    if not rows:
        return 1
    current = rows[0]["properties"].get("Tartib", {}).get("number")
    return int(current or 0) + 1


def find_duplicate(data: dict) -> dict | None:
    tid = (data.get("tranzaksiya_id") or "").strip()
    if tid:
        rows = notion_query(
            {"property": "Tranzaksiya ID", "rich_text": {"equals": tid}}, limit=1
        )
        return rows[0] if rows else None

    summa, sana = data.get("summa"), data.get("sana")
    if summa is None or not sana:
        return None

    conditions = [
        {"property": "Summa", "number": {"equals": float(summa)}},
        {"property": "Tolov sanasi", "date": {"equals": sana[:10]}},
    ]
    karta = (data.get("yuboruvchi_kartasi") or "").strip()
    if karta:
        conditions.append(
            {"property": "Yuboruvchi kartasi", "rich_text": {"equals": karta}}
        )

    rows = notion_query({"and": conditions}, limit=1)
    return rows[0] if rows else None


def find_by_tg_id(tg_id: int) -> dict | None:
    rows = notion_query(
        {"property": "TG_ID", "rich_text": {"equals": str(tg_id)}}, limit=1
    )
    return rows[0] if rows else None


def build_props(data: dict, meta: dict, status: str, tartib: int | None) -> dict:
    """AI natijasi + Telegram metadata -> Notion properties."""
    def rt(value):
        return {"rich_text": [{"text": {"content": str(value)[:1900]}}] if value else []}

    props: dict = {
        "Name": {
            "title": [
                {
                    "text": {
                        "content": f"#{tartib} — {money(data.get('summa'))} so'm"
                        if tartib
                        else money(data.get("summa"))
                    }
                }
            ]
        },
        "Summa": {"number": data.get("summa")},
        "Komissiya": {"number": data.get("komissiya")},
        "Status": {"select": {"name": status}},
        "Yuboruvchi": rt(data.get("yuboruvchi")),
        "Yuboruvchi kartasi": rt(data.get("yuboruvchi_kartasi")),
        "Qabul qiluvchi kartasi": rt(data.get("qabul_kartasi")),
        "Tranzaksiya ID": rt(data.get("tranzaksiya_id")),
        "Izoh": rt(data.get("izoh")),
        "TG_ID": rt(meta.get("tg_id")),
        "Manba": rt(meta.get("manba")),
        "Xabar vaqti": rt(meta.get("xabar_vaqti")),
        "Kim tashladi": rt(meta.get("kim_tashladi")),
        "Kim tashladi ID": rt(meta.get("kim_tashladi_id")),
        "Forward manba": rt(meta.get("forward_manba")),
        "Forward ID": rt(meta.get("forward_id")),
    }

    if tartib is not None:
        props["Tartib"] = {"number": tartib}

    if data.get("bank"):
        props["Bank"] = {"select": {"name": str(data["bank"])[:100]}}

    if data.get("sana"):
        props["Tolov sanasi"] = {"date": {"start": data["sana"]}}

    if meta.get("file_upload_id"):
        props["Chek"] = {
            "files": [
                {
                    "type": "file_upload",
                    "file_upload": {"id": meta["file_upload_id"]},
                    "name": meta.get("filename", "chek"),
                }
            ]
        }

    return props


# ----------------------------------------------------------------------------
# Javob matni
# ----------------------------------------------------------------------------

def pending_rows() -> list[dict]:
    return notion_query(
        {"property": "Status", "select": {"equals": "Tekshirilmagan"}},
        sorts=[{"property": "Tolov sanasi", "direction": "ascending"}],
        limit=300,
    )


def build_list_text(rows: list[dict], new_page_id: str | None) -> str:
    """Tekshirilmagan cheklar ro'yxati + JAMI."""
    items = []
    total = 0.0
    for row in rows:
        p = row["properties"]
        summa = p.get("Summa", {}).get("number")
        sana = (p.get("Tolov sanasi", {}).get("date") or {}).get("start")
        total += summa or 0
        items.append((row["id"].replace("-", ""), summa, sana))

    lines = []
    shown = items[-MAX_LIST_ROWS:] if len(items) > MAX_LIST_ROWS else items
    offset = len(items) - len(shown)

    for i, (pid, summa, sana) in enumerate(shown, start=offset + 1):
        mark = " ← yangi" if new_page_id and pid == new_page_id.replace("-", "") else ""
        lines.append(f"{i}. {money(summa)} — {short_date(sana)}{mark}")

    header = ""
    if offset:
        header = f"Jami {len(items)} ta chek, oxirgi {len(shown)} tasi:\n"

    return f"{header}" + "\n".join(lines) + f"\n\nJAMI: {money(total)} so'm"


# ----------------------------------------------------------------------------
# Telegram: chek qabul qilish
# ----------------------------------------------------------------------------

async def extract_file(message, context) -> tuple[bytes, str, str] | None:
    """(bytes, media_type, filename) yoki None."""
    if message.photo:
        tg_file = await context.bot.get_file(message.photo[-1].file_id)
        data = bytes(await tg_file.download_as_bytearray())
        return data, "image/jpeg", f"chek_{message.message_id}.jpg"

    doc = message.document
    if not doc:
        return None

    mt = (doc.mime_type or "").lower()
    if mt not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        return None
    if doc.file_size and doc.file_size > 18 * 1024 * 1024:
        return None

    tg_file = await context.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    return data, mt, doc.file_name or f"chek_{message.message_id}"


def collect_meta(message) -> dict:
    meta = {
        "tg_id": str(message.message_id),
        "manba": message.chat.title or str(message.chat.id),
        "xabar_vaqti": message.date.astimezone(TZ).strftime("%d.%m.%Y %H:%M"),
    }

    if message.from_user and not message.from_user.is_bot:
        u = message.from_user
        name = " ".join(filter(None, [u.first_name, u.last_name]))
        if u.username:
            name = f"{name} (@{u.username})".strip()
        meta["kim_tashladi"] = name
        meta["kim_tashladi_id"] = str(u.id)

    origin = getattr(message, "forward_origin", None)
    if origin:
        kind = getattr(origin, "type", "")
        if kind == "user" and getattr(origin, "sender_user", None):
            u = origin.sender_user
            name = " ".join(filter(None, [u.first_name, u.last_name]))
            if u.username:
                name = f"{name} (@{u.username})".strip()
            meta["forward_manba"] = name
            meta["forward_id"] = str(u.id)
        elif kind == "hidden_user":
            meta["forward_manba"] = getattr(origin, "sender_user_name", "")
        elif kind in ("channel", "chat"):
            chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
            if chat:
                meta["forward_manba"] = chat.title or ""
                meta["forward_id"] = str(chat.id)

    return meta


async def on_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or message.chat.id not in ALLOWED_CHAT_IDS:
        return

    is_edit = bool(update.edited_message or update.edited_channel_post)

    try:
        extracted = await extract_file(message, context)
    except Exception as exc:
        log.exception("Fayl yuklashda xato")
        await message.reply_text(f"❌ Faylni yuklab bo'lmadi: {exc}")
        return

    if not extracted:
        return

    file_bytes, media_type, filename = extracted

    try:
        data = await asyncio.to_thread(call_claude, file_bytes, media_type)
    except Exception as exc:
        log.exception("AI xatosi")
        await message.reply_text(f"❌ Chekni o'qib bo'lmadi: {exc}")
        return

    log.info("Transkript: %s", data.get("_transkript", "")[:500])

    if data.get("chek_emas"):
        if not is_edit:
            await message.reply_text("❌ Bu chekka o'xshamadi\nNotion'ga yozilmadi")
        return

    if is_edit:
        await handle_edit(message, data, file_bytes, filename, media_type)
        return

    async with create_lock:
        try:
            duplicate = await asyncio.to_thread(find_duplicate, data)
        except Exception:
            log.exception("Dublikat tekshiruvida xato")
            duplicate = None

        meta = collect_meta(message)
        meta["filename"] = filename
        meta["file_upload_id"] = await asyncio.to_thread(
            notion_upload_file, file_bytes, filename, media_type
        )

        shubhali = (
            data.get("holat") != "muvaffaqiyatli"
            or (data.get("ishonch") or 0) < 0.8
            or data.get("summa") is None
        )

        if duplicate:
            status = "Dublikat"
        elif shubhali:
            status = "Shubhali"
        else:
            status = "Tekshirilmagan"

        try:
            tartib = None if duplicate else await asyncio.to_thread(next_tartib)
            props = build_props(data, meta, status, tartib)
            page = await asyncio.to_thread(notion_create, props)
        except Exception as exc:
            log.exception("Notion yozishda xato")
            await message.reply_text(f"❌ Notion'ga yozib bo'lmadi: {exc}")
            return

    # --- javob matni ---
    if duplicate:
        await message.reply_text(
            f"♻️ Bu chek allaqachon kiritilgan\n\n"
            f"Summa: {money(data.get('summa'))} so'm — {short_date(data.get('sana'))}\n"
            f"Notion'ga \"Dublikat\" deb yozildi, jamiga qo'shilmadi"
        )
        return

    try:
        rows = await asyncio.to_thread(pending_rows)
    except Exception:
        log.exception("Ro'yxatni olishda xato")
        rows = []

    if status == "Tekshirilmagan" and not any(r["id"] == page["id"] for r in rows):
        rows.append(page)

    list_text = build_list_text(rows, page["id"])

    if status == "Shubhali":
        reason = data.get("izoh") or (
            "to'lov hali tasdiqlanmagan"
            if data.get("holat") == "tasdiqlanmagan"
            else "ma'lumot to'liq o'qilmadi"
        )
        text = (
            f"⚠️ Tekshirish kerak\n\n"
            f"Summa: {money(data.get('summa'))} so'm\n"
            f"Muammo: {reason}\n\n{list_text}"
        )
        pid = page["id"].replace("-", "")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"ok:{pid}"),
                    InlineKeyboardButton("✏️ Tuzatish", callback_data=f"fix:{pid}"),
                ]
            ]
        )
        await message.reply_text(text[:4000], reply_markup=keyboard)
    else:
        text = f"✅ Chek qabul qilindi — {money(data.get('summa'))} so'm\n\n{list_text}"
        await message.reply_text(text[:4000])


async def handle_edit(message, data, file_bytes, filename, media_type) -> None:
    """Post tahrirlansa: TG_ID bo'yicha qatorni yangilaydi."""
    row = await asyncio.to_thread(find_by_tg_id, message.message_id)
    if not row:
        return

    p = row["properties"]
    old = (
        f"{message.date.astimezone(TZ).strftime('%d.%m %H:%M')} | "
        f"eski summa: {money(p.get('Summa', {}).get('number'))}, "
        f"komissiya: {money(p.get('Komissiya', {}).get('number'))}"
    )
    previous = txt(p.get("Edited", {}))
    tartib = p.get("Tartib", {}).get("number")

    meta = collect_meta(message)
    meta["filename"] = filename
    meta["file_upload_id"] = await asyncio.to_thread(
        notion_upload_file, file_bytes, filename, media_type
    )

    status = p.get("Status", {}).get("select", {}).get("name") or "Tekshirilmagan"
    props = build_props(data, meta, status, tartib)
    props.pop("Status")  # tahrirda status tegilmaydi
    props["Edited"] = {
        "rich_text": [{"text": {"content": f"{previous}\n{old}".strip()[:1900]}}]
    }

    try:
        await asyncio.to_thread(notion_update, row["id"], props)
    except Exception as exc:
        log.exception("Tahrirni yozishda xato")
        await message.reply_text(f"❌ Yangilab bo'lmadi: {exc}")
        return

    await message.reply_text(
        f"♻️ Yangilandi — {money(data.get('summa'))} so'm\n"
        f"Eski qiymat \"Edited\" ustuniga yozildi"
    )


# ----------------------------------------------------------------------------
# Tugmalar
# ----------------------------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await query.answer("Sizda ruxsat yo'q", show_alert=True)
        return

    action, _, page_id = query.data.partition(":")

    if action == "ok":
        try:
            await asyncio.to_thread(
                notion_update, page_id, {"Status": {"select": {"name": "Tekshirilmagan"}}}
            )
        except Exception as exc:
            await query.answer(f"Xato: {exc}"[:200], show_alert=True)
            return
        await query.answer("Tasdiqlandi")
        await query.edit_message_text(
            (query.message.text or "") + "\n\n✅ Tasdiqlandi — ro'yxatga qo'shildi"
        )
        return

    if action == "fix":
        pending_fix[user_id] = page_id
        try:
            await context.bot.send_message(
                user_id,
                "✏️ To'g'ri summani yuboring (faqat raqam, masalan: 450000).\n"
                "Bekor qilish uchun: /bekor",
            )
            await query.answer("Sizga shaxsiy xabar yuborildi")
        except Forbidden:
            pending_fix.pop(user_id, None)
            await query.answer(
                "Avval botga /start yozing, keyin qayta urinib ko'ring",
                show_alert=True,
            )


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    page_id = pending_fix.get(user_id)
    if not page_id:
        return

    raw = re.sub(r"[^\d]", "", update.message.text or "")
    if not raw:
        await update.message.reply_text("Faqat raqam yuboring, masalan: 450000")
        return

    summa = int(raw)
    props = {
        "Summa": {"number": summa},
        "Status": {"select": {"name": "Tekshirilmagan"}},
        "Name": {"title": [{"text": {"content": f"{money(summa)} so'm (tuzatildi)"}}]},
    }

    try:
        await asyncio.to_thread(notion_update, page_id, props)
    except Exception as exc:
        await update.message.reply_text(f"❌ Yangilab bo'lmadi: {exc}")
        return

    pending_fix.pop(user_id, None)
    await update.message.reply_text(
        f"✅ Tuzatildi: {money(summa)} so'm\nStatus \"Tekshirilmagan\" qilindi"
    )


# ----------------------------------------------------------------------------
# Buyruqlar
# ----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Salom! Men cheklarni o'qib Notion'ga yozaman.\n\n"
        "Guruh yoki kanalda ID'ni bilish uchun: /id"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Chat ID: `{chat.id}`\nTuri: {chat.type}\n"
        f"Sizning ID: `{update.effective_user.id if update.effective_user else '—'}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_bekor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending_fix.pop(update.effective_user.id, None)
    await update.message.reply_text("Bekor qilindi")


async def cmd_hisobot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        return
    rows = await asyncio.to_thread(pending_rows)
    if not rows:
        await update.effective_message.reply_text("Tekshirilmagan chek yo'q")
        return
    await update.effective_message.reply_text(build_list_text(rows, None)[:4000])


# ----------------------------------------------------------------------------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("bekor", cmd_bekor))
    app.add_handler(CommandHandler("hisobot", cmd_hisobot))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text
        )
    )
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, on_receipt)
    )

    log.info("Bot ishga tushdi. Ruxsat berilgan chatlar: %s", ALLOWED_CHAT_IDS or "—")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
