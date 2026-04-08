import sqlite3
import os

DB_PATH = r'f:\Downloads\zawdni appp\app sas\payments.db'

def check_db():
    if not os.path.exists(DB_PATH):
        print("DB not found at:", DB_PATH)
        return
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Check columns in installations table
    c.execute("PRAGMA table_info(installations)")
    columns = [row[1] for row in c.fetchall()]
    print("Columns in 'installations':")
    for col in columns:
        print("-", col)
        
    # Check latest 5 records
    c.execute("SELECT id, fullname, status, is_verified, verified_by FROM installations ORDER BY id DESC LIMIT 5")
    rows = c.fetchall()
    print("\nLatest 5 Installations:")
    for row in rows:
        print(dict(row))
    
    conn.close()

if __name__ == "__main__":
    check_db()
