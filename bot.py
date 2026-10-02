"""
Telegram-бот «Олимпиады»: следит за Google-таблицей и присылает изменения, дедлайны и напоминания.

Запуск:  BOT_TOKEN=123:abc ADMIN_IDS=111,222 python bot.py
"""
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

# ============================ НАСТРОЙКИ ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8077378921:AAGZHT4ZsXucZPq52gXKNtAUXTUK-PVN22Y")
# Telegram ID админов через запятую (узнать свой: бот @userinfobot). Нужны для /stats, /broadcast и алертов.
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}

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
POLL_SECONDS = 300          # как часто проверять таблицу
DAILY_HOUR = 9              # во сколько слать сводки и напоминания (по времени TZ_NAME)
TZ_NAME = "Europe/Moscow"
ACADEMIC_START_YEAR = 2026  # учебный год 2026/2027: сентябрь-декабрь -> 2026, январь-август -> 2027
DB_PATH = "olymp.db"
# ===================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("olymp")

try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = timezone(timedelta(hours=3))

SUBJECTS = list(SHEETS)
LEVELS = ["I", "II", "III"]
GRADES = list(range(5, 12))
REMIND_OPTIONS = [(7, "за 7 дней"), (3, "за 3 дня"), (1, "за 1 день"), (0, "в день события")]
PAGE = 10

FIELD_LABEL = {
    "level": "Уровень", "classes": "Классы", "reg": "Регистрация",
    "tour1": "I тур", "reg_final": "Регистрация на заключительный этап",
    "tour2": "II тур", "tour3": "III тур", "tour4": "IV тур",
    "link": "Ссылка", "note": "Примечание",
}
EVENT_FIELDS = ["reg", "tour1", "reg_final", "tour2", "tour3", "tour4"]
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

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
MONTH_GEN = [""] + list(MONTHS)
DATE_RE = re.compile(r"((?:\d{1,2}\s*(?:[-–—]|,|и)\s*)*\d{1,2})\s*(" + "|".join(MONTHS) + r")(?:\s*(\d{4}))?")

esc = html.escape


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
    """{название олимпиады: {поле: значение}}. Столбцы ищутся по заголовкам."""
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


def diff(old: dict, new: dict) -> list[dict]:
    """-> [{name, data, changes: [(вид, текст)]}]"""
    res = []
    for name, d in new.items():
        if name not in old:
            res.append({"name": name, "data": d, "changes": [("new", "🆕 Появилась в таблице")]})
            continue
        ch = []
        for f, label in FIELD_LABEL.items():
            a, b = old[name].get(f, ""), d.get(f, "")
            if a == b:
                continue
            if not a:
                t = f"{label}: добавлено: {b}"
            elif not b:
                t = f"{label}: убрано (было: {a})"
            else:
                t = f"{label}: {a} → {b}"
            ch.append((FIELD_KIND[f], t))
        if ch:
            res.append({"name": name, "data": d, "changes": ch})
    for name, d in old.items():
        if name not in new:
            res.append({"name": name, "data": d, "changes": [("removed", "🗑 Удалена из таблицы")]})
    return res


