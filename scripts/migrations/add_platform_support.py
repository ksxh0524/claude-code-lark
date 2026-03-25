"""Database migration script to add platform support.

Run this script to add the 'platform' column to the users table
for multi-platform support (Telegram, Lark, etc.).

Usage:
    python scripts/migrations/add_platform_support.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


async def migrate():
    """Run the migration."""
    from aiosqlite import connect
    from src.config.settings import Settings

    # Load settings
    settings = Settings()

    # Get database path
    db_path = settings.database_path
    if not db_path:
        print("Error: Could not determine database path from settings")
        return False

    print(f"Migrating database: {db_path}")

    async with connect(db_path) as db:
        # Check if platform column already exists
        cursor = await db.execute(
            "PRAGMA table_info(users)"
        )
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]

        if "platform" in column_names:
            print("✓ Platform column already exists in users table")
            return True

        # Add platform column to users table
        print("Adding platform column to users table...")
        await db.execute(
            "ALTER TABLE users ADD COLUMN platform TEXT DEFAULT 'telegram'"
        )

        # Rename telegram_username to platform_username for clarity
        if "telegram_username" in column_names:
            print("Renaming telegram_username to platform_username...")
            await db.execute(
                "ALTER TABLE users RENAME COLUMN telegram_username TO platform_username"
            )

        # Create index on platform for faster queries
        print("Creating index on platform column...")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_platform ON users(platform)"
        )

        # Update existing users to have platform='telegram'
        print("Updating existing users with platform='telegram'...")
        await db.execute(
            "UPDATE users SET platform = 'telegram' WHERE platform IS NULL"
        )

        await db.commit()

        print("✓ Migration completed successfully")
        print("\nDatabase schema updated:")
        print("  - Added 'platform' column to users table")
        print("  - Renamed 'telegram_username' to 'platform_username'")
        print("  - Created index on platform column")
        print("  - Set default platform='telegram' for existing users")

        return True


async def rollback():
    """Rollback the migration."""
    from aiosqlite import connect
    from src.config.settings import Settings

    settings = Settings()
    db_path = settings.database_path
    if not db_path:
        print("Error: Could not determine database path from settings")
        return False

    print(f"Rolling back migration: {db_path}")

    async with connect(db_path) as db:
        # Check if platform column exists
        cursor = await db.execute(
            "PRAGMA table_info(users)"
        )
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]

        if "platform" not in column_names:
            print("✓ Platform column does not exist (nothing to rollback)")
            return True

        # SQLite doesn't support DROP COLUMN directly, so we need to recreate table
        print("Warning: SQLite has limited ALTER TABLE support.")
        print("To rollback, you need to manually recreate the users table without platform column.")
        print("\nSuggested steps:")
        print("  1. Export data: sqlite3 bot.db '.dump users' > users.sql")
        print("  2. Edit users.sql to remove platform column")
        print("  3. Drop table: DROP TABLE users;")
        print("  4. Import: sqlite3 bot.db < users.sql")

        return False


async def show_status():
    """Show current migration status."""
    from aiosqlite import connect
    from src.config.settings import Settings

    settings = Settings()
    db_path = settings.database_path
    if not db_path:
        print("Error: Could not determine database path from settings")
        return

    print(f"Database: {db_path}\n")

    async with connect(db_path) as db:
        # Show users table schema
        cursor = await db.execute(
            "PRAGMA table_info(users)"
        )
        columns = await cursor.fetchall()
        print("Users table schema:")
        for col in columns:
            col_id, name, type_, notnull, default, pk = col
            required = "NOT NULL" if notnull else ""
            primary = "PRIMARY KEY" if pk else ""
            default_val = f"DEFAULT {default}" if default else ""
            print(f"  - {name}: {type_} {required} {default_val} {primary}".strip())

        # Show platform distribution
        cursor = await db.execute(
            "SELECT platform, COUNT(*) as count FROM users GROUP BY platform"
        )
        rows = await cursor.fetchall()
        if rows:
            print("\nPlatform distribution:")
            for platform, count in rows:
                print(f"  - {platform or 'NULL'}: {count} users")
        else:
            print("\nNo users in database")


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Database migration for multi-platform support"
    )
    parser.add_argument(
        "action",
        choices=["migrate", "rollback", "status"],
        help="Action to perform",
    )

    args = parser.parse_args()

    if args.action == "migrate":
        success = await migrate()
        sys.exit(0 if success else 1)
    elif args.action == "rollback":
        success = await rollback()
        sys.exit(0 if success else 1)
    elif args.action == "status":
        await show_status()


if __name__ == "__main__":
    asyncio.run(main())
