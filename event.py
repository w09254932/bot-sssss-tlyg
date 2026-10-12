# -*- coding: utf-8 -*-
# بوت الفعالية (السحب/الجوائز) — ملف مستقل يُدمج مع main.py
# المرحلة 1: التأسيس + لوحة إعدادات كاملة للمالك داخل بوت الفعالية
#   - تسجيل القناة والقروب، الجوائز + الأكواد، مؤقّت الجولة، الفاصل،
#     استبعاد الفائز، وقت الجدولة، نصوص رسائل القناة/القروب
# كل بوت فعالية له توكن خاص، ويُشغّل على مسار /ev/{token}
import asyncio
import html as _html
import os
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import StateFilter, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, Message, CallbackQuery, MessageReactionUpdated, ChatMemberUpdated
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

# حالة الفعاليات الجارية (بالذاكرة) — bot_id -> state
ACTIVE = {}

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
async def _ev_start(message: Message, bot: Bot, state: FSMContext, command: CommandObject):
    await state.clear()
    u = message.from_user
    if u is None:
        return
    payload = (command.args or "").strip() if command else ""
    if payload:
        await _handle_claim(message, bot, payload)
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
    cfg = _load_cfg(bot.id)
    problems = []
    if not cfg.get("channel_id"):
        problems.append("• عيّن القناة")
    if not cfg.get("group_id"):
        problems.append("• عيّن القروب")
    prizes = [p for p in (cfg.get("prizes") or []) if p.get("code")]
    if not prizes:
        problems.append("• أضف جائزة واحدة على الأقل مع كود")
    if problems:
        await cb.message.answer("⚠️ أكمل الإعدادات أولًا:\n" + "\n".join(problems))
        return
    if bot.id in ACTIVE and ACTIVE[bot.id].get("running"):
        await cb.message.answer("⏳ فيه فعالية جارية بالفعل.")
        return
    await cb.message.answer("▶️ بدأت الفعالية.")
    asyncio.create_task(_run_event(bot, cfg))


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


# ================= محرّك السحب =================
async def _bot_username(bot):
    for t, info in EV_INFO.items():
        if info.get("id") == bot.id and info.get("username"):
            return info["username"]
    try:
        me = await bot.get_me()
        return me.username
    except Exception:
        return None


def _make_claim(bot_id, round_idx, prize, winner_id, winner_name):
    suffix = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    claim_id = f"{bot_id}_{round_idx}_{suffix}"
    if _db is not None:
        try:
            _db.collection("event_winners").document(claim_id).set({
                "bot_id": bot_id,
                "round": round_idx,
                "prize": prize.get("name"),
                "code": prize.get("code"),
                "winner_id": winner_id,
                "winner_name": winner_name,
                "claimed": False,
                "created_at": _now(),
            })
        except Exception as e:
            if _logger:
                _logger.exception("event winner store failed: %s", e)
    return claim_id