def oid(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()[:8]


# ------------------------------ даты и классы ------------------------------
def extract_dates(text: str) -> list[date]:
    """Достаёт даты из строк вроде '24 сентября -11 октября', '16 – 29 ноября 2026', '27 сентября (очно)'."""
    text = re.sub(r"\([^)]*\)", " ", (text or "").lower())
    out = set()
    for m in DATE_RE.finditer(text):
        days = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
        mon = MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else (ACADEMIC_START_YEAR if mon >= 9 else ACADEMIC_START_YEAR + 1)
        for dd in {days[0], days[-1]}:
            try:
                out.add(date(year, mon, dd))
            except ValueError:
                pass
    return sorted(out)


def class_range(s: str):
    m = re.search(r"(\d+)\s*[-–—]\s*(\d+)", s or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\d+", s or "")
    return (int(m.group()), int(m.group())) if m else None


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTH_GEN[d.month]}"


def days_phrase(n: int) -> str:
    return "сегодня" if n == 0 else "завтра" if n == 1 else f"через {n} дн." if n > 0 else f"{-n} дн. назад"


# ------------------------------ база ------------------------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS snap(subject TEXT, ord INT, name TEXT, data TEXT, PRIMARY KEY(subject,name));
CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY, prefs TEXT);
CREATE TABLE IF NOT EXISTS log(ts INT, subject TEXT, name TEXT, level TEXT, classes TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS pending(chat_id INT, block TEXT);
CREATE TABLE IF NOT EXISTS sent(chat_id INT, key TEXT, PRIMARY KEY(chat_id,key));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")
db.commit()


def load_snap(subject):
    rows = db.execute("SELECT name,data FROM snap WHERE subject=? ORDER BY ord", (subject,)).fetchall()
    return {n: json.loads(d) for n, d in rows} if rows else None


def save_snap(subject, data):
    with db:
        db.execute("DELETE FROM snap WHERE subject=?", (subject,))
        db.executemany("INSERT INTO snap VALUES(?,?,?,?)",
                       [(subject, i, n, json.dumps(d, ensure_ascii=False)) for i, (n, d) in enumerate(data.items())])


def set_meta(k, v):
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


def get_meta(k, default=None):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def default_prefs():
    return {"subjects": list(SUBJECTS), "kinds": list(KIND_LABEL), "levels": list(LEVELS),
            "grades": [], "favs": [], "fav_only": False, "remind": [3, 1], "mode": "instant", "paused": False}


def merge_prefs(raw: str | None):
    p = default_prefs()
    if raw:
        p.update(json.loads(raw))
    return p


def get_prefs(chat_id):
    row = db.execute("SELECT prefs FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return merge_prefs(row[0] if row else None)


def save_prefs(chat_id, p):
    with db:
        db.execute("INSERT OR REPLACE INTO users VALUES(?,?)", (chat_id, json.dumps(p)))


def all_users():
    return [(cid, merge_prefs(pr)) for cid, pr in db.execute("SELECT chat_id, prefs FROM users")]


def all_names() -> dict[str, str]:
    return {oid(n): n for (n,) in db.execute("SELECT DISTINCT name FROM snap ORDER BY ord")}


def matches(p: dict, subject: str, name: str, d: dict) -> bool:
    """Избранные олимпиады проходят всегда. Иначе работают фильтры предмета, уровня и класса."""
    h = oid(name)
    if h in p["favs"]:
        return True
    if p["fav_only"] or subject not in p["subjects"]:
        return False
    lv = d.get("level", "")
    if lv and lv not in p["levels"]:
        return False
    if p["grades"]:
        r = class_range(d.get("classes", ""))
        if r and not any(r[0] <= g <= r[1] for g in p["grades"]):
            return False
    return True


def collect_events(p: dict, start: date, end: date | None = None):
    """Даты событий для пользователя: [(дата, название, поле, исходный_текст, [предметы], data)] по возрастанию."""
    ev = {}
    for subject in SUBJECTS:
        for name, d in (load_snap(subject) or {}).items():
            if not matches(p, subject, name, d):
                continue
            for f in EVENT_FIELDS:
                for dt in extract_dates(d.get(f, "")):
                    if dt < start or (end and dt > end):
                        continue
                    key = (dt, name, f)
                    if key in ev:
                        ev[key][3].append(subject)
                    else:
                        ev[key] = (dt, name, f, [subject], d)
    return [(dt, n, f, d.get(f, ""), subs, d) for (dt, n, f), (_, _, _, subs, d) in sorted(ev.items(), key=lambda kv: (kv[0][0], kv[0][1]))]


# ------------------------------ отправка ------------------------------
def chunk(blocks: list[str], header: str = "", sep: str = "\n\n", limit: int = 3800) -> list[str]:
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(sep) + len(b) > limit and cur.strip() != header.strip():
            msgs.append(cur)
            cur = ""
        cur += (sep if cur else "") + b
    if cur.strip():
        msgs.append(cur)
    return msgs


async def send(bot, chat_id, text, **kw):
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)
        return True
    except Forbidden:
        with db:
            db.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        log.info("пользователь %s заблокировал бота, удалён", chat_id)
    except Exception as e:
        log.warning("send to %s failed: %s", chat_id, e)
    return False


async def alert_admins(bot, text):
    for a in ADMIN_IDS:
        await send(bot, a, text)


def make_block(subject: str, it: dict) -> str:
    d, name = it["data"], it["name"]
    title = esc(name)
    link = d.get("link", "")
    if link.startswith("http") and link.isascii():
        title = f'<a href="{esc(link, quote=True)}">{title}</a>'
    meta = " · ".join(x for x in [f"{d['level']} уровень" if d.get("level") else "",
                                  f"{d['classes']} кл." if d.get("classes") else "", subject] if x)
    lines = "\n".join(f"• {esc(t)}" for _, t in it["changes"])
    return f"<b>{title}</b>\n<i>{esc(meta)}</i>\n{lines}"


async def fanout(bot, subject: str, items: list[dict]):
    for chat_id, p in all_users():
        if p["paused"]:
            continue
        blocks = []
        for it in items:
            if not matches(p, subject, it["name"], it["data"]):
                continue
            ch = [c for c in it["changes"] if c[0] in p["kinds"]]
            if ch:
                blocks.append(make_block(subject, {**it, "changes": ch}))
        if not blocks:
            continue
        if p["mode"] == "digest":
            with db:
                db.executemany("INSERT INTO pending VALUES(?,?)", [(chat_id, b) for b in blocks])
        else:
            for part in chunk(blocks, "🔔 <b>Изменения в таблице</b>\n"):
                await send(bot, chat_id, part)
                await asyncio.sleep(0.05)


# ------------------------------ проверка таблицы ------------------------------
poll_lock = asyncio.Lock()
fail_count: dict[str, int] = {}


async def poll(ctx: ContextTypes.DEFAULT_TYPE):
    async with poll_lock:
        for subject, gid in SHEETS.items():
            if gid is None:
                continue
            try:
                new = parse(await asyncio.to_thread(fetch_csv, gid))
                if not new:
                    raise RuntimeError("вкладка пустая")
            except Exception as e:
                fail_count[subject] = fail_count.get(subject, 0) + 1
                log.warning("%s: ошибка загрузки: %s", subject, e)
                if fail_count[subject] == 3:
                    await alert_admins(ctx.bot, f"⚠️ <b>{esc(subject)}</b>: 3 проверки подряд с ошибкой: {esc(str(e))}")
                continue
            if fail_count.get(subject, 0) >= 3:
                await alert_admins(ctx.bot, f"✅ <b>{esc(subject)}</b>: загрузка снова работает")
            fail_count[subject] = 0
            set_meta(f"last_ok:{subject}", int(time.time()))
            old = load_snap(subject)
            save_snap(subject, new)
            if old is None:
                log.info("%s: первый снимок (%d олимпиад), без уведомлений", subject, len(new))
                continue
            items = diff(old, new)
            if not items:
                continue
            log.info("%s: %d изменений", subject, len(items))
            now = int(time.time())
            with db:
                db.executemany("INSERT INTO log VALUES(?,?,?,?,?,?,?)",
                               [(now, subject, it["name"], it["data"].get("level", ""), it["data"].get("classes", ""), k, t)
                                for it in items for k, t in it["changes"]])
            await fanout(ctx.bot, subject, items)


async def daily(ctx: ContextTypes.DEFAULT_TYPE):
    bot = ctx.bot
    # 1) сводки
    for chat_id in [r[0] for r in db.execute("SELECT DISTINCT chat_id FROM pending")]:
        blocks = [r[0] for r in db.execute("SELECT block FROM pending WHERE chat_id=? ORDER BY rowid", (chat_id,))]
        for part in chunk(blocks, "🗞 <b>Сводка изменений за сутки</b>\n"):
            await send(bot, chat_id, part)
        with db:
            db.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))
    # 2) напоминания о дедлайнах
    today = datetime.now(TZ).date()
    for chat_id, p in all_users():
        if p["paused"] or not p["remind"]:
            continue
        horizon = today + timedelta(days=max(p["remind"]))
        lines, keys = [], []
        for dt, name, f, raw, subs, d in collect_events(p, today, horizon):
            delta = (dt - today).days
            if delta not in p["remind"]:
                continue
            key = f"{name}|{f}|{dt}|{delta}"
            if db.execute("SELECT 1 FROM sent WHERE chat_id=? AND key=?", (chat_id, key)).fetchone():
                continue
            keys.append(key)
            star = "⭐ " if oid(name) in p["favs"] else ""
            lines.append(f"⏰ <b>{days_phrase(delta).capitalize()}</b> ({fmt_date(dt)}): {star}<b>{esc(name)}</b>\n"
                         f"{esc(FIELD_LABEL[f])}: {esc(raw[:120])}")
        if not lines:
            continue
        ok = True
        for part in chunk(lines, "<b>Напоминания о датах</b>\n"):
            ok = await send(bot, chat_id, part) and ok
        if ok:
            with db:
                db.executemany("INSERT OR IGNORE INTO sent VALUES(?,?)", [(chat_id, k) for k in keys])


