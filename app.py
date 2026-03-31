import sqlite3
import csv
import io
import math
import time
import os
import shutil
import random
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, send_file, send_from_directory, make_response
from sas import SasAPI
import json
import aes
import requests
import threading
import uuid
from typing import Any
from dotenv import load_dotenv

# Load environment variables from .env file for local development
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'super_secret_sas_dashboard_key_123')

# PWA Routes
@app.route('/sw.js')
def sw():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route('/manifest.json')
def manifest():
    response = make_response(send_from_directory('static', 'manifest.json'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.after_request
def add_header(response):
    """
    Add headers to both force latest IE rendering engine or Chrome Frame,
    and also to cache the rendered page for 10 minutes.
    """
    if 'Cache-Control' not in response.headers:
        if request.path.startswith('/static/'):
            # Cache static assets for 1 year
            response.headers['Cache-Control'] = 'public, max-age=31536000'
        else:
            # Do not cache dynamic content
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

# Initialize the SAS API client with the user's server IP
# SAS Radius Configuration
SAS_API_IP = os.getenv('SAS_API_IP', '193.43.140.218')
SAS_ADMIN_USER = os.getenv('SAS_ADMIN_USER', 'Top')
SAS_ADMIN_PASS = os.getenv('SAS_ADMIN_PASS', 'omar@123')
SPECIAL_LOGIN_USER = os.getenv('SPECIAL_LOGIN_USER', 'maram')
SPECIAL_LOGIN_PASS = os.getenv('SPECIAL_LOGIN_PASS', 'm@123')

sasclient = SasAPI(f"https://{SAS_API_IP}", portal='admin')
subscriber_client = SasAPI(f"https://{SAS_API_IP}", portal='user')

# Webhook for n8n/WhatsApp (Set this in environment variables)
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')

# Database Path
DB_PATH = os.getenv('DB_PATH', 'payments.db')
# Ensure BACKUP_DIR is relative to DB_PATH directory for persistence in Docker
BACKUP_DIR = os.getenv('BACKUP_DIR', os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'backups'))

if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

USER_CACHE: dict[str, Any] = {
    'data': None,
    'total': 0,
    'timestamp': 0.0,
    'is_refreshing': False
}
CACHE_DURATION = 1800 # 30 minutes background revalidation (Non-blocking)

DELETE_PASS_FILE = 'delete_password.txt'

def get_or_create_delete_password():
    if not os.path.exists(DELETE_PASS_FILE):
        pwd = "admin" + str(random.randint(100, 999))
        with open(DELETE_PASS_FILE, 'w') as f:
            f.write(pwd)
        return pwd
    with open(DELETE_PASS_FILE, 'r') as f:
        return f.read().strip()

get_or_create_delete_password()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 1. Create ALL tables if they don't exist
    c.execute('''
        CREATE TABLE IF NOT EXISTS payments_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            fullname TEXT,
            profile_name TEXT,
            parent TEXT,
            amount REAL NOT NULL,
            admin_name TEXT NOT NULL,
            phone TEXT, 
            public_token TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS installations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT NOT NULL,
            phone1 TEXT NOT NULL,
            phone2 TEXT,
            area TEXT,
            address_details TEXT,
            notes TEXT,
            status TEXT DEFAULT 'Pending',
            assigned_to TEXT,
            registered_by TEXT,
            payment_amount REAL,
            payment_notes TEXT,
            connection_type TEXT,
            dish_ip TEXT,
            payment_amount_usd REAL DEFAULT 0,
            payment_amount_syp REAL DEFAULT 0,
            public_token TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS subscribers (
            username TEXT PRIMARY KEY,
            password TEXT,
            firstname TEXT,
            lastname TEXT,
            mobile TEXT,
            profile TEXT,
            expiration TEXT,
            status INTEGER,
            parent_username TEXT,
            json_data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            maintenance_id TEXT,
            phone TEXT,
            parent TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            fullname TEXT,
            phone1 TEXT NOT NULL,
            phone2 TEXT,
            area TEXT,
            address_details TEXT,
            complaint_text TEXT NOT NULL,
            status TEXT DEFAULT 'Open',
            maintenance_notes TEXT,
            maintenance_user_id TEXT,
            assigned_to TEXT,
            registered_by TEXT,
            connection_type TEXT,
            dish_ip TEXT,
            parent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS complaint_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id INTEGER NOT NULL,
            action_by TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (complaint_id) REFERENCES complaints (id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriber_info (
            username TEXT PRIMARY KEY,
            connection_type TEXT,
            dish_ip TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS sas_cache (
            id INTEGER PRIMARY KEY,
            json_data TEXT,
            total INTEGER,
            updated_at REAL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS landing_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            speed TEXT,
            price_syp TEXT,
            price_usd TEXT,
            description TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            admin_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT DEFAULT '',
            cost_price REAL DEFAULT 0,
            sell_price REAL DEFAULT 0,
            stock_qty INTEGER DEFAULT 0,
            min_stock INTEGER DEFAULT 0,
            unit TEXT DEFAULT 'قطعة',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            cost_price REAL NOT NULL,
            total_cost REAL NOT NULL,
            supplier TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            admin_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES inventory_items (id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            sell_price REAL NOT NULL,
            discount REAL DEFAULT 0,
            total_amount REAL NOT NULL,
            installation_id INTEGER,
            customer_name TEXT DEFAULT '',
            admin_name TEXT,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES inventory_items (id)
        )
    ''')


    # 2. Run Migrations (Adding columns if they don't exist)
    migrations = [
        ("payments_v3", "phone", "TEXT"),
        ("payments_v3", "public_token", "TEXT"),
        ("users", "phone", "TEXT"),
        ("users", "maintenance_id", "TEXT"),
        ("users", "parent", "TEXT"),
        ("complaints", "assigned_to", "TEXT"),
        ("complaints", "registered_by", "TEXT"),
        ("complaints", "connection_type", "TEXT"),
        ("complaints", "dish_ip", "TEXT"),
        ("complaints", "parent", "TEXT"),
        ("installations", "connection_type", "TEXT"),
        ("installations", "dish_ip", "TEXT"),
        ("installations", "public_token", "TEXT"),
        ("installations", "parent", "TEXT"),
        ("installations", "payment_amount_usd", "REAL DEFAULT 0"),
        ("installations", "payment_amount_syp", "REAL DEFAULT 0")
    ]

    for table, column, col_type in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass # Already exists

    # Special migration: Index for token
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_token ON payments_v3(public_token)")
    except sqlite3.OperationalError:
        pass

    # 3. Seed/Sync missing data
    # Populate missing public_tokens for payments
    rows = c.execute("SELECT id FROM payments_v3 WHERE public_token IS NULL").fetchall()
    for row in rows:
        token = str(uuid.uuid4())
        c.execute("UPDATE payments_v3 SET public_token = ? WHERE id = ?", (token, row[0]))

    # Populate missing public_tokens for installations
    rows = c.execute("SELECT id FROM installations WHERE public_token IS NULL").fetchall()
    for row in rows:
        token = str(uuid.uuid4())
        c.execute("UPDATE installations SET public_token = ? WHERE id = ?", (token, row[0]))

    # Seed default webhooks/settings from ENV if not set
    default_webhook = os.getenv('WEBHOOK_URL', '')
    for key in ['webhook_payments', 'webhook_complaints', 'webhook_installations']:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, default_webhook))
    
    toggles = [
        ('webhook_payments_enabled', '1'),
        ('webhook_complaints_enabled', '1'),
        ('webhook_installations_enabled', '1'),
        ('webhook_complaints_on_new', '1'),
        ('webhook_complaints_on_assign', '1'),
        ('webhook_complaints_on_update', '1'),
        ('webhook_complaints_on_resolve', '1'),
        ('webhook_installations_on_new', '1'),
        ('webhook_installations_on_assign', '1'),
        ('webhook_installations_on_update', '1'),
        ('webhook_installations_on_complete', '1')
    ]
    for key, val in toggles:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    # Default admin user if not exists
    c.execute('SELECT count(*) FROM users WHERE username = "admin"')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', ('admin', 'admin@123', 'admin'))

    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def fetch_all_users_from_api(token, force_refresh=False) -> dict[str, Any]:
    """
    Fetches users from the SAS API with zero UI blocking.
    Always returns current cache (mem/db) immediately, refreshes in background.
    """
    now = time.time()
    
    # 1. Load from DB Cache ONLY if memory is completely empty
    if USER_CACHE['data'] is None:
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT json_data, total, updated_at FROM sas_cache WHERE id = 1").fetchone()
            conn.close()
            if row:
                USER_CACHE['data'] = json.loads(row[0])
                USER_CACHE['total'] = row[1]
                USER_CACHE['timestamp'] = row[2]
                print(f"DEBUG: Initial load: {len(USER_CACHE['data'])} users found in DB cache.")
        except Exception as e:
            print(f"DEBUG: DB Cache error: {e}")

    # 2. Determine if we need to start a background refresh
    should_refresh = force_refresh or (USER_CACHE['data'] is None) or (now - USER_CACHE['timestamp'] > CACHE_DURATION)
    
    if should_refresh and not USER_CACHE['is_refreshing']:
        # Start background thread immediately, no waiting
        threading.Thread(target=background_refresh, args=(token,), daemon=True).start()
    
    # 3. Always return current state immediately
    status = 'online'
    if USER_CACHE['is_refreshing']:
        status = 'refreshing'
    elif USER_CACHE['data'] is not None:
        status = 'cached'
    else:
        status = 'empty_init_sync'

    return {
        'data': USER_CACHE['data'] or [], 
        'total': USER_CACHE['total'], 
        'status': status, 
        'timestamp': USER_CACHE['timestamp']
    }

@app.route('/api/user/<username>')
def get_single_user(username):
    """
    On-demand lookup for a specific user. 
    Checks local cache first, then queries SAS API directly if not found.
    Allows for instant discovery of newly created users.
    """
    if 'token' not in session: return json.dumps({'error': 'Unauthorized'}), 401
    
    username = username.strip()
    # 1. Check current memory cache
    if USER_CACHE['data']:
        found = next((u for u in USER_CACHE['data'] if u.get('username') == username), None)
        if found:
            return json.dumps({'status': 'cached', 'user': found})
    
    # 2. Not in memory? Check DB subscribers table
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT json_data FROM subscribers WHERE username = ?", (username,)).fetchone()
        conn.close()
        if row:
            user_data = json.loads(row['json_data'])
            # Soft update memory if needed
            return json.dumps({'status': 'db_cached', 'user': user_data})
    except Exception as e:
        print(f"DEBUG: DB lookup error: {e}")

    # 3. Not in DB? Query SAS API directly for THIS username
    try:
        payload_dict = {
            "page": 1,
            "count": 1, 
            "search": username,
            "show_password": 1,
        }
        encrypted_payload = aes.encrypt(json.dumps(payload_dict))
        print(f"DEBUG: On-demand SAS lookup for user: {username}")
        response = sasclient.post(session['token'], 'index/user', encrypted_payload)
        
        if isinstance(response, dict) and response.get('data'):
            users = response.get('data') or []
            found_user = next((u for u in users if u.get('username') == username), None)
            
            if found_user:
                # Update DB & Memory Cache locally
                ts = time.time()
                try:
                    conn = sqlite3.connect(DB_PATH)
                    pwd = (found_user.get('password') or found_user.get('plain_password') or found_user.get('user_password') or '')
                    conn.execute('''
                        INSERT OR REPLACE INTO subscribers 
                        (username, password, firstname, lastname, mobile, profile, expiration, status, parent_username, json_data, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        found_user.get('username'), pwd, found_user.get('firstname'), found_user.get('lastname'), found_user.get('mobile') or found_user.get('phone'),
                        found_user.get('profile_details', {}).get('name') if isinstance(found_user.get('profile_details'), dict) else '',
                        found_user.get('expiration'), 1 if (isinstance(found_user.get('status'), dict) and found_user.get('status').get('status')) else 0,
                        found_user.get('parent_username'), json.dumps(found_user), ts
                    ))
                    conn.commit()
                    conn.close()
                    
                    # Update memory cache if it exists
                    if USER_CACHE['data'] is not None:
                        # Replace or append
                        idx = next((i for i, u in enumerate(USER_CACHE['data']) if u.get('username') == username), -1)
                        if idx >= 0: USER_CACHE['data'][idx] = found_user
                        else: USER_CACHE['data'].append(found_user)
                except Exception as db_e:
                    print(f"DEBUG: Async single-user save error: {db_e}")
                
                return json.dumps({'status': 'fresh', 'user': found_user})
    except Exception as e:
        print(f"DEBUG: On-demand lookup Exception: {e}")

    return json.dumps({'status': 'not_found', 'message': 'User not found in SAS'}), 404

def background_refresh(token_inner):
    """Heavy lifting is done here, outside the main request/response cycle."""
    if USER_CACHE['is_refreshing']: return
    USER_CACHE['is_refreshing'] = True
    try:
        payload_dict = {
            "page": 1,
            "count": 5000, 
            "sortBy": "username",
            "direction": "asc",
            "search": "",
            "show_password": 1,
            "show_passwords": 1, 
            "plain_password": 1,
            "with_password": 1,
        }
        encrypted_payload = aes.encrypt(json.dumps(payload_dict))
        print(f"DEBUG: Sync started (background thread)...")
        response = sasclient.post(token_inner, 'index/user', encrypted_payload)
        
        if isinstance(response, dict) and 'data' in response:
            users = response.get('data') or []
            total = response.get('total') or 0
            ts = time.time()
            
            # Update memory cache
            USER_CACHE['data'] = users
            USER_CACHE['total'] = total
            USER_CACHE['timestamp'] = ts
            
            # Update persistent DB cache
            try:
                conn = sqlite3.connect(DB_PATH)
                json_str = json.dumps(users)
                conn.execute("INSERT OR REPLACE INTO sas_cache (id, json_data, total, updated_at) VALUES (1, ?, ?, ?)", (json_str, total, ts))
                
                # Update individual subscribers table (Offline persistence for public invoices/details)
                for u in users:
                    pwd = (u.get('password') or u.get('plain_password') or u.get('user_password') or '')
                    conn.execute('''
                        INSERT OR REPLACE INTO subscribers 
                        (username, password, firstname, lastname, mobile, profile, expiration, status, parent_username, json_data, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        u.get('username'), pwd, u.get('firstname'), u.get('lastname'), u.get('mobile') or u.get('phone'),
                        u.get('profile_details', {}).get('name') if isinstance(u.get('profile_details'), dict) else '',
                        u.get('expiration'), 1 if (isinstance(u.get('status'), dict) and u.get('status').get('status')) else 0,
                        u.get('parent_username'), json.dumps(u), ts
                    ))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"DEBUG: Failed to save async cache to DB: {e}")
            print(f"DEBUG: Sync complete! {len(users)} users stored.")
        else:
            print(f"DEBUG: Sync failed: {response}")
    except Exception as e:
        print(f"DEBUG: Sync Exception: {e}")
    finally:
        USER_CACHE['is_refreshing'] = False

