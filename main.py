import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from config import logger
from database import db_mgr, decrypt_data
from handlers import router, task_queue, set_bot_username

async def verify_saved_sessions():
    logger.info("Verifying all active account database sessions...")
    import aiosqlite
    async with aiosqlite.connect(db_mgr.db_path) as db:
        accounts = await (await db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'")).fetchall()
    
    semaphore = asyncio.Semaphore(10)
    async def check_account(phone, enc_session):
        async with semaphore:
            try:
                client = TelegramClient(StringSession(decrypt_data(enc_session)), config.API_ID, config.API_HASH)
                await client.connect()
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                await client.disconnect()
            except Exception: pass
                
    await asyncio.gather(*(check_account(p, s) for p, s in accounts))

async def main():
    await db_mgr.init()
    await verify_saved_sessions()
    
    if not config.BOT_TOKEN:
        logger.error("Missing BOT_TOKEN in config configuration profile!")
        return
        
    bot = Bot(token=config.BOT_TOKEN)
    bot_info = await bot.get_me()
    set_bot_username(bot_info.username)
    
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
        logger.info("Bot execution successfully stopped.")
