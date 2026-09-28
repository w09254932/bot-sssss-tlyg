# -*- coding: utf-8 -*-
# مراقبة تغيير اليوزر/الاسم داخل القروبات — ملف مستقل يُدمج مع main.py
# كل بوت مراقبة له توكن خاص، ويُشغّل على مسار /mon/{token}
import asyncio
import html as _html
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ---- حقن من main.py عبر setup_monitor ----
_bot = None            # البوت الرئيسي
_dp = None             # ديسباتشر البوت الرئيسي
_db = None             # Firestore client
_ADMIN_ID = 0
_WEBHOOK_HOST = ""
_logger = None
_admin_menu_markup = None   # دالة لوحة المالك من main.py

# ---- حالة التشغيل ----
MONITOR_BOTS = {}      # token -> Bot
BOT_INFO = {}          # token -> {"id": int, "username": str}
_watch_cache = {}      # (bot_id, chat_id, user_id) -> (username, first, last)

monitor_dp = Dispatcher()   # ديسباتشر واحد يخدم كل بوتات المراقبة


class MonAdminFSM(StatesGroup):
    token = State()


# ================= أدوات =================
def _mention(u) -> str:
    name = _html.escape(u.full_name or u.first_name or "صديقي")
    return f'<a href="tg://user?id={u.id}">{name}</a>'


def _uname(v) -> str:
    return f"@{v}" if v else "بدون يوزر"


def _full_name(first, last) -> str:
    first = first or ""
    last = last or ""
    return (first + (" " + last if last else "")).strip()


def _build_announcements(u, prev, cur):
    prev_username, prev_first, prev_last = prev
    cur_username, cur_first, cur_last = cur
    out = []
    m = _mention(u)
    if prev_username != cur_username:
        out.append(
            f"👀 يا {m}، ليش غيّرت اليوزر؟\n"
            f"من: {_uname(prev_username)}\n"
            f"إلى: {_uname(cur_username)}"
        )
    prev_name = _full_name(prev_first, prev_last)
    cur_name = _full_name(cur_first, cur_last)
    if prev_name != cur_name:
        out.append(
            f"👀 يا {m}، ليش غيّرت اسمك؟\n"
            f"من: {_html.escape(prev_name) or 'بدون اسم'}\n"
            f"إلى: {_html.escape(cur_name) or 'بدون اسم'}"
        )
    return out


# ================= تخزين الأعضاء =================
def _watch_doc_id(bot_id, chat_id, user_id) -> str:
    return f"{bot_id}_{chat_id}_{user_id}"


def _load_watch(bot_id, chat_id, user_id):
    if _db is None:
        return None
    try:
        snap = _db.collection("watched").document(_watch_doc_id(bot_id, chat_id, user_id)).get()
        if snap.exists:
            d = snap.to_dict()
            return (d.get("username"), d.get("first_name") or "", d.get("last_name") or "")
    except Exception as e:
        _logger.exception("load_watch failed: %s", e)
    return None


