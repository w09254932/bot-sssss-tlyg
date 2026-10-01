# -*- coding: utf-8 -*-
# مراقبة تغيير اليوزر/الاسم داخل القروبات — ملف مستقل يُدمج مع main.py
# - تاريخ آخر 5 أسماء + آخر 5 يوزرات لكل عضو
# - بحث بالخاص للمالك فقط: بالآيدي / باليوزر / بالاسم (قائمة مطابقين) + أزرار
# - قائمة بيضاء: يشتغل فقط في القروبات التي يضيفه لها المالك، ويغادر غيرها تلقائياً
# - تأكيد قبل إزالة قروب من المراقبة
# كل بوت مراقبة له توكن خاص، ويُشغّل على مسار /mon/{token}
import asyncio
import html as _html
import os
import time
import traceback
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
_silent_chats = {}     # bot_id -> set(chat_id) في وضع صامت (يراقب بدون تنبيهات)
_last_err = {}         # توقيع الخطأ -> آخر وقت أُرسل فيه (throttle)
_rate_cb = {}          # user_id -> آخر وقت ضغط زر (حد المعدّل)
_RATE_WINDOW = 0.5     # ثانية — أقل من كذا بين ضغطتين = يتجاهل

monitor_dp = Dispatcher()   # ديسباتشر واحد يخدم كل بوتات المراقبة


class MonAdminFSM(StatesGroup):
    token = State()


class MonLookupFSM(StatesGroup):
    by_id = State()
    by_uname = State()
    by_name = State()
    add_id = State()
    man_id = State()
    man_uname = State()
    man_name = State()


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


# ================= تشفير التوكنات =================
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
        _logger.info("token encryption enabled")
    except Exception as e:
        _logger.exception("crypto init failed: %s", e)
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
            _logger.warning("encrypted token but MON_KEY missing")
            return None
        try:
            return _fernet.decrypt(stored[4:].encode()).decode()
        except Exception:
            _logger.exception("token decrypt failed")
            return None
    return stored


# ================= تخزين الأعضاء + الفهارس =================
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


def _norm_name(name):
    return (name or "").strip().lower().replace("/", "_")


def _index_name(bot_id, name, uid):
    if _db is None:
        return
    norm = _norm_name(name)
    if not norm:
        return
    try:
        ref = _db.collection("mon_nindex").document(f"{bot_id}_{norm}")
        snap = ref.get()
        ids = []
        if snap.exists:
            ids = snap.to_dict().get("user_ids") or []
        if uid not in ids:
            ids.append(uid)
            ids = ids[-50:]
        ref.set({
            "bot_id": bot_id,
            "name": name,
            "user_ids": ids,
            "updated_at": _now(),
        })
    except Exception as e:
        _logger.exception("index_name failed: %s", e)


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
    ss = _silent_chats.get(bot_id)
    if ss and chat_id in ss:
        ss.discard(chat_id)
    if _db is None:
        return
    try:
        _db.collection("mon_chats").document(f"{bot_id}_{chat_id}").delete()
    except Exception:
        pass


def _is_silent(bot_id, chat_id) -> bool:
    return chat_id in _silent_chats.get(bot_id, set())


def _set_silent(bot_id, chat_id, silent):
    s = _silent_chats.setdefault(bot_id, set())
    if silent:
        s.add(chat_id)
    else:
        s.discard(chat_id)
    if _db is None:
        return
    try:
        ref = _db.collection("mon_chats").document(f"{bot_id}_{chat_id}")
        snap = ref.get()
        data = snap.to_dict() if snap.exists else {"bot_id": bot_id, "chat_id": chat_id}
        data["silent"] = bool(silent)
        ref.set(data)
    except Exception as e:
        _logger.exception("set_silent failed: %s", e)


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
                if rec.get("silent"):
                    _silent_chats.setdefault(bid, set()).add(cid)
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
            _index_name(bot_id, cur_name, u.id)
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
        _index_name(bot_id, cur_name, u.id)

    rec["username"] = cur_un
    rec["first"] = cur_first
    rec["last"] = cur_last
    _watch_cache[key] = rec
    _save_rec(bot_id, u.id, rec)

    if _is_silent(bot_id, chat_id):
        return  # وضع صامت — سجّلنا التاريخ بدون تنبيه

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


