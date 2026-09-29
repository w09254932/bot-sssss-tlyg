# -*- coding: utf-8 -*-
# مراقبة تغيير اليوزر/الاسم داخل القروبات — ملف مستقل يُدمج مع main.py
# - تاريخ آخر 5 أسماء + آخر 5 يوزرات لكل عضو
# - بحث بالخاص للمالك فقط (بالآيدي أو بيوزر قديم/حالي)
# - قائمة بيضاء: يشتغل فقط في القروبات التي يضيفه لها المالك، ويغادر غيرها تلقائياً
# كل بوت مراقبة له توكن خاص، ويُشغّل على مسار /mon/{token}
import asyncio
import html as _html
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, Message, CallbackQuery, ChatMemberUpdated
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ---- المالك (كما زوّدنا المستخدم) — يُستخدم مع ADMIN_ID المحقون ----
OWNER_ID = 1652300694

# ---- حقن من main.py عبر setup_monitor ----
_bot = None            # البوت الرئيسي (للتنبيهات)
_dp = None             # ديسباتشر البوت الرئيسي
_db = None             # Firestore client
_ADMIN_ID = 0
_WEBHOOK_HOST = ""
_logger = None
_admin_menu_markup = None   # دالة لوحة المالك من main.py

# ---- حالة التشغيل ----
MONITOR_BOTS = {}      # token -> Bot
BOT_INFO = {}          # token -> {"id": int, "username": str}
_watch_cache = {}      # (bot_id, user_id) -> rec dict
_allowed_chats = {}    # bot_id -> set(chat_id)

monitor_dp = Dispatcher()   # ديسباتشر واحد يخدم كل بوتات المراقبة


class MonAdminFSM(StatesGroup):
    token = State()


def _is_owner(uid) -> bool:
    return uid == _ADMIN_ID or uid == OWNER_ID


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


def _now():
    return datetime.now(timezone.utc)


def _push_hist(lst, value, cap=5):
    out = list(lst or [])
    out.insert(0, {"v": value, "at": _now().isoformat()})
    return out[:cap]


# ================= تخزين الأعضاء + الفهرس =================
def _load_rec(bot_id, uid):
    if _db is None:
        return None
    try:
        snap = _db.collection("watched").document(f"{bot_id}_{uid}").get()
        if snap.exists:
            d = snap.to_dict()
            return {
                "username": d.get("username"),
                "first": d.get("first_name") or "",
                "last": d.get("last_name") or "",
                "names": d.get("names") or [],
                "unames": d.get("unames") or [],
            }
    except Exception as e:
        _logger.exception("load_rec failed: %s", e)
    return None


def _save_rec(bot_id, uid, rec):
    if _db is None:
        return
    try:
        _db.collection("watched").document(f"{bot_id}_{uid}").set({
            "bot_id": bot_id,
            "user_id": uid,
            "username": rec.get("username"),
            "first_name": rec.get("first"),
            "last_name": rec.get("last"),
            "names": rec.get("names") or [],
            "unames": rec.get("unames") or [],
            "updated_at": _now(),
        })
    except Exception as e:
        _logger.exception("save_rec failed: %s", e)


def _index_username(bot_id, username, uid):
    if _db is None or not username:
        return
    try:
        _db.collection("mon_uindex").document(f"{bot_id}_{username.lower()}").set({
            "bot_id": bot_id,
            "username": username,
            "user_id": uid,
            "updated_at": _now(),
        })
    except Exception as e:
        _logger.exception("index_username failed: %s", e)


# ================= قائمة القروبات المصرّح بها =================
def _approve_chat(bot_id, chat_id, added_by, title=None):
    _allowed_chats.setdefault(bot_id, set()).add(chat_id)
    if _db is None:
        return
    try:
        _db.collection("mon_chats").document(f"{bot_id}_{chat_id}").set({
            "bot_id": bot_id,
            "chat_id": chat_id,
            "added_by": added_by,
            "title": title,
            "added_at": _now(),
        })
    except Exception as e:
        _logger.exception("approve_chat failed: %s", e)


def _revoke_chat(bot_id, chat_id):
    s = _allowed_chats.get(bot_id)
    if s and chat_id in s:
        s.discard(chat_id)
    if _db is None:
        return
    try:
        _db.collection("mon_chats").document(f"{bot_id}_{chat_id}").delete()
    except Exception:
        pass


def _load_allowed_chats():
    if _db is None:
        return
    try:
        for d in _db.collection("mon_chats").limit(5000).stream():
            rec = d.to_dict()
            bid = rec.get("bot_id")
            cid = rec.get("chat_id")
            if bid is not None and cid is not None:
                _allowed_chats.setdefault(bid, set()).add(cid)
    except Exception as e:
        _logger.exception("load_allowed_chats failed: %s", e)