# ------------------------------ экраны и меню ------------------------------
def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def mark(on):
    return "✅" if on else "▫️"


def home_row():
    return [btn("🏠 В меню", "m:main")]


def render_card(h: str, ctx_si: str, p: dict):
    name = all_names().get(h)
    if not name:
        return "Олимпиада не найдена (возможно, её удалили из таблицы).", InlineKeyboardMarkup([home_row()])
    parts, link = [f"<b>{esc(name)}</b>"], ""
    for s in SUBJECTS:
        d = (load_snap(s) or {}).get(name)
        if not d:
            continue
        link = link or d.get("link", "")
        head = " · ".join(x for x in [s, f"{d['level']} уровень" if d.get("level") else "",
                                      f"{d['classes']} кл." if d.get("classes") else ""] if x)
        lines = [f"{FIELD_LABEL[f]}: {esc(d[f])}" for f in EVENT_FIELDS if d.get(f)]
        if d.get("note"):
            lines.append(f"Примечание: {esc(d['note'])}")
        parts.append(f"<b>{esc(head)}</b>\n" + ("\n".join(lines) or "Дат пока нет"))
    if link:
        parts.append(esc(link))
    fav = h in p["favs"]
    kb = [[btn("⭐ Убрать из избранного" if fav else "☆ Добавить в избранное", f"fav:{h}:{ctx_si}")]]
    if link.startswith("http") and link.isascii():
        kb.append([InlineKeyboardButton("🔗 Открыть сайт", url=link)])
    if ctx_si.isdigit():
        kb.append([btn("⬅️ К списку", f"ls:{ctx_si}:0")])
    kb.append(home_row())
    return "\n\n".join(parts), InlineKeyboardMarkup(kb)


