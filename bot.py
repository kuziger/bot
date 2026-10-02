"""
Telegram-бот: следит за Google-таблицей «Перечневые олимпиады» и присылает изменения.
Фильтры у каждого пользователя свои: предметы, тип изменения, уровень, конкретные олимпиады.

Запуск:  BOT_TOKEN=123:abc python bot.py
"""
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import os
import sqlite3
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

# ============================ НАСТРОЙКИ ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8077378921:AAGZHT4ZsXucZPq52gXKNtAUXTUK-PVN22Y")
SHEET_ID = "1l_1J5hsVWJMV1kSME5SWiq6z5nzdlSznn-0ACvqsS-c"

# Предмет -> gid вкладки (число после "gid=" в адресе, когда открыта нужная вкладка).
# Физика известна, остальные нужно вписать. None = вкладка пропускается.
SHEETS = {
    "Физика": 293754618,
    "Химия": 0,
    "Биология": 517109209,
    "Математика": 672098678,
    "Литература": 477330812,
}
POLL_SECONDS = 300
DB_PATH = "olymp.db"
# ===================================================================ы

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("olymp")

SUBJECTS = list(SHEETS)
LEVELS = ["I", "II", "III"]

FIELD_LABEL = {
    "level": "Уровень", "classes": "Классы", "reg": "Регистрация",
    "tour1": "I тур", "reg_final": "Регистрация на заключительный этап",
    "tour2": "II тур", "tour3": "III тур", "tour4": "IV тур",
    "link": "Ссылка", "note": "Примечание",
}
FIELD_KIND = {
    "reg": "reg", "reg_final": "reg",
    "tour1": "tours", "tour2": "tours", "tour3": "tours", "tour4": "tours",
    "level": "info", "classes": "info", "link": "info", "note": "info",
}
KIND_LABEL = {
    "new": "🆕 Новые олимпиады",
    "removed": "🗑 Удалённые олимпиады",
    "reg": "📝 Сроки регистрации",
    "tours": "📅 Даты туров",
    "info": "ℹ️ Уровень / классы / ссылка / примечание",
}
HEADER_MAP = {"Уровень": "level", "Классы": "classes", "Регистрация": "reg",
              "I тур": "tour1", "II тур": "tour2", "III тур": "tour3", "IV тур": "tour4"}


# ------------------------------ таблица ------------------------------
def clean(s: str) -> str:
    return " ".join((s or "").split())


def fetch_csv(gid: int) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    with urllib.request.urlopen(url, timeout=30) as r:
        raw = r.read().decode("utf-8-sig")
    if raw.lstrip().startswith("<"):
        raise RuntimeError("Google вернул HTML (нет доступа по ссылке?)")
    return list(csv.reader(io.StringIO(raw)))


def parse(rows: list[list[str]]) -> dict[str, dict]:
    """Возвращает {название олимпиады: {поле: значение}}. Столбцы ищутся по заголовкам."""
    hdr_i = next((i for i, r in enumerate(rows[:6]) if any(clean(c) == "Уровень" for c in r)), None)
    if hdr_i is None:
        raise RuntimeError("не найдена строка заголовков")
    cols = {}
    for j, c in enumerate(rows[hdr_i]):
        c = clean(c)
        if c.startswith("Регистрация на"):
            cols[j] = "reg_final"
        elif c in HEADER_MAP:
            cols[j] = HEADER_MAP[c]
    out = {}
    for r in rows[hdr_i + 1:]:
        name = clean(r[0]) if r else ""
        if not name:
            continue
        d = {f: clean(r[j]) if j < len(r) else "" for j, f in cols.items()}
        link_j = next((j for j, c in enumerate(r) if clean(c).startswith("http")), None)
        d["link"] = clean(r[link_j]) if link_j is not None else ""
        d["note"] = clean(r[link_j + 1]) if link_j is not None and link_j + 1 < len(r) else ""
        out[name] = d
    return out


def diff(old: dict, new: dict) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """-> [(название, уровень, [(вид_изменения, текст), ...])]"""
    res = []
    for name, d in new.items():
        if name not in old:
            res.append((name, d.get("level", ""), [("new", "Появилась в таблице")]))
            continue
        ch = []
        for f, label in FIELD_LABEL.items():
            a, b = old[name].get(f, ""), d.get(f, "")
            if a != b:
                ch.append((FIELD_KIND[f], f"{label}: {a or '—'} → {b or '—'}"))
        if ch:
            res.append((name, d.get("level", ""), ch))
    for name, d in old.items():
        if name not in new:
            res.append((name, d.get("level", ""), [("removed", "Удалена из таблицы")]))
    return res