def send_webhook_async(data, webhook_type='payments', event_name=None, base_url=None):
    """Wrapper to run send_webhook in a background thread."""
    threading.Thread(target=send_webhook, args=(data, None, webhook_type, event_name, base_url), daemon=True).start()

def send_webhook(data, webhook_url_override=None, webhook_type='payments', event_name=None, base_url=None):
    """Send data to n8n webhook for WhatsApp notifications."""
    conn = get_db_connection()
    
    # 1. Check if this webhook channel is enabled
    enabled_key = f'webhook_{webhook_type}_enabled'
    is_enabled = conn.execute("SELECT value FROM settings WHERE key = ?", (enabled_key,)).fetchone()
    if is_enabled and is_enabled['value'] == '0':
        conn.close()
        return False, "Disabled"

    # 2. Check if this specific event is enabled
    if event_name:
        event_key = f'webhook_{webhook_type}_on_{event_name}'
        is_event_enabled = conn.execute("SELECT value FROM settings WHERE key = ?", (event_key,)).fetchone()
        if is_event_enabled and is_event_enabled['value'] == '0':
            conn.close()
            return False, f"Event {event_name} disabled"

    # 3. Get URL
    if webhook_url_override:
        webhook_url = webhook_url_override
    else:
        key = f'webhook_{webhook_type}'
        webhook_setting = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        webhook_url = webhook_setting['value'] if (webhook_setting and webhook_setting['value']) else WEBHOOK_URL
    
    conn.close()
    
    if not webhook_url or not webhook_url.startswith('http'):
        return False, f"Invalid or empty URL: '{webhook_url}'"
        
    try:
        # Prepare a clean dictionary for the webhook based on type
        if webhook_type == 'payments':
            public_token = data.get('public_token', '')
            public_url = f"{base_url}v/{public_token}" if (base_url and public_token) else ""
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={public_url}" if public_url else ""
            
            payload = {
                "id": data.get('id', 'test_id'),
                "invoice_no": f"SAS-{data.get('id', '0000')}",
                "username": data.get('username', 'test_user'),
                "fullname": data.get('fullname', 'Test User'),
                "profile_name": data.get('profile_name', 'Test Profile'),
                "amount": data.get('amount', '0'),
                "phone": data.get('phone', '0900000000'),
                "admin_name": data.get('admin_name', 'Admin'),
                "public_invoice_url": public_url,
                "qr_code_url": qr_url,
                "message": f"✅ تم استلام دفعة بقيمة {data.get('amount', '0')} ل.س للحساب {data.get('username', '')}. شكراً لتعاملكم معنا (TopNet). \nرابط الفاتورة: {public_url}",
                "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "status": data.get('status', "paid"),
                "event": event_name or "new_payment"
            }
        elif webhook_type == 'complaints':
            event = event_name or "new"
            if event == 'resolve':
                msg = f"✅ تم حل الشكوى بنجاح للمشترك {data.get('username', '')}. ملاحظات: {data.get('notes', '')}"
                target_phone = data.get('phone', '') # Customer phone
            elif event == 'assign':
                msg = f"📌 مهمة جديدة: تم تعيين الشكوى رقم {data.get('id', '')} للمشترك {data.get('username', '')} إليك. يرجى المتابعة."
                target_phone = data.get('employee_phone', '') # Employee phone
            elif event == 'update':
                msg = f"🔄 تحديث في الشكوى: تم التحديث على شكوى المشترك {data.get('username', '')}. ملاحظات: {data.get('notes', '')}"
                target_phone = data.get('employee_phone', '') or data.get('phone', '') # Depends on context
            else:
                msg = f"⚠️ شكوى جديدة: المشترك {data.get('username', '')} أبلغ عن: {data.get('text', '')}"
                target_phone = data.get('phone', '') # Customer phone
                
            payload = {
                "id": data.get('id', 'test_id'),
                "username": data.get('username', 'test_user'),
                "fullname": data.get('fullname', 'Test User'),
                "phone": target_phone,
                "customer_phone": data.get('phone', ''),
                "employee_phone": data.get('employee_phone', ''),
                "employee_name": data.get('employee_name', ''),
                "is_assigned": bool(data.get('employee_name')),
                "area": data.get('area', ''),
                "text": data.get('text', ''),
                "notes": data.get('notes', ''),
                "status": data.get('status', 'Open'),
                "message": msg,
                "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "event": event
            }
        elif webhook_type == 'installations':
            event = event_name or "new"
            public_token = data.get('public_token', '')
            public_url = f"{base_url}vi/{public_token}" if (base_url and public_token) else ""
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={public_url}" if public_url else ""

            if event == 'complete':
                msg = f"✅ تم استكمال طلب تركيب للمشترك {data.get('fullname', '')} وإتمامه بنجاح."
            elif event == 'assign':
                msg = f"📌 مهمة جديدة: تم تعيين طلب تركيب للمشترك {data.get('fullname', '')} إليك. يرجى المتابعة."
            elif event == 'update':
                msg = f"🔄 تحديث في طلب التركيب: تم تحديث طلب المشترك {data.get('fullname', '')}."
            else:
                msg = f"⚠️ طلب تركيب جديد: باسم {data.get('fullname', '')}. للتحقق والمتابعة."
                
            payload = {
                "id": data.get('id', 'test_id'),
                "fullname": data.get('fullname', 'Test User'),
                "phone": data.get('phone', ''),
                "employee_phone": data.get('employee_phone', ''),
                "employee_name": data.get('employee_name', ''),
                "is_assigned": bool(data.get('employee_name')),
                "area": data.get('area', ''),
                "address": data.get('address', ''),
                "notes": data.get('notes', ''),
                "public_invoice_url": public_url,
                "qr_code_url": qr_url,
                "amount_usd": data.get('amount_usd', '0'),
                "amount_syp": data.get('amount_syp', '0'),
                "connection": data.get('connection_type', ''),
                "dish_ip": data.get('dish_ip', ''),
                "status": data.get('status', 'Pending'),
                "message": f"{msg} \nرابط التفاصيل: {public_url}",
                "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "event": event
            }
        else:
            payload = data
            payload['event'] = event_name or "unknown"

        requests.post(webhook_url, json=payload, timeout=10, verify=False)
        return True, "OK"
    except Exception as e:
        print(f"Webhook Error ({webhook_type}): {e}")
        return False, str(e)

@app.route('/settings/sync')
def force_sync():
    """Manual trigger to refresh the SAS user cache."""
    if 'token' not in session: return redirect(url_for('login'))
    
    # We allow both Admin and Manager to sync data
    if session.get('role') not in ['admin', 'manager']:
        flash('🚫 Only admins/managers can trigger a full sync.', 'error')
        return redirect(url_for('dashboard'))
        
    fetch_all_users_from_api(session['token'], force_refresh=True)
    flash('🔄 SAS Data Synchronization manually triggered. Data will update in a few seconds.', 'success')
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/image/<path:filename>')
def serve_image(filename):
    return send_from_directory('image', filename)

@app.route('/')
def landing():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'token' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        auth_username = username
        auth_password = password
        
        if username == SPECIAL_LOGIN_USER and password == SPECIAL_LOGIN_PASS:
            auth_username = SAS_ADMIN_USER
            auth_password = SAS_ADMIN_PASS

        # 1. Check local database for users
        conn = get_db_connection()
        local_user = conn.execute('SELECT * FROM users WHERE username = ? AND password = ?', (username, password)).fetchone()
        conn.close()
        
        if local_user:
            # For local users, we use the background SAS account to get a valid token
            token, error_msg = sasclient.login(username=SAS_ADMIN_USER, password=SAS_ADMIN_PASS)
            if token:
                session['token'] = token
                session['username'] = username
                session['role'] = local_user['role']
                session['parent'] = dict(local_user).get('parent') if dict(local_user).get('parent') else None
                if local_user['role'] == 'maintenance':
                    return redirect(url_for('complaints'))
                return redirect(url_for('dashboard'))
            else:
                flash(f'🚨 SAS API Connection Error: {error_msg or "Local login failed because SAS is unreachable."}', 'error')
                return redirect(url_for('login'))

        # 2. If not in local db, check SAS API directly
        token, error_msg = sasclient.login(username=auth_username, password=auth_password)
        if token:
            session['token'] = token
            session['username'] = username 
            # Default role for SAS users is 'employee' unless they exist in local DB
            if 'role' not in session:
                session['role'] = 'employee'
            
            if session.get('role') == 'maintenance':
                return redirect(url_for('complaints'))
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')

    return render_template('login.html')

@app.route('/backup')
def backup_db():
    if 'token' not in session: return redirect(url_for('login'))
    
    # Restrict Maintenance role
    if session.get('role') == 'maintenance':
        return redirect(url_for('complaints'))
    
    db_path = 'payments.db'
    if not os.path.exists(db_path):
        flash('Database file not found.', 'error')
        return redirect(url_for('dashboard'))
        
    filename = f"TopNetPay_DB_Backup_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.sqlite"
    return send_file(db_path, as_attachment=True, download_name=filename)

@app.route('/restore', methods=['POST'])
def restore_db():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied: Admin only.', 'error')
        return redirect(url_for('dashboard'))
    
    if 'backup_file' not in request.files:
        flash('No file uploaded.', 'error')
        return redirect(url_for('dashboard'))
        
    file = request.files['backup_file']
    if file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('dashboard'))
        
    if file:
        # Create an emergency backup of the current DB
        if os.path.exists(DB_PATH):
            shutil.copy(DB_PATH, DB_PATH + ".bak")
            
        try:
            # Save the new file over the old one
            file.save(DB_PATH)
            
            # Simple SQLite validation (header check)
            with open(DB_PATH, 'rb') as f:
                header = f.read(16)
                if header != b'SQLite format 3\x00':
                    # Restore from backup if invalid
                    shutil.move(DB_PATH + ".bak", DB_PATH)
                    flash('Invalid database file format.', 'error')
                    return redirect(url_for('dashboard'))
            
            # Re-initialize DB to sync schema if any new columns were added in the restore
            init_db()
            
            flash('✅ Database restored successfully! A backup of the old database was saved as payments.db.bak.', 'success')
        except Exception as e:
            if os.path.exists(DB_PATH + ".bak"):
                shutil.move(DB_PATH + ".bak", DB_PATH)
            flash(f'Error during restoration: {e}', 'error')
            
    return redirect(url_for('dashboard'))

@app.route('/settings/database')
def settings_database():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied: Admin only.', 'error')
        return redirect(url_for('dashboard'))
    
    backups = []
    if os.path.exists(BACKUP_DIR):
        for f in os.listdir(BACKUP_DIR):
            if f.endswith('.sqlite') or f.endswith('.db'):
                path = os.path.join(BACKUP_DIR, f)
                stats = os.stat(path)
                backups.append({
                    'name': f,
                    'size': f"{stats.st_size / (1024*1024):.2f} MB",
                    'date': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'timestamp': stats.st_mtime
                })
    
    # Sort by newest first
    backups.sort(key=lambda x: x['timestamp'], reverse=True)
    
    return render_template('settings_database.html', backups=backups)

@app.route('/settings/database/create', methods=['POST'])
def create_backup():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied.', 'error')
        return redirect(url_for('dashboard'))
        
    try:
        filename = f"SAS_Backup_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.sqlite"
        dest = os.path.join(BACKUP_DIR, filename)
        shutil.copy(DB_PATH, dest)
        flash(f'✅ Backup created successfully: {filename}', 'success')
    except Exception as e:
        flash(f'Error creating backup: {e}', 'error')
        
    return redirect(url_for('settings_database'))

@app.route('/settings/database/restore/<filename>', methods=['POST'])
def restore_from_file(filename):
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied.', 'error')
        return redirect(url_for('dashboard'))
        
    src = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(src):
        flash('Backup file not found.', 'error')
        return redirect(url_for('settings_database'))
        
    # Create an emergency backup of the current DB
    shutil.copy(DB_PATH, DB_PATH + ".bak")
    
    try:
        shutil.copy(src, DB_PATH)
        # Re-initialize DB to sync schema
        init_db()
        flash(f'✅ Database restored successfully from {filename}!', 'success')
    except Exception as e:
        if os.path.exists(DB_PATH + ".bak"):
            shutil.move(DB_PATH + ".bak", DB_PATH)
        flash(f'Error during restoration: {e}', 'error')
        
    return redirect(url_for('settings_database'))

@app.route('/settings/database/download/<filename>')
def download_backup(filename):
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied.', 'error')
        return redirect(url_for('dashboard'))
        
    return send_from_directory(BACKUP_DIR, filename, as_attachment=True)

@app.route('/settings/database/delete/<filename>', methods=['POST'])
def delete_backup_file(filename):
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied.', 'error')
        return redirect(url_for('dashboard'))
        
    path = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
        flash(f'🗑️ Backup {filename} deleted.', 'success')
    else:
        flash('File not found.', 'error')
        
    return redirect(url_for('settings_database'))

