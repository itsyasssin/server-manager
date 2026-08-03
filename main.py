"""
env_bot.py
──────────
Single-file aiogram 3.x Telegram bot for managing the PasarGuard Autoscaler
.env configuration file. All user-facing text (buttons, headers, messages)
is in Persian/Farsi; the underlying .env keys and values stay untouched.

Features
- Browse config by section (mirrors the .env's own comment headers)
- Edit any key's value via a simple text-prompt FSM flow
- Numeric keys are validated as integers
- PASARGUARD_NODE_CONNECTION_TYPE is edited via a rest/grpc button choice
- VALID_COUNTRIES and VALID_DATACENTERS are edited via an inline-keyboard
  checklist: tap an option to toggle a ✅/⬜ mark, then Save to write the
  updated comma-separated list back to the .env file
- PASARGUARD_HOST_INBOUND_TAG is a free-form tag list (stored as
  TAG1,TAG2,TAG3): each tag is its own button — click it to delete it
  immediately — plus a "+ Add tag" button that prompts for a new tag name
- Secret-looking keys (API keys, passwords) are masked when displayed
- Writes are done in-place, preserving comments/ordering, with a .env.bak
  backup taken before every save

Setup
    pip install aiogram python-dotenv

    export BOT_TOKEN="123456:ABC-your-telegram-bot-token"
    export BOT_ADMIN_IDS="111111111,222222222"      # your Telegram user id(s)
    export TARGET_ENV_PATH="/path/to/.env"           # the file this bot edits
    python env_bot.py

Notes
- BOT_ADMIN_IDS restricts every handler to those Telegram user IDs.
- COUNTRY_OPTIONS / DATACENTER_OPTIONS below are a starter list — edit them
  to match Doprax's actual current catalogue, since that list isn't encoded
  in the .env file itself.
"""

import asyncio
import logging
import os
import re
import shutil

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("BOT_ADMIN_IDS", "437187307,6921201032").split(",") if x.strip()}
ENV_PATH = os.environ.get("TARGET_ENV_PATH", ".env")

# Real reference data: Doprax countries and infrastructure providers.
COUNTRIES: dict[str, str] = {
    "AU": "استرالیا", "BE": "بلژیک", "BR": "برزیل", "CA": "کانادا",
    "CH": "سوئیس", "CL": "شیلی", "DE": "آلمان", "ES": "اسپانیا",
    "FI": "فنلاند", "FR": "فرانسه", "GB": "بریتانیا", "HK": "هنگ‌کنگ",
    "ID": "اندونزی", "IL": "اسرائیل", "IN": "هند", "IT": "ایتالیا",
    "JP": "ژاپن", "KR": "کره جنوبی", "MX": "مکزیک", "NL": "هلند",
    "PL": "لهستان", "QA": "قطر", "SA": "عربستان سعودی", "SE": "سوئد",
    "SG": "سنگاپور", "TW": "تایوان", "US": "ایالات متحده", "ZA": "آفریقای جنوبی",
}
PROVIDER_LABELS: dict[str, str] = {
    "digitalocean": "دیجیتال‌اوشن",
    "gcore": "جی‌کور",
    "gcp": "گوگل کلود",
    "hetzner": "هتزنر",
    "ovh": "او‌وی‌اچ",
    "provm": "پرو‌وی‌ام",
    "scaleway": "اسکیل‌وی",
    "vultr": "وولتر",
}
COUNTRY_OPTIONS = list(COUNTRIES.keys())
DATACENTER_OPTIONS = list(PROVIDER_LABELS.keys())

