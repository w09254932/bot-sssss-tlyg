# -*- coding: utf-8 -*-
# بوت الفعالية (السحب/الجوائز) — ملف مستقل يُدمج مع main.py
# المرحلة 1: التأسيس + لوحة إعدادات كاملة للمالك داخل بوت الفعالية
#   - تسجيل القناة والقروب، الجوائز + الأكواد، مؤقّت الجولة، الفاصل،
#     استبعاد الفائز، وقت الجدولة، نصوص رسائل القناة/القروب
# كل بوت فعالية له توكن خاص، ويُشغّل على مسار /ev/{token}
import asyncio
import html as _html
import os
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

OWNER_ID = 1652300694

# ---- حقن من main.py عبر setup_event ----
_main_bot = None       # البوت الرئيسي (للتنبيهات)
_db = None             # Firestore client
_ADMIN_ID = 0
_WEBHOOK_HOST = ""
_logger = None

EVENT_BOTS = {}        # token -> Bot
EV_INFO = {}           # token -> {"id":.., "username":..}

event_dp = Dispatcher()

# افتراضيات الإعدادات
_DEF_TIMER = 60        # ثانية لكل جولة
_DEF_GAP = 10          # ثانية بين الجوائز


def _is_owner(uid) -> bool:
    return uid == OWNER_ID or (_ADMIN_ID and uid == _ADMIN_ID)


def _now():
    return datetime.now(timezone.utc)


# ================= التشفير (نفس مفتاح المراقبة MON_KEY) =================
_fernet = None


def _init_crypto():
    global _fernet
    key = os.environ.get("MON_KEY")
    if not key:
        _fernet = None
        return
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        if _logger:
            _logger.exception("event crypto init failed: %s", e)
        _fernet = None


def _enc(token):
    if _fernet is None or not token:
        return token
    try:
        return "enc:" + _fernet.encrypt(token.encode()).decode()
    except Exception:
        return token


def _dec(stored):
    if not stored:
        return stored
    if isinstance(stored, str) and stored.startswith("enc:"):
        if _fernet is None:
            return None
        try:
            return _fernet.decrypt(stored[4:].encode()).decode()
        except Exception:
            return None
    return stored


# ================= تخزين الإعدادات =================
def _default_cfg(bot_id):
    return {
        "bot_id": bot_id,
        "channel_id": None,
        "group_id": None,
        "prizes": [],            # [{"name":.., "code":..}]
        "round_timer": _DEF_TIMER,
        "gap": _DEF_GAP,
        "exclude_winners": True,
        "schedule_at": None,     # ISO string (توقيت الرياض)
        "msg_channel": None,
        "msg_group": None,
    }


def _load_cfg(bot_id):
    if _db is None:
        return _default_cfg(bot_id)
    try:
        snap = _db.collection("event_cfg").document(str(bot_id)).get()
        if snap.exists:
            d = snap.to_dict() or {}
            cfg = _default_cfg(bot_id)
            cfg.update(d)
            cfg["bot_id"] = bot_id
            cfg["prizes"] = list(d.get("prizes") or [])
            return cfg
    except Exception as e:
        if _logger:
            _logger.exception("event load_cfg failed: %s", e)
    return _default_cfg(bot_id)


def _save_cfg(bot_id, cfg):
    if _db is None:
        return
    try:
        data = dict(cfg)
        data["bot_id"] = bot_id
        data["updated_at"] = _now()
        _db.collection("event_cfg").document(str(bot_id)).set(data)
    except Exception as e:
        if _logger:
            _logger.exception("event save_cfg failed: %s", e)


# ================= واجهة الإعدادات =================
class EvFSM(StatesGroup):
    channel = State()
    group = State()
    prize_name = State()
    prize_code = State()
    timer = State()
    gap = State()
    schedule = State()
    msg_channel = State()
    msg_group = State()


def _fmt_id(v):
    return str(v) if v not in (None, "") else "—"


