import asyncio
import base64
import json
import os
import re
import random
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

# Import local configurations
import config
from config import logger

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
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int]]:
    link = link.strip()
    if not link:
        return None, None
        
    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return channel_id, msg_id

    if "+ " in link or "/+" in link or "joinchat/" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        return (hash_match.group(1) if hash_match else link, None)
        
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match:
        return msg_match.group(1), int(msg_match.group(2))
        
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = int(parts[1])
            return target, msg_id
            
    return target, None

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

    async def log_action(self, user_id: int, action: str, bot_instance: Optional[Bot] = None, force_send: bool = False):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
                await db.commit()
        except Exception as db_err:
            logger.error(f"Failed to log action to local DB: {db_err}")
        
        # Reduced logging clutter: Only alerts channel logs for critical additions/terminations
        if force_send and bot_instance and config.LOG_CHANNEL_ID:
            try:
                log_text = (
                    f"📝 **System Audit Log**\n"
                    f"👤 **User ID:** `{user_id}`\n"
                    f"⚡ **Action:** {action}"
                )
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=log_text)
            except Exception as e:
                logger.error(f"Failed sending log channel metric updates: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in config.SUPER_OWNER_IDS:
            return "super_owner"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else "user"

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                if not await cursor.fetchone():
                    await db.execute(
                        "INSERT INTO users (user_id, username, role, referred_by) VALUES (?, ?, 'user', ?)",
                        (user_id, username, referred_by)
                    )
                    await db.commit()

db_mgr = Database()
registration_sessions: Dict[int, Dict[str, Any]] = {}
bot_username: str = "bot"

# --- ANTI-BAN TASK MANAGER ENGINE ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance))

    def clear_pending_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def cancel_all_active_tasks(self) -> int:
        count = 0
        self.clear_pending_queue()
        active_ids = list(self.current_tasks.keys())
        for t_id in active_ids:
            loop_task = self.current_tasks.get(t_id)
            if loop_task and not loop_task.done():
                loop_task.cancel()
                count += 1
                async with aiosqlite.connect(db_mgr.db_path) as db:
                    await db.execute(
                        "UPDATE tasks SET status = 'cancelled', progress = 'Terminated via Panic Command' WHERE task_id = ?", 
                        (t_id,)
                    )
                    await db.commit()
        return count

    async def start_worker(self):
        logger.info("Anti-Ban Task pipeline processing loop started.")
        while True:
            try:
                task_id, creator_id, task_type, payload, bot_instance = await self.queue.get()
            except asyncio.CancelledError:
                break
                
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except asyncio.CancelledError:
                logger.warning(f"Task #{task_id} execution was forcefully cancelled.")
            except Exception as e:
                logger.error(f"Execution failure on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
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

        # REDUCED COOLDOWN TUNING (High Speed + Anti-Ban Matrix Optimization)
        BATCH_SIZE = 10               
        BASE_COOLDOWN = 2           

        for index, (phone, enc_session) in enumerate(clients_data):
            client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                    failed_ids.append((phone, "Unauthorized/Session Expired"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id = parse_telegram_link(target)
                msg_id = int(payload.get("msg_id", link_msg_id or 0))

                # COMPONENT OPERATIONS RESOLVER FOR MULTI-ACTION ROUTINES
                do_react = "react" in task_type
                do_vote = "vote" in task_type
                do_view = "views" in task_type or "view" in task_type

                if task_type == "join":
                    if isinstance(parsed_target, str) and ("/+" in target or "joinchat/" in target or target.startswith("+")):
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                        
                elif task_type == "leave":
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))

                elif task_type == "dm":
                    message_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, message_text)

                elif task_type == "start_bot":
                    bot_username_target = str(parsed_target) if parsed_target else target
                    bot_username_target = bot_username_target.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
                    start_param = None
                    if "start=" in target:
                        param_match = re.search(r'start=([^&\s]+)', target)
                        if param_match:
                            start_param = param_match.group(1)
                    if "?" in bot_username_target:
                        bot_username_target = bot_username_target.split("?")[0]
                    await client.send_message(bot_username_target, f"/start {start_param}" if start_param else "/start")

                # PIPELINED MULTI-ACTION HANDLING
                if do_view:
                    if msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(peer=parsed_target, id=[msg_id], increment=True))
                    else:
                        await client.get_messages(parsed_target, limit=5)

                if do_react:
                    emojis = payload.get("reactions", ["👍"])
                    assigned_emoji = emojis[index % len(emojis)]
                    await client(functions.messages.SendReactionRequest(
                        peer=parsed_target, msg_id=msg_id, reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                    ))

                if do_vote:
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
                                peer=parsed_target, msg_id=msg_id, data=target_button.data
                            ))

                passed_ids.append(phone)
                
            except FloodWaitError as fwe:
                failed_ids.append((phone, f"FloodWaitError: {fwe.seconds}s"))
                await asyncio.sleep(min(fwe.seconds, 5))
            except Exception as e:
                failed_ids.append((phone, str(e)))
            finally:
                await client.disconnect()

            # High-speed backoff delays
            if (index + 1) % BATCH_SIZE == 0 and (index + 1) < total_accounts:
                await asyncio.sleep(BASE_COOLDOWN)
            else:
                await asyncio.sleep(0.15)

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

        # Final analytical summaries are pushed cleanly to your log terminal
        await db_mgr.log_action(
            creator_id, 
            f"Finished executing task #{task_id} ({task_type.upper()}). Passed: {len(passed_ids)}, Failed: {len(failed_ids)}", 
            bot_instance,
            force_send=True
        )

