"""
Multi-Account Automation Framework - Global Configuration Matrix
Provides environmental defaults, security configuration, and feature setups.
"""
import os

# --- CORE API CONFIGURATION ---
# Telethon/Pyrogram application credentials
API_ID: int = int(os.getenv("TG_API_ID", "30636134"))
API_HASH: str = os.getenv("TG_API_HASH", "9c5bb2bbeb19a0da5bfb0e7052875d2f")

# Standard Bot Token (from @BotFather)
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8733721396:AAHJrr4uHC2WEx5r6BCHqBmx4LbMKh1Ngds")

# --- LOGGING & TELEMETRY INFRASTRUCTURE ---
LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "-1004349607766"))

# --- OWNER & SECURITY RULES ---
# List of administrative Telegram IDs who bypass limit restrictions
SUPER_OWNER_IDS: list[int] = [7952327997, 7953147643, 8064493735]

# Encription Key for securing Telethon string sessions inside SQLite
SECRET_KEY: str = os.getenv("ENCRYPTION_KEY", "pydroid_secure_fallback_key_2026")

# --- ANTI-BAN TIMING CONFIGURATIONS ---
BATCH_SIZE: int = 5              # Process this many accounts simultaneously
BASE_COOLDOWN: int = 15          # Standard rest time in seconds between batches
MIN_ACCOUNT_DELAY: float = 3.0   # Minimum delay between individual account actions (seconds)
MAX_ACCOUNT_DELAY: float = 6.5   # Maximum delay between individual account actions (seconds)

# --- REACTION EXPRESSION LISTS ---
# Complete bank of supported standard Telegram message reactions
REACTION_EMOJIS: list[str] = [
    "👍", "👎", "🔥", "🎉", "👏", "🥰", "😮", "😢", 
    "😡", "💩", "🤩", "🤔", "👀", "💯", "🤣", "⚡", 
    "🤡", "🙏", "✍️", "❤️", "🎈", "🥱", "😇"
]

# --- SYSTEM SETTINGS ---
DB_PATH: str = "bot_core_data.db"
DEFAULT_USER_MAX_ACCOUNTS: int = 5
