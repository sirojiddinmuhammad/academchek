# Chek Bot

Telegram kanal yoki guruhga tushgan to'lov cheklarini (screenshot yoki PDF)
sun'iy intellekt bilan o'qib, Notion "Cheklar" bazasiga yozadi.

## Fayllar

- `bot.py` — botning to'liq kodi
- `requirements.txt` — kutubxonalar
- `Procfile` — Railway uchun (`worker: python bot.py`)
- `.env.example` — kerakli o'zgaruvchilar ro'yxati

## 1. Tokenlar

| O'zgaruvchi | Qayerdan |
|---|---|
| `BOT_TOKEN` | @BotFather → /newbot |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `NOTION_TOKEN` | notion.so/my-integrations → New integration → Internal Integration Secret |
| `NOTION_DB_ID` | `78f0692553644d508756f0a88d750500` |
| `ALLOWED_CHAT_IDS` | botni guruhga qo'shib `/id` yozing |
| `ADMIN_IDS` | `/id` javobidagi "Sizning ID" |

## 2. Notion sozlamasi

"Cheklar" bazasini oching → yuqori o'ngda `⋯` → **Connections** → yaratgan
integratsiyangizni qo'shing. Busiz bot bazani ko'rmaydi (404 xatosi beradi).

## 3. Telegram sozlamasi

- **Kanal**: botni admin qiling, "Post yuborish" huquqi bilan.
- **Guruh**: botni admin qiling (shunda barcha xabarlarni ko'radi).
- Botga bir marta `/start` yozing — "Tuzatish" tugmasi shaxsiy chat orqali ishlaydi.

## 4. Railway'ga deploy

1. Bu papkani GitHub repozitoriyasiga yuklang
2. Railway → New Project → Deploy from GitHub repo
3. Variables bo'limiga `.env.example` dagi o'zgaruvchilarni kiriting
4. Deploy tugagach, Logs'da "Bot ishga tushdi" yozuvini ko'rasiz

Railway avtomatik `Procfile` ni o'qiydi. `worker` turi muhim — `web` emas,
chunki bot polling rejimida ishlaydi va port ochmaydi.

## Buyruqlar

| Buyruq | Nima qiladi |
|---|---|
| `/id` | chat ID va sizning ID'ingizni ko'rsatadi |
| `/hisobot` | tekshirilmagan cheklar ro'yxatini chiqaradi |
| `/bekor` | "Tuzatish" rejimidan chiqadi |

## Statuslar

- **Tekshirilmagan** — yangi chek, ro'yxatda va JAMI'da turadi
- **Tasdiqlangan** — siz Notion'da qo'yasiz, ro'yxatdan tushadi
- **Shubhali** — AI ishonchi past yoki to'lov tasdiqlanmagan; ro'yxatga kirmaydi
- **Dublikat** — takroriy chek; jamiga qo'shilmaydi

## Sozlash mumkin bo'lgan joylar

- `CLAUDE_MODEL` — aniqlik yetmasa `claude-sonnet-5` ga o'zgartiring
- `MAX_LIST_ROWS` (bot.py) — javobdagi ro'yxat uzunligi, hozir 30
- `SYSTEM_PROMPT` (bot.py) — AI uchun o'qish qoidalari
