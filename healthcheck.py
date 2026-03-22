"""Docker health check script.

Verifies database connectivity and basic system health.
Exit code 0 = healthy, 1 = unhealthy.
"""

import sys
import sqlite3
from config import DATABASE_PATH


def check_health():
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        conn.close()

        if result and result[0] == 1:
            print("healthy")
            return 0
        else:
            print("unhealthy: unexpected query result")
            return 1

    except Exception as e:
        print(f"unhealthy: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(check_health())
