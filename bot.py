"""
Refer & Earn VPS Bot  (aiogram 3.x)

Run:   pip install -U aiogram paramiko   ->   python bot.py
Config: .env file or environment variables (see .env.example)

New features:
  - Points system: 1 refer = 1 point
  - 5 points = 1 non-root VPS (auto-created via SSH on host VPS)
  - Redeem codes (owner creates, users claim)
  - Broadcast: with button / without button
  - Referral tiers = owner-added VPS (unchanged)
  - Colored inline buttons in main menu
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from db import DB, POINTS_PER_REFERRAL, POINTS_PER_NONROOT_VPS
from utils import bar, esc, fmt_date, fmt_dt, mention, ordinal, parse_vps, remaining, sb, t


# ============================================================== config
def _load_env(path=".env"):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_ENV_ADMINS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.lstrip("-").isdigit()}
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0) or (next(iter(_ENV_ADMINS)) if _ENV_ADMINS else 0)
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").lstrip("@").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL") or f"https://t.me/{OWNER_USERNAME}"
DB_PATH = os.getenv("DB_PATH", "referbot.db")  # only used when Turso is not configured
TURSO_URL = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

# Host VPS SSH credentials for auto-creating non-root VPS
# IP is auto-detected from the server running this bot — no need to set it manually
HOST_VPS_PORT = int(os.getenv("HOST_VPS_PORT", "22") or 22)
HOST_VPS_ROOT_USER = os.getenv("HOST_VPS_ROOT_USER", "root").strip()
HOST_VPS_ROOT_PASS = os.getenv("HOST_VPS_ROOT_PASS", "").strip()


def _detect_host_ip() -> str:
    """Detect the public-facing IP of the VPS this bot is running on."""
    import socket
    try:
        # Connect to an external address (8.8.8.8) — no data is sent,
        # but the OS picks the outbound interface, giving us our own IP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(3)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


HOST_VPS_IP = _detect_host_ip()

BOT_USERNAME = ""
db = DB(DB_PATH, TURSO_URL or None, TURSO_TOKEN or None)
log = logging.getLogger("referbot")

# Coloured inline buttons (Bot API `style`): primary = blue, success = green, danger = red
P, S, D = "primary", "success", "danger"
LINE = "━━━━━━━━━━━━━━━━━━━━"
PAGE_SIZE = 10
BAR_TOTAL = 10


def _extra_admins() -> set:
    raw = db.get_setting("extra_admins")
    if not raw:
        return set()
    try:
        return {int(x) for x in json.loads(raw)}
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def all_admin_ids() -> set:
    return ({OWNER_ID} if OWNER_ID else set()) | _ENV_ADMINS | _extra_admins()


def is_owner(uid: int) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID


def progress_emoji(pct: int) -> str:
    if pct <= 0:
        return "🌀"
    if pct < 20:
        return "🌒"
    if pct < 30:
        return "🌓"
    if pct < 40:
        return "🌔"
    if pct < 50:
        return "🌕"
    if pct < 60:
        return "⚡"
    if pct < 70:
        return "🔥"
    if pct < 80:
        return "💫"
    if pct < 90:
        return "✨"
    if pct < 100:
        return "🚀"
    return "🎯"


def progress_bar(filled: int, total: int = BAR_TOTAL) -> str:
    filled = max(0, min(total, filled))
    return "▰" * filled + "▱" * (total - filled)


async def animate_boot(msg: Message, delay: float = 0.04):
    """Fast boot bar 0→100%, then Successful. Caller deletes afterwards."""
    steps = 5
    for i in range(steps + 1):
        filled = int((i / steps) * BAR_TOTAL)
        pct = int((i / steps) * 100)
        text = (
            f"⚡ <b>INITIALIZING…</b>\n{LINE}\n"
            f"<code>{progress_bar(filled)}  {pct:3d}%  {progress_emoji(pct)}</code>"
        )
        try:
            await msg.edit_text(text)
        except TelegramBadRequest:
            pass
        if i < steps:
            await asyncio.sleep(delay)
    try:
        await msg.edit_text(f"✅ <b>SUCCESSFUL</b>\n{LINE}\nLoading profile…")
    except TelegramBadRequest:
        pass
    await asyncio.sleep(0.12)


# ============================================== quoted replies (global)
def quote_wrap(html_text):
    """Every bot message becomes ONE quote block (nested quotes are not allowed by Telegram)."""
    inner = re.sub(r"</?blockquote[^>]*>", "", html_text)
    return f"<blockquote>{inner}</blockquote>"


class QuoteMW(BaseRequestMiddleware):
    """Wraps text / captions of every outgoing message in a blockquote.
    copy_message (broadcast) is left untouched so broadcasts stay exactly as written."""

    async def __call__(self, make_request, bot, method):
        name = type(method).__name__
        if name in ("SendMessage", "EditMessageText") and getattr(method, "text", None):
            method = method.model_copy(update={"text": quote_wrap(method.text)})
        elif name in ("SendPhoto", "EditMessageCaption") and getattr(method, "caption", None):
            method = method.model_copy(update={"caption": quote_wrap(method.caption)})
        return await make_request(bot, method)


# ============================================================== helpers
def b(text, cb=None, url=None, style=None):
    """Inline button (sub-screens)."""
    kw = {"text": sb(text)}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def rb(text, style=None):
    """Reply-keyboard button (main menu — plain text so Telegram matches reliably)."""
    kw = {"text": text}  # do NOT bold — clients must send the exact label back
    if style:
        kw["style"] = style
    try:
        return KeyboardButton(**kw)
    except TypeError:
        kw.pop("style", None)
        return KeyboardButton(**kw)


def _is_btn(text: str, label: str) -> bool:
    """Match reply-button press even if client strips fancy unicode."""
    if not text:
        return False
    t1 = text.strip()
    if t1 == label or t1 == sb(label):
        return True
    # emoji-stripped compare
    import re as _re
    bare = _re.sub(r"[^\w\s]", "", t1, flags=_re.UNICODE).strip().lower()
    bare_l = _re.sub(r"[^\w\s]", "", label, flags=_re.UNICODE).strip().lower()
    return bare == bare_l or bare_l in bare


def full_name(u):
    return (u.full_name or str(u.id)).strip()[:64]


def ref_link(uid):
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"


def is_admin(uid):
    return uid in all_admin_ids()


# Main menu button labels (must match handlers)
BTN_MY_VPS = "🖥 My VPS"
BTN_GET_VPS = "🎁 Get VPS"
BTN_REF = "🔗 Referral"
BTN_MYREF = "👥 My Referral"
BTN_LB = "🏆 Leaderboard"
BTN_PROF = "👤 My Profile"
BTN_BUY = "🛒 Buy VPS"
BTN_ADMIN = "🛡 Admin Panel"
BTN_POINTS = "⭐ My Points"
BTN_REDEEM = "🎟 Redeem Code"
BTN_NONROOT = "🖱 My Non-Root VPS"


async def show(c: CallbackQuery, text, markup):
    """Edit the current screen (handles photo messages and 'not modified')."""
    msg = c.message
    try:
        if msg.photo:
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            await c.bot.send_message(msg.chat.id, text, reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        await c.bot.send_message(msg.chat.id, text, reply_markup=markup)


# ============================================================== views
def home_kb(uid):
    """Main menu as InlineKeyboard with colored buttons."""
    pts = db.get_user_points(uid)
    rows = [
        [b(BTN_MY_VPS, "u:vps", style=P), b(BTN_GET_VPS, "u:get", style=S)],
        [b(BTN_REF, "u:ref", style=S), b(BTN_MYREF, "u:myref:0", style=P)],
        [b(BTN_LB, "u:lb", style=P), b(BTN_PROF, "u:prof", style=P)],
        [b(f"⭐ Points: {pts}", "u:points", style=S), b(BTN_REDEEM, "u:redeem", style=S)],
        [b(BTN_NONROOT, "u:nonroot", style=P)],
        [b(BTN_BUY, "u:buy", style=D)],
    ]
    if is_admin(uid):
        rows.append([b(BTN_ADMIN, "a:panel", style=D)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_view(uid, name):
    row = db.get_user(uid)
    pts = db.get_user_points(uid)
    text = t(
        "✦ <b>REFER AND EARN VPS</b> ✦\n"
        f"{LINE}\n"
        "👋 Welcome, {who}!\n\n"
        "Invite your friends, complete the referral targets and win free premium VPS servers.\n\n"
        "<blockquote>👤 Name : {name}\n"
        "🆔 ID : <code>{uid}</code>\n"
        "👥 Referrals : {refs}\n"
        "⭐ Points : {pts}\n"
        "🖥 VPS Won : {won}</blockquote>\n"
        f"{LINE}\n"
        "👇 Choose an option below",
        who=mention(uid, name),
        name=esc(name),
        uid=str(uid),
        refs=row["referral_count"] if row else 0,
        pts=pts,
        won=row["vps_claimed"] if row else 0,
    )
    return text, home_kb(uid)


async def send_home(bot, chat_id, uid, name):
    """Show home card with inline buttons."""
    text, markup = home_view(uid, name)
    pfp = db.get_setting("pfp")
    if pfp:
        try:
            await bot.send_photo(chat_id, pfp, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            log.warning("welcome photo failed: %s", e)
    await bot.send_message(chat_id, text, reply_markup=markup)


async def show_home(c: CallbackQuery):
    uid, name = c.from_user.id, full_name(c.from_user)
    text, markup = home_view(uid, name)
    pfp = db.get_setting("pfp")
    if pfp and c.message.photo:
        try:
            await c.message.edit_caption(caption=text, reply_markup=markup)
        except TelegramBadRequest:
            pass
        return
    if pfp:
        try:
            await c.message.delete()
        except TelegramBadRequest:
            pass
        await send_home(c.bot, c.message.chat.id, uid, name)
        return
    await show(c, text, markup)


def back_row(target="u:menu"):
    return [b("⬅️ Back", target, style=P)]


def vps_card(v, idx, now):
    """Clean, copy-friendly VPS details card."""
    active = v["expires_at"] > now
    host = esc(v["host"])
    port = esc(v["port"])
    user = esc(v["username"])
    pw = esc(v["password"])
    status = "🟢 ACTIVE" if active else "🔴 EXPIRED"
    ssh = f"ssh {v['username']}@{v['host']} -p {v['port']}"
    return (
        f"🖥 <b>VPS #{idx}</b>  ·  {status}\n"
        f"{LINE}\n"
        f"<blockquote>"
        f"🌐 <b>Host</b>     : <code>{host}</code>\n"
        f"🔌 <b>Port</b>     : <code>{port}</code>\n"
        f"👤 <b>Username</b> : <code>{user}</code>\n"
        f"🔑 <b>Password</b> : <code>{pw}</code>\n"
        f"📅 <b>Assigned</b> : {fmt_dt(v['assigned_at'])}\n"
        f"⏳ <b>Expires</b>  : {fmt_dt(v['expires_at'])}\n"
        f"🕒 <b>Left</b>     : {remaining(v['expires_at'], now)}"
        f"</blockquote>\n\n"
        f"💻 <b>SSH</b>\n<code>{esc(ssh)}</code>"
    )


def tier_lines(st):
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, target in enumerate(st["tiers"]):
        out.append(
            t(
                "{icon} {ordn} VPS › {n} referrals {state}",
                icon=medals[i] if i < 3 else "🏅",
                ordn=ordinal(i),
                n=target,
                state="✅" if st["count"] >= target else "🔒",
            )
        )
    return "\n".join(out)


def progress_block(st):
    if st["next"] is None:
        return t("🏁 All reward tiers completed!")
    if st["pending"]:
        return t("🎁 A VPS is ready to claim — tap <b>Get VPS</b>!")
    return t(
        "🎯 Next VPS : {nxt} referrals\n{bar}  {cur}/{nxt2}\n📌 {left} more to go",
        nxt=st["next"],
        bar=bar(st["count"], st["next"]),
        cur=st["count"],
        nxt2=st["next"],
        left=st["next"] - st["count"],
    )


# ============================================================ force join
_ok_cache = {}


def _clear_fj_cache(user_id=None):
    """Drop membership cache (all users, or one user)."""
    if user_id is None:
        _ok_cache.clear()
        return
    for k in list(_ok_cache):
        if k[1] == user_id:
            _ok_cache.pop(k, None)


async def _is_member(bot, chat, user_id, fresh=False):
    key = (chat["chat_id"], user_id)
    if not fresh and time.time() - _ok_cache.get(key, 0) < 20:
        return True
    ok = False
    try:
        m = await bot.get_chat_member(chat["chat_id"], user_id)
        status = m.status
        # aiogram may return enum or str depending on version
        status_val = status.value if hasattr(status, "value") else str(status)
        if status_val in (
            "member",
            "administrator",
            "creator",
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        ) or (
            status_val in ("restricted", ChatMemberStatus.RESTRICTED)
            and getattr(m, "is_member", False)
        ):
            ok = True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # bot lost access to that chat -> fail open so users are not locked out
        log.warning("force-join check failed for %s: %s", chat["chat_id"], e)
        ok = True
    if not ok and chat["is_private"]:
        ok = db.has_join_request(chat["chat_id"], user_id)  # join request seen = allowed
    if ok:
        _ok_cache[key] = time.time()
    return ok


async def missing_chats(bot, user_id, fresh=False):
    if is_admin(user_id):
        return []
    out = []
    for chat in db.list_fj():
        if not await _is_member(bot, chat, user_id, fresh):
            out.append(chat)
    return out


def gate_view(missing):
    text = t(
        "🔒 <b>ACCESS LOCKED</b>\n"
        f"{LINE}\n"
        "Join all the channels / groups below to use this bot.\n\n"
        "<blockquote>Private chat? Send a join request, then tap Verify.</blockquote>"
    )
    rows, row = [], []
    for ch in missing:
        title = (ch["title"] or str(ch["chat_id"]))[:18]
        link = ch["link"] if ch["link"] else None
        if not link and ch["username"]:
            link = f"https://t.me/{ch['username']}"
        if link:
            row.append(b(f"Join {title}", url=link, style=P))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([b("✅ Verify", "u:verify", style=S)])
    return text, kb(*rows)


async def credit_referral(bot, uid):
    """Count the referral (once) and notify the referrer."""
    res = db.credit_referral(uid)
    if not res:
        return
    ref_id, count = res["referrer_id"], res["count"]
    joined = db.get_user(uid)
    st = db.claim_status(ref_id)
    pts = db.get_user_points(ref_id)
    if st["next"] is not None and st["pending"] == 0:
        nxt = t(
            "🎯 Next target : {n} ({left} more)", n=st["next"], left=st["next"] - st["count"]
        )
    else:
        nxt = t("🎯 Next target : —")
    text = t(
        "🔔 <b>NEW REFERRAL</b>\n"
        f"{LINE}\n"
        "{who} joined the bot using your referral link!\n\n"
        "<blockquote>👥 Total Referrals : {n}\n"
        "⭐ Your Points : {pts}\n"
        "{nxt}</blockquote>",
        who=mention(uid, joined["name"] if joined else "User"),
        n=count,
        pts=pts,
        nxt=nxt,
    )
    try:
        await bot.send_message(ref_id, text)
        tiers = db.get_tiers()
        if count in tiers:
            i = tiers.index(count)
            await bot.send_message(
                ref_id,
                t(
                    "🎉 <b>CONGRATULATIONS!</b> 🎉\n"
                    f"{LINE}\n"
                    "You completed the <b>{ordn} VPS</b> target of {n} referrals!\n\n"
                    "<blockquote>🎁 Your reward is ready. Tap the button below to claim it.</blockquote>",
                    ordn=ordinal(i),
                    n=count,
                ),
                reply_markup=kb([b("🎁 Get VPS", "u:get", style=S)]),
            )
        # Notify if points crossed multiple of POINTS_PER_NONROOT_VPS
        if pts % POINTS_PER_NONROOT_VPS == 0 and pts > 0:
            await bot.send_message(
                ref_id,
                t(
                    "⭐ <b>POINTS MILESTONE!</b>\n"
                    f"{LINE}\n"
                    "You have <b>{pts}</b> points now!\n\n"
                    "<blockquote>🖥 Tap below to get your free Non-Root VPS!\n"
                    "(5 points = 1 Non-Root VPS)</blockquote>",
                    pts=pts,
                ),
                reply_markup=kb([b("🖱 Get Non-Root VPS", "u:getnonroot", style=S)]),
            )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


class Gate(BaseMiddleware):
    """Blocks every user action until all force-join chats are joined."""

    async def __call__(self, handler, event, data):
        user = event.from_user
        if user is None or is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "u:verify":
            return await handler(event, data)
        db.upsert_user(user.id, full_name(user))
        missing = await missing_chats(event.bot, user.id)
        if missing:
            text, markup = gate_view(missing)
            if isinstance(event, CallbackQuery):
                await event.answer()
                await show(event, text, markup)
            else:
                await event.answer(text, reply_markup=markup)
            return None
        await credit_referral(event.bot, user.id)
        return await handler(event, data)


# ================================================================ routers
admin = Router(name="admin")
user = Router(name="user")
# Dynamic admin check (env + DB extra_admins)
admin.message.filter(F.from_user.id.func(lambda i: is_admin(i)))
admin.callback_query.filter(F.from_user.id.func(lambda i: is_admin(i)))
user.message.middleware(Gate())
user.callback_query.middleware(Gate())

NOT_CMD = ~Command("start", "cancel", "admin")


class St(StatesGroup):
    add_vps = State()
    fj_add = State()
    tier_edit = State()
    tier_add = State()
    days = State()
    bc = State()
    bc_buttons = State()       # waiting for button name+url
    pfp = State()
    adm_add = State()
    redeem_use = State()       # user entering redeem code
    redeem_create_code = State()   # owner entering code
    redeem_create_pts = State()    # owner entering points
    redeem_create_claims = State() # owner entering max claims
    nonroot_user = State()     # asking desired username for new account
    nonroot_pass = State()     # asking desired password for new account


# ================================================================ USER
@user.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    u = m.from_user
    ref = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        ref = int(command.args[4:])
    db.upsert_user(u.id, full_name(u), ref)

    # Loading bar 0→100% → Successful → delete → then menu / force-join
    boot = await m.answer(
        f"⚡ <b>INITIALIZING…</b>\n{LINE}\n"
        f"<code>{progress_bar(0)}    0%  🌀</code>"
    )
    try:
        await animate_boot(boot)
    except Exception:  # noqa: BLE001
        log.exception("boot animation failed")
    try:
        await boot.delete()
    except TelegramBadRequest:
        pass

    try:
        missing = await missing_chats(m.bot, u.id)
        if missing:
            text, markup = gate_view(missing)
            await m.answer(text, reply_markup=markup)
            return
        await credit_referral(m.bot, u.id)
        await send_home(m.bot, m.chat.id, u.id, full_name(u))
    except Exception:  # noqa: BLE001
        log.exception("start home failed for %s", u.id)
        try:
            await m.answer(t("⚠️ Something went wrong. Tap /start again."))
        except Exception:  # noqa: BLE001
            pass


@user.callback_query(F.data == "u:verify")
async def cb_verify(c: CallbackQuery):
    u = c.from_user
    db.upsert_user(u.id, full_name(u))
    _clear_fj_cache(u.id)
    missing = await missing_chats(c.bot, u.id, fresh=True)
    if missing:
        await c.answer(sb("You have not joined everything yet!"), show_alert=True)
        text, markup = gate_view(missing)
        await show(c, text, markup)
        return
    await c.answer(sb("Verified ✅"))
    await credit_referral(c.bot, u.id)
    await send_home(c.bot, c.message.chat.id, u.id, full_name(u))


@user.callback_query(F.data == "u:menu")
async def cb_menu(c: CallbackQuery):
    await c.answer()
    await show_home(c)


# Also handle old reply keyboard presses by ignoring them
@user.message(F.text.func(lambda txt: any(_is_btn(txt, lbl) for lbl in [
    BTN_MY_VPS, BTN_GET_VPS, BTN_REF, BTN_MYREF, BTN_LB, BTN_PROF, BTN_BUY,
    BTN_POINTS, BTN_REDEEM, BTN_NONROOT
])))
async def msg_menu_btn(m: Message):
    """Route legacy reply-keyboard presses to inline handlers."""
    txt = m.text.strip()
    uid = m.from_user.id
    if _is_btn(txt, BTN_MY_VPS):
        text, markup = _my_vps_payload(uid)
        await m.answer(text, reply_markup=markup)
    elif _is_btn(txt, BTN_GET_VPS):
        text, markup, vps_row, claim_n = _get_vps_payload(uid)
        await m.answer(text, reply_markup=markup)
        if vps_row is not None:
            await notify_admins_vps_claim(m.bot, m.from_user, vps_row, claim_n)
    elif _is_btn(txt, BTN_REF):
        text, markup = _referral_payload(uid)
        await m.answer(text, reply_markup=markup)
    elif _is_btn(txt, BTN_MYREF):
        await _send_myref(m, uid, 0)
    elif _is_btn(txt, BTN_LB):
        text, markup = _leaderboard_payload(uid)
        await m.answer(text, reply_markup=markup)
    elif _is_btn(txt, BTN_PROF):
        text, markup = _profile_payload(uid, full_name(m.from_user))
        await m.answer(text, reply_markup=markup)
    elif _is_btn(txt, BTN_BUY):
        text, markup = _buy_payload()
        await m.answer(text, reply_markup=markup)
    elif _is_btn(txt, BTN_POINTS):
        text, markup = _points_payload(uid)
        await m.answer(text, reply_markup=markup)
    elif _is_btn(txt, BTN_REDEEM):
        await m.answer(
            t("🎟 <b>REDEEM CODE</b>\n{line}\nSend your redeem code:", line=LINE),
            reply_markup=kb(back_row()),
        )
    elif _is_btn(txt, BTN_NONROOT):
        text, markup = _nonroot_vps_payload(uid)
        await m.answer(text, reply_markup=markup)


# --------------------------------------------------------- My VPS
def _my_vps_payload(uid):
    now = int(time.time())
    rows = db.user_vps(uid)
    if not rows:
        st = db.claim_status(uid)
        text = t(
            "🖥 <b>MY VPS</b>\n"
            f"{LINE}\n"
            "You don't have any VPS yet.\n\n"
            "<blockquote>{prog}</blockquote>",
            prog=progress_block(st),
        )
        markup = kb(
            [b("🎁 Get VPS", "u:get", style=S), b("🔗 Referral", "u:ref", style=P)],
            back_row(),
        )
    else:
        shown = rows[-6:]
        start_idx = len(rows) - len(shown) + 1
        cards = "\n\n".join(vps_card(v, start_idx + i, now) for i, v in enumerate(shown))
        text = t("🖥 <b>MY VPS</b>\n" f"{LINE}\n") + cards
        markup = kb([b("🎁 Get VPS", "u:get", style=S)], back_row())
    return text, markup


@user.callback_query(F.data == "u:vps")
async def cb_my_vps(c: CallbackQuery):
    await c.answer()
    await show(c, *_my_vps_payload(c.from_user.id))


# --------------------------------------------------------- Referral
def _referral_payload(uid):
    st = db.claim_status(uid)
    pts = db.get_user_points(uid)
    link = ref_link(uid)
    pts_progress = pts % POINTS_PER_NONROOT_VPS
    text = t(
        "🔗 <b>YOUR REFERRAL LINK</b>\n"
        f"{LINE}\n"
        '<a href="{link}">{link}</a>\n\n'
        "<blockquote>👥 Total Referrals : {n}\n"
        "⭐ Your Points : {pts}</blockquote>\n\n"
        "🎯 <b>REFERRAL REWARD TIERS</b>\n"
        "{tiers}\n\n"
        "{prog}\n\n"
        "⭐ <b>POINTS REWARD</b>\n"
        "<blockquote>Every referral = 1 point\n"
        "5 points = 1 Non-Root VPS 🖥\n"
        "Progress: {pts_now}/{pts_need} pts</blockquote>\n\n"
        "Share your link — you get notified the moment someone joins!",
        link=link,
        n=st["count"],
        pts=pts,
        tiers=tier_lines(st),
        prog=progress_block(st),
        pts_now=pts_progress,
        pts_need=POINTS_PER_NONROOT_VPS,
    )
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('Join and win a free VPS! 🚀')}"
    markup = kb(
        [b("📤 Share Link", url=share, style=S)],
        [b("🎁 Get VPS", "u:get", style=S), b("👥 My Referral", "u:myref:0", style=P)],
        [b("⭐ My Points", "u:points", style=S)],
        back_row(),
    )
    return text, markup


@user.callback_query(F.data == "u:ref")
async def cb_referral(c: CallbackQuery):
    await c.answer()
    await show(c, *_referral_payload(c.from_user.id))


# --------------------------------------------------------- Leaderboard
def _leaderboard_payload(uid):
    rows = db.leaderboard(10)
    medals = ["🥇", "🥈", "🥉"]
    if rows:
        lines = []
        for i, r in enumerate(rows):
            lines.append(
                t(
                    "{medal} {who} — <b>{n}</b>",
                    medal=medals[i] if i < 3 else sb(f"{i + 1}."),
                    who=mention(r["user_id"], r["name"]),
                    n=r["referral_count"],
                )
            )
        body = "\n".join(lines)
    else:
        body = t("No referrals yet — be the first one!")
    rank = db.rank(uid)
    mine = (
        t("\n\n<blockquote>📍 Your Rank : #{r}</blockquote>", r=rank) if rank else ""
    )
    text = t("🏆 <b>TOP 10 LEADERBOARD</b>\n" f"{LINE}\n") + body + mine
    return text, kb(back_row())


@user.callback_query(F.data == "u:lb")
async def cb_leaderboard(c: CallbackQuery):
    await c.answer()
    await show(c, *_leaderboard_payload(c.from_user.id))


# --------------------------------------------------------- Admin notify VPS claim
async def notify_admins_vps_claim(bot, user, vps_row, claim_n):
    """Send full user + VPS details to every admin when a VPS is claimed."""
    admins = all_admin_ids()
    if not admins or not vps_row:
        return
    now = int(time.time())
    uid = user.id
    name = full_name(user)
    uname = f"@{user.username}" if getattr(user, "username", None) else "—"
    row = db.get_user(uid)
    refs = row["referral_count"] if row else 0
    won = row["vps_claimed"] if row else claim_n
    text = (
        f"🎁 <b>VPS CLAIMED — ADMIN ALERT</b>\n{LINE}\n"
        f"<blockquote>"
        f"👤 User : {mention(uid, name)}\n"
        f"🆔 ID : <code>{uid}</code>\n"
        f"🔗 Username : {esc(uname)}\n"
        f"👥 Referrals : {refs}\n"
        f"🖥 VPS Won (total) : {won}"
        f"</blockquote>\n\n"
        f"{vps_card(vps_row, claim_n, now)}"
    )
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("admin notify failed for %s: %s", admin_id, e)


# --------------------------------------------------------- Get VPS (referral tier)
def _get_vps_payload(uid):
    """Returns (text, markup, vps_row_or_None, claim_index)."""
    now = int(time.time())
    status, payload = db.claim_vps(uid)
    claimed_vps = None
    claim_n = 0
    if status == "ok":
        claim_n = len(db.user_vps(uid))
        claimed_vps = payload
        text = (
            f"🎉 <b>CONGRATULATIONS!</b>\n"
            f"{LINE}\n"
            f"You unlocked a free premium VPS.\n"
            f"Tap any value below to copy.\n\n"
            f"{vps_card(payload, claim_n, now)}\n\n"
            f"<blockquote>⚠️ Keep these details private. Do not share your password.</blockquote>"
        )
        markup = kb([b("🖥 My VPS", "u:vps", style=P)], back_row())
    elif status == "no_stock":
        text = t(
            "⚠️ <b>OUT OF STOCK</b>\n"
            f"{LINE}\n"
            "My database has run out of VPS.\n"
            "Please contact the owner.\n\n"
            "<blockquote>Your reward is safe — you can claim it again once stock is added.</blockquote>"
        )
        markup = kb([b("💬 Support", url=SUPPORT_URL, style=S)], back_row())
    elif status == "not_eligible":
        text = t(
            "🔒 <b>NOT ELIGIBLE YET</b>\n"
            f"{LINE}\n"
            "Complete the referral target to unlock your VPS.\n\n"
            "<blockquote>{prog}</blockquote>",
            prog=progress_block(db.claim_status(uid)),
        )
        markup = kb([b("🔗 Referral", "u:ref", style=S)], back_row())
    elif status == "all_claimed":
        text = t(
            "🏁 <b>ALL TIERS CLAIMED</b>\n"
            f"{LINE}\n"
            "You have already claimed every reward VPS. Need more? Buy one from the owner."
        )
        markup = kb([b("🛒 Buy VPS", "u:buy", style=D)], back_row())
    else:
        text = t("Please press /start first.")
        markup = kb(back_row())
    return text, markup, claimed_vps, claim_n


@user.callback_query(F.data == "u:get")
async def cb_get_vps(c: CallbackQuery):
    await c.answer()
    text, markup, vps_row, claim_n = _get_vps_payload(c.from_user.id)
    await show(c, text, markup)
    if vps_row is not None:
        await notify_admins_vps_claim(c.bot, c.from_user, vps_row, claim_n)


# --------------------------------------------------------- Points
def _points_payload(uid):
    pts = db.get_user_points(uid)
    needed = POINTS_PER_NONROOT_VPS
    progress_filled = int((pts % needed) / needed * BAR_TOTAL)
    progress_pct = (pts % needed) * 100 // needed
    nonroot_list = db.user_nonroot_vps(uid)
    text = t(
        "⭐ <b>MY POINTS</b>\n"
        f"{LINE}\n"
        "<blockquote>⭐ Current Points : <b>{pts}</b>\n"
        "🖥 Non-Root VPS Claimed : {nr}\n"
        "🎯 Each Referral = {pp} point(s)\n"
        "🔓 {pnr} pts = 1 Non-Root VPS</blockquote>\n\n"
        "📊 <b>PROGRESS TO NEXT VPS</b>\n"
        "<code>{bar}  {pct}%</code>\n"
        "<blockquote>{now}/{need} pts</blockquote>",
        pts=pts,
        nr=len(nonroot_list),
        pp=POINTS_PER_REFERRAL,
        pnr=POINTS_PER_NONROOT_VPS,
        bar=progress_bar(progress_filled),
        pct=progress_pct,
        now=pts % needed,
        need=needed,
    )
    rows = []
    if pts >= needed:
        rows.append([b("🖥 Get Non-Root VPS", "u:getnonroot", style=S)])
    rows.append([b("🔗 Referral Link", "u:ref", style=P)])
    rows.append(back_row())
    return text, kb(*rows)


@user.callback_query(F.data == "u:points")
async def cb_points(c: CallbackQuery):
    await c.answer()
    await show(c, *_points_payload(c.from_user.id))


# --------------------------------------------------------- Non-Root VPS (points)
def _nonroot_vps_payload(uid):
    now = int(time.time())
    rows = db.user_nonroot_vps(uid)
    pts = db.get_user_points(uid)
    if not rows:
        text = t(
            "🖱 <b>MY NON-ROOT VPS</b>\n"
            f"{LINE}\n"
            "You don't have any Non-Root VPS yet.\n\n"
            "<blockquote>⭐ Your Points : {pts}\n"
            "Need {need} pts to get a Non-Root VPS\n"
            "Earn by referring friends!</blockquote>",
            pts=pts,
            need=POINTS_PER_NONROOT_VPS,
        )
    else:
        cards = []
        for i, v in enumerate(rows[-6:], 1):
            active = not v["expires_at"] or v["expires_at"] > now
            status = "🟢 ACTIVE" if active else "🔴 EXPIRED"
            ssh = f"ssh {v['username']}@{v['host']} -p {v['port']}"
            cards.append(
                f"🖱 <b>Non-Root VPS #{i}</b>  ·  {status}\n"
                f"{LINE}\n"
                f"<blockquote>"
                f"🌐 Host : <code>{esc(v['host'])}</code>\n"
                f"🔌 Port : <code>{esc(v['port'])}</code>\n"
                f"👤 User : <code>{esc(v['username'])}</code>\n"
                f"🔑 Pass : <code>{esc(v['password'])}</code>"
                f"</blockquote>\n"
                f"💻 SSH: <code>{esc(ssh)}</code>"
            )
        text = t("🖱 <b>MY NON-ROOT VPS</b>\n" f"{LINE}\n") + "\n\n".join(cards)
    rows_kb = []
    if pts >= POINTS_PER_NONROOT_VPS:
        rows_kb.append([b("⚡ Get Non-Root VPS", "u:getnonroot", style=S)])
    rows_kb.append([b("⭐ My Points", "u:points", style=P)])
    rows_kb.append(back_row())
    return text, kb(*rows_kb)


@user.callback_query(F.data == "u:nonroot")
async def cb_nonroot_vps(c: CallbackQuery):
    await c.answer()
    await show(c, *_nonroot_vps_payload(c.from_user.id))


@user.callback_query(F.data == "u:getnonroot")
async def cb_get_nonroot(c: CallbackQuery, state: FSMContext):
    """Check points, then ask for desired username/password for new non-root account."""
    uid = c.from_user.id
    pts = db.get_user_points(uid)
    if pts < POINTS_PER_NONROOT_VPS:
        await c.answer(
            sb(f"Need {POINTS_PER_NONROOT_VPS} points! You have {pts}."), show_alert=True
        )
        return
    await c.answer()
    await state.set_state(St.nonroot_user)
    await state.update_data(uid=uid)
    await show(
        c,
        t(
            "🖱 <b>CREATE NON-ROOT VPS</b>\n"
            f"{LINE}\n"
            "Bot will create a new non-root user on the host VPS.\n\n"
            "<blockquote>⭐ Cost : {pts} points\n"
            "Send the <b>username</b> you want for your new account:</blockquote>",
            pts=POINTS_PER_NONROOT_VPS,
        ),
        kb([b("❌ Cancel", "u:menu", style=D)]),
    )


@user.message(St.nonroot_user, NOT_CMD)
async def nonroot_user_msg(m: Message, state: FSMContext):
    username = (m.text or "").strip()
    if not username or " " in username or len(username) > 32:
        await m.answer(
            t("❌ Invalid username. No spaces, max 32 chars."),
            reply_markup=kb([b("❌ Cancel", "u:menu", style=D)]),
        )
        return
    await state.update_data(nr_new_user=username)
    await state.set_state(St.nonroot_pass)
    msg_text = t(
        "✅ Username: <code>{u}</code>\n\nNow send the <b>password</b> you want for this new account:",
        u=esc(username),
    )
    await m.answer(msg_text, reply_markup=kb([b("❌ Cancel", "u:menu", style=D)]))
    try:
        await m.delete()
    except TelegramBadRequest:
        pass


@user.message(St.nonroot_pass, NOT_CMD)
async def nonroot_pass_msg(m: Message, state: FSMContext):
    pw = (m.text or "").strip()
    if not pw:
        await m.answer(t("❌ Send a password."), reply_markup=kb([b("❌ Cancel", "u:menu", style=D)]))
        return
    try:
        await m.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    await state.clear()
    uid = data.get("uid") or m.from_user.id

    # Re-check points
    pts = db.get_user_points(uid)
    if pts < POINTS_PER_NONROOT_VPS:
        await m.answer(
            t("❌ Not enough points! You need {n} pts.", n=POINTS_PER_NONROOT_VPS),
            reply_markup=kb(back_row()),
        )
        return

    # SSH target is always this bot's own VPS (auto-detected IP + env credentials)
    ssh_host = HOST_VPS_IP
    ssh_port = HOST_VPS_PORT
    ssh_root_user = HOST_VPS_ROOT_USER
    ssh_root_pass = HOST_VPS_ROOT_PASS
    new_user = data.get("nr_new_user", "")
    new_pass = pw

    wait_msg = await m.answer(t("⏳ Creating your non-root VPS user... please wait."))

    success, result = await asyncio.get_event_loop().run_in_executor(
        None, create_nonroot_user, ssh_host, ssh_port, ssh_root_user, ssh_root_pass, new_user, new_pass
    )

    try:
        await wait_msg.delete()
    except TelegramBadRequest:
        pass

    if success:
        # Deduct points
        db.deduct_points(uid, POINTS_PER_NONROOT_VPS)
        # Save VPS record
        db.save_nonroot_vps(uid, ssh_host, str(ssh_port), new_user, new_pass)
        pts_now = db.get_user_points(uid)
        ssh_cmd = f"ssh {new_user}@{ssh_host} -p {ssh_port}"
        text = (
            f"🎉 <b>NON-ROOT VPS CREATED!</b>\n"
            f"{LINE}\n"
            f"<blockquote>"
            f"🌐 Host : <code>{esc(ssh_host)}</code>\n"
            f"🔌 Port : <code>{ssh_port}</code>\n"
            f"👤 Username : <code>{esc(new_user)}</code>\n"
            f"🔑 Password : <code>{esc(new_pass)}</code>\n"
            f"⭐ Points Left : {pts_now}"
            f"</blockquote>\n\n"
            f"💻 <b>SSH Command</b>\n<code>{esc(ssh_cmd)}</code>\n\n"
            f"<blockquote>⚠️ Keep these credentials private!</blockquote>"
        )
        await m.answer(text, reply_markup=kb(
            [b("🖱 My Non-Root VPS", "u:nonroot", style=P)],
            back_row(),
        ))
        # Notify admins
        for admin_id in all_admin_ids():
            try:
                await m.bot.send_message(
                    admin_id,
                    f"🖱 <b>NON-ROOT VPS CREATED</b>\n{LINE}\n"
                    f"<blockquote>👤 User: {mention(uid, full_name(m.from_user))}\n"
                    f"🆔 ID: <code>{uid}</code>\n"
                    f"🌐 Host: <code>{esc(ssh_host)}</code>\n"
                    f"👤 New User: <code>{esc(new_user)}</code></blockquote>",
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
    else:
        await m.answer(
            t(
                "❌ <b>FAILED TO CREATE VPS USER</b>\n"
                f"{LINE}\n"
                "<blockquote>{err}</blockquote>\n\n"
                "Check your VPS credentials and try again.",
                err=esc(str(result)[:200]),
            ),
            reply_markup=kb(
                [b("🔄 Try Again", "u:getnonroot", style=S)],
                back_row(),
            ),
        )


def _random_pass(length=12):
    import random, string
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=length))


def create_nonroot_user(host, port, root_user, root_pass, new_user, new_pass):
    """SSH into host VPS and create a non-root user. Returns (success, message)."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, port=port, username=root_user, password=root_pass, timeout=15)

        # Create user and set password
        commands = [
            f"useradd -m -s /bin/bash {new_user} 2>/dev/null || echo 'user exists'",
            f"echo '{new_user}:{new_pass}' | chpasswd",
            f"usermod -aG sudo {new_user} 2>/dev/null || true",
        ]
        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd)
            stdout.channel.recv_exit_status()  # wait

        client.close()
        return True, "User created successfully"
    except ImportError:
        return False, "paramiko not installed. Run: pip install paramiko"
    except Exception as e:
        return False, str(e)