def oid(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()[:8]


# ------------------------------ база ------------------------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS snap(subject TEXT, ord INT, name TEXT, data TEXT, PRIMARY KEY(subject,name))")
db.execute("CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY, prefs TEXT)")
db.commit()


def load_snap(subject):
    rows = db.execute("SELECT name,data FROM snap WHERE subject=? ORDER BY ord", (subject,)).fetchall()
    return {n: json.loads(d) for n, d in rows} if rows else None


def save_snap(subject, data):
    with db:
        db.execute("DELETE FROM snap WHERE subject=?", (subject,))
        db.executemany("INSERT INTO snap VALUES(?,?,?,?)",
                       [(subject, i, n, json.dumps(d, ensure_ascii=False)) for i, (n, d) in enumerate(data.items())])


def default_prefs():
    return {"subjects": list(SUBJECTS), "kinds": list(KIND_LABEL), "levels": list(LEVELS), "olymps": []}


def get_prefs(chat_id):
    row = db.execute("SELECT prefs FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return json.loads(row[0]) if row else default_prefs()


def save_prefs(chat_id, p):
    with db:
        db.execute("INSERT OR REPLACE INTO users VALUES(?,?)", (chat_id, json.dumps(p)))


def all_olymp_names():
    return {oid(n): n for (n,) in db.execute("SELECT DISTINCT name FROM snap")}


# ------------------------------ рассылка ------------------------------
async def notify(bot, subject, changes):
    users = db.execute("SELECT chat_id, prefs FROM users").fetchall()
    for chat_id, prefs in users:
        p = json.loads(prefs)
        if subject not in p["subjects"]:
            continue
        blocks = []
        for name, level, ch in changes:
            if p["levels"] and level and level not in p["levels"]:
                continue
            if p["olymps"] and oid(name) not in p["olymps"]:
                continue
            ch = [c for c in ch if c[0] in p["kinds"]]
            if not ch:
                continue
            lines = "\n".join(f"• {html.escape(t)}" for _, t in ch)
            lvl = f" · {level} уровень" if level else ""
            blocks.append(f"<b>{html.escape(name)}</b>{lvl}\n{lines}")
        if not blocks:
            continue
        text, parts = f"📚 <b>{subject}</b>: изменения в таблице\n\n", []
        for b in blocks:
            if len(text) + len(b) > 3800:
                parts.append(text)
                text = ""
            text += b + "\n\n"
        parts.append(text)
        for part in parts:
            try:
                await bot.send_message(chat_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                log.warning("send to %s failed: %s", chat_id, e)


async def poll(ctx: ContextTypes.DEFAULT_TYPE):
    for subject, gid in SHEETS.items():
        if gid is None:
            continue
        try:
            new = parse(await asyncio.to_thread(fetch_csv, gid))
        except Exception as e:
            log.warning("%s: ошибка загрузки: %s", subject, e)
            continue
        if not new:  # защита от массового «всё удалено» при сбое
            continue
        old = load_snap(subject)
        save_snap(subject, new)
        if old is None:
            log.info("%s: первый снимок (%d олимпиад), без уведомлений", subject, len(new))
            continue
        changes = diff(old, new)
        if changes:
            log.info("%s: %d изменений", subject, len(changes))
            await notify(ctx.bot, subject, changes)


# ------------------------------ меню настроек ------------------------------
def mark(on):
    return "✅" if on else "▫️"


def render(screen: list[str], p: dict):
    kb = []
    if screen[0] == "main":
        n_ol = len(p["olymps"])
        text = ("<b>Настройки уведомлений</b>\n\n"
                f"Предметы: {len(p['subjects'])}/{len(SUBJECTS)}\n"
                f"Типы изменений: {len(p['kinds'])}/{len(KIND_LABEL)}\n"
                f"Уровни: {', '.join(p['levels']) or 'не выбраны'}\n"
                f"Олимпиады: {'все' if not n_ol else f'выбрано {n_ol}'}")
        kb = [[InlineKeyboardButton("📚 Предметы", callback_data="m:subj")],
              [InlineKeyboardButton("🔔 Что изменилось", callback_data="m:kinds")],
              [InlineKeyboardButton("🎚 Уровни", callback_data="m:levels")],
              [InlineKeyboardButton("🏆 Конкретные олимпиады", callback_data="m:olsub")],
              [InlineKeyboardButton("♻️ Сбросить всё", callback_data="r:all")]]
    elif screen[0] == "subj":
        text = "Уведомления по каким предметам присылать?"
        kb = [[InlineKeyboardButton(f"{mark(s in p['subjects'])} {s}", callback_data=f"t:s:{i}")]
              for i, s in enumerate(SUBJECTS)]
    elif screen[0] == "kinds":
        text = "Какие типы изменений присылать?"
        kb = [[InlineKeyboardButton(f"{mark(k in p['kinds'])} {v}", callback_data=f"t:k:{k}")]
              for k, v in KIND_LABEL.items()]
    elif screen[0] == "levels":
        text = "Какие уровни олимпиад интересны?"
        kb = [[InlineKeyboardButton(f"{mark(l in p['levels'])} {l} уровень", callback_data=f"t:l:{l}")]
              for l in LEVELS]
    elif screen[0] == "olsub":
        text = ("Выберите предмет, чтобы отметить конкретные олимпиады.\n"
                "Если ничего не отмечено, приходят <b>все</b> олимпиады.")
        kb = [[InlineKeyboardButton(s, callback_data=f"m:ol:{i}")] for i, s in enumerate(SUBJECTS)]
        kb.append([InlineKeyboardButton("Очистить выбор олимпиад", callback_data="r:ol")])
    elif screen[0] == "ol":
        si = int(screen[1])
        text = f"<b>{SUBJECTS[si]}</b>: отметьте олимпиады, о которых хотите получать уведомления"
        names = load_snap(SUBJECTS[si]) or {}
        for n in names:
            kb.append([InlineKeyboardButton(f"{mark(oid(n) in p['olymps'])} {n[:45]}",
                                            callback_data=f"t:o:{oid(n)}:{si}")])
        if not names:
            text += "\n\nДанных по этой вкладке пока нет."
        kb.append([InlineKeyboardButton("⬅️ К предметам", callback_data="m:olsub")])
        kb = kb  # noqa
        kb.append([InlineKeyboardButton("🏠 В меню", callback_data="m:main")])
        return text, InlineKeyboardMarkup(kb)
    else:
        return render(["main"], p)
    if screen[0] != "main":
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="m:main")])
    return text, InlineKeyboardMarkup(kb)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if not db.execute("SELECT 1 FROM users WHERE chat_id=?", (cid,)).fetchone():
        save_prefs(cid, default_prefs())
    await update.message.reply_text(
        "Привет! Я слежу за таблицей перечневых олимпиад и пишу, когда что-то меняется: "
        "новые олимпиады, сроки регистрации, даты туров и т.д.\n\n"
        "/settings: настроить фильтры (предметы, типы изменений, уровни, олимпиады)\n"
        "/check: проверить таблицу прямо сейчас")
    await cmd_settings(update, ctx)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    p = get_prefs(cid)
    save_prefs(cid, p)
    text, kb = render(["main"], p)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю таблицу…")
    await poll(ctx)
    await update.message.reply_text("Готово. Если были изменения, они пришли отдельным сообщением.")


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.message.chat_id
    p = get_prefs(cid)
    parts = q.data.split(":")
    screen = ["main"]
    if parts[0] == "m":
        screen = parts[1:]
    elif parts[0] == "r":
        if parts[1] == "all":
            p = default_prefs()
        else:
            p["olymps"] = []
        save_prefs(cid, p)
        screen = ["main"] if parts[1] == "all" else ["olsub"]
    elif parts[0] == "t":
        grp, val = parts[1], parts[2]
        key = {"s": "subjects", "k": "kinds", "l": "levels", "o": "olymps"}[grp]
        if grp == "s":
            val = SUBJECTS[int(val)]
        s = set(p[key])
        s ^= {val}
        p[key] = sorted(s)
        save_prefs(cid, p)
        screen = {"s": ["subj"], "k": ["kinds"], "l": ["levels"], "o": ["ol", parts[3] if len(parts) > 3 else "0"]}[grp]
    text, kb = render(screen, p)
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def main():
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.job_queue.run_repeating(poll, interval=POLL_SECONDS, first=5)
    app.run_polling()


if __name__ == "__main__":
    main()