@app.route('/dashboard')
def dashboard():
    if 'token' not in session:
        return redirect(url_for('login'))
    
    # Restrict Maintenance role to complaints only
    if session.get('role') == 'maintenance':
        return redirect(url_for('complaints'))

    token = session['token']
    search_query = request.args.get('search', '').strip()
    parent_query = request.args.get('parent', '').strip()
    status_query = request.args.get('status', '').strip()
    page = int(request.args.get('page', 1))
    per_page = 50

    response = fetch_all_users_from_api(token)
    
    if isinstance(response, int) or response is None:
        if USER_CACHE['data'] is None:
            session.clear()
            flash('Connection Error or Session expired. Please log in again.', 'error')
            return redirect(url_for('login'))
        else:
            # Should not happen as fallback is handled, but fallback safety
            response = {'data': USER_CACHE['data'], 'total': USER_CACHE['total'], 'status': 'offline', 'timestamp': USER_CACHE['timestamp']}


    raw_users = response.get('data', [])
    if isinstance(raw_users, dict):
        all_users = list(raw_users.values())
    elif not isinstance(raw_users, list):
        all_users = []
    else:
        all_users = raw_users
        
    all_users = [u for u in all_users if isinstance(u, dict)]
    global_total = response.get('total', 0)
    
    unique_parents = sorted(list(set([u.get('parent_username') for u in all_users if u.get('parent_username')])))

    filtered_users = all_users
    
    user_role = session.get('role')
    user_parent = session.get('parent')
    if user_role in ['employee', 'manager'] and user_parent:
        p_lower = user_parent.lower()
        filtered_users = [u for u in filtered_users if str(u.get('parent_username', '')).lower() == p_lower or str(u.get('owner', '')).lower() == p_lower or str(u.get('parent', '')).lower() == p_lower]
        
    if parent_query:
        filtered_users = [u for u in filtered_users if str(u.get('parent_username', '')).lower() == parent_query.lower()]

    if status_query:
        if status_query == 'active':
            filtered_users = [u for u in filtered_users if u.get('status') and u['status'].get('status') == True]
        elif status_query == 'inactive':
            filtered_users = [u for u in filtered_users if not (u.get('status') and u['status'].get('status') == True)]

    if search_query:
        search_lower = search_query.lower()
        filtered_users = [u for u in filtered_users if search_lower in str(u.get('username', '')).lower() 
                          or search_lower in str(u.get('firstname', '') or '').lower()
                          or search_lower in str(u.get('lastname', '') or '').lower()]

    active_count = len([u for u in filtered_users if u.get('status') and u['status'].get('status') == True])
    inactive_count = len(filtered_users) - active_count

    total_filtered = len(filtered_users)
    total_pages = max(1, math.ceil(total_filtered / per_page)) if isinstance(filtered_users, list) else 1 
    
    start_idx = int((page - 1) * per_page)
    end_idx = int(start_idx + per_page)
    
    if not isinstance(filtered_users, list):
        filtered_users = list(filtered_users)
        
    paginated_users = filtered_users[start_idx:end_idx]  # type: ignore

    return render_template(
        'dashboard.html', 
        users=paginated_users, 
        unique_parents=unique_parents,
        search=search_query, 
        parent=parent_query,
        status=status_query,
        active_count=active_count,
        inactive_count=inactive_count,
        total=global_total,
        page=page,
        total_pages=total_pages,
        api_status=response.get('status', 'online'),
        last_update_str=datetime.fromtimestamp(float(response.get('timestamp', time.time()))).strftime('%H:%M:%S')
    )

@app.route('/payments', methods=['GET', 'POST'])
def payments():
    if 'token' not in session:
        return redirect(url_for('login'))

    # Restrict Maintenance role to complaints only
    if session.get('role') == 'maintenance':
        return redirect(url_for('complaints'))

    if request.method == 'POST':
        username = request.form.get('username')
        fullname = request.form.get('fullname', '')
        profile_name = request.form.get('profile_name', '')
        parent = request.form.get('parent', '')
        amount = request.form.get('amount')
        phone = request.form.get('phone', '') # Added phone
        admin_name = session.get('username', 'Unknown')
        
        if username and amount:
            public_token = str(uuid.uuid4())
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO payments_v3 
                (username, fullname, profile_name, parent, amount, admin_name, phone, public_token) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (username, fullname, profile_name, parent, amount, admin_name, phone, public_token))
            payment_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            # Send Notification via Webhook (Asynchronously)
            send_webhook_async({
                'id': payment_id,
                'username': username,
                'fullname': fullname,
                'profile_name': profile_name,
                'parent': parent,
                'amount': amount,
                'phone': phone,
                'admin_name': admin_name,
                'public_token': public_token
            }, webhook_type='payments', event_name='new', base_url=request.host_url)
            
            flash(f'Successfully registered payment of {amount} ل.س for {username}.', 'success')
            return redirect(url_for('payments'))
            
    token = session['token']
    response = fetch_all_users_from_api(token)
    
    if isinstance(response, int) or response is None:
        if USER_CACHE['data'] is None:
            session.clear()
            flash('Connection Error or Session expired. Please log in again.', 'error')
            return redirect(url_for('login'))
        else:
            response = {'data': USER_CACHE['data'], 'total': USER_CACHE['total'], 'status': 'offline', 'timestamp': USER_CACHE['timestamp']}
            flash('API Connection Error. Showing cached offline data.', 'error')

    raw_users = response.get('data') or []
    if isinstance(raw_users, dict):
        all_users = list(raw_users.values())
    elif not isinstance(raw_users, list):
        all_users = []
    else:
        all_users = raw_users
    all_users = [u for u in all_users if isinstance(u, dict)]
    status_filter = request.args.get('status', 'all')
    search_query = request.args.get('search', '').strip()
    user_role = session.get('role')
    user_name = session.get('username')

    conn = get_db_connection()
    
    # Base query for payments
    base_query = 'SELECT * FROM payments_v3'
    params = []
    where_clauses = []

    if status_filter == 'paid':
        where_clauses.append('amount > 0')
    elif status_filter == 'unpaid':
        where_clauses.append('amount = 0')

    # Visibility Restriction: Employees only see their own
    if user_role == 'employee':
        where_clauses.append('admin_name = ?')
        params.append(user_name)

    # Customer Search: Filter by username or fullname
    if search_query:
        search_pattern = f"%{search_query}%"
        where_clauses.append('(username LIKE ? OR fullname LIKE ?)')
        params.append(search_pattern)
        params.append(search_pattern)

    if where_clauses:
        base_query += ' WHERE ' + ' AND '.join(where_clauses)
    
    base_query += ' ORDER BY created_at DESC'
    recent_payments = conn.execute(base_query, params).fetchall()
    
    # Daily Stats Query with Visibility Restriction
    stats_params = []
    stats_where = "WHERE date(created_at) = date('now')"
    if user_role == 'employee':
        stats_where += " AND admin_name = ?"
        stats_params.append(user_name)

    today_totals_query = f'''
        SELECT parent, SUM(amount) as daily_total 
        FROM payments_v3 
        {stats_where}
        GROUP BY parent
        ORDER BY daily_total DESC
    '''
    daily_stats = conn.execute(today_totals_query, stats_params).fetchall()
    conn.close()

    current_date = datetime.now().strftime('%Y-%m-%d')

    return render_template(
        'payments.html', 
        recent_payments=recent_payments, 
        all_users=all_users, 
        daily_stats=daily_stats, 
        current_date=current_date,
        status_filter=status_filter,
        search_query=search_query,
        api_status=response.get('status', 'online'),
        last_update_str=datetime.fromtimestamp(float(response.get('timestamp', time.time()))).strftime('%H:%M:%S')
    )

@app.route('/payments/edit/<int:p_id>', methods=['POST'])
def edit_payment(p_id):
    if 'token' not in session: return redirect(url_for('login'))
    
    auth_pass = request.form.get('admin_pass')
    conn = get_db_connection()
    target_payment = conn.execute('SELECT username FROM payments_v3 WHERE id = ?', (p_id,)).fetchone()
    
    if not target_payment or auth_pass != target_payment['username']:
        conn.close()
        flash('🚫 Unauthorized: Password must exactly match the Username of the customer to edit.', 'error')
        return redirect(url_for('payments'))
        
    new_amount = request.form.get('amount')
    conn.execute('UPDATE payments_v3 SET amount = ? WHERE id = ?', (new_amount, p_id))
    conn.commit()
    conn.close()
    flash(f'Payment #{p_id} updated successfully to {new_amount} ل.س.', 'success')
    return redirect(url_for('payments'))

@app.route('/payments/delete/<int:p_id>', methods=['POST'])
def delete_payment(p_id):
    if 'token' not in session: return redirect(url_for('login'))
    
    auth_pass = request.form.get('admin_pass')
    required_pass = get_or_create_delete_password()
    
    if auth_pass != required_pass:
        flash('🚫 Unauthorized: Incorrect Delete Password from file.', 'error')
        return redirect(url_for('payments'))
        
    conn = get_db_connection()
    conn.execute('DELETE FROM payments_v3 WHERE id = ?', (p_id,))
    conn.commit()
    conn.close()
    flash(f'Payment #{p_id} deleted successfully.', 'success')
    return redirect(url_for('payments'))

@app.route('/payments/export')
def export_payments():
    if 'token' not in session:
        return redirect(url_for('login'))
        
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if not start_date or not end_date:
        start_date = end_date = datetime.now().strftime('%Y-%m-%d')
        
    conn = get_db_connection()
    user_role = session.get('role')
    user_name = session.get('username')

    if user_role == 'employee':
        query = '''
            SELECT id, username, fullname, phone, profile_name, parent, amount, admin_name, created_at 
            FROM payments_v3 
            WHERE date(created_at) BETWEEN ? AND ? AND admin_name = ?
            ORDER BY created_at DESC
        '''
        payments_data = conn.execute(query, (start_date, end_date, user_name)).fetchall()
    else:
        query = '''
            SELECT id, username, fullname, phone, profile_name, parent, amount, admin_name, created_at 
            FROM payments_v3 
            WHERE date(created_at) BETWEEN ? AND ? 
            ORDER BY created_at DESC
        '''
        payments_data = conn.execute(query, (start_date, end_date)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Username', 'Full Name', 'Phone', 'Profile / Speed', 'Parent', 'Amount (SYP)', 'Admin', 'Date Registered'])
    
    for p in payments_data:
        writer.writerow([p['id'], p['username'], p['fullname'], p['phone'], p['profile_name'], p['parent'], p['amount'], p['admin_name'], p['created_at']])

    csv_data = output.getvalue()
    csv_data_with_bom = '\ufeff' + csv_data
    
    filename = f"payments_{start_date}_to_{end_date}.csv"
    
    return Response(
        csv_data_with_bom,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

@app.route('/settings/sas', methods=['GET', 'POST'])
def settings_sas():
    global sasclient, SAS_API_IP, SAS_ADMIN_USER, SAS_ADMIN_PASS
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied: Admin only.', 'error')
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    test_result = None

    if request.method == 'POST':
        action = request.form.get('action')
        new_ip = request.form.get('sas_ip', '').strip()
        new_user = request.form.get('sas_user', '').strip()
        new_pass = request.form.get('sas_pass', '').strip()

        if action == 'test':
            # Test connection without saving
            test_client = SasAPI(f"https://{new_ip}", portal='admin')
            result = test_client.login(new_user, new_pass)
            token = result[0] if isinstance(result, tuple) else result
            error = result[1] if isinstance(result, tuple) else None
            if token:
                test_result = {'success': True, 'detail': f"✅ Connection successful!\nServer: {new_ip}\nAPI Base: {test_client.base_url}\nToken received: {token[:20]}..."}
                flash('✅ اتصال ناجح بسيرفر الساس!', 'success')
            else:
                test_result = {'success': False, 'detail': f"❌ Connection failed!\nServer: {new_ip}\nError: {error}\n\nAttempts:\n" + "\n".join([f"  {a['url']} → {a['status']}" for a in test_client.attempts])}
                flash(f'❌ فشل الاتصال: {error}', 'error')

        elif action in ['save', 'save_and_sync']:
            # Save to DB
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sas_ip', ?)", (new_ip,))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sas_user', ?)", (new_user,))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sas_pass', ?)", (new_pass,))
            
            # Save exchange rate
            exchange_rate = request.form.get('exchange_rate', '').strip()
            if exchange_rate:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('exchange_rate', ?)", (exchange_rate,))
            
            conn.commit()

            # Update global variables
            SAS_API_IP = new_ip
            SAS_ADMIN_USER = new_user
            SAS_ADMIN_PASS = new_pass

            # Reinitialize the SAS client with new IP
            sasclient = SasAPI(f"https://{new_ip}", portal='admin')

            flash('✅ تم حفظ إعدادات الساس بنجاح!', 'success')

            if action == 'save_and_sync':
                # Attempt login and sync
                result = sasclient.login(SAS_ADMIN_USER, SAS_ADMIN_PASS)
                token = result[0] if isinstance(result, tuple) else result
                if token:
                    threading.Thread(target=background_refresh, args=(token,), daemon=True).start()
                    flash('🔄 بدأت المزامنة في الخلفية...', 'success')
                else:
                    error = result[1] if isinstance(result, tuple) else 'Unknown'
                    flash(f'⚠️ تم الحفظ لكن فشل تسجيل الدخول للمزامنة: {error}', 'error')

    # Load current settings
    sas_ip_row = conn.execute("SELECT value FROM settings WHERE key = 'sas_ip'").fetchone()
    sas_user_row = conn.execute("SELECT value FROM settings WHERE key = 'sas_user'").fetchone()
    sas_pass_row = conn.execute("SELECT value FROM settings WHERE key = 'sas_pass'").fetchone()
    rate_row = conn.execute("SELECT value FROM settings WHERE key = 'exchange_rate'").fetchone()
    conn.close()

    current_ip = sas_ip_row['value'] if sas_ip_row else SAS_API_IP
    current_user = sas_user_row['value'] if sas_user_row else SAS_ADMIN_USER
    current_pass = sas_pass_row['value'] if sas_pass_row else SAS_ADMIN_PASS
    exchange_rate = rate_row['value'] if rate_row else ''

    # Last sync time
    last_sync = None
    if USER_CACHE['timestamp'] > 0:
        last_sync = datetime.fromtimestamp(USER_CACHE['timestamp']).strftime('%Y-%m-%d %H:%M:%S')

    return render_template('settings_sas.html',
        current_ip=current_ip,
        current_user=current_user,
        current_pass=current_pass,
        last_sync=last_sync,
        test_result=test_result,
        exchange_rate=exchange_rate
    )

