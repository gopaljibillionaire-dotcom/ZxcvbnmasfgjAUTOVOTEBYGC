"""
Multi-Account Automation Framework - Persistent Database Engine
"""
import sqlite3
import aiosqlite
import logging
import json
import os
from typing import Any, Optional, Union, List, Tuple
import config

logger = logging.getLogger("DatabaseEngine")

class Database:
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path

    async def init(self) -> None:
        """Initializes tables, indexes, and validates schemas."""
        logger.info("Initializing connection with core SQLite database...")
        async with aiosqlite.connect(self.db_path) as db:
            # Users Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    role TEXT DEFAULT 'user', 
                    max_accounts INTEGER DEFAULT 5,
                    referred_by INTEGER,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Accounts Table (Individual Telegram Sessions)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY,
                    user_id INTEGER, 
                    username TEXT,
                    session_string TEXT,
                    status TEXT DEFAULT 'active', 
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            # System Tasks Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER,
                    type TEXT, 
                    payload TEXT, 
                    status TEXT DEFAULT 'pending', 
                    progress TEXT DEFAULT '0%',
                    success_report TEXT DEFAULT '[]',
                    failure_report TEXT DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Security Audit Logs Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexing structures for lightning-fast queries
            await db.execute("CREATE INDEX IF NOT EXISTS idx_users_id ON users(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_creator ON tasks(creator_id)")
            await db.commit()
            
        logger.info("Database schemas confirmed and loaded successfully.")

    async def _handle_operational_error(self, error: aiosqlite.sqlite3.OperationalError) -> bool:
        """Internal helper to automatically fix missing columns without crashing."""
        error_msg = str(error)
        if "no such column: status" in error_msg:
            logger.warning("Migration Guard: 'status' column is missing in 'users' or 'accounts' table! Fixing structural layout...")
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    # Attempt altering both tables safely just in case
                    try:
                        await db.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
                    except aiosqlite.sqlite3.OperationalError:
                        pass # Column might already be there in this specific table
                    
                    try:
                        await db.execute("ALTER TABLE accounts ADD COLUMN status TEXT DEFAULT 'active'")
                    except aiosqlite.sqlite3.OperationalError:
                        pass
                        
                    await db.commit()
                logger.info("Migration Guard: Column structural updates successfully applied.")
                return True
            except Exception as migrate_err:
                logger.error(f"Migration Guard failed to patch database schema: {migrate_err}")
        return False

    async def execute_write(self, query: str, parameters: tuple = ()) -> int:
        """Helper to run INSERT, UPDATE, DELETE queries safely."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query, parameters) as cursor:
                    last_row_id = cursor.lastrowid
                    await db.commit()
                    return last_row_id
        except aiosqlite.sqlite3.OperationalError as e:
            if await self._handle_operational_error(e):
                # Retry transaction once layout modifications settle
                async with aiosqlite.connect(self.db_path) as db:
                    async with db.execute(query, parameters) as cursor:
                        last_row_id = cursor.lastrowid
                        await db.commit()
                        return last_row_id
            raise e

    async def execute_read_one(self, query: str, parameters: tuple = ()) -> Optional[Tuple]:
        """Fetch a single record cleanly."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query, parameters) as cursor:
                    return await cursor.fetchone()
        except aiosqlite.sqlite3.OperationalError as e:
            if await self._handle_operational_error(e):
                # Retry query execution following structural fix
                async with aiosqlite.connect(self.db_path) as db:
                    async with db.execute(query, parameters) as cursor:
                        return await cursor.fetchone()
            raise e

    async def execute_read_all(self, query: str, parameters: tuple = ()) -> List[Tuple]:
        """Fetch a list of matching database entries."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query, parameters) as cursor:
                    return await cursor.fetchall()
        except aiosqlite.sqlite3.OperationalError as e:
            if await self._handle_operational_error(e):
                # Retry query execution following structural fix
                async with aiosqlite.connect(self.db_path) as db:
                    async with db.execute(query, parameters) as cursor:
                        return await cursor.fetchall()
            raise e

    # --- USER ADMINISTRATION QUERIES ---
    async def get_user_role(self, user_id: int) -> str:
        if user_id in config.SUPER_OWNER_IDS:
            return "super_owner"
        
        row = await self.execute_read_one("SELECT role, status FROM users WHERE user_id = ?", (user_id,))
        if row:
            role, status = row
            if status == "banned":
                return "banned"
            return role
        return "user"

    async def get_user_account_limit(self, user_id: int) -> int:
        if user_id in config.SUPER_OWNER_IDS:
            return 999999
        row = await self.execute_read_one("SELECT max_accounts FROM users WHERE user_id = ?", (user_id,))
        return row[0] if row else config.DEFAULT_USER_MAX_ACCOUNTS

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None) -> bool:
        row = await self.execute_read_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if not row:
            # Prevent self-referral loop strings
            if referred_by == user_id:
                referred_by = None
            await self.execute_write(
                "INSERT INTO users (user_id, username, role, referred_by, max_accounts) VALUES (?, ?, 'user', ?, ?)",
                (user_id, username, referred_by, config.DEFAULT_USER_MAX_ACCOUNTS)
            )
            return True
        return False

    # --- ACCOUNT INFRASTRUCTURE CONTROL ---
    async def get_active_sessions(self, user_id: int, user_role: str) -> List[Tuple[str, str, str]]:
        """
        Fetches tuple lists of active accounts.
        Admins/Owners pull everything. Standard users pull only their registered items.
        Returns: [(phone, session_string, username)]
        """
        if user_role in ["admin", "owner", "super_owner"]:
            rows = await self.execute_read_all("SELECT phone, session_string, username FROM accounts WHERE status = 'active'")
        else:
            rows = await self.execute_read_all("SELECT phone, session_string, username FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        return rows

    async def register_session(self, phone: str, user_id: int, username: str, enc_session: str) -> None:
        await self.execute_write(
            """
            INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
            VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """,
            (phone.replace("+", "").strip(), user_id, username or "None", enc_session)
        )

    async def update_account_status(self, phone: str, status: str) -> None:
        await self.execute_write("UPDATE accounts SET status = ?, last_active = CURRENT_TIMESTAMP WHERE phone = ?", (status, phone))

    async def delete_account(self, phone: str) -> bool:
        clean_phone = phone.replace("+", "").strip()
        # Verify it exists first
        row = await self.execute_read_one("SELECT 1 FROM accounts WHERE phone = ?", (clean_phone,))
        if not row:
            return False
        await self.execute_write("DELETE FROM accounts WHERE phone = ?", (clean_phone,))
        return True

    # --- TASK QUEUE OPERATIONS ---
    async def create_task(self, creator_id: int, task_type: str, payload_data: dict) -> int:
        payload_str = json.dumps(payload_data)
        task_id = await self.execute_write(
            "INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)",
            (creator_id, task_type, payload_str)
        )
        return task_id

    async def update_task_progress(self, task_id: int, progress: str) -> None:
        await self.execute_write("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress, task_id))

    async def complete_task(self, task_id: int, status: str, progress: str, success_list: list, failure_list: list) -> None:
        success_str = json.dumps(success_list)
        failure_str = json.dumps(failure_list)
        await self.execute_write(
            """
            UPDATE tasks 
            SET status = ?, progress = ?, success_report = ?, failure_report = ? 
            WHERE task_id = ?
            """,
            (status, progress, success_str, failure_str, task_id)
        )

db_mgr = Database()