# ================= معالج المراقبة (قروبات) =================
@monitor_dp.message(F.chat.type.in_({"group", "supergroup"}))
async def watch_handler(message: Message, bot: Bot):
    u = message.from_user
    if u is None or u.is_bot:
        return
    bot_id = bot.id
    chat_id = message.chat.id
    if chat_id not in _allowed_chats.get(bot_id, set()):
        return  # قروب غير مصرّح — تجاهل

    key = (bot_id, u.id)
    cur_un = u.username
    cur_first = u.first_name or ""
    cur_last = u.last_name or ""
    cur_name = _full_name(cur_first, cur_last)

    rec = _watch_cache.get(key)
    if rec is None:
        rec = _load_rec(bot_id, u.id)
        if rec is None:
            rec = {
                "username": cur_un,
                "first": cur_first,
                "last": cur_last,
                "names": [{"v": cur_name, "at": _now().isoformat()}],
                "unames": [{"v": cur_un, "at": _now().isoformat()}],
            }
            _watch_cache[key] = rec
            _save_rec(bot_id, u.id, rec)
            _index_username(bot_id, cur_un, u.id)
            return
        _watch_cache[key] = rec

    prev_un = rec.get("username")
    prev_name = _full_name(rec.get("first"), rec.get("last"))
    if prev_un == cur_un and prev_name == cur_name:
        return

    announcements = []
    m = _mention(u)
    if prev_un != cur_un:
        announcements.append(
            f"👀 يا {m}، ليش غيّرت اليوزر؟\n"
            f"من: {_uname(prev_un)}\n"
            f"إلى: {_uname(cur_un)}"
        )
        rec["unames"] = _push_hist(rec.get("unames"), cur_un)
        _index_username(bot_id, cur_un, u.id)
    if prev_name != cur_name:
        announcements.append(
            f"👀 يا {m}، ليش غيّرت اسمك؟\n"
            f"من: {_html.escape(prev_name) or 'بدون اسم'}\n"
            f"إلى: {_html.escape(cur_name) or 'بدون اسم'}"
        )
        rec["names"] = _push_hist(rec.get("names"), cur_name)

    rec["username"] = cur_un
    rec["first"] = cur_first
    rec["last"] = cur_last
    _watch_cache[key] = rec
    _save_rec(bot_id, u.id, rec)

    for text in announcements:
        try:
            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            _logger.exception("announce failed: %s", e)


# ================= إضافة/إزالة البوت من القروبات =================
@monitor_dp.my_chat_member()
async def on_my_member(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = event.new_chat_member.status
    added_by = event.from_user.id if event.from_user else None

    if status in ("member", "administrator"):
        if _is_owner(added_by):
            _approve_chat(bot.id, chat.id, added_by, title=chat.title)
            try:
                await _bot.send_message(
                    _ADMIN_ID,
                    f"✅ اعتُمد القروب للمراقبة:\n{chat.title or chat.id} (آيدي: <code>{chat.id}</code>)",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            try:
                await bot.leave_chat(chat.id)
            except Exception:
                pass
            try:
                await _bot.send_message(
                    _ADMIN_ID,
                    f"⚠️ حاول شخص (آيدي: {added_by}) إضافة بوت المراقبة لقروب «{chat.title or chat.id}». غادر تلقائياً.",
                )
            except Exception:
                pass
    elif status in ("left", "kicked"):
        _revoke_chat(bot.id, chat.id)


# ================= بحث المالك بالخاص =================
def _lookup(bot_id, q: str) -> str:
    if _db is None:
        return "التخزين غير مفعّل حاليًا."
    q = (q or "").strip()
    uid = None
    if q.lstrip("-").isdigit():
        uid = int(q)
    else:
        uname = q.lstrip("@").strip().lower()
        try:
            snap = _db.collection("mon_uindex").document(f"{bot_id}_{uname}").get()
            if snap.exists:
                uid = snap.to_dict().get("user_id")
        except Exception as e:
            _logger.exception("uindex lookup failed: %s", e)
    if uid is None:
        return (
            "❌ ما لقيت هذا الشخص.\n"
            "لازم يكون البوت شافه في قروب مراقَب (أرسل آيدي رقمي أو @يوزر شافه البوت)."
        )
    try:
        snap = _db.collection("watched").document(f"{bot_id}_{uid}").get()
    except Exception as e:
        _logger.exception("watched lookup failed: %s", e)
        return "⚠️ تعذّر الجلب الآن."
    if not snap.exists:
        return "❌ ما لقيت بيانات لهذا الشخص."
    d = snap.to_dict()
    cur_name = _full_name(d.get("first_name"), d.get("last_name")) or "بدون اسم"
    lines = [
        "🔎 نتيجة البحث",
        "━━━━━━━━━━━━━━",
        f"👤 الاسم الحالي: {_html.escape(cur_name)}",
        f"🔗 اليوزر الحالي: {_uname(d.get('username'))}",
        f"🆔 الآيدي: <code>{uid}</code>",
        "━━━━━━━━━━━━━━",
        "📛 آخر الأسماء:",
    ]
    names = d.get("names") or []
    if names:
        for i, e in enumerate(names[:5], 1):
            lines.append(f"{i}. {_html.escape(e.get('v') or 'بدون اسم')}")
    else:
        lines.append("—")
    lines += ["━━━━━━━━━━━━━━", "🔗 آخر اليوزرات:"]
    unames = d.get("unames") or []
    if unames:
        for i, e in enumerate(unames[:5], 1):
            v = e.get("v")
            lines.append(f"{i}. {('@' + v) if v else 'بدون يوزر'}")
    else:
        lines.append("—")
    return "\n".join(lines)


@monitor_dp.message(F.chat.type == "private")
async def mon_private(message: Message, bot: Bot):
    u = message.from_user
    if u is None:
        return
    if not _is_owner(u.id):
        await message.answer(
            "🔎 أنا بوت مراقبة تغيير اليوزرات والأسماء.\n"
            "الإدارة والبحث للمالك فقط."
        )
        return
    q = (message.text or "").strip()
    if not q or q.startswith("/"):
        await message.answer(
            "🔎 بوت المراقبة — بحث المالك\n\n"
            "أرسل آيدي رقمي أو @يوزر (حتى لو قديم) لأعطيك:\n"
            "• الاسم الحالي واليوزر الحالي\n"
            "• آخر 5 أسماء\n"
            "• آخر 5 يوزرات"
        )
        return
    await message.answer(_lookup(bot.id, q), parse_mode="HTML")


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
            allowed_updates=["message", "my_chat_member"],
        )
        _logger.info("monitor webhook set for @%s", username)
    except Exception as e:
        _logger.exception("monitor set_webhook failed: %s", e)
    return b


async def _mon_startup_bg():
    if _db is None:
        return
    _load_allowed_chats()
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
        kb.button(text="📋 القروبات المراقَبة", callback_data="mon:chats")
        kb.button(text="🗑 حذف بوت", callback_data="mon:dellist")
    kb.button(text="🔙 رجوع", callback_data="mon:back")
    kb.adjust(1)
    return kb.as_markup()


async def _cb_mon_open(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
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
    if not _is_owner(callback.from_user.id):
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
                "added_at": _now(),
            })
        except Exception as e:
            _logger.exception("monitor store failed: %s", e)
    MONITOR_BOTS[token] = tmp
    BOT_INFO[token] = {"id": me.id, "username": me.username}
    try:
        await tmp.set_webhook(
            f"{_WEBHOOK_HOST}/mon/{token}",
            drop_pending_updates=True,
            allowed_updates=["message", "my_chat_member"],
        )
    except Exception as e:
        _logger.exception("monitor set_webhook failed: %s", e)
    await message.answer(
        f"✅ تم ربط البوت بنجاح: @{me.username}\n\n"
        "📌 باقي خطوتين لتفعيل المراقبة:\n"
        "1) من @BotFather أوقف Privacy Mode لهذا البوت\n"
        "   (Bot Settings ← Group Privacy ← Turn OFF)\n"
        f"2) أضِف أنت @{me.username} في قروبك (مشرف أفضل).\n"
        "   يُعتمد القروب تلقائياً لأنك المالك.\n\n"
        "بعدها يبدأ يراقب تلقائياً 👍",
        reply_markup=_mon_menu_kb(),
    )