@app.route('/settings/webhook', methods=['GET', 'POST'])
def webhook_settings():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Access Denied: Admin only.', 'error')
        return redirect(url_for('dashboard'))
        
    conn = get_db_connection()
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'save':
            # 1. Update URLs
            keys = ['webhook_payments', 'webhook_complaints', 'webhook_installations']
            for k in keys:
                val = request.form.get(k, '').strip()
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, val))
            
            # 2. Update Toggles and Events
            toggle_keys = [
                'webhook_payments_enabled', 'webhook_complaints_enabled', 'webhook_installations_enabled',
                'webhook_complaints_on_new', 'webhook_complaints_on_assign', 'webhook_complaints_on_update', 'webhook_complaints_on_resolve',
                'webhook_installations_on_new', 'webhook_installations_on_assign', 'webhook_installations_on_update', 'webhook_installations_on_complete'
            ]
            for k in toggle_keys:
                val = '1' if request.form.get(k) else '0'
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, val))
            
            # Note: payments only has 'new' for now, always 1 if channel enabled
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('webhook_payments_on_new', '1')")

            conn.commit()
            flash('✅ Webhook settings updated successfully!', 'success')
        elif action == 'test':
            test_type = request.form.get('test_type', 'payments')
            test_url = request.form.get(f'webhook_{test_type}', '').strip()
            
            if not test_url:
                flash(f'❌ Please enter a URL for {test_type} to test.', 'error')
            else:
                success, msg = send_webhook({'status': 'testing_alert', 'type': test_type}, webhook_url_override=test_url)
                if success:
                    flash(f'🚀 Test Webhook for {test_type} sent successfully!', 'success')
                else:
                    flash(f'❌ Test Failed: {msg}', 'error')
        elif action == 'clear_data':
            target = request.form.get('target')
            confirm_pwd = request.form.get('password')
            master_pwd = get_or_create_delete_password()
            
            if confirm_pwd == master_pwd:
                if target == 'payments':
                    conn.execute("DELETE FROM payments_v3")
                elif target == 'complaints':
                    conn.execute("DELETE FROM complaints")
                    conn.execute("DELETE FROM complaint_logs")
                elif target == 'installations':
                    conn.execute("DELETE FROM installations")
                elif target == 'expenses':
                    conn.execute("DELETE FROM expenses")
                
                conn.commit()
                flash(f'🗑️ تم مسح بيانات {target} بنجاح.', 'success')
            else:
                flash('❌ كلمة مرور الحذف غير صحيحة.', 'error')
        
    settings = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'webhook_%'").fetchall()
    s_dict = {row['key']: row['value'] for row in settings}
    deletion_password = get_or_create_delete_password()
    conn.close()
    
    return render_template('settings_webhook.html', s_dict=s_dict, deletion_password=deletion_password)

@app.route('/expenses', methods=['GET', 'POST'])
def expenses():
    if 'token' not in session: return redirect(url_for('login'))
    
    # Restrict Maintenance role to complaints only
    if session.get('role') == 'maintenance':
        return redirect(url_for('complaints'))
    
    # Retrieve api_status for navbar
    token = session['token']
    response = fetch_all_users_from_api(token)
    api_status = response.get('status', 'online') if isinstance(response, dict) else 'offline'
    last_update_str = datetime.fromtimestamp(response.get('timestamp', time.time())).strftime('%H:%M:%S') if isinstance(response, dict) else ''

    if request.method == 'POST':
        category = request.form.get('category')
        amount = request.form.get('amount')
        description = request.form.get('description', '')
        admin_name = session.get('username', 'Admin')
        
        if category and amount:
            conn = get_db_connection()
            conn.execute('''
                INSERT INTO expenses (category, amount, description, admin_name) 
                VALUES (?, ?, ?, ?)
            ''', (category, amount, description, admin_name))
            conn.commit()
            conn.close()
            flash(f'Successfully registered expense of {amount} ل.س for {category}.', 'success')
            return redirect(url_for('expenses'))

    conn = get_db_connection()
    user_role = session.get('role')
    user_name = session.get('username')

    # Visibility Restriction: Employees only see their own expenses
    if user_role == 'employee':
        recent_expenses = conn.execute('SELECT * FROM expenses WHERE admin_name = ? ORDER BY created_at DESC', (user_name,)).fetchall()
        today_totals_query = "SELECT SUM(amount) as daily_total FROM expenses WHERE date(created_at) = date('now') AND admin_name = ?"
        month_totals_query = "SELECT SUM(amount) as monthly_total FROM expenses WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now') AND admin_name = ?"
        daily_stats = conn.execute(today_totals_query, (user_name,)).fetchone()
        monthly_stats = conn.execute(month_totals_query, (user_name,)).fetchone()
    else:
        # Admin / Manager sees everything
        recent_expenses = conn.execute('SELECT * FROM expenses ORDER BY created_at DESC').fetchall()
        today_totals_query = "SELECT SUM(amount) as daily_total FROM expenses WHERE date(created_at) = date('now')"
        month_totals_query = "SELECT SUM(amount) as monthly_total FROM expenses WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
        daily_stats = conn.execute(today_totals_query).fetchone()
        monthly_stats = conn.execute(month_totals_query).fetchone()
        
    conn.close()

    current_date = datetime.now().strftime('%Y-%m-%d')
    
    return render_template(
        'expenses.html', 
        recent_expenses=recent_expenses, 
        daily_total=daily_stats['daily_total'] or 0,
        monthly_total=monthly_stats['monthly_total'] or 0,
        current_date=current_date,
        api_status=api_status,
        last_update_str=last_update_str
    )

@app.route('/expenses/delete/<int:e_id>', methods=['POST'])
def delete_expense(e_id):
    if 'token' not in session: return redirect(url_for('login'))
    
    auth_pass = request.form.get('admin_pass')
    required_pass = get_or_create_delete_password()
    
    if auth_pass != required_pass:
        flash('🚫 Unauthorized: Incorrect Delete Password from file.', 'error')
        return redirect(url_for('expenses'))
        
    conn = get_db_connection()
    conn.execute('DELETE FROM expenses WHERE id = ?', (e_id,))
    conn.commit()
    conn.close()
    flash(f'Expense #{e_id} deleted successfully.', 'success')
    return redirect(url_for('expenses'))

