#!/bin/bash

echo "=========================================="
echo "Verifying Tasks Feature Installation"
echo "=========================================="
echo ""

# Check Python version
echo "1. Checking Python version..."
python --version
echo ""

# Check if requirements.txt exists
echo "2. Checking requirements.txt..."
if [ -f "requirements.txt" ]; then
    echo "✓ requirements.txt found"
    if grep -q "APScheduler" requirements.txt; then
        echo "✓ APScheduler listed in requirements.txt"
    else
        echo "✗ APScheduler not found in requirements.txt"
    fi
else
    echo "✗ requirements.txt not found"
fi
echo ""

# Check if database functions exist
echo "3. Checking database functions..."
python -c "
import sys
sys.path.insert(0, '.')
try:
    from database.database import create_task, get_all_tasks, get_user_tasks, delete_task_by_id
    print('✓ All database functions imported successfully')
except ImportError as e:
    print(f'✗ Failed to import database functions: {e}')
"
echo ""

# Check if handler functions exist
echo "4. Checking handler functions..."
python -c "
import sys
sys.path.insert(0, '.')
try:
    from bot.conversation_handlers import (
        open_tasks_menu,
        start_add_task,
        handle_task_prompt,
        handle_task_time,
        handle_task_interval,
        list_tasks,
        delete_task_handler,
        set_scheduler,
        schedule_task_job,
        send_scheduled_task
    )
    print('✓ All handler functions imported successfully')
except ImportError as e:
    print(f'✗ Failed to import handler functions: {e}')
"
echo ""

# Check if translation files exist
echo "5. Checking translation files..."
if [ -f "locales/en/LC_MESSAGES/messages.mo" ]; then
    echo "✓ English translations compiled"
else
    echo "✗ English translations not compiled"
fi

if [ -f "locales/ru/LC_MESSAGES/messages.mo" ]; then
    echo "✓ Russian translations compiled"
else
    echo "✗ Russian translations not compiled"
fi
echo ""

# Check if documentation exists
echo "6. Checking documentation..."
docs=("TASKS_FEATURE.md" "SETUP_TASKS.md" "TASKS_ARCHITECTURE.md" "IMPLEMENTATION_SUMMARY.md")
for doc in "${docs[@]}"; do
    if [ -f "$doc" ]; then
        echo "✓ $doc exists"
    else
        echo "✗ $doc not found"
    fi
done
echo ""

echo "=========================================="
echo "Installation Verification Complete"
echo "=========================================="
echo ""
echo "To install dependencies, run:"
echo "  pip install -r requirements.txt"
echo ""
echo "To test the feature, run:"
echo "  python test_tasks.py"
echo ""