def render(screen: list[str], p: dict):
    s0 = screen[0]
    kb = []
    if s0 == "subj":
        text = "Изменения по каким предметам присылать?"
        kb = [[btn(f"{mark(s in p['subjects'])} {s}", f"t:s:{i}")] for i, s in enumerate(SUBJECTS)]
    elif s0 == "kinds":
        text = "Какие типы изменений присылать?"
        kb = [[btn(f"{mark(k in p['kinds'])} {v}", f"t:k:{k}")] for k, v in KIND_LABEL.items()]
    elif s0 == "levels":
        text = "Какие уровни олимпиад интересны?"
        kb = [[btn(f"{mark(l in p['levels'])} {l} уровень", f"t:l:{l}")] for l in LEVELS]
    elif s0 == "grades":
        text = ("В каком ты классе? Придут только олимпиады, где можно участвовать в этом классе "
                "(можно выбрать несколько). Ничего не выбрано = все классы.")
        row = []
        for g in GRADES:
            row.append(btn(f"{mark(g in p['grades'])} {g}", f"t:g:{g}"))
            if len(row) == 4:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
    elif s0 == "remind":
        text = ("Когда напоминать о датах регистрации и туров? Напоминания приходят в "
                f"{DAILY_HOUR}:00 ({TZ_NAME}). Можно выбрать несколько вариантов.")
        kb = [[btn(f"{mark(n in p['remind'])} {label}", f"t:r:{n}")] for n, label in REMIND_OPTIONS]
    elif s0 == "mode":
        text = "Как доставлять уведомления об изменениях?"
        kb = [[btn(f"{mark(p['mode'] == 'instant')} ⚡ Сразу, как только изменилось", "mode:instant")],
              [btn(f"{mark(p['mode'] == 'digest')} 🗞 Одной сводкой раз в день ({DAILY_HOUR}:00)", "mode:digest")]]
    elif s0 == "favs":
        names = all_names()
        text = ("<b>Избранное</b>\nИзбранные олимпиады приходят всегда, независимо от остальных фильтров. "
                "Добавлять их можно в /list и /find.\n\n"
                f"Режим «только избранные»: {'включён' if p['fav_only'] else 'выключен'}")
        kb = [[btn(f"{mark(p['fav_only'])} Присылать только избранное", "t:fo:x")]]
        for h in p["favs"]:
            kb.append([btn(f"❌ {names.get(h, h)[:50]}", f"fav:{h}:F")])
    elif s0 == "lr":
        text = "Выберите предмет:"
        kb = [[btn(f"{s} ({len(load_snap(s) or {})})", f"ls:{i}:0")] for i, s in enumerate(SUBJECTS) if SHEETS[s] is not None]
    elif s0 == "ls":
        si, page = int(screen[1]), int(screen[2])
        items = list((load_snap(SUBJECTS[si]) or {}).items())
        pages = max(1, (len(items) + PAGE - 1) // PAGE)
        page = min(max(page, 0), pages - 1)
        text = f"<b>{esc(SUBJECTS[si])}</b>: нажмите на олимпиаду, чтобы увидеть даты (стр. {page + 1}/{pages})"
        if not items:
            text += "\n\nДанных по этой вкладке пока нет."
        for n, d in items[page * PAGE:(page + 1) * PAGE]:
            label = f"{'⭐ ' if oid(n) in p['favs'] else ''}{d.get('level', '')} · {n}".strip(" ·")
            kb.append([btn(label[:60], f"c:{oid(n)}:{si}")])
        nav = []
        if page > 0:
            nav.append(btn("◀️", f"ls:{si}:{page - 1}"))
        if page < pages - 1:
            nav.append(btn("▶️", f"ls:{si}:{page + 1}"))
        if nav:
            kb.append(nav)
        kb.append([btn("⬅️ К предметам", "m:lr")])
    else:  # main
        n_fav = len(p["favs"])
        text = ("<b>Настройки</b>" + ("  ⏸ <i>уведомления на паузе</i>" if p["paused"] else "") + "\n\n"
                f"📚 Предметы: {len(p['subjects'])}/{len(SUBJECTS)}\n"
                f"🔔 Типы изменений: {len(p['kinds'])}/{len(KIND_LABEL)}\n"
                f"🎚 Уровни: {', '.join(p['levels']) or 'не выбраны'}\n"
                f"🎓 Классы: {', '.join(map(str, sorted(p['grades']))) or 'все'}\n"
                f"⏰ Напоминания: {', '.join('за ' + str(n) + ' дн.' if n else 'в день' for n in sorted(p['remind'], reverse=True)) or 'выключены'}\n"
                f"📨 Доставка: {'сразу' if p['mode'] == 'instant' else 'сводка раз в день'}\n"
                f"⭐ Избранное: {n_fav}{' (только оно)' if p['fav_only'] else ''}")
        kb = [[btn("📚 Предметы", "m:subj"), btn("🔔 Что изменилось", "m:kinds")],
              [btn("🎚 Уровни", "m:levels"), btn("🎓 Классы", "m:grades")],
              [btn("⏰ Напоминания", "m:remind"), btn("📨 Доставка", "m:mode")],
              [btn("⭐ Избранное", "m:favs"), btn("📋 Все олимпиады", "m:lr")],
              [btn("▶️ Возобновить уведомления" if p["paused"] else "⏸ Пауза", "pause")],
              [btn("♻️ Сбросить настройки", "reset")]]
        return text, InlineKeyboardMarkup(kb)
    if s0 == "ls":
        kb.append(home_row())
    else:
        kb.append([btn("⬅️ Назад", "m:main")] if s0 != "lr" else home_row())
    return text, InlineKeyboardMarkup(kb)


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.message.chat_id
    p = get_prefs(cid)
    a = q.data.split(":")
    cmd, screen, card = a[0], ["main"], None
    if cmd == "m":
        screen = a[1:]
    elif cmd == "t":
        grp, val = a[1], a[2]
        if grp == "fo":
            p["fav_only"] = not p["fav_only"]
            screen = ["favs"]
        else:
            key, scr = {"s": ("subjects", "subj"), "k": ("kinds", "kinds"), "l": ("levels", "levels"),
                        "g": ("grades", "grades"), "r": ("remind", "remind")}[grp]
            v = SUBJECTS[int(val)] if grp == "s" else int(val) if grp in "gr" else val
            cur = set(p[key])
            cur ^= {v}
            p[key] = sorted(cur) if grp not in "sk" else [x for x in (SUBJECTS if grp == "s" else list(KIND_LABEL)) if x in cur]
            screen = [scr]
    elif cmd == "mode":
        p["mode"] = a[1]
        screen = ["mode"]
    elif cmd == "pause":
        p["paused"] = not p["paused"]
    elif cmd == "reset":
        p = default_prefs()
    elif cmd == "fav":
        cur = set(p["favs"])
        cur ^= {a[1]}
        p["favs"] = sorted(cur)
        if a[2] == "F":
            screen = ["favs"]
        else:
            card = (a[1], a[2])
    elif cmd == "ls":
        screen = ["ls", a[1], a[2]]
    elif cmd == "c":
        card = (a[1], a[2])
    save_prefs(cid, p)
    text, kb = render_card(card[0], card[1], p) if card else render(screen, p)
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ------------------------------ команды ------------------------------
def ensure_user(chat_id):
    if not db.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone():
        save_prefs(chat_id, default_prefs())


async def reply(update: Update, text: str, kb=None):
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                              disable_web_page_preview=True)


