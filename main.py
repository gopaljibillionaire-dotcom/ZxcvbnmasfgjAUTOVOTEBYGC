"""
Multi-Account Automation Framework - Main Telegram Interface Daemon
"""
import asyncio
import logging
import os
import sys
import json
from typing import Dict, Any, List, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError
)

import config
from database import db_mgr
from helpers import encrypt_data, decrypt_data, parse_telegram_link, dispatch_log
from tasks import task_engine

# --- SETUP ROOT LOGGING CORE ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("runtime.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("SystemCore")

bot = Bot(token=config.BOT_TOKEN)

# RAM holding pool for step-by-step OTP logins
registration_sessions: Dict[int, Dict[str, Any]] = {}

# --- STATE MACHINE DEFINITIONS ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_target = State()
    choosing_react_mode = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()

# --- RUNTIME DYNAMIC KEYBOARD BUILDERS ---
def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage Client Infrastructure", callback_data="manage_accounts")],
        [InlineKeyboardButton(text="⚡ Setup Automation Task", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 Operational Pipeline Logs", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 My Referral Matrix", callback_data="view_referrals")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Administrative Control Console", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Core Backups", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 System Performance Analytics", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def make_accounts_keyboard(user_id: int, role: str) -> InlineKeyboardMarkup:
    keyboard_layout = []
    
    if role == "super_owner":
        rows = await db_mgr.execute_read_all("SELECT phone, user_id, username FROM accounts WHERE status = 'active'")
    else:
        rows = await db_mgr.get_active_sessions(user_id, role)

    for row in rows:
        phone = row[0]
        owner_id = row[1]
        username = row[2]
        name_display = f"@{username}" if username and username != "None" else "No Username"
        ownership_ctx = f" (Owner: {owner_id})" if role == "super_owner" else ""
        
        keyboard_layout.append([
            InlineKeyboardButton(text=f"🟢 +{phone} {name_display}{ownership_ctx}", callback_data=f"info_node:{phone}")
        ])
        keyboard_layout.append([
            InlineKeyboardButton(text=f"📥 Export +{phone} .session file", callback_data=f"direct_export:{phone}")
        ])

    keyboard_layout.append([InlineKeyboardButton(text="➕ Link via OTP (Phone)", callback_data="add_account_phone")])
    keyboard_layout.append([InlineKeyboardButton(text="📥 Link via Session File", callback_data="add_account_session_file")])
    keyboard_layout.append([InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard_layout)

# --- START ROUTER REGISTRATION ---
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    role = await db_mgr.get_user_role(user_id)
    if role == "banned":
        await message.answer("🚫 You have been banned from using this system.")
        return

    referred_by = None
    args = message.text.split()
    if len(args) > 1:
        ref_payload = args[1]
        if ref_payload.startswith("ref_") and ref_payload[4:].isdigit():
            referred_by = int(ref_payload[4:])

    is_new = await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    if is_new:
        await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, "User Registered"))
        await dispatch_log(bot, f"👤 **New registration:** `{username}` (`{user_id}`) [Ref: `{referred_by}`]")

    role = await db_mgr.get_user_role(user_id)
    welcome_text = (
        f"👋 Welcome to the **Enterprise Multi-Account Automation Framework**!\n\n"
        f"👤 **Account ID:** `{user_id}`\n"
        f"🛡️ **Privilege Level:** `{role.upper()}`\n\n"
        "Deploy, coordinate, and monitor distributed infrastructure tasks safely."
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role == "banned":
        await callback.answer("🚫 Access denied.", show_alert=True)
        return
    await callback.message.edit_text(
        "👋 **Main Control Console**\nSelect an action vector below:",
        reply_markup=get_main_keyboard(role)
    )

# --- ACCOUNT INFRASTRUCTURE CONTROL INTERFACE ---
@router.callback_query(F.data == "manage_accounts")
async def list_user_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role == "banned":
        await callback.answer("🚫 Banned.", show_alert=True)
        return

    markup = await make_accounts_keyboard(user_id, role)
    await callback.message.edit_text(
        "📱 **Infrastructure Node Manager**\n\nUse the export links below each phone to download their `.session` string files instantly:",
        reply_markup=markup
    )

@router.callback_query(F.data.startswith("direct_export:"))
async def handle_direct_export(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role == "banned":
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return

    phone = callback.data.split(":")[1]
    
    if role == "super_owner":
        row = await db_mgr.execute_read_one("SELECT session_string FROM accounts WHERE phone = ?", (phone,))
    elif role in ["admin", "owner"]:
        row = await db_mgr.execute_read_one("SELECT session_string FROM accounts WHERE phone = ?", (phone,))
    else:
        row = await db_mgr.execute_read_one("SELECT session_string FROM accounts WHERE phone = ? AND user_id = ?", (phone, user_id))

    if not row:
        await callback.answer("❌ Verification Failed: Unauthorized session request.", show_alert=True)
        return

    await callback.answer("📥 Generating session file...")
    session_str = decrypt_data(row[0])
    
    file_payload = BufferedInputFile(session_str.encode('utf-8'), filename=f"+{phone}.session")
    await callback.message.reply_document(
        document=file_payload, 
        caption=f"🔑 **Secure Extraction File**\n📱 Phone: `+{phone}`\n🔒 Encrypted strictly for your system nodes."
    )

@router.callback_query(F.data.startswith("info_node:"))
async def handle_node_info_alert(callback: CallbackQuery):
    phone = callback.data.split(":")[1]
    await callback.answer(f"Selected +{phone}. Click the 'Export' button directly below it to pull its credentials.", show_alert=True)

# --- INTERACTIVE WIZARD: LINK VIA SESSION FILE ---
@router.callback_query(F.data == "add_account_session_file")
async def start_session_file_wizard(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📥 **Session File Importer Wizard**\n\n"
        "Send or forward a valid Telethon `.session` document to this chat. "
        "The bot will verify the MTProto handshake and link it instantly.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Cancel", callback_data="manage_accounts")]
        ])
    )
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file), F.document)
async def process_session_file_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not message.document.file_name.endswith(".session"):
        await message.answer("❌ Invalid format. Please upload a file ending with `.session`.")
        return

    status_msg = await message.answer("📥 **Downloading and verifying session structural integrity...**")
    file_info = await bot.get_file(message.document.file_id)
    
    temp_path = f"wizard_temp_{message.document.file_name}"
    await bot.download_file(file_info.file_path, temp_path)

    try:
        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
            session_data = f.read().strip()

        session_str = StringSession(session_data).save() if len(session_data) > 60 else session_data
        
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await status_msg.edit_text("❌ Connection failed: The uploaded session file has expired or is invalid.")
            await client.disconnect()
            return

        me = await client.get_me()
        phone = me.phone
        encrypted_session = encrypt_data(session_str)

        await db_mgr.register_session(phone, user_id, me.username, encrypted_session)
        await client.disconnect()

        await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, f"Imported session file +{phone}"))
        await dispatch_log(bot, f"📥 **Session File Imported:** +{phone} linked by ID `{user_id}`")

        await status_msg.edit_text(
            f"✅ **Import Successful!**\n\n"
            f"📱 **Phone:** `+{phone}`\n"
            f"👤 **Username:** @{me.username or 'None'}\n\n"
            f"This account has been added to your client database.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Manage Accounts", callback_data="manage_accounts")]])
        )
        await state.clear()

    except Exception as e:
        await status_msg.edit_text(f"❌ **Failed to import session file:** {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- BACKWARD COMPATIBLE LISTENER FOR LOOSE FORWARDED FILES ---
@router.message(F.document)
async def handle_loose_forwarded_session(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if not message.document.file_name.endswith(".session"):
        return

    status_msg = await message.answer("📥 **Loose Session File Detected: Validating session...**")
    file_info = await bot.get_file(message.document.file_id)
    temp_path = f"loose_temp_{message.document.file_name}"
    await bot.download_file(file_info.file_path, temp_path)

    try:
        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
            session_data = f.read().strip()

        session_str = StringSession(session_data).save() if len(session_data) > 60 else session_data
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await status_msg.edit_text("❌ Verification failed: The uploaded session file has expired or is invalid.")
            await client.disconnect()
            return

        me = await client.get_me()
        phone = me.phone
        encrypted_session = encrypt_data(session_str)

        await db_mgr.register_session(phone, user_id, me.username, encrypted_session)
        await client.disconnect()

        await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, f"Imported loose session +{phone}"))
        await dispatch_log(bot, f"📥 **Loose Session Linked:** +{phone} connected by ID `{user_id}`")
        await status_msg.edit_text(f"✅ **Import Successful!**\n📱 Phone: `+{phone}`\n👤 Username: @{me.username or 'None'} has been linked.")

    except Exception as e:
        await status_msg.edit_text(f"❌ **Failed to import forwarded session:** {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- INTERACTIVE WIZARD: LINK VIA OTP ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📞 Enter the account phone number with country prefix code (e.g. `+123456789`):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id

    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash
        }
        await message.answer("📩 **OTP Code dispatched.** Input validation sequence string below:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ Error initializing MTProto channel: {str(e)}\nUse /start to reset state machine.")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext):
    user_id = message.from_user.id
    otp = message.text.strip()
    
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Cache timeout. Reinitialize sequence with /start.")
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id)
    except SessionPasswordNeededError:
        await message.answer("🔒 Two-Factor Authentication (2FA) active. Enter account security password:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except PhoneCodeInvalidError:
        await message.answer("❌ Validation mismatch. Verify code entry parameters:")
    except Exception as e:
        await message.answer(f"❌ Handshake failed: {str(e)}")
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext):
    user_id = message.from_user.id
    password = message.text.strip()

    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session context dropped. Reinitialize framework.")
        await state.clear()
        return

    client, phone = reg_data["client"], reg_data["phone"]
    try:
        await client.sign_in(password=password)
        await complete_registration(message, state, client, phone, user_id)
    except PasswordHashInvalidError:
        await message.answer("❌ Password mismatch validation failed. Re-enter 2FA phrase:")
    except Exception as e:
        await message.answer(f"❌ Auth Exception: {str(e)}")
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int):
    try:
        me = await client.get_me()
        session_str = client.session.save()
        encrypted_session = encrypt_data(session_str)

        await db_mgr.register_session(phone, user_id, me.username, encrypted_session)
        await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, f"Linked account session +{phone}"))
        await dispatch_log(bot, f"📱 **New account linked via OTP:** +{phone} (ID: `{user_id}`)")

        await message.answer(f"🎉 Channel Verified! Account `+{phone}` (@{me.username or 'N/A'}) is active in the cluster.")
        
        clean_phone = phone.replace("+", "").strip()
        session_bytes = session_str.encode('utf-8')
        session_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
        
        caption_text = (
            f"🔑 **New Session File String Extracted**\n"
            f"📱 **Phone:** `+{clean_phone}`\n"
            f"👤 **Username:** @{me.username or 'N/A'}\n"
            f"🆔 **Linked By User ID:** `{user_id}`"
        )
        
        try:
            await message.answer_document(document=session_file, caption=caption_text)
        except Exception as e:
            logger.error(f"Failed sending session file copy to linking user: {e}")
            
        for owner_id in config.SUPER_OWNER_IDS:
            try:
                owner_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
                await message.bot.send_document(chat_id=owner_id, document=owner_file, caption=caption_text)
            except Exception as owner_err:
                logger.error(f"Could not forward session data packet to superowner {owner_id}: {owner_err}")

    except Exception as e:
        await message.answer(f"❌ Database registration failure: {str(e)}")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- ADMINISTRATIVE CMDS ---
