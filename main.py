import os
import asyncio
import json
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChosenInlineResult,
    BotCommand,
    BotCommandScopeChat,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import firebase_admin
from firebase_admin import credentials, firestore

# ---------------- Config ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_HOST = (os.environ.get("WEBHOOK_HOST") or os.environ["RENDER_EXTERNAL_URL"]).rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
PORT = int(os.environ.get("PORT", "10000"))
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("battlebot")

# ---------------- Firestore ----------------
def init_firestore():
    raw = os.environ.get("FIREBASE_CREDENTIALS")
    if not raw:
        logger.warning("FIREBASE_CREDENTIALS not set - storage disabled")
        return None
    try:
        cred = credentials.Certificate(json.loads(raw))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        logger.exception("Firestore init failed: %s", e)
        return None

db = init_firestore()
BOT_USERNAME = None

# ---------------- Points tables ----------------
TIERS_INDIVIDUAL = [
    (0, 2000, 6),
    (2001, 4000, 10),
    (4001, 8000, 14),
    (8001, 15000, 16),
    (15001, 50000, 20),
    (50001, 120000, 24),
    (120001, 260000, 28),
    (260001, 500000, 32),
    (500001, 900000, 36),
    (900001, 2000000, 40),
    (2000001, 8000000, 42),
    (8000001, float("inf"), 44),
]

TIERS_TEAM = [
    (0, 5000, 6),
    (5001, 12000, 10),
    (12001, 26000, 14),
    (26001, 48000, 16),
    (48001, 120000, 20),
    (120001, 200000, 24),
    (200001, 400000, 28),
    (400001, 560000, 32),
    (560001, 800000, 34),
    (800001, 1600000, 36),
    (1600001, 3200000, 38),
    (3200001, 6000000, 40),
    (6000001, float("inf"), 42),
]

MODE_LABELS = {"individual": "الفردية", "team": "الفريق", "home": "المنزل"}
TEAM_WORDS = {"فريق", "الفريق", "team", "Team", "TEAM"}

def points_for(n: int, mode: str = "individual") -> int:
    tiers = TIERS_TEAM if mode == "team" else TIERS_INDIVIDUAL
    for lo, hi, pts in tiers:
        if lo <= n <= hi:
            return pts
    return 0

ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
WESTERN_DIGITS = "0123456789"

def parse_number(text: str):
    if not text:
        return None
    t = text.strip().translate(str.maketrans(ARABIC_DIGITS, WESTERN_DIGITS))
    t = t.replace(",", "").replace("،", "").replace(" ", "")
    if not t.isdigit():
        return None
    return int(t)

def parse_inline(text: str):
    if not text:
        return None
    t = text.translate(str.maketrans(ARABIC_DIGITS, WESTERN_DIGITS)).replace(",", "").replace("،", "")
    mode = "individual"
    nums = []
    for tok in t.split():
        if tok in TEAM_WORDS:
            mode = "team"
        elif tok.isdigit():
            nums.append(int(tok))
        else:
            return None
    if len(nums) != 2:
        return None
    return mode, nums[0], nums[1]

def fmt(x) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:.1f}"