@app.route('/expenses/export')
def export_expenses():
    if 'token' not in session: return redirect(url_for('login'))
        
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if not start_date or not end_date:
        start_date = end_date = datetime.now().strftime('%Y-%m-%d')
        
    conn = get_db_connection()
    user_role = session.get('role')
    user_name = session.get('username')

    if user_role == 'employee':
        query = '''
            SELECT id, category, amount, description, admin_name, created_at 
            FROM expenses 
            WHERE date(created_at) BETWEEN ? AND ? AND admin_name = ?
            ORDER BY created_at DESC
        '''
        expenses_data = conn.execute(query, (start_date, end_date, user_name)).fetchall()
    else:
        query = '''
            SELECT id, category, amount, description, admin_name, created_at 
            FROM expenses 
            WHERE date(created_at) BETWEEN ? AND ? 
            ORDER BY created_at DESC
        '''
        expenses_data = conn.execute(query, (start_date, end_date)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Category', 'Amount (SYP)', 'Description', 'Admin', 'Date Registered'])
    
    for e in expenses_data:
        writer.writerow([e['id'], e['category'], e['amount'], e['description'], e['admin_name'], e['created_at']])

    csv_data = output.getvalue()
    csv_data_with_bom = '\ufeff' + csv_data
    filename = f"expenses_{start_date}_to_{end_date}.csv"
    
    return Response(csv_data_with_bom, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment;filename={filename}"})

# =============================================
#  INVENTORY & ACCOUNTING MODULE
# =============================================

@app.route('/inventory', methods=['GET', 'POST'])
def inventory():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') == 'maintenance':
        return redirect(url_for('complaints'))

    conn = get_db_connection()
    user_role = session.get('role')
    user_name = session.get('username')

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_rate' and user_role in ['admin', 'manager']:
            new_rate = request.form.get('exchange_rate', '').strip()
            if new_rate:
                conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', ('exchange_rate', new_rate))
                conn.commit()
                flash('✅ تم تحديث سعر الصرف بنجاح.', 'success')

        elif action == 'add_item' and user_role in ['admin', 'manager']:
            name = request.form.get('name', '').strip()
            category = request.form.get('category', '').strip()
            cost_price = float(request.form.get('cost_price', 0) or 0)
            sell_price = float(request.form.get('sell_price', 0) or 0)
            unit = request.form.get('unit', 'قطعة').strip()
            min_stock = int(request.form.get('min_stock', 0) or 0)
            if name:
                conn.execute('''
                    INSERT INTO inventory_items (name, category, cost_price, sell_price, unit, min_stock)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (name, category, cost_price, sell_price, unit, min_stock))
                conn.commit()
                flash('✅ تم إضافة المنتج بنجاح.', 'success')
            else:
                flash('❌ يرجى إدخال اسم المنتج.', 'error')

        elif action == 'edit_item' and user_role in ['admin', 'manager']:
            item_id = request.form.get('item_id')
            name = request.form.get('name', '').strip()
            category = request.form.get('category', '').strip()
            cost_price = float(request.form.get('cost_price', 0) or 0)
            sell_price = float(request.form.get('sell_price', 0) or 0)
            unit = request.form.get('unit', 'قطعة').strip()
            min_stock = int(request.form.get('min_stock', 0) or 0)
            conn.execute('''
                UPDATE inventory_items SET name=?, category=?, cost_price=?, sell_price=?, unit=?, min_stock=?
                WHERE id=?
            ''', (name, category, cost_price, sell_price, unit, min_stock, item_id))
            conn.commit()
            flash('✅ تم تحديث المنتج.', 'success')

        elif action == 'delete_item' and user_role in ['admin', 'manager']:
            item_id = request.form.get('item_id')
            conn.execute('DELETE FROM inventory_items WHERE id = ?', (item_id,))
            conn.commit()
            flash('🗑️ تم حذف المنتج.', 'success')

        elif action == 'add_purchase':
            item_id = int(request.form.get('item_id', 0))
            quantity = int(request.form.get('quantity', 0) or 0)
            cost_price = float(request.form.get('cost_price', 0) or 0)
            supplier = request.form.get('supplier', '').strip()
            notes = request.form.get('notes', '').strip()
            total_cost = quantity * cost_price
            if item_id and quantity > 0:
                conn.execute('''
                    INSERT INTO inventory_purchases (item_id, quantity, cost_price, total_cost, supplier, notes, admin_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (item_id, quantity, cost_price, total_cost, supplier, notes, user_name))
                # Update stock quantity
                conn.execute('UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?', (quantity, item_id))
                # Update cost price if changed
                if cost_price > 0:
                    conn.execute('UPDATE inventory_items SET cost_price = ? WHERE id = ?', (cost_price, item_id))
                conn.commit()
                flash(f'✅ تم تسجيل شراء {quantity} وحدة وتحديث المخزون.', 'success')
            else:
                flash('❌ يرجى اختيار المنتج والكمية.', 'error')

        elif action == 'edit_purchase' and user_role in ['admin', 'manager']:
            purchase_id = request.form.get('purchase_id')
            item_id = int(request.form.get('item_id', 0))
            quantity = int(request.form.get('quantity', 0) or 0)
            cost_price = float(request.form.get('cost_price', 0) or 0)
            supplier = request.form.get('supplier', '').strip()
            notes = request.form.get('notes', '').strip()
            total_cost = quantity * cost_price

            old_p = conn.execute('SELECT item_id, quantity FROM inventory_purchases WHERE id = ?', (purchase_id,)).fetchone()
            if old_p and item_id and quantity > 0:
                # Update stock
                if old_p['item_id'] == item_id:
                    diff = quantity - old_p['quantity']
                    conn.execute('UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?', (diff, item_id))
                else:
                    # Item changed
                    conn.execute('UPDATE inventory_items SET stock_qty = stock_qty - ? WHERE id = ?', (old_p['quantity'], old_p['item_id']))
                    conn.execute('UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?', (quantity, item_id))
                
                conn.execute('''
                    UPDATE inventory_purchases 
                    SET item_id=?, quantity=?, cost_price=?, total_cost=?, supplier=?, notes=?
                    WHERE id=?
                ''', (item_id, quantity, cost_price, total_cost, supplier, notes, purchase_id))
                conn.commit()
                flash('✅ تم تحديث بيانات الشراء وتعديل المخزون.', 'success')

        elif action == 'delete_purchase' and user_role in ['admin', 'manager']:
            purchase_id = request.form.get('purchase_id')
            p = conn.execute('SELECT item_id, quantity FROM inventory_purchases WHERE id = ?', (purchase_id,)).fetchone()
            if p:
                conn.execute('UPDATE inventory_items SET stock_qty = stock_qty - ? WHERE id = ?', (p['quantity'], p['item_id']))
                conn.execute('DELETE FROM inventory_purchases WHERE id = ?', (purchase_id,))
                conn.commit()
                flash('🗑️ تم حذف سجل الشراء وتعديل المخزون.', 'success')

        elif action == 'add_sale':
            item_id = int(request.form.get('item_id', 0))
            quantity = int(request.form.get('quantity', 0) or 0)
            sell_price = float(request.form.get('sell_price', 0) or 0)
            discount = float(request.form.get('discount', 0) or 0)
            customer_name = request.form.get('customer_name', '').strip()
            notes = request.form.get('notes', '').strip()
            total_amount = (sell_price * quantity) - discount
            if item_id and quantity > 0:
                # Check stock
                item = conn.execute('SELECT stock_qty FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
                if item and item['stock_qty'] >= quantity:
                    conn.execute('''
                        INSERT INTO inventory_sales (item_id, quantity, sell_price, discount, total_amount, customer_name, admin_name, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (item_id, quantity, sell_price, discount, total_amount, customer_name, user_name, notes))
                    conn.execute('UPDATE inventory_items SET stock_qty = stock_qty - ? WHERE id = ?', (quantity, item_id))
                    conn.commit()
                    flash(f'✅ تم تسجيل بيع {quantity} وحدة.', 'success')
                else:
                    flash(f'❌ الكمية المطلوبة ({quantity}) أكثر من المتوفر ({item["stock_qty"] if item else 0}).', 'error')
            else:
                flash('❌ يرجى اختيار المنتج والكمية.', 'error')

        return redirect(url_for('inventory'))

    # GET - Load data
    items = conn.execute('SELECT * FROM inventory_items ORDER BY name').fetchall()
    
    # Recent purchases with item names
    purchases = conn.execute('''
        SELECT p.*, i.name as item_name, i.unit 
        FROM inventory_purchases p 
        JOIN inventory_items i ON p.item_id = i.id 
        ORDER BY p.created_at DESC LIMIT 50
    ''').fetchall()

    # Recent sales with item names
    sales = conn.execute('''
        SELECT s.*, i.name as item_name, i.unit, i.cost_price as item_cost
        FROM inventory_sales s 
        JOIN inventory_items i ON s.item_id = i.id 
        ORDER BY s.created_at DESC LIMIT 50
    ''').fetchall()

    # Low stock alerts
    low_stock = conn.execute('SELECT * FROM inventory_items WHERE stock_qty <= min_stock AND is_active = 1').fetchall()

    # Exchange rate
    rate_row = conn.execute("SELECT value FROM settings WHERE key = 'exchange_rate'").fetchone()
    exchange_rate = float(rate_row['value']) if rate_row else 0

    conn.close()

    return render_template('inventory.html', 
        items=items, purchases=purchases, sales=sales, 
        low_stock=low_stock, exchange_rate=exchange_rate)

@app.route('/inventory/api/items')
def inventory_api_items():
    """JSON API for getting inventory items (used by installation completion modal)."""
    if 'token' not in session: return json.dumps([]), 401
    conn = get_db_connection()
    items = conn.execute('SELECT id, name, sell_price, stock_qty, unit FROM inventory_items WHERE is_active = 1 AND stock_qty > 0 ORDER BY name').fetchall()
    conn.close()
    return json.dumps([dict(i) for i in items])

@app.route('/inventory/reports')
def inventory_reports():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 غير مصرح. هذه الصفحة مخصصة للمدير فقط.', 'error')
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    
    # Overall stats
    total_items = conn.execute('SELECT COUNT(*) FROM inventory_items WHERE is_active = 1').fetchone()[0]
    total_stock_value = conn.execute('SELECT COALESCE(SUM(cost_price * stock_qty), 0) FROM inventory_items WHERE is_active = 1').fetchone()[0]
    total_purchases = conn.execute('SELECT COALESCE(SUM(total_cost), 0) FROM inventory_purchases').fetchone()[0]
    total_sales_amount = conn.execute('SELECT COALESCE(SUM(total_amount), 0) FROM inventory_sales').fetchone()[0]
    total_sales_cost = conn.execute('''
        SELECT COALESCE(SUM(s.quantity * i.cost_price), 0) 
        FROM inventory_sales s JOIN inventory_items i ON s.item_id = i.id
    ''').fetchone()[0]
    total_profit = total_sales_amount - total_sales_cost

    # Items with profit details
    items_report = conn.execute('''
        SELECT i.*, 
            COALESCE((SELECT SUM(p.quantity) FROM inventory_purchases p WHERE p.item_id = i.id), 0) as total_purchased,
            COALESCE((SELECT SUM(s.quantity) FROM inventory_sales s WHERE s.item_id = i.id), 0) as total_sold,
            COALESCE((SELECT SUM(s.total_amount) FROM inventory_sales s WHERE s.item_id = i.id), 0) as revenue,
            COALESCE((SELECT SUM(s.quantity * i.cost_price) FROM inventory_sales s WHERE s.item_id = i.id), 0) as cost_of_sold
        FROM inventory_items i 
        ORDER BY total_sold DESC
    ''').fetchall()

    # Monthly breakdown
    monthly_sales = conn.execute('''
        SELECT strftime('%Y-%m', s.created_at) as month,
            SUM(s.total_amount) as revenue,
            SUM(s.quantity * i.cost_price) as cost,
            SUM(s.total_amount) - SUM(s.quantity * i.cost_price) as profit
        FROM inventory_sales s 
        JOIN inventory_items i ON s.item_id = i.id
        GROUP BY month ORDER BY month DESC LIMIT 12
    ''').fetchall()

    # Exchange rate
    rate_row = conn.execute("SELECT value FROM settings WHERE key = 'exchange_rate'").fetchone()
    exchange_rate = float(rate_row['value']) if rate_row else 0

    conn.close()

    return render_template('inventory_reports.html',
        total_items=total_items, total_stock_value=total_stock_value,
        total_purchases=total_purchases, total_sales_amount=total_sales_amount,
        total_profit=total_profit, items_report=items_report,
        monthly_sales=monthly_sales, exchange_rate=exchange_rate)

@app.route('/inventory/export')
def export_inventory():
    if 'token' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    items = conn.execute('SELECT * FROM inventory_items ORDER BY name').fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Name', 'Category', 'Cost Price', 'Sell Price', 'Stock', 'Unit', 'Min Stock'])
    for i in items:
        writer.writerow([i['id'], i['name'], i['category'], i['cost_price'], i['sell_price'], i['stock_qty'], i['unit'], i['min_stock']])
    csv_data = '\ufeff' + output.getvalue()
    return Response(csv_data, mimetype='text/csv', headers={'Content-Disposition': f'attachment;filename=inventory_{datetime.now().strftime("%Y-%m-%d")}.csv'})

@app.route('/inventory/sale/from-installation', methods=['POST'])
def inventory_sale_from_installation():
    """Record inventory sales linked to an installation (called via AJAX or form submission)."""
    if 'token' not in session:
        return json.dumps({'error': 'Unauthorized'}), 401

    installation_id = request.form.get('installation_id')
    items_json = request.form.get('items_json', '[]')
    admin_name = session.get('username', 'Unknown')

    try:
        sale_items = json.loads(items_json)
    except (json.JSONDecodeError, TypeError):
        sale_items = []

    if not sale_items:
        return json.dumps({'status': 'ok', 'message': 'No items to record'}), 200

    conn = get_db_connection()
    total_sale = 0

    for si in sale_items:
        item_id = si.get('item_id')
        quantity = int(si.get('quantity', 0))
        sell_price = float(si.get('sell_price', 0))
        discount = float(si.get('discount', 0))

        if item_id and quantity > 0:
            item = conn.execute('SELECT stock_qty, name FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            if item and item['stock_qty'] >= quantity:
                total_amount = (quantity * sell_price) - discount
                if total_amount < 0:
                    total_amount = 0
                conn.execute('''
                    INSERT INTO inventory_sales (item_id, quantity, sell_price, discount, total_amount, installation_id, admin_name, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (item_id, quantity, sell_price, discount, total_amount, installation_id, admin_name, f'تركيب #{installation_id}'))
                conn.execute('UPDATE inventory_items SET stock_qty = stock_qty - ? WHERE id = ?', (quantity, item_id))
                total_sale += total_amount

    conn.commit()
    conn.close()

    return json.dumps({'status': 'ok', 'total_sale': total_sale}), 200, {'Content-Type': 'application/json'}

@app.route('/report')
def report():
    if 'token' not in session: return redirect(url_for('login'))
    
    # Restrict Maintenance role to complaints only
    if session.get('role') == 'maintenance':
        return redirect(url_for('complaints'))

    if session.get('role') not in ['admin', 'manager']:
        flash('🚫 Unauthorized: Only Admin and Manager can access reports.', 'error')
        return redirect(url_for('dashboard'))

    token = session['token']
    response = fetch_all_users_from_api(token)
    api_status = response.get('status', 'online') if isinstance(response, dict) else 'offline'
    last_update_str = datetime.fromtimestamp(response.get('timestamp', time.time())).strftime('%H:%M:%S') if isinstance(response, dict) else ''

    start_date = request.args.get('start_date', datetime.now().replace(day=1).strftime('%Y-%m-%d'))
    end_date = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))

    conn = get_db_connection()

    # Total subscriber payments in range
    subscriber_income = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM payments_v3 WHERE date(created_at) BETWEEN ? AND ?",
        (start_date, end_date)
    ).fetchone()['total']

    # Total installations income in range (USD/SYP)
    inst_revenue = conn.execute("""
        SELECT 
            COALESCE(SUM(payment_amount_usd),0) as total_usd,
            COALESCE(SUM(payment_amount_syp),0) as total_syp 
        FROM installations 
        WHERE status = 'Completed' AND date(updated_at) BETWEEN ? AND ?
    """, (start_date, end_date)).fetchone()
    
    rate_row = conn.execute("SELECT value FROM settings WHERE key = 'exchange_rate'").fetchone()
    exchange_rate = float(rate_row['value']) if rate_row and float(rate_row['value']) > 0 else 1.0

    installations_income_usd = 0
    installations_income_syp = inst_revenue['total_syp'] + (inst_revenue['total_usd'] * exchange_rate)
    
    # Total income
    income_total_syp = subscriber_income + installations_income_syp
    income_total_usd = 0

    # Installations Count
    installations_count = conn.execute(
        "SELECT COUNT(*) as count FROM installations WHERE status = 'Completed' AND date(updated_at) BETWEEN ? AND ?",
        (start_date, end_date)
    ).fetchone()['count']

    # Total expenses in range
    expense_total = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM expenses WHERE date(created_at) BETWEEN ? AND ?",
        (start_date, end_date)
    ).fetchone()['total']

    # Income breakdown by Profile
    income_by_profile = conn.execute(
        "SELECT profile_name, SUM(amount) as total, COUNT(*) as count FROM payments_v3 WHERE date(created_at) BETWEEN ? AND ? GROUP BY profile_name ORDER BY total DESC",
        (start_date, end_date)
    ).fetchall()

    # Daily breakdown: income by day (Unified Payments + Installations)
    daily_income = conn.execute(
        '''
        SELECT day, SUM(total_syp) as total_syp, SUM(total_usd) as total_usd FROM (
            SELECT date(created_at) as day, SUM(amount) as total_syp, 0 as total_usd FROM payments_v3 WHERE date(created_at) BETWEEN ? AND ? GROUP BY day
            UNION ALL
            SELECT date(updated_at) as day, SUM(payment_amount_syp) as total_syp, SUM(payment_amount_usd) as total_usd FROM installations WHERE status = 'Completed' AND date(updated_at) BETWEEN ? AND ? GROUP BY day
        ) GROUP BY day ORDER BY day ASC
        ''',
        (start_date, end_date, start_date, end_date)
    ).fetchall()

    # Daily breakdown: expenses by day
    daily_expense = conn.execute(
        "SELECT date(created_at) as day, SUM(amount) as total FROM expenses WHERE date(created_at) BETWEEN ? AND ? GROUP BY day ORDER BY day ASC",
        (start_date, end_date)
    ).fetchall()

    # Expenses by category
    expense_by_category = conn.execute(
        "SELECT category, SUM(amount) as total FROM expenses WHERE date(created_at) BETWEEN ? AND ? GROUP BY category ORDER BY total DESC",
        (start_date, end_date)
    ).fetchall()

    # Build unified day map
    day_map = {}
    for row in daily_income:
        if row['day'] not in day_map:
            day_map[row['day']] = {'income_syp': 0, 'income_usd': 0, 'expense': 0}
        day_map[row['day']]['income_syp'] += row['total_syp'] + (row['total_usd'] * exchange_rate)
        day_map[row['day']]['income_usd'] = 0
    for row in daily_expense:
        if row['day'] not in day_map:
            day_map[row['day']] = {'income_syp': 0, 'income_usd': 0, 'expense': 0}
        day_map[row['day']]['expense'] = row['total']

    daily_rows = sorted([
        {
            'day': d, 
            'income_syp': v['income_syp'], 
            'income_usd': 0,
            'expense': v['expense'], 
            'net_syp': v['income_syp'] - v['expense'],
            'net_usd': 0
        }
        for d, v in day_map.items()
    ], key=lambda x: x['day'], reverse=True)

    # Calculate percentages for the UI progress bars
    sub_percent = (subscriber_income / income_total_syp * 100) if income_total_syp > 0 else 0
    inst_percent = (installations_income_syp / income_total_syp * 100) if income_total_syp > 0 else 0

    return render_template(
        'report.html',
        start_date=start_date,
        end_date=end_date,
        subscriber_income=subscriber_income,
        income_total_syp=income_total_syp,
        income_total_usd=income_total_usd,
        expense_total=expense_total,
        net_profit_syp=income_total_syp - expense_total,
        net_profit_usd=income_total_usd,
        installations_income_syp=installations_income_syp,
        installations_income_usd=installations_income_usd,
        installations_count=installations_count,
        daily_rows=daily_rows,
        expense_by_category=expense_by_category,
        income_by_profile=income_by_profile,
        sub_percent=sub_percent,
        inst_percent=inst_percent,
        api_status=api_status,
        last_update_str=last_update_str
    )

@app.route('/payments/print/<int:p_id>')
def print_invoice(p_id):
    if 'token' not in session: return redirect(url_for('login'))
    
    conn = get_db_connection()
    payment = conn.execute('SELECT * FROM payments_v3 WHERE id = ?', (p_id,)).fetchone()
    conn.close()
    
    if not payment:
        flash('Invoice not found.', 'error')
        return redirect(url_for('payments'))
        
    return render_template('invoice.html', p=payment, now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/v/<token>')
def public_invoice(token):
    conn = get_db_connection()
    # Fetch payment
    p = conn.execute('SELECT * FROM payments_v3 WHERE public_token = ?', (token,)).fetchone()
    
    if not p:
        conn.close()
        return "Invoice not found or invalid link.", 404
        
    # Fetch subscriber info if available locally for extra details
    sub = conn.execute('SELECT * FROM subscribers WHERE username = ?', (p['username'],)).fetchone()
    conn.close()
    
    # We use a special public template for cleaner view
    return render_template('public_invoice.html', p=p, sub=sub)

@app.route('/vi/<token>')
def public_installation(token):
    conn = get_db_connection()
    p = conn.execute('SELECT * FROM installations WHERE public_token = ?', (token,)).fetchone()
    conn.close()
    
    if not p:
        return "Installation record not found or invalid link.", 404
        
    return render_template('public_installation.html', p=p)

@app.route('/report/export')
def export_report():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') not in ['admin', 'manager']:
        flash('🚫 Unauthorized access.', 'error')
        return redirect(url_for('dashboard'))
    
    start_date = request.args.get('start_date', datetime.now().replace(day=1).strftime('%Y-%m-%d'))
    end_date = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))
    
    conn = get_db_connection()
    
    # Daily breakdown: income by day (Unified Payments + Installations)
    daily_income = conn.execute(
        '''
        SELECT day, SUM(total_syp) as total_syp, SUM(total_usd) as total_usd FROM (
            SELECT date(created_at) as day, SUM(amount) as total_syp, 0 as total_usd FROM payments_v3 WHERE date(created_at) BETWEEN ? AND ? GROUP BY day
            UNION ALL
            SELECT date(updated_at) as day, SUM(payment_amount_syp) as total_syp, SUM(payment_amount_usd) as total_usd FROM installations WHERE status = 'Completed' AND date(updated_at) BETWEEN ? AND ? GROUP BY day
        ) GROUP BY day ORDER BY day ASC
        ''',
        (start_date, end_date, start_date, end_date)
    ).fetchall()
    
    daily_expense = conn.execute(
        "SELECT date(created_at) as day, SUM(amount) as total FROM expenses WHERE date(created_at) BETWEEN ? AND ? GROUP BY day ORDER BY day ASC",
        (start_date, end_date)
    ).fetchall()
    
    day_map = {}
    for row in daily_income:
        if row['day'] not in day_map:
            day_map[row['day']] = {'income_syp': 0, 'income_usd': 0, 'expense': 0}
        day_map[row['day']]['income_syp'] = row['total_syp']
        day_map[row['day']]['income_usd'] = row['total_usd']
    for row in daily_expense:
        if row['day'] not in day_map:
            day_map[row['day']] = {'income_syp': 0, 'income_usd': 0, 'expense': 0}
        day_map[row['day']]['expense'] = row['total']
    
    daily_rows = sorted([
        {
            'day': d, 
            'income_syp': v['income_syp'], 
            'income_usd': v['income_usd'],
            'expense': v['expense'], 
            'net_syp': v['income_syp'] - v['expense']
        }
        for d, v in day_map.items()
    ], key=lambda x: x['day'])
    
    conn.close()
    
    # Generate CSV
    output = io.StringIO()
    # Add BOM for Excel UTF-8
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['Date (التاريخ)', 'Income (الدخل)', 'Expenses (المصاريف)', 'Net (الصافي)'])
    
    total_income = 0
    total_expense = 0
    
    for r in daily_rows:
        writer.writerow([r['day'], r['income_syp'], r['expense'], r['net_syp']])
        total_income += float(r['income_syp'])
        total_expense += float(r['expense'])
    
    writer.writerow([])
    writer.writerow(['TOTAL SUM (المجموع الكلي)', total_income, total_expense, total_income - total_expense])
    
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename=Financial_Report_{start_date}_to_{end_date}.csv'
    return response