async def _cb_mon_dellist(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
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
    if not _is_owner(callback.from_user.id):
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


async def _cb_mon_chats(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    if _db is None:
        await callback.message.answer("التخزين غير مفعّل.", reply_markup=_mon_menu_kb())
        return
    rows = []
    try:
        for d in _db.collection("mon_chats").limit(1000).stream():
            rows.append(d.to_dict())
    except Exception as e:
        _logger.exception("list chats failed: %s", e)
    if not rows:
        await callback.message.answer(
            "لا توجد قروبات مراقَبة بعد.\nأضف أنت بوت المراقبة لقروبك ليُعتمد تلقائياً.",
            reply_markup=_mon_menu_kb(),
        )
        return
    kb = InlineKeyboardBuilder()
    lines = ["📋 القروبات المراقَبة:", ""]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or str(r.get("chat_id"))
        lines.append(f"{i}. {title}")
        kb.button(text=f"🗑 {title}", callback_data=f"mon:dc:{r.get('bot_id')}:{r.get('chat_id')}")
    kb.button(text="🔙 رجوع", callback_data="admin:mon")
    kb.adjust(1)
    await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup())


async def _cb_mon_delchat(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    parts = callback.data.split(":")  # mon:dc:{bot_id}:{chat_id}
    try:
        bid = int(parts[2])
        cid = int(parts[3])
    except Exception:
        await callback.message.answer("خطأ في البيانات.", reply_markup=_mon_menu_kb())
        return
    _revoke_chat(bid, cid)
    b = None
    for token, info in BOT_INFO.items():
        if (info or {}).get("id") == bid:
            b = MONITOR_BOTS.get(token)
            break
    if b:
        try:
            await b.leave_chat(cid)
        except Exception:
            pass
    await callback.message.answer("✅ تمت إزالة القروب وإيقاف مراقبته.", reply_markup=_mon_menu_kb())


async def _cb_mon_back(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
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
    dp.callback_query.register(_cb_mon_chats, F.data == "mon:chats")
    dp.callback_query.register(_cb_mon_delchat, F.data.startswith("mon:dc:"))
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
