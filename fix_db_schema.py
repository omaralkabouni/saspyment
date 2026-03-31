import sqlite3
import os

DB_PATH = 'payments.db'

def fix_db():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA table_info(payments_v3)")
        columns = [row[1] for row in cursor.fetchall()]
        print(f"Current columns: {columns}")
        
        if 'payment_method' not in columns:
            print("Adding payment_method...")
            cursor.execute("ALTER TABLE payments_v3 ADD COLUMN payment_method TEXT DEFAULT 'Cash'")
        
        if 'receipt_number' not in columns:
            print("Adding receipt_number...")
            cursor.execute("ALTER TABLE payments_v3 ADD COLUMN receipt_number TEXT")
            
        conn.commit()
        print("Schema update completed successfully.")
    except Exception as e:
        print(f"Error updating schema: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    fix_db()
