"""Bot Telegram theo dõi máy GPU trên Vast.ai (1 GPU, on-demand, verified, Việt Nam).

Mỗi người dùng có danh sách GPU, trạng thái tạm dừng và giá tối đa riêng.
"""
import asyncio
import html
import logging
import os
import time

from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      Update)
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

import gpu
from db import DB, User
from vast import VastClient, VastError

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

def env(name: str, default: str = "") -> str:
    """Đọc biến môi trường, bỏ phần comment '#' nếu dotenv để lọt vào giá trị."""
    v = os.getenv(name, "").split("#", 1)[0].strip()
    return v or default


TOKEN = env("TELEGRAM_BOT_TOKEN")
VAST_API_KEY = env("VAST_API_KEY")
if not TOKEN or not VAST_API_KEY:
    raise SystemExit("Thiếu TELEGRAM_BOT_TOKEN hoặc VAST_API_KEY trong .env")
CHECK_INTERVAL = int(env("CHECK_INTERVAL", "60"))
COUNTRY = env("COUNTRY", "VN").upper()
MIN_RELIABILITY = float(env("MIN_RELIABILITY", "0.9"))
DB_PATH = env("DB_PATH", "bot.db")
ALERT_AFTER = int(env("ALERT_AFTER_SECONDS", "900"))  # báo lỗi API sau 15 phút
ALLOWED = {int(x) for x in env("ALLOWED_CHAT_IDS").replace(" ", "").split(",") if x}

RENT_URL = "https://cloud.vast.ai/create/"
TG_LIMIT = 3900  # Telegram giới hạn 4096 ký tự/tin

db = DB(DB_PATH)
vast = VastClient(VAST_API_KEY, COUNTRY, MIN_RELIABILITY)


class State:
    offers: list[dict] = []
    fetched_at: float = 0.0
    last_error: str | None = None
    fail_since: float | None = None
    alerted: bool = False
    lock = asyncio.Lock()


S = State()


# ---------- Lấy dữ liệu ----------

async def fetch_offers(max_age: float = 0) -> list[dict]:
    """Lấy danh sách máy, dùng cache nếu còn mới hơn max_age giây."""
    async with S.lock:
        if max_age and time.time() - S.fetched_at < max_age:
            return S.offers
        try:
            offers = await vast.search()
        except VastError as e:
            S.last_error = str(e)
            if S.fail_since is None:
                S.fail_since = time.time()
            raise
        S.offers, S.fetched_at, S.last_error, S.fail_since = offers, time.time(), None, None
        return offers


def match_user(u: User, offers: list[dict]) -> list[dict]:
    out = []
    for o in offers:
        if u.max_price is not None and o["price"] > u.max_price:
            continue
        if any(gpu.matches(p, o["gpu_name"]) for p in u.gpus):
            out.append(o)
    return out


# ---------- Định dạng tin nhắn ----------

def fmt_offer(o: dict, new: bool) -> str:
    e = html.escape
    mark = "🆕 " if new else "• "
    net = ""
    if o["inet_down"] is not None:
        net = f"Mạng ↓{o['inet_down']:.0f} / ↑{(o['inet_up'] or 0):.0f} Mbps · "
    cpu = f" · {o['cpu_cores']:.0f} vCPU" if o["cpu_cores"] else ""
    return (
        f"{mark}<b>{e(o['gpu_name'])}</b> · <b>${o['price']:.3f}/giờ</b>\n"
        f"   VRAM {o['vram_gb']:.0f} GB · RAM {o['ram_gb']:.0f} GB{cpu}\n"
        f"   {net}Tin cậy {o['reliability'] * 100:.1f}%\n"
        f"   {e(o['geolocation'])} · Máy <code>{o['machine_id']}</code> · "
        f"Offer <code>{o['offer_id']}</code>"
    )