# ---------------- Core compute ----------------
def compute_battle(my_number: int, opp_number: int, mode: str):
    my_points = points_for(my_number, mode)
    opp_points = points_for(opp_number, mode)

    outcome = "win" if my_number > opp_number else "loss"  # تعادل = خسارة

    if outcome == "win":
        result_label = "فوز ✅"
        my_result = my_points + opp_points / 2
        opp_result = opp_points / 2
    else:
        result_label = "خسارة ❌"
        my_result = my_points / 2
        opp_result = opp_points + my_points / 2

    if mode == "team":
        note = "فوز = نقاطكم كاملة + نصف نقاط التيم ضدكم" if outcome == "win" else "خسارة = تأخذون نصف نقاطكم فقط"
        text = (
            "🏆 نتيجة معركة الشعبية الفريق\n"
            "━━━━━━━━━━━━━━\n"
            f"👤 دعم تيمي : {my_number:,}  =  {my_points} نقطة\n"
            f"🎯 دعم تيم خصمي : {opp_number:,}  =  {opp_points} نقطة\n"
            "━━━━━━━━━━━━━━\n"
            f"النتيجة : {result_label}\n"
            f"لكل شخص من تيمك : {fmt(my_result)} نقطة\n"
            f"لكل شخص من تيم الخصم : {fmt(opp_result)} نقطة\n"
            f"{note}"
        )
    else:
        note = "فوز = نقاطك+نصف نقاط خصمك" if outcome == "win" else "خسارة = تأخذ نصف نقاطك فقط"
        text = (
            f"🏆 نتيجة معركة الشعبية {MODE_LABELS.get(mode, '')}\n"
            "━━━━━━━━━━━━━━\n"
            f"👤 نقاطك: {my_number:,}  =  {my_points} نقطة\n"
            f"🎯 نقاط الخصم: {opp_number:,}  =  {opp_points} نقطة\n"
            "━━━━━━━━━━━━━━\n"
            f"النتيجة : {result_label}\n"
            f"نقاطك : {fmt(my_result)} نقطة\n"
            f"نقاط الخصم : {fmt(opp_result)} نقطة\n"
            f"{note}"
        )

    return {
        "text": text,
        "my_points": my_points,
        "opp_points": opp_points,
        "result_label": result_label,
        "my_result": my_result,
        "opp_result": opp_result,
    }

# ---------------- Help text + keyboards ----------------
HELP_TEMPLATE = (
    "احسب معركتك في أي محادثة:\n"
    "⚔️ فردية: @{u} 20000 10000\n"
    "👥 فريق: @{u} فريق 10000 20000\n\n"
    "الحسبة تقريبية وليست 100%\n"
    "استخدم الأزرار وعدّل الأرقام 👇"
)

def help_text():
    return HELP_TEMPLATE.format(u=BOT_USERNAME or "اسم_البوت")

def help_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ معركة الفردية", switch_inline_query_current_chat="20000 10000")
    kb.button(text="👥 معركة فريق", switch_inline_query_current_chat="فريق 20000 10000")
    kb.button(text="🙈 إخفاء الشرح", callback_data="hide_help")
    kb.adjust(2, 1)
    return kb.as_markup()

def menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ معركة الشعبية الفردية", callback_data="battle_individual")
    kb.button(text="🏠 معركة شعبية المنزل", callback_data="battle_home")
    kb.button(text="👥 معركة الشعبية فريق", callback_data="battle_team")
    kb.button(text="🛒 شراء شدات", callback_data="shop")
    kb.adjust(2, 1, 1)
    return kb.as_markup()

