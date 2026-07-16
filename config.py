import os

# --- CORE API CONFIGURATION ---
API_ID = int(os.getenv("TG_API_ID", "30636134"))
API_HASH = os.getenv("TG_API_HASH", "9c5bb2bbeb19a0da5bfb0e7052875d2f")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8733721396:AAHJrr4uHC2WEx5r6BCHqBmx4LbMKh1Ngds")

# --- LOGGING INFRASTRUCTURE ---
# IMPORTANT: Make sure the bot is an ADMIN in this channel. 
# Private channel IDs MUST start with -100 (e.g., -1002234567890)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1004349607766"))

# --- ADMIN MATRIX ---
SUPER_OWNER_IDS = [7952327997, 7953147643, 8064493735]

# --- CRYPTO FALLBACKS ---
SECRET_KEY = os.getenv("ENCRYPTION_KEY", "pydroid_secure_fallback_key_2026")

# Default reaction bank
REACTION_EMOJIS = ["👍", "👎", "🔥", "🎉", "👏", "🥰", "😮", "😢", "😡", "💩", "🤩", "🤔", "👀", "💯", "🤣"]