def build_messages(header: str, offers: list[dict], new_keys: set[str]) -> list[str]:
    # Máy mới lên đầu, còn lại theo giá tăng dần.
    ordered = sorted(offers, key=lambda o: (o["key"] not in new_keys, o["price"]))
    footer = f'\n\n<a href="{RENT_URL}">Mở trang thuê Vast.ai</a>'
    msgs, cur = [], header
    for o in ordered:
        block = "\n\n" + fmt_offer(o, o["key"] in new_keys)
        if len(cur) + len(block) + len(footer) > TG_LIMIT:
            msgs.append(cur)
            cur = "(tiếp)"
        cur += block
    msgs.append(cur + footer)
    return msgs


async def send(bot, chat_id: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True, **kw)
        return True
    except Forbidden:
        log.warning("Người dùng %s đã chặn bot", chat_id)
    except TelegramError as e:
        log.error("Gửi tin cho %s lỗi: %s", chat_id, e)
    return False


async def notify_if_new(bot, u: User, offers: list[dict]) -> int:
    """Nếu có máy mới so với lần trước, gửi toàn bộ danh sách máy đang khớp.

    Trả về số máy đang khớp. Luôn cập nhật last_seen để máy biến mất rồi
    xuất hiện lại sẽ được báo lại.
    """
    matched = match_user(u, offers)
    keys = {o["key"] for o in matched}
    new = keys - u.last_seen
    if new:
        header = (f"🔔 <b>Có {len(new)} máy mới</b> · "
                  f"tổng {len(matched)} máy đang khớp danh sách của bạn")
        for m in build_messages(header, matched, new):
            await send(bot, u.chat_id, m)
    u.last_seen = keys
    db.set_last_seen(u.chat_id, keys)
    return len(matched)


async def recheck_now(update: Update, u: User):
    """Dùng sau khi đổi thiết lập: xóa last_seen để gửi ngay danh sách hiện có."""
    u.last_seen = set()
    db.set_last_seen(u.chat_id, set())
    if u.paused or not u.gpus:
        return
    try:
        offers = await fetch_offers(max_age=30)
    except VastError as e:
        await update.effective_message.reply_text(f"⚠️ Không gọi được Vast.ai: {e}")
        return
    n = await notify_if_new(update.get_bot(), u, offers)
    if n == 0:
        await update.effective_message.reply_text(
            "Hiện chưa có máy nào khớp. Bot sẽ nhắn ngay khi có.")


# ---------- Vòng lặp kiểm tra ----------

