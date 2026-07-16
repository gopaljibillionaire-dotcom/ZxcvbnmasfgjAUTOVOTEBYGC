import os
import aiosqlite

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self):
        """
        Ensures the database file exists and sets up tables if they do not exist.
        Also patches/migrates the table dynamically if the 'status' or 'id_limit' 
        columns are missing from an older database.
        """
        # Create directory path if it doesn't exist
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            # 1. Create the base 'users' table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    role TEXT DEFAULT 'normal',
                    status TEXT DEFAULT 'active',
                    id_limit INTEGER DEFAULT 5
                )
            """)

            # 2. Create the target IDs table for tracking registered target accounts
            await db.execute("""
                CREATE TABLE IF NOT EXISTS target_ids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    target_id TEXT UNIQUE,
                    FOREIGN KEY(owner_id) REFERENCES users(user_id)
                )
            """)
            await db.commit()

            # 3. MIGRATION CHECK: Safe verification of columns (prevents future "no such column" errors)
            async with db.execute("PRAGMA table_info(users)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]
                
            if 'status' not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active';")
                await db.commit()
                print("Migration: Added 'status' column to users table.")

            if 'id_limit' not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN id_limit INTEGER DEFAULT 5;")
                await db.commit()
                print("Migration: Added 'id_limit' column to users table.")

    async def get_user_role(self, user_id: int) -> dict:
        """Retrieves user's role, status, and target ID limit."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT role, status, id_limit FROM users WHERE user_id = ?", 
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"role": row[0], "status": row[1], "id_limit": row[2]}
                # Fallback values for unregistered users
                return {"role": "normal", "status": "active", "id_limit": 5}

    async def register_user(self, user_id: int, role: str = "normal"):
        """Registers a user with a default role if they do not already exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, role, status, id_limit) VALUES (?, ?, 'active', 5)",
                (user_id, role)
            )
            await db.commit()

    async def update_role(self, user_id: int, role: str, limit: int = 5):
        """Updates a user's role and limit, or creates them if missing."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, role, id_limit, status) 
                VALUES (?, ?, ?, 'active') 
                ON CONFLICT(user_id) DO UPDATE SET 
                    role = excluded.role, 
                    id_limit = excluded.id_limit
            """, (user_id, role, limit))
            await db.commit()

    async def add_target_id(self, owner_id: int, target_id: str) -> bool:
        """Adds a target ID under the user's ownership, checking limit restrictions."""
        async with aiosqlite.connect(self.db_path) as db:
            # Check user role and limits
            async with db.execute("SELECT role, id_limit FROM users WHERE user_id = ?", (owner_id,)) as cursor:
                user = await cursor.fetchone()
            
            role = user[0] if user else "normal"
            limit = user[1] if user else 5

            # Super owners completely bypass the target ID counts/limits
            if role != "super_owner":
                async with db.execute("SELECT COUNT(*) FROM target_ids WHERE owner_id = ?", (owner_id,)) as cursor:
                    count_row = await cursor.fetchone()
                    count = count_row[0] if count_row else 0
                    if count >= limit:
                        return False  # Limit reached! Reject addition

            # Add the target ID safely
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO target_ids (owner_id, target_id) VALUES (?, ?)", 
                    (owner_id, target_id)
                )
                await db.commit()
                return True
            except Exception:
                return False

    async def get_user_targets(self, user_id: int, role: str) -> list:
        """
        Retrieves targets. 
        - Normal and Owners only see target IDs they registered.
        - Super Owners see all target IDs in the database.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if role == "super_owner":
                async with db.execute("SELECT target_id FROM target_ids") as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]
            else:
                async with db.execute("SELECT target_id FROM target_ids WHERE owner_id = ?", (user_id,)) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]