def landing_keyboard(mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 سجلي", callback_data=f"hist:{mode}")
    kb.button(text="▶️ ابدأ الحسبة", callback_data=f"calc:{mode}")
    kb.button(text="🔙 رجوع", callback_data="menu")
    kb.adjust(2, 1)
    return kb.as_markup()

def result_keyboard(mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 معركة جديدة", callback_data=f"calc:{mode}")
    kb.button(text="📜 سجلي", callback_data=f"hist:{mode}")
    kb.button(text="🔙 القائمة", callback_data="menu")
    kb.adjust(2, 1)
    return kb.as_markup()

def history_picker_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 سجل الفردية", callback_data="hist:individual")
    kb.button(text="📜 سجل المنزل", callback_data="hist:home")
    kb.button(text="📜 سجل الفريق", callback_data="hist:team")
    kb.adjust(1)
    return kb.as_markup()

def cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ إلغاء", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()

def admin_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 الإحصائيات", callback_data="admin:stats")
    kb.button(text="⭐ رصيد النجوم", callback_data="admin:stars")
    kb.button(text="🛒 إدارة المتجر", callback_data="admin:store")
    kb.button(text="📢 بث رسالة", callback_data="admin:broadcast")
    kb.button(text="🚫 إدارة الحظر", callback_data="admin:ban")
    kb.adjust(1)
    return kb.as_markup()

def store_admin_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ إضافة منتج", callback_data="st:addp")
    kb.button(text="📦 إضافة أكواد", callback_data="st:addc")
    kb.button(text="📋 المنتجات والمخزون", callback_data="st:list")
    kb.adjust(1)
    return kb.as_markup()

def compute_admin_stats() -> str:
    if db is None:
        return "📊 التخزين غير مفعّل حاليًا."
    try:
        try:
            users_count = len(list(db.collection("users").limit(10000).stream()))
        except Exception:
            users_count = 0
        snap = db.collection("stats").document("counters").get()
        c = snap.to_dict() if snap.exists else {}
        total = c.get("total", 0)
        ind = c.get("mode_individual", 0)
        home = c.get("mode_home", 0)
        team = c.get("mode_team", 0)
        try:
            orders_count = len(list(db.collection("orders").limit(10000).stream()))
        except Exception:
            orders_count = 0
        return (
            "🛠 لوحة المالك - الإحصائيات\n"
            "━━━━━━━━━━━━━━\n"
            f"👥 المستخدمون: {users_count}\n"
            f"⚔️ إجمالي المعارك: {total}\n"
            f"   • فردية: {ind}\n"
            f"   • منزل: {home}\n"
            f"   • فريق: {team}\n"
            f"🛒 مبيعات المتجر: {orders_count}"
        )
    except Exception as e:
        logger.exception("admin stats failed: %s", e)
        return "⚠️ تعذّر جلب الإحصائيات الآن."

# ---------------- FSM ----------------
class BattleFSM(StatesGroup):
    my_number = State()
    opp_number = State()

class AdminFSM(StatesGroup):
    broadcast = State()
    p_name = State()
    p_price = State()
    codes = State()

dp = Dispatcher()
bot = Bot(BOT_TOKEN)

async def send_menu(message: Message, state: FSMContext = None):
    if state:
        await state.clear()
    name = message.chat.first_name or message.chat.full_name or "صديقي"
    await message.answer(
        f"👋 أهلًا يا {name} في حاسبة معركة الشعبية\n"
        "\n"
        "اختر نوع حاسبة الشعبية:",
        reply_markup=menu_keyboard(),
    )

async def send_landing(message: Message, mode: str, state: FSMContext):
    await state.clear()
    await message.answer(
        f"🔥 معركة الشعبية - {MODE_LABELS.get(mode, '')}\n"
        "━━━━━━━━━━━━━━\n"
        "اختر من الأزرار:",
        reply_markup=landing_keyboard(mode),
    )

async def start_calc(message: Message, mode: str, state: FSMContext):
    await state.clear()
    await state.set_state(BattleFSM.my_number)
    await state.update_data(mode=mode)
    prompt = "أرسل عدد شعبية تيمك كامل:" if mode == "team" else "أرسل عدد شعبيتك"
    await message.answer(
        f"🔥 معركة الشعبية - {MODE_LABELS.get(mode, '')}\n"
        "━━━━━━━━━━━━━━\n"
        f"{prompt}",
        reply_markup=cancel_keyboard(),
    )

@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext, command: CommandObject = None):
    register_user(message.from_user)
    arg = (command.args or "").strip() if command else ""
    if arg == "team":
        await send_landing(message, "team", state)
    elif arg == "home":
        await send_landing(message, "home", state)
    elif arg == "individual":
        await send_landing(message, "individual", state)
    else:
        await send_menu(message, state)

@dp.message(CommandStart(), F.chat.type.in_({"group", "supergroup"}))
async def start_cmd_group(message: Message):
    kb = InlineKeyboardBuilder()
    if BOT_USERNAME:
        kb.button(text="🔗 افتح البوت بالخاص", url=f"https://t.me/{BOT_USERNAME}?start=1")
    kb.adjust(1)
    await message.answer(
        "👋 للحاسبة الكاملة افتح البوت بالخاص\n"
        "أو اكتب: حاسبة",
        reply_markup=kb.as_markup(),
    )

@dp.message(Command("battle"), F.chat.type == "private")
async def battle_cmd(message: Message, state: FSMContext):
    await send_menu(message, state)

@dp.message(Command("history"), F.chat.type == "private")
async def history_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("اختر السجل:", reply_markup=history_picker_keyboard())

@dp.message(Command("words"), F.chat.type == "private")
async def words_cmd(message: Message):
    words = "\n".join(f"• {w}" for w in TRIGGER_WORDS)
    await message.answer(
        "📋 الكلمات التي يرد عليها البوت داخل القروبات:\n"
        "━━━━━━━━━━━━━━\n"
        f"{words}\n\n"
        "اكتب أي كلمة منها في القروب ليظهر شرح الحاسبة."
    )

@dp.message(Command("wafi_al"))
async def admin_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "👑 أهلًا بك يا المالك\n"
        "\n"
        "وش حاب تختار:",
        reply_markup=admin_menu_keyboard(),
    )