SECTIONS: dict[str, list[str]] = {
    "Timing": ["INTERVAL", "PING_POLL_TIMEOUT", "VM_READY_TIMEOUT", "VM_READY_POLL_INTERVAL"],
    "Thresholds": ["MINIMUM_RATING", "MINIMUM_PING"],
    "Doprax": ["DOPRAX_API_KEY", "DOPRAX_IMAGE", "MAX_BUDGET", "VALID_COUNTRIES", "VALID_DATACENTERS"],
    "Scaling": ["MIN_NODES", "MAX_CREATE_RETRIES", "PASARGUARD_HOST_INBOUND_TAG"],
    "PasarGuard Panel": ["PASARGUARD_BASE_URL", "PASARGUARD_ADMIN_USERNAME", "PASARGUARD_ADMIN_PASSWORD"],
    "PasarGuard Node Defaults": [
        "PASARGUARD_NODE_CONNECTION_TYPE", "PASARGUARD_NODE_KEEP_ALIVE",
        "PASARGUARD_NODE_CORE_CONFIG_ID", "PASARGUARD_NODE_USAGE_COEFFICIENT",
        "PASARGUARD_NODE_DATA_LIMIT", "PASARGUARD_NODE_DEFAULT_TIMEOUT",
        "PASARGUARD_NODE_INTERNAL_TIMEOUT",
    ],
    "State File": ["STATE_FILE"],
}
SECTION_NAMES = list(SECTIONS.keys())

DESCRIPTIONS: dict[str, str] = {
    "INTERVAL": "هر چند وقت یک‌بار (ثانیه) چرخه کامل بررسی اجرا شود",
    "PING_POLL_TIMEOUT": "حداکثر زمان انتظار (ثانیه) برای نتایج پینگ از check-host.net",
    "VM_READY_TIMEOUT": "حداکثر زمان انتظار (ثانیه) تا فعال‌شدن سرور جدید در داپرکس",
    "VM_READY_POLL_INTERVAL": "فاصله زمانی (ثانیه) بین بررسی‌های وضعیت سرور",
    "MINIMUM_RATING": "حداقل نرخ موفقیت پینگ قابل‌قبول (درصد)",
    "MINIMUM_PING": "حداکثر میانگین زمان پینگ قابل‌قبول (میلی‌ثانیه)",
    "DOPRAX_API_KEY": "کلید API داپرکس شما (با فرمت '<prefix>.<secret>')",
    "DOPRAX_IMAGE": "ایمیج سیستم‌عامل برای سرورهای جدید (باید مطابق کاتالوگ داپرکس باشد)",
    "MAX_BUDGET": "حداکثر بودجه ماهانه برای هر سرور (دلار)",
    "VALID_COUNTRIES": "کدهای کشور مجاز برای سرورهای جدید",
    "VALID_DATACENTERS": "کدام ارائه‌دهندگان زیرساخت برای سرورهای جدید مجاز هستند (خالی بگذارید یعنی هر ارائه‌دهنده‌ای در کشورهای مجاز)",
    "MIN_NODES": "حداقل تعداد نودهای فعالی که باید حفظ شود",
    "MAX_CREATE_RETRIES": "حداکثر تعداد تلاش هنگام ساخت/جایگزینی سرور",
    "PASARGUARD_HOST_INBOUND_TAG": "تگ inbound برای هاست‌های ساخته‌شده خودکار (خالی بگذارید تا ساخت هاست انجام نشود)",
    "PASARGUARD_BASE_URL": "آدرس پایه پنل PasarGuard",
    "PASARGUARD_ADMIN_USERNAME": "نام کاربری مدیر پنل PasarGuard",
    "PASARGUARD_ADMIN_PASSWORD": "رمز عبور مدیر پنل PasarGuard",
    "PASARGUARD_NODE_CONNECTION_TYPE": "نوع اتصال برای نودهای جدید: rest یا grpc",
    "PASARGUARD_NODE_KEEP_ALIVE": "فاصله زمانی Keep-Alive (ثانیه)",
    "PASARGUARD_NODE_CORE_CONFIG_ID": "شناسه پیکربندی هسته از پنل PasarGuard شما",
    "PASARGUARD_NODE_USAGE_COEFFICIENT": "ضریب مصرف (ضریب محاسبه ترافیک)",
    "PASARGUARD_NODE_DATA_LIMIT": "محدودیت حجم داده به بایت (۰ یعنی نامحدود)",
    "PASARGUARD_NODE_DEFAULT_TIMEOUT": "مهلت زمانی پیش‌فرض (ثانیه)",
    "PASARGUARD_NODE_INTERNAL_TIMEOUT": "مهلت زمانی داخلی (ثانیه)",
    "STATE_FILE": "مسیر فایل وضعیت اتواسکیلر (خالی بگذارید برای مسیر پیش‌فرض)",
}