@app.route('/admin/users', methods=['GET', 'POST'])
def manage_users():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('🚫 Unauthorized: Admin access only.', 'error')
        return redirect(url_for('dashboard'))
    
    conn = get_db_connection()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            username = request.form.get('username')
            password = request.form.get('password')
            role = request.form.get('role', 'employee')
            m_id = request.form.get('maintenance_id', '')
            phone = request.form.get('phone', '')
            parent = request.form.get('parent', '')
            if username and password:
                try:
                    conn.execute('INSERT INTO users (username, password, role, maintenance_id, phone, parent) VALUES (?, ?, ?, ?, ?, ?)', 
                                 (username, password, role, m_id, phone, parent))
                    conn.commit()
                    flash(f'User {username} added successfully.', 'success')
                except sqlite3.IntegrityError:
                    flash('Username already exists.', 'error')
        elif action == 'edit_user':
            user_id = request.form.get('user_id')
            new_password = request.form.get('password')
            new_phone = request.form.get('phone', '')
            parent = request.form.get('parent', '')
            if user_id:
                if new_password:
                    conn.execute('UPDATE users SET password = ?, phone = ?, parent = ? WHERE id = ?', (new_password, new_phone, parent, user_id))
                else:
                    conn.execute('UPDATE users SET phone = ?, parent = ? WHERE id = ?', (new_phone, parent, user_id))
                conn.commit()
                flash('User updated successfully.', 'success')
        elif action == 'delete':
            user_id = request.form.get('user_id')
            if user_id:
                conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
                conn.commit()
                flash('User deleted successfully.', 'success')

        return redirect(url_for('manage_users'))

    response = fetch_all_users_from_api(session['token'])
    raw_users = response.get('data', [])
    if isinstance(raw_users, dict):
        all_users = list(raw_users.values())
    elif not isinstance(raw_users, list):
        all_users = []
    else:
        all_users = raw_users
        
    all_users = [u for u in all_users if isinstance(u, dict)]
    unique_parents = sorted(list(set([str(u.get('parent_username')) for u in all_users if u.get('parent_username')])))

    users = conn.execute('SELECT id, username, password, role, maintenance_id, phone, parent FROM users').fetchall()
    conn.close()
    return render_template('admin_users.html', users=users, unique_parents=unique_parents)

@app.route('/complaints', methods=['GET', 'POST'])
def complaints():
    if 'token' not in session: return redirect(url_for('login'))
    
    token = session['token']
    user_role = session.get('role')
    user_name = session.get('username')
    
    # Retrieve API data for search cache
    response = fetch_all_users_from_api(token)
    all_users = response.get('data') or []
    api_status = response.get('status', 'online') if isinstance(response, dict) else 'offline'
    last_update_str = datetime.fromtimestamp(response.get('timestamp', time.time())).strftime('%H:%M:%S') if isinstance(response, dict) else ''

    conn = get_db_connection()
    # Retrieve maintenance users for assignment
    maintenance_users = conn.execute('SELECT username, maintenance_id FROM users WHERE role = "maintenance"').fetchall()
    
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':

            username = request.form.get('username')
            fullname = request.form.get('fullname', '')
            phone1 = request.form.get('phone1')
            phone2 = request.form.get('phone2', '')
            area = request.form.get('area', '')
            address = request.form.get('address_details', '')
            text = request.form.get('complaint_text')
            assigned = request.form.get('assigned_to', '')
            connection_type = request.form.get('connection_type', '')
            dish_ip = request.form.get('dish_ip', '')
            user_parent = session.get('parent', '')
            
            if username and phone1 and text:
                conn.execute('''
                    INSERT INTO complaints (username, fullname, phone1, phone2, area, address_details, complaint_text, assigned_to, registered_by, connection_type, dish_ip, parent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (username, fullname, phone1, phone2, area, address, text, assigned, user_name, connection_type, dish_ip, user_parent))
                
                # Save or update subscriber info
                if connection_type:
                    conn.execute('INSERT OR REPLACE INTO subscriber_info (username, connection_type, dish_ip, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)', (username, connection_type, dish_ip))
                
                conn.commit()
                
                # Send Webhook Notification for Complaint
                send_webhook_async({
                    'id': conn.execute('SELECT last_insert_rowid()').fetchone()[0],
                    'type': 'complaint_new',
                    'username': username,
                    'fullname': fullname,
                    'phone': phone1,
                    'area': area,
                    'text': text,
                    'assigned': assigned
                }, webhook_type='complaints', event_name='new', base_url=request.host_url)

                flash('✅ تم تسجيل الشكوى بنجاح.', 'success')
            else:
                flash('❌ يرجى ملء الحقول المطلوبة.', 'error')
        
        elif action == 'update_status':
            c_id = request.form.get('complaint_id')
            new_status = request.form.get('status')
            notes = request.form.get('notes', '').strip()
            assigned_to = request.form.get('assigned_to', None)
            connection_type = request.form.get('connection_type', '')
            dish_ip = request.form.get('dish_ip', '')
            
            if not notes:
                flash('❌ يرجى إضافة ملاحظات الصيانة أو تفاصيل التحديث قبل الحفظ.', 'error')
                return redirect(url_for('complaints'))
            
            # Security: Prevent maintenance from updating already Resolved/Closed complaints
            current_complaint = conn.execute('SELECT * FROM complaints WHERE id = ?', (c_id,)).fetchone()
            if current_complaint and user_role == 'maintenance':
                if current_complaint['status'] in ['Resolved', 'Closed']:
                    flash('🚫 لا يمكن تعديل الشكوى بعد الإصلاح أو الإغلاق من قبل موظف الصيانة.', 'error')
                    return redirect(url_for('complaints'))
                
                # Security: Prevent maintenance from updating complaints assigned to someone else
                assigned = current_complaint['assigned_to']
                if assigned and str(assigned).strip() and assigned != user_name:
                    flash('🚫 هذه الشكوى محالة لموظف صيانة آخر، لا يمكنك تعديلها.', 'error')
                    return redirect(url_for('complaints'))
            
            # Maintenance users add their ID to the record
            m_user = conn.execute('SELECT maintenance_id FROM users WHERE username = ?', (user_name,)).fetchone()
            m_id = m_user['maintenance_id'] if m_user else 'N/A'
            
            # Re-assignment handling
            assigned_changed = False
            if assigned_to is not None:
                # Security: Maintenance can only assign to themselves
                if user_role == 'maintenance' and assigned_to != user_name:
                    flash('🚫 Maintenance staff can only assign tasks to themselves.', 'error')
                else:
                    if current_complaint and assigned_to != current_complaint['assigned_to']:
                        assigned_changed = True
                    conn.execute('UPDATE complaints SET assigned_to = ? WHERE id = ?', (assigned_to, c_id))
                    
                    # Send Webhook Notification for Task Assignment
                    if assigned_to and assigned_changed:
                        emp = conn.execute('SELECT phone, username, maintenance_id FROM users WHERE username = ?', (assigned_to,)).fetchone()
                        if emp and current_complaint:
                            send_webhook_async({
                                'id': c_id,
                                'username': current_complaint['username'],
                                'fullname': current_complaint['fullname'],
                                'phone': current_complaint['phone1'],
                                'area': current_complaint['area'],
                                'text': current_complaint['complaint_text'],
                                'employee_name': emp['username'],
                                'employee_phone': emp['phone'] or emp['maintenance_id'] or '',
                                'event': 'assign',
                                'status': new_status
                            }, webhook_type='complaints', event_name='assign', base_url=request.host_url)

            # Record the log entry
            conn.execute('''
                INSERT INTO complaint_logs (complaint_id, action_by, old_status, new_status, notes)
                VALUES (?, ?, ?, ?, ?)
            ''', (c_id, user_name, current_complaint['status'] if current_complaint else 'Unknown', new_status, notes))

            conn.execute('''
                UPDATE complaints 
                SET status = ?, maintenance_notes = ?, maintenance_user_id = ?, connection_type = ?, dish_ip = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (new_status, notes, m_id, connection_type, dish_ip, c_id))
            
            # Save or update subscriber info
            if current_complaint and connection_type:
                conn.execute('INSERT OR REPLACE INTO subscriber_info (username, connection_type, dish_ip, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)', (current_complaint['username'], connection_type, dish_ip))
                
            conn.commit()
            
            # Send Webhook Notification for Complaint Resolution/Update
            if new_status == 'Resolved':
                send_webhook_async({
                    'id': c_id,
                    'username': current_complaint['username'] if current_complaint else '',
                    'notes': notes,
                    'status': new_status
                }, webhook_type='complaints', event_name='resolve', base_url=request.host_url)
            elif not assigned_changed:
                send_webhook_async({
                    'id': c_id,
                    'username': current_complaint['username'] if current_complaint else '',
                    'notes': notes,
                    'status': new_status
                }, webhook_type='complaints', event_name='update', base_url=request.host_url)
            
            flash('✅ تم تحديث الشكوى بنجاح.', 'success')

        elif action == 'assign_self':
            c_id = request.form.get('complaint_id')
            if user_role == 'maintenance':
                current_c = conn.execute('SELECT assigned_to, status FROM complaints WHERE id = ?', (c_id,)).fetchone()
                if current_c and current_c['assigned_to'] and str(current_c['assigned_to']).strip() and current_c['assigned_to'] != user_name:
                    flash('🚫 المهمة محالة بالفعل لموظف آخر ولا يمكنك استلامها.', 'error')
                    return redirect(url_for('complaints'))

                conn.execute('UPDATE complaints SET assigned_to = ? WHERE id = ?', (user_name, c_id))
                conn.commit()
                
                comp = conn.execute('SELECT * FROM complaints WHERE id = ?', (c_id,)).fetchone()
                emp = conn.execute('SELECT phone, username, maintenance_id FROM users WHERE username = ?', (user_name,)).fetchone()
                if comp and emp:
                    send_webhook_async({
                        'id': c_id,
                        'username': comp['username'],
                        'fullname': comp['fullname'],
                        'phone': comp['phone1'],
                        'area': comp['area'],
                        'text': comp['complaint_text'],
                        'employee_name': emp['username'],
                        'employee_phone': emp['phone'] or emp['maintenance_id'] or '',
                        'event': 'assign',
                        'status': comp['status']
                    }, webhook_type='complaints', event_name='assign')
                
                flash('✅ تم استلام المهمة بنجاح.', 'success')
            else:
                flash('🚫 Unauthorized.', 'error')

        return redirect(url_for('complaints'))

    # Stats calculation
    stats = {}
    stats['total_all'] = conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0]
    stats['total_my'] = conn.execute('SELECT COUNT(*) FROM complaints WHERE assigned_to = ?', (user_name,)).fetchone()[0]
    stats['resolved_my'] = conn.execute('SELECT COUNT(*) FROM complaints WHERE status = "Resolved" AND maintenance_user_id = (SELECT maintenance_id FROM users WHERE username = ?)', (user_name,)).fetchone()[0]
    stats['resolved_all'] = conn.execute('SELECT COUNT(*) FROM complaints WHERE status = "Resolved"').fetchone()[0]

    # Filtering logic
    query = 'SELECT * FROM complaints'
    params = []
    where = []
    
    user_parent = session.get('parent')
    if user_role in ['employee', 'manager'] and user_parent:
        where.append('parent = ?')
        params.append(user_parent)
    
    status_filter = request.args.get('status', '')
    if status_filter:
        where.append('status = ?')
        params.append(status_filter)
        
    assigned_filter = request.args.get('assigned', '')
    if assigned_filter == 'me':
        where.append('assigned_to = ?')
        params.append(user_name)
    elif assigned_filter == 'unassigned':
        where.append("(assigned_to IS NULL OR assigned_to = '')")
    elif assigned_filter:
        where.append('assigned_to = ?')
        params.append(assigned_filter)
        
    search = request.args.get('search', '')
    if search:
        search_pattern = f"%{search}%"
        where.append('(username LIKE ? OR fullname LIKE ? OR phone1 LIKE ? OR phone2 LIKE ? OR area LIKE ? OR address_details LIKE ? OR assigned_to LIKE ?)')
        params.extend([search_pattern] * 7)
        
    if where:
        query += ' WHERE ' + ' AND '.join(where)
        
    query += ' ORDER BY created_at DESC'
    all_complaints = conn.execute(query, params).fetchall()
    
    # Merge subscriber_info into all_users for the frontend to pre-fill
    subscriber_info_rows = conn.execute('SELECT * FROM subscriber_info').fetchall()
    subscriber_info_dict = {row['username']: dict(row) for row in subscriber_info_rows}
    
    for u in all_users:
        uname = u.get('username')
        if uname in subscriber_info_dict:
            u['connection_type'] = subscriber_info_dict[uname].get('connection_type', '')
            u['dish_ip'] = subscriber_info_dict[uname].get('dish_ip', '')
            
    conn.close()

    return render_template(
        'complaints.html',
        complaints=all_complaints,
        all_users=all_users,
        maintenance_users=maintenance_users,
        stats=stats,
        api_status=api_status,
        last_update_str=last_update_str
    )