@dp.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await callback.message.answer(compute_admin_stats())

@dp.callback_query(F.data == "admin:stars")
async def cb_admin_stars(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    balance = None
    try:
        bal = await bot.get_my_star_balance()
        balance = getattr(bal, "amount", None)
    except Exception as e:
        logger.exception("get_my_star_balance failed: %s", e)
    earned = 0
    if db is not None:
        try:
            for d in db.collection("orders").limit(10000).stream():
                earned += int(d.to_dict().get("stars", 0) or 0)
        except Exception:
            pass
    bal_line = f"{balance} ⭐" if balance is not None else "غير متاح الآن"
    await callback.message.answer(
        "⭐ رصيد النجوم\n"
        "━━━━━━━━━━━━━━\n"
        f"الرصيد الحالي: {bal_line}\n"
        f"إجمالي المكتسب من المبيعات: {earned} ⭐\n\n"
        "ℹ️ السحب/التحويل من BotFather بعد ~21 يوم من كل عملية."
    )

@dp.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(AdminFSM.broadcast)
    await callback.message.answer(
        "📢 أرسل الآن الرسالة اللي تبغى تبثها لكل المستخدمين.\n"
        "(أرسل /cancel للإلغاء)"
    )

@dp.callback_query(F.data == "admin:ban")
async def cb_admin_soon(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer("🔜 قريبًا — نضيفها بالخطوة الجاية", show_alert=True)

# ---------------- Admin: Store management ----------------
@dp.callback_query(F.data == "admin:store")
async def cb_admin_store(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await state.clear()
    await callback.message.answer("🛒 إدارة المتجر:", reply_markup=store_admin_keyboard())

@dp.callback_query(F.data == "st:addp")
async def cb_st_addp(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(AdminFSM.p_name)
    await callback.message.answer("أرسل اسم المنتج (مثال: 60 شدة):\n/cancel للإلغاء")

@dp.message(StateFilter(AdminFSM.p_name), F.from_user.id == ADMIN_ID)
async def got_p_name(message: Message, state: FSMContext):
    await state.update_data(p_name=(message.text or "").strip())
    await state.set_state(AdminFSM.p_price)
    await message.answer("أرسل السعر بالنجوم (رقم فقط، مثال: 100):")

@dp.message(StateFilter(AdminFSM.p_price), F.from_user.id == ADMIN_ID)
async def got_p_price(message: Message, state: FSMContext):
    price = parse_number(message.text or "")
    if price is None or price < 1:
        await message.answer("❌ أرسل رقمًا صحيحًا (1 فأكثر).")
        return
    data = await state.get_data()
    add_product(data.get("p_name", "منتج"), price)
    await state.clear()
    await message.answer(
        f"✅ تمت إضافة المنتج: {data.get('p_name','')} بسعر {price} ⭐",
        reply_markup=store_admin_keyboard(),
    )

@dp.callback_query(F.data == "st:addc")
async def cb_st_addc(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    prods = list_products(active_only=False)
    if not prods:
        await callback.message.answer("لا توجد منتجات. أضف منتجًا أولًا.", reply_markup=store_admin_keyboard())
        return
    kb = InlineKeyboardBuilder()
    for pid, d in prods:
        kb.button(text=f"{d.get('name','')}", callback_data=f"st:c:{pid}")
    kb.adjust(1)
    await callback.message.answer("اختر المنتج لإضافة أكواد له:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("st:c:"))
async def cb_st_pick(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    pid = callback.data.split(":", 2)[2]
    await state.set_state(AdminFSM.codes)
    await state.update_data(codes_pid=pid)
    await callback.message.answer("أرسل الأكواد — كل كود في سطر:\n/cancel للإلغاء")

@dp.message(StateFilter(AdminFSM.codes), F.from_user.id == ADMIN_ID)
async def got_codes(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("codes_pid")
    lines = [l.strip() for l in (message.text or "").splitlines() if l.strip()]
    n = add_codes(pid, lines)
    await state.clear()
    await message.answer(f"✅ تمت إضافة {n} كود للمخزون.", reply_markup=store_admin_keyboard())

@dp.callback_query(F.data == "st:list")
async def cb_st_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    prods = list_products(active_only=False)
    if not prods:
        await callback.message.answer("لا توجد منتجات بعد.", reply_markup=store_admin_keyboard())
        return
    lines = ["📋 المنتجات والمخزون:", "━━━━━━━━━━━━━━"]
    kb = InlineKeyboardBuilder()
    for pid, d in prods:
        stock = count_available_codes(pid)
        active = d.get("active", True)
        status = "✅" if active else "⛔"
        lines.append(f"{status} {d.get('name','')} — {d.get('price',0)} ⭐ — المخزون: {stock}")
        kb.button(text=f"{'⛔ تعطيل' if active else '✅ تفعيل'}: {d.get('name','')}", callback_data=f"st:t:{pid}")
    kb.adjust(1)
    await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("st:t:"))
async def cb_st_toggle(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    pid = callback.data.split(":", 2)[2]
    toggle_product(pid)
    await callback.answer("تم التحديث ✅", show_alert=False)

@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("تم الإلغاء. أرسل /start للبدء من جديد.")

@dp.callback_query(F.data == "battle_individual")
async def cb_individual(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_landing(callback.message, "individual", state)

@dp.callback_query(F.data == "battle_team")
async def cb_team(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_landing(callback.message, "team", state)

@dp.callback_query(F.data == "battle_home")
async def cb_home(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_landing(callback.message, "home", state)

@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_menu(callback.message, state)

@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await send_menu(callback.message, state)

@dp.callback_query(F.data == "hide_help")
async def cb_hide_help(callback: CallbackQuery):
    await callback.answer("تم إخفاء الشرح")
    try:
        if callback.inline_message_id:
            await bot.edit_message_text(
                inline_message_id=callback.inline_message_id,
                text="🔥 حاسبة معركة الشعبية",
            )
        elif callback.message:
            await callback.message.edit_text("🔥 حاسبة معركة الشعبية")
    except Exception as e:
        logger.exception("hide_help failed: %s", e)

@dp.callback_query(F.data.startswith("calc:"))
async def cb_calc(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    await start_calc(callback.message, mode, state)

@dp.callback_query(F.data.startswith("hist:"))
async def cb_hist(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    await show_history(callback.from_user.id, callback.message, mode)

# ---------------- Store: customer ----------------
@dp.callback_query(F.data == "shop")
async def cb_shop(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    prods = list_products(active_only=True)
    avail = [(pid, d) for pid, d in prods if count_available_codes(pid) > 0]
    if not avail:
        await callback.message.answer("🛒 لا توجد منتجات متوفرة حاليًا.")
        return
    kb = InlineKeyboardBuilder()
    for pid, d in avail:
        kb.button(text=f"{d.get('name','')} — {d.get('price',0)} ⭐", callback_data=f"buy:{pid}")
    kb.button(text="🔙 القائمة", callback_data="menu")
    kb.adjust(1)
    await callback.message.answer("🛒 اختر المنتج للشراء:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    await callback.answer()
    pid = callback.data.split(":", 1)[1]
    p = get_product(pid)
    if not p or not p.get("active", True):
        await callback.message.answer("المنتج غير متوفر حاليًا.")
        return
    if count_available_codes(pid) < 1:
        await callback.message.answer("نفد المخزون لهذا المنتج.")
        return
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=p.get("name", "شدات"),
            description=f"شحن {p.get('name','')} — تسليم فوري بعد الدفع",
            payload=f"buy:{pid}",
            currency="XTR",
            prices=[LabeledPrice(label=p.get("name", "شدات"), amount=int(p.get("price", 1)))],
        )
    except Exception as e:
        logger.exception("send_invoice failed: %s", e)
        await callback.message.answer("تعذّر إنشاء الفاتورة، حاول لاحقًا.")

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = True
    try:
        pid = q.invoice_payload.split(":", 1)[1]
        if count_available_codes(pid) < 1:
            ok = False
    except Exception:
        ok = False
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="نفد المخزون، جرّب منتجًا آخر")

@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    try:
        pid = sp.invoice_payload.split(":", 1)[1]
    except Exception:
        return
    code = pop_code(pid, message.from_user)
    if code is None:
        try:
            await bot.refund_star_payment(message.from_user.id, sp.telegram_payment_charge_id)
        except Exception as e:
            logger.exception("refund failed: %s", e)
        await message.answer("⚠️ عذرًا، نفد المخزون في آخر لحظة. تم استرجاع النجوم لك.")
        return
    p = get_product(pid) or {}
    save_order(message.from_user, pid, p.get("name", ""), code, sp.total_amount)
    await message.answer(
        "✅ تم الشراء بنجاح!\n"
        f"المنتج: {p.get('name','')}\n"
        "━━━━━━━━━━━━━━\n"
        "🔑 كودك:\n"
        f"{code}\n"
        "━━━━━━━━━━━━━━\n"
        "انسخ الكود واستخدمه. شكرًا لك 🌟"
    )

@dp.message(StateFilter(BattleFSM.my_number))
async def got_my_number(message: Message, state: FSMContext):
    n = parse_number(message.text or "")
    if n is None:
        await message.answer("❌ أرسل رقمًا صحيحًا فقط (مثال: 100000).")
        return
    data = await state.get_data()
    mode = data.get("mode", "individual")
    await state.update_data(my_number=n)
    await state.set_state(BattleFSM.opp_number)
    if mode == "team":
        await message.answer(
            f"✅ شعبيتكم: {n:,} = {points_for(n, mode)} نقطة\n\n"
            "الآن أرسل عدد شعبية تيم الخصم كامل:",
            reply_markup=cancel_keyboard(),
        )
    else:
        await message.answer(
            f"✅ شعبيتك: {n:,} = {points_for(n, mode)} نقطة\n\n"
            "الآن أرسل عدد شعبية الخصم:",
            reply_markup=cancel_keyboard(),
        )

@dp.message(StateFilter(BattleFSM.opp_number))
async def got_opp_number(message: Message, state: FSMContext):
    opp_number = parse_number(message.text or "")
    if opp_number is None:
        await message.answer("❌ أرسل رقمًا صحيحًا فقط (مثال: 60000).")
        return
    data = await state.get_data()
    mode = data.get("mode", "individual")
    my_number = data["my_number"]
    r = compute_battle(my_number, opp_number, mode)
    await message.answer(r["text"], reply_markup=result_keyboard(mode))
    save_battle(message.from_user, mode, my_number, r["my_points"], opp_number,
                r["opp_points"], r["result_label"], r["my_result"], r["opp_result"], source="calc")
    await state.clear()

# ---------------- Online (inline) ----------------
@dp.inline_query()
async def inline_battle(query: InlineQuery):
    parsed = parse_inline(query.query or "")
    if parsed is None:
        help_article = InlineQueryResultArticle(
            id="inline_help",
            title="🔥 حاسبة معركة الشعبية - الشرح",
            description="اكتب رقمين — أو أضف (فريق) قبلهم",
            input_message_content=InputTextMessageContent(message_text=help_text()),
            reply_markup=help_keyboard(),
        )
        await query.answer([help_article], cache_time=1, is_personal=True)
        return
    mode, my_number, opp_number = parsed
    r = compute_battle(my_number, opp_number, mode)
    article = InlineQueryResultArticle(
        id=f"{mode}|{my_number}|{opp_number}",
        title=f"{MODE_LABELS[mode]}: {my_number:,} ضد {opp_number:,}",
        description=f"النتيجة: {r['result_label']} — اضغط للإرسال",
        input_message_content=InputTextMessageContent(message_text=r["text"]),
    )
    await query.answer([article], cache_time=1, is_personal=True)

@dp.chosen_inline_result()
async def online_chosen(chosen: ChosenInlineResult):
    try:
        mode, my_s, opp_s = chosen.result_id.split("|")
        my_number, opp_number = int(my_s), int(opp_s)
    except Exception:
        return
    r = compute_battle(my_number, opp_number, mode)
    save_battle(chosen.from_user, mode, my_number, r["my_points"], opp_number,
                r["opp_points"], r["result_label"], r["my_result"], r["opp_result"], source="online")

# ---------------- Group handler (bot is a member) ----------------
TRIGGER_WORDS = {
    "حاسبة", "حاسبه", "نقاط",
    "معركة", "معركه",
    "معركة الشعبية", "معركه الشعبيه",
}

async def send_group_help(message: Message):
    await message.answer(help_text(), reply_markup=help_keyboard())

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def group_handler(message: Message):
    if message.from_user and message.from_user.is_bot:
        return
    text = (message.text or "").strip()
    if text in TRIGGER_WORDS:
        await send_group_help(message)
        return

# ---------------- Storage ----------------
def register_user(user):
    if db is None or user is None:
        return
    try:
        ref = db.collection("users").document(str(user.id))
        snap = ref.get()
        data = {
            "user_id": user.id,
            "username": user.username,
            "name": user.full_name,
            "last_seen": datetime.now(timezone.utc),
        }
        if not snap.exists:
            data["first_seen"] = datetime.now(timezone.utc)
            data["banned"] = False
        ref.set(data, merge=True)
    except Exception as e:
        logger.exception("register_user failed: %s", e)

def bump_counters(mode: str):
    if db is None:
        return
    try:
        db.collection("stats").document("counters").set(
            {"total": firestore.Increment(1), f"mode_{mode}": firestore.Increment(1)},
            merge=True,
        )
    except Exception as e:
        logger.exception("bump_counters failed: %s", e)

def trim_history(user_id: int, mode: str, keep: int = 3):
    if db is None:
        return
    try:
        docs = list(db.collection("battles").where("user_id", "==", user_id).limit(100).stream())
        same = [d for d in docs if d.to_dict().get("mode") == mode]
        same.sort(key=lambda d: d.to_dict().get("ts") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for d in same[keep:]:
            d.reference.delete()
    except Exception as e:
        logger.exception("trim_history failed: %s", e)

def save_battle(user, mode, my_number, my_points, opp_number, opp_points,
                result_label, my_result, opp_result, source="calc"):
    register_user(user)
    if db is None:
        return
    try:
        db.collection("battles").add({
            "user_id": user.id,
            "username": user.username,
            "name": user.full_name,
            "mode": mode,
            "source": source,
            "my_number": my_number,
            "my_points": my_points,
            "opp_number": opp_number,
            "opp_points": opp_points,
            "result": result_label,
            "my_result": my_result,
            "opp_result": opp_result,
            "ts": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.exception("Firestore save failed: %s", e)
        return
    bump_counters(mode)
    trim_history(user.id, mode, keep=3)

# ---------------- Store storage ----------------
def add_product(name: str, price: int):
    if db is None:
        return
    try:
        db.collection("products").add({
            "name": name, "price": int(price), "active": True,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.exception("add_product failed: %s", e)

def get_product(pid: str):
    if db is None:
        return None
    try:
        snap = db.collection("products").document(pid).get()
        return snap.to_dict() if snap.exists else None
    except Exception as e:
        logger.exception("get_product failed: %s", e)
        return None

def list_products(active_only: bool = True):
    if db is None:
        return []
    try:
        docs = list(db.collection("products").limit(100).stream())
        out = []
        for d in docs:
            data = d.to_dict()
            if active_only and not data.get("active", True):
                continue
            out.append((d.id, data))
        out.sort(key=lambda x: x[1].get("price", 0))
        return out
    except Exception as e:
        logger.exception("list_products failed: %s", e)
        return []

def toggle_product(pid: str):
    if db is None:
        return
    try:
        ref = db.collection("products").document(pid)
        snap = ref.get()
        if snap.exists:
            cur = snap.to_dict().get("active", True)
            ref.update({"active": not cur})
    except Exception as e:
        logger.exception("toggle_product failed: %s", e)

def add_codes(pid: str, codes: list):
    if db is None or not codes:
        return 0
    try:
        batch = db.batch()
        n = 0
        for c in codes:
            ref = db.collection("codes").document()
            batch.set(ref, {
                "product_id": pid, "code": c, "sold": False,
                "added_at": datetime.now(timezone.utc),
            })
            n += 1
        batch.commit()
        return n
    except Exception as e:
        logger.exception("add_codes failed: %s", e)
        return 0

def count_available_codes(pid: str) -> int:
    if db is None:
        return 0
    try:
        docs = list(db.collection("codes").where("product_id", "==", pid).limit(1000).stream())
        return sum(1 for d in docs if not d.to_dict().get("sold"))
    except Exception as e:
        logger.exception("count_available_codes failed: %s", e)
        return 0

def pop_code(pid: str, user):
    if db is None:
        return None
    try:
        docs = list(db.collection("codes").where("product_id", "==", pid).limit(500).stream())
        for d in docs:
            if d.to_dict().get("sold"):
                continue
            ref = d.reference
            transaction = db.transaction()

            @firestore.transactional
            def claim(tx):
                snap = ref.get(transaction=tx)
                dd = snap.to_dict()
                if not dd or dd.get("sold"):
                    return None
                tx.update(ref, {
                    "sold": True,
                    "buyer_id": user.id,
                    "sold_at": datetime.now(timezone.utc),
                })
                return dd.get("code")

            result = claim(transaction)
            if result is not None:
                return result
        return None
    except Exception as e:
        logger.exception("pop_code failed: %s", e)
        return None

def save_order(user, pid, pname, code, stars):
    if db is None:
        return
    try:
        db.collection("orders").add({
            "user_id": user.id,
            "username": user.username,
            "name": user.full_name,
            "product_id": pid,
            "product_name": pname,
            "code": code,
            "stars": stars,
            "ts": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.exception("save_order failed: %s", e)

async def show_history(user_id: int, target: Message, mode: str):
    label = MODE_LABELS.get(mode, "")
    if db is None:
        await target.answer("📜 التخزين غير مفعّل حاليًا.")
        return
    try:
        docs = list(db.collection("battles").where("user_id", "==", user_id).limit(100).stream())
        rows = [d.to_dict() for d in docs]
        rows = [r for r in rows if r.get("mode") == mode]
        rows.sort(key=lambda r: r.get("ts") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        rows = rows[:3]
        if not rows:
            await target.answer(f"📜 لا توجد نتائج في سجل {label} بعد.")
            return
        sep = "━━━━━━━━━━━━━━"
        lines = [f"📜 آخر 3 معارك - {label}:", sep]
        for i, r in enumerate(rows, 1):
            res = r.get("result", "?").replace("✅", "").replace("❌", "").replace("🤝", "").strip()
            if mode == "team":
                lines.append(
                    f"{i}- النتيجة: {res}\n"
                    f"دعم تيمي : {r.get('my_number', 0):,} ({r.get('my_points', 0)}نقطة)\n"
                    f"دعم تيم خصمي : {r.get('opp_number', 0):,} ({r.get('opp_points', 0)}نقطة)\n"
                    f"نقاط تيمي : {fmt(r.get('my_result', 0))} نقطة\n"
                    f"نقاط تيم الخصم : {fmt(r.get('opp_result', 0))} نقطة"
                )
            else:
                lines.append(
                    f"{i}- النتيجة: {res}\n"
                    f"دعمي : {r.get('my_number', 0):,} ({r.get('my_points', 0)}نقطة)\n"
                    f"دعم خصمي : {r.get('opp_number', 0):,} ({r.get('opp_points', 0)}نقطة)\n"
                    f"نقاطي : {fmt(r.get('my_result', 0))} نقطة\n"
                    f"نقاط الخصم : {fmt(r.get('opp_result', 0))} نقطة"
                )
            lines.append(sep)
        await target.answer("\n".join(lines))
    except Exception as e:
        logger.exception("history failed: %s", e)
        await target.answer("⚠️ تعذّر جلب السجل الآن.")

@dp.message(StateFilter(None), F.chat.type == "private")
async def fallback(message: Message):
    await message.answer("👋 أرسل /start لاختيار نوع المعركة.")

# ---------------- Webhook app ----------------
async def _startup_bg() -> None:
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logger.info("Bot username: @%s", BOT_USERNAME)
    except Exception as e:
        logger.exception("get_me failed: %s", e)
    try:
        await bot.set_webhook(
            WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result", "pre_checkout_query"],
        )
        logger.info("Webhook set to %s", WEBHOOK_URL)
    except Exception as e:
        logger.exception("set_webhook failed (service still running): %s", e)
    if ADMIN_ID:
        try:
            await bot.set_my_commands(
                [BotCommand(command="wafi_al", description="لوحة المالك")],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
            logger.info("Admin command registered for %s", ADMIN_ID)
        except Exception as e:
            logger.exception("set_my_commands failed: %s", e)

async def on_startup() -> None:
    asyncio.create_task(_startup_bg())

async def health(request):
    return web.Response(text="ok")

def main():
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
