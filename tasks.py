import asyncio
import random
import json
import logging
import aiosqlite
from typing import Dict, Optional
from aiogram import Bot

from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyUnregisteredError,
    UserDeactivatedError,
    FloodWaitError
)

from config import API_ID, API_HASH
from database import db_mgr
from helpers import decrypt_data, parse_telegram_link, SecurityHubLogger

logger = logging.getLogger("MultiAccountSystem.Tasks")

class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}
        self.bot_instance: Optional[Bot] = None

    def set_bot(self, bot: Bot):
        self.bot_instance = bot

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict):
        await self.queue.put((task_id, creator_id, task_type, payload))
        if self.bot_instance:
            await SecurityHubLogger.log_task_submission(self.bot_instance, task_id, creator_id, task_type, payload)

    async def start_worker(self):
        logger.info("Anti-Ban Task pipeline processing loop started.")
        while True:
            try:
                task_id, creator_id, task_type, payload = await self.queue.get()
                loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload))
                self.current_tasks[task_id] = loop_task
                try:
                    await loop_task
                except asyncio.CancelledError:
                    logger.info(f"Task {task_id} execution was explicitly cancelled.")
                except Exception as e:
                    logger.error(f"Execution failure on task {task_id}: {e}")
                finally:
                    self.current_tasks.pop(task_id, None)
                    self.queue.task_done()
            except Exception as e:
                logger.error(f"Error in task queue worker loop: {e}")
                await asyncio.sleep(1)

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict):
        async with aiosqlite.connect(db_mgr.db_path) as db:
            async with db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'") as cursor:
                accounts = await cursor.fetchall()

        if not accounts:
            logger.warning(f"Task {task_id} aborted: Zero active sessions found.")
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = 'No active accounts connected' WHERE task_id = ?", (task_id,))
                await db.commit()
            return

        success_accounts = []
        failed_accounts = []
        
        target_link = payload.get("target", "")
        parsed_target, msg_ids = parse_telegram_link(target_link)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
            await db.commit()

        total_accounts = len(accounts)
        
        for index, (phone, enc_session) in enumerate(accounts):
            if asyncio.current_task().cancelled():
                raise asyncio.CancelledError()

            decrypted_session = decrypt_data(enc_session)
            if not decrypted_session:
                failed_accounts.append((phone, "Decryption Error"))
                continue

            client = TelegramClient(StringSession(decrypted_session), API_ID, API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise AuthKeyUnregisteredError("Session deactivated or expired.")

                if task_type == "join":
                    if isinstance(parsed_target, str) and (len(parsed_target) == 22 or not parsed_target.isalnum()):
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                
                elif task_type == "leave":
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))
                
                elif task_type == "react":
                    if not msg_ids:
                        raise ValueError("No valid message target ID specified for reactions.")
                    
                    reactions_list = payload.get("reactions", [])
                    random_match = payload.get("random_match", False)

                    for msg_id in msg_ids:
                        if random_match:
                            msg_objs = await client.get_messages(parsed_target, ids=[msg_id])
                            if msg_objs and msg_objs[0].reactions:
                                available = [r.reaction.emoj for r in msg_objs[0].reactions.results if hasattr(r.reaction, 'emoj')]
                                chosen_emoji = random.choice(available) if available else "👍"
                            else:
                                chosen_emoji = "👍"
                        else:
                            chosen_emoji = random.choice(reactions_list) if reactions_list else "👍"
                            
                        await client(functions.messages.SendReactionRequest(
                            peer=parsed_target,
                            msg_id=msg_id,
                            reaction=[tg_types.ReactionEmoji(emoj=chosen_emoji)]
                        ))
                        await asyncio.sleep(0.5)

                elif task_type == "button_vote":
                    if not msg_ids:
                        raise ValueError("No valid message target ID specified for inline buttons.")
                    
                    target_msg_id = msg_ids[0]
                    button_text = payload.get("button_text", "")
                    msg_objs = await client.get_messages(parsed_target, ids=[target_msg_id])
                    
                    if not msg_objs or not msg_objs[0].reply_markup:
                        raise ValueError("No inline markup found on this post link.")
                        
                    clicked = False
                    for row in msg_objs[0].reply_markup.rows:
                        for button in row.buttons:
                            if button_text.lower() in button.text.lower():
                                if isinstance(button, tg_types.KeyboardButtonCallback):
                                    await client(functions.messages.GetBotCallbackAnswerRequest(
                                        peer=parsed_target,
                                        msg_id=target_msg_id,
                                        data=button.data
                                    ))
                                else:
                                    await msg_objs[0].click(button)
                                clicked = True
                                break
                        if clicked:
                            break
                    if not clicked:
                        raise ValueError(f"Target button with label containing '{button_text}' not found.")

                elif task_type == "dm":
                    dm_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, dm_text)

                success_accounts.append(phone)
                
            except (AuthKeyUnregisteredError, UserDeactivatedError):
                failed_accounts.append((phone, "Dead Session"))
                async with aiosqlite.connect(db_mgr.db_path) as db_flag:
                    await db_flag.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_flag.commit()
            except FloodWaitError as e:
                failed_accounts.append((phone, f"FloodWait limit: {e.seconds}s"))
            except Exception as e:
                failed_accounts.append((phone, str(e)))
            finally:
                await client.disconnect()

            await asyncio.sleep(random.uniform(2.5, 5.0))
            
            progress_percent = f"{int(((index + 1) / total_accounts) * 100)}%"
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_percent, task_id))
                await db.commit()

        report_lines = [
            f"=== PRODUCTION LOG REPORT FOR RUNTIME TASK {task_id} ===",
            f"⚡ TASK INSTANCE TYPE: {task_type.upper()}",
            f"👤 OWNER ARCHITECTURE PROFILE: ID {creator_id}",
            f"\n🟢 SUCCESSFUL AUTOMATIONS ({len(success_accounts)}):"
        ]
        for s in success_accounts:
            report_lines.append(f" - +{s}: COMPLETE OPERATION ACTION")
        report_lines.append(f"\n🔴 CRITICAL FAILURES SUMMARY ({len(failed_accounts)}):")
        for f_phone, reason in failed_accounts:
            report_lines.append(f" - +{f_phone}: DECLINED | Trigger Exception: {reason}")
            
        full_report_text = "\n".join(report_lines)
        
        final_status = "completed" if success_accounts else "failed"
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute(
                "UPDATE tasks SET status = ?, progress = '100%', success_report = ?, failure_report = ? WHERE task_id = ?",
                (final_status, json.dumps(success_accounts), json.dumps(failed_accounts), task_id)
            )
            await db.commit()
            
        if self.bot_instance:
            await SecurityHubLogger.log_task_completion(
                self.bot_instance, task_id, creator_id, task_type, 
                len(success_accounts), len(failed_accounts), full_report_text
            )

    async def cancel_specific_task(self, task_id: int) -> bool:
        if task_id in self.current_tasks:
            self.current_tasks[task_id].cancel()
            return True
            
        found = False
        temp_list = []
        while not self.queue.empty():
            item = await self.queue.get()
            if item[0] == task_id:
                found = True
                self.queue.task_done()
            else:
                temp_list.append(item)
                
        for item in temp_list:
            await self.queue.put(item)
            
        return found

task_queue = TaskQueue()
