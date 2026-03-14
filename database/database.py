import sqlite3
import json
from sqlite3 import Error


def create_connection(db_file):
    """create a database connection to the SQLite database
        specified by db_file
    :param db_file: database file
    :return: Connection object or None
    """
    conn = None
    try:
        conn = sqlite3.connect(db_file)
    except Error as e:
        print(e)

    return conn


def create_table(conn):
    """create tables for conversations and tasks
    :param conn: Connection object
    :return:
    """
    try:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                conv_id TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                history TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                run_time TEXT NOT NULL,
                interval TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
    except Error as e:
        print(e)


def create_conversation(conn, conversation):
    """
    Create a new conversation or update history if conv_id exists
    :param conn:
    :param conversation: (conv_id, user_id, title, history_json)
    :return: conversation id
    """
    sql = """ INSERT INTO conversations(conv_id, user_id, title, history)
              VALUES(?,?,?,?)
              ON CONFLICT(conv_id) DO UPDATE SET
              history=excluded.history,
              title=excluded.title """
    cur = conn.cursor()
    cur.execute(sql, conversation)
    conn.commit()
    return cur.lastrowid


def get_user_conversation_count(conn, user_id):
    """
    Query count of all conversations for each user
    :param conn: the Connection object
    :param user_id:
    :return count of conversations
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM conversations WHERE user_id=?;",
        (user_id,),
    )

    conv_count = cur.fetchone()
    if conv_count:
        return conv_count[0]

    return 0


def select_conversations_by_user(conn, conversation_page):
    """
    Query conversations for each user by limit and offset
    :param conn: the Connection object
    :param conversation_page: (user_id, offset)
    :return list of conversations
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, conv_id, user_id, title FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 10 OFFSET ?;",
        conversation_page,
    )

    results = cur.fetchall()

    return [
        {
            "id": item[0],
            "conversation_id": item[1],
            "user_id": item[2],
            "title": item[3],
        }
        for item in results
    ]


def select_conversation_by_id(conn, conversation):
    """
    Query conversation by conv_id
    :param conn: the Connection object
    :param conversation: (user_id, conv_id):
    :return conversation dict
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT conv_id, title, history FROM conversations WHERE user_id=? AND conv_id=?;",
        conversation,
    )

    item = cur.fetchone()
    if item:
        return {"conv_id": item[0], "title": item[1], "history": item[2]}
    return None


def delete_conversation_by_id(conn, conversation):
    """
    Delete conversation by conv_id
    :param conn: the Connection object
    :param conversation: (user_id, conv_id):
    :return: True if deleted
    """
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM conversations WHERE user_id=? AND conv_id=?;", conversation
    )
    conn.commit()
    return cur.rowcount > 0


# --- Task Functions ---

def create_task(conn, task):
    """
    Create a new task
    :param conn:
    :param task: (user_id, prompt, run_time, interval)
    :return: task id
    """
    sql = """ INSERT INTO tasks(user_id, prompt, run_time, interval)
              VALUES(?,?,?,?) """
    cur = conn.cursor()
    cur.execute(sql, task)
    conn.commit()
    return cur.lastrowid


def get_all_tasks(conn):
    """
    Retrieve all tasks from the database
    :param conn:
    :return: list of tasks
    """
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, prompt, run_time, interval FROM tasks")
    results = cur.fetchall()
    return [
        {
            "id": item[0],
            "user_id": item[1],
            "prompt": item[2],
            "run_time": item[3],
            "interval": item[4],
        }
        for item in results
    ]


def get_user_tasks(conn, user_id):
    """
    Retrieve tasks for a specific user
    :param conn:
    :param user_id:
    :return: list of tasks
    """
    cur = conn.cursor()
    cur.execute("SELECT id, prompt, run_time, interval FROM tasks WHERE user_id=?", (user_id,))
    results = cur.fetchall()
    return [
        {
            "id": item[0],
            "prompt": item[1],
            "run_time": item[2],
            "interval": item[3],
        }
        for item in results
    ]


def delete_task_by_id(conn, task_specs):
    """
    Delete a task by its ID and user ID
    :param conn:
    :param task_specs: (user_id, task_id)
    :return: True if deleted
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE user_id=? AND id=?", task_specs)
    conn.commit()
    return cur.rowcount > 0
