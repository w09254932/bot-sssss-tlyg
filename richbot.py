# -*- coding: utf-8 -*-
# بوت تفاعلي بالرسائل الغنية (Bot API 10.x) — ملف مستقل يُدمج مع main.py
# يُعالَج بمعالج webhook خام (بدون aiogram) حتى نقدر نستقبل ضغطات الأزرار على
# الرسائل الغنية بدون ما تتعطّل بسبب قدم aiogram. المرحلة الأولى: ديمو عدّاد.
# كل بوت تفاعلي له توكن خاص، ويُشغّل على مسار /rich/{token}
import asyncio
import json as _json
import os

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

OWNER_ID = 1652300694

# ---- حقن من main.py عبر setup_richbot ----
_main_bot = None
_db = None
_ADMIN_ID = 0
_WEBHOOK_HOST = ""
_logger = None
_admin_menu_markup = None

RICH_BOTS = {}   # token -> {"id":.., "username":..}
_compute_battle = None   # يُحقن من main.py (معادلة معركة الشعبية)

_API = "https://api.telegram.org/bot{token}/{method}"


def _is_owner(uid) -> bool:
    return uid == OWNER_ID or (_ADMIN_ID and uid == _ADMIN_ID)


# ================= التشفير (نفس مفتاح MON_KEY) =================
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
    except Exception:
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