# ================= صياغة النتائج =================
def _get_watched(bot_id, uid):
    if _db is None:
        return None
    try:
        snap = _db.collection("watched").document(f"{bot_id}_{uid}").get()
        return snap.to_dict() if snap.exists else None
    except Exception as e:
        _logger.exception("get_watched failed: %s", e)
        return None


def _format_person(d, uid) -> str:
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


def _lookup_by_id(bot_id, q) -> str:
    if _db is None:
        return "التخزين غير مفعّل حاليًا."
    q = (q or "").strip()
    if not q.lstrip("-").isdigit():
        return "❌ أرسل آيدي رقمي صحيح."
    uid = int(q)
    d = _get_watched(bot_id, uid)
    if not d:
        return "❌ ما لقيت بيانات لهذا الآيدي (لازم البوت شافه في قروب مراقَب)."
    return _format_person(d, uid)


def _lookup_by_uname(bot_id, q) -> str:
    if _db is None:
        return "التخزين غير مفعّل حاليًا."
    uname = (q or "").lstrip("@").strip().lower()
    if not uname:
        return "❌ أرسل يوزر صحيح."
    try:
        snap = _db.collection("mon_uindex").document(f"{bot_id}_{uname}").get()
    except Exception as e:
        _logger.exception("uindex lookup failed: %s", e)
        return "⚠️ تعذّر الجلب الآن."
    if not snap.exists:
        return "❌ ما لقيت أحد بهذا اليوزر (حتى القديم)."
    uid = snap.to_dict().get("user_id")
    d = _get_watched(bot_id, uid)
    if not d:
        return "❌ ما لقيت بيانات لهذا الشخص."
    return _format_person(d, uid)


def _lookup_by_name(bot_id, q) -> str:
    if _db is None:
        return "التخزين غير مفعّل حاليًا."
    norm = _norm_name(q)
    if not norm:
        return "❌ أرسل اسمًا للبحث."
    try:
        snap = _db.collection("mon_nindex").document(f"{bot_id}_{norm}").get()
    except Exception as e:
        _logger.exception("nindex lookup failed: %s", e)
        return "⚠️ تعذّر الجلب الآن."
    if not snap.exists:
        return "❌ ما لقيت أحد بهذا الاسم."
    ids = snap.to_dict().get("user_ids") or []
    if not ids:
        return "❌ ما لقيت أحد بهذا الاسم."
    lines = [f"📛 نتائج الاسم «{_html.escape((q or '').strip())}»: {len(ids)}", "━━━━━━━━━━━━━━"]
    shown = 0
    for uid in ids[:10]:
        d = _get_watched(bot_id, uid)
        if not d:
            continue
        shown += 1
        cur_name = _full_name(d.get("first_name"), d.get("last_name")) or "بدون اسم"
        lines.append(f"{shown}. 👤 {_html.escape(cur_name)}")
        lines.append(f"    🔗 {_uname(d.get('username'))} | 🆔 <code>{uid}</code>")
    if shown == 0:
        return "❌ ما لقيت بيانات مطابقة."
    if len(ids) > 10:
        lines.append(f"… (عرض 10 من {len(ids)})")
    lines += ["━━━━━━━━━━━━━━", "للتفاصيل الكاملة أرسل الآيدي."]
    return "\n".join(lines)


# ================= بحث المالك بالخاص =================
def _mon_search_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔢 بحث بآيدي", callback_data="monq:id")
    kb.button(text="🔗 بحث بيوزر", callback_data="monq:uname")
    kb.button(text="📛 بحث بالاسم", callback_data="monq:name")
    kb.button(text="➕ إضافة بالآيدي", callback_data="monq:add")
    kb.button(text="✍️ إضافة يدوية", callback_data="monq:man")
    kb.adjust(1)
    return kb.as_markup()


def _mon_search_intro():
    return (
        "🔎 بوت المراقبة — بحث المالك\n\n"
        "اختر نوع البحث، أو أرسل مباشرة:\n"
        "• آيدي رقمي (بحث بالآيدي)\n"
        "• @يوزر (بحث باليوزر، حتى قديم)\n"
        "• اسم (بحث بالاسم — قائمة مطابقين)\n\n"
        "➕ إضافة بالآيدي: يجلب اليوزر والاسم تلقائياً ويحفظهم.\n"
        "✍️ إضافة يدوية: تكتب الآيدي واليوزر والاسم بنفسك."
    )