@app.route('/complaints/view/<int:c_id>')
def view_complaint(c_id):
    if 'token' not in session: return redirect(url_for('login'))
    
    conn = get_db_connection()
    complaint = conn.execute('SELECT * FROM complaints WHERE id = ?', (c_id,)).fetchone()
    logs = conn.execute('SELECT * FROM complaint_logs WHERE complaint_id = ? ORDER BY created_at DESC', (c_id,)).fetchall()
    
    # Fetch all previous/other complaints by the same username
    user_history = []
    if complaint:
        user_history = conn.execute('''
            SELECT id, status, complaint_text, created_at, maintenance_notes, connection_type, dish_ip 
            FROM complaints 
            WHERE username = ? AND id != ?
            ORDER BY created_at DESC
        ''', (complaint['username'], c_id)).fetchall()
        
    conn.close()
    
    if not complaint:
        flash('الشكوى غير موجودة.', 'error')
        return redirect(url_for('complaints'))
        
    return render_template('complaint_details.html', c=complaint, logs=logs, user_history=user_history)

@app.route('/complaints/export')
def export_complaints():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') not in ['admin', 'manager']:
        flash('🚫 Unauthorized.', 'error')
        return redirect(url_for('dashboard'))
        
    conn = get_db_connection()
    complaints_data = conn.execute('SELECT * FROM complaints ORDER BY created_at DESC').fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Username', 'Full Name', 'Connection', 'Dish IP', 'Phone 1', 'Phone 2', 'Area', 'Address Details', 'Complaint', 'Status', 'Maintenance Notes', 'Assigned To', 'Maintenance ID', 'Date'])
    
    for c in complaints_data:
        writer.writerow([c['id'], c['username'], c['fullname'], dict(c).get('connection_type', ''), dict(c).get('dish_ip', ''), c['phone1'], c['phone2'], c['area'], c['address_details'], c['complaint_text'], c['status'], c['maintenance_notes'], c['assigned_to'], c['maintenance_user_id'], c['created_at']])

    csv_data = '\ufeff' + output.getvalue()
    filename = f"complaints_{datetime.now().strftime('%Y-%m-%d')}.csv"
    
    return Response(
        csv_data, 
        mimetype="text/csv", 
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

@app.route('/complaints/reports')
def complaints_reports():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') not in ['admin', 'manager']:
        flash('🚫 غير مصرح لك بدخول هذه الصفحة.', 'error')
        return redirect(url_for('complaints'))
        
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    conn = get_db_connection()
    
    where_clause = ""
    params = []
    if start_date:
        where_clause += " AND DATE(created_at) >= ?"
        params.append(start_date)
    if end_date:
        where_clause += " AND DATE(created_at) <= ?"
        params.append(end_date)
        
    # Global Stats
    total = conn.execute(f"SELECT COUNT(*) FROM complaints WHERE 1=1 {where_clause}", params).fetchone()[0]
    open_c = conn.execute(f"SELECT COUNT(*) FROM complaints WHERE status = 'Open' {where_clause}", params).fetchone()[0]
    progress_c = conn.execute(f"SELECT COUNT(*) FROM complaints WHERE status = 'In Progress' {where_clause}", params).fetchone()[0]
    resolved_c = conn.execute(f"SELECT COUNT(*) FROM complaints WHERE status IN ('Resolved', 'Closed') {where_clause}", params).fetchone()[0]
    
    # Per-Staff Stats
    # We use complaint_logs to find who actually resolved the complaints
    staff_stats = conn.execute(f'''
        SELECT 
            u.username,
            u.maintenance_id,
            (SELECT COUNT(*) FROM complaints c WHERE c.assigned_to = u.username {where_clause}) as total_assigned,
            (SELECT COUNT(DISTINCT l.complaint_id) FROM complaint_logs l 
             WHERE l.action_by = u.username AND l.new_status IN ('Resolved', 'Closed')
             {"AND DATE(l.created_at) >= ?" if start_date else ""}
             {"AND DATE(l.created_at) <= ?" if end_date else ""}
            ) as total_resolved,
            (SELECT COUNT(*) FROM complaint_logs l 
             WHERE l.action_by = u.username
             {"AND DATE(l.created_at) >= ?" if start_date else ""}
             {"AND DATE(l.created_at) <= ?" if end_date else ""}
            ) as total_actions
        FROM users u
        WHERE u.role = 'maintenance'
    ''', [p for p in params for _ in range(3)] if params else []).fetchall()
    
    # Data for Timeline Chart
    timeline = conn.execute(f'''
        SELECT DATE(created_at) as day, COUNT(*) as count
        FROM complaints
        WHERE 1=1 {where_clause}
        GROUP BY day ORDER BY day ASC LIMIT 30
    ''', params).fetchall()
    
    conn.close()
    
    return render_template('complaints_reports.html', 
        total=total, open=open_c, progress=progress_c, resolved=resolved_c,
        staff_stats=staff_stats, timeline=timeline,
        start_date=start_date, end_date=end_date)

@app.route('/profile/password', methods=['GET', 'POST'])
def change_password():
    if 'token' not in session: return redirect(url_for('login'))
    
    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if new_password and new_password == confirm_password:
            conn = get_db_connection()
            conn.execute('UPDATE users SET password = ? WHERE username = ?', (new_password, session['username']))
            conn.commit()
            conn.close()
            flash('Your password has been changed successfully.', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Passwords do not match.', 'error')
            
    return render_template('change_password.html')

@app.route('/set_lang/<lang>')
def set_lang(lang):
    if lang in ['ar', 'en']:
        session['lang'] = lang
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/installations', methods=['GET', 'POST'])
def installations():
    if 'token' not in session: return redirect(url_for('login'))
    
    user_role = session.get('role')
    user_name = session.get('username')
    conn = get_db_connection()
    maintenance_users = conn.execute('SELECT username, maintenance_id FROM users WHERE role = "maintenance"').fetchall()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            if user_role not in ['admin', 'manager', 'employee']:
                flash('🚫 غير مصرح.', 'error')
                return redirect(url_for('installations'))
            fullname = request.form.get('fullname', '').strip()
            phone1 = request.form.get('phone1', '').strip()
            phone2 = request.form.get('phone2', '')
            area = request.form.get('area', '')
            address = request.form.get('address_details', '')
            notes = request.form.get('notes', '')
            user_parent = session.get('parent', '')
            if fullname and phone1:
                public_token = str(uuid.uuid4())
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO installations (fullname, phone1, phone2, area, address_details, notes, registered_by, parent, public_token)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (fullname, phone1, phone2, area, address, notes, user_name, user_parent, public_token))
                new_id = cursor.lastrowid
                conn.commit()
                
                # Send Webhook Notification for Installation Request
                send_webhook_async({
                    'id': new_id,
                    'type': 'installation_new',
                    'fullname': fullname,
                    'phone': phone1,
                    'area': area,
                    'address': address,
                    'notes': notes,
                    'public_token': public_token
                }, webhook_type='installations', base_url=request.host_url)

                flash('✅ تم تسجيل طلب التركيب بنجاح.', 'success')
            else:
                flash('❌ يرجى ملء الاسم ورقم الهاتف.', 'error')

        elif action == 'assign':
            inst_id = request.form.get('inst_id')
            assigned_user = request.form.get('assigned_to')
            
            if user_role == 'maintenance':
                assigned_user = user_name
            elif user_role not in ['admin', 'manager']:
                flash('🚫 غير مصرح.', 'error')
                return redirect(url_for('installations'))
                
            if assigned_user:
                conn.execute('UPDATE installations SET assigned_to = ?, status = "Assigned", updated_at = CURRENT_TIMESTAMP WHERE id = ?', (assigned_user, inst_id))
                conn.commit()
                
                inst = conn.execute('SELECT * FROM installations WHERE id = ?', (inst_id,)).fetchone()
                emp = conn.execute('SELECT phone, username, maintenance_id FROM users WHERE username = ?', (assigned_user,)).fetchone()
                if inst and emp:
                    send_webhook_async({
                        'id': inst_id,
                        'type': 'installation_assigned',
                        'employee_name': emp['username'],
                        'employee_phone': emp['phone'] or emp['maintenance_id'] or '',
                        'status': 'Assigned',
                        'public_token': inst['public_token']
                    }, webhook_type='installations', event_name='assign', base_url=request.host_url)
                    
                flash('✅ تم تعيين مهمة التركيب بنجاح.', 'success')

        elif action == 'cancel':
            inst_id = request.form.get('inst_id')
            if user_role in ['admin', 'manager', 'employee']:
                conn.execute('UPDATE installations SET status = "Cancelled", updated_at = CURRENT_TIMESTAMP WHERE id = ?', (inst_id,))
                conn.commit()
                flash('❌ تم إلغاء طلب التركيب.', 'info')
            else:
                flash('🚫 غير مصرح لك بإلغاء الطلب.', 'error')

        elif action == 'delete':
            inst_id = request.form.get('inst_id')
            if user_role in ['admin', 'manager']:
                conn.execute('DELETE FROM installations WHERE id = ?', (inst_id,))
                conn.commit()
                flash('🗑️ تم حذف طلب التركيب نهائياً.', 'success')
            else:
                flash('🚫 غير مصرح لك بحذف البيانات.', 'error')

        elif action == 'edit':
            if user_role in ['admin', 'manager']:
                inst_id = request.form.get('inst_id')
                fullname = request.form.get('fullname', '').strip()
                phone1 = request.form.get('phone1', '').strip()
                phone2 = request.form.get('phone2', '')
                area = request.form.get('area', '')
                address = request.form.get('address_details', '')
                notes = request.form.get('notes', '')
                
                if fullname and phone1:
                    conn.execute('''
                        UPDATE installations 
                        SET fullname = ?, phone1 = ?, phone2 = ?, area = ?, address_details = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (fullname, phone1, phone2, area, address, notes, inst_id))
                    conn.commit()
                    
                    inst = conn.execute('SELECT * FROM installations WHERE id = ?', (inst_id,)).fetchone()
                    emp = conn.execute('SELECT phone, username, maintenance_id FROM users WHERE username = ?', (inst['assigned_to'] or '',)).fetchone() if inst and inst['assigned_to'] else None
                    if inst:
                        send_webhook_async({
                            'id': inst_id,
                            'type': 'installation_updated',
                            'employee_name': emp['username'] if emp else '',
                            'employee_phone': emp['phone'] or emp['maintenance_id'] or '' if emp else '',
                            'status': inst['status'],
                            'public_token': inst['public_token']
                        }, webhook_type='installations', event_name='update', base_url=request.host_url)
                        
                    flash('✅ تم تحديث بيانات طلب التركيب.', 'success')
                else:
                    flash('❌ يرجى ملء الحقول المطلوبة.', 'error')
            else:
                flash('🚫 غير مصرح لك بتعديل البيانات.', 'error')

        elif action == 'complete':
            inst_id = request.form.get('inst_id')
            payment_amount_usd = request.form.get('payment_amount_usd', 0) or 0
            payment_amount_syp = request.form.get('payment_amount_syp', 0) or 0
            payment_notes = request.form.get('payment_notes', '')
            connection_type = request.form.get('connection_type', '')
            dish_ip = request.form.get('dish_ip', '')
            inst = conn.execute('SELECT * FROM installations WHERE id = ?', (inst_id,)).fetchone()
            
            inv_item_ids = request.form.getlist('inv_item_id[]')
            inv_qtys = request.form.getlist('inv_qty[]')

            if inst and (user_role in ['admin', 'manager'] or inst['assigned_to'] == user_name):
                # Process inventory items used during installation
                total_items_cost = 0
                for item_id, qty_str in zip(inv_item_ids, inv_qtys):
                    if item_id and qty_str.isdigit():
                        qty = int(qty_str)
                        if qty > 0:
                            item = conn.execute('SELECT id, stock_qty, sell_price, name FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
                            if item and item['stock_qty'] >= qty:
                                sell_price = item['sell_price']
                                total_amount = qty * sell_price
                                conn.execute('''
                                    INSERT INTO inventory_sales (item_id, quantity, sell_price, discount, total_amount, installation_id, admin_name, notes)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''', (item['id'], qty, sell_price, 0, total_amount, inst_id, user_name, f'مستهلك بتركيب #{inst_id}'))
                                conn.execute('UPDATE inventory_items SET stock_qty = stock_qty - ? WHERE id = ?', (qty, item['id']))
                                total_items_cost += total_amount

                conn.execute('''
                    UPDATE installations
                    SET status = "Completed", 
                        payment_amount_usd = ?, 
                        payment_amount_syp = ?, 
                        payment_notes = ?, 
                        connection_type = ?, 
                        dish_ip = ?, 
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (float(payment_amount_usd), float(payment_amount_syp), payment_notes, connection_type, dish_ip, inst_id))
                conn.commit()
                
                # Send Webhook Notification for Installation Completion
                emp = conn.execute('SELECT phone, username, maintenance_id FROM users WHERE username = ?', (inst['assigned_to'] or '',)).fetchone() if inst['assigned_to'] else None
                
                discount = float(request.form.get('discount', 0) or 0)
                send_webhook_async({
                    'id': inst_id,
                    'type': 'installation_completed',
                    'connection_type': connection_type,
                    'dish_ip': dish_ip,
                    'payment_amount_usd': float(payment_amount_usd),
                    'payment_amount_syp': float(payment_amount_syp),
                    'discount': discount,
                    'total_items_cost': total_items_cost,
                    'public_token': inst['public_token']
                }, webhook_type='installations', event_name='complete', base_url=request.host_url)

                flash('✅ تم انهاء التركيب وتسجيل الدفعة.', 'success')
            else:
                flash('🚫 غير مصرح.', 'error')
                
        return redirect(url_for('installations'))

    # Filtering
    query = 'SELECT * FROM installations'
    where = []
    params = []
    
    user_parent = session.get('parent')
    if user_role in ['employee', 'manager'] and user_parent:
        where.append('parent = ?')
        params.append(user_parent)
        
    status_filter = request.args.get('status', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    if status_filter:
        where.append('status = ?')
        params.append(status_filter)
    
    if start_date:
        where.append("DATE(created_at) >= ?")
        params.append(start_date)
    
    if end_date:
        where.append("DATE(created_at) <= ?")
        params.append(end_date)

    if user_role == 'maintenance':
        where.append("(status = 'Pending' OR assigned_to = ?)")
        params.append(user_name)

    if where:
        query += ' WHERE ' + ' AND '.join(where)
    query += ' ORDER BY created_at DESC'
    all_installations = [dict(row) for row in conn.execute(query, params).fetchall()]

    stats = {
        'total': conn.execute('SELECT COUNT(*) FROM installations').fetchone()[0],
        'pending': conn.execute('SELECT COUNT(*) FROM installations WHERE status = "Pending"').fetchone()[0],
        'assigned': conn.execute('SELECT COUNT(*) FROM installations WHERE status = "Assigned"').fetchone()[0],
        'completed': conn.execute('SELECT COUNT(*) FROM installations WHERE status = "Completed"').fetchone()[0],
        'total_revenue_usd': conn.execute('SELECT COALESCE(SUM(payment_amount_usd),0) FROM installations WHERE status = "Completed"').fetchone()[0],
        'total_revenue_syp': conn.execute('SELECT COALESCE(SUM(payment_amount_syp),0) FROM installations WHERE status = "Completed"').fetchone()[0],
    }

    rate_row = conn.execute("SELECT value FROM settings WHERE key = 'exchange_rate'").fetchone()
    exchange_rate = float(rate_row['value']) if rate_row and rate_row['value'] else 0

    conn.close()
    return render_template('installations.html', installations=all_installations, maintenance_users=maintenance_users, stats=stats, exchange_rate=exchange_rate)

@app.route('/installations/export')
def export_installations():
    if 'token' not in session: return redirect(url_for('login'))
    if session.get('role') not in ['admin', 'manager']:
        flash('🚫 Unauthorized.', 'error')
        return redirect(url_for('installations'))
    conn = get_db_connection()
    data = conn.execute('SELECT * FROM installations ORDER BY created_at DESC').fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Full Name', 'Phone 1', 'Phone 2', 'Area', 'Address', 'Notes', 'Status', 'Assigned To', 'Registered By', 'Amount (USD)', 'Amount (SYP)', 'Payment Notes', 'Connection Type', 'Dish IP', 'Created At'])
    for r in data:
        writer.writerow([r['id'], r['fullname'], r['phone1'], r['phone2'], r['area'], r['address_details'], r['notes'], r['status'], r['assigned_to'], r['registered_by'], r['payment_amount_usd'], r['payment_amount_syp'], r['payment_notes'], r['connection_type'], r['dish_ip'], r['created_at']])
    csv_data = '\ufeff' + output.getvalue()
    filename = f"installations_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(csv_data, mimetype='text/csv', headers={'Content-Disposition': f'attachment;filename={filename}'})



@app.route('/admin/packages')
def manage_packages():
    if 'token' not in session or session.get('role') not in ['admin', 'manager']:
        return redirect(url_for('login'))
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM landing_packages').fetchall()
    packages = [dict(row) for row in rows]
    conn.close()
    return render_template('admin_packages.html', packages=packages)

@app.route('/admin/packages/add', methods=['POST'])
def add_package():
    if 'token' not in session or session.get('role') not in ['admin', 'manager']:
        return redirect(url_for('login'))
    name = request.form.get('name')
    speed = request.form.get('speed')
    price_syp = request.form.get('price_syp')
    price_usd = request.form.get('price_usd')
    description = request.form.get('description')
    
    conn = get_db_connection()
    conn.execute('INSERT INTO landing_packages (name, speed, price_syp, price_usd, description) VALUES (?, ?, ?, ?, ?)',
                 (name, speed, price_syp, price_usd, description))
    conn.commit()
    conn.close()
    flash('✅ Package added successfully.', 'success')
    return redirect(url_for('manage_packages'))

@app.route('/admin/packages/edit/<int:pkg_id>', methods=['POST'])
def edit_package(pkg_id):
    if 'token' not in session or session.get('role') not in ['admin', 'manager']:
        return redirect(url_for('login'))
    name = request.form.get('name')
    speed = request.form.get('speed')
    price_syp = request.form.get('price_syp')
    price_usd = request.form.get('price_usd')
    description = request.form.get('description')
    is_active = 1 if request.form.get('is_active') == 'on' else 0
    
    conn = get_db_connection()
    conn.execute('UPDATE landing_packages SET name=?, speed=?, price_syp=?, price_usd=?, description=?, is_active=? WHERE id=?',
                 (name, speed, price_syp, price_usd, description, is_active, pkg_id))
    conn.commit()
    conn.close()
    flash('✅ Package updated successfully.', 'success')
    return redirect(url_for('manage_packages'))

@app.route('/admin/packages/delete/<int:pkg_id>')
def delete_package(pkg_id):
    if 'token' not in session or session.get('role') not in ['admin', 'manager']:
        return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute('DELETE FROM landing_packages WHERE id=?', (pkg_id,))
    conn.commit()
    conn.close()
    flash('🗑️ Package deleted successfully.', 'success')
    return redirect(url_for('manage_packages'))

@app.route('/client/login', methods=['POST'])
def client_login():
    username = request.form.get('username')
    password = request.form.get('password')
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user = conn.execute('SELECT * FROM subscribers WHERE username = ?', (username,)).fetchone()
    conn.close()
    
    if user:
        if user['password'] == password:
            session['username'] = username
            session['is_client'] = True
            flash(f'👋 أهلاً بك {user["firstname"] or username}', 'success')
            return redirect(url_for('client_portal'))
        else:
            flash('❌ كلمة المرور غير صحيحة.', 'error')
    else:
        flash('❌ اسم المستخدم غير موجود محلياً. يرجى التواصل مع الإدارة للمزامنة.', 'error')
        
    return redirect(url_for('landing'))

    username = session['username']
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user = conn.execute('SELECT * FROM subscribers WHERE username = ?', (username,)).fetchone()
    conn.close()
    
    if not user:
        flash('⚠️ لم يتم العثور على بياناتك محلياً.', 'warning')
        return redirect(url_for('landing'))
    
    # Load details from the cached JSON blob
    try:
        details = json.loads(user['json_data'] or '{}')
    except:
        details = dict(user)
    
    return render_template('client_portal.html', details=details)

@app.route('/debug')
def debug_portal():
    # Collect attempts from both clients
    all_attempts = []
    for att in subscriber_client.attempts:
        all_attempts.append({'portal': 'Subscriber/User', **att})
    for att in sasclient.attempts:
        all_attempts.append({'portal': 'Admin/Manager', **att})
    
    html = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>SAS Debug Portal</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700&display=swap" rel="stylesheet">
        <style>body { font-family: 'Cairo', sans-serif; background: #0f172a; color: white; }</style>
    </head>
    <body class="p-8">
        <div class="max-w-5xl mx-auto">
            <h1 class="text-3xl font-bold mb-8 text-blue-400">سجل محاولات الاتصال بالسيرفر (Debug)</h1>
            <p class="mb-6 text-slate-400">هذه الصفحة تظهر لك الروابط التي جربها البرنامج والنتيجة القادمة من السيرفر.</p>
            
            <div class="overflow-hidden rounded-xl border border-white/10 bg-slate-900 shadow-2xl">
                <table class="w-full text-right">
                    <thead>
                        <tr class="bg-slate-800 text-slate-300 text-sm">
                            <th class="p-4 text-sm font-bold">البوابة</th>
                            <th class="p-4 text-sm font-bold">الرابط المجرب (URL)</th>
                            <th class="p-4 text-sm font-bold">الحالة (Status)</th>
                            <th class="p-4 text-sm font-bold">محتوى الرد (Response Sample)</th>
                            <th class="p-4 text-sm font-bold">تفاصيل الخطأ</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-white/5">
                        """
    for att in all_attempts:
        status_color = "text-emerald-400" if att['status'] == 200 else "text-red-400"
        html += f"""
                        <tr class="hover:bg-white/5 transition-colors">
                            <td class="p-4 font-bold">{att['portal']}</td>
                            <td class="p-4 font-mono text-[10px] text-slate-300">{att['url']}</td>
                            <td class="p-4 font-bold {status_color}">{att['status']}</td>
                            <td class="p-4 font-mono text-[10px] text-slate-400 max-w-xs overflow-hidden text-ellipsis whitespace-nowrap">{att.get('resp_sample', '-')}</td>
                            <td class="p-4 text-xs text-slate-500">{att.get('error_detail', att.get('msg', '-'))}</td>
                        </tr>
        """
    
    if not all_attempts:
        html += """<tr><td colspan="4" class="p-20 text-center text-slate-500">لا توجد محاولات مسجلة بعد. حاول تسجيل الدخول أولاً.</td></tr>"""
        
    html += """
                    </tbody>
                </table>
            </div>
            <div class="mt-8 flex justify-center">
                <a href="/" class="bg-blue-600 hover:bg-blue-500 text-white px-8 py-3 rounded-xl font-bold transition-all">العودة للرئيسية</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    # Startup Sync: attempt to login and refresh cache silently on boot
    def startup_sync():
        global sasclient, SAS_API_IP, SAS_ADMIN_USER, SAS_ADMIN_PASS
        try:
            # Short delay to ensure Flask/Server is properly initialized
            time.sleep(10)
            
            # Load saved settings from DB if available
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                ip_row = conn.execute("SELECT value FROM settings WHERE key = 'sas_ip'").fetchone()
                user_row = conn.execute("SELECT value FROM settings WHERE key = 'sas_user'").fetchone()
                pass_row = conn.execute("SELECT value FROM settings WHERE key = 'sas_pass'").fetchone()
                conn.close()
                
                if ip_row and ip_row['value']:
                    SAS_API_IP = ip_row['value']
                    sasclient = SasAPI(f"https://{SAS_API_IP}", portal='admin')
                    print(f"DEBUG: Startup Sync: Loaded SAS IP from DB: {SAS_API_IP}")
                if user_row and user_row['value']:
                    SAS_ADMIN_USER = user_row['value']
                if pass_row and pass_row['value']:
                    SAS_ADMIN_PASS = pass_row['value']
            except Exception as db_e:
                print(f"DEBUG: Startup Sync: Could not load DB settings, using .env defaults: {db_e}")

            result = sasclient.login(SAS_ADMIN_USER, SAS_ADMIN_PASS)
            token = result[0] if isinstance(result, tuple) else result
            if token:
                print("DEBUG: Startup Sync: Logged into SAS successfully. Starting silent fetch...")
                background_refresh(token)
            else:
                error_msg = result[1] if isinstance(result, tuple) else 'Unknown'
                print(f"DEBUG: Startup Sync: SAS Login failed ({error_msg}). Cache will update on next user login.")
        except Exception as e:
            print(f"DEBUG: Startup Sync error: {e}")
            
    threading.Thread(target=startup_sync, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
