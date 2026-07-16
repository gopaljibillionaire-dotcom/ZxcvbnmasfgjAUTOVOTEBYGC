"""
Multi-Account Automation Framework - Dynamic Task Queue Worker
"""
import asyncio
import json
import logging
import random
from typing import Dict, Any, List, Tuple
from aiogram import Bot
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

import config
from database import db_mgr
from helpers import decrypt_data, parse_telegram_link, dispatch_log

logger = logging.getLogger("TaskWorkerEngine")

class TaskQueue:
    def __init__(self, bot: Bot = None):
        self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue()
        self.active_workers: Dict[int, asyncio.Task] = {}

    def set_bot(self, bot: Bot) -> None:
        """Allows dynamic assignment of the bot instance after initialization."""
        self.bot = bot

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict) -> None:
        """Pushes a raw task configuration onto the execution queue."""
        await self.queue.put((task_id, creator_id, task_type, payload))
        logger.info(f"Task #{task_id} loaded into RAM queue.")

    async def start_worker(self, *args, **kwargs) -> None:
        """
        Continuous listener loop that runs pending actions.
        Accepts *args and **kwargs to safely ingest configuration flags passed by main.py.
        """
        logger.info("Initializing Task Queue daemon worker...")
        while True:
            try:
                # Safe fetching from the true asyncio Queue object instance
                task_id, creator_id, task_type, payload = await self.queue.get()
                worker = asyncio.create_task(self._execute_task(task_id, creator_id, task_type, payload))
                self.active_workers[task_id] = worker
                
                try:
                    await worker
                except Exception as ex:
                    logger.error(f"Task #{task_id} raised unhandled exception: {ex}")
                finally:
                    self.active_workers.pop(task_id, None)
                    self.queue.task_done()
            except asyncio.CancelledError:
                logger.warning("Queue worker daemon received shut down instruction.")
                break
            except Exception as e:
                logger.critical(f"Queue worker crashed: {e}. Recovering in 5s...")
                await asyncio.sleep(5)

    async def _execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict) -> None:
        """Coordinates and executes an automation task across active accounts."""
        logger.info(f"Processing Task #{task_id} ({task_type.upper()})...")
        await db_mgr.execute_write("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))

        user_role = await db_mgr.get_user_role(creator_id)
        
        # 1. Fetch sessions matching the user's privilege level
        if user_role in ["admin", "owner", "super_owner"]:
            raw_sessions = await db_mgr.execute_read_all("SELECT phone, session_string FROM accounts WHERE status = 'active'")
        else:
            raw_sessions = await db_mgr.execute_read_all("SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?", (creator_id,))

        if not raw_sessions:
            await db_mgr.complete_task(
                task_id=task_id,
                status="failed",
                progress="0 active accounts found",
                success_list=[],
                failure_list=[("System", "No active accounts registered for task execution.")]
            )
            return

        success_phones: List[str] = []
        failure_phones: List[Tuple[str, str]] = []
        total_accounts = len(raw_sessions)

        # 2. Sequential/Batch Execution
        for index, (phone, enc_session) in enumerate(raw_sessions):
            session_str = decrypt_data(enc_session)
            client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
            
            try:
                # Randomized minor initial stagger delay to mimic human login patterns
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await client.connect()
                
                if not await client.is_user_authorized():
                    await db_mgr.update_account_status(phone, "dead")
                    failure_phones.append((phone, "Session expired/Unauthorized"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id, is_private_hash = parse_telegram_link(target)
                msg_id = int(payload.get("msg_id", link_msg_id or 0))

                # Humanized pre-action delay
                await asyncio.sleep(random.uniform(2.0, 4.0))

                # 3. Action routing
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
                        try:
                            msg = await client.get_messages(parsed_target, ids=msg_id)
                            if msg and msg.reactions and msg.reactions.results:
                                active_reactions = [
                                    r.reaction.emoticon for r in msg.reactions.results 
                                    if hasattr(r.reaction, 'emoticon')
                                ]
                                assigned_emoji = random.choice(active_reactions) if active_reactions else random.choice(emojis)
                            else:
                                assigned_emoji = random.choice(emojis)
                        except Exception as fetch_err:
                            logger.warning(f"Failed fetching reaction counts: {fetch_err}. Defaulting to choice array.")
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
                            raise ValueError(f"No match for callback text '{button_text}'.")
                    else:
                        raise ValueError("Target message does not contain inline keyboard buttons.")
                        
                elif task_type == "dm":
                    message_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, message_text)

                success_phones.append(phone)
                
            except FloodWaitError as fwe:
                failure_phones.append((phone, f"Rate limited: wait {fwe.seconds}s"))
                await asyncio.sleep(min(fwe.seconds, 15))
            except Exception as action_ex:
                failure_phones.append((phone, str(action_ex)))
                logger.error(f"Error executing on phone +{phone}: {action_ex}")
            finally:
                await client.disconnect()

            # 4. Anti-Ban cooldown batching logic
            if (index + 1) % config.BATCH_SIZE == 0 and (index + 1) < total_accounts:
                cooldown_time = config.BASE_COOLDOWN + random.randint(5, 15)
                logger.info(f"Task #{task_id}: Batch threshold met. Cooling down for {cooldown_time}s...")
                await asyncio.sleep(cooldown_time)
            else:
                await asyncio.sleep(random.uniform(config.MIN_ACCOUNT_DELAY, config.MAX_ACCOUNT_DELAY))

            # Progress updater
            progress_pct = f"{int(((index + 1) / total_accounts) * 100)}%"
            await db_mgr.update_task_progress(task_id, progress_pct)

        # 5. Compile and finalize task reports
        final_status = "completed" if success_phones else "failed"
        final_progress = f"{len(success_phones)}/{total_accounts} Passed"
        
        await db_mgr.complete_task(
            task_id=task_id,
            status=final_status,
            progress=final_progress,
            success_list=success_phones,
            failure_list=failure_phones
        )

        # Send execution details to your private audit logging channel
        if self.bot:
            await dispatch_log(
                self.bot,
                f"📋 **Task Complete Report**\n\n"
                f"🆔 **Task ID:** `#{task_id}`\n"
                f"⚙️ **Type:** `{task_type.upper()}`\n"
                f"👤 **Initiator ID:** `{creator_id}`\n"
                f"📊 **Final Output:** `{final_progress}`\n"
                f"🟢 **Successes:** `{len(success_phones)}`\n"
                f"🔴 **Failures:** `{len(failure_phones)}`"
            )


# ==========================================
# EXPORT GLOBAL INSTANCE FOR MAIN.PY
# ==========================================
# We instantiate the engine here so that main.py safely uses a unified run loop.
task_engine = TaskQueue()