# --------------------------------------------------------- Redeem Code (User)
@user.callback_query(F.data == "u:redeem")
async def cb_redeem(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.redeem_use)
    await show(
        c,
        t(
            "🎟 <b>REDEEM CODE</b>\n"
            f"{LINE}\n"
            "Enter your redeem code below to claim points.\n\n"
            "<blockquote>Codes are case-insensitive.</blockquote>"
        ),
        kb([b("❌ Cancel", "u:menu", style=D)]),
    )


@user.message(St.redeem_use, NOT_CMD)
async def redeem_code_msg(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    uid = m.from_user.id
    pts_awarded, err = db.use_redeem_code(code, uid)
    await state.clear()
    if err:
        await m.answer(
            t("{err}\n\nTry again or contact the owner.", err=err),
            reply_markup=kb(
                [b("🎟 Try Again", "u:redeem", style=S)],
                back_row(),
            ),
        )
    else:
        pts_now = db.get_user_points(uid)
        await m.answer(
            t(
                "✅ <b>CODE REDEEMED!</b>\n"
                f"{LINE}\n"
                "<blockquote>🎟 Code : <code>{code}</code>\n"
                "⭐ Points Awarded : +{pts}\n"
                "⭐ Total Points : {total}</blockquote>",
                code=esc(code),
                pts=pts_awarded,
                total=pts_now,
            ),
            reply_markup=kb(
                [b("⭐ My Points", "u:points", style=S)],
                back_row(),
            ),
        )


# --------------------------------------------------------- Profile
def _profile_payload(uid, name):
    row = db.get_user(uid)
    if row and row["referrer_id"]:
        ref_user = db.get_user(row["referrer_id"])
        source = t("🔗 Referral of {who}", who=mention(row["referrer_id"], ref_user["name"] if ref_user else "User"))
    else:
        source = t("🚪 Direct")
    st = db.claim_status(uid)
    pts = db.get_user_points(uid)
    link = ref_link(uid)
    text = t(
        "👤 <b>MY PROFILE</b>\n"
        f"{LINE}\n"
        "<blockquote>📛 Name : {name}\n"
        "🆔 ID : <code>{uid}</code>\n"
        "📅 Joined : {joined}\n"
        "📥 Came From : {source}</blockquote>\n\n"
        '🔗 <b>Your Referral Link</b>\n<a href="{link}">{link}</a>\n\n'
        "<blockquote>👥 Users Joined : {n}\n"
        "⭐ Points : {pts}\n"
        "🖥 VPS Won : {won}</blockquote>",
        name=esc(name),
        uid=str(uid),
        joined=sb(fmt_date(row["joined_at"])) if row else "—",
        source=source,
        link=link,
        n=st["count"],
        pts=pts,
        won=st["claimed"],
    )
    return text, kb(back_row())


@user.callback_query(F.data == "u:prof")
async def cb_profile(c: CallbackQuery):
    await c.answer()
    await show(c, *_profile_payload(c.from_user.id, full_name(c.from_user)))


# --------------------------------------------------------- My Referrals
@user.callback_query(F.data.startswith("u:myref:"))
async def cb_my_referrals(c: CallbackQuery):
    await c.answer()
    uid = c.from_user.id
    page = max(0, int(c.data.split(":")[2]))
    total = db.get_user(uid)["referral_count"] if db.get_user(uid) else 0
    rows = db.referrals_page(uid, PAGE_SIZE, page * PAGE_SIZE)
    if not rows and page:
        page, rows = 0, db.referrals_page(uid, PAGE_SIZE, 0)
    if rows:
        lines = []
        for i, r in enumerate(rows, page * PAGE_SIZE + 1):
            lines.append(
                t(
                    "{i}. {who}\n    🆔 <code>{uid}</code> • 📅 {date}",
                    i=i,
                    who=mention(r["user_id"], r["name"]),
                    uid=str(r["user_id"]),
                    date=sb(fmt_dt(r["created_at"])),
                )
            )
        body = "\n\n".join(lines)
    else:
        body = t("Nobody has joined through your link yet.")
    text = t("👥 <b>MY REFERRALS</b>  ({n})\n" f"{LINE}\n", n=total) + body
    nav = []
    if page > 0:
        nav.append(b("◀️ Prev", f"u:myref:{page - 1}", style=P))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(b("Next ▶️", f"u:myref:{page + 1}", style=P))
    rows_kb = ([nav] if nav else []) + [back_row()]
    await show(c, text, kb(*rows_kb))


async def _send_myref(m: Message, uid, page):
    total = db.get_user(uid)["referral_count"] if db.get_user(uid) else 0
    rows = db.referrals_page(uid, PAGE_SIZE, 0)
    if rows:
        lines = []
        for i, r in enumerate(rows, 1):
            lines.append(
                t(
                    "{i}. {who} — {dt}",
                    i=i,
                    who=mention(r["user_id"], r["name"] or "User"),
                    dt=sb(fmt_date(r["created_at"])),
                )
            )
        body = "\n".join(lines)
    else:
        body = t("No referrals yet. Share your link!")
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = t(
        "👥 <b>MY REFERRALS</b>\n"
        f"{LINE}\n"
        "<blockquote>Total : {n}</blockquote>\n\n"
        "{body}\n\n"
        "Page {p}/{pages}",
        n=total,
        body=body,
        p=page + 1,
        pages=pages,
    )
    nav = []
    if pages > 1:
        nav.append(b("Next ▶️", f"u:myref:1", style=P))
    rows_kb = [nav] if nav else []
    rows_kb.append(back_row())
    await m.answer(text, reply_markup=kb(*rows_kb))


# --------------------------------------------------------- Buy VPS
def _buy_payload():
    text = t(
        "🛒 <b>BUY VPS</b>\n"
        f"{LINE}\n"
        "Want a VPS right now without referrals?\n\n"
        "<blockquote>Contact the owner to buy a premium VPS.</blockquote>"
    )
    return text, kb([b("📩 Contact Owner", url=SUPPORT_URL, style=S)], back_row())


@user.callback_query(F.data == "u:buy")
async def cb_buy(c: CallbackQuery):
    await c.answer()
    await show(c, *_buy_payload())


# ================================================================ ADMIN
@admin.message(F.text.func(lambda txt: _is_btn(txt, BTN_ADMIN)))
async def msg_admin_panel(m: Message, state: FSMContext):
    await state.clear()
    text, markup = panel_view(m.from_user.id)
    await m.answer(text, reply_markup=markup)


def panel_view(uid=None):
    s = db.stats()
    text = t(
        "🛡 <b>ADMIN PANEL</b>\n"
        f"{LINE}\n"
        "Manage VPS stock, force join, rewards and broadcasts.\n\n"
        "<blockquote>🖥 VPS In Stock : {stock}\n👥 Total Users : {users}</blockquote>",
        stock=s["stock"],
        users=s["users"],
    )
    rows = [
        [b("➕ Add VPS", "a:addvps", style=S), b("📦 Stock", "a:stock", style=P)],
        [b("📢 Force Join", "a:fj", style=P), b("🎯 Referral MGT", "a:ref", style=P)],
        [b("📊 Stats", "a:stats", style=P), b("📣 Broadcast", "a:bc", style=S)],
        [b("🖼 Profile", "a:pfp", style=P), b("🎟 Redeem Codes", "a:redeem", style=S)],
    ]
    # ADM management — owner only
    if uid is not None and is_owner(uid):
        rows.append([b("👑 ADM", "a:adm", style=D)])
    rows.append([b("⬅️ Back", "u:menu", style=D)])
    return text, kb(*rows)


@admin.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    text, markup = panel_view(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@admin.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    text, markup = panel_view(m.from_user.id)
    await m.answer(t("❌ Cancelled.") + "\n\n" + text, reply_markup=markup)


@admin.callback_query(F.data == "a:panel")
async def a_panel(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, *panel_view(c.from_user.id))


# ---- ADM management (owner only)
def adm_menu_view():
    extra = sorted(_extra_admins())
    env_list = sorted(_ENV_ADMINS - ({OWNER_ID} if OWNER_ID else set()))
    lines = [f"👑 Owner : <code>{OWNER_ID}</code>"]
    if env_list:
        lines.append("📌 Env admins : " + ", ".join(f"<code>{i}</code>" for i in env_list))
    if extra:
        lines.append("➕ Extra admins : " + ", ".join(f"<code>{i}</code>" for i in extra))
    else:
        lines.append("➕ Extra admins : —")
    text = (
        f"👑 <b>ADM MANAGEMENT</b>\n{LINE}\n"
        f"Only the <b>owner</b> can add / remove admins.\n\n"
        f"<blockquote>{chr(10).join(lines)}</blockquote>"
    )
    markup = kb(
        [b("➕ Add", "a:adm:add", style=S), b("➖ Remove", "a:adm:rm", style=D)],
        [b("📋 List", "a:adm:list", style=P)],
        [b("⬅️ Back", "a:panel", style=P)],
    )
    return text, markup


def _owner_only(c: CallbackQuery) -> bool:
    if is_owner(c.from_user.id):
        return True
    return False


@admin.callback_query(F.data == "a:adm")
async def a_adm(c: CallbackQuery, state: FSMContext):
    if not _owner_only(c):
        await c.answer(sb("Owner only"), show_alert=True)
        return
    await c.answer()
    await state.clear()
    await show(c, *adm_menu_view())


@admin.callback_query(F.data == "a:adm:add")
async def a_adm_add(c: CallbackQuery, state: FSMContext):
    if not _owner_only(c):
        await c.answer(sb("Owner only"), show_alert=True)
        return
    await c.answer()
    await state.set_state(St.adm_add)
    await show(
        c,
        t(
            "➕ <b>ADD ADMIN</b>\n"
            f"{LINE}\n"
            "Send the Telegram <b>user ID</b> (numbers only).\n\n"
            "<blockquote>User must have started the bot at least once.</blockquote>"
        ),
        kb([b("❌ Cancel", "a:adm", style=D)]),
    )


@admin.message(St.adm_add, NOT_CMD)
async def a_adm_add_msg(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        await state.clear()
        return
    cancel = kb([b("❌ Cancel", "a:adm", style=D)])
    raw = (m.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await m.answer(t("❌ Send a numeric Telegram user ID."), reply_markup=cancel)
        return
    uid = int(raw)
    if uid == OWNER_ID:
        await m.answer(t("❌ That ID is already the owner."), reply_markup=cancel)
        return
    if uid in all_admin_ids():
        await m.answer(t("❌ Already an admin."), reply_markup=cancel)
        return
    extra = _extra_admins()
    extra.add(uid)
    db.set_setting("extra_admins", json.dumps(sorted(extra)))
    await state.clear()
    text, markup = adm_menu_view()
    await m.answer(t("✅ Admin added : <code>{uid}</code>", uid=str(uid)) + "\n\n" + text, reply_markup=markup)


@admin.callback_query(F.data == "a:adm:list")
async def a_adm_list(c: CallbackQuery):
    if not _owner_only(c):
        await c.answer(sb("Owner only"), show_alert=True)
        return
    await c.answer()
    ids = sorted(all_admin_ids())
    body = "\n".join(
        t(
            "{icon} <code>{uid}</code> {tag}",
            icon="👑" if uid == OWNER_ID else "🛡",
            uid=str(uid),
            tag=sb("Owner" if uid == OWNER_ID else ("Env" if uid in _ENV_ADMINS else "Extra")),
        )
        for uid in ids
    )
    await show(
        c,
        t("📋 <b>ADMIN LIST</b>\n" f"{LINE}\n") + body,
        kb(
            [b("➕ Add", "a:adm:add", style=S), b("➖ Remove", "a:adm:rm", style=D)],
            [b("⬅️ Back", "a:adm", style=P)],
        ),
    )


@admin.callback_query(F.data == "a:adm:rm")
async def a_adm_rm(c: CallbackQuery):
    if not _owner_only(c):
        await c.answer(sb("Owner only"), show_alert=True)
        return
    await c.answer()
    extra = sorted(_extra_admins())
    if not extra:
        await show(
            c,
            t("No extra admins to remove.\n(Env / owner cannot be removed here.)"),
            kb([b("⬅️ Back", "a:adm", style=P)]),
        )
        return
    rows = [[b(f"❌ {uid}", f"a:adm:del:{uid}", style=D)] for uid in extra]
    rows.append([b("⬅️ Back", "a:adm", style=P)])
    await show(
        c,
        t("➖ <b>REMOVE ADMIN</b>\n" f"{LINE}\nTap an ID to remove."),
        kb(*rows),
    )


@admin.callback_query(F.data.startswith("a:adm:del:"))
async def a_adm_del(c: CallbackQuery):
    if not _owner_only(c):
        await c.answer(sb("Owner only"), show_alert=True)
        return
    try:
        uid = int(c.data.split(":")[3])
    except (IndexError, ValueError):
        await c.answer(sb("Invalid"), show_alert=True)
        return
    if uid == OWNER_ID or uid in _ENV_ADMINS:
        await c.answer(sb("Cannot remove owner/env admin"), show_alert=True)
        return
    extra = _extra_admins()
    extra.discard(uid)
    db.set_setting("extra_admins", json.dumps(sorted(extra)))
    await c.answer(sb("Removed ✅"))
    await a_adm_rm(c)


# ---- Stock (list + delete unassigned VPS)
def stock_view():
    s = db.stats()
    rows = db.stock_vps(25)
    text = t(
        "📦 <b>VPS STOCK</b>\n"
        f"{LINE}\n"
        "<blockquote>📦 In Stock : {stock}\n"
        "🎁 Given Away : {given}\n"
        "🖥 Total Entries : {total}\n"
        "🏆 Unique Winners : {winners}</blockquote>\n\n"
        "Tap ❌ to permanently delete a stock VPS (cannot be given after delete).",
        stock=s["stock"],
        given=s["given"],
        total=s["total_vps"],
        winners=s["winners"],
    )
    markup_rows = []
    if rows:
        for v in rows:
            label = f"❌ #{v['id']} {str(v['host'])[:16]}:{v['port']}"
            markup_rows.append([b(label, f"a:stock:del:{v['id']}", style=D)])
    else:
        text += "\n\n" + t("<blockquote>No VPS in stock right now.</blockquote>")
    markup_rows.append(
        [b("🔄 Refresh", "a:stock", style=P), b("➕ Add VPS", "a:addvps", style=S)]
    )
    markup_rows.append([b("⬅️ Back", "a:panel", style=P)])
    return text, kb(*markup_rows)


@admin.callback_query(F.data == "a:stock")
async def a_stock(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, *stock_view())


@admin.callback_query(F.data.startswith("a:stock:del:"))
async def a_stock_del(c: CallbackQuery):
    try:
        vid = int(c.data.split(":")[3])
    except (IndexError, ValueError):
        await c.answer(sb("Invalid"), show_alert=True)
        return
    row = db.get_vps(vid)
    if not row:
        await c.answer(sb("Already removed"), show_alert=True)
        await show(c, *stock_view())
        return
    if row["assigned_to"] is not None:
        await c.answer(sb("Already given — cannot delete"), show_alert=True)
        await show(c, *stock_view())
        return
    ok = db.delete_vps(vid)
    if ok:
        await c.answer(sb(f"Deleted #{vid} ✅"))
    else:
        await c.answer(sb("Delete failed"), show_alert=True)
    await show(c, *stock_view())


# ---- Add VPS
@admin.callback_query(F.data == "a:addvps")
async def a_addvps(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.add_vps)
    example = "<pre>IP : your.host.com\nPORT : 22\nUSER : root\nPASS : yourpassword\nDAYS : 30</pre>"
    text = t(
        "➕ <b>ADD VPS</b>\n"
        f"{LINE}\n"
        "Send the VPS details in this format:\n\n"
        "{ex}\n"
        "<blockquote>• DAYS is optional (default validity is used)\n"
        "• Send many VPS in one message — leave a blank line between them\n"
        "• These VPS will be given for <b>referral tiers</b></blockquote>",
        ex=example,
    )
    await show(c, text, kb([b("❌ Cancel", "a:panel", style=D)]))


@admin.message(St.add_vps, NOT_CMD)
async def a_addvps_msg(m: Message, state: FSMContext):
    items, bad = parse_vps(m.text or "")
    if not items:
        await m.answer(
            t("❌ Could not read any VPS. Make sure IP, PORT, USER and PASS are present, then send again."),
            reply_markup=kb([b("❌ Cancel", "a:panel", style=D)]),
        )
        return
    db.add_vps(items, vps_type="referral")
    await state.clear()
    try:  # remove the message that contains credentials
        await m.delete()
    except TelegramBadRequest:
        pass
    text = t(
        "✅ <b>VPS SAVED</b>\n"
        f"{LINE}\n"
        "<blockquote>➕ Added : {a}\n⚠️ Invalid blocks : {bad}\n🖥 Stock now : {s}</blockquote>",
        a=len(items),
        bad=bad,
        s=db.stats()["stock"],
    )
    await m.answer(
        text,
        reply_markup=kb(
            [b("➕ Add More", "a:addvps", style=S), b("🛡 Panel", "a:panel", style=P)]
        ),
    )


# ---- Force join
def fj_menu_view():
    n = len(db.list_fj())
    text = t(
        "📢 <b>FORCE JOIN</b>\n"
        f"{LINE}\n"
        "Public / private channels and groups are supported.\n\n"
        "<blockquote>📌 Active chats : {n}\n"
        "Private chats use join-request links — a request counts as joined.\n"
        "Bot must be <b>admin</b> with invite permission.</blockquote>",
        n=n,
    )
    markup = kb(
        [b("➕ Add", "a:fj:add", style=S), b("➖ Remove", "a:fj:rm", style=D)],
        [b("📋 List", "a:fj:list", style=P), b("🔄 Refresh", "a:fj", style=P)],
        [b("⬅️ Back", "a:panel", style=P)],
    )
    return text, markup


@admin.callback_query(F.data == "a:fj")
async def a_fj(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, *fj_menu_view())


@admin.callback_query(F.data == "a:fj:add")
async def a_fj_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.fj_add)
    text = t(
        "➕ <b>ADD FORCE JOIN</b>\n"
        f"{LINE}\n"
        "First make the bot an <b>admin</b> in the channel / group, then send ONE of:\n\n"
        "<blockquote>• @username or t.me/username (public)\n"
        "• Chat ID like <code>-1001234567890</code> (private)\n"
        "• Forward any post from the channel / group\n"
        "• Optional: own invite link after the ID</blockquote>"
    )
    await show(c, text, kb([b("❌ Cancel", "a:fj", style=D)]))


def parse_chat_token(tok):
    tok = (tok or "").strip()
    if not tok:
        return None, None
    if re.fullmatch(r"-?\d{5,}", tok):
        return "chat", int(tok)
    if re.match(r"^(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)", tok, re.I):
        return "link", tok if tok.startswith("http") else "https://" + tok.lstrip("/")
    m = re.match(r"^(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_]{4,})/?$", tok, re.I)
    if m:
        return "chat", "@" + m.group(1)
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", tok):
        return "chat", "@" + tok.lstrip("@")
    return None, None


def _chat_kind(chat) -> str:
    ctype = getattr(chat, "type", None)
    if ctype is None:
        return "unknown"
    return ctype.value if hasattr(ctype, "value") else str(ctype)


def _is_bot_admin(member) -> bool:
    status = member.status
    status_val = status.value if hasattr(status, "value") else str(status)
    return status_val in (
        "administrator",
        "creator",
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    )


@admin.message(St.fj_add, NOT_CMD)
async def a_fj_add_msg(m: Message, state: FSMContext):
    chat_ref, link = None, None
    # Forwarded message (aiogram 3: forward_origin)
    origin = getattr(m, "forward_origin", None)
    if origin is not None:
        ochat = getattr(origin, "chat", None)
        if ochat is not None:
            chat_ref = ochat.id
    # Legacy forward_from_chat
    if chat_ref is None and getattr(m, "forward_from_chat", None) is not None:
        chat_ref = m.forward_from_chat.id
    for tok in (m.text or m.caption or "").split():
        kind, val = parse_chat_token(tok)
        if kind == "chat" and chat_ref is None:
            chat_ref = val
        elif kind == "link":
            link = val
    cancel = kb([b("❌ Cancel", "a:fj", style=D)])
    if chat_ref is None:
        await m.answer(
            t("❌ Could not understand. Send @username, chat ID, or forward a post."),
            reply_markup=cancel,
        )
        return
    try:
        chat = await m.bot.get_chat(chat_ref)
        me = await m.bot.get_chat_member(chat.id, m.bot.id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await m.answer(
            t(
                "❌ Chat not found or bot is not a member.\n"
                "<blockquote>{e}</blockquote>\n"
                "Add the bot to the chat first, make it admin, then try again.",
                e=esc(e),
            ),
            reply_markup=cancel,
        )
        return
    if not _is_bot_admin(me):
        await m.answer(
            t("❌ Make the bot an <b>admin</b> in that chat first, then send again."),
            reply_markup=cancel,
        )
        return
    is_private = not bool(chat.username)
    if chat.username:
        link = f"https://t.me/{chat.username}"
    elif not link:
        try:
            inv = await m.bot.create_chat_invite_link(
                chat.id,
                name="ReferBot FJ",
                creates_join_request=True,
            )
            link = inv.invite_link
        except (TelegramBadRequest, TelegramForbiddenError):
            try:
                inv = await m.bot.create_chat_invite_link(chat.id, name="ReferBot FJ")
                link = inv.invite_link
            except (TelegramBadRequest, TelegramForbiddenError):
                await m.answer(
                    t(
                        "❌ Could not create invite link.\n"
                        "Give bot <b>Invite Users</b> right, or send:\n"
                        "<code>-100… https://t.me/+xxxxx</code>"
                    ),
                    reply_markup=cancel,
                )
                return
    kind = _chat_kind(chat)
    db.add_fj(
        chat.id,
        chat.title or str(chat.id),
        kind,
        chat.username,
        link,
        is_private,
    )
    _clear_fj_cache()
    await state.clear()
    text = t(
        "✅ <b>FORCE JOIN ADDED</b>\n"
        f"{LINE}\n"
        "<blockquote>📛 {title}\n"
        "🆔 <code>{cid}</code>\n"
        "🔐 Type : {typ}\n"
        "🔗 Link : {link}</blockquote>",
        title=esc(chat.title or chat.id),
        cid=str(chat.id),
        typ=sb("Private (join request)" if is_private else "Public"),
        link=esc(link),
    )
    await m.answer(
        text,
        reply_markup=kb(
            [b("➕ Add More", "a:fj:add", style=S), b("📋 List", "a:fj:list", style=P)],
            [b("📢 Force Join", "a:fj", style=P)],
        ),
    )


@admin.callback_query(F.data == "a:fj:list")
async def a_fj_list(c: CallbackQuery):
    await c.answer()
    chats = db.list_fj()
    if chats:
        body = "\n\n".join(
            t(
                "{i}. <b>{title}</b>\n"
                "    🆔 <code>{cid}</code>\n"
                "    🔐 {typ} • {kind}\n"
                "    🔗 {link}",
                i=i,
                title=esc(ch["title"] or ""),
                cid=str(ch["chat_id"]),
                typ=sb("Private" if ch["is_private"] else "Public"),
                kind=sb(ch["kind"] or "—"),
                link=esc((ch["link"] or "—")[:40]),
            )
            for i, ch in enumerate(chats, 1)
        )
    else:
        body = t("No force-join chats added yet.\nTap ➕ Add to add one.")
    await show(
        c,
        t("📋 <b>FORCE JOIN LIST</b>\n" f"{LINE}\n") + body,
        kb(
            [b("➕ Add", "a:fj:add", style=S), b("➖ Remove", "a:fj:rm", style=D)],
            [b("⬅️ Back", "a:fj", style=P)],
        ),
    )


@admin.callback_query(F.data == "a:fj:rm")
async def a_fj_rm(c: CallbackQuery):
    await c.answer()
    chats = db.list_fj()
    if not chats:
        await show(c, t("No force-join chats to remove."), kb([b("⬅️ Back", "a:fj", style=P)]))
        return
    rows = [
        [b(f"❌ {(ch['title'] or str(ch['chat_id']))[:28]}", f"a:fj:del:{ch['chat_id']}", style=D)]
        for ch in chats
    ]
    rows.append([b("⬅️ Back", "a:fj", style=P)])
    await show(
        c,
        t("➖ <b>REMOVE FORCE JOIN</b>\n" f"{LINE}\nTap a chat to remove it."),
        kb(*rows),
    )


@admin.callback_query(F.data.startswith("a:fj:del:"))
async def a_fj_del(c: CallbackQuery):
    try:
        cid = int(c.data.split(":")[3])
    except (IndexError, ValueError):
        await c.answer(sb("Invalid"), show_alert=True)
        return
    db.remove_fj(cid)
    _clear_fj_cache()
    await c.answer(sb("Removed ✅"))
    await a_fj_rm(c)


# join-request tracking (private channels / groups)
@user.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    if not db.get_fj(req.chat.id):
        return
    uid = req.from_user.id
    db.add_join_request(req.chat.id, uid)
    _clear_fj_cache(uid)
    # Auto-approve so user becomes a real member when possible
    try:
        await req.approve()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    # Notify user in DM
    dm_id = getattr(req, "user_chat_id", None) or uid
    try:
        await req.bot.send_message(
            dm_id,
            t(
                "✅ <b>JOIN REQUEST RECEIVED</b>\n"
                f"{LINE}\n"
                "Your request for <b>{title}</b> was recorded.\n"
                "Tap <b>Verify</b> to unlock the bot.",
                title=esc(req.chat.title or ""),
            ),
            reply_markup=kb([b("✅ Verify", "u:verify", style=S)]),
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


# ---- Referral management
def ref_menu_view():
    tiers = db.get_tiers()
    medals = ["🥇", "🥈", "🥉"]
    lines = "\n".join(
        t("{icon} {ordn} VPS › {n} referrals", icon=medals[i] if i < 3 else "🏅", ordn=ordinal(i), n=v)
        for i, v in enumerate(tiers)
    )
    text = t(
        "🎯 <b>REFERRAL MANAGEMENT</b>\n"
        f"{LINE}\n"
        "{lines}\n\n"
        "<blockquote>⏳ VPS validity : {days} days\n"
        "♾ Referrals are cumulative — they never reset.</blockquote>",
        lines=lines,
        days=db.get_vps_days(),
    )
    rows, row = [], []
    for i in range(len(tiers)):
        row.append(b(f"✏️ {ordinal(i)} VPS", f"a:ref:edit:{i}", style=S))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([b("➕ Add Tier", "a:ref:add", style=S), b("➖ Remove Last", "a:ref:del", style=D)])
    rows.append([b("🗓 VPS Validity", "a:ref:days", style=P)])
    rows.append([b("⬅️ Back", "a:panel", style=P)])
    return text, kb(*rows)


@admin.callback_query(F.data == "a:ref")
async def a_ref(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, *ref_menu_view())


@admin.callback_query(F.data.startswith("a:ref:edit:"))
async def a_ref_edit(c: CallbackQuery, state: FSMContext):
    await c.answer()
    idx = int(c.data.split(":")[3])
    tiers = db.get_tiers()
    if idx >= len(tiers):
        return
    await state.set_state(St.tier_edit)
    await state.update_data(idx=idx)
    text = t(
        "✏️ <b>EDIT {ordn} VPS</b>\n{line}\nCurrent target : {n} referrals\n\nSend the new number of referrals required.",
        ordn=ordinal(idx),
        line=LINE,
        n=tiers[idx],
    )
    await show(c, text, kb([b("❌ Cancel", "a:ref", style=D)]))


@admin.message(St.tier_edit, NOT_CMD)
async def a_ref_edit_msg(m: Message, state: FSMContext):
    data = await state.get_data()
    idx = data["idx"]
    if not (m.text or "").strip().isdigit():
        await m.answer(t("❌ Send a number."), reply_markup=kb([b("❌ Cancel", "a:ref", style=D)]))
        return
    tiers = db.get_tiers()
    tiers[idx] = int(m.text.strip())
    try:
        db.set_tiers(tiers)
    except ValueError as e:
        await m.answer(t("❌ {e}", e=esc(e)), reply_markup=kb([b("❌ Cancel", "a:ref", style=D)]))
        return
    await state.clear()
    text, markup = ref_menu_view()
    await m.answer(text, reply_markup=markup)


@admin.callback_query(F.data == "a:ref:add")
async def a_ref_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    tiers = db.get_tiers()
    await state.set_state(St.tier_add)
    text = t(
        "➕ <b>ADD TIER</b>\n{line}\nSend the referral target for the <b>{ordn} VPS</b>.\nIt must be higher than {last}.",
        line=LINE,
        ordn=ordinal(len(tiers)),
        last=tiers[-1],
    )
    await show(c, text, kb([b("❌ Cancel", "a:ref", style=D)]))


@admin.message(St.tier_add, NOT_CMD)
async def a_ref_add_msg(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer(t("❌ Send a number."), reply_markup=kb([b("❌ Cancel", "a:ref", style=D)]))
        return
    try:
        db.set_tiers(db.get_tiers() + [int(m.text.strip())])
    except ValueError as e:
        await m.answer(t("❌ {e}", e=esc(e)), reply_markup=kb([b("❌ Cancel", "a:ref", style=D)]))
        return
    await state.clear()
    text, markup = ref_menu_view()
    await m.answer(text, reply_markup=markup)


@admin.callback_query(F.data == "a:ref:del")
async def a_ref_del(c: CallbackQuery):
    tiers = db.get_tiers()
    if len(tiers) <= 1:
        await c.answer(sb("At least one tier is required"), show_alert=True)
        return
    db.set_tiers(tiers[:-1])
    await c.answer(sb("Removed ✅"))
    await show(c, *ref_menu_view())


@admin.callback_query(F.data == "a:ref:days")
async def a_ref_days(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.days)
    text = t(
        "🗓 <b>VPS VALIDITY</b>\n{line}\nCurrent : {d} days\n\nSend the new default validity in days.",
        line=LINE,
        d=db.get_vps_days(),
    )
    await show(c, text, kb([b("❌ Cancel", "a:ref", style=D)]))


@admin.message(St.days, NOT_CMD)
async def a_ref_days_msg(m: Message, state: FSMContext):
    try:
        db.set_vps_days(int((m.text or "").strip()))
    except ValueError:
        await m.answer(t("❌ Send a number between 1 and 3650."), reply_markup=kb([b("❌ Cancel", "a:ref", style=D)]))
        return
    await state.clear()
    text, markup = ref_menu_view()
    await m.answer(text, reply_markup=markup)


# ---- Stats
@admin.callback_query(F.data == "a:stats")
async def a_stats(c: CallbackQuery):
    await c.answer()
    s = db.stats()
    text = t(
        "📊 <b>STATISTICS</b>\n"
        f"{LINE}\n"
        "<blockquote>👥 Total Users : {users}\n"
        "🆕 Joined Today : {today}\n"
        "🚫 Blocked Bot : {blocked}\n"
        "🔗 Total Referrals : {refs}</blockquote>\n"
        "<blockquote>🖥 VPS In Stock : {stock}\n"
        "📦 Total VPS Added : {total}\n"
        "🎁 VPS Given Away : {given}\n"
        "🏆 Winners : {winners}\n"
        "⌛ Expired VPS : {expired}</blockquote>",
        users=s["users"], today=s["today"], blocked=s["blocked"], refs=s["referrals"],
        stock=s["stock"], total=s["total_vps"], given=s["given"], winners=s["winners"],
        expired=s["expired"],
    )
    await show(c, text, kb([b("🔄 Refresh", "a:stats", style=S), b("⬅️ Back", "a:panel", style=P)]))


# ---- Broadcast (with button / without button)
@admin.callback_query(F.data == "a:bc")
async def a_bc(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.bc)
    text = t(
        "📣 <b>BROADCAST</b>\n"
        f"{LINE}\n"
        "Send the message you want to broadcast (text, photo, video, anything).\n\n"
        "<blockquote>After sending the message, you will choose:\n"
        "• <b>Without Button</b> — forward hide sender\n"
        "• <b>With Button</b> — same msg + custom inline button</blockquote>"
    )
    await show(c, text, kb([b("❌ Cancel", "a:panel", style=D)]))


@admin.message(St.bc, NOT_CMD)
async def a_bc_msg(m: Message, state: FSMContext):
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id)
    total = len(db.all_user_ids())
    await m.bot.copy_message(m.chat.id, m.chat.id, m.message_id)  # preview
    await m.answer(
        t("👆 <b>Preview above.</b>\nSend to <b>{n}</b> users.\n\nChoose broadcast type:", n=total),
        reply_markup=kb(
            [
                b("📨 Without Button", "a:bc:nowbtn", style=S),
                b("🔘 With Button", "a:bc:withbtn", style=P),
            ],
            [b("❌ Cancel", "a:panel", style=D)],
        ),
    )


# Without button — simple forward hide sender
@admin.callback_query(F.data == "a:bc:nowbtn")
async def a_bc_nowbtn(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if "src_msg" not in data:
        await c.answer(sb("Nothing to send"), show_alert=True)
        return
    await c.answer(sb("Broadcast started 🚀"))
    status = await c.message.edit_text(t("📣 Broadcasting… please wait."))
    task = asyncio.create_task(run_broadcast(c.bot, status, data["src_chat"], data["src_msg"], buttons=None))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# With button — ask for button name + URL
@admin.callback_query(F.data == "a:bc:withbtn")
async def a_bc_withbtn(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.bc_buttons)
    await show(
        c,
        t(
            "🔘 <b>ADD BUTTON</b>\n"
            f"{LINE}\n"
            "Send button details in this format:\n\n"
            "<pre>Button Name | https://example.com</pre>\n\n"
            "<blockquote>One button per line (max 3 buttons)\n"
            "Example:\n"
            "Join Channel | https://t.me/yourchannel\n"
            "Visit Website | https://yoursite.com</blockquote>"
        ),
        kb([b("❌ Cancel", "a:panel", style=D)]),
    )


@admin.message(St.bc_buttons, NOT_CMD)
async def a_bc_buttons_msg(m: Message, state: FSMContext):
    lines = [l.strip() for l in (m.text or "").strip().splitlines() if l.strip()]
    buttons = []
    errors = []
    for line in lines[:3]:
        if "|" not in line:
            errors.append(f"❌ Missing | in: {line[:30]}")
            continue
        parts = line.split("|", 1)
        btn_name = parts[0].strip()
        btn_url = parts[1].strip()
        if not btn_name or not btn_url.startswith("http"):
            errors.append(f"❌ Invalid: {line[:30]}")
            continue
        buttons.append({"name": btn_name, "url": btn_url})

    if not buttons:
        await m.answer(
            t("❌ No valid buttons found.\n{err}\n\nFormat: Button Name | URL", err="\n".join(errors)),
            reply_markup=kb([b("❌ Cancel", "a:panel", style=D)]),
        )
        return

    data = await state.get_data()
    await state.clear()
    if "src_msg" not in data:
        await m.answer(t("❌ Message not found. Start broadcast again."))
        return

    total = len(db.all_user_ids())
    # Preview
    btn_markup = kb(*[[b(btn["name"], url=btn["url"], style=P)] for btn in buttons])
    await m.bot.copy_message(m.chat.id, data["src_chat"], data["src_msg"], reply_markup=btn_markup)
    btn_list = "\n".join(f"  • {btn['name']} → {btn['url']}" for btn in buttons)
    await m.answer(
        t("👆 <b>Preview above (with buttons).</b>\nSend to <b>{n}</b> users?", n=total),
        reply_markup=kb(
            [b("✅ Send Now", "a:bc:gobtn", style=S), b("❌ Cancel", "a:panel", style=D)]
        ),
    )
    # Store buttons in FSM temp (abuse state data for this)
    # We use a separate FSM state key trick: store in DB setting temporarily
    db.set_setting("_bc_buttons_tmp", json.dumps({
        "src_chat": data["src_chat"],
        "src_msg": data["src_msg"],
        "buttons": buttons,
    }))


@admin.callback_query(F.data == "a:bc:gobtn")
async def a_bc_gobtn(c: CallbackQuery, state: FSMContext):
    raw = db.get_setting("_bc_buttons_tmp")
    db.del_setting("_bc_buttons_tmp")
    if not raw:
        await c.answer(sb("Nothing to send"), show_alert=True)
        return
    data = json.loads(raw)
    buttons = data.get("buttons", [])
    btn_markup = kb(*[[b(btn["name"], url=btn["url"], style=P)] for btn in buttons])
    await c.answer(sb("Broadcast started 🚀"))
    status = await c.message.edit_text(t("📣 Broadcasting… please wait."))
    task = asyncio.create_task(run_broadcast(c.bot, status, data["src_chat"], data["src_msg"], buttons=btn_markup))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


_bg_tasks = set()


async def run_broadcast(bot, status, src_chat, src_msg, buttons=None):
    ids = db.all_user_ids()
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        for attempt in range(2):
            try:
                await bot.copy_message(uid, src_chat, src_msg, reply_markup=buttons)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                if attempt == 1:
                    fail += 1
            except TelegramForbiddenError:
                db.mark_blocked(uid)
                fail += 1
                break
            except Exception:  # noqa: BLE001
                fail += 1
                break
        await asyncio.sleep(0.05)
        if i % 100 == 0:
            try:
                await status.edit_text(t("📣 Broadcasting… {i}/{n}", i=i, n=len(ids)))
            except TelegramBadRequest:
                pass
    text = t(
        "✅ <b>BROADCAST FINISHED</b>\n"
        f"{LINE}\n"
        "<blockquote>📨 Delivered : {ok}\n❌ Failed : {fail}\n👥 Total : {n}</blockquote>",
        ok=ok, fail=fail, n=len(ids),
    )
    try:
        await status.edit_text(text, reply_markup=kb([b("🛡 Panel", "a:panel", style=P)]))
    except TelegramBadRequest:
        await bot.send_message(status.chat.id, text)


# ---- Welcome profile picture
def pfp_view():
    has = bool(db.get_setting("pfp"))
    text = t(
        "🖼 <b>WELCOME PICTURE</b>\n"
        f"{LINE}\n"
        "This photo is shown on /start with the welcome message and user info as caption.\n\n"
        "<blockquote>Status : {s}</blockquote>",
        s=sb("✅ Set" if has else "❌ Not set"),
    )
    markup = kb(
        [b("➕ Add", "a:pfp:add", style=S), b("➖ Remove", "a:pfp:rm", style=D)],
        [b("⬅️ Back", "a:panel", style=P)],
    )
    return text, markup


@admin.callback_query(F.data == "a:pfp")
async def a_pfp(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, *pfp_view())


@admin.callback_query(F.data == "a:pfp:add")
async def a_pfp_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.pfp)
    await show(c, t("🖼 Send the <b>photo</b> you want as the welcome picture."), kb([b("❌ Cancel", "a:pfp", style=D)]))


@admin.message(St.pfp, F.photo)
async def a_pfp_photo(m: Message, state: FSMContext):
    db.set_setting("pfp", m.photo[-1].file_id)
    await state.clear()
    text, markup = pfp_view()
    await m.answer(t("✅ Welcome picture saved!") + "\n\n" + text, reply_markup=markup)


@admin.message(St.pfp, NOT_CMD)
async def a_pfp_wrong(m: Message):
    await m.answer(t("❌ Please send a photo."), reply_markup=kb([b("❌ Cancel", "a:pfp", style=D)]))


@admin.callback_query(F.data == "a:pfp:rm")
async def a_pfp_rm(c: CallbackQuery):
    db.del_setting("pfp")
    await c.answer(sb("Removed ✅"))
    await show(c, *pfp_view())


# ---- Redeem Code Management (Admin)
def redeem_menu_view():
    codes = db.list_redeem_codes()
    if codes:
        lines = []
        for rc in codes[:10]:
            lines.append(
                f"🎟 <code>{esc(rc['code'])}</code> — {rc['points']} pts — "
                f"{rc['claims_used']}/{rc['max_claims']} used"
            )
        body = "\n".join(lines)
    else:
        body = t("No redeem codes yet.")
    text = (
        f"🎟 <b>REDEEM CODES</b>\n{LINE}\n"
        f"<blockquote>{body}</blockquote>"
    )
    markup = kb(
        [b("➕ Create Code", "a:redeem:create", style=S), b("❌ Delete Code", "a:redeem:del", style=D)],
        [b("⬅️ Back", "a:panel", style=P)],
    )
    return text, markup


@admin.callback_query(F.data == "a:redeem")
async def a_redeem(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, *redeem_menu_view())


@admin.callback_query(F.data == "a:redeem:create")
async def a_redeem_create(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(St.redeem_create_code)
    await show(
        c,
        t(
            "➕ <b>CREATE REDEEM CODE</b>\n"
            f"{LINE}\n"
            "Send the <b>code</b> you want to create.\n\n"
            "<blockquote>Or send <code>auto</code> to generate a random code.</blockquote>"
        ),
        kb([b("❌ Cancel", "a:redeem", style=D)]),
    )


@admin.message(St.redeem_create_code, NOT_CMD)
async def a_redeem_code_msg(m: Message, state: FSMContext):
    code_raw = (m.text or "").strip().upper()
    if not code_raw:
        await m.answer(t("❌ Send a code or 'auto'."), reply_markup=kb([b("❌ Cancel", "a:redeem", style=D)]))
        return
    code = None if code_raw == "AUTO" else code_raw
    await state.update_data(rc_code=code)
    await state.set_state(St.redeem_create_pts)
    await m.answer(
        t("✅ Code set.\n\nNow send the number of <b>points</b> this code will give:"),
        reply_markup=kb([b("❌ Cancel", "a:redeem", style=D)]),
    )


@admin.message(St.redeem_create_pts, NOT_CMD)
async def a_redeem_pts_msg(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await m.answer(t("❌ Send a positive number."), reply_markup=kb([b("❌ Cancel", "a:redeem", style=D)]))
        return
    await state.update_data(rc_pts=int(raw))
    await state.set_state(St.redeem_create_claims)
    await m.answer(
        t("✅ Points set: <b>{pts}</b>\n\nNow send <b>max claims</b> (how many users can use this code):", pts=raw),
        reply_markup=kb([b("❌ Cancel", "a:redeem", style=D)]),
    )


@admin.message(St.redeem_create_claims, NOT_CMD)
async def a_redeem_claims_msg(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await m.answer(t("❌ Send a positive number."), reply_markup=kb([b("❌ Cancel", "a:redeem", style=D)]))
        return
    data = await state.get_data()
    await state.clear()
    code, err = db.create_redeem_code(
        points=data["rc_pts"],
        max_claims=int(raw),
        created_by=m.from_user.id,
        code=data.get("rc_code"),
    )
    if err:
        await m.answer(t("❌ {err}", err=err), reply_markup=kb([b("⬅️ Back", "a:redeem", style=P)]))
        return
    text, markup = redeem_menu_view()
    await m.answer(
        t(
            "✅ <b>REDEEM CODE CREATED</b>\n"
            f"{LINE}\n"
            "<blockquote>🎟 Code : <code>{code}</code>\n"
            "⭐ Points : {pts}\n"
            "👥 Max Claims : {claims}</blockquote>\n\n"
            + text,
            code=esc(code),
            pts=data["rc_pts"],
            claims=raw,
        ),
        reply_markup=markup,
    )


@admin.callback_query(F.data == "a:redeem:del")
async def a_redeem_del(c: CallbackQuery):
    await c.answer()
    codes = db.list_redeem_codes()
    if not codes:
        await show(c, t("No codes to delete."), kb([b("⬅️ Back", "a:redeem", style=P)]))
        return
    rows = [[b(f"❌ {rc['code']} ({rc['claims_used']}/{rc['max_claims']})", f"a:rdel:{rc['code']}", style=D)] for rc in codes[:10]]
    rows.append([b("⬅️ Back", "a:redeem", style=P)])
    await show(c, t("❌ <b>DELETE CODE</b>\n{line}\nTap to delete:", line=LINE), kb(*rows))


@admin.callback_query(F.data.startswith("a:rdel:"))
async def a_rdel(c: CallbackQuery):
    code = c.data[7:]
    db.delete_redeem_code(code)
    await c.answer(sb(f"Deleted {code} ✅"))
    await show(c, *redeem_menu_view())


# =========================================================== background
async def expiry_watcher(bot):
    while True:
        try:
            now = int(time.time())
            for v in db.vps_to_warn(now):
                try:
                    await bot.send_message(
                        v["assigned_to"],
                        t(
                            "⏰ <b>VPS EXPIRING SOON</b>\n"
                            f"{LINE}\n"
                            "<blockquote>🌐 Host : <code>{host}</code>\n⏳ Expires : {exp}\n🕒 Remaining : {rem}</blockquote>\n"
                            "Renew from the owner before it is gone.",
                            host=esc(v["host"]),
                            exp=sb(fmt_dt(v["expires_at"])),
                            rem=sb(remaining(v["expires_at"], now)),
                        ),
                        reply_markup=kb([b("🛒 Buy VPS", "u:buy", style=D)]),
                    )
                except (TelegramForbiddenError, TelegramBadRequest):
                    pass
                db.mark_warned(v["id"])
        except Exception:  # noqa: BLE001
            log.exception("expiry watcher error")
        await asyncio.sleep(1800)


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN is missing. Put it in .env (see .env.example).")
    if not OWNER_ID and not _ENV_ADMINS:
        sys.exit("ADMIN_IDS is missing. Put your Telegram user id in .env.")
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    bot.session.middleware(QuoteMW())
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher()
    dp.include_router(admin)
    dp.include_router(user)
    await bot.delete_webhook(drop_pending_updates=False)
    watcher = asyncio.create_task(expiry_watcher(bot))
    _bg_tasks.add(watcher)
    log.info("Bot @%s started", BOT_USERNAME)
    # Explicit updates: messages, callbacks, and chat_join_request for force-join
    allowed = dp.resolve_used_update_types()
    if "chat_join_request" not in allowed:
        allowed = list(allowed) + ["chat_join_request"]
    await dp.start_polling(bot, allowed_updates=allowed)


if __name__ == "__main__":
    asyncio.run(main())