async def _run_event(bot, cfg):
    bot_id = bot.id
    group_id = cfg.get("group_id")
    channel_id = cfg.get("channel_id")
    prizes = [p for p in (cfg.get("prizes") or []) if p.get("code")]
    timer = int(cfg.get("round_timer", _DEF_TIMER))
    gap = int(cfg.get("gap", _DEF_GAP))
    exclude = bool(cfg.get("exclude_winners"))
    username = await _bot_username(bot)
    winners = set()
    ACTIVE[bot_id] = {"running": True, "group_id": group_id, "msg_id": None, "participants": {}}
    try:
        if channel_id:
            txt = cfg.get("msg_channel") or "🎉 الفعالية بدأت!"
            try:
                await bot.send_message(channel_id, txt)
            except Exception as e:
                if _logger:
                    _logger.exception("event channel post failed: %s", e)
                try:
                    await _main_bot.send_message(_ADMIN_ID, "⚠️ ما قدرت أنشر في القناة. تأكد أن البوت مشرف فيها.")
                except Exception:
                    pass
        if cfg.get("msg_group"):
            try:
                await bot.send_message(group_id, cfg["msg_group"])
            except Exception:
                pass
        for idx, prize in enumerate(prizes):
            ACTIVE[bot_id]["participants"] = {}
            pname = _html.escape(prize.get("name") or "جائزة")
            try:
                msg = await bot.send_message(
                    group_id,
                    f"🎰 <b>روليت الجائزة {idx+1}:</b> {pname}\n\n"
                    f"تفاعل على هذي الرسالة للدخول! 🎉\n"
                    f"⏱ ينتهي خلال {timer} ثانية.",
                    parse_mode="HTML",
                )
            except Exception as e:
                if _logger:
                    _logger.exception("event round post failed: %s", e)
                try:
                    await _main_bot.send_message(_ADMIN_ID, "⚠️ ما قدرت أنشر في القروب. تأكد أن البوت مشرف فيه.")
                except Exception:
                    pass
                break
            ACTIVE[bot_id]["msg_id"] = msg.message_id
            await asyncio.sleep(timer)
            parts = dict(ACTIVE[bot_id]["participants"])
            ACTIVE[bot_id]["msg_id"] = None
            pool = [(uid, nm) for uid, nm in parts.items() if not (exclude and uid in winners)]
            if not pool:
                try:
                    await bot.send_message(group_id, f"🎁 {pname}: ما فيه مشاركين مؤهلين. تخطّينا الجائزة.")
                except Exception:
                    pass
                if idx < len(prizes) - 1:
                    await asyncio.sleep(gap)
                continue
            names = [nm for _, nm in pool]
            for _ in range(min(6, max(3, len(names)))):
                try:
                    await bot.edit_message_text(
                        chat_id=group_id, message_id=msg.message_id,
                        text=f"🎰 جاري سحب: {pname}\n\n🎲 {_html.escape(random.choice(names))}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.7)
            win_id, win_name = random.choice(pool)
            winners.add(win_id)
            claim_id = _make_claim(bot_id, idx, prize, win_id, win_name)
            mention = f'<a href="tg://user?id={win_id}">{_html.escape(win_name or "الفائز")}</a>'
            kb = InlineKeyboardBuilder()
            if username:
                kb.button(text="🎁 استلم جائزتك", url=f"https://t.me/{username}?start={claim_id}")
            try:
                await bot.send_message(
                    group_id,
                    f"🎉 مبروك! الفائز بـ <b>{pname}</b>:\n{mention}\n\n"
                    f"اضغط الزر تحت لاستلام الكود بالخاص 👇",
                    parse_mode="HTML",
                    reply_markup=kb.as_markup() if username else None,
                )
            except Exception as e:
                if _logger:
                    _logger.exception("event winner post failed: %s", e)
            if idx < len(prizes) - 1:
                await asyncio.sleep(gap)
        try:
            await bot.send_message(group_id, "🏁 انتهت الفعالية. شكرًا للجميع!")
        except Exception:
            pass
    finally:
        ACTIVE.pop(bot_id, None)


@event_dp.message_reaction()
async def _on_reaction(event: MessageReactionUpdated, bot: Bot):
    st = ACTIVE.get(bot.id)
    if not st or not st.get("running"):
        return
    if event.chat.id != st.get("group_id") or event.message_id != st.get("msg_id"):
        return
    u = event.user
    if u is None or getattr(u, "is_bot", False):
        return
    if event.new_reaction:
        st["participants"][u.id] = u.first_name or u.full_name or "مشارك"
    else:
        st["participants"].pop(u.id, None)


@event_dp.my_chat_member()
async def _ev_my_member(event: ChatMemberUpdated, bot: Bot):
    # تسجيل تلقائي للقناة/المجموعة عند إضافة البوت (من المالك فقط)
    if event.from_user is None or not _is_owner(event.from_user.id):
        return
    status = event.new_chat_member.status if event.new_chat_member else None
    if status not in ("administrator", "member", "creator"):
        return
    chat = event.chat
    cfg = _load_cfg(bot.id)
    if chat.type == "channel":
        cfg["channel_id"] = chat.id
        label = "القناة"
    elif chat.type in ("group", "supergroup"):
        cfg["group_id"] = chat.id
        label = "المجموعة"
    else:
        return
    _save_cfg(bot.id, cfg)
    try:
        await bot.send_message(
            chat.id,
            f"✅ تم تفعيل بوت الفعالية في {label}.\n🆔 <code>{chat.id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    try:
        if _main_bot is not None and _ADMIN_ID:
            await _main_bot.send_message(
                _ADMIN_ID,
                f"✅ ارتبطت {label}: {chat.title or chat.id}\n🆔 {chat.id}",
            )
    except Exception:
        pass


async def _handle_claim(message, bot, payload):
    if _db is None:
        return
    try:
        snap = _db.collection("event_winners").document(payload).get()
    except Exception:
        return
    if not snap.exists:
        return
    d = snap.to_dict() or {}
    if d.get("bot_id") != bot.id:
        return
    if message.from_user.id != d.get("winner_id"):
        return  # ليس الفائز — بدون رد
    code = d.get("code")
    prize = d.get("prize") or "جائزتك"
    try:
        await message.answer(
            f"🎁 مبروك! كود {_html.escape(str(prize))}:\n<code>{_html.escape(str(code))}</code>",
            parse_mode="HTML",
        )
    except Exception:
        return
    if not d.get("claimed"):
        try:
            _db.collection("event_winners").document(payload).set({"claimed": True, "claimed_at": _now()}, merge=True)
        except Exception:
            pass


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
            allowed_updates=["message", "callback_query", "my_chat_member", "message_reaction"],
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


def _due(schedule_at):
    if not schedule_at:
        return False
    try:
        dt = datetime.strptime(schedule_at, "%Y-%m-%d %H:%M")
    except Exception:
        return False
    dt_utc = dt.replace(tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
    return _now() >= dt_utc


async def _sched_check():
    for token, b in list(EVENT_BOTS.items()):
        try:
            bot_id = b.id
        except Exception:
            continue
        if bot_id in ACTIVE and ACTIVE[bot_id].get("running"):
            continue
        cfg = _load_cfg(bot_id)
        if not _due(cfg.get("schedule_at")):
            continue
        prizes = [p for p in (cfg.get("prizes") or []) if p.get("code")]
        if not cfg.get("group_id") or not prizes:
            cfg["schedule_at"] = None
            _save_cfg(bot_id, cfg)
            continue
        cfg["schedule_at"] = None
        _save_cfg(bot_id, cfg)
        try:
            if _main_bot is not None and _ADMIN_ID:
                await _main_bot.send_message(_ADMIN_ID, "▶️ انطلقت الفعالية المجدولة.")
        except Exception:
            pass
        asyncio.create_task(_run_event(b, cfg))


async def _sched_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await _sched_check()
        except Exception:
            pass


async def _ev_startup():
    asyncio.create_task(_ev_startup_bg())
    asyncio.create_task(_sched_loop())


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