def _status_text(cfg) -> str:
    prizes = cfg.get("prizes") or []
    plines = []
    for i, pz in enumerate(prizes, 1):
        code = "✅" if pz.get("code") else "⚠️ بدون كود"
        plines.append(f"  {i}. {_html.escape(pz.get('name') or 'بدون اسم')} — {code}")
    prizes_block = "\n".join(plines) if plines else "  —"
    return (
        "🎉 <b>إعدادات بوت الفعالية</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📢 القناة: <code>{_fmt_id(cfg.get('channel_id'))}</code>\n"
        f"👥 القروب: <code>{_fmt_id(cfg.get('group_id'))}</code>\n"
        f"⏱ مؤقّت الجولة: {cfg.get('round_timer', _DEF_TIMER)} ثانية\n"
        f"⏳ الفاصل بين الجوائز: {cfg.get('gap', _DEF_GAP)} ثانية\n"
        f"🔁 استبعاد الفائز: {'نعم' if cfg.get('exclude_winners') else 'لا'}\n"
        f"🕐 وقت الجدولة: {cfg.get('schedule_at') or '—'}\n"
        f"✉️ رسالة القناة: {'مضبوطة' if cfg.get('msg_channel') else '—'}\n"
        f"✉️ رسالة القروب: {'مضبوطة' if cfg.get('msg_group') else '—'}\n"
        "━━━━━━━━━━━━━━\n"
        f"🎁 الجوائز ({len(prizes)}):\n{prizes_block}"
    )


def _panel_kb(cfg):
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 تعيين القناة", callback_data="evset:chan")
    kb.button(text="👥 تعيين القروب", callback_data="evset:grp")
    kb.button(text="🎁 إضافة جائزة", callback_data="evset:prize_add")
    kb.button(text="🗑 مسح الجوائز", callback_data="evset:prize_clear")
    kb.button(text="⏱ مؤقّت الجولة", callback_data="evset:timer")
    kb.button(text="⏳ الفاصل", callback_data="evset:gap")
    excl = "🔁 استبعاد الفائز: نعم" if cfg.get("exclude_winners") else "🔁 استبعاد الفائز: لا"
    kb.button(text=excl, callback_data="evset:excl")
    kb.button(text="🕐 وقت الجدولة", callback_data="evset:sched")
    kb.button(text="✉️ رسالة القناة", callback_data="evset:mchan")
    kb.button(text="✉️ رسالة القروب", callback_data="evset:mgrp")
    kb.button(text="▶️ ابدأ الآن", callback_data="evset:startnow")
    kb.button(text="🔄 تحديث", callback_data="evset:refresh")
    kb.adjust(2, 2, 2, 1, 2, 1, 1)
    return kb.as_markup()


async def _show_panel(message, cfg):
    await message.answer(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))


# ---- فتح البوت بالخاص ----
@event_dp.message(CommandStart())
async def _ev_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = message.from_user
    if u is None:
        return
    if not _is_owner(u.id):
        await message.answer("🎉 بوت فعالية. تابع القناة/القروب للمشاركة.")
        return
    await _show_panel(message, _load_cfg(bot.id))


# ---- أزرار الإعدادات ----
def _bid(cb):
    return cb.bot.id


@event_dp.callback_query(F.data == "evset:refresh")
async def _cb_refresh(cb: CallbackQuery, bot: Bot):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await cb.answer("تم التحديث")
    cfg = _load_cfg(bot.id)
    try:
        await cb.message.edit_text(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))
    except Exception:
        await cb.message.answer(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))


@event_dp.callback_query(F.data == "evset:excl")
async def _cb_excl(cb: CallbackQuery, bot: Bot):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    cfg = _load_cfg(bot.id)
    cfg["exclude_winners"] = not cfg.get("exclude_winners")
    _save_cfg(bot.id, cfg)
    await cb.answer("تم التبديل")
    try:
        await cb.message.edit_text(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))
    except Exception:
        await cb.message.answer(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))


@event_dp.callback_query(F.data == "evset:prize_clear")
async def _cb_prize_clear(cb: CallbackQuery, bot: Bot):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    cfg = _load_cfg(bot.id)
    cfg["prizes"] = []
    _save_cfg(bot.id, cfg)
    await cb.answer("تم مسح الجوائز")
    try:
        await cb.message.edit_text(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))
    except Exception:
        await cb.message.answer(_status_text(cfg), parse_mode="HTML", reply_markup=_panel_kb(cfg))


@event_dp.callback_query(F.data == "evset:startnow")
async def _cb_startnow(cb: CallbackQuery, bot: Bot):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await cb.answer()
    await cb.message.answer("▶️ تشغيل الفعالية — قيد الإنشاء (المرحلة 2).")


