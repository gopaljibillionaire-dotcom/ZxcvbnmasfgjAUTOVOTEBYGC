"""
Multi-Account Automation Framework - Security & Utility Helpers
"""
import re
import base64
import logging
from typing import Tuple, Optional, Any
from aiogram import Bot
import config

logger = logging.getLogger("SystemHelpers")

# --- BITWISE XOR CRYPTOGRAPHY MATRICES ---
def _get_crypto_key() -> int:
    """Dynamically generates a XOR byte mask from the configured Secret Key."""
    return sum(ord(char) for char in config.SECRET_KEY) % 256 or 42

def encrypt_data(data: str) -> str:
    """Encrypts plaintext string using dynamic XOR byte shift arrays."""
    if not data:
        return ""
    key = _get_crypto_key()
    cipher_bytes = bytes([b ^ key for b in data.encode('utf-8')])
    return base64.b64encode(cipher_bytes).decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    """Decrypts dynamic XOR shifted cipher text safely."""
    if not encrypted_data:
        return ""
    key = _get_crypto_key()
    try:
        raw_cipher = base64.b64decode(encrypted_data.encode('utf-8'))
        plain_bytes = bytes([b ^ key for b in raw_cipher])
        return plain_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"XOR Cryptographic Decryption Engine Fault: {e}")
        return ""

# --- TELEGRAM LINK RESOLVER & REGEX ENGINE ---
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int], bool]:
    """
    Parses complex and standard Telegram URLs.
    Supports public links, private hashes, and explicit message node markers.
    
    Returns:
        Tuple[target, msg_id, is_private_hash]
    """
    link = link.strip().replace(" ", "")
    if not link:
        return None, None, False
        
    # Pattern 1: Private channel join invite hashes (e.g., t.me/joinchat/... or t.me/+...)
    hash_match = re.search(r'(?:joinchat/|\+|t\.me/\+)([a-zA-Z0-9_\-]+)', link)
    if hash_match:
        return hash_match.group(1), None, True

    # Pattern 2: Explicit private channel message links (e.g., t.me/c/1234567/45)
    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return channel_id, msg_id, False
        
    # Pattern 3: Standard public channel posts (e.g., t.me/username/45)
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match:
        return msg_match.group(1), int(msg_match.group(2)), False
        
    # Pattern 4: Raw usernames or trimmed URLs with standard message paths
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = int(parts[1])
            return target, msg_id, False
            
    return target, None, False

# --- LOG RECIPIENTS LOGISTICS ---
async def dispatch_log(bot: Bot, text: str):
    """Prints event logs to standard output and posts updates directly to the log channel."""
    logger.info(f"[AUDIT LOG]: {text}")
    try:
        await bot.send_message(
            chat_id=config.LOG_CHANNEL_ID, 
            text=f"🔔 **System Audit Event**\n\n{text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.critical(
            f"\n❌ LOG TRANSMISSION FAILURE to channel {config.LOG_CHANNEL_ID}!\n"
            f"REASON: {e}\n"
            f"FIX: Add the bot as an Admin inside the log channel with 'Post Messages' rights!\n"
        )