task_queue = TaskQueue()

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

# --- UI KEYBOARD GENERATORS ---
REACTION_EMOJIS = ["👍", "👎", "🔥", "🎉", "👏", "🥰", "👀", "💯", "🤩"]

def get_emoji_selection_keyboard(selected_emojis: List[str]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in REACTION_EMOJIS:
        btn_text = f"{emoji} ✅" if emoji in selected_emojis else emoji
        row.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_emoji:{emoji}"))
        if len(row) == 3:  
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="✅ Done Selecting Emojis", callback_data="finish_emoji_selection")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton(text="⚡ Do Tasks (Adv Campaign)", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 Tasks Report", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 My Referral Matrix", callback_data="view_referrals")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Administrative Control Console", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="React Only", callback_data="set_type:react"), InlineKeyboardButton(text="Vote Only", callback_data="set_type:button_vote")],
        [InlineKeyboardButton(text="React + Vote", callback_data="set_type:react_vote"), InlineKeyboardButton(text="View Only", callback_data="set_type:views")],
        [InlineKeyboardButton(text="React + View", callback_data="set_type:react_view"), InlineKeyboardButton(text="Vote + View", callback_data="set_type:vote_view")],
        [InlineKeyboardButton(text="React + Vote + View", callback_data="set_type:react_vote_view")],
        [InlineKeyboardButton(text="Join Channel", callback_data="set_type:join"), InlineKeyboardButton(text="Leave Channel", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="Bulk DM", callback_data="set_type:dm"), InlineKeyboardButton(text="Start Bot", callback_data="set_type:start_bot")],
        [InlineKeyboardButton(text="Cancel", callback_data="main_menu")]
    ])

# --- ROUTER MODULES ---
router = Router()