# Human-friendly names shown in buttons/headers — a non-technical admin
# never needs to see the raw ENV_VAR_NAME.
KEY_LABELS: dict[str, str] = {
    "INTERVAL": "فاصله زمانی بررسی (ثانیه)",
    "PING_POLL_TIMEOUT": "مهلت زمانی پینگ (ثانیه)",
    "VM_READY_TIMEOUT": "مهلت آماده‌سازی سرور (ثانیه)",
    "VM_READY_POLL_INTERVAL": "فاصله بررسی وضعیت سرور (ثانیه)",
    "MINIMUM_RATING": "حداقل نرخ موفقیت (٪)",
    "MINIMUM_PING": "حداکثر میانگین پینگ (میلی‌ثانیه)",
    "DOPRAX_API_KEY": "کلید API داپرکس",
    "DOPRAX_IMAGE": "سیستم‌عامل سرور",
    "MAX_BUDGET": "حداکثر بودجه ماهانه هر سرور ($)",
    "VALID_COUNTRIES": "کشورهای مجاز",
    "VALID_DATACENTERS": "ارائه‌دهندگان مجاز",
    "MIN_NODES": "حداقل تعداد سرورهای فعال",
    "MAX_CREATE_RETRIES": "حداکثر تعداد تلاش برای ساخت سرور",
    "PASARGUARD_HOST_INBOUND_TAG": "تگ‌های اتصال",
    "PASARGUARD_BASE_URL": "آدرس وب‌سایت پنل",
    "PASARGUARD_ADMIN_USERNAME": "نام کاربری مدیر پنل",
    "PASARGUARD_ADMIN_PASSWORD": "رمز عبور مدیر پنل",
    "PASARGUARD_NODE_CONNECTION_TYPE": "روش اتصال سرور",
    "PASARGUARD_NODE_KEEP_ALIVE": "فاصله Keep-Alive (ثانیه)",
    "PASARGUARD_NODE_CORE_CONFIG_ID": "شناسه پیکربندی هسته",
    "PASARGUARD_NODE_USAGE_COEFFICIENT": "ضریب مصرف ترافیک",
    "PASARGUARD_NODE_DATA_LIMIT": "محدودیت حجم داده (بایت)",
    "PASARGUARD_NODE_DEFAULT_TIMEOUT": "مهلت زمانی پیش‌فرض (ثانیه)",
    "PASARGUARD_NODE_INTERNAL_TIMEOUT": "مهلت زمانی داخلی (ثانیه)",
    "STATE_FILE": "مسیر فایل ذخیره وضعیت",
}
SECTION_LABELS: dict[str, str] = {
    "Timing": "⏱ تنظیمات زمان‌بندی",
    "Thresholds": "📊 آستانه‌های عملکرد",
    "Doprax": "🖥 ارائه‌دهنده سرور (داپرکس)",
    "Scaling": "📈 مقیاس‌پذیری خودکار",
    "PasarGuard Panel": "🔑 اتصال پنل",
    "PasarGuard Node Defaults": "⚙️ تنظیمات پیش‌فرض سرور جدید",
    "State File": "💾 فایل ذخیره وضعیت",
}

SECRET_KEYS = {"DOPRAX_API_KEY", "PASARGUARD_ADMIN_PASSWORD"}
INT_KEYS = {
    "INTERVAL", "PING_POLL_TIMEOUT", "VM_READY_TIMEOUT", "VM_READY_POLL_INTERVAL",
    "MINIMUM_RATING", "MINIMUM_PING", "MAX_BUDGET", "MIN_NODES", "MAX_CREATE_RETRIES",
    "PASARGUARD_NODE_KEEP_ALIVE", "PASARGUARD_NODE_CORE_CONFIG_ID",
    "PASARGUARD_NODE_USAGE_COEFFICIENT", "PASARGUARD_NODE_DATA_LIMIT",
    "PASARGUARD_NODE_DEFAULT_TIMEOUT", "PASARGUARD_NODE_INTERNAL_TIMEOUT",
}
ENUM_KEYS = {"PASARGUARD_NODE_CONNECTION_TYPE": ["rest", "grpc"]}
TOGGLE_KEYS = {"VALID_COUNTRIES": COUNTRY_OPTIONS, "VALID_DATACENTERS": DATACENTER_OPTIONS}
# Human-friendly display text for each toggle option code.
TOGGLE_LABELS: dict[str, dict[str, str]] = {
    "VALID_COUNTRIES": COUNTRIES,
    "VALID_DATACENTERS": PROVIDER_LABELS,
}
ENUM_LABELS: dict[str, str] = {"rest": "REST", "grpc": "gRPC"}
# Free-form comma-separated lists where the admin can add/remove arbitrary
# values (no fixed option set, unlike TOGGLE_KEYS).
TAG_LIST_KEYS = {"PASARGUARD_HOST_INBOUND_TAG"}
# Tags can't contain commas (the list delimiter) or whitespace/colons
# (used as callback-data delimiters).
TAG_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("env_bot")

