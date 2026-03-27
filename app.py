import sqlite3
import csv
import io
import math
import time
import os
import random
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, send_file, send_from_directory, make_response
from sas import SasAPI
import json
import aes
import requests
import threading
from typing import Any

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'super_secret_sas_dashboard_key_123')

# PWA Routes
@app.route('/sw.js')
def sw():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

# Initialize the SAS API client with the user's server IP
SAS_API_IP = os.getenv('SAS_API_IP', '193.43.140.218')
sasclient = SasAPI(f"https://{SAS_API_IP}")

# Webhook for n8n/WhatsApp (Set this in environment variables)
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')

# Database Path
DB_PATH = os.getenv('DB_PATH', 'payments.db')

USER_CACHE: dict[str, Any] = {
    'data': None,
    'total': 0,
    'timestamp': 0.0,
    'is_refreshing': False
}
CACHE_DURATION = 14400 # 4 hours cache for better performance by preserving data in DB

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Migration: Add phone column if it doesn't exist
    try:
        c.execute("ALTER TABLE payments_v3 ADD COLUMN phone TEXT")
    except sqlite3.OperationalError:
        pass # Already exists

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Seed default webhooks from ENV if not set
    default_webhook = os.getenv('WEBHOOK_URL', '')
    for key in ['webhook_payments', 'webhook_complaints', 'webhook_installations']:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, default_webhook))
    
    # Seed toggles
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
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            maintenance_id TEXT
        )
    ''')

    # Migrations for users table
    for col_name in ['phone', 'maintenance_id']:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col_name} TEXT")
        except sqlite3.OperationalError:
            pass # Already exists

    # Migration for complaints table
    try:
        c.execute("ALTER TABLE complaints ADD COLUMN assigned_to TEXT")
    except sqlite3.OperationalError:
        pass # Already exists

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
            registered_by TEXT,
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

    # Migration: Add registered_by column if it doesn't exist
    try:
        c.execute("ALTER TABLE complaints ADD COLUMN registered_by TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
        
    # Migration: Add connection_type column to complaints
    try:
        c.execute("ALTER TABLE complaints ADD COLUMN connection_type TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
        
    # Migration: Add dish_ip column to complaints
    try:
        c.execute("ALTER TABLE complaints ADD COLUMN dish_ip TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
        
    # Migration: Add parent column to user schemas
    try:
        c.execute("ALTER TABLE users ADD COLUMN parent TEXT")
    except sqlite3.OperationalError: pass
    try:
        c.execute("ALTER TABLE complaints ADD COLUMN parent TEXT")
    except sqlite3.OperationalError: pass
    try:
        c.execute("ALTER TABLE installations ADD COLUMN parent TEXT")
    except sqlite3.OperationalError: pass
        
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriber_info (
            username TEXT PRIMARY KEY,
            connection_type TEXT,
            dish_ip TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Migration: Add connection_type and dish_ip to installations
    try:
        c.execute("ALTER TABLE installations ADD COLUMN connection_type TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE installations ADD COLUMN dish_ip TEXT")
    except sqlite3.OperationalError:
        pass
    
    # Migration: Add USD and SYP payment columns
    try:
        c.execute("ALTER TABLE installations ADD COLUMN payment_amount_usd REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE installations ADD COLUMN payment_amount_syp REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add a default admin user if not exists
    c.execute('SELECT count(*) FROM users WHERE username = "admin"')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', ('admin', 'admin@123', 'admin'))

    c.execute('''
        CREATE TABLE IF NOT EXISTS sas_cache (
            id INTEGER PRIMARY KEY,
            json_data TEXT,
            total INTEGER,
            updated_at REAL
        )
    ''')

    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def fetch_all_users_from_api(token, force_refresh=False) -> dict[str, Any]:
    """
    Fetches users from the SAS API. 
    Checks memory first, then DB, and refreshes in background if needed.
    """
    now = time.time()
    
    # 1. Load from DB Cache if memory is empty
    if USER_CACHE['data'] is None:
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT json_data, total, updated_at FROM sas_cache WHERE id = 1").fetchone()
            conn.close()
            if row:
                USER_CACHE['data'] = json.loads(row[0])
                USER_CACHE['total'] = row[1]
                USER_CACHE['timestamp'] = row[2]
        except Exception as e:
            print(f"DEBUG: DB Cache error: {e}")

    # 2. Return if fresh
    if not force_refresh and USER_CACHE['data'] is not None and (now - USER_CACHE['timestamp'] < CACHE_DURATION):
        return {'data': USER_CACHE['data'], 'total': USER_CACHE['total'], 'status': 'cached_recent', 'timestamp': USER_CACHE['timestamp']}
    
    # 3. If refreshing already
    if USER_CACHE['is_refreshing']:
        return {'data': USER_CACHE['data'], 'total': USER_CACHE['total'], 'status': 'refreshing', 'timestamp': USER_CACHE['timestamp']}

    def background_refresh(token_inner):
        USER_CACHE['is_refreshing'] = True
        try:
            payload_dict = {
                "page": 1,
                "count": 5000, 
                "sortBy": "username",
                "direction": "asc",
                "search": "",
            }
            encrypted_payload = aes.encrypt(json.dumps(payload_dict))
            print(f"DEBUG: Triggering SAS API fetch for 'index/user'...")
            response = sasclient.post(token_inner, 'index/user', encrypted_payload)
            
            if isinstance(response, dict) and 'data' in response:
                users = response.get('data') or []
                total = response.get('total') or 0
                ts = time.time()
                
                USER_CACHE['data'] = users
                USER_CACHE['total'] = total
                USER_CACHE['timestamp'] = ts
                
                # Save to DB asynchronously inside the background task
                try:
                    conn = sqlite3.connect(DB_PATH)
                    json_str = json.dumps(users)
                    conn.execute("INSERT OR REPLACE INTO sas_cache (id, json_data, total, updated_at) VALUES (1, ?, ?, ?)", (json_str, total, ts))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"DEBUG: Failed to save to sas_cache DB: {e}")
                    
                print(f"DEBUG: SAS API Success! Fetched {len(users)} users. Total: {total}")
            else:
                print(f"DEBUG: SAS API Failed or empty. Response type: {type(response)}, Status/Result: {response}")
        except Exception as e:
            print(f"DEBUG: SAS API Exception during fetch: {e}")
        finally:
            USER_CACHE['is_refreshing'] = False

    # 4. If memory is STILL EMPTY (first time run && db empty), block and refresh
    if USER_CACHE['data'] is None or force_refresh:
        background_refresh(token)
        return {'data': USER_CACHE['data'], 'total': USER_CACHE['total'], 'status': 'online', 'timestamp': USER_CACHE['timestamp']}
    else:
        # 5. We have STALE data (DB or Mem > 4 hours old). Return immediately & refresh
        threading.Thread(target=background_refresh, args=(token,), daemon=True).start()
        return {'data': USER_CACHE['data'], 'total': USER_CACHE['total'], 'status': 'cached_stale', 'timestamp': USER_CACHE['timestamp']}

def send_webhook_async(data, webhook_type='payments', event_name=None):
    """Wrapper to run send_webhook in a background thread."""
    threading.Thread(target=send_webhook, args=(data, None, webhook_type, event_name), daemon=True).start()

def send_webhook(data, webhook_url_override=None, webhook_type='payments', event_name=None):
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
            payload = {
                "id": data.get('id', 'test_id'),
                "invoice_no": f"SAS-{data.get('id', '0000')}",
                "username": data.get('username', 'test_user'),
                "fullname": data.get('fullname', 'Test User'),
                "profile_name": data.get('profile_name', 'Test Profile'),
                "amount": data.get('amount', '0'),
                "phone": data.get('phone', '0900000000'),
                "admin_name": data.get('admin_name', 'Admin'),
                "message": f"✅ تم استلام دفعة بقيمة {data.get('amount', '0')} ل.س للحساب {data.get('username', '')}. شكراً لتعاملكم معنا (TopNet).",
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
                "amount_usd": data.get('amount_usd', '0'),
                "amount_syp": data.get('amount_syp', '0'),
                "connection": data.get('connection_type', ''),
                "dish_ip": data.get('dish_ip', ''),
                "status": data.get('status', 'Pending'),
                "message": msg,
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

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'token' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        auth_username = username
        auth_password = password
        
        if username == 'maram' and password == 'm@123':
            auth_username = 'Top'
            auth_password = 'omar@123'

        # 1. Check local database for users
        conn = get_db_connection()
        local_user = conn.execute('SELECT * FROM users WHERE username = ? AND password = ?', (username, password)).fetchone()
        conn.close()
        
        if local_user:
            # For local users, we use the background SAS account to get a valid token
            token = sasclient.login(username='Top', password='omar@123')
            if token:
                session['token'] = token
                session['username'] = username
                session['role'] = local_user['role']
                session['parent'] = dict(local_user).get('parent') if dict(local_user).get('parent') else None
                if local_user['role'] == 'maintenance':
                    return redirect(url_for('complaints'))
                return redirect(url_for('dashboard'))
            else:
                flash('🚨 SAS API Connection Error. Local login failed because SAS is unreachable.', 'error')
                return redirect(url_for('login'))

        # 2. If not in local db, check SAS API directly
        token = sasclient.login(username=auth_username, password=auth_password)
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
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO payments_v3 
                (username, fullname, profile_name, parent, amount, admin_name, phone) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (username, fullname, profile_name, parent, amount, admin_name, phone))
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
                'admin_name': admin_name
            }, webhook_type='payments', event_name='new')
            
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
    
    installations_income_usd = inst_revenue['total_usd']
    installations_income_syp = inst_revenue['total_syp']
    
    # Total income (Subscribers only SYP for now + Installations SYP/USD)
    income_total_syp = subscriber_income + installations_income_syp
    income_total_usd = installations_income_usd

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
            'net_syp': v['income_syp'] - v['expense'],
            'net_usd': v['income_usd']
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
                }, webhook_type='complaints', event_name='new')

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
            if current_complaint and current_complaint['status'] in ['Resolved', 'Closed'] and user_role == 'maintenance':
                flash('🚫 لا يمكن تعديل الشكوى بعد الإصلاح أو الإغلاق من قبل موظف الصيانة.', 'error')
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
                            }, webhook_type='complaints', event_name='assign')

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
                }, webhook_type='complaints', event_name='resolve')
            elif not assigned_changed:
                send_webhook_async({
                    'id': c_id,
                    'username': current_complaint['username'] if current_complaint else '',
                    'notes': notes,
                    'status': new_status
                }, webhook_type='complaints', event_name='update')
            
            flash('✅ تم تحديث الشكوى بنجاح.', 'success')

        elif action == 'assign_self':
            c_id = request.form.get('complaint_id')
            if user_role == 'maintenance':
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
                conn.execute('''
                    INSERT INTO installations (fullname, phone1, phone2, area, address_details, notes, registered_by, parent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (fullname, phone1, phone2, area, address, notes, user_name, user_parent))
                conn.commit()
                
                # Send Webhook Notification for Installation Request
                send_webhook_async({
                    'id': conn.execute('SELECT last_insert_rowid()').fetchone()[0],
                    'type': 'installation_new',
                    'fullname': fullname,
                    'phone': phone1,
                    'area': area,
                    'address': address,
                    'notes': notes
                }, webhook_type='installations')

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
                        'fullname': inst['fullname'],
                        'phone': inst['phone1'],
                        'area': inst['area'],
                        'address': inst['address_details'],
                        'notes': inst['notes'],
                        'employee_name': emp['username'],
                        'employee_phone': emp['phone'] or emp['maintenance_id'] or '',
                        'status': 'Assigned'
                    }, webhook_type='installations', event_name='assign')
                    
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
                            'fullname': inst['fullname'],
                            'phone': inst['phone1'],
                            'area': inst['area'],
                            'address': inst['address_details'],
                            'notes': inst['notes'],
                            'employee_name': emp['username'] if emp else '',
                            'employee_phone': emp['phone'] or emp['maintenance_id'] or '' if emp else '',
                            'status': inst['status']
                        }, webhook_type='installations', event_name='update')
                        
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
            inst = conn.execute('SELECT fullname, phone1, assigned_to FROM installations WHERE id = ?', (inst_id,)).fetchone()
            if inst and (user_role in ['admin', 'manager'] or inst['assigned_to'] == user_name):
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
                send_webhook_async({
                    'id': inst_id,
                    'type': 'installation_completed',
                    'fullname': inst['fullname'],
                    'phone': inst['phone1'],
                    'area': inst['area'],
                    'address': inst['address_details'],
                    'employee_name': emp['username'] if emp else '',
                    'employee_phone': emp['phone'] or emp['maintenance_id'] or '' if emp else '',
                    'status': 'Completed',
                    'amount_usd': payment_amount_usd,
                    'amount_syp': payment_amount_syp,
                    'notes': payment_notes,
                    'connection_type': connection_type,
                    'dish_ip': dish_ip
                }, webhook_type='installations', event_name='complete')

                flash('✅ تم انهاء التركيب وتسجيل الدفعة.', 'success')
            else:
                flash('🚫 غير مصرح.', 'error')

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
    conn.close()
    return render_template('installations.html', installations=all_installations, maintenance_users=maintenance_users, stats=stats)

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



@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