# ================= استدعاء Bot API خام =================
async def _api(token, method, payload):
    url = _API.format(token=token, method=method)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload) as resp:
                return await resp.json()
    except Exception as e:
        if _logger:
            _logger.exception("rich api %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


# ================= بناء البطاقة الغنية (ديمو عدّاد) =================
def _fmt(x):
    try:
        return str(int(x)) if float(x) == int(x) else f"{x:.1f}"
    except Exception:
        return str(x)


def _digit_rows(step, a, cur):
    def row(keys):
        return {"type": "buttons", "align": "center", "buttons": [
            {"text": label, "callback_data": f"k:{step}:{a}:{cur}:{code}"} for code, label in keys
        ]}
    return [
        row([("1", "1"), ("2", "2"), ("3", "3")]),
        row([("4", "4"), ("5", "5"), ("6", "6")]),
        row([("7", "7"), ("8", "8"), ("9", "9")]),
        row([("C", "C"), ("0", "0"), ("B", "⌫")]),
    ]


def _calc_card(step, a, cur):
    shown = f"{int(cur):,}" if cur else "0"
    if step in (1, "1"):
        blocks = [
            {"type": "heading", "size": 2, "text": "⚔️ معركة الشعبية"},
            {"type": "paragraph", "text": ["الخطوة 1 من 2 — أدخل عدد شعبيتك:"]},
            {"type": "paragraph", "text": ["➡️ ", {"type": "bold", "text": shown}]},
        ]
        blocks += _digit_rows("1", a, cur)
        blocks.append({"type": "buttons", "align": "center", "buttons": [
            {"text": "التالي ➡️", "callback_data": f"k:1:{a}:{cur}:OK", "style": "primary"}]})
    else:
        a_show = f"{int(a):,}" if a else "0"
        blocks = [
            {"type": "heading", "size": 2, "text": "⚔️ معركة الشعبية"},
            {"type": "paragraph", "text": ["شعبيتك: ", {"type": "bold", "text": a_show}]},
            {"type": "paragraph", "text": ["الخطوة 2 من 2 — أدخل عدد شعبية خصمك:"]},
            {"type": "paragraph", "text": ["➡️ ", {"type": "bold", "text": shown}]},
        ]
        blocks += _digit_rows("2", a, cur)
        blocks.append({"type": "buttons", "align": "center", "buttons": [
            {"text": "احسب ✅", "callback_data": f"k:2:{a}:{cur}:OK", "style": "success"}]})
    return {"blocks": blocks}


def _cell(text, header=False, align="center"):
    return {"text": text, "is_header": header, "align": align, "valign": "middle"}


def _result_card(my, opp, res):
    return {"blocks": [
        {"type": "heading", "size": 2, "text": "🏆 نتيجة معركة الشعبية"},
        {"type": "paragraph", "text": ["النتيجة: ", {"type": "bold", "text": res.get("result_label", "")}]},
        {"type": "table", "is_bordered": True, "is_striped": True, "cells": [
            [_cell(""), _cell("الشعبية", True), _cell("النقاط", True), _cell("الناتج", True)],
            [_cell("أنت", True, "right"), _cell(f"{my:,}"),
             _cell(str(res.get("my_points", ""))), _cell(_fmt(res.get("my_result")))],
            [_cell("الخصم", True, "right"), _cell(f"{opp:,}"),
             _cell(str(res.get("opp_points", ""))), _cell(_fmt(res.get("opp_result")))],
        ]},
        {"type": "buttons", "align": "center", "buttons": [
            {"text": "🔄 معركة جديدة", "callback_data": "k:new", "style": "primary"}]},
    ]}


# ================= المعالج الخام للتحديثات =================
async def _handle(token, upd):
    try:
        if "message" in upd:
            msg = upd["message"] or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            if chat.get("type") == "private" and text.startswith("/start"):
                r = await _api(token, "sendRichMessage",
                               {"chat_id": chat_id, "rich_message": _calc_card(1, "", "")})
                if not (isinstance(r, dict) and r.get("ok")) and _logger:
                    _logger.warning("rich sendRichMessage: %s", _json.dumps(r)[:300])
        elif "callback_query" in upd:
            cq = upd["callback_query"] or {}
            cq_id = cq.get("id")
            data = cq.get("data") or ""
            m = cq.get("message") or {}
            chat_id = (m.get("chat") or {}).get("id")
            message_id = m.get("message_id")
            new_card = None
            parts = data.split(":")
            if parts and parts[0] == "k":
                if len(parts) == 2 and parts[1] == "new":
                    new_card = _calc_card(1, "", "")
                elif len(parts) == 5:
                    step, a, cur, key = parts[1], parts[2], parts[3], parts[4]
                    if key == "C":
                        new_card = _calc_card(step, a, "")
                    elif key == "B":
                        new_card = _calc_card(step, a, cur[:-1])
                    elif key.isdigit():
                        nc = cur + key
                        if len(nc) > 1 and nc[0] == "0":
                            nc = nc.lstrip("0")
                        if len(nc) > 15:
                            nc = cur
                        new_card = _calc_card(step, a, nc)
                    elif key == "OK":
                        if step == "1" and cur:
                            new_card = _calc_card("2", cur, "")
                        elif step == "2" and a and cur and _compute_battle is not None:
                            try:
                                res = _compute_battle(int(a), int(cur), "individual")
                                new_card = _result_card(int(a), int(cur), res)
                            except Exception as e:
                                if _logger:
                                    _logger.exception("rich calc failed: %s", e)
            if cq_id:
                await _api(token, "answerCallbackQuery", {"callback_query_id": cq_id})
            if new_card is not None and chat_id and message_id:
                await _api(token, "editMessageText",
                           {"chat_id": chat_id, "message_id": message_id,
                            "rich_message": new_card})
    except Exception as e:
        if _logger:
            _logger.exception("rich handle failed: %s", e)


async def _rich_webhook(request):
    token = request.match_info.get("token", "")
    if token not in RICH_BOTS:
        return web.Response(status=404, text="not found")
    try:
        upd = await request.json()
    except Exception:
        return web.Response(status=400, text="bad request")
    asyncio.create_task(_handle(token, upd))
    return web.Response(text="ok")


# ================= تفعيل بوتات التفاعل =================
async def _activate_rich_bot(token, username=None, bot_id=None):
    RICH_BOTS[token] = {"id": bot_id, "username": username}
    try:
        await _api(token, "setWebhook", {
            "url": f"{_WEBHOOK_HOST}/rich/{token}",
            "drop_pending_updates": True,
            "allowed_updates": ["message", "callback_query"],
        })
        if _logger:
            _logger.info("rich webhook set for @%s", username)
    except Exception as e:
        if _logger:
            _logger.exception("rich set_webhook failed: %s", e)


async def add_rich_bot(token, me):
    bot_id = me.get("id") if isinstance(me, dict) else getattr(me, "id", None)
    username = me.get("username") if isinstance(me, dict) else getattr(me, "username", None)
    if _db is not None:
        try:
            _db.collection("rich_bots").document(str(bot_id)).set({
                "token": _enc(token),
                "username": username,
                "bot_id": bot_id,
            })
        except Exception as e:
            if _logger:
                _logger.exception("rich store failed: %s", e)
    await _activate_rich_bot(token, username, bot_id)
    return True


# ================= لوحة إدارة البوت التفاعلي (داخل بوت الشعبية) =================
class RichAddFSM(StatesGroup):
    token = State()


def _rich_admin_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ إضافة بوت تفاعلي", callback_data="rich:add")
    kb.button(text="🔙 رجوع", callback_data="rich:back")
    kb.adjust(1)
    return kb.as_markup()


async def _cb_admin_rich(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer(); return
    await callback.answer()
    await state.clear()
    txt = (
        "🧮 <b>البوت التفاعلي (رسائل غنية)</b>\n"
        "━━━━━━━━━━━━━━\n"
        "أضف توكن بوت مستقل، وبعدها افتحه بالخاص وأرسل /start لتجربة العدّاد التفاعلي.\n"
        "الأزرار تعدّل البطاقة داخل نفس الرسالة."
    )
    try:
        await callback.message.edit_text(txt, parse_mode="HTML", reply_markup=_rich_admin_kb())
    except Exception:
        await callback.message.answer(txt, parse_mode="HTML", reply_markup=_rich_admin_kb())


async def _cb_rich_add(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer(); return
    await callback.answer()
    await state.set_state(RichAddFSM.token)
    await callback.message.answer(
        "📥 أرسل الآن توكن البوت التفاعلي (من @BotFather).\n\n"
        "مثال: 1234567890:AAExampleTokenHere...\n"
        "/cancel للإلغاء"
    )


async def _got_rich_token(message: Message, state: FSMContext):
    token = (message.text or "").strip()
    await state.clear()
    if ":" not in token or len(token) < 20:
        await message.answer("❌ التوكن غير صالح. تأكد من نسخه كامل وحاول مرة ثانية.")
        return
    me_resp = await _api(token, "getMe", {})
    if not (isinstance(me_resp, dict) and me_resp.get("ok")):
        await message.answer("❌ التوكن غير صالح. تأكد من نسخه كامل وحاول مرة ثانية.")
        return
    me = me_resp.get("result") or {}
    try:
        await add_rich_bot(token, me)
        await message.answer(
            f"✅ تم ربط البوت التفاعلي: @{me.get('username')}\n\n"
            f"افتح @{me.get('username')} بالخاص وأرسل /start لتجربة العدّاد الغني."
        )
    except Exception as e:
        if _logger:
            _logger.exception("rich add failed: %s", e)
        await message.answer("⚠️ تعذّر تفعيل البوت التفاعلي.")


async def _cb_rich_back(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer(); return
    await callback.answer()
    await state.clear()
    markup = _admin_menu_markup() if _admin_menu_markup else None
    try:
        await callback.message.edit_text("🛠 لوحة المالك", reply_markup=markup)
    except Exception:
        await callback.message.answer("🛠 لوحة المالك", reply_markup=markup)


def _register_admin_handlers(dp: Dispatcher):
    dp.callback_query.register(_cb_admin_rich, F.data == "admin:rich")
    dp.callback_query.register(_cb_rich_add, F.data == "rich:add")
    dp.callback_query.register(_cb_rich_back, F.data == "rich:back")
    dp.message.register(_got_rich_token, StateFilter(RichAddFSM.token), F.from_user.id == _ADMIN_ID)


# ================= الإقلاع =================
async def _rich_startup_bg():
    if _db is None:
        return
    try:
        docs = list(_db.collection("rich_bots").limit(200).stream())
    except Exception as e:
        if _logger:
            _logger.exception("rich load bots failed: %s", e)
        return
    for d in docs:
        rec = d.to_dict()
        token = _dec(rec.get("token"))
        if token:
            await _activate_rich_bot(token, rec.get("username"), rec.get("bot_id"))


async def _rich_startup():
    asyncio.create_task(_rich_startup_bg())


def setup_richbot(app, dp, main_bot, db, admin_id, webhook_host, logger, admin_menu_markup=None, compute_battle=None):
    global _main_bot, _db, _ADMIN_ID, _WEBHOOK_HOST, _logger, _admin_menu_markup, _compute_battle
    _compute_battle = compute_battle
    _main_bot = main_bot
    _db = db
    _ADMIN_ID = admin_id
    _WEBHOOK_HOST = (webhook_host or "").rstrip("/")
    _logger = logger
    _admin_menu_markup = admin_menu_markup
    _init_crypto()
    _register_admin_handlers(dp)
    app.router.add_post("/rich/{token}", _rich_webhook)
    dp.startup.register(_rich_startup)
    logger.info("richbot module ready")