def _save_watch(bot_id, chat_id, user_id, cur):
    if _db is None:
        return
    try:
        _db.collection("watched").document(_watch_doc_id(bot_id, chat_id, user_id)).set({
            "bot_id": bot_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "username": cur[0],
            "first_name": cur[1],
            "last_name": cur[2],
            "updated_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        _logger.exception("save_watch failed: %s", e)


# ================= معالج المراقبة (على monitor_dp) =================
@monitor_dp.message(F.chat.type.in_({"group", "supergroup"}))
async def watch_handler(message: Message, bot: Bot):
    u = message.from_user
    if u is None or u.is_bot:
        return
    bot_id = bot.id
    chat_id = message.chat.id
    key = (bot_id, chat_id, u.id)
    cur = (u.username, u.first_name or "", u.last_name or "")

    prev = _watch_cache.get(key)
    if prev is None:
        prev = _load_watch(bot_id, chat_id, u.id)
        if prev is None:
            # أول ظهور — نسجّل بصمت بدون رد
            _watch_cache[key] = cur
            _save_watch(bot_id, chat_id, u.id, cur)
            return
        _watch_cache[key] = prev

    if prev == cur:
        return

    announcements = _build_announcements(u, prev, cur)
    _watch_cache[key] = cur
    _save_watch(bot_id, chat_id, u.id, cur)
    for text in announcements:
        try:
            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            _logger.exception("watch announce failed: %s", e)


# ================= ويبهوك بوتات المراقبة =================
async def _safe_feed(b: Bot, update: Update):
    try:
        await monitor_dp.feed_update(bot=b, update=update)
    except Exception as e:
        _logger.exception("monitor feed failed: %s", e)


async def _mon_webhook(request: web.Request):
    token = request.match_info.get("token", "")
    b = MONITOR_BOTS.get(token)
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
        _logger.exception("monitor webhook parse failed: %s", e)
    return web.Response(text="ok")


# ================= تفعيل/تحميل بوتات المراقبة =================
async def _activate_bot(token: str, username=None):
    if token in MONITOR_BOTS:
        return MONITOR_BOTS[token]
    b = Bot(token)
    MONITOR_BOTS[token] = b
    BOT_INFO[token] = {"id": b.id, "username": username}
    try:
        await b.set_webhook(
            f"{_WEBHOOK_HOST}/mon/{token}",
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
        _logger.info("monitor webhook set for @%s", username)
    except Exception as e:
        _logger.exception("monitor set_webhook failed: %s", e)
    return b


async def _mon_startup_bg():
    if _db is None:
        return
    try:
        docs = list(_db.collection("monitor_bots").limit(500).stream())
    except Exception as e:
        _logger.exception("monitor load failed: %s", e)
        return
    for d in docs:
        rec = d.to_dict()
        token = rec.get("token")
        if token:
            await _activate_bot(token, rec.get("username"))


async def _mon_startup():
    asyncio.create_task(_mon_startup_bg())


# ================= لوحة المالك: أزرار المراقبة =================
def _mon_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ إضافة بوت مراقبة", callback_data="mon:add")
    if MONITOR_BOTS:
        kb.button(text="🗑 حذف بوت", callback_data="mon:dellist")
    kb.button(text="🔙 رجوع", callback_data="mon:back")
    kb.adjust(1)
    return kb.as_markup()


async def _cb_mon_open(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != _ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await state.clear()
    if not MONITOR_BOTS:
        text = (
            "🔎 مراقبة تغيير اليوزرات والأسماء\n\n"
            "لا يوجد بوتات مراقبة مضافة حالياً.\n"
            "أضف بوت مراقبة ليبدأ العمل في قروبك."
        )
    else:
        lines = ["🔎 بوتات المراقبة المضافة:", ""]
        for i, token in enumerate(MONITOR_BOTS.keys(), 1):
            un = (BOT_INFO.get(token, {}) or {}).get("username") or "?"
            lines.append(f"{i}. @{un} ✅ يعمل")
        text = "\n".join(lines)
    await callback.message.answer(text, reply_markup=_mon_menu_kb())


async def _cb_mon_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != _ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(MonAdminFSM.token)
    await callback.message.answer(
        "📥 أرسل الآن توكن البوت الذي تريد استخدامه للمراقبة.\n\n"
        "مثال على شكل التوكن:\n"
        "1234567890:AAExampleTokenHere...\n\n"
        "⚠️ استخدم توكن بوت خاص بك (من @BotFather).\n"
        "/cancel للإلغاء"
    )


async def _got_token(message: Message, state: FSMContext):
    token = (message.text or "").strip()
    await state.clear()
    if ":" not in token or len(token) < 20:
        await message.answer("❌ التوكن غير صالح. تأكد من نسخه كامل وحاول مرة ثانية.")
        return
    tmp = Bot(token)
    try:
        me = await tmp.get_me()
    except Exception:
        await message.answer("❌ التوكن غير صالح. تأكد من نسخه كامل وحاول مرة ثانية.")
        try:
            await tmp.session.close()
        except Exception:
            pass
        return
    if _db is not None:
        try:
            _db.collection("monitor_bots").document(str(me.id)).set({
                "token": token,
                "username": me.username,
                "bot_id": me.id,
                "added_at": datetime.now(timezone.utc),
            })
        except Exception as e:
            _logger.exception("monitor store failed: %s", e)
    MONITOR_BOTS[token] = tmp
    BOT_INFO[token] = {"id": me.id, "username": me.username}
    try:
        await tmp.set_webhook(
            f"{_WEBHOOK_HOST}/mon/{token}",
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
    except Exception as e:
        _logger.exception("monitor set_webhook failed: %s", e)
    await message.answer(
        f"✅ تم ربط البوت بنجاح: @{me.username}\n\n"
        "📌 باقي خطوتين لتفعيل المراقبة:\n"
        "1) من @BotFather أوقف Privacy Mode لهذا البوت\n"
        "   (Bot Settings ← Group Privacy ← Turn OFF)\n"
        f"2) أضف @{me.username} في قروبك واجعله مشرف.\n\n"
        "بعدها يبدأ يراقب تلقائياً 👍",
        reply_markup=_mon_menu_kb(),
    )


async def _cb_mon_dellist(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != _ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    if not MONITOR_BOTS:
        await callback.message.answer("لا يوجد بوتات مراقبة.", reply_markup=_mon_menu_kb())
        return
    kb = InlineKeyboardBuilder()
    for token in MONITOR_BOTS.keys():
        info = BOT_INFO.get(token, {}) or {}
        un = info.get("username") or "?"
        bid = info.get("id")
        kb.button(text=f"@{un}", callback_data=f"mon:del:{bid}")
    kb.button(text="🔙 رجوع", callback_data="admin:mon")
    kb.adjust(1)
    await callback.message.answer("اختر البوت الذي تريد حذفه:", reply_markup=kb.as_markup())


async def _cb_mon_del(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != _ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    bid = callback.data.split(":")[2]
    target = None
    for token, info in BOT_INFO.items():
        if str((info or {}).get("id")) == bid:
            target = token
            break
    if target is None:
        await callback.message.answer("البوت غير موجود.", reply_markup=_mon_menu_kb())
        return
    b = MONITOR_BOTS.pop(target, None)
    info = BOT_INFO.pop(target, None)
    un = (info or {}).get("username") or "?"
    try:
        if b:
            await b.delete_webhook()
    except Exception:
        pass
    try:
        if b:
            await b.session.close()
    except Exception:
        pass
    if _db is not None:
        try:
            _db.collection("monitor_bots").document(bid).delete()
        except Exception:
            pass
    await callback.message.answer(f"✅ تم حذف @{un} وإيقاف مراقبته.", reply_markup=_mon_menu_kb())


async def _cb_mon_back(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != _ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await state.clear()
    markup = _admin_menu_markup() if _admin_menu_markup else None
    await callback.message.answer("👑 لوحة المالك", reply_markup=markup)


def _register_admin_handlers(dp: Dispatcher):
    dp.callback_query.register(_cb_mon_open, F.data == "admin:mon")
    dp.callback_query.register(_cb_mon_add, F.data == "mon:add")
    dp.callback_query.register(_cb_mon_dellist, F.data == "mon:dellist")
    dp.callback_query.register(_cb_mon_del, F.data.startswith("mon:del:"))
    dp.callback_query.register(_cb_mon_back, F.data == "mon:back")
    dp.message.register(_got_token, StateFilter(MonAdminFSM.token), F.from_user.id == _ADMIN_ID)


# ================= نقطة الدخول =================
def setup_monitor(app, dp, bot, db, admin_id, webhook_host, logger, admin_menu_markup):
    global _bot, _dp, _db, _ADMIN_ID, _WEBHOOK_HOST, _logger, _admin_menu_markup
    _bot = bot
    _dp = dp
    _db = db
    _ADMIN_ID = admin_id
    _WEBHOOK_HOST = (webhook_host or "").rstrip("/")
    _logger = logger
    _admin_menu_markup = admin_menu_markup

    _register_admin_handlers(dp)
    app.router.add_post("/mon/{token}", _mon_webhook)
    dp.startup.register(_mon_startup)
    logger.info("monitor module ready")