@router.message(Command("start"))
@router.message(Command("owner"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    await db_mgr.create_user_if_not_exists(user_id, username)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        active_count = (await cursor.fetchone())[0]

    welcome_text = (
        f"🚀 **Adv Campaign**\n"
        f"-----------------------------------------\n\n"
        f"📱 `{active_count} active account(s) available.`\n\n"
        f"**Step 1** — _Choose what your accounts should do below:_"
    )
    await message.answer(welcome_text, reply_markup=get_task_types_keyboard())

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text(
        "👋 **Main Control Console**\nSelect an action vector below:",
        reply_markup=get_main_keyboard(role)
    )

# --- ACCOUNT INFRASTRUCTURE DEPLOYMENT ---
@router.callback_query(F.data == "manage_accounts")
async def list_user_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer() 
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT phone, status FROM accounts")
        else:
            cursor = await db.execute("SELECT phone, status FROM accounts WHERE user_id = ?", (user_id,))
        rows = await cursor.fetchall()

    text = "📱 **Operational Channels Infrastructure**\n\n"
    if not rows:
        text += "_No automation profiles currently linked._"
    else:
        for row in rows:
            icon = "🟢" if row[1] == "active" else "🔴"
            text += f"{icon} `+{row[0]}` - **{row[1].upper()}**\n"

    buttons = [
        [InlineKeyboardButton(text="📁 Link String Session File", callback_data="add_account_session")],
        [InlineKeyboardButton(text="💥 Purge Dead Accounts", callback_data="purge_dead_accounts")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "purge_dead_accounts")
async def handle_purge_dead_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            await db.execute("DELETE FROM accounts WHERE status = 'dead'")
        else:
            await db.execute("DELETE FROM accounts WHERE status = 'dead' AND user_id = ?", (user_id,))
        await db.commit()
    await callback.answer("🔥 Successfully purged dead sessions!", show_alert=True)
    await list_user_accounts(callback, bot)

# --- DEPLOY SESSION STRINGS DIRECTLY ---
@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📁 Upload a `.txt`/`.session` string file or paste your pure Telethon string token straight into chat:")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    session_str = ""

    if message.document:
        file_info = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        session_str = file_bytes.read().decode('utf-8', errors='ignore').strip()
    elif message.text:
        session_str = message.text.strip()

    if not session_str or len(session_str) < 20:
        await message.answer("❌ Invalid session token data format.")
        await state.clear()
        return

    try:
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await message.answer("❌ Session expired or revoked.")
            await client.disconnect()
            await state.clear()
            return
            
        me = await client.get_me()
        phone = me.phone or f"custom_{me.id}"
        encrypted_session = encrypt_data(session_str)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", ""), user_id, me.username or "None", encrypted_session))
            await db.commit()

        # Channel logs will cleanly alert that a session string addition event occurred
        await db_mgr.log_action(user_id, f"➕ Added new active session for user Profile: +{phone}", bot, force_send=True)
        await message.answer(f"🎉 Integration complete for user profile: `+{phone}`!")
        await client.disconnect()
    except Exception as e:
        await message.answer(f"❌ Integration failure: {e}")
    finally:
        await state.clear()

# --- TASK WIZARD INTERFACE ORCHESTRATOR ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("⚡ **Select execution module:**", reply_markup=get_task_types_keyboard())
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    await callback.message.edit_text("🔗 **Provide Target Link / Chat Username:**")
    await state.set_state(TaskWizardStates.waiting_for_target)

@router.message(StateFilter(TaskWizardStates.waiting_for_target))
async def task_hub_process_target(message: Message, state: FSMContext, bot: Bot):
    target = message.text.strip()
    await state.update_data(target=target)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if "react" in task_type:
        await state.update_data(selected_emojis=[])
        await message.answer("🎭 **Select Reaction Expression Emojis:**", reply_markup=get_emoji_selection_keyboard([]))
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        await message.answer("🔘 **Enter Button text or matching emoji parameter:**")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    elif task_type == "dm":
        await message.answer("📝 **Message Body Content:**")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)
    else:
        await finalize_task_creation(message, state, bot)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data.startswith("toggle_emoji:"))
async def handle_toggle_emoji(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    emoji = callback.data.split(":")[1]
    data = await state.get_data()
    selected_emojis = data.get("selected_emojis", [])

    if emoji in selected_emojis:
        selected_emojis.remove(emoji)
    else:
        selected_emojis.append(emoji)

    await state.update_data(selected_emojis=selected_emojis)
    await callback.message.edit_reply_markup(reply_markup=get_emoji_selection_keyboard(selected_emojis))

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data == "finish_emoji_selection")
async def finish_emoji_selection(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("selected_emojis"):
        await callback.answer("⚠️ Select at least one emoji!", show_alert=True)
        return
    await callback.answer()
    await state.update_data(reactions=data.get("selected_emojis"))
    
    task_type = data.get("task_type")
    if "vote" in task_type:
        await callback.message.answer("🔘 **Enter Button text or matching emoji parameter:**")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    else:
        await callback.message.delete()
        await finalize_task_creation(callback.message, state, bot)

@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(button_text=message.text.strip())
    await finalize_task_creation(message, state, bot)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(text=message.text.strip())
    await finalize_task_creation(message, state, bot)

async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    
    target = data.get("target", "")
    parsed_target, link_msg_id = parse_telegram_link(target)
    if link_msg_id:
        data["msg_id"] = link_msg_id

    payload_json = json.dumps(data)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)", (user_id, task_type, payload_json))
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot)
    
    response_msg = f"🚀 **Task #{task_id} successfully queued!**\nWorkers are executing operations. Use `/taskreport_{task_id}` to pull state data."
    if isinstance(message, Message):
        await message.answer(response_msg)
    else:
        await message.answer(response_msg)
    await state.clear()

# --- METRIC REPORTS SYSTEM ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
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

    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    try:
        task_id = int(message.text.split("_")[1])
    except (IndexError, ValueError):
        await message.answer("❌ Invalid format.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress, success_report, failure_report FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("❌ Task matrix records match empty.")
        return

    creator_id, task_type, status, progress, success_rep, failure_rep = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await message.answer("🚫 Access denied.")
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    report_text = (
        f"📊 **Manifest Diagnostics Report for Task #{task_id}**\n"
        f"⚙️ **Type:** `{task_type.upper()}`\n"
        f"🚦 **State:** `{status}` | `{progress}`\n\n"
        f"🟢 **Successful Actions:** `{len(passed_list)}` accounts\n"
        f"🔴 **Failed Actions:** `{len(failed_list)}` accounts\n"
    )
    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")]]))

# --- REFERRAL NETWORK MATRIX ---
@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]

    text = f"👥 **Referral Network Matrix**\n\n🔗 **Your Direct Invite link:**\n`{ref_link}`\n\n📈 **Nodes across matrix:** `{count}` users."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

# --- ADMINISTRATIVE CONTROL FOR OVERSEERS ---
@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await callback.message.answer("🚫 Access Denied.")
        return
    await callback.message.edit_text(
        "🛠️ **Administrative Control Console**\n\n"
        "🔹 `/canceltasks` (Global Kill Pipeline Switch)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message, bot: Bot):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        return
    killed_count = await task_queue.cancel_all_active_tasks()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE tasks SET status = 'cancelled' WHERE status = 'pending' OR status = 'running'")
        await db.commit()
    await message.answer(f"✅ Emergency switch thrown. Halted `{killed_count}` active threads running loops cleanly.")

# --- BOOTSTRAPPING ENGINE POOL RUNTIME ---
async def main():
    global bot_username
    await db_mgr.init()
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is missing inside config.py!")
        return
    bot = Bot(token=config.BOT_TOKEN)
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    logger.info(f"Connected to Telegram API endpoint as @{bot_username}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    worker_task = asyncio.create_task(task_queue.start_worker())
    
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process execution halted cleanly.")