async def _add_by_id(bot: Bot, uid: int) -> str:
    if _db is None:
        return "التخزين غير مفعّل حاليًا."
    try:
        chat = await bot.get_chat(uid)
    except Exception as e:
        _logger.exception("add_by_id get_chat failed: %s", e)
        return (
            "❌ ما قدرت أجيب بيانات هذا الآيدي.\n"
            "لازم يكون الشخص شارك قروب مع بوت المراقبة أو فتح البوت.\n"
            "تأكد من الآيدي وحاول مرة ثانية."
        )
    username = getattr(chat, "username", None)
    first = getattr(chat, "first_name", "") or ""
    last = getattr(chat, "last_name", "") or ""
    name = _full_name(first, last)
    bot_id = bot.id
    rec = _load_rec(bot_id, uid) or {
        "username": None, "first": "", "last": "", "names": [], "unames": [],
    }
    if not rec.get("names") or _full_name(rec.get("first"), rec.get("last")) != name:
        rec["names"] = _push_hist(rec.get("names"), name)
    if not rec.get("unames") or rec.get("username") != username:
        rec["unames"] = _push_hist(rec.get("unames"), username)
    rec["username"] = username
    rec["first"] = first
    rec["last"] = last
    _save_rec(bot_id, uid, rec)
    _watch_cache[(bot_id, uid)] = rec
    _index_username(bot_id, username, uid)
    _index_name(bot_id, name, uid)
    return "✅ تمت الإضافة:\n" + _format_person(_get_watched(bot_id, uid) or {}, uid)


def _manual_add(bot_id, uid, username, name) -> str:
    if _db is None:
        return "التخزين غير مفعّل حاليًا."
    username = (username or "").lstrip("@").strip() or None
    name = (name or "").strip()
    rec = _load_rec(bot_id, uid) or {
        "username": None, "first": "", "last": "", "names": [], "unames": [],
    }
    if name and (not rec.get("names") or _full_name(rec.get("first"), rec.get("last")) != name):
        rec["names"] = _push_hist(rec.get("names"), name)
    if not rec.get("unames") or rec.get("username") != username:
        rec["unames"] = _push_hist(rec.get("unames"), username)
    rec["username"] = username
    rec["first"] = name
    rec["last"] = ""
    _save_rec(bot_id, uid, rec)
    _watch_cache[(bot_id, uid)] = rec
    _index_username(bot_id, username, uid)
    _index_name(bot_id, name, uid)
    return "✅ تمت الإضافة اليدوية:\n" + _format_person(_get_watched(bot_id, uid) or {}, uid)


