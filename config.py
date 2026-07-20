import os
import sys
import logging

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MultiAccountSystem")

# --- CORE TELEGRAM API CREDENTIALS ---
API_ID = int(os.getenv("TG_API_ID", "34043431"))
API_HASH = os.getenv("TG_API_HASH", "1b35dae0978194f1088cb6168b70779c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8733721396:AAHJrr4uHC2WEx5r6BCHqBmx4LbMKh1Ngds")

# --- PRIVILEGED AUTHORIZATION LIFTS ---
# Super Owners bypass administrative data isolation boundaries 
# and gain access to cross-tier bulk dynamic account exports.
SUPER_OWNER_IDS = [7952327997, 7953147643, 8064493735] 

# --- SYSTEM TEAM ATTRIBUTIONS ---
DESIGNER_HANDLE = "Gopalji_choubey"
MANAGER_HANDLE = "BMWM4Z"

# --- LOCAL STORAGE OBFUSCATION KEY ---
# Leveraged inside the core cryptographic cipher block matrix (database.py)
SECRET_KEY = os.getenv("ENCRYPTION_KEY", "secure_fallback_key_2026")

# --- AUDIT EVENT CAPTURE PIPE ---
# Central logging channel where performance reports and telemetry parameters are securely mirrored.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1004349607766"))