@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied: Insufficient authorization profiles.")
        return

    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("ℹ️ **Usage:** `/addadmin <target_user_id> <max_account_limit>`\nExample: `/addadmin 987654321 50`")
        return

    target_id, limit = int(args[1]), int(args[2])
    
    row = await db_mgr.execute_read_one("SELECT 1 FROM users WHERE user_id = ?", (target_id,))
    if not row:
        await db_mgr.execute_write(
            "INSERT INTO users (user_id, username, role, max_accounts) VALUES (?, 'Admin Profile', 'admin', ?)", 
            (target_id, limit)
        )
    else:
        await db_mgr.execute_write("UPDATE users SET role = 'admin', max_accounts = ? WHERE user_id = ?", (limit, target_id))

    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (message.from_user.id, f"Added admin {target_id}"))
    await dispatch_log(bot, f"👑 **Privilege escalation:** `{target_id}` set to Admin (Max: `{limit}`).")
    await message.answer(f"✅ Success! User ID `{target_id}` is now registered as an **Admin** with access limits set to `{limit}`.")

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ **Usage:** `/removeadmin <target_user_id>`")
        return

    target_id = int(args[1])
    target_role = await db_mgr.get_user_role(target_id)
    if target_role == "super_owner":
        await message.answer("❌ Security Violation: Super Owners cannot be demoted.")
        return

    await db_mgr.execute_write("UPDATE users SET role = 'user', max_accounts = 5 WHERE user_id = ?", (target_id,))
    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (message.from_user.id, f"Removed admin {target_id}"))
    await dispatch_log(bot, f" Demoted admin `{target_id}` back to regular user status.")
    await message.answer(f"✅ User ID `{target_id}` has been stripped of Admin access privileges.")

