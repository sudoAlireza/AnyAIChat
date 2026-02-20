#!/usr/bin/env python3
"""
Database migration script for tasks table.
This script will drop the old tasks table (if it exists) and create a new one with the correct schema.
"""

import os
import sys
import sqlite3
from datetime import datetime

def migrate_database(db_path):
    """Migrate the tasks table to the new schema"""
    
    print("=" * 60)
    print("Tasks Table Migration Script")
    print("=" * 60)
    print()
    
    if not os.path.exists(db_path):
        print(f"✗ Database not found at: {db_path}")
        print("  The database will be created when you run the bot.")
        return True
    
    # Backup the database first
    backup_path = f"{db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        import shutil
        shutil.copy2(db_path, backup_path)
        print(f"✓ Created backup at: {backup_path}")
    except Exception as e:
        print(f"✗ Failed to create backup: {e}")
        response = input("Continue without backup? (yes/no): ")
        if response.lower() != 'yes':
            return False
    
    # Connect to database
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        print(f"✓ Connected to database")
    except Exception as e:
        print(f"✗ Failed to connect to database: {e}")
        return False
    
    # Check if tasks table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks';")
    table_exists = cursor.fetchone() is not None
    
    if table_exists:
        print(f"✓ Found existing tasks table")
        
        # Get current schema
        cursor.execute("PRAGMA table_info(tasks);")
        columns = cursor.fetchall()
        print(f"  Current columns: {[col[1] for col in columns]}")
        
        # Check if chat_id column exists
        has_chat_id = any(col[1] == 'chat_id' for col in columns)
        
        if has_chat_id:
            print(f"  ⚠ Table has old schema with 'chat_id' column")
            
            # Check if there are any existing tasks
            cursor.execute("SELECT COUNT(*) FROM tasks;")
            task_count = cursor.fetchone()[0]
            
            if task_count > 0:
                print(f"  ⚠ Found {task_count} existing tasks")
                response = input("  Drop old table and lose existing tasks? (yes/no): ")
                if response.lower() != 'yes':
                    print("  Migration cancelled")
                    conn.close()
                    return False
            
            # Drop old table
            print(f"  Dropping old tasks table...")
            cursor.execute("DROP TABLE tasks;")
            conn.commit()
            print(f"  ✓ Dropped old table")
        else:
            print(f"  ✓ Table already has correct schema")
            conn.close()
            return True
    else:
        print(f"  No existing tasks table found")
    
    # Create new table with correct schema
    print(f"  Creating new tasks table...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            user_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            run_time TEXT NOT NULL,
            interval TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    print(f"  ✓ Created new tasks table")
    
    # Verify new schema
    cursor.execute("PRAGMA table_info(tasks);")
    columns = cursor.fetchall()
    print(f"  New columns: {[col[1] for col in columns]}")
    
    conn.close()
    
    print()
    print("=" * 60)
    print("Migration completed successfully!")
    print("=" * 60)
    print()
    print("You can now run the bot with: python main.py")
    print()
    
    return True


if __name__ == "__main__":
    database_path = "data/conversations_data.db"
    
    if len(sys.argv) > 1:
        database_path = sys.argv[1]
    
    try:
        success = migrate_database(database_path)
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ Migration failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