async def check_job(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    try:
        offers = await fetch_offers()
    except VastError as e:
        users = [u for u in db.all() if u.gpus and not u.paused]
        log.error("Check lỗi: %s", e)
        if (S.fail_since and time.time() - S.fail_since > ALERT_AFTER and not S.alerted):
            S.alerted = True
            mins = int((time.time() - S.fail_since) // 60)
            for u in users:
                await send(bot, u.chat_id,
                           f"⚠️ Không gọi được Vast.ai suốt {mins} phút, bot đang "
                           f"không theo dõi được.\nLỗi: <code>{html.escape(str(e))}</code>")
        return
    users = [u for u in db.all() if u.gpus and not u.paused]
    if S.alerted:
        S.alerted = False
        for u in users:
            await send(bot, u.chat_id, "✅ Đã kết nối lại Vast.ai, tiếp tục theo dõi.")
    for u in users:
        await notify_if_new(bot, u, offers)


# ---------- Lệnh ----------

HELP = (
    "<b>Bot theo dõi GPU Vast.ai</b>\n"
    f"Chỉ báo máy {COUNTRY}, 1 GPU, on-demand, verified, tin cậy ≥ "
    f"{MIN_RELIABILITY * 100:.0f}%. Kiểm tra mỗi {CHECK_INTERVAL} giây.\n\n"
    "/gpu 5070ti 3090 — thêm GPU cần theo dõi\n"
    "/list — xem GPU đang theo dõi\n"
    "/remove 3090 — xóa một vài GPU\n"
    "/clear — xóa hết GPU\n"
    "/now — máy đang có khớp danh sách của bạn\n"
    f"/vn — tất cả GPU đang có ở {COUNTRY} (xem tên để thêm)\n"
    "/maxprice 0.4 — chỉ báo máy ≤ $0.4/giờ (/maxprice off để tắt)\n"
    "/pause, /resume — tạm dừng / tiếp tục nhận tin\n"
    "/status — trạng thái bot\n\n"
    "Tên GPU không phân biệt hoa thường và dấu cách: <code>5070ti</code> = "
    "<code>RTX 5070 Ti</code>. <code>3090</code> không khớp <code>3090 Ti</code>. "
    "Thêm <code>*</code> để khớp mọi biến thể: <code>h100*</code>."
)


def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


def guard(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not allowed(update):
            await update.effective_message.reply_text("Bot này là bot riêng.")
            return
        await fn(update, context, db.get(update.effective_chat.id))
    return wrapper


async def reply(update: Update, text: str, **kw):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)


def fmt_list(u: User) -> str:
    if not u.gpus:
        return "Bạn chưa theo dõi GPU nào. Dùng /gpu 5070ti để thêm."
    lines = ", ".join(f"<code>{html.escape(g)}</code>" for g in u.gpus)
    extra = []
    if u.max_price is not None:
        extra.append(f"giá ≤ ${u.max_price:g}/giờ")
    if u.paused:
        extra.append("⏸ đang tạm dừng")
    return f"Đang theo dõi: {lines}" + (f"\n({'; '.join(extra)})" if extra else "")


@guard
async def cmd_start(update, context, u: User):
    await reply(update, HELP)


@guard
async def cmd_gpu(update, context, u: User):
    pats = gpu.parse_args(context.args)
    if not pats:
        await reply(update, "Cách dùng: <code>/gpu 5070ti 3090</code>")
        return
    added = [p for p in pats if p not in u.gpus]
    u.gpus += added
    db.save(u)
    msg = (f"Đã thêm: {', '.join(added)}" if added else "Các GPU này đã có trong danh sách.")
    await reply(update, f"{msg}\n{fmt_list(u)}")
    if added:
        await recheck_now(update, u)


@guard
async def cmd_list(update, context, u: User):
    await reply(update, fmt_list(u))


@guard
async def cmd_remove(update, context, u: User):
    pats = gpu.parse_args(context.args)
    if not pats:
        await reply(update, "Cách dùng: <code>/remove 3090</code> (xóa hết dùng /clear)")
        return
    removed = [p for p in pats if p in u.gpus]
    missing = [p for p in pats if p not in u.gpus]
    u.gpus = [g for g in u.gpus if g not in removed]
    db.save(u)
    parts = []
    if removed:
        parts.append(f"Đã xóa: {', '.join(removed)}")
    if missing:
        parts.append(f"Không có trong danh sách: {', '.join(missing)}")
    await reply(update, "\n".join(parts) + "\n" + fmt_list(u))


@guard
async def cmd_clear(update, context, u: User):
    if not u.gpus:
        await reply(update, "Danh sách đang trống.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Xóa hết", callback_data="clear:yes"),
        InlineKeyboardButton("Hủy", callback_data="clear:no"),
    ]])
    await reply(update, f"Xóa toàn bộ {len(u.gpus)} GPU?\n{fmt_list(u)}", reply_markup=kb)


async def on_clear_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    if q.data == "clear:yes":
        u = db.get(update.effective_chat.id)
        u.gpus = []
        db.save(u)
        db.set_last_seen(u.chat_id, set())
        await q.edit_message_text("Đã xóa hết danh sách GPU.")
    else:
        await q.edit_message_text("Đã hủy.")


@guard
async def cmd_now(update, context, u: User):
    if not u.gpus:
        await reply(update, fmt_list(u))
        return
    try:
        offers = await fetch_offers(max_age=30)
    except VastError as e:
        await reply(update, f"⚠️ Không gọi được Vast.ai: {html.escape(str(e))}")
        return
    matched = match_user(u, offers)
    if not matched:
        await reply(update, "Hiện chưa có máy nào khớp danh sách của bạn.\n" + fmt_list(u))
        return
    header = f"📋 <b>{len(matched)} máy đang khớp danh sách của bạn</b>"
    for m in build_messages(header, matched, set()):
        await reply(update, m)


