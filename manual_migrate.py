import sqlite3
import os

DB_PATH = r'f:\Downloads\zawdni appp\app sas\payments.db'

def migrate():
    if not os.path.exists(DB_PATH):
        print("DB not found")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    migrations = [
        ("installations", "is_verified", "INTEGER DEFAULT 0"),
        ("installations", "verified_by", "TEXT"),
        ("installations", "verified_at", "TIMESTAMP"),
        ("installations", "verification_notes", "TEXT")
    ]
    
    for table, column, col_type in migrations:
        try:
            print(f"Adding column {column} to {table}...")
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"Column {column} added.")
        except sqlite3.OperationalError as e:
            print(f"Skipping {column}: {e}")
            
    conn.commit()
    conn.close()
    print("Migration finished.")

if __name__ == "__main__":
    migrate()
