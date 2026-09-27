# language: Python 3.10+
# file: bot_telegram.py
# Telegram-обёртка над kassir_core. Запуск: python -u bot_telegram.py
#
# Логика: пришли ссылку на сеанс или slug → дата → сектор → ряд → места → бронь.
# Свободные места = все места зала минус занятые (занятые приходят из order/item).

import asyncio
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, USER_UUID, ALLOWED_USER_IDS
from kassir_core import (
    KassirClient, Watcher, Target,
    fetch_session, fetch_svg_map, list_sessions_for_event,
    occupied_svg_ids, free_svg_ids,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kassir_bot")


# ---------------------------------------------------------------- guard

def allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_USER_IDS


async def deny(update: Update):
    msg = "тебе сюда нельзя"
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg)


# ---------------------------------------------------------------- state

def get_state(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "sessions": [],
            "session_idx": None,
            "item": None,
            "svg_map": {},
            "available_svg_ids": set(),
            "sector": None,
            "picked_svg": [],
            "target": None,
            "watcher": None,
            "client": KassirClient(user_uuid=USER_UUID),
        }
    return ctx.user_data["state"]


def reset_progress(st: dict):
    st["sessions"] = []
    st["session_idx"] = None
    st["item"] = None
    st["svg_map"] = {}
    st["available_svg_ids"] = set()
    st["sector"] = None
    st["picked_svg"] = []


# ---------------------------------------------------------------- keyboards

def kb_sessions(sessions: list) -> InlineKeyboardMarkup:
    rows = []
    for i, s in enumerate(sessions):
        rows.append([InlineKeyboardButton(s["date_time"], callback_data=f"se:{i}")])
    rows.append([InlineKeyboardButton("← другая ссылка", callback_data="se:back")])
    return InlineKeyboardMarkup(rows)


def kb_sectors(sectors: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(s, callback_data=f"sc:{i}")]
            for i, s in enumerate(sectors)]
    rows.append([InlineKeyboardButton("← назад к датам", callback_data="sc:back")])
    return InlineKeyboardMarkup(rows)


def kb_rows(rows_list: list) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(f"ряд {r}", callback_data=f"rw:{r}") for r in rows_list]
    grid = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    grid.append([InlineKeyboardButton("← назад к секторам", callback_data="rw:back")])
    grid.append([InlineKeyboardButton("✅ показать выбранное", callback_data="rw:done")])
    return InlineKeyboardMarkup(grid)


def kb_seats(seats: list, row: int, picked_seats: set) -> InlineKeyboardMarkup:
    btns = []
    for s in seats:
        label = f"·{s}·" if s in picked_seats else str(s)
        btns.append(InlineKeyboardButton(label, callback_data=f"st:{row}:{s}"))
    grid = [btns[i:i + 6] for i in range(0, len(btns), 6)]
    grid.append([
        InlineKeyboardButton("← назад к рядам", callback_data="rw:back"),
        InlineKeyboardButton("✅ показать выбранное", callback_data="rw:done"),
    ])
    return InlineKeyboardMarkup(grid)


def kb_book() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 забронировать и следить", callback_data="book")],
        [InlineKeyboardButton("🧹 сбросить выбор мест", callback_data="unpick")],
    ])


def kb_watcher() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 стоп watcher", callback_data="stopwatch")],
        [InlineKeyboardButton("📊 статус", callback_data="status")],
    ])