# أزرار تطلب إدخال نصّي → نحفظ أي بوت (bot.id) في الحالة
_PROMPTS = {
    "evset:chan": (EvFSM.channel, "📢 أرسل آيدي القناة (مثال: -1001234567890)\nأضِف البوت مشرفًا في القناة أولًا.\n/cancel للإلغاء"),
    "evset:grp": (EvFSM.group, "👥 أرسل آيدي القروب (مثال: -1001234567890)\nأضِف البوت مشرفًا في القروب أولًا.\n/cancel للإلغاء"),
    "evset:prize_add": (EvFSM.prize_name, "🎁 أرسل اسم الجائزة:\n/cancel للإلغاء"),
    "evset:timer": (EvFSM.timer, "⏱ أرسل مدة الجولة بالثواني (مثال: 60):\n/cancel للإلغاء"),
    "evset:gap": (EvFSM.gap, "⏳ أرسل الفاصل بين الجوائز بالثواني (مثال: 10):\n/cancel للإلغاء"),
    "evset:sched": (EvFSM.schedule, "🕐 أرسل وقت الجدولة بصيغة:\nYYYY-MM-DD HH:MM (توقيت الرياض)\nمثال: 2026-10-15 21:30\nأرسل - لإلغاء الجدولة.\n/cancel للإلغاء"),
    "evset:mchan": (EvFSM.msg_channel, "✉️ أرسل نص رسالة القناة (يُنشر عند بدء الفعالية):\n/cancel للإلغاء"),
    "evset:mgrp": (EvFSM.msg_group, "✉️ أرسل نص رسالة القروب (تمهيد الفعالية):\n/cancel للإلغاء"),
}


@event_dp.callback_query(F.data.startswith("evset:"))
async def _cb_prompt(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    entry = _PROMPTS.get(cb.data)
    if entry is None:
        await cb.answer(); return
    st, text = entry
    await cb.answer()
    await state.set_state(st)
    await state.update_data(bot_id=bot.id)
    await cb.message.answer(text)


# ---- /cancel ----
@event_dp.message(F.text == "/cancel")
async def _ev_cancel(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    await _show_panel(message, _load_cfg(bot.id))


def _parse_id(txt):
    txt = (txt or "").strip()
    try:
        return int(txt)
    except Exception:
        return None


# ---- مُدخلات الإعدادات (حالات FSM) ----
@event_dp.message(StateFilter(EvFSM.channel))
async def _st_channel(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    cid = _parse_id(message.text)
    await state.clear()
    if cid is None:
        await message.answer("❌ آيدي غير صحيح. أرسل رقمًا مثل -1001234567890.")
        return
    cfg = _load_cfg(bot.id)
    cfg["channel_id"] = cid
    _save_cfg(bot.id, cfg)
    await message.answer("✅ تم تعيين القناة.")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.group))
async def _st_group(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    gid = _parse_id(message.text)
    await state.clear()
    if gid is None:
        await message.answer("❌ آيدي غير صحيح. أرسل رقمًا مثل -1001234567890.")
        return
    cfg = _load_cfg(bot.id)
    cfg["group_id"] = gid
    _save_cfg(bot.id, cfg)
    await message.answer("✅ تم تعيين القروب.")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.prize_name))
async def _st_prize_name(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ أرسل اسمًا صحيحًا.")
        return
    await state.update_data(prize_name=name, bot_id=bot.id)
    await state.set_state(EvFSM.prize_code)
    await message.answer("🔑 أرسل كود هذه الجائزة (يُعطى للفائز):\n/cancel للإلغاء")


@event_dp.message(StateFilter(EvFSM.prize_code))
async def _st_prize_code(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    code = (message.text or "").strip()
    data = await state.get_data()
    name = data.get("prize_name") or "جائزة"
    await state.clear()
    if not code:
        await message.answer("❌ أرسل كودًا صحيحًا.")
        return
    cfg = _load_cfg(bot.id)
    prizes = list(cfg.get("prizes") or [])
    prizes.append({"name": name, "code": code})
    cfg["prizes"] = prizes
    _save_cfg(bot.id, cfg)
    await message.answer(f"✅ أُضيفت الجائزة: {name}")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.timer))
async def _st_timer(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    n = _parse_id(message.text)
    if n is None or n < 5 or n > 3600:
        await message.answer("❌ أرسل رقمًا بين 5 و 3600 ثانية.")
        return
    cfg = _load_cfg(bot.id)
    cfg["round_timer"] = n
    _save_cfg(bot.id, cfg)
    await message.answer(f"✅ مؤقّت الجولة: {n} ثانية.")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.gap))
async def _st_gap(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    n = _parse_id(message.text)
    if n is None or n < 0 or n > 3600:
        await message.answer("❌ أرسل رقمًا بين 0 و 3600 ثانية.")
        return
    cfg = _load_cfg(bot.id)
    cfg["gap"] = n
    _save_cfg(bot.id, cfg)
    await message.answer(f"✅ الفاصل: {n} ثانية.")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.schedule))
