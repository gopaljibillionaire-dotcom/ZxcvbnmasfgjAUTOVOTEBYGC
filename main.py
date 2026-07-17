"""
Multi-Account Automation Framework - Master Control Main Engine
Handles asynchronous long-polling, inline keyboard router, session login flows, 
and maps all UI requests directly into the dynamic TaskQueue worker.
"""
import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from database import db_mgr
from helpers import encrypt_data, dispatch_log
from tasks import task_engine

# --- SYSTEM SETUP & LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MainControlEngine")

# Initialize Master Bot Instance
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# --- FSM DESIGN STATES ---
class AuthSessionStates(StatesGroup):
    entering_phone = State()
    entering_otp = State()
    entering_password = State()

class AutomationStates(StatesGroup):
    entering_target = State()
    entering_reactions = State()
    entering_button_text = State()
    entering_dm_text = State()
    entering_bot_username = State()

# --- REUSABLE CORE INTERFACE MARKUPS ---
def get_main_menu_keyboard(role: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Add New Account", callback_data="menu_add_account")
    builder.button(text="📊 Task Dashboard", callback_data="menu_dashboard")
    builder.row(
        types.InlineKeyboardButton(text="📢 Join Channel", callback_data="task_init:join"),
        types.InlineKeyboardButton(text="🚪 Leave Channel", callback_data="task_init:leave")
    )
    builder.row(
        types.InlineKeyboardButton(text="🔥 Send Reactions", callback_data="task_init:react"),
        types.InlineKeyboardButton(text="🔘 Inline Button Vote", callback_data="task_init:button_vote")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 Bulk DM Target", callback_data="task_init:dm"),
        types.InlineKeyboardButton(text="🤖 Start Target Bot", callback_data="task_init:start_bot")
    )
    if role in ["admin", "owner", "super_owner"]:
        builder.row(types.InlineKeyboardButton(text="🛠️ System Admin Panel", callback_data="menu_admin"))
    return builder.as_markup()

# --- CORE COMMAND HANDLERS ---
@dp.message(CommandStart())
async def cmd_start_handler(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    
    # Track or initialize user account inside SQLite data array
    await db_mgr.create_user_if_not_exists(user_id, username)
    role = await db_mgr.get_user_role(user_id)
    
    if role == "banned":
        await message.answer("❌ You are permanently banned from using this automation network framework.")
        return
        
    await message.answer(
        f"👋 **Welcome to Multi-Account Automation Console v2.0**\n\n"
        f"Your Current Privilege Authorization: `{role.upper()}`\n"
        f"Select an operation vector from the configuration console layout matrix below:",
        reply_markup=get_main_menu_keyboard(role),
        parse_mode="Markdown"
    )

# --- AUTHENTICATION FLOW (TELETHON STRING SESSION EXTRACTOR) ---
@dp.callback_query(F.data == "menu_add_account")
async def cb_add_account_init(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    current_count_rows = await db_mgr.execute_read_all("SELECT phone FROM accounts WHERE user_id = ?", (user_id,))
    limit = await db_mgr.get_user_account_limit(user_id)
    
    if len(current_count_rows) >= limit and role not in ["admin", "owner", "super_owner"]:
        await callback.answer(f"❌ Account limit reached ({limit}). Upgrades managed by Super Owners.", show_alert=True)
        return
        
    await callback.message.edit_text(
        "📱 **Account Integration Step 1/3**\n\n"
        "Please enter the phone number of the Telegram account you want to connect.\n"
        "Format: `+1234567890` (Include country code prefix string)",
        parse_mode="Markdown"
    )
    await state.set_state(AuthSessionStates.entering_phone)

@dp.message(AuthSessionStates.entering_phone)
async def process_auth_phone(message: types.Message, state: FSMContext):
    phone = message.text.replace(" ", "").strip()
    if not phone.startswith("+") or len(phone) < 8:
        await message.answer("❌ Invalid format. Please start with a clear '+' country code identifier prefix.")
        return
        
    await message.answer("🔄 **Contacting Telegram Telegram Core Matrix Servers...**")
    
    # Initialize transient string session client memory layout allocation
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(phone)
        # Cache client parameters into FSM engine safely
        await state.update_data(phone=phone, phone_code_hash=sent_code.phone_code_hash, string_session=client.session.save())
        await message.answer(
            f"📩 **OTP Security Token Transmitted!**\n\n"
            f"Enter the verification code sent to target connection **{phone}**.\n"
            f"Format it using spaces if raw layout strings fail: Example: `1 2 3 4 5`"
        )
        await state.set_state(AuthSessionStates.entering_otp)
    except Exception as e:
        logger.error(f"Failed to transmit auth packet array sequence: {e}")
        await message.answer(f"❌ Transmission Error: `{str(e)}`")
        await state.clear()
    finally:
        await client.disconnect()

@dp.message(AuthSessionStates.entering_otp)
async def process_auth_otp(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    otp_code = message.text.replace(" ", "").strip()
    
    client = TelegramClient(StringSession(user_data['string_session']), config.API_ID, config.API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(phone=user_data['phone'], code=otp_code, phone_code_hash=user_data['phone_code_hash'])
        
        # Registration payload execution block on successful direct authentication
        me = await client.get_me()
        enc_session = encrypt_data(client.session.save())
        await db_mgr.register_session(user_data['phone'], message.from_user.id, me.username, enc_session)
        
        await message.answer(f"🎉 **Account Verification Successful!**\nConnected asset: `@{me.username or me.first_name}` (+{user_data['phone']})")
        await state.clear()
    except Exception as otp_ex:
        if "Two-step verification" in str(otp_ex) or "password" in str(otp_ex).lower():
            await state.update_data(otp=otp_code)
            await message.answer("🔐 **2FA Authentication Detected!**\nPlease enter the account's Cloud Password below:")
            await state.set_state(AuthSessionStates.entering_password)
        else:
            await message.answer(f"❌ Error signing into runtime node block: `{str(otp_ex)}`")
            await state.clear()
    finally:
        await client.disconnect()

@dp.message(AuthSessionStates.entering_password)
async def process_auth_password(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    cloud_password = message.text.strip()
    
    client = TelegramClient(StringSession(user_data['string_session']), config.API_ID, config.API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(password=cloud_password)
        me = await client.get_me()
        enc_session = encrypt_data(client.session.save())
        await db_mgr.register_session(user_data['phone'], message.from_user.id, me.username, enc_session)
        
        await message.answer(f"🎉 **Account Authenticated via 2FA!**\nConnected node asset: `@{me.username or me.first_name}`")
        await state.clear()
    except Exception as pass_ex:
        await message.answer(f"❌ Verification Failure on target 2FA structure check: `{str(pass_ex)}`")
        await state.clear()
    finally:
        await client.disconnect()

# --- WORKER ROUTER DIRECTION CONTROL MATRIX ---
@dp.callback_query(F.data.startswith("task_init:"))
async def process_task_initialization(callback: types.CallbackQuery, state: FSMContext):
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    text_prompts = {
        "join": "🔗 **Join Automation**\nEnter public link username (`@channel`) or complete private join invite URL (`t.me/+hash`):",
        "leave": "🚪 **Leave Automation**\nEnter the username or tracking link structure of the target entity to depart:",
        "react": "🔥 **Reaction Automation**\nEnter post link structure (e.g., `t.me/channel_name/1234`):",
        "button_vote": "🔘 **Inline Button Click Voter**\nEnter standard post URL location reference string (e.g., `t.me/c/1234/45`):",
        "dm": "💬 **Bulk Distribution Direct Message**\nEnter target recipient username (`@username`) or full identity sequence payload link:",
        "start_bot": "🤖 **Multi-Account Bot Initialization Engine**\nEnter the target bot username you wish to launch across all active nodes (e.g., `@ExampleBot`):"
    }
    
    await callback.message.edit_text(text_prompts.get(task_type, "Provide target pointer string input reference value:"), parse_mode="Markdown")
    await state.set_state(AutomationStates.entering_target)

@dp.message(AutomationStates.entering_target)
async def process_target_input(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    target = message.text.strip()
    task_type = user_data['task_type']
    
    await state.update_data(target=target)
    
    # Conditional branching maps based on task parameters
    if task_type == "react":
        builder = InlineKeyboardBuilder()
        builder.button(text="👍 Default ThumbsUp", callback_data="set_rx:default")
        builder.button(text="🎲 Random Expressive Mix", callback_data="set_rx:random")
        builder.button(text="👥 Mirror Existing Reactions", callback_data="set_rx:existing")
        await message.answer("🎭 **Select Reaction Distribution Logic Matrix Type:**", reply_markup=builder.as_markup())
        
    elif task_type == "button_vote":
        await message.answer("✍️ **Enter the exact text string of the target button:**\n*(Case-insensitive text mapping array check matches, e.g., 'Vote 1' or 'Join Now')*")
        await state.set_state(AutomationStates.entering_button_text)
        
    elif task_type == "dm":
        await message.answer("📝 **Enter the text message payload to transmit dynamically via accounts:**")
        await state.set_state(AutomationStates.entering_dm_text)
        
    elif task_type == "start_bot":
        # Start bot works exactly like a DM task executing `/start` command payload 
        payload_data = {"target": target, "text": "/start"}
        task_id = await db_mgr.create_task(message.from_user.id, "dm", payload_data)
        await task_engine.add_task(task_id, message.from_user.id, "dm", payload_data)
        await message.answer(f"🚀 **Bot Starter Matrix Array Active!**\nQueued tracking ID sequence index: `#{task_id}`")
        await state.clear()
        
    else: # join and leave tasks carry simple target payloads to pipeline directly 
        payload_data = {"target": target}
        task_id = await db_mgr.create_task(message.from_user.id, task_type, payload_data)
        await task_engine.add_task(task_id, message.from_user.id, task_type, payload_data)
        await message.answer(f"🚀 **Task Engine Queue Sync Confirmed!**\nTracking reference sequence locator ID: `#{task_id}`")
        await state.clear()

# --- REACTION TYPE CALLBACK CONTROLLER ---
@dp.callback_query(F.data.startswith("set_rx:"))
async def finalize_reaction_task(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    rx_mode_flag = callback.data.split(":")[1]
    
    payload_data = {"target": user_data['target'], "react_mode": "standard", "reactions": ["👍"]}
    if rx_mode_flag == "random":
        payload_data["react_mode"] = "random"
    elif rx_mode_flag == "existing":
        payload_data["react_mode"] = "existing_reactions"
        
    task_id = await db_mgr.create_task(callback.from_user.id, "react", payload_data)
    await task_engine.add_task(task_id, callback.from_user.id, "react", payload_data)
    await callback.message.edit_text(f"🚀 **Reaction Packet Framework Sequence Synced!**\nTracking sequence task code: `#{task_id}`")
    await state.clear()

# --- INLINE VOTING & DM COMPLETION ROUTERS ---
@dp.message(AutomationStates.entering_button_text)
async def process_button_text_vote(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    payload_data = {"target": user_data['target'], "button_text": message.text.strip()}
    
    task_id = await db_mgr.create_task(message.from_user.id, "button_vote", payload_data)
    await task_engine.add_task(task_id, message.from_user.id, "button_vote", payload_data)
    await message.answer(f"🚀 **Inline Keyboard Click Matrix Active!**\nTask Identifier Tracking index: `#{task_id}`")
    await state.clear()

@dp.message(AutomationStates.entering_dm_text)
async def process_dm_text_distribution(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    payload_data = {"target": user_data['target'], "text": message.text.strip()}
    
    task_id = await db_mgr.create_task(message.from_user.id, "dm", payload_data)
    await task_engine.add_task(task_id, message.from_user.id, "dm", payload_data)
    await message.answer(f"🚀 **Bulk DM Distribution Vector Active!**\nTask Identifier Tracking index: `#{task_id}`")
    await state.clear()

# --- TASK MONITORING DASHBOARD ---
@dp.callback_query(F.data == "menu_dashboard")
async def process_dashboard_render(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role in ["admin", "owner", "super_owner"]:
        tasks = await db_mgr.execute_read_all("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 5")
    else:
        tasks = await db_mgr.execute_read_all("SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 5", (user_id,))
        
    if not tasks:
        await callback.answer("📭 No active automation tasks found inside local memory arrays.", show_alert=True)
        return
        
    report = "📊 **Active Framework Execution Logs Matrix (Last 5)**\n\n"
    for tid, ttype, status, progress in tasks:
        icon = "⏳" if status == "pending" else "🔄" if status == "running" else "✅" if status == "completed" else "❌"
        report += f"{icon} `#{tid}` | `{ttype.upper()}` | Status: `{status}` | Progress: `({progress})`\n"
        
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Refresh Status Panel", callback_data="menu_dashboard")
    await callback.message.edit_text(report, reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- CORE APPLICATION BOOT ENGINE ENTRY POINT ---
async def main():
    # Pass running core client dependencies to task worker system 
    task_engine.set_bot(bot)
    
    # Fire up DB tables matrix arrays validation 
    await db_mgr.init()
    
    # Initialize long-polling dispatcher listener array pipelines asynchronously
    logger.info("Starting background execution daemon loops and long-polling engines...")
    
    # Start the execution daemon loop alongside the Telegram bot long-polling infrastructure
    await asyncio.gather(
        dp.start_polling(bot),
        task_engine.start_worker()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("System run sequence dropped safely via explicit manual execution halt signal.")