# ---------------------------------------------------------------- commands

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    st = get_state(ctx)
    reset_progress(st)
    await update.message.reply_text(
        "Kassir.kg бот.\n\n"
        "Пришли ссылку на сеанс или slug — начнём.\n\n"
        "Примеры:\n"
        "• https://www.kassir.kg/ru/session/4474-raimaly-menen-begimai-etno-miuzikli\n"
        "• 4474-raimaly-menen-begimai-etno-miuzikli\n\n"
        "Команды: /status /stop"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    st = get_state(ctx)
    t: Target = st.get("target")
    if not t:
        return await update.message.reply_text("ничего не удерживается")
    try:
        held = st["watcher"]._held() if st["watcher"] else set()
    except Exception:
        held = set()
    timer = st["watcher"].basket_timer() if st["watcher"] else None
    lines = [
        f"мероприятие: {t.title}",
        f"сеанс: {t.date_time}",
        f"удержано: {len(held)}/{len(t.ticket_seat_ids)}",
        f"таймер: {timer}s" if timer is not None else "таймер: —",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    st = get_state(ctx)
    w: Watcher = st.get("watcher")
    if w:
        w.stop()
        st["watcher"] = None
        await update.message.reply_text("watcher остановлен")
    else:
        await update.message.reply_text("watcher и так не работает")


# ---------------------------------------------------------------- текст со ссылкой

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    text = (update.message.text or "").strip()

    slug = None
    m = re.search(r'/session/([a-z0-9\-]+)', text, re.I)
    if m:
        slug = m.group(1)
    elif re.match(r'^\d+-[a-z0-9\-]+$', text, re.I):
        slug = text

    if not slug:
        return await update.message.reply_text(
            "не понял. пришли ссылку вида\n"
            "https://www.kassir.kg/ru/session/4474-raimaly-menen-begimai-etno-miuzikli\n"
            "или slug 4474-raimaly-menen-begimai-etno-miuzikli"
        )

    st = get_state(ctx)
    reset_progress(st)
    await update.message.reply_text(f"загружаю {slug}…")
    try:
        sessions = list_sessions_for_event(slug)
    except Exception as e:
        return await update.message.reply_text(f"ошибка: {e!r}")

    st["sessions"] = sessions
    await update.message.reply_text(
        f"{sessions[0]['title']}\n\nВыбери дату:",
        reply_markup=kb_sessions(sessions),
    )


# ---------------------------------------------------------------- callbacks

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    st = get_state(ctx)

    # ---------- назад к другой ссылке ----------
    if data == "se:back":
        return await q.edit_message_text(
            "пришли новую ссылку или slug мероприятия"
        )

    # ---------- выбор даты ----------
    if data.startswith("se:"):
        try:
            idx = int(data.split(":")[1])
        except (ValueError, IndexError):
            return
        st["session_idx"] = idx
        s = st["sessions"][idx]
        await q.edit_message_text("Загружаю схему зала…")
        try:
            item = fetch_session(s["slug"])
            svg_map = fetch_svg_map(s["slug"])
            occupied_items = st["client"].get_order_items(s["slug"])
        except Exception as e:
            return await q.edit_message_text(f"ошибка: {e!r}")

        all_seats = item["scheme"]["seats"]
        occupied_ids = occupied_svg_ids(occupied_items)
        available_svg_ids = free_svg_ids(all_seats, occupied_ids)

        st["item"] = item
        st["svg_map"] = svg_map
        st["available_svg_ids"] = available_svg_ids
        st["picked_svg"] = []

        sectors = sorted({svg_map[i]["sector"] for i in available_svg_ids
                          if i in svg_map})
        if not sectors:
            return await q.edit_message_text("нет свободных мест")
        await q.edit_message_text(
            f"{item['event']['title']}\n{s['date_time']}\n"
            f"занято: {len(occupied_ids)}, свободно: {len(available_svg_ids)}\n\n"
            f"Выбери сектор:",
            reply_markup=kb_sectors(sectors),
        )
        return

    # ---------- назад к датам ----------
    if data == "sc:back":
        return await q.edit_message_text(
            "Выбери дату:", reply_markup=kb_sessions(st["sessions"]))

    # ---------- выбор сектора ----------
    if data.startswith("sc:"):
        try:
            idx = int(data.split(":")[1])
        except (ValueError, IndexError):
            return
        svg_map = st["svg_map"]
        avail = st["available_svg_ids"]
        sectors = sorted({svg_map[i]["sector"] for i in avail if i in svg_map})
        if idx >= len(sectors):
            return
        st["sector"] = sectors[idx]
        rows_list = sorted({svg_map[i]["row"] for i in avail
                            if i in svg_map and svg_map[i]["sector"] == st["sector"]})
        await q.edit_message_text(
            f"{st['sector']}\nВыбери ряд:",
            reply_markup=kb_rows(rows_list),
        )
        return

    # ---------- назад к секторам ----------
    if data == "rw:back":
        svg_map = st["svg_map"]
        avail = st["available_svg_ids"]
        sectors = sorted({svg_map[i]["sector"] for i in avail if i in svg_map})
        return await q.edit_message_text(
            "Выбери сектор:", reply_markup=kb_sectors(sectors))

    # ---------- показать выбранное ----------
    if data == "rw:done":
        return await show_picked(update, ctx)

    # ---------- выбор ряда ----------
    if data.startswith("rw:"):
        try:
            row = int(data.split(":")[1])
        except (ValueError, IndexError):
            return
        svg_map = st["svg_map"]
        avail = st["available_svg_ids"]
        sector = st["sector"]
        seats = sorted(svg_map[i]["seat"] for i in avail
                       if i in svg_map and svg_map[i]["sector"] == sector
                       and svg_map[i]["row"] == row)
        picked_seats = {svg_map[h]["seat"] for h in st["picked_svg"]
                        if h in svg_map
                        and svg_map[h]["sector"] == sector
                        and svg_map[h]["row"] == row}
        await q.edit_message_text(
            f"{sector}, ряд {row}\nВыбрано мест: {len(st['picked_svg'])}\n"
            f"Жми место чтобы отметить, потом «показать выбранное»:",
            reply_markup=kb_seats(seats, row, picked_seats),
        )
        return

    # ---------- клик по месту ----------
    if data.startswith("st:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        try:
            row = int(parts[1])
            seat = int(parts[2])
        except ValueError:
            return
        sector = st["sector"]
        svg_map = st["svg_map"]
        target_id = None
        for hid, info in svg_map.items():
            if info["sector"] == sector and info["row"] == row and info["seat"] == seat:
                target_id = hid
                break
        if not target_id:
            return await q.answer("не нашёл это место", show_alert=True)
        if target_id in st["picked_svg"]:
            st["picked_svg"].remove(target_id)
        else:
            st["picked_svg"].append(target_id)

        avail = st["available_svg_ids"]
        seats = sorted(svg_map[i]["seat"] for i in avail
                       if i in svg_map and svg_map[i]["sector"] == sector
                       and svg_map[i]["row"] == row)
        picked_seats = {svg_map[h]["seat"] for h in st["picked_svg"]
                        if h in svg_map
                        and svg_map[h]["sector"] == sector
                        and svg_map[h]["row"] == row}
        await q.edit_message_reply_markup(
            reply_markup=kb_seats(seats, row, picked_seats)
        )
        return

    # ---------- сброс выбора мест ----------
    if data == "unpick":
        st["picked_svg"] = []
        return await q.edit_message_text("выбор мест сброшен. жми /start для новой ссылки")

    # ---------- бронь ----------
    if data == "book":
        return await do_book(update, ctx)

    # ---------- watcher ----------
    if data == "stopwatch":
        w: Watcher = st.get("watcher")
        if w:
            w.stop()
            st["watcher"] = None
            return await q.edit_message_text("watcher остановлен")
        return await q.edit_message_text("watcher не работает")

    if data == "status":
        t: Target = st.get("target")
        if not t:
            return await q.edit_message_text("ничего не удерживается")
        try:
            held = st["watcher"]._held() if st["watcher"] else set()
        except Exception:
            held = set()
        timer = st["watcher"].basket_timer() if st["watcher"] else None
        return await q.edit_message_text(
            f"мероприятие: {t.title}\n"
            f"сеанс: {t.date_time}\n"
            f"удержано: {len(held)}/{len(t.ticket_seat_ids)}\n"
            + (f"таймер: {timer}s" if timer is not None else "таймер: —"),
            reply_markup=kb_watcher(),
        )


async def show_picked(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = get_state(ctx)
    svg_map = st["svg_map"]
    picked = st["picked_svg"]
    if not picked:
        return await q.edit_message_text("ничего не выбрано")
    lines = [f"Выбрано {len(picked)} мест:"]
    for hid in picked[:30]:
        i = svg_map.get(hid)
        if i:
            lines.append(f"• {i['sector']} ряд {i['row']} место {i['seat']}")
    if len(picked) > 30:
        lines.append(f"… и ещё {len(picked) - 30}")
    await q.edit_message_text("\n".join(lines), reply_markup=kb_book())


async def do_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = get_state(ctx)
    item = st["item"]
    picked = st["picked_svg"]
    if not item or not picked:
        return await q.answer("сначала выбери места", show_alert=True)

    ticket_map = {s["html_id"]: s["id"] for s in item["scheme"]["seats"]}
    ticket_ids = [ticket_map[h] for h in picked if h in ticket_map]
    if not ticket_ids:
        return await q.edit_message_text("не удалось сопоставить места")

    target = Target(
        slug=item["slug"],
        event_id=item["event"]["id"],
        session_id=item["id"],
        title=item["event"]["title"],
        date_time=item["date_time"],
        ticket_seat_ids=ticket_ids,
        svg_ids=picked,
        svg_map=st["svg_map"],
    )
    st["target"] = target

    chat_id = q.message.chat_id
    bot = ctx.bot

    def log_to_chat(msg: str):
        main_loop = ctx.user_data.get("main_loop")
        if not main_loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                bot.send_message(chat_id=chat_id, text=msg), main_loop
            )
        except Exception:
            pass

    watcher = Watcher(st["client"], target, on_event=log_to_chat)
    st["watcher"] = watcher

    await q.edit_message_text(
        f"Бронирую {len(ticket_ids)} мест…",
        reply_markup=kb_watcher(),
    )

    loop = asyncio.get_event_loop()
    ctx.user_data["main_loop"] = loop

    async def run_book():
        result = await loop.run_in_executor(None, watcher.book_all)
        await bot.send_message(
            chat_id=chat_id,
            text=f"готово: ok={result['ok']} fail={result['fail']}\n"
                 f"watcher следит дальше. /status /stop",
            reply_markup=kb_watcher(),
        )
        watcher.start()

    asyncio.create_task(run_book())


# ---------------------------------------------------------------- main

def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("ВСТАВЬ"):
        print("!!! вставь TELEGRAM_BOT_TOKEN в config.py")
        return
    if not USER_UUID or USER_UUID.startswith("ВСТАВЬ"):
        print("!!! вставь USER_UUID в config.py")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("бот запущен. Ctrl+C для выхода.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()