import sqlite3
import os

DB_PATH = 'payments.db'

def migrate():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # Check if column exists
        cursor.execute("PRAGMA table_info(payments_v3)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'payment_method' not in columns:
            print("Adding payment_method column to payments_v3...")
            cursor.execute("ALTER TABLE payments_v3 ADD COLUMN payment_method TEXT DEFAULT 'Cash'")
            conn.commit()
            print("payment_method column added.")
        
        if 'receipt_number' not in columns:
            print("Adding receipt_number column to payments_v3...")
            cursor.execute("ALTER TABLE payments_v3 ADD COLUMN receipt_number TEXT")
            conn.commit()
            print("receipt_number column added.")
        
        if not any(col not in columns for col in ['payment_method', 'receipt_number']):
            print("All columns already exist.")
        else:
            print("Migration successful.")
    except Exception as e:
        print(f"Migration error: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    migrate()