HELP = ("<b>Что я умею</b>\n"
        "/settings: фильтры (предметы, типы изменений, уровни, классы, напоминания, доставка)\n"
        "/list: все олимпиады по предметам, карточки с датами, ⭐ избранное\n"
        "/find <i>текст</i>: поиск олимпиады по названию\n"
        "/upcoming [дней]: ближайшие даты регистраций и туров (по умолчанию 14 дней)\n"
        "/history [дней]: что менялось в таблице (по умолчанию 14 дней)\n"
        "/calendar: файл .ics с датами для Google/Apple Calendar\n"
        "/pause и /resume: выключить и включить уведомления\n"
        "/status: когда я последний раз проверял таблицу\n"
        "/check: проверить таблицу прямо сейчас")


async def cmd_start(update: Update, ctx):
    ensure_user(update.effective_chat.id)
    await reply(update, "Привет! Я слежу за таблицей перечневых олимпиад: пишу, когда что-то меняется, "
                        "и напоминаю о приближающихся датах.\n\n" + HELP +
                "\n\nНачни с выбора предметов, уровней и своего класса:")
    await cmd_settings(update, ctx)


async def cmd_help(update: Update, ctx):
    await reply(update, HELP)


async def cmd_settings(update: Update, ctx):
    cid = update.effective_chat.id
    ensure_user(cid)
    text, kb = render(["main"], get_prefs(cid))
    await reply(update, text, kb)