async def _st_schedule(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    txt = (message.text or "").strip()
    cfg = _load_cfg(bot.id)
    if txt == "-":
        cfg["schedule_at"] = None
        _save_cfg(bot.id, cfg)
        await message.answer("✅ أُلغيت الجدولة.")
        await _show_panel(message, cfg)
        return
    try:
        dt = datetime.strptime(txt, "%Y-%m-%d %H:%M")
    except Exception:
        await message.answer("❌ صيغة غير صحيحة. مثال: 2026-10-15 21:30")
        return
    cfg["schedule_at"] = dt.strftime("%Y-%m-%d %H:%M")
    _save_cfg(bot.id, cfg)
    await message.answer(f"✅ وقت الجدولة: {cfg['schedule_at']} (الرياض)")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.msg_channel))
async def _st_mchan(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    cfg = _load_cfg(bot.id)
    cfg["msg_channel"] = (message.text or "").strip() or None
    _save_cfg(bot.id, cfg)
    await message.answer("✅ تم حفظ رسالة القناة.")
    await _show_panel(message, cfg)


@event_dp.message(StateFilter(EvFSM.msg_group))
async def _st_mgrp(message: Message, bot: Bot, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    cfg = _load_cfg(bot.id)
    cfg["msg_group"] = (message.text or "").strip() or None
    _save_cfg(bot.id, cfg)
    await message.answer("✅ تم حفظ رسالة القروب.")
    await _show_panel(message, cfg)


# ---- أي رسالة خاصة أخرى من المالك → اعرض اللوحة ----
@event_dp.message(F.chat.type == "private")
async def _ev_private(message: Message, bot: Bot, state: FSMContext):
    u = message.from_user
    if u is None:
        return
    if not _is_owner(u.id):
        await message.answer("🎉 بوت فعالية. تابع القناة/القروب للمشاركة.")
        return
    cur = await state.get_state()
    if cur is not None:
        return  # في منتصف إدخال
    await _show_panel(message, _load_cfg(bot.id))


# ================= تفعيل بوتات الفعالية =================
async def _activate_event_bot(token, username=None):
    if token in EVENT_BOTS:
        return EVENT_BOTS[token]
    b = Bot(token)
    EVENT_BOTS[token] = b
    EV_INFO[token] = {"id": b.id, "username": username}
    try:
        await b.set_webhook(
            f"{_WEBHOOK_HOST}/ev/{token}",
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )
        if _logger:
            _logger.info("event webhook set for @%s", username)
    except Exception as e:
        if _logger:
            _logger.exception("event set_webhook failed: %s", e)
    return b


async def add_event_bot(token, me):
    """يُستدعى من monitor._got_token عند اختيار دور (فعالية)."""
    if _db is not None:
        try:
            _db.collection("event_bots").document(str(me.id)).set({
                "token": _enc(token),
                "username": me.username,
                "bot_id": me.id,
                "added_at": _now(),
            })
        except Exception as e:
            if _logger:
                _logger.exception("event store failed: %s", e)
    await _activate_event_bot(token, me.username)
    return True


# ================= الويبهوك =================
async def _safe_feed(b, update):
    try:
        await event_dp.feed_update(bot=b, update=update)
    except Exception as e:
        if _logger:
            _logger.exception("event feed failed: %s", e)


async def _ev_webhook(request):
    token = request.match_info.get("token", "")
    b = EVENT_BOTS.get(token)
    if b is None:
        return web.Response(status=404, text="not found")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad request")
    try:
        update = Update.model_validate(data, context={"bot": b})
        asyncio.create_task(_safe_feed(b, update))
    except Exception as e:
        if _logger:
            _logger.exception("event webhook parse failed: %s", e)
    return web.Response(text="ok")


async def _ev_startup_bg():
    if _db is None:
        return
    try:
        docs = list(_db.collection("event_bots").limit(200).stream())
    except Exception as e:
        if _logger:
            _logger.exception("event load bots failed: %s", e)
        return
    for d in docs:
        rec = d.to_dict()
        token = _dec(rec.get("token"))
        if token:
            await _activate_event_bot(token, rec.get("username"))


async def _ev_startup():
    asyncio.create_task(_ev_startup_bg())


def setup_event(app, dp, main_bot, db, admin_id, webhook_host, logger):
    global _main_bot, _db, _ADMIN_ID, _WEBHOOK_HOST, _logger
    _main_bot = main_bot
    _db = db
    _ADMIN_ID = admin_id
    _WEBHOOK_HOST = (webhook_host or "").rstrip("/")
    _logger = logger
    _init_crypto()
    app.router.add_post("/ev/{token}", _ev_webhook)
    dp.startup.register(_ev_startup)
    logger.info("event module ready")