@monitor_dp.callback_query(F.data == "monq:id")
async def _cbq_by_id(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(MonLookupFSM.by_id)
    await callback.message.answer("🔢 أرسل الآيدي الرقمي:")


@monitor_dp.callback_query(F.data == "monq:uname")
async def _cbq_by_uname(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(MonLookupFSM.by_uname)
    await callback.message.answer("🔗 أرسل اليوزر (بدون @ أو معه):")


@monitor_dp.callback_query(F.data == "monq:name")
async def _cbq_by_name(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(MonLookupFSM.by_name)
    await callback.message.answer("📛 أرسل الاسم للبحث:")


@monitor_dp.callback_query(F.data == "monq:add")
async def _cbq_add_id(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(MonLookupFSM.add_id)
    await callback.message.answer("➕ أرسل آيدي الشخص لإضافته (يجلب اليوزر والاسم تلقائياً):")


@monitor_dp.message(StateFilter(MonLookupFSM.add_id), F.chat.type == "private")
async def _st_add_id(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    q = (message.text or "").strip()
    if not q.lstrip("-").isdigit():
        await message.answer("❌ أرسل آيدي رقمي صحيح.", reply_markup=_mon_search_kb())
        return
    result = await _add_by_id(bot, int(q))
    await message.answer(result, parse_mode="HTML", reply_markup=_mon_search_kb())


@monitor_dp.callback_query(F.data == "monq:man")
async def _cbq_manual(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(MonLookupFSM.man_id)
    await callback.message.answer("✍️ إضافة يدوية\nأرسل الآيدي الرقمي:")


@monitor_dp.message(StateFilter(MonLookupFSM.man_id), F.chat.type == "private")
async def _st_man_id(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    q = (message.text or "").strip()
    if not q.lstrip("-").isdigit():
        await message.answer("❌ أرسل آيدي رقمي صحيح (أو /cancel).")
        return
    await state.update_data(man_id=int(q))
    await state.set_state(MonLookupFSM.man_uname)
    await message.answer("أرسل اليوزر (بدون @)، أو أرسل - إذا بدون يوزر:")


@monitor_dp.message(StateFilter(MonLookupFSM.man_uname), F.chat.type == "private")
async def _st_man_uname(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    un = (message.text or "").strip()
    if un in ("-", "لا", "بدون", ""):
        un = None
    await state.update_data(man_uname=un)
    await state.set_state(MonLookupFSM.man_name)
    await message.answer("أرسل الاسم:")


@monitor_dp.message(StateFilter(MonLookupFSM.man_name), F.chat.type == "private")
async def _st_man_name(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        return
    name = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    uid = data.get("man_id")
    un = data.get("man_uname")
    if uid is None:
        await message.answer("❌ صار خطأ، ابدأ من جديد.", reply_markup=_mon_search_kb())
        return
    res = _manual_add(bot.id, uid, un, name)
    await message.answer(res, parse_mode="HTML", reply_markup=_mon_search_kb())


@monitor_dp.message(StateFilter(MonLookupFSM.by_id), F.chat.type == "private")
async def _st_by_id(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    await message.answer(_lookup_by_id(bot.id, message.text or ""), parse_mode="HTML", reply_markup=_mon_search_kb())


@monitor_dp.message(StateFilter(MonLookupFSM.by_uname), F.chat.type == "private")
async def _st_by_uname(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    await message.answer(_lookup_by_uname(bot.id, message.text or ""), parse_mode="HTML", reply_markup=_mon_search_kb())


@monitor_dp.message(StateFilter(MonLookupFSM.by_name), F.chat.type == "private")
async def _st_by_name(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    await message.answer(_lookup_by_name(bot.id, message.text or ""), parse_mode="HTML", reply_markup=_mon_search_kb())


@monitor_dp.message(StateFilter(None), F.chat.type == "private")
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
        await message.answer(_mon_search_intro(), reply_markup=_mon_search_kb())
        return
    if q.lstrip("-").isdigit():
        res = _lookup_by_id(bot.id, q)
    elif q.startswith("@"):
        res = _lookup_by_uname(bot.id, q)
    else:
        res = _lookup_by_name(bot.id, q)
    await message.answer(res, parse_mode="HTML", reply_markup=_mon_search_kb())


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


# ================= حد المعدّل (منع سبام الأزرار) =================
async def _throttle_cb(handler, event, data):
    uid = event.from_user.id if getattr(event, "from_user", None) else 0
    now = time.monotonic()
    if now - _rate_cb.get(uid, 0) < _RATE_WINDOW:
        try:
            await event.answer()  # يوقف علامة التحميل بدون تنفيذ
        except Exception:
            pass
        return  # تجاهل الضغطة المتكررة
    _rate_cb[uid] = now
    return await handler(event, data)


# ================= معالجة الأخطاء الموحّدة =================
@monitor_dp.errors()
async def _on_error(event):
    try:
        exc = getattr(event, "exception", None)
        etype = type(exc).__name__ if exc else "Error"
        emsg = str(exc) if exc else ""
        kind = "?"
        try:
            if getattr(event, "update", None) is not None:
                kind = event.update.event_type
        except Exception:
            pass
        sig = f"{etype}:{kind}"
        now = time.monotonic()
        if now - _last_err.get(sig, 0) < 60:
            return True  # نفس الخطأ تكرر بأقل من دقيقة — لا نكرر التنبيه
        _last_err[sig] = now
        tail = ""
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
            tail = "\n".join(lines[-3:])
        except Exception:
            pass
        text = (
            "🚨 خطأ في البوت\n"
            f"المكان: {kind}\n"
            f"النوع: {etype}\n"
            f"الرسالة: {emsg[:300]}\n"
            "─────\n"
            f"{tail[:700]}"
        )
        if _bot is not None and _ADMIN_ID:
            try:
                await _bot.send_message(_ADMIN_ID, text)
            except Exception:
                pass
    except Exception:
        pass
    return True


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
            allowed_updates=["message", "my_chat_member", "callback_query"],
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
        raw = rec.get("token")
        token = _dec(raw)
        if not token:
            continue
        # ترحيل: لو التوكن مخزّن نص صريح والمفتاح متوفر، نعيد حفظه مشفّراً
        if _fernet is not None and isinstance(raw, str) and not raw.startswith("enc:"):
            try:
                _db.collection("monitor_bots").document(str(rec.get("bot_id"))).set(
                    {"token": _enc(token)}, merge=True
                )
            except Exception:
                pass
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
                "token": _enc(token),
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
            allowed_updates=["message", "my_chat_member", "callback_query"],
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


def _build_chats_view():
    # يرجّع (text, markup) أو (None, None) إذا ما فيه قروبات
    if _db is None:
        return None, None
    rows = []
    try:
        for d in _db.collection("mon_chats").limit(1000).stream():
            rows.append(d.to_dict())
    except Exception as e:
        _logger.exception("list chats failed: %s", e)
    if not rows:
        return None, None
    kb = InlineKeyboardBuilder()
    lines = ["📋 القروبات المراقَبة:", "🔔 = يكتب التنبيهات | 🤫 = صامت", ""]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or str(r.get("chat_id"))
        bid = r.get("bot_id")
        cid = r.get("chat_id")
        mode = "🤫" if r.get("silent") else "🔔"
        lines.append(f"{i}. {mode} {title}")
        kb.button(text=f"{mode} {title}", callback_data=f"mon:sil:{bid}:{cid}")
        kb.button(text="🗑", callback_data=f"mon:dc:{bid}:{cid}")
    kb.button(text="🔙 رجوع", callback_data="admin:mon")
    kb.adjust(2)
    return "\n".join(lines), kb.as_markup()


async def _cb_mon_chats(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    text, markup = _build_chats_view()
    if text is None:
        await callback.message.answer(
            "لا توجد قروبات مراقَبة بعد.\nأضف أنت بوت المراقبة لقروبك ليُعتمد تلقائياً.",
            reply_markup=_mon_menu_kb(),
        )
        return
    await callback.message.answer(text, reply_markup=markup)


async def _cb_mon_silent(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    parts = callback.data.split(":")  # mon:sil:{bot_id}:{chat_id}
    try:
        bid = int(parts[2])
        cid = int(parts[3])
    except Exception:
        await callback.answer()
        return
    new_silent = not _is_silent(bid, cid)
    _set_silent(bid, cid, new_silent)
    await callback.answer("🤫 صار صامت" if new_silent else "🔔 صار يكتب التنبيهات")
    text, markup = _build_chats_view()
    if text is None:
        try:
            await callback.message.edit_text("لا توجد قروبات مراقَبة.", reply_markup=_mon_menu_kb())
        except Exception:
            pass
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


async def _cb_mon_delchat(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    parts = callback.data.split(":")  # mon:dc:{bot_id}:{chat_id}
    bid, cid = parts[2], parts[3]
    title = cid
    if _db is not None:
        try:
            snap = _db.collection("mon_chats").document(f"{bid}_{cid}").get()
            if snap.exists:
                title = snap.to_dict().get("title") or cid
        except Exception:
            pass
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 نعم، أزلها", callback_data=f"mon:dcok:{bid}:{cid}")
    kb.button(text="🔙 لا، رجوع", callback_data="mon:chats")
    kb.adjust(1)
    await callback.message.answer(
        f"⚠️ متأكد تبي تزيل «{title}» من المراقبة؟\n"
        "البوت بيغادر القروب ويوقف مراقبته.\n"
        "(تاريخ الأسماء واليوزرات ما ينحذف)",
        reply_markup=kb.as_markup(),
    )


async def _cb_mon_delchat_ok(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    parts = callback.data.split(":")  # mon:dcok:{bot_id}:{chat_id}
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
    dp.callback_query.register(_cb_mon_silent, F.data.startswith("mon:sil:"))
    dp.callback_query.register(_cb_mon_delchat_ok, F.data.startswith("mon:dcok:"))
    dp.callback_query.register(_cb_mon_delchat, F.data.startswith("mon:dc:"))
    dp.callback_query.register(_cb_mon_back, F.data == "mon:back")
    dp.message.register(_got_token, StateFilter(MonAdminFSM.token), F.from_user.id == _ADMIN_ID)
    dp.errors.register(_on_error)


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

    _init_crypto()
    _register_admin_handlers(dp)
    monitor_dp.callback_query.outer_middleware(_throttle_cb)
    dp.callback_query.outer_middleware(_throttle_cb)
    app.router.add_post("/mon/{token}", _mon_webhook)
    dp.startup.register(_mon_startup)
    logger.info("monitor module ready")