@router.message(Command("banuser"))
async def cmd_ban_user(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ **Usage:** `/banuser <target_user_id>`")
        return

    target_id = int(args[1])
    target_role = await db_mgr.get_user_role(target_id)
    if target_role in ["owner", "super_owner"]:
        await message.answer("❌ System Error: You cannot ban administrative owners.")
        return

    await db_mgr.execute_write("UPDATE users SET status = 'banned' WHERE user_id = ?", (target_id,))
    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (message.from_user.id, f"Banned user {target_id}"))
    await dispatch_log(bot, f"🔨 **Ban issued:** User `{target_id}` has been banned.")
    await message.answer(f"✅ User ID `{target_id}` has been successfully banned from using the system.")

@router.message(Command("unbanuser"))
async def cmd_unban_user(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ **Usage:** `/unbanuser <target_user_id>`")
        return

    target_id = int(args[1])
    await db_mgr.execute_write("UPDATE users SET status = 'active' WHERE user_id = ?", (target_id,))
    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (message.from_user.id, f"Unbanned user {target_id}"))
    await dispatch_log(bot, f"😇 **Ban lifted:** User `{target_id}` has been unbanned.")
    await message.answer(f"✅ User ID `{target_id}` status set back to active.")

@router.message(Command("deletenumber"))
async def cmd_delete_number(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("ℹ️ **Usage:** `/deletenumber <phone_number_without_plus>`\nExample: `/deletenumber 1234567890`")
        return

    phone = args[1].replace("+", "").strip()
    success = await db_mgr.delete_account(phone)
    if not success:
        await message.answer("❌ This phone number does not exist inside our active nodes.")
        return

    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (message.from_user.id, f"Deleted number +{phone}"))
    await dispatch_log(bot, f"🗑️ **Account deleted:** +{phone} unlinked.")
    await message.answer(f"✅ Bridge node account `+{phone}` has been successfully unlinked and wiped.")

