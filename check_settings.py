import sqlite3
import os

db_path = os.getenv('DB_PATH', 'payments.db')
if not os.path.exists(db_path):
    print(f"Error: Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM settings WHERE key LIKE 'webhook%';").fetchall()
    for row in rows:
        print(f"{row['key']}: {row['value']}")
    conn.close()