async def cmd_list(update: Update, ctx):
    cid = update.effective_chat.id
    ensure_user(cid)
    text, kb = render(["lr"], get_prefs(cid))
    await reply(update, text, kb)


async def cmd_find(update: Update, ctx):
    cid = update.effective_chat.id
    ensure_user(cid)
    q = " ".join(ctx.args).strip().lower()
    if not q:
        return await reply(update, "Напишите часть названия, например: <code>/find физтех</code>")
    p = get_prefs(cid)
    found = [(h, n) for h, n in all_names().items() if q in n.lower()]
    if not found:
        return await reply(update, "Ничего не нашёл. Попробуйте другое слово или откройте /list.")
    if len(found) == 1:
        text, kb = render_card(found[0][0], "-", p)
        return await reply(update, text, kb)
    kb = InlineKeyboardMarkup([[btn(f"{'⭐ ' if h in p['favs'] else ''}{n}"[:60], f"c:{h}:-")] for h, n in found[:15]])
    await reply(update, f"Нашёл {len(found)}:", kb)


async def cmd_upcoming(update: Update, ctx):
    cid = update.effective_chat.id
    ensure_user(cid)
    p = get_prefs(cid)
    try:
        n = min(max(int(ctx.args[0]), 1), 180) if ctx.args else 14
    except ValueError:
        n = 14
    today = datetime.now(TZ).date()
    events = collect_events(p, today, today + timedelta(days=n))
    if not events:
        return await reply(update, f"В ближайшие {n} дн. дат по вашим фильтрам нет. Проверьте /settings или /list.")
    lines = []
    for dt, name, f, raw, subs, d in events:
        star = "⭐ " if oid(name) in p["favs"] else ""
        lines.append(f"<b>{fmt_date(dt)}</b> ({days_phrase((dt - today).days)}): {star}{esc(name)}\n"
                     f"{esc(FIELD_LABEL[f])}: {esc(raw[:100])}")
    for part in chunk(lines, f"📅 <b>Ближайшие {n} дн.</b>\n"):
        await reply(update, part)