@guard
async def cmd_vn(update, context, u: User):
    try:
        offers = await fetch_offers(max_age=30)
    except VastError as e:
        await reply(update, f"⚠️ Không gọi được Vast.ai: {html.escape(str(e))}")
        return
    if not offers:
        await reply(update, f"Hiện không có máy nào ở {COUNTRY} đạt điều kiện.")
        return
    groups: dict[str, list[float]] = {}
    for o in offers:
        groups.setdefault(o["gpu_name"], []).append(o["price"])
    lines = [f"🇻🇳 <b>GPU đang có ở {COUNTRY}</b> (đạt điều kiện)\n"]
    for name, prices in sorted(groups.items(), key=lambda kv: min(kv[1])):
        lines.append(f"• {html.escape(name)}: {len(prices)} máy, từ ${min(prices):.3f}/giờ "
                     f"→ <code>/gpu {gpu.normalize(name)}</code>")
    await reply(update, "\n".join(lines))


@guard
async def cmd_maxprice(update, context, u: User):
    arg = (context.args[0] if context.args else "").lower().replace("$", "").replace(",", ".")
    if arg in ("off", "tat", "tắt", "0"):
        u.max_price = None
        db.save(u)
        await reply(update, "Đã tắt giới hạn giá.")
    else:
        try:
            v = float(arg)
            assert v > 0
        except (ValueError, AssertionError):
            cur = f"${u.max_price:g}/giờ" if u.max_price is not None else "không giới hạn"
            await reply(update, f"Hiện tại: {cur}\nCách dùng: <code>/maxprice 0.4</code> "
                                "hoặc <code>/maxprice off</code>")
            return
        u.max_price = v
        db.save(u)
        await reply(update, f"Chỉ báo máy ≤ ${v:g}/giờ.")
    await recheck_now(update, u)


@guard
async def cmd_pause(update, context, u: User):
    u.paused = True
    db.save(u)
    await reply(update, "⏸ Đã tạm dừng. Danh sách GPU vẫn giữ nguyên. Dùng /resume để tiếp tục.")


@guard
async def cmd_resume(update, context, u: User):
    u.paused = False
    db.save(u)
    await reply(update, "▶️ Đã tiếp tục theo dõi.\n" + fmt_list(u))
    await recheck_now(update, u)


@guard
async def cmd_status(update, context, u: User):
    if S.fetched_at:
        ago = int(time.time() - S.fetched_at)
        last = f"Lần kiểm tra thành công gần nhất: {ago} giây trước, {len(S.offers)} máy {COUNTRY}"
    else:
        last = "Chưa kiểm tra thành công lần nào."
    err = f"\n⚠️ Lỗi gần nhất: <code>{html.escape(S.last_error)}</code>" if S.last_error else ""
    active = sum(1 for x in db.all() if x.gpus and not x.paused)
    await reply(update, f"{last}{err}\nNgười dùng đang theo dõi: {active}\n\n{fmt_list(u)}")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("gpu", "Thêm GPU cần theo dõi"),
        BotCommand("list", "Xem GPU đang theo dõi"),
        BotCommand("remove", "Xóa một vài GPU"),
        BotCommand("clear", "Xóa hết GPU"),
        BotCommand("now", "Máy đang có khớp danh sách"),
        BotCommand("vn", "Tất cả GPU đang có ở VN"),
        BotCommand("maxprice", "Giới hạn giá mỗi giờ"),
        BotCommand("pause", "Tạm dừng nhận tin"),
        BotCommand("resume", "Tiếp tục nhận tin"),
        BotCommand("status", "Trạng thái bot"),
        BotCommand("help", "Hướng dẫn"),
    ])


async def post_shutdown(app: Application):
    await vast.close()


def main():
    app = (Application.builder().token(TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("gpu", cmd_gpu),
                     ("list", cmd_list), ("remove", cmd_remove), ("clear", cmd_clear),
                     ("now", cmd_now), ("vn", cmd_vn), ("maxprice", cmd_maxprice),
                     ("pause", cmd_pause), ("resume", cmd_resume), ("status", cmd_status)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_clear_button, pattern=r"^clear:"))
    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL, first=5)
    log.info("Bot chạy: %s, mỗi %ss, reliability ≥ %s", COUNTRY, CHECK_INTERVAL, MIN_RELIABILITY)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