@router.message(Command("deleteuser"))
async def cmd_delete_user(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ **Usage:** `/deleteuser <user_id>`")
        return

    target_id = int(args[1])
    target_role = await db_mgr.get_user_role(target_id)
    if target_role == "super_owner":
        await message.answer("❌ Exception: Cannot delete Super Owners.")
        return

    await db_mgr.execute_write("DELETE FROM accounts WHERE user_id = ?", (target_id,))
    await db_mgr.execute_write("DELETE FROM tasks WHERE creator_id = ?", (target_id,))
    await db_mgr.execute_write("DELETE FROM users WHERE user_id = ?", (target_id,))
    
    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (message.from_user.id, f"Wiped user data of {target_id}"))
    await dispatch_log(bot, f"🔥 **Wiped user database record:** {target_id}")
    await message.answer(f"✅ User ID `{target_id}` and all associated nodes/operations have been deleted.")

@router.message(Command("export_session"))
async def cmd_export_session(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("ℹ️ **Usage:** `/export_session <phone_number_without_plus>`")
        return

    phone = args[1].replace("+", "").strip()
    row = await db_mgr.execute_read_one("SELECT session_string FROM accounts WHERE phone = ?", (phone,))
    if not row:
        await message.answer("❌ No record of this active connection found.")
        return

    session_str = decrypt_data(row[0])
    file_data = session_str.encode('utf-8')
    file_payload = BufferedInputFile(file_data, filename=f"+{phone}.session")
    
    await message.reply_document(file_payload, caption=f"🔑 Session extraction file for `+{phone}`.")

# --- TASKS BUILDER INTERFACE ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = [
        [InlineKeyboardButton(text="📢 Join Channel (Pub/Priv)", callback_data="set_type:join")],
        [InlineKeyboardButton(text="💨 Leave Channel", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="🎭 Add Reaction Expression", callback_data="set_type:react")],
        [InlineKeyboardButton(text="🔘 Press Inline Markup Button", callback_data="set_type:button_vote")],
        [InlineKeyboardButton(text="✉️ Send Direct Message (DM)", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")]
    ]
    await callback.message.edit_text(
        "⚡ **Automation Task Configurator Wizard**\nSelect the type of action you wish to deploy:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    if task_type in ["react", "button_vote"]:
        await callback.message.edit_text(
            "🔗 **Provide Message Link:**\nPaste the link pointing to the specific post (e.g., `https://t.me/c/12345678/2` or `https://t.me/channel/2`):"
        )
    else:
        await callback.message.edit_text(
            "🔗 **Enter Target Resource:**\nProvide the Username, Public Link, or Private Join Link to scan:"
        )
    await state.set_state(TaskWizardStates.waiting_for_target)

@router.message(StateFilter(TaskWizardStates.waiting_for_target))
async def task_hub_process_target(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if task_type in ["join", "leave"]:
        await finalize_task_creation(message, state)
    elif task_type == "react":
        buttons = [
            [InlineKeyboardButton(text="🎭 Choose Specific Emojis", callback_data="react_mode:standard")],
            [InlineKeyboardButton(text="⚡ Sync Existing Reactions On Post", callback_data="react_mode:existing_reactions")],
            [InlineKeyboardButton(text="🎲 Distribute Random Reactions", callback_data="react_mode:random")]
        ]
        await message.answer("⚙️ **Select Reaction Strategy Mode:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await state.set_state(TaskWizardStates.choosing_react_mode)
    elif task_type == "button_vote":
        await message.answer("🔘 **Enter Button Text/Emoji:**\nType the exact emoji or text label you want to click (e.g., `👍`):")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    elif task_type == "dm":
        await message.answer("📝 **Message Body Content:** Enter the text string to dispatch to the target:")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

@router.callback_query(StateFilter(TaskWizardStates.choosing_react_mode), F.data.startswith("react_mode:"))
async def process_react_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    await state.update_data(react_mode=mode)
    
    if mode == "standard":
        await state.update_data(selected_emojis=[])
        await callback.message.edit_text(
            "🎭 **Choose Emojis to Distribute:**\nSelect reactions:",
            reply_markup=get_emoji_selection_keyboard([])
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    else:
        await callback.message.delete()
        await finalize_task_creation(callback.message, state)

def get_emoji_selection_keyboard(selected_emojis: List[str]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in config.REACTION_EMOJIS:
        is_selected = emoji in selected_emojis
        btn_text = f"{emoji} ✅" if is_selected else emoji
        row.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_emoji:{emoji}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="✅ Complete Selection", callback_data="finish_emoji_selection")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data.startswith("toggle_emoji:"))
async def handle_toggle_emoji(callback: CallbackQuery, state: FSMContext):
    emoji = callback.data.split(":")[1]
    data = await state.get_data()
    selected_emojis = data.get("selected_emojis", [])

    if emoji in selected_emojis:
        selected_emojis.remove(emoji)
    else:
        selected_emojis.append(emoji)

    await state.update_data(selected_emojis=selected_emojis)
    await callback.message.edit_reply_markup(reply_markup=get_emoji_selection_keyboard(selected_emojis))
    await callback.answer()

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data == "finish_emoji_selection")
async def finish_emoji_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_emojis = data.get("selected_emojis", [])
    if not selected_emojis:
        await callback.answer("⚠️ Select at least one emoji!", show_alert=True)
        return
    await state.update_data(reactions=selected_emojis)
    await callback.message.delete()
    await finalize_task_creation(callback.message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip())
    await finalize_task_creation(message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text.strip())
    await finalize_task_creation(message, state)

async def finalize_task_creation(message: Message, state: FSMContext):
    data = await state.get_data()
    
    # Context-agnostic extractors for runtime compatibility between message types
    if isinstance(message, Message):
        user_id = message.chat.id
        username = message.from_user.username or "Unknown"
    else:
        user_id = message.from_user.id
        username = message.from_user.username or "Unknown"
        
    task_type = data.pop("task_type")
    target = data.get("target", "")
    parsed_target, link_msg_id, is_private_hash = parse_telegram_link(target)
    if link_msg_id:
        data["msg_id"] = link_msg_id

    # Create task entries inside core architecture structures
    task_id = await db_mgr.create_task(user_id, task_type, data)
    await task_engine.add_task(task_id, user_id, task_type, data)

    await db_mgr.execute_write("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, f"Queued task #{task_id}"))
    
    # Frame extra parameters dynamically depending on operation types without touching DB schemas
    extra_details = ""
    if task_type == "react":
        mode = data.get("react_mode", "standard")
        emojis = ", ".join(data.get("reactions", [])) if mode == "standard" else "N/A"
        extra_details = f"\n▪️ **Strategy Mode:** `{mode}`\n▪️ **Target Emojis:** `{emojis}`"
    elif task_type == "button_vote":
        extra_details = f"\n▪️ **Target Button Label:** `{data.get('button_text', 'N/A')}`"
    elif task_type == "dm":
        extra_details = f"\n▪️ **Message Contents:** `{data.get('text', 'N/A')}`"

    # Forward comprehensive tactical deployment log directly to the channel via dispatch_log
    channel_log_payload = (
        f"⚡ **Automation Pipeline Initialized**\n"
        f"▪️ **Task Ref Reference:** `#{task_id}`\n"
        f"▪️ **Action Vector:** `{task_type.upper()}`\n"
        f"▪️ **Target Channel/Resource:** `{target}`\n"
        f"▪️ **Triggered By Actor:** @{username} (`{user_id}`)"
        f"{extra_details}"
    )
    await dispatch_log(bot, channel_log_payload)

    response = f"🚀 **Task #{task_id} successfully queued!**\nWorkers are executing operations. Reports: `/taskreport_{task_id}`"
    if isinstance(message, Message):
        await message.answer(response)
    else:
        await message.answer(response)
    await state.clear()

# --- SYSTEM TELEMETRY VIEWS ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role in ["super_owner", "admin", "owner"]:
        rows = await db_mgr.execute_read_all("SELECT task_id, type, status, progress, creator_id FROM tasks ORDER BY task_id DESC LIMIT 15")
    else:
        rows = await db_mgr.execute_read_all("SELECT task_id, type, status, progress, creator_id FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10", (user_id,))

    text = "📊 **Operations Pipeline Log Engine**\n\n"
    if not rows:
        text += "_Queue completely empty._"
    else:
        for row in rows:
            owner_ctx = f" | User: `{row[4]}`" if role == "super_owner" else ""
            text += f"🔹 **Task #{row[0]}** ({row[1].upper()}){owner_ctx}\nStatus: `{row[2]}` | Metrics: `{row[3]}`\n↳ /taskreport_{row[0]}\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    try:
        task_id = int(message.text.split("_")[1])
    except:
        return

    row = await db_mgr.execute_read_one(
        "SELECT creator_id, type, status, progress, success_report, failure_report FROM tasks WHERE task_id = ?", 
        (task_id,)
    )
    if not row:
        await message.answer("❌ Task manifest index not found inside storage.")
        return

    creator_id, task_type, status, progress, success_rep, failure_rep = row
    if role != "super_owner" and creator_id != user_id:
        await message.answer("🚫 Unauthorized profile request.")
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    prev_row = await db_mgr.execute_read_one("SELECT task_id FROM tasks WHERE task_id < ? ORDER BY task_id DESC LIMIT 1", (task_id,))
    prev_id_str = f"#{prev_row[0]}" if prev_row else "None"

    report_text = (
        f"📊 **Manifest Diagnostics Report for Task #{task_id}**\n\n"
        f"⚙️ **Type:** `{task_type.upper()}`\n"
        f"🚦 **Current State:** `{status.upper()}`\n"
        f"⏮️ **Preceding Task Pointer:** `{prev_id_str}`\n"
        f"📈 **Progress Metric:** `{progress}`\n\n"
        f"🟢 **Success Count:** `{len(passed_list)}` worker nodes\n"
        f"🔴 **Failure Count:** `{len(failed_list)}` worker nodes"
    )
    
    buttons = []
    if status in ["pending", "running", "queued"]:
        buttons.append([InlineKeyboardButton(text="🛑 Force Terminate Pipeline", callback_data=f"abort_task:{task_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")])
    
    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("abort_task:"))
async def handle_abort_task(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    task_id = int(callback.data.split(":")[1])
    
    row = await db_mgr.execute_read_one("SELECT creator_id, status FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        await callback.answer("Task not found.", show_alert=True)
        return
        
    creator_id, current_status = row
    if role != "super_owner" and creator_id != user_id:
        await callback.answer("🚫 Unauthorized access profile.", show_alert=True)
        return
        
    if current_status in ["completed", "failed", "cancelled"]:
        await callback.answer("⚠️ Task pipeline has already concluded.", show_alert=True)
        return

    await db_mgr.execute_write("UPDATE tasks SET status = 'cancelled' WHERE task_id = ?", (task_id,))
    await task_engine.cancel_task_memory(task_id)
    
    await callback.message.edit_text(
        f"🛑 **Termination Signal Distributed!**\nTask pipeline `#{task_id}` has been marked as **CANCELLED**.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")]])
    )
    
    await dispatch_log(
        bot, 
        f"🛑 **Manual Abort Intercept Executed:**\n"
        f"▪️ **Task Context:** `#{task_id}`\n"
        f"▪️ **Intercepted By User:** `{user_id}`\n"
        f"▪️ **State Interrupted:** `{current_status}`"
    )

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    
    row = await db_mgr.execute_read_one("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
    count = row[0] if row else 0
    
    await callback.message.edit_text(
        f"👥 **Referrals Matrix**\n\nInvite link:\n`{ref_link}`\n\nTotal referred: `{count}`", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery):
    await callback.message.edit_text(
        "🛠️ **Administrative Control Console**\n\n"
        "• `/addadmin <id> <limit>`\n• `/removeadmin <id>`\n• `/banuser <id>`\n"
        "• `/unbanuser <id>`\n• `/deletenumber <phone>`\n• `/deleteuser <id>`\n"
        "• `/export_session <phone>`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery):
    await callback.message.edit_text("💾 **Core Database Exporter**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Download DB", callback_data="export_db")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]))

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery):
    try:
        with open(db_mgr.db_path, "rb") as f:
            file_data = f.read()
        await callback.message.reply_document(BufferedInputFile(file_data, filename="core.db"))
    except Exception as e:
        await callback.answer(f"Error exporting database: {e}")

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery):
    row_users = await db_mgr.execute_read_one("SELECT COUNT(*) FROM users")
    row_accs = await db_mgr.execute_read_one("SELECT COUNT(*) FROM accounts")
    
    users = row_users[0] if row_users else 0
    accs = row_accs[0] if row_accs else 0
    
    await callback.message.edit_text(
        f"📊 **Telemetry Diagnostics**\n\nUsers registered: `{users}`\nAccounts linked: `{accs}`", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

# --- STARTUP HANDSHAKE CHANNELS ---
async def verify_saved_sessions():
    logger.info("Performing MTProto authentication checks on all linked bridge sessions...")
    rows = await db_mgr.execute_read_all("SELECT phone, session_string FROM accounts WHERE status = 'active'")
    for phone, enc_session in rows:
        try:
            session_str = decrypt_data(enc_session)
            client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await db_mgr.update_account_status(phone, "dead")
                logger.warning(f"Session +{phone} was flagged as dead during verification.")
            await client.disconnect()
        except Exception as e:
            logger.error(f"Error checking verification status of +{phone}: {e}")

async def main():
    await db_mgr.init()
    await verify_saved_sessions()

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    worker_task = asyncio.create_task(task_engine.start_worker(1))
    
    await dispatch_log(bot, "🚀 **Multi-Account Automation System Core Online.** Operational loops active.")

    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("System shutting down smoothly.")