# ─────────────────────────────────────────────────────────────────────────
# .env read/write helpers (comment- and order-preserving)
# ─────────────────────────────────────────────────────────────────────────


def read_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def write_env_value(path: str, key: str, new_value: str) -> None:
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == key:
            lines[i] = f"{key}={new_value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={new_value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def mask(key: str, value: str) -> str:
    if key not in SECRET_KEYS:
        return value if value else "(خالی)"
    if not value:
        return "(خالی)"
    return "•" * max(len(value) - 4, 4) + value[-4:]


def section_of(key: str) -> str:
    for name, keys in SECTIONS.items():
        if key in keys:
            return name
    return ""


# ─────────────────────────────────────────────────────────────────────────
# FSM states
# ─────────────────────────────────────────────────────────────────────────


class EditValue(StatesGroup):
    waiting_for_text = State()


class AddTag(StatesGroup):
    waiting_for_tag = State()


# ─────────────────────────────────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────────────────────────────────


def kb_main_menu():
    b = InlineKeyboardBuilder()
    for i, name in enumerate(SECTION_NAMES):
        b.button(text=SECTION_LABELS.get(name, name), callback_data=f"sec:{i}")
    b.adjust(1)
    return b.as_markup()


def kb_section(section_idx: int):
    b = InlineKeyboardBuilder()
    for key in SECTIONS[SECTION_NAMES[section_idx]]:
        b.button(text=KEY_LABELS.get(key, key), callback_data=f"key:{key}")
    b.button(text="⬅️ بازگشت", callback_data="back:main")
    b.adjust(1)
    return b.as_markup()


def kb_key_detail(key: str):
    b = InlineKeyboardBuilder()
    if key in TOGGLE_KEYS:
        b.button(text="✏️ مدیریت لیست", callback_data=f"listopen:{key}")
    elif key in TAG_LIST_KEYS:
        b.button(text="🏷 مدیریت تگ‌ها", callback_data=f"tagsopen:{key}")
    elif key in ENUM_KEYS:
        for opt in ENUM_KEYS[key]:
            b.button(text=ENUM_LABELS.get(opt, opt), callback_data=f"enum:{key}:{opt}")
    else:
        b.button(text="✏️ ویرایش", callback_data=f"edit:{key}")
    sec_idx = SECTION_NAMES.index(section_of(key))
    b.button(text="⬅️ بازگشت", callback_data=f"sec:{sec_idx}")
    b.adjust(1)
    return b.as_markup()


def kb_toggle_list(key: str, selected: set[str]):
    b = InlineKeyboardBuilder()
    labels = TOGGLE_LABELS.get(key, {})
    codes_sorted = sorted(TOGGLE_KEYS[key], key=lambda c: labels.get(c, c))
    for code in codes_sorted:
        mark = "✅" if code in selected else "⬜"
        name = labels.get(code, code)
        b.button(text=f"{mark} {name}", callback_data=f"toggle:{key}:{code}")
    b.button(text="💾 ذخیره", callback_data=f"savelist:{key}")
    b.button(text="✖️ انصراف", callback_data=f"cancellist:{key}")
    # Option buttons in rows of 2, then Save/Cancel in their own row of 2.
    n_options = len(TOGGLE_KEYS[key])
    rows = [2] * ((n_options + 1) // 2) + [2]
    b.adjust(*rows)
    return b.as_markup()


def kb_tag_list(key: str, tags: list[str]):
    b = InlineKeyboardBuilder()
    for tag in tags:
        b.button(text=f"{tag}  ✕", callback_data=f"deltag:{key}:{tag}")
    b.button(text="➕ افزودن تگ", callback_data=f"addtag:{key}")
    sec_idx = SECTION_NAMES.index(section_of(key))
    b.button(text="⬅️ بازگشت", callback_data=f"sec:{sec_idx}")
    n_tags = len(tags)
    rows = [2] * ((n_tags + 1) // 2) + [1, 1]
    b.adjust(*rows)
    return b.as_markup()


# ─────────────────────────────────────────────────────────────────────────
# Router / handlers
# ─────────────────────────────────────────────────────────────────────────

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "مدیریت تنظیمات Autoscaler پاسارگارد.\nیک بخش را انتخاب کنید:",
        reply_markup=kb_main_menu(),
    )


@router.callback_query(F.data == "back:main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("یک بخش را انتخاب کنید:", reply_markup=kb_main_menu())
    await call.answer()


@router.callback_query(F.data.startswith("sec:"))
async def cb_section(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":", 1)[1])
    await state.clear()
    label = SECTION_LABELS.get(SECTION_NAMES[idx], SECTION_NAMES[idx])
    await call.message.edit_text(
        f"بخش: {label}", reply_markup=kb_section(idx)
    )
    await call.answer()


@router.callback_query(F.data.startswith("key:"))
async def cb_key_detail(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    env = read_env(ENV_PATH)
    current = mask(key, env.get(key, ""))
    desc = DESCRIPTIONS.get(key, "")
    label = KEY_LABELS.get(key, key)
    text = f"<b>{label}</b>\n{desc}\n\nمقدار فعلی: <code>{current}</code>"
    await call.message.edit_text(text, reply_markup=kb_key_detail(key), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit_start(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.update_data(edit_key=key)
    await state.set_state(EditValue.waiting_for_text)
    await call.message.edit_text(f"مقدار جدید را برای <b>{KEY_LABELS.get(key, key)}</b> ارسال کنید:", parse_mode="HTML")
    await call.answer()


@router.message(EditValue.waiting_for_text)
async def on_new_value(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["edit_key"]
    new_value = message.text.strip()
    label = KEY_LABELS.get(key, key)

    if key in INT_KEYS:
        try:
            int(new_value)
        except ValueError:
            await message.answer(f"⚠️ مقدار {label} باید عدد صحیح باشد. دوباره تلاش کنید:")
            return

    write_env_value(ENV_PATH, key, new_value)
    await state.clear()
    sec_idx = SECTION_NAMES.index(section_of(key))
    await message.answer(
        f"✅ {label} به‌روزرسانی شد.",
        reply_markup=kb_section(sec_idx),
    )


@router.callback_query(F.data.startswith("enum:"))
async def cb_enum_set(call: CallbackQuery, state: FSMContext):
    _, key, value = call.data.split(":", 2)
    write_env_value(ENV_PATH, key, value)
    env = read_env(ENV_PATH)
    label = KEY_LABELS.get(key, key)
    text = f"<b>{label}</b>\n{DESCRIPTIONS.get(key, '')}\n\nمقدار فعلی: <code>{ENUM_LABELS.get(env.get(key, ''), env.get(key, ''))}</code>"
    await call.message.edit_text(text, reply_markup=kb_key_detail(key), parse_mode="HTML")
    await call.answer("ذخیره شد.")


@router.callback_query(F.data.startswith("listopen:"))
async def cb_list_open(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    env = read_env(ENV_PATH)
    current = {c.strip() for c in env.get(key, "").split(",") if c.strip()}
    await state.update_data(**{f"draft_{key}": sorted(current)})
    label = KEY_LABELS.get(key, key)
    await call.message.edit_text(
        f"<b>{label}</b>\nبرای انتخاب/لغو انتخاب ضربه بزنید، سپس ذخیره کنید.",
        reply_markup=kb_toggle_list(key, current),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def cb_list_toggle(call: CallbackQuery, state: FSMContext):
    _, key, code = call.data.split(":", 2)
    data = await state.get_data()
    selected = set(data.get(f"draft_{key}", []))
    if code in selected:
        selected.discard(code)
    else:
        selected.add(code)
    await state.update_data(**{f"draft_{key}": sorted(selected)})
    await call.message.edit_reply_markup(reply_markup=kb_toggle_list(key, selected))
    await call.answer()


@router.callback_query(F.data.startswith("savelist:"))
async def cb_list_save(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = sorted(data.get(f"draft_{key}", []))
    write_env_value(ENV_PATH, key, ",".join(selected))
    await state.update_data(**{f"draft_{key}": None})
    sec_idx = SECTION_NAMES.index(section_of(key))
    labels = TOGGLE_LABELS.get(key, {})
    friendly = "، ".join(labels.get(c, c) for c in selected)
    await call.message.edit_text(
        f"✅ {KEY_LABELS.get(key, key)} به‌روزرسانی شد به: {friendly or '(هیچ موردی انتخاب نشده)'}",
        reply_markup=kb_section(sec_idx),
    )
    await call.answer("ذخیره شد.")


@router.callback_query(F.data.startswith("cancellist:"))
async def cb_list_cancel(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.update_data(**{f"draft_{key}": None})
    sec_idx = SECTION_NAMES.index(section_of(key))
    await call.message.edit_text("لغو شد، تغییری ذخیره نشد.", reply_markup=kb_section(sec_idx))
    await call.answer()


def _get_tags(key: str) -> list[str]:
    env = read_env(ENV_PATH)
    return [t.strip() for t in env.get(key, "").split(",") if t.strip()]


@router.callback_query(F.data.startswith("tagsopen:"))
async def cb_tags_open(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    tags = _get_tags(key)
    label = KEY_LABELS.get(key, key)
    await call.message.edit_text(
        f"<b>{label}</b>\nبرای حذف روی یک تگ ضربه بزنید، یا یک تگ جدید اضافه کنید.\n"
        f"مقدار فعلی: <code>{','.join(tags) or '(خالی)'}</code>",
        reply_markup=kb_tag_list(key, tags),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("deltag:"))
async def cb_tag_delete(call: CallbackQuery, state: FSMContext):
    _, key, tag = call.data.split(":", 2)
    tags = [t for t in _get_tags(key) if t != tag]
    write_env_value(ENV_PATH, key, ",".join(tags))
    label = KEY_LABELS.get(key, key)
    await call.message.edit_text(
        f"<b>{label}</b>\nبرای حذف روی یک تگ ضربه بزنید، یا یک تگ جدید اضافه کنید.\n"
        f"مقدار فعلی: <code>{','.join(tags) or '(خالی)'}</code>",
        reply_markup=kb_tag_list(key, tags),
        parse_mode="HTML",
    )
    await call.answer(f"{tag} حذف شد")


@router.callback_query(F.data.startswith("addtag:"))
async def cb_tag_add_start(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.update_data(tag_key=key)
    await state.set_state(AddTag.waiting_for_tag)
    await call.message.edit_text(
        f"نام تگ جدید را برای <b>{KEY_LABELS.get(key, key)}</b> ارسال کنید\n"
        f"(فقط حروف، اعداد، <code>_ . -</code> — بدون کاما یا فاصله):",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AddTag.waiting_for_tag)
async def on_new_tag(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["tag_key"]
    new_tag = message.text.strip()

    if not TAG_NAME_RE.match(new_tag):
        await message.answer(
            "⚠️ تگ نامعتبر است. فقط از حروف، اعداد، زیرخط (_)، نقطه (.) یا "
            "خط تیره (-) استفاده کنید — بدون کاما یا فاصله. دوباره امتحان کنید:"
        )
        return

    tags = _get_tags(key)
    if new_tag in tags:
        await message.answer(f"⚠️ «{new_tag}» از قبل وجود دارد. یک تگ دیگر ارسال کنید:")
        return

    tags.append(new_tag)
    write_env_value(ENV_PATH, key, ",".join(tags))
    await state.clear()
    await message.answer(
        f"✅ «{new_tag}» اضافه شد.\nمقدار ذخیره‌شده: <code>{','.join(tags)}</code>",
        reply_markup=kb_tag_list(key, tags),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────

async def main():
    if not ADMIN_IDS:
        log.warning("BOT_ADMIN_IDS is empty — no one will be able to use this bot.")

    from aiogram.client.session.aiohttp import AiohttpSession  # noqa: F401

    bot = Bot(token=BOT_TOKEN,
        session=AiohttpSession(proxy="socks5://me.computer:10809"),

    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
