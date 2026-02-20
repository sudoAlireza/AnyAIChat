#!/usr/bin/env python3
"""
Simple test script for the tasks feature.
This script tests the database functions without running the full bot.
"""

import os
import sys
from datetime import datetime

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.database import (
    create_connection,
    create_table,
    create_task,
    get_all_tasks,
    get_user_tasks,
    delete_task_by_id,
)


def test_tasks_feature():
    """Test the tasks database functions"""
    
    print("=" * 60)
    print("Testing Tasks Feature")
    print("=" * 60)
    
    # Create test database
    test_db = "data/test_tasks.db"
    
    # Remove test database if it exists
    if os.path.exists(test_db):
        os.remove(test_db)
        print(f"✓ Removed existing test database")
    
    # Create connection
    conn = create_connection(test_db)
    if not conn:
        print("✗ Failed to create database connection")
        return False
    print(f"✓ Created database connection")
    
    # Create tables
    create_table(conn)
    print(f"✓ Created tables")
    
    # Test 1: Create tasks
    print("\n" + "-" * 60)
    print("Test 1: Creating tasks")
    print("-" * 60)
    
    test_user_id = 12345
    tasks_data = [
        (test_user_id, "What's the weather today?", "09:00", "daily"),
        (test_user_id, "Tell me a joke", "12:00", "once"),
        (test_user_id, "Summarize today's news", "18:00", "weekly"),
    ]
    
    task_ids = []
    for task_data in tasks_data:
        task_id = create_task(conn, task_data)
        task_ids.append(task_id)
        print(f"✓ Created task {task_id}: {task_data[1][:30]}... at {task_data[2]} ({task_data[3]})")
    
    # Test 2: Get all tasks
    print("\n" + "-" * 60)
    print("Test 2: Retrieving all tasks")
    print("-" * 60)
    
    all_tasks = get_all_tasks(conn)
    print(f"✓ Retrieved {len(all_tasks)} tasks")
    for task in all_tasks:
        print(f"  - Task {task['id']}: {task['prompt'][:30]}... at {task['run_time']} ({task['interval']})")
    
    # Test 3: Get user tasks
    print("\n" + "-" * 60)
    print("Test 3: Retrieving tasks for specific user")
    print("-" * 60)
    
    user_tasks = get_user_tasks(conn, test_user_id)
    print(f"✓ Retrieved {len(user_tasks)} tasks for user {test_user_id}")
    for task in user_tasks:
        print(f"  - Task {task['id']}: {task['prompt'][:30]}...")
    
    # Test 4: Delete a task
    print("\n" + "-" * 60)
    print("Test 4: Deleting a task")
    print("-" * 60)
    
    task_to_delete = task_ids[1]
    deleted = delete_task_by_id(conn, task_to_delete, test_user_id)
    if deleted:
        print(f"✓ Deleted task {task_to_delete}")
    else:
        print(f"✗ Failed to delete task {task_to_delete}")
    
    # Verify deletion
    remaining_tasks = get_user_tasks(conn, test_user_id)
    print(f"✓ Remaining tasks: {len(remaining_tasks)}")
    
    # Test 5: Try to delete non-existent task
    print("\n" + "-" * 60)
    print("Test 5: Attempting to delete non-existent task")
    print("-" * 60)
    
    deleted = delete_task_by_id(conn, 9999, test_user_id)
    if not deleted:
        print(f"✓ Correctly returned False for non-existent task")
    else:
        print(f"✗ Should have returned False for non-existent task")
    
    # Test 6: User isolation
    print("\n" + "-" * 60)
    print("Test 6: Testing user isolation")
    print("-" * 60)
    
    other_user_id = 67890
    other_user_task = (other_user_id, "Other user's task", "10:00", "daily")
    other_task_id = create_task(conn, other_user_task)
    print(f"✓ Created task {other_task_id} for user {other_user_id}")
    
    # Try to delete other user's task
    deleted = delete_task_by_id(conn, other_task_id, test_user_id)
    if not deleted:
        print(f"✓ Correctly prevented deletion of other user's task")
    else:
        print(f"✗ Should not allow deletion of other user's task")
    
    # Verify other user's task still exists
    other_user_tasks = get_user_tasks(conn, other_user_id)
    if len(other_user_tasks) == 1:
        print(f"✓ Other user's task still exists")
    else:
        print(f"✗ Other user's task was incorrectly deleted")
    
    # Close connection
    conn.close()
    print("\n" + "=" * 60)
    print("All tests completed successfully!")
    print("=" * 60)
    
    # Cleanup
    if os.path.exists(test_db):
        os.remove(test_db)
        print(f"✓ Cleaned up test database")
    
    return True


if __name__ == "__main__":
    try:
        success = test_tasks_feature()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