async def cmd_history(update: Update, ctx):
    cid = update.effective_chat.id
    ensure_user(cid)
    p = get_prefs(cid)
    try:
        n = min(max(int(ctx.args[0]), 1), 180) if ctx.args else 14
    except ValueError:
        n = 14
    since = int(time.time()) - n * 86400
    lines = []
    for ts, subject, name, level, classes, kind, text in db.execute(
            "SELECT ts,subject,name,level,classes,kind,text FROM log WHERE ts>? ORDER BY ts DESC LIMIT 400", (since,)):
        if kind in p["kinds"] and matches(p, subject, name, {"level": level, "classes": classes}):
            when = datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")
            lines.append(f"<i>{when}</i> · {esc(subject)}\n<b>{esc(name)}</b>: {esc(text)}")
        if len(lines) >= 30:
            break
    if not lines:
        return await reply(update, f"За {n} дн. изменений по вашим фильтрам не было.")
    for part in chunk(lines, f"🕘 <b>Изменения за {n} дн.</b>\n"):
        await reply(update, part)


def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


async def cmd_calendar(update: Update, ctx):
    cid = update.effective_chat.id
    ensure_user(cid)
    p = get_prefs(cid)
    events = collect_events(p, date(ACADEMIC_START_YEAR, 8, 1))
    if not events:
        return await reply(update, "Нет дат для календаря по вашим фильтрам.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//olymp-bot//RU", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:Олимпиады"]
    for dt, name, f, raw, subs, d in events:
        uid = hashlib.md5(f"{name}|{f}|{dt}".encode()).hexdigest()
        out += ["BEGIN:VEVENT", f"UID:{uid}@olymp-bot", f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{dt:%Y%m%d}", f"DTEND;VALUE=DATE:{dt + timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{ics_escape(name + ': ' + FIELD_LABEL[f])}",
                f"DESCRIPTION:{ics_escape(raw + (chr(10) + d['link'] if d.get('link') else ''))}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    bio = io.BytesIO("\r\n".join(out).encode("utf-8"))
    await update.effective_message.reply_document(
        document=bio, filename="olympiads.ics",
        caption=f"Событий: {len(events)}. Откройте файл, чтобы добавить даты в календарь.")


async def cmd_pause(update: Update, ctx):
    cid = update.effective_chat.id
    p = get_prefs(cid)
    p["paused"] = True
    save_prefs(cid, p)
    await reply(update, "⏸ Уведомления на паузе. Включить обратно: /resume. Что пропущено, смотрите в /history.")


async def cmd_resume(update: Update, ctx):
    cid = update.effective_chat.id
    p = get_prefs(cid)
    p["paused"] = False
    save_prefs(cid, p)
    await reply(update, "▶️ Уведомления включены. Что менялось за время паузы: /history.")


async def cmd_status(update: Update, ctx):
    lines = []
    for s, gid in SHEETS.items():
        if gid is None:
            lines.append(f"▫️ {s}: вкладка не подключена")
            continue
        ts = get_meta(f"last_ok:{s}")
        when = datetime.fromtimestamp(int(ts), TZ).strftime("%d.%m %H:%M") if ts else "ещё не загружалась"
        n = len(load_snap(s) or {})
        warn = f" ⚠️ ошибок подряд: {fail_count[s]}" if fail_count.get(s) else ""
        lines.append(f"✅ {s}: {n} олимпиад, проверено {when}{warn}")
    await reply(update, "<b>Состояние</b>\n" + "\n".join(esc(l) for l in lines) +
                f"\n\nПроверка каждые {POLL_SECONDS // 60} мин.")


last_manual = 0.0


async def cmd_check(update: Update, ctx):
    global last_manual
    if time.time() - last_manual < 60:
        return await reply(update, "Недавно уже проверяли, подождите минуту.")
    last_manual = time.time()
    await reply(update, "Проверяю таблицу…")
    await poll(ctx)
    await reply(update, "Готово. Если были изменения, они придут отдельным сообщением.")


async def cmd_stats(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    users = all_users()
    modes = sum(1 for _, p in users if p["mode"] == "digest")
    paused = sum(1 for _, p in users if p["paused"])
    n_log = db.execute("SELECT COUNT(*) FROM log").fetchone()[0]
    await reply(update, f"👥 Пользователей: {len(users)} (на паузе: {paused}, со сводкой: {modes})\n"
                        f"🕘 Записей в истории изменений: {n_log}")


async def cmd_broadcast(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        return await reply(update, "Использование: /broadcast текст")
    n = 0
    for cid, _ in all_users():
        n += await send(ctx.bot, cid, esc(text))
        await asyncio.sleep(0.05)
    await reply(update, f"Отправлено: {n}")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("settings", "Настройки и фильтры"), BotCommand("list", "Все олимпиады"),
        BotCommand("find", "Поиск олимпиады"), BotCommand("upcoming", "Ближайшие даты"),
        BotCommand("history", "История изменений"), BotCommand("calendar", "Календарь .ics"),
        BotCommand("pause", "Пауза уведомлений"), BotCommand("resume", "Включить уведомления"),
        BotCommand("status", "Состояние бота"), BotCommand("check", "Проверить таблицу сейчас"),
        BotCommand("help", "Справка")])


def main():
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for name, fn in [("start", cmd_start), ("help", cmd_help), ("settings", cmd_settings), ("list", cmd_list),
                     ("find", cmd_find), ("upcoming", cmd_upcoming), ("history", cmd_history),
                     ("calendar", cmd_calendar), ("pause", cmd_pause), ("resume", cmd_resume),
                     ("status", cmd_status), ("check", cmd_check), ("stats", cmd_stats),
                     ("broadcast", cmd_broadcast)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.job_queue.run_repeating(poll, interval=POLL_SECONDS, first=5)
    app.job_queue.run_daily(daily, time=dtime(DAILY_HOUR, 0, tzinfo=TZ))
    app.run_polling()


if __name__ == "__main__":
    main()