import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import smtplib
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

from db import init_db, db, q, now_iso, log_audit, get_conn
from ingest import scan_and_import_air
from air_parser import parse_air_text
from documents import build_pdf, branding_from_row

ROOT = os.path.dirname(__file__)
STATIC_DIR = os.path.join(ROOT, 'static')
WORKSPACE_ROOT = os.path.abspath(os.path.join(ROOT, '..'))
SESSIONS = {}
SESS_LOCK = threading.Lock()


def json_bytes(obj, status=200):
    b = json.dumps(obj, default=str).encode('utf-8')
    return b, status, 'application/json; charset=utf-8'



def role_group(role):
    r = (role or '').strip().lower()
    if r in ('owner', 'admin'):
        return 'airline_admin'
    if r == 'accountant':
        return 'airline_accountant'
    if r in ('reservations', 'reservation', 'agent'):
        return 'airline_reservations'
    if r in ('operations', 'operation', 'checkin', 'station'):
        return 'airline_operations'
    if r == 'agency_admin':
        return 'agency_admin'
    if r in ('agency', 'agency_agent'):
        return 'agency_agent'
    return 'viewer'


ROLE_API = {
    'airline_admin': None,
    'airline_accountant': ('/api/dashboard', '/api/analytics', '/api/finance/', '/api/invoices',
                           '/api/credit-notes', '/api/payments', '/api/bills', '/api/expenses',
                           '/api/setup/chart-of-accounts', '/api/documents', '/api/shares',
                           '/api/tasks', '/api/notifications', '/api/session', '/api/logout'),
    'airline_reservations': ('/api/dashboard', '/api/analytics', '/api/global-search', '/api/orders',
                             '/api/customers', '/api/travelers', '/api/pss/', '/api/documents',
                             '/api/shares', '/api/tasks', '/api/notifications', '/api/session', '/api/logout'),
    'airline_operations': ('/api/dashboard', '/api/analytics', '/api/global-search', '/api/pss/',
                           '/api/orders', '/api/travelers', '/api/tasks', '/api/notifications',
                           '/api/documents', '/api/session', '/api/logout'),
    'agency_admin': ('/api/session', '/api/logout', '/api/dashboard', '/api/pss/partner-workspace',
                     '/api/pss/partner-clients', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                     '/api/pss/partner-users', '/api/pss/bookings', '/api/pss/flights',
                     '/api/customers', '/api/estimates', '/api/invoices', '/api/payments',
                     '/api/finance/summary', '/api/credit-notes', '/api/tasks', '/api/notifications',
                     '/api/documents', '/api/shares', '/api/public/share/', '/api/pss/airport-info'),
    'agency_agent': ('/api/session', '/api/logout', '/api/dashboard', '/api/pss/partner-workspace',
                     '/api/pss/partner-clients', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                     '/api/pss/bookings', '/api/pss/flights',
                     '/api/customers', '/api/estimates', '/api/invoices', '/api/payments',
                     '/api/finance/summary', '/api/credit-notes', '/api/tasks', '/api/notifications',
                     '/api/documents', '/api/shares', '/api/public/share/', '/api/pss/airport-info'),
    'viewer': ('/api/session', '/api/logout', '/api/dashboard', '/api/analytics', '/api/global-search',
               '/api/notifications'),
}


def read_json(handler):
    try:
        ln = int(handler.headers.get('Content-Length') or 0)
    except Exception:
        ln = 0
    if ln > MAX_BODY:
        raise PayloadTooLarge()
    raw = handler.rfile.read(ln) if ln else b''
    if not raw:
        return {}
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return {}


def make_token():
    return base64.urlsafe_b64encode(os.urandom(24)).decode('ascii').rstrip('=')


class PayloadTooLarge(Exception):
    pass


SESSION_TTL = 7 * 86400
SESSION_IDLE = 25 * 60
_SECURE_COOKIES_ENV = os.environ.get('SECURE_COOKIES')
PBKDF2_ITERATIONS = 210_000
MAX_BODY = int(os.environ.get('MAX_BODY_MB', '10')) * 1024 * 1024


def _use_secure_cookie(handler):
    if _SECURE_COOKIES_ENV is not None:
        return _SECURE_COOKIES_ENV not in ('0', 'false', 'no')
    # Default: auto-detect - avoid Secure flag on plain HTTP loopback so local login works
    try:
        host = (handler.headers.get('Host') or '').split(':')[0].lower()
    except Exception:
        host = ''
    if host in ('localhost', '127.0.0.1', '::1', '[::1]'):
        return False
    xfproto = (handler.headers.get('X-Forwarded-Proto') or '').lower()
    if xfproto == 'https':
        return True
    return True


_LOGIN_FAILS = {}
_LOGIN_FAIL_LOCK = threading.Lock()


def _hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', (password or '').encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return f"v2${salt.hex()}${dk.hex()}"


def _verify_password(stored, password):
    """Returns (ok, upgraded_hash)."""
    if not isinstance(stored, str) or not stored:
        return (False, None)
    if stored.startswith('v2$'):
        try:
            _, salt_hex, dk_hex = stored.split('$')
            salt = bytes.fromhex(salt_hex)
        except Exception:
            return (False, None)
        dk = hashlib.pbkdf2_hmac('sha256', (password or '').encode('utf-8'), salt, PBKDF2_ITERATIONS)
        if hmac.compare_digest(dk.hex(), dk_hex):
            return (True, None)
        return (False, None)
    legacy = hashlib.sha256((password or '').encode('utf-8')).hexdigest()
    if stored == password or stored == legacy:
        return (True, _hash_password(password))
    return (False, None)


def _password_policy_error(pw):
    if not pw or len(pw) < 8:
        return 'New password must be at least 8 characters long'
    if not any(c.isupper() for c in pw):
        return 'New password must contain an uppercase letter'
    if not any(c.islower() for c in pw):
        return 'New password must contain a lowercase letter'
    if not any(c.isdigit() for c in pw):
        return 'New password must contain a digit'
    return None


def _client_hash(handler):
    try:
        ip = handler.client_address[0] if handler.client_address else ''
    except Exception:
        ip = ''
    try:
        ua = (handler.headers.get('User-Agent') or '')[:120] if handler.headers else ''
    except Exception:
        ua = ''
    return hashlib.sha256(f"{ip}|{ua}".encode('utf-8')).hexdigest()


def _note_login_fail(key):
    with _LOGIN_FAIL_LOCK:
        lst = _LOGIN_FAILS.setdefault(key, [])
        lst.append(time.time())
        del lst[:-5]


def _login_locked_until(key):
    with _LOGIN_FAIL_LOCK:
        lst = _LOGIN_FAILS.get(key) or []
        recent = [t for t in lst if time.time() - t < 900]
        return recent[-1] + 900 if len(recent) >= 5 else None


def _note_login_success(key):
    with _LOGIN_FAIL_LOCK:
        _LOGIN_FAILS.pop(key, None)


def _session_expiry(days=90):
    from datetime import timedelta
    return (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat() + "Z"


def _invalidate_user_sessions(user_id):
    if not user_id:
        return
    with SESS_LOCK:
        for sid in [t for t, s in SESSIONS.items() if s.get('user_id') == user_id]:
            SESSIONS.pop(sid, None)


def get_session(handler):
    try:
        cookie = SimpleCookie(handler.headers.get('Cookie'))
    except Exception:
        return None
    if 'sid' not in cookie:
        return None
    sid = cookie['sid'].value
    now = time.time()
    with SESS_LOCK:
        sess = SESSIONS.get(sid)
        if not sess:
            return None
        if sess.get('expires_at') is not None and now >= float(sess.get('expires_at')):
            SESSIONS.pop(sid, None)
            return None
        if now - float(sess.get('last_activity') or 0) > SESSION_IDLE:
            SESSIONS.pop(sid, None)
            return None
        if sess.get('client_hash') and sess['client_hash'] != _client_hash(handler):
            SESSIONS.pop(sid, None)
            return None
        sess['last_activity'] = now
        return sess



def get_portal_session(handler):
    try:
        cookie = SimpleCookie(handler.headers.get('Cookie'))
    except Exception:
        return None
    if 'psid' not in cookie:
        return None
    token = cookie['psid'].value
    with db() as conn:
        row = q(conn, """SELECT s.token,s.customer_id,s.tenant_id,s.expires_at,s.client_hash,c.full_name,c.email,c.phone
                         FROM portal_sessions s JOIN portal_customers c ON c.id=s.customer_id
                         WHERE s.token=?""", (token,), one=True)
        if not row:
            return None
        if row.get('expires_at') and row['expires_at'] < now_iso():
            conn.execute("DELETE FROM portal_sessions WHERE token=?", (token,))
            return None
        if row.get('client_hash') and row['client_hash'] != _client_hash(handler):
            conn.execute("DELETE FROM portal_sessions WHERE token=?", (token,))
            return None
    return row


def _ensure_portal_session_cols():
    with db() as conn:
        cols = {r['name'] for r in q(conn, "PRAGMA table_info(portal_sessions)")}
        if 'client_hash' not in cols:
            conn.execute("ALTER TABLE portal_sessions ADD COLUMN client_hash TEXT")



def require_auth(handler):
    sess = get_session(handler)
    if not sess:
        return None
    return sess


def month_range():
    return now_iso()[:7]


def plan_for_tenant(conn, tenant_id):
    return q(conn, "SELECT p.* FROM plans p JOIN tenants t ON t.plan_id=p.id WHERE t.id=?", (tenant_id,), one=True)


def usage_counts(conn, tenant_id):
    m = month_range()
    orders_m = q(conn, "SELECT COUNT(*) c FROM orders WHERE tenant_id=? AND substr(created_at,1,7)=?", (tenant_id, m), one=True)['c']
    users = q(conn, "SELECT COUNT(*) c FROM users WHERE tenant_id=? AND status='active'", (tenant_id,), one=True)['c']
    branches = q(conn, "SELECT COUNT(*) c FROM branches WHERE tenant_id=?", (tenant_id,), one=True)['c']
    connectors = q(conn, "SELECT COUNT(*) c FROM connectors WHERE tenant_id=? AND status='active'", (tenant_id,), one=True)['c']
    return {'orders_this_month': orders_m, 'users': users, 'branches': branches, 'connectors': connectors}


def mask_secret(val):
    if not val:
        return ''
    s = str(val)
    if len(s) <= 8:
        return '*' * len(s)
    return s[:4] + '*' * (len(s) - 8) + s[-4:]


def get_billing_gateway(conn, tenant_id=None, provider='flutterwave'):
    row = q(conn, "SELECT * FROM billing_gateways WHERE provider=? AND (scope='platform' OR tenant_id=?) ORDER BY CASE WHEN tenant_id=? THEN 0 ELSE 1 END, is_active DESC, id DESC LIMIT 1", (provider, tenant_id, tenant_id), one=True)
    if row and row.get('config_json'):
        try:
            row['config'] = json.loads(row['config_json'])
        except Exception:
            row['config'] = {}
    elif row:
        row['config'] = {}
    return row


def flutterwave_request(method, path, secret_key, payload=None, timeout=30):
    url = 'https://api.flutterwave.com/v3' + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header('Authorization', f'Bearer {secret_key}')
    req.add_header('Content-Type', 'application/json')
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode('utf-8', errors='ignore')
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', errors='ignore')
            return e.code, (json.loads(body) if body else {'status': 'error', 'message': str(e)})
        except Exception:
            return e.code, {'status': 'error', 'message': str(e)}
    except Exception as e:
        return 0, {'status': 'error', 'message': str(e)}


def flutterwave_verify(secret_key, tx_ref=None, transaction_id=None):
    if transaction_id:
        return flutterwave_request('GET', f"/transactions/{transaction_id}/verify", secret_key)
    if tx_ref:
        qs = urllib.parse.urlencode({'tx_ref': tx_ref})
        return flutterwave_request('GET', f"/transactions/verify_by_reference?{qs}", secret_key)
    return 0, {'status': 'error', 'message': 'tx_ref or transaction_id required'}


def apply_subscription_payment_success(conn, tenant_id, invoice_id, payment_row, gateway_data, actor_user_id=None):
    inv = q(conn, "SELECT * FROM subscription_invoices WHERE id=? AND tenant_id=?", (invoice_id, tenant_id), one=True)
    if not inv:
        return False, 'Invoice not found'
    ts = now_iso()
    if inv.get('status') == 'paid':
        if payment_row and payment_row.get('id'):
            conn.execute("UPDATE subscription_payments SET status='successful', raw_json=?, verified_at=COALESCE(verified_at,?) WHERE id=?", (json.dumps(gateway_data or {}, default=str), ts, payment_row['id']))
        return True, 'Already paid'
    # mark payment
    if payment_row and payment_row.get('id'):
        txid = str((gateway_data or {}).get('id') or payment_row.get('transaction_id') or '')
        flw_ref = str((gateway_data or {}).get('flw_ref') or payment_row.get('flw_ref') or '')
        amount = float((gateway_data or {}).get('amount') or payment_row.get('amount') or inv.get('amount') or 0)
        currency = (gateway_data or {}).get('currency') or payment_row.get('currency') or inv.get('currency') or 'USD'
        method = (gateway_data or {}).get('payment_type') or payment_row.get('payment_method') or 'mobilemoney'
        cust = (gateway_data or {}).get('customer') or {}
        conn.execute("""UPDATE subscription_payments SET status='successful', transaction_id=?, flw_ref=?, amount=?, currency=?, payment_method=?, customer_phone=COALESCE(?,customer_phone), customer_email=COALESCE(?,customer_email), raw_json=?, verified_at=? WHERE id=?""",
                     (txid, flw_ref, amount, currency, method, cust.get('phone_number'), cust.get('email'), json.dumps(gateway_data or {}, default=str), ts, payment_row['id']))
    # mark invoice paid
    conn.execute("UPDATE subscription_invoices SET status='paid', paid_at=?, gateway_ref=?, meta_json=? WHERE id=?",
                 (ts, str((gateway_data or {}).get('flw_ref') or (gateway_data or {}).get('id') or ''), json.dumps({**(json.loads(inv.get('meta_json') or '{}') if isinstance(inv.get('meta_json'), str) else (inv.get('meta_json') or {})), 'gateway_verify': gateway_data or {}}, default=str), invoice_id))
    # activate/update subscription
    sub = None
    if inv.get('subscription_id'):
        sub = q(conn, "SELECT * FROM subscriptions WHERE id=? AND tenant_id=?", (inv['subscription_id'], tenant_id), one=True)
    if not sub:
        sub = q(conn, "SELECT * FROM subscriptions WHERE tenant_id=? ORDER BY id DESC LIMIT 1", (tenant_id,), one=True)
    start = ts[:10]
    cycle = inv.get('billing_cycle') or 'monthly'
    days = 365 if cycle == 'yearly' else 30
    try:
        end = (datetime.utcnow() + timedelta(days=days)).date().isoformat()
    except Exception:
        end = None
    if sub:
        conn.execute("UPDATE subscriptions SET plan_id=?, status='active', start_date=?, end_date=?, billing_cycle=?, mrr=? WHERE id=?",
                     (inv.get('plan_id') or sub.get('plan_id'), start, end, cycle, inv.get('amount') or sub.get('mrr') or 0, sub['id']))
        sub_id = sub['id']
    else:
        cur = conn.execute("INSERT INTO subscriptions(tenant_id,plan_id,status,start_date,end_date,billing_cycle,mrr,created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (tenant_id, inv.get('plan_id'), 'active', start, end, cycle, inv.get('amount') or 0, ts))
        sub_id = cur.lastrowid
    if inv.get('plan_id'):
        conn.execute("UPDATE tenants SET plan_id=?, status='active' WHERE id=?", (inv.get('plan_id'), tenant_id))
    log_audit(conn, tenant_id, actor_user_id, 'subscription_payment', 'subscription_invoice', invoice_id, {'status': 'paid', 'gateway': inv.get('gateway'), 'tx_ref': inv.get('tx_ref')})
    return True, 'Payment verified and subscription activated'


class Handler(BaseHTTPRequestHandler):
    server_version = "TravelERP"

    def send_response(self, code, message=None):
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header('Date', self.date_time_string())

    def _send(self, b, status=200, ctype='application/json; charset=utf-8', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
                         "base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _csrf_ok(self, path, qs):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie') or '')
        except Exception:
            return False
        csrf_cookie = cookie['csrftoken'].value if 'csrftoken' in cookie else ''
        supplied = (self.headers.get('X-CSRF-Token') or '').strip()
        if not supplied:
            supplied = (qs.get('csrf') or [''])[0].strip()
        if not csrf_cookie or not supplied:
            return False
        return hmac.compare_digest(csrf_cookie, supplied)

    def _json(self, obj, status=200, headers=None):
        b, st, ct = json_bytes(obj, status)
        self._send(b, st, ct, headers=headers)

    def _serve_static(self, path):
        if path == '/':
            path = '/index.html'
        static_root = os.path.normpath(STATIC_DIR) + os.sep
        fs = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip('/')))
        if not fs.startswith(static_root) or not os.path.isfile(fs):
            self._json({'error': 'Not found'}, 404)
            return
        ctype = mimetypes.guess_type(fs)[0] or 'application/octet-stream'
        with open(fs, 'rb') as f:
            b = f.read()
        headers = None
        if ctype.startswith('text/html'):
            headers = {'Set-Cookie': f'csrftoken={make_token()}; Path=/; SameSite=Lax'}
        self._send(b, 200, ctype, headers=headers)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path.startswith('/api/'):
            return self.handle_api('GET', path, qs)
        if path.startswith('/share/'):
            return self._serve_static('/share.html')
        if path == '/portal' or path.startswith('/portal/'):
            return self._serve_static('/portal.html')
        if path.startswith('/agency/'):
            return self._serve_static('/agency.html')
        return self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith('/api/'):
            return self.handle_api('POST', path, parse_qs(parsed.query))
        self._json({'error': 'Not found'}, 404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith('/api/'):
            return self.handle_api('PUT', path, parse_qs(parsed.query))
        self._json({'error': 'Not found'}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith('/api/'):
            return self.handle_api('DELETE', path, parse_qs(parsed.query))
        self._json({'error': 'Not found'}, 404)

    def log_message(self, fmt, *args):
        try:
            return super().log_message(fmt, *args)
        except Exception:
            pass

    def handle_api(self, method, path, qs):
        try:
            if method in ('POST', 'PUT', 'DELETE', 'PATCH') and path != '/api/billing/flutterwave/webhook':
                if not self._csrf_ok(path, qs):
                    return self._json({'error': 'CSRF token missing or invalid'}, 403)
            if path == '/api/health':
                return self._json({'ok': True, 'time': now_iso()})
            if path == '/api/portal/disruptions' and method == 'GET':
                return self.api_portal_disruptions(qs)
            if path == '/api/portal/register' and method == 'POST':
                return self.api_portal_register()
            if path == '/api/portal/login' and method == 'POST':
                return self.api_portal_login()
            if path == '/api/portal/logout' and method == 'POST':
                return self.api_portal_logout()
            if path == '/api/portal/me' and method == 'GET':
                return self.api_portal_me()
            if path == '/api/portal/documents' and method == 'GET':
                return self.api_portal_documents_get(qs)
            if path == '/api/portal/documents' and method == 'POST':
                return self.api_portal_documents_post()
            if path == '/api/pss/payments/flutterwave/init' and method == 'POST':
                return self.api_pss_flw_init()
            if path == '/api/pss/payments/flutterwave/verify' and method == 'GET':
                return self.api_pss_flw_verify(qs)
            if path == '/api/portal/tenants' and method == 'GET':
                return self.api_portal_tenants()
            if path == '/api/portal/flights' and method == 'GET':
                return self.api_portal_flights(qs)
            if path == '/api/portal/search' and method == 'GET':
                return self.api_portal_search(qs)
            if path == '/api/portal/charter' and method == 'POST':
                return self.api_portal_charter()
            if path == '/api/portal/book' and method == 'POST':
                return self.api_portal_book()
            if path == '/api/portal/pay' and method == 'POST':
                return self.api_portal_pay()
            if path == '/api/portal/booking' and method == 'GET':
                return self.api_portal_booking(qs)
            if path.startswith('/api/agency/offer/') and path.endswith('/bookings') and method == 'GET':
                return self.api_agency_offer_bookings(path.split('/')[4])
            if path.startswith('/api/agency/offer/') and path.endswith('/book') and method == 'POST':
                return self.api_agency_offer_book(path.split('/')[4])
            if path.startswith('/api/agency/offer/') and method == 'GET':
                return self.api_agency_offer(path.split('/')[4])
            if path.startswith('/api/public/share/') and method == 'GET':
                return self.api_public_share(path.rsplit('/', 1)[-1])
            if path.startswith('/api/documents/pdf/') and method == 'GET':
                parts = path.strip('/').split('/')
                if len(parts) >= 5:
                    return self.api_pdf_document(parts[3], parts[4], qs)
            if path == '/api/login' and method == 'POST':
                return self.api_login()
            if path == '/api/logout' and method == 'POST':
                return self.api_logout()
            if path == '/api/session' and method == 'GET':
                return self.api_session()
            if path == '/api/billing/flutterwave/webhook' and method == 'POST':
                return self.api_flutterwave_webhook()

            sess = require_auth(self)
            if path == '/api/auth/change-password' and method == 'POST':
                if not sess:
                    return self._json({'error': 'Unauthorized'}, 401)
                return self.api_auth_change_password(sess['tenant_id'], sess['user_id'], sess)
            if not sess:
                return self._json({'error': 'Unauthorized'}, 401)
            tenant_id = sess['tenant_id']
            user_id = sess['user_id']
            role = sess.get('role')
            group = role_group(role)
            allowed = ROLE_API.get(group)
            if allowed is not None:
                if not any(path.startswith(prefix) for prefix in allowed):
                    return self._json({'error': 'Access denied for your role'}, 403)
                if group == 'agency_agent' and method != 'GET' and not path.startswith((
                        '/api/login', '/api/logout', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                        '/api/pss/partner-clients/', '/api/customers', '/api/estimates', '/api/invoices',
                        '/api/payments', '/api/tasks', '/api/notifications', '/api/documents', '/api/shares')):
                    return self._json({'error': 'Access denied for your role'}, 403)
                if group == 'agency_admin' and method != 'GET' and not path.startswith((
                        '/api/login', '/api/logout', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                        '/api/pss/partner-clients/', '/api/pss/partner-users', '/api/customers', '/api/estimates',
                        '/api/invoices', '/api/payments', '/api/credit-notes', '/api/tasks', '/api/notifications',
                        '/api/documents', '/api/shares')):
                    return self._json({'error': 'Access denied for your role'}, 403)
                if group == 'viewer' and method != 'GET' and not path.startswith('/api/notifications/read'):
                    return self._json({'error': 'View-only access'}, 403)

            if path == '/api/dashboard' and method == 'GET':
                return self.api_dashboard(tenant_id)
            if path == '/api/analytics' and method == 'GET':
                return self.api_analytics(tenant_id)
            if path == '/api/pss/ancillary-sales' and method == 'GET':
                return self.api_pss_ancillary_sales(tenant_id)
            if path.startswith('/api/pss/bookings/') and path.endswith('/status') and method == 'POST':
                return self.api_pss_ticket_status(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/bookings/') and path.endswith('/ancillary') and method == 'POST':
                return self.api_pss_sell_ancillary(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/bookings/') and path.endswith('/baggage') and method == 'POST':
                return self.api_pss_add_baggage(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/bookings/') and path.endswith('/ancillaries') and method == 'GET':
                return self.api_pss_booking_ancillaries(tenant_id, path.split('/')[4])
            if path == '/api/pss/airport-info' and method == 'GET':
                return self.api_pss_airport_info(tenant_id, qs)
            if path == '/api/pss/service-cases' and method == 'GET':
                return self.api_pss_service_cases(tenant_id, 'GET')
            if path == '/api/pss/service-cases' and method == 'POST':
                return self.api_pss_service_cases(tenant_id, 'POST', user_id=user_id)
            if path.startswith('/api/pss/service-cases/') and path.endswith('/resolve') and method == 'POST':
                return self.api_pss_case_resolve(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/service-cases/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_pss_service_cases(tenant_id, method, path.rsplit('/', 1)[-1], user_id=user_id)
            if path == '/api/pss/eticket-tracker' and method == 'GET':
                return self.api_pss_eticket_tracker(tenant_id)
            if path == '/api/pss/agencies' and method == 'GET':
                return self.api_pss_agencies(tenant_id, 'GET')
            if path == '/api/pss/agencies' and method == 'POST':
                return self.api_pss_agencies(tenant_id, 'POST')
            if path.startswith('/api/pss/agencies/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_pss_agencies(tenant_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/direct-etickets' and method == 'GET':
                return self.api_pss_directs(tenant_id, 'GET')
            if path == '/api/pss/direct-etickets' and method == 'POST':
                return self.api_pss_directs(tenant_id, 'POST', user_id=user_id)
            if path.startswith('/api/pss/direct-etickets/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_pss_directs(tenant_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/charters' and method == 'GET':
                return self.api_pss_charters(tenant_id, 'GET')
            if path == '/api/pss/charters' and method == 'POST':
                return self.api_pss_charters(tenant_id, 'POST', user_id=user_id)
            if path.startswith('/api/pss/charters/') and path.endswith('/quote') and method == 'POST':
                return self.api_pss_charter_quote(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/charters/') and path.endswith('/confirm') and method == 'POST':
                return self.api_pss_charter_confirm(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/charters/') and path.endswith('/complete') and method == 'POST':
                return self.api_pss_charter_status(tenant_id, user_id, path.split('/')[4], 'completed')
            if path.startswith('/api/pss/charters/') and path.endswith('/passenger') and method == 'POST':
                return self.api_pss_charter_passenger(tenant_id, user_id, path.split('/')[4])
            if path.startswith('/api/pss/charters/') and method == 'GET':
                return self.api_pss_charter_detail(tenant_id, path.rsplit('/', 1)[-1])
            if path.startswith('/api/pss/charters/') and method in ('PUT', 'DELETE'):
                return self.api_pss_charters(tenant_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/charter-flights' and method == 'GET':
                return self.api_pss_charter_flights(tenant_id)
            if path == '/api/pss/trace' and method == 'GET':
                return self.api_pss_trace(tenant_id, qs)
            if path.startswith('/api/pss/trace/') and method == 'GET':
                return self.api_pss_trace_detail(tenant_id, path.rsplit('/', 1)[-1])
            if path == '/api/pss/travel-documents' and method == 'GET':
                return self.api_pss_travel_documents(tenant_id, qs)
            if path == '/api/pss/partner-clients' and method == 'GET':
                return self.api_pss_partner_clients(tenant_id, sess)
            if path.startswith('/api/pss/partner-bookings/') and method == 'PUT':
                return self.api_pss_partner_amend(tenant_id, sess, path.split('/')[4])
            if path == '/api/pss/partner-payments/flutterwave' and method == 'POST':
                return self.api_pss_partner_flw(tenant_id, sess)
            if path == '/api/pss/partner-workspace' and method == 'GET':
                return self.api_pss_partner_workspace(tenant_id, sess)
            if path == '/api/pss/partner-users' and method == 'GET':
                return self.api_pss_partner_users(tenant_id, 'GET', sess=sess)
            if path == '/api/pss/partner-users' and method == 'POST':
                return self.api_pss_partner_users(tenant_id, 'POST', sess=sess)
            if path.startswith('/api/pss/partner-users/') and method in ('PUT', 'DELETE'):
                return self.api_pss_partner_users(tenant_id, method, path.rsplit('/', 1)[-1], sess)
            if path.startswith('/api/pss/bookings/') and path.endswith('/financials') and method in ('GET', 'POST'):
                return self.api_pss_financials(tenant_id, user_id, path.split('/')[4], method)
            if path == '/api/pss/document-audit' and method == 'GET':
                return self.api_pss_document_audit(tenant_id, qs)
            if path == '/api/pss/overview' and method == 'GET':
                return self.api_pss_overview(tenant_id)
            if path == '/api/pss/flights/import' and method == 'POST':
                return self.api_pss_flights_import(tenant_id, user_id)
            if path == '/api/pss/flights' and method == 'GET':
                return self.api_pss_flights(tenant_id, 'GET')
            if path == '/api/pss/flights' and method == 'POST':
                return self.api_pss_flights(tenant_id, 'POST')
            if path.startswith('/api/pss/flights/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_pss_flights(tenant_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/bookings' and method == 'GET':
                return self.api_pss_bookings(tenant_id, 'GET', sess=sess)
            if path == '/api/pss/bookings' and method == 'POST':
                return self.api_pss_bookings(tenant_id, 'POST', user_id=user_id, sess=sess)
            if path.startswith('/api/pss/bookings/') and path.endswith('/ticket') and method == 'POST':
                return self.api_pss_booking_ticket(tenant_id, user_id, path.split('/')[4], sess=sess)
            if path.startswith('/api/pss/bookings/') and path.endswith('/checkin') and method == 'POST':
                return self.api_pss_booking_checkin(tenant_id, user_id, path.split('/')[4], sess=sess)
            if path.startswith('/api/pss/bookings/') and path.endswith('/payment') and method == 'POST':
                return self.api_pss_booking_payment(tenant_id, user_id, path.split('/')[4], sess=sess)
            if path.startswith('/api/pss/bookings/') and method == 'GET':
                return self.api_pss_booking_detail(tenant_id, path.rsplit('/', 1)[-1], sess=sess)
            if path.startswith('/api/pss/bookings/') and method in ('PUT', 'DELETE'):
                return self.api_pss_bookings(tenant_id, method, path.rsplit('/', 1)[-1], user_id=user_id, sess=sess)
            if path == '/api/pss/checkins' and method == 'GET':
                return self.api_pss_checkins(tenant_id)
            if path == '/api/pss/payments' and method == 'GET':
                return self.api_pss_payments(tenant_id, sess=sess)
            if path == '/api/pss/stock' and method == 'GET':
                return self.api_pss_stock(tenant_id)
            if path == '/api/pss/stock' and method == 'POST':
                return self.api_pss_stock_create(tenant_id, user_id)
            if path == '/api/pss/allocations' and method == 'GET':
                return self.api_pss_allocations(tenant_id)
            if path == '/api/pss/stock/allocate' and method == 'POST':
                return self.api_pss_stock_allocate(tenant_id, user_id)
            if path == '/api/pss/stock/issue' and method == 'POST':
                return self.api_pss_stock_issue(tenant_id, user_id)
            if path.startswith('/api/pss/stock/') and method in ('PUT', 'DELETE'):
                tail = path.rsplit('/', 1)[-1]
                if tail not in ('allocate', 'issue'):
                    return self.api_pss_stock_mutate(tenant_id, user_id, tail, method)
            if path == '/api/orders' and method == 'GET':
                return self.api_orders_list(tenant_id, qs)
            if path.startswith('/api/orders/') and method == 'GET':
                oid = path.rsplit('/', 1)[-1]
                return self.api_order_detail(tenant_id, oid)
            if path == '/api/orders/create' and method == 'POST':
                return self.api_order_create(tenant_id, user_id)
            if path == '/api/global-search' and method == 'GET':
                return self.api_global_search(tenant_id, qs)
            if path == '/api/customers' and method == 'GET':
                return self.api_customers(tenant_id, sess=sess)
            if path == '/api/customers' and method == 'POST':
                return self.api_customer_create(tenant_id, user_id=user_id, sess=sess)
            if path == '/api/connectors' and method == 'GET':
                return self.api_connectors(tenant_id)
            if path == '/api/connectors' and method == 'POST':
                return self.api_connector_create(tenant_id, user_id)
            if path.startswith('/api/connectors/') and method in ('PUT', 'DELETE'):
                return self.api_connector_mutate(tenant_id, user_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/import/scan_air' and method == 'POST':
                return self.api_import_scan_air(tenant_id, user_id)
            if path == '/api/import/upload_air' and method == 'POST':
                return self.api_import_upload_air(tenant_id, user_id)
            if path == '/api/import/ingest_payload' and method == 'POST':
                return self.api_ingest_payload(tenant_id, user_id)
            if path == '/api/import/jobs' and method == 'GET':
                return self.api_import_jobs(tenant_id)
            if path == '/api/invoices' and method == 'GET':
                return self.api_invoices(tenant_id, sess=sess)
            if path == '/api/invoices' and method == 'POST':
                return self.api_invoice_create(tenant_id, user_id, sess=sess)
            if path == '/api/payments' and method == 'GET':
                return self.api_payments(tenant_id, sess=sess)
            if path == '/api/payments' and method == 'POST':
                return self.api_payment_create(tenant_id, user_id, sess=sess)
            if path == '/api/finance/summary' and method == 'GET':
                return self.api_finance_summary(tenant_id, sess=sess)
            if path == '/api/tasks' and method == 'GET':
                return self.api_tasks(tenant_id, sess=sess)
            if path == '/api/tasks' and method == 'POST':
                return self.api_task_create(tenant_id, user_id, sess=sess)
            if path == '/api/tasks/update' and method == 'POST':
                return self.api_task_update(tenant_id, user_id, sess=sess)
            if path == '/api/notifications' and method == 'GET':
                return self.api_notifications(tenant_id, sess=sess)
            if path == '/api/notifications' and method == 'POST':
                return self.api_notification_create(tenant_id, user_id, sess=sess)
            if path == '/api/notifications/read' and method == 'POST':
                return self.api_notification_read(tenant_id, sess=sess)
            if path == '/api/admin/overview' and method == 'GET':
                return self.api_admin_overview(tenant_id)
            if path == '/api/billing/overview' and method == 'GET':
                return self.api_billing_overview(tenant_id)
            if path == '/api/billing/gateway' and method == 'POST':
                return self.api_billing_gateway_save(tenant_id, user_id, role)
            if path == '/api/billing/subscription/invoice' and method == 'POST':
                return self.api_billing_subscription_invoice(tenant_id, user_id)
            if path == '/api/billing/subscription/pay/flutterwave' and method == 'POST':
                return self.api_billing_subscription_pay_flutterwave(tenant_id, user_id)
            if path == '/api/billing/subscription/verify' and method == 'POST':
                return self.api_billing_subscription_verify(tenant_id, user_id)
            if path == '/api/raw_documents' and method == 'GET':
                return self.api_raw_docs(tenant_id)

            if path.startswith('/api/orders/items/') and method in ('PUT', 'DELETE'):
                return self.api_order_item_mutate(tenant_id, path.rsplit('/', 1)[-1], method)
            if path.startswith('/api/orders/') and method in ('PUT', 'DELETE'):
                if path.startswith('/api/orders/items'):
                    return self._json({'error': 'Not found'}, 404)
                return self.api_order_mutate(tenant_id, user_id, path.rsplit('/', 1)[-1], method)
            if path.startswith('/api/orders/') and path.endswith('/items') and method == 'POST':
                return self.api_order_item_create(tenant_id, path.split('/')[3])
            if path.startswith('/api/customers/') and method in ('PUT', 'DELETE'):
                return self.api_customer_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path == '/api/travelers' and method == 'GET':
                return self.api_travelers_list(tenant_id)
            if path == '/api/travelers' and method == 'POST':
                return self.api_traveler_create(tenant_id)
            if path == '/api/travelers/export' and method == 'GET':
                return self.api_travelers_export(tenant_id)
            if path == '/api/travelers/import' and method == 'POST':
                return self.api_travelers_import(tenant_id)
            if path == '/api/travelers/reconcile-customers' and method == 'POST':
                return self.api_travelers_reconcile_customers(tenant_id)
            if path == '/api/travelers/bulk-status' and method == 'POST':
                return self.api_travelers_bulk_status(tenant_id)
            if path == '/api/travelers/bulk-delete' and method == 'POST':
                return self.api_travelers_bulk_delete(tenant_id)
            if path.startswith('/api/travelers/') and path.endswith('/duplicate') and method == 'POST':
                return self.api_traveler_duplicate(tenant_id, path.split('/')[3])
            if path.startswith('/api/travelers/') and path.endswith('/status') and method == 'POST':
                return self.api_traveler_status(tenant_id, path.split('/')[3])
            if path.startswith('/api/travelers/') and path.endswith('/statement/email') and method == 'POST':
                return self.api_traveler_statement_email(tenant_id, path.split('/')[3])
            if path.startswith('/api/travelers/') and method == 'GET':
                return self.api_traveler_detail(tenant_id, path.rsplit('/', 1)[-1])
            if path.startswith('/api/travelers/') and method in ('PUT', 'DELETE'):
                return self.api_traveler_mutate(tenant_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/estimates' and method == 'GET':
                return self.api_estimates_list(tenant_id, sess=sess)
            if path == '/api/estimates' and method == 'POST':
                return self.api_estimate_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/estimates/') and path.endswith('/invoice') and method == 'POST':
                return self.api_estimate_convert(tenant_id, user_id, path.split('/')[3], sess=sess)
            if path.startswith('/api/estimates/') and method in ('PUT', 'DELETE'):
                return self.api_estimate_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path == '/api/credit-notes' and method == 'GET':
                return self.api_credit_notes_list(tenant_id, sess=sess)
            if path == '/api/credit-notes' and method == 'POST':
                return self.api_credit_note_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/credit-notes/') and method in ('PUT', 'DELETE'):
                return self.api_credit_note_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)

            if path == '/api/setup/agency-profile' and method == 'GET':
                return self.api_agency_profile_get(tenant_id)
            if path == '/api/setup/agency-profile' and method == 'POST':
                return self.api_agency_profile_save(tenant_id)
            setup_map = {
                'users': ('users', 'users'),
                'chart-of-accounts': ('chart_of_accounts', 'accounts'),
                'products': ('products', 'products'),
                'currencies': ('currencies', 'currencies'),
                'contacts': ('contacts', 'contacts'),
                'business-rules': ('business_rules', 'rules'),
                'preferences': ('preferences', 'preferences'),
                'mail-templates': ('mail_templates', 'templates'),
                'integrations': ('integrations', 'integrations'),
                'partners': ('partners', 'partners'),
                'unused-tickets': ('unused_tickets', 'unused_tickets'),
                'disruptions': ('disruptions', 'disruptions'),
                'refunds': ('refunds', 'refunds'),
                'vouchers': ('vouchers', 'vouchers'),
                'coupons': ('coupons', 'coupons'),
                'bank-accounts': ('bank_accounts', 'bank_accounts'),
                'bank-transactions': ('bank_transactions', 'bank_transactions'),
                'reconciliations': ('reconciliations', 'reconciliations'),
                'pss-airports': ('pss_airports', 'airports'),
                'pss-channels': ('pss_channels', 'channels'),
                'pss-stock-pools': ('pss_stock_pools', 'pools'),
                'pss-ancillaries': ('pss_ancillaries', 'ancillaries'),
                'pss-baggage-rules': ('pss_baggage_rules', 'baggage_rules'),
                'pss-airport-docs': ('pss_airport_docs', 'airport_docs'),
                'pss-weather': ('pss_weather', 'weather'),
                'branches': ('branches', 'branches')
            }
            setup_match = None
            for slug, cfg in setup_map.items():
                if path == f'/api/setup/{slug}' and method in ('GET', 'POST'):
                    setup_match = (slug, cfg, None, method); break
                if path.startswith(f'/api/setup/{slug}/') and method in ('PUT', 'DELETE'):
                    setup_match = (slug, cfg, path.rsplit('/', 1)[-1], method); break
            if setup_match:
                slug, (table, key), rid, method = setup_match
                return self.api_setup_resource(tenant_id, slug, table, key, rid, method)
            if path.startswith('/api/setup/integrations/') and path.endswith('/test') and method == 'POST':
                return self.api_integration_test(tenant_id, path.split('/')[4])
            if path.startswith('/api/setup/mail-templates/') and path.endswith('/preview') and method == 'POST':
                return self.api_mail_template_preview(tenant_id, path.split('/')[4])

            if path == '/api/documents' and method == 'GET':
                return self.api_documents_list(tenant_id, qs)
            if path == '/api/documents' and method == 'POST':
                return self.api_document_create(tenant_id, user_id)
            if path.startswith('/api/documents/') and path.endswith('/download') and method == 'GET':
                return self.api_document_download(tenant_id, path.split('/')[3])
            if path.startswith('/api/documents/') and method in ('PUT', 'DELETE'):
                return self.api_document_mutate(tenant_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/shares' and method == 'GET':
                return self.api_shares_list(tenant_id)
            if path == '/api/shares' and method == 'POST':
                return self.api_share_create(tenant_id, user_id)
            if path.startswith('/api/shares/') and method == 'DELETE':
                return self.api_share_delete(tenant_id, path.rsplit('/', 1)[-1])
            if path == '/api/branding' and method == 'GET':
                return self.api_branding_get(tenant_id)
            if path == '/api/branding' and method == 'POST':
                return self.api_branding_save(tenant_id, user_id)

            if path == '/api/finance/chart-of-accounts' and method == 'GET':
                return self.api_chart_accounts_list(tenant_id, sess=sess)
            if path == '/api/finance/chart-of-accounts' and method == 'POST':
                return self.api_chart_account_create(tenant_id)
            if path.startswith('/api/finance/chart-of-accounts/') and method in ('PUT', 'DELETE'):
                return self.api_chart_account_mutate(tenant_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/finance/bills' and method == 'GET':
                return self.api_bills_list(tenant_id, sess=sess)
            if path == '/api/finance/bills' and method == 'POST':
                return self.api_bill_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/finance/bills/') and path.endswith('/pay') and method == 'POST':
                return self.api_bill_pay(tenant_id, user_id, path.split('/')[4], sess=sess)
            if path.startswith('/api/finance/bills/') and method in ('PUT', 'DELETE'):
                return self.api_bill_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path == '/api/finance/expenses' and method == 'GET':
                return self.api_expenses_list(tenant_id, sess=sess)
            if path == '/api/finance/expenses' and method == 'POST':
                return self.api_expense_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/finance/expenses/') and method in ('PUT', 'DELETE'):
                return self.api_expense_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path == '/api/finance/ledger' and method == 'GET':
                return self.api_ledger(tenant_id, qs, sess=sess)
            if path == '/api/finance/trial-balance' and method == 'GET':
                return self.api_trial_balance(tenant_id, sess=sess)
            if path == '/api/finance/journals' and method == 'GET':
                return self.api_ledger(tenant_id, qs, sess=sess)
            if path == '/api/finance/journals' and method == 'POST':
                return self.api_manual_journal(tenant_id, user_id, sess=sess)
            return self._json({'error': 'Not found'}, 404)
        except PayloadTooLarge:
            return self._json({'error': 'Request body too large'}, 413)
        except Exception as e:
            self._log_exception(e)
            return self._json({'error': 'Something went wrong'}, 500)

    def _log_exception(self, exc):
        print('API error:', file=sys.stderr)
        traceback.print_exc()

    def api_login(self):
        data = read_json(self)
        if not isinstance(data, dict):
            data = {}
        tenant_code = (data.get('tenant') or 'fayaka').strip()
        if tenant_code.lower() == 'fayaka':
            tenant_code = 'demo'
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        ip = self.client_address[0] if self.client_address else 'unknown'
        fail_key = 'airline:' + hashlib.sha256(str(ip).encode()).hexdigest()[:16]
        locked_until = _login_locked_until(fail_key)
        if locked_until and locked_until > time.time():
            return self._json({'error': 'Too many failed attempts. Try again later.'}, 401)
        with db() as conn:
            t = q(conn, "SELECT * FROM tenants WHERE code=? AND status='active'", (tenant_code,), one=True)
            if not t:
                _note_login_fail(fail_key)
                return self._json({'error': 'Invalid tenant'}, 401)
            u = q(conn, "SELECT * FROM users WHERE tenant_id=? AND username=? AND status='active'",
                  (t['id'], username), one=True)
            ok, upgraded = _verify_password((u or {}).get('password'), password)
            if upgraded:
                conn.execute("UPDATE users SET password=? WHERE id=?", (upgraded, u['id']))
            if not u or not ok:
                _note_login_fail(fail_key)
                try:
                    log_audit(conn, t['id'], None, 'login_failed', 'user', None, {'username': username, 'ip': ip})
                except Exception:
                    pass
                return self._json({'error': 'Invalid credentials'}, 401)
            log_audit(conn, t['id'], u['id'], 'login', 'user', u['id'], {'username': username})
        _note_login_success(fail_key)
        token = make_token()
        now = time.time()
        sess = {'tenant_id': t['id'], 'tenant_code': t['code'], 'tenant_name': t['name'], 'user_id': u['id'],
                'username': u['username'], 'full_name': u.get('full_name'), 'role': u.get('role'),
                'agency_id': u.get('agency_id'), 'station_code': u.get('station_code'), 'branch_id': u.get('branch_id'),
                'client_hash': _client_hash(self), 'issued_at': now, 'last_activity': now,
                'expires_at': now + SESSION_TTL}
        with SESS_LOCK:
            SESSIONS[token] = sess
        flags = '; Secure' if _use_secure_cookie(self) else ''
        headers = {'Set-Cookie': f'sid={token}; Path=/; HttpOnly; SameSite=Strict{flags}'}
        return self._json({'ok': True, 'session': sess}, 200, headers=headers)

    def api_auth_change_password(self, tenant_id, user_id, sess):
        data = read_json(self)
        if not isinstance(data, dict):
            data = {}
        new_pw = data.get('new_password') or ''
        if new_pw != (data.get('confirm_password') or ''):
            return self._json({'error': 'Passwords do not match'}, 400)
        err = _password_policy_error(new_pw)
        if err:
            return self._json({'error': err}, 400)
        with db() as conn:
            u = q(conn, "SELECT password FROM users WHERE id=? AND tenant_id=?", (user_id, tenant_id), one=True)
            ok, _ = _verify_password((u or {}).get('password'), data.get('old_password') or '')
            if not ok:
                return self._json({'error': 'Current password is incorrect'}, 401)
            conn.execute("UPDATE users SET password=? WHERE id=? AND tenant_id=?",
                         (_hash_password(new_pw), user_id, tenant_id))
            log_audit(conn, tenant_id, user_id, 'password_changed', 'user', user_id,
                      {'username': sess.get('username')})
        _invalidate_user_sessions(user_id)
        return self._json({'ok': True, 'message': 'Password changed'})

    def api_logout(self):
        cookie = SimpleCookie(self.headers.get('Cookie'))
        sid = cookie['sid'].value if 'sid' in cookie else None
        if sid:
            with SESS_LOCK:
                SESSIONS.pop(sid, None)
        flags = '; Secure' if _use_secure_cookie(self) else ''
        headers = {'Set-Cookie': f'sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{flags}'}
        return self._json({'ok': True}, 200, headers=headers)

    def api_session(self):
        sess = require_auth(self)
        if not sess:
            return self._json({'error': 'Unauthorized'}, 401)
        with db() as conn:
            plan = plan_for_tenant(conn, sess['tenant_id'])
            usage = usage_counts(conn, sess['tenant_id'])
            branding = self._branding(conn, sess['tenant_id'])
        if branding:
            branding['logo_data_set'] = bool(branding.get('logo_data'))
        return self._json({'session': sess, 'plan': plan, 'usage': usage, 'branding': branding})

    def api_dashboard(self, tenant_id):
        with db() as conn:
            orders = q(conn, "SELECT COUNT(*) c FROM orders WHERE tenant_id=?", (tenant_id,), one=True)['c']
            ticketed = q(conn, "SELECT COUNT(*) c FROM orders WHERE tenant_id=? AND ticket_status='ticketed'", (tenant_id,), one=True)['c']
            unticketed = q(conn, "SELECT COUNT(*) c FROM orders WHERE tenant_id=? AND ticket_status!='ticketed'", (tenant_id,), one=True)['c']
            customers = q(conn, "SELECT COUNT(*) c FROM customers WHERE tenant_id=?", (tenant_id,), one=True)['c']
            inv = q(conn, "SELECT COALESCE(SUM(total),0) total, COALESCE(SUM(balance),0) balance, COALESCE(SUM(amount_paid),0) paid FROM invoices WHERE tenant_id=? AND kind='invoice'", (tenant_id,), one=True)
            ap = q(conn, "SELECT COALESCE(SUM(balance),0) c FROM bills WHERE tenant_id=? AND status!='paid'", (tenant_id,), one=True)['c']
            expenses = q(conn, "SELECT COALESCE(SUM(amount),0) c FROM expenses WHERE tenant_id=?", (tenant_id,), one=True)['c']
            margin = q(conn, "SELECT COALESCE(SUM(margin),0) c FROM orders WHERE tenant_id=?", (tenant_id,), one=True)['c']
            open_tasks = q(conn, "SELECT COUNT(*) c FROM tasks WHERE tenant_id=? AND status!='closed'", (tenant_id,), one=True)['c']
            today = now_iso()[:10]
            departures_soon = q(conn, "SELECT o.id,o.pnr,o.origin,o.destination,o.travel_start,o.ticket_status,o.source_type FROM orders o WHERE tenant_id=? AND o.travel_start>=? ORDER BY o.travel_start LIMIT 8", (tenant_id, today))
            tktl_due = q(conn, "SELECT id,pnr,tktl,ticket_status,source_type FROM orders WHERE tenant_id=? AND ticket_status!='ticketed' AND tktl IS NOT NULL AND tktl!='' ORDER BY tktl LIMIT 8", (tenant_id,))
            unpaid = q(conn, "SELECT i.id,i.invoice_no,i.total,i.balance,i.due_date,c.name customer FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.tenant_id=? AND i.kind='invoice' AND i.balance>0 ORDER BY i.balance DESC LIMIT 8", (tenant_id,))
            recent_orders = q(conn, "SELECT id,pnr,order_number,source_type,status,ticket_status,origin,destination,travel_start,total_sell,created_at FROM orders WHERE tenant_id=? ORDER BY created_at DESC LIMIT 8", (tenant_id,))
            by_source = q(conn, "SELECT source_type, COUNT(*) c, COALESCE(SUM(total_sell),0) sell, COALESCE(SUM(margin),0) margin FROM orders WHERE tenant_id=? GROUP BY source_type", (tenant_id,))
            plan = plan_for_tenant(conn, tenant_id)
            usage = usage_counts(conn, tenant_id)
        return self._json({'kpis': {
            'orders': orders, 'ticketed': ticketed, 'unticketed': unticketed, 'customers': customers,
            'revenue': inv['total'], 'ar_balance': inv['balance'], 'cash_collected': inv['paid'],
            'ap_open': ap, 'expenses': expenses, 'margin': margin, 'open_tasks': open_tasks
        }, 'departures_soon': departures_soon, 'tktl_due': tktl_due, 'unpaid': unpaid, 'recent_orders': recent_orders, 'by_source': by_source, 'plan': plan, 'usage': usage})

    def api_analytics(self, tenant_id):
        with db() as conn:
            by_source = q(conn, "SELECT source_type as k, COUNT(*) orders, COALESCE(SUM(total_sell),0) sell, COALESCE(SUM(total_cost),0) cost, COALESCE(SUM(margin),0) margin FROM orders WHERE tenant_id=? GROUP BY source_type", (tenant_id,))
            by_airline = q(conn, "SELECT COALESCE(s.carrier,'UNK') k, COUNT(DISTINCT s.order_id) orders FROM segments s WHERE s.tenant_id=? AND s.is_void=0 GROUP BY s.carrier ORDER BY orders DESC LIMIT 10", (tenant_id,))
            by_status = q(conn, "SELECT ticket_status k, COUNT(*) c FROM orders WHERE tenant_id=? GROUP BY ticket_status", (tenant_id,))
            months = q(conn, "SELECT substr(created_at,1,7) k, COUNT(*) orders, COALESCE(SUM(total_sell),0) sell, COALESCE(SUM(margin),0) margin FROM orders WHERE tenant_id=? GROUP BY substr(created_at,1,7) ORDER BY k DESC LIMIT 12", (tenant_id,))
        return self._json({'by_source': by_source, 'by_airline': by_airline, 'by_status': by_status, 'months': list(reversed(months))})

    def api_orders_list(self, tenant_id, qs):
        src = (qs.get('source_type') or [''])[0]
        st = (qs.get('status') or [''])[0]
        term = (qs.get('q') or [''])[0].strip()
        sql = """SELECT o.id,o.pnr,o.order_number,o.source_type,o.source_id,o.status,o.ticket_status,o.tktl,o.travel_start,o.travel_end,
                        o.origin,o.destination,o.total_sell,o.margin,o.balance_due,o.created_at,c.name customer_name
                 FROM orders o LEFT JOIN customers c ON c.id=o.customer_id WHERE o.tenant_id=?"""
        args = [tenant_id]
        if src:
            sql += " AND o.source_type=?"; args.append(src)
        if st:
            sql += " AND o.ticket_status=?"; args.append(st)
        if term:
            like = f"%{term}%"
            sql += " AND (o.pnr LIKE ? OR o.order_number LIKE ? OR o.origin LIKE ? OR o.destination LIKE ? OR c.name LIKE ?)"
            args += [like, like, like, like, like]
        sql += " ORDER BY o.created_at DESC LIMIT 500"
        with db() as conn:
            rows = q(conn, sql, args)
        return self._json({'orders': rows})

    def api_order_detail(self, tenant_id, oid):
        try:
            oid = int(oid)
        except Exception:
            return self._json({'error': 'Invalid id'}, 400)
        with db() as conn:
            o = q(conn, "SELECT o.*, c.name customer_name, c.email customer_email, c.phone customer_phone, r.filename raw_filename, r.content_text raw_text FROM orders o LEFT JOIN customers c ON c.id=o.customer_id LEFT JOIN raw_documents r ON r.id=o.raw_document_id WHERE o.tenant_id=? AND o.id=?", (tenant_id, oid), one=True)
            if not o:
                return self._json({'error': 'Not found'}, 404)
            segs = q(conn, "SELECT * FROM segments WHERE order_id=? ORDER BY id", (oid,))
            pax = q(conn, "SELECT * FROM order_passengers WHERE order_id=? ORDER BY id", (oid,))
            svcs = q(conn, "SELECT * FROM order_services WHERE order_id=? ORDER BY id", (oid,))
            hist = q(conn, "SELECT * FROM order_status_history WHERE order_id=? ORDER BY id DESC", (oid,))
            invs = q(conn, "SELECT * FROM invoices WHERE order_id=? ORDER BY id DESC", (oid,))
            pays = q(conn, "SELECT * FROM payments WHERE order_id=? ORDER BY id DESC", (oid,))
            tasks = q(conn, "SELECT * FROM tasks WHERE order_id=? ORDER BY id DESC", (oid,))
        return self._json({'order': o, 'segments': segs, 'passengers': pax, 'services': svcs, 'history': hist, 'invoices': invs, 'payments': pays, 'tasks': tasks})

    def api_order_create(self, tenant_id, user_id):
        data = read_json(self)
        with db() as conn:
            ts = now_iso()
            src = data.get('source_type') or 'manual'
            pnr = data.get('pnr') or data.get('order_number') or f"MAN-{datetime.utcnow().strftime('%H%M%S')}"
            source_id = f"manual:{pnr}:{ts}"
            sell = float(data.get('total_sell') or 0); cost = float(data.get('total_cost') or 0)
            cur = conn.execute("""INSERT INTO orders(tenant_id,source_type,source_id,pnr,order_number,office_id,customer_id,product_type,status,ticket_status,tktl,booking_date,travel_start,travel_end,origin,destination,currency,total_sell,total_cost,margin,balance_due,notes,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, src, source_id, pnr, pnr, data.get('office_id'), data.get('customer_id'), data.get('product_type') or 'Flight', 'booked', data.get('ticket_status') or 'unticketed', data.get('tktl'), ts[:10], data.get('travel_start'), data.get('travel_end'), data.get('origin'), data.get('destination'), data.get('currency') or 'USD', sell, cost, sell-cost, sell, data.get('notes'), ts, ts))
            oid = cur.lastrowid
            traveler_id = self._oid(data.get('traveler_id'))
            if traveler_id:
                traveler = q(conn, "SELECT * FROM travelers WHERE id=? AND tenant_id=?", (traveler_id, tenant_id), one=True)
                if not traveler:
                    return self._json({'error': 'Traveler not found'}, 404)
                if str(traveler.get('status') or 'active').lower() == 'inactive':
                    return self._json({'error': 'Traveler is inactive and cannot receive new travel items'}, 400)
                conn.execute("""INSERT INTO order_passengers(tenant_id,order_id,traveler_id,pax_ref,full_name,traveler_type,phone,email,loyalty_id,ticket_numbers)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (tenant_id, oid, traveler_id, 'P1', traveler.get('full_name'), traveler.get('traveler_type'), traveler.get('phone') or traveler.get('mobile'), traveler.get('email'), None, None))
                if not data.get('customer_id') and traveler.get('customer_id'):
                    conn.execute("UPDATE orders SET customer_id=? WHERE id=?", (traveler['customer_id'], oid))
            conn.execute("INSERT INTO order_status_history(tenant_id,order_id,status,note,source,created_at) VALUES (?,?,?,?,?,?)", (tenant_id, oid, 'booked', 'Created manually', src, ts))
            auto_task = q(conn, "SELECT value,is_active FROM business_rules WHERE tenant_id=? AND rule_key='auto_ticket_task'", (tenant_id,), one=True)
            if auto_task and auto_task['is_active'] and str(auto_task['value']).lower() == 'true':
                conn.execute("INSERT INTO tasks(tenant_id,order_id,type,title,description,status,priority,due_at,sla_status,created_at) VALUES (?,?,?,?,?,'open','high',?,'ok',?)", (tenant_id, oid, 'ticketing', f"Ticket {pnr}", f"Ticketing task auto-created for {pnr}", data.get('tktl'), ts))
            log_audit(conn, tenant_id, user_id, 'create', 'order', oid, {})
        return self._json({'ok': True, 'order_id': oid})

    def api_customers(self, tenant_id, sess=None):
        with db() as conn:
            sql = "SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.customer_id=c.id) bookings, (SELECT COALESCE(SUM(total_sell),0) FROM orders o WHERE o.customer_id=c.id) spend FROM customers c WHERE c.tenant_id=?"
            args = [tenant_id]
            if (sess or {}).get('agency_id'):
                sql += " AND c.agency_id=?"; args.append(sess['agency_id'])
            sql += " ORDER BY c.created_at DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'customers': rows})

    def api_global_search(self, tenant_id, qs):
        term = (qs.get('q') or [''])[0].strip()
        if len(term) < 2:
            return self._json({'results': []})
        like = '%' + term + '%'
        results = []
        with db() as conn:
            for r in q(conn, """SELECT o.id,o.pnr,o.origin,o.destination,c.name customer_name FROM orders o LEFT JOIN customers c ON c.id=o.customer_id
                               WHERE o.tenant_id=? AND (o.pnr LIKE ? OR o.order_number LIKE ? OR o.origin LIKE ? OR o.destination LIKE ? OR c.name LIKE ?) LIMIT 10""", (tenant_id, like, like, like, like, like)):
                results.append({'type': 'Booking', 'label': r.get('pnr') or f"Order {r['id']}", 'detail': f"{r.get('origin') or '-'} → {r.get('destination') or '-'} · {r.get('customer_name') or '-'}", 'view': 'orders', 'action': 'view-booking', 'id': r['id']})
            for r in q(conn, "SELECT id,name,company,email FROM customers WHERE tenant_id=? AND (name LIKE ? OR company LIKE ? OR email LIKE ?) LIMIT 10", (tenant_id, like, like, like)):
                results.append({'type': 'Customer', 'label': r.get('name'), 'detail': f"{r.get('company') or '-'} · {r.get('email') or '-'}", 'view': 'crm', 'action': 'view-customer', 'id': r['id']})
            for r in q(conn, "SELECT id,invoice_no,total,status FROM invoices WHERE tenant_id=? AND invoice_no LIKE ? LIMIT 10", (tenant_id, like)):
                results.append({'type': 'Invoice', 'label': r.get('invoice_no'), 'detail': f"USD {float(r.get('total') or 0):,.2f} · {r.get('status') or '-'}", 'view': 'invoicing', 'action': 'invoice-pdf', 'id': r['id']})
            for r in q(conn, "SELECT id,full_name,display_name,doc_no,email FROM travelers WHERE tenant_id=? AND (full_name LIKE ? OR display_name LIKE ? OR doc_no LIKE ? OR email LIKE ?) LIMIT 10", (tenant_id, like, like, like, like)):
                results.append({'type': 'Traveler', 'label': r.get('display_name') or r.get('full_name'), 'detail': f"Passport {r.get('doc_no') or '-'} · {r.get('email') or '-'}", 'view': 'travelers', 'action': 'view-traveler', 'id': r['id']})
        return self._json({'results': results[:30]})

    def api_customer_create(self, tenant_id, user_id=None, sess=None):
        data = read_json(self)
        name = (data.get('name') or '').strip()
        if not name:
            return self._json({'error': 'name required'}, 400)
        with db() as conn:
            agency_id = (sess or {}).get('agency_id')
            cur = conn.execute("INSERT INTO customers(tenant_id,type,name,company,email,phone,loyalty_id,notes,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (tenant_id, data.get('type') or 'B2C', name, data.get('company'), data.get('email'), data.get('phone'), data.get('loyalty_id'), data.get('notes'), now_iso(), agency_id, 'agency' if agency_id else 'backoffice'))
            cid = cur.lastrowid
        return self._json({'ok': True, 'customer_id': cid})

    def api_connectors(self, tenant_id):
        with db() as conn:
            rows = q(conn, "SELECT c.*, (SELECT COUNT(*) FROM raw_documents r WHERE r.connector_id=c.id) docs, (SELECT COUNT(*) FROM orders o WHERE o.connector_id=c.id) orders_count FROM connectors c WHERE c.tenant_id=? ORDER BY c.id", (tenant_id,))
        return self._json({'connectors': rows})

    def api_connector_create(self, tenant_id, user_id):
        data = read_json(self)
        name = (data.get('name') or '').strip()
        ctype = (data.get('type') or 'manual').strip()
        if not name:
            return self._json({'error': 'name required'}, 400)
        with db() as conn:
            plan = plan_for_tenant(conn, tenant_id)
            usage = usage_counts(conn, tenant_id)
            if plan and usage['connectors'] >= (plan.get('max_connectors') or 0):
                return self._json({'error': 'Connector limit reached for plan'}, 403)
            cur = conn.execute("INSERT INTO connectors(tenant_id,name,type,config_json,status,created_at) VALUES (?,?,?,?,?,?)",
                               (tenant_id, name, ctype, json.dumps(data.get('config') or {}), 'active', now_iso()))
            cid = cur.lastrowid
            log_audit(conn, tenant_id, user_id, 'create', 'connector', cid, {'type': ctype})
        return self._json({'ok': True, 'connector_id': cid})

    def api_connector_mutate(self, tenant_id, user_id, cid, method):
        cid = self._oid(cid)
        with db() as conn:
            row = q(conn, "SELECT * FROM connectors WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Connector not found'}, 404)
            if method == 'DELETE':
                docs = q(conn, "SELECT COUNT(*) c FROM raw_documents WHERE connector_id=? AND tenant_id=?", (cid, tenant_id), one=True)['c']
                orders = q(conn, "SELECT COUNT(*) c FROM orders WHERE connector_id=? AND tenant_id=?", (cid, tenant_id), one=True)['c']
                if docs or orders:
                    return self._json({'error': 'Connector has imported documents or bookings and cannot be deleted'}, 400)
                conn.execute("DELETE FROM connectors WHERE id=? AND tenant_id=?", (cid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            status = data.get('status')
            if status not in ('active', 'inactive', 'disabled'):
                status = 'inactive' if str(row.get('status')).lower() == 'active' else 'active'
            conn.execute("UPDATE connectors SET name=?,type=?,status=?,config_json=? WHERE id=? AND tenant_id=?",
                         (data.get('name') or row.get('name'), data.get('type') or row.get('type'), status, json.dumps(data.get('config') or json.loads(row.get('config_json') or '{}')), cid, tenant_id))
            log_audit(conn, tenant_id, user_id, 'update', 'connector', cid, {'status': status})
        return self._json({'ok': True, 'status': status})

    def api_import_scan_air(self, tenant_id, user_id):
        from ingest import EXTERNAL_IMPORT_MSG
        return self._json({'error': EXTERNAL_IMPORT_MSG}, 400)
        data = read_json(self)
        root = os.path.realpath(WORKSPACE_ROOT)
        base = (data.get('path') or 'workspace').strip() or 'workspace'
        real = os.path.realpath(os.path.join(root, base if not os.path.isabs(base) else base))
        if real != root and not real.startswith(root + os.sep):
            return self._json({'error': 'Path must be inside the workspace root'}, 400)
        folder = real
        with db() as conn:
            connector_id = data.get('connector_id')
            if not connector_id:
                c = q(conn, "SELECT id FROM connectors WHERE tenant_id=? AND type='amadeus_air' LIMIT 1", (tenant_id,), one=True)
                connector_id = c['id'] if c else None
            stats = scan_and_import_air(conn, tenant_id, folder, connector_id=connector_id, user_id=user_id, limit=data.get('limit'))
        return self._json({'ok': True, 'folder': folder, 'stats': {k: v for k, v in stats.items() if k != 'logs'}, 'sample_logs': stats.get('logs', [])[:20]})

    def api_import_upload_air(self, tenant_id, user_id):
        from ingest import EXTERNAL_IMPORT_MSG
        return self._json({'error': EXTERNAL_IMPORT_MSG}, 400)
        import hashlib
        from ingest import import_air_parsed
        data = read_json(self)
        filename = data.get('filename') or 'upload.AIR'
        content_b64 = data.get('content_base64')
        text = data.get('text')
        if content_b64:
            try:
                raw = base64.b64decode(content_b64)
                text = raw.decode('utf-8', errors='ignore')
            except Exception:
                return self._json({'error': 'Invalid base64'}, 400)
        if text is None:
            return self._json({'error': 'No content'}, 400)
        with db() as conn:
            connector_id = data.get('connector_id')
            h = hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()
            existing_raw = q(conn, "SELECT id FROM raw_documents WHERE tenant_id=? AND content_hash=? LIMIT 1", (tenant_id, h), one=True)
            if existing_raw:
                raw_id = existing_raw['id']
            else:
                cur = conn.execute("INSERT INTO raw_documents(tenant_id,connector_id,source_type,source_ref,filename,content_text,content_hash,received_at,meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
                                   (tenant_id, connector_id, 'amadeus_air', filename, filename, text, h, now_iso(), json.dumps({'upload': True})))
                raw_id = cur.lastrowid
            parsed = parse_air_text(text, filename=filename)
            parsed['content_hash'] = h
            action, order_id = import_air_parsed(conn, tenant_id, parsed, source_type='amadeus_air', connector_id=connector_id, user_id=user_id, raw_document_id=raw_id)
        return self._json({'ok': True, 'action': action, 'order_id': order_id})

    def api_ingest_payload(self, tenant_id, user_id):
        import hashlib
        from ingest import EXTERNAL_IMPORT_MSG
        data = read_json(self)
        source_type = data.get('source_type') or 'manual'
        if source_type in ('amadeus_air', 'gds_amadeus', 'gds_sabre', 'gds_travelport', 'ndc'):
            return self._json({'error': EXTERNAL_IMPORT_MSG}, 400)
        connector_id = data.get('connector_id')
        payload = data.get('payload') or data
        raw_text = json.dumps(payload, default=str)
        h = hashlib.sha256(raw_text.encode('utf-8')).hexdigest()
        with db() as conn:
            cur = conn.execute("INSERT INTO raw_documents(tenant_id,connector_id,source_type,source_ref,filename,content_text,content_hash,received_at,meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
                               (tenant_id, connector_id, source_type, payload.get('order_id') or payload.get('pnr'), f"payload-{h[:10]}.json", raw_text, h, now_iso(), json.dumps({'mode':'api'})))
            raw_id = cur.lastrowid
            ts = now_iso()
            source_id = payload.get('source_id') or payload.get('order_id') or payload.get('pnr') or h
            existing = q(conn, "SELECT id FROM orders WHERE tenant_id=? AND source_type=? AND source_id=?", (tenant_id, source_type, str(source_id)), one=True)
            pnr = payload.get('pnr') or payload.get('order_number')
            total_sell = float(payload.get('total_sell') or 0)
            total_cost = float(payload.get('total_cost') or 0)
            if existing:
                oid = existing['id']
                conn.execute("UPDATE orders SET connector_id=?, raw_document_id=?, pnr=?, status=?, ticket_status=?, total_sell=?, total_cost=?, margin=?, balance_due=?, origin=?, destination=?, travel_start=?, travel_end=?, updated_at=? WHERE id=?",
                             (connector_id, raw_id, pnr, payload.get('status') or 'booked', payload.get('ticket_status') or 'unticketed', total_sell, total_cost, total_sell-total_cost, total_sell, payload.get('origin'), payload.get('destination'), payload.get('travel_start'), payload.get('travel_end'), ts, oid))
                action = 'updated'
            else:
                cur2 = conn.execute("""INSERT INTO orders(tenant_id,source_type,source_id,connector_id,raw_document_id,pnr,order_number,status,ticket_status,origin,destination,travel_start,travel_end,currency,total_sell,total_cost,margin,balance_due,notes,created_at,updated_at)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (tenant_id, source_type, str(source_id), connector_id, raw_id, pnr, pnr, payload.get('status') or 'booked', payload.get('ticket_status') or 'unticketed', payload.get('origin'), payload.get('destination'), payload.get('travel_start'), payload.get('travel_end'), payload.get('currency') or 'USD', total_sell, total_cost, total_sell-total_cost, total_sell, payload.get('notes'), ts, ts))
                oid = cur2.lastrowid
                conn.execute("INSERT INTO order_status_history(tenant_id,order_id,status,note,source,created_at) VALUES (?,?,?,?,?,?)", (tenant_id, oid, payload.get('status') or 'booked', 'Ingested payload', source_type, ts))
                action = 'imported'
            if payload.get('passengers'):
                conn.execute("DELETE FROM order_passengers WHERE order_id=?", (oid,))
                for p in payload['passengers']:
                    conn.execute("INSERT INTO order_passengers(tenant_id,order_id,full_name,traveler_type,email,phone,raw_json) VALUES (?,?,?,?,?,?,?)",
                                 (tenant_id, oid, p.get('name') or p.get('full_name'), p.get('type') or 'ADT', p.get('email'), p.get('phone'), json.dumps(p, default=str)))
            if payload.get('segments'):
                conn.execute("DELETE FROM segments WHERE order_id=?", (oid,))
                for s in payload['segments']:
                    conn.execute("INSERT INTO segments(tenant_id,order_id,carrier,flight_no,origin,destination,depart_at,arrive_at,status_code,is_void,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (tenant_id, oid, s.get('carrier'), s.get('flight_no'), s.get('origin'), s.get('destination'), s.get('depart_at'), s.get('arrive_at'), s.get('status'), 0, json.dumps(s, default=str)))
            log_audit(conn, tenant_id, user_id, 'ingest', 'order', oid, {'source_type': source_type})
        return self._json({'ok': True, 'action': action, 'order_id': oid})

    def api_import_jobs(self, tenant_id):
        with db() as conn:
            rows = q(conn, "SELECT j.*, c.name connector_name FROM import_jobs j LEFT JOIN connectors c ON c.id=j.connector_id WHERE j.tenant_id=? ORDER BY j.id DESC LIMIT 50", (tenant_id,))
        return self._json({'jobs': rows})

    def api_invoices(self, tenant_id, sess=None):
        with db() as conn:
            sql = "SELECT i.*, c.name customer_name, o.pnr FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id LEFT JOIN orders o ON o.id=i.order_id WHERE i.tenant_id=?"
            args = [tenant_id]
            if (sess or {}).get('agency_id'):
                sql += " AND i.agency_id=?"; args.append(sess['agency_id'])
            sql += " ORDER BY i.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'invoices': rows})

    def api_invoice_create(self, tenant_id, user_id, sess=None):
        from ingest import _recompute_order
        data = read_json(self)
        with db() as conn:
            ts = now_iso()
            agency_id = (sess or {}).get('agency_id')
            customer_id = self._oid(data.get('customer_id'))
            order_id = self._oid(data.get('order_id'))
            if agency_id and customer_id:
                c = q(conn, "SELECT 1 FROM customers WHERE id=? AND tenant_id=? AND agency_id=?", (customer_id, tenant_id, agency_id), one=True)
                if not c: return self._json({'error': 'Customer not found'}, 404)
            if agency_id and order_id:
                o = q(conn, "SELECT agency_id FROM orders WHERE id=? AND tenant_id=?", (order_id, tenant_id), one=True)
                if o and o.get('agency_id') and o['agency_id'] != agency_id: return self._json({'error': 'Order not found'}, 404)
            n = q(conn, "SELECT COUNT(*) c FROM invoices WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            inv_no = data.get('invoice_no') or f"INV-{ts[:4]}-{n:05d}"
            subtotal = float(data.get('subtotal') or data.get('total') or 0)
            tax = float(data.get('tax') or 0)
            fees = float(data.get('fees') or 0)
            discount = float(data.get('discount') or 0)
            total = float(data.get('total') or (subtotal + tax + fees - discount))
            due_date = data.get('due_date')
            if not due_date:
                rule = q(conn, "SELECT value FROM business_rules WHERE tenant_id=? AND rule_key='invoice_due_days' AND is_active=1", (tenant_id,), one=True)
                days = int(float(rule['value'])) if rule and str(rule['value']).replace('.','',1).isdigit() else 14
                due_date = (datetime.utcnow() + timedelta(days=days)).strftime('%Y-%m-%d')
            cur = conn.execute("""INSERT INTO invoices(tenant_id,branch_id,order_id,customer_id,invoice_no,kind,status,issue_date,due_date,currency,subtotal,tax,fees,discount,total,amount_paid,balance,notes,created_at,agency_id,source)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (tenant_id, data.get('branch_id'), order_id, customer_id, inv_no, data.get('kind') or 'invoice', 'issued', ts[:10], due_date, data.get('currency') or 'USD', subtotal, tax, fees, discount, total, 0, total, data.get('notes'), ts, agency_id, 'agency' if agency_id else 'backoffice'))

            iid = cur.lastrowid
            lines = data.get('lines') or [{"description": data.get('description') or 'Travel services', 'qty':1, 'unit_cost': float(data.get('unit_cost') or 0), 'unit_price': subtotal, 'tax': tax, 'line_total': total}]
            for ln in lines:
                conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                             (tenant_id, iid, ln.get('description'), float(ln.get('qty') or 1), float(ln.get('unit_cost') or 0), float(ln.get('unit_price') or 0), float(ln.get('tax') or 0), float(ln.get('line_total') or 0)))
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'invoice', iid, ts[:10], f"Invoice {inv_no}", 'Accounts Receivable', 'Sales', total, data.get('currency') or 'USD', ts, agency_id, 'agency' if agency_id else 'backoffice'))
            if order_id:
                conn.execute("UPDATE orders SET total_sell=?, balance_due=?, updated_at=? WHERE id=?", (total, total, ts, order_id))
                _recompute_order(conn, tenant_id, order_id)
            log_audit(conn, tenant_id, user_id, 'create', 'invoice', iid, {'invoice_no': inv_no})
        return self._json({'ok': True, 'invoice_id': iid, 'invoice_no': inv_no})

    def api_payments(self, tenant_id, sess=None):
        with db() as conn:
            sql = "SELECT p.*, c.name customer_name, i.invoice_no FROM payments p LEFT JOIN customers c ON c.id=p.customer_id LEFT JOIN invoices i ON i.id=p.invoice_id WHERE p.tenant_id=?"
            args = [tenant_id]
            if (sess or {}).get('agency_id'):
                sql += " AND p.agency_id=?"; args.append(sess['agency_id'])
            sql += " ORDER BY p.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'payments': rows})

    def api_payment_create(self, tenant_id, user_id, sess=None):
        from ingest import recompute_invoice
        data = read_json(self)
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        with db() as conn:
            ts = now_iso()
            agency_id = (sess or {}).get('agency_id')
            invoice_id = self._oid(data.get('invoice_id'))
            customer_id = self._oid(data.get('customer_id'))
            order_id = self._oid(data.get('order_id'))
            if agency_id and invoice_id:
                inv = q(conn, "SELECT agency_id, customer_id, order_id FROM invoices WHERE id=? AND tenant_id=?", (invoice_id, tenant_id), one=True)
                if not inv or inv.get('agency_id') != agency_id: return self._json({'error': 'Invoice not found'}, 404)
                customer_id = customer_id or inv.get('customer_id')
                order_id = order_id or inv.get('order_id')
            if agency_id and customer_id and not invoice_id:
                c = q(conn, "SELECT 1 FROM customers WHERE id=? AND tenant_id=? AND agency_id=?", (customer_id, tenant_id, agency_id), one=True)
                if not c: return self._json({'error': 'Customer not found'}, 404)
            n = q(conn, "SELECT COUNT(*) c FROM payments WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            pno = data.get('payment_no') or f"PMT-{ts[:4]}-{n:05d}"
            cur = conn.execute("""INSERT INTO payments(tenant_id,branch_id,invoice_id,order_id,customer_id,payment_no,method,reference,amount,currency,status,paid_at,notes,created_at,agency_id,source)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('branch_id'), invoice_id, order_id, customer_id, pno, data.get('method') or 'cash', data.get('reference'), amount, data.get('currency') or 'USD', 'received', ts[:10], data.get('notes'), ts, agency_id, 'agency' if agency_id else 'backoffice'))
            pid = cur.lastrowid
            if invoice_id:
                conn.execute("INSERT INTO allocations(tenant_id,payment_id,invoice_id,amount,created_at) VALUES (?,?,?,?,?)", (tenant_id, pid, invoice_id, amount, ts))
                recompute_invoice(conn, invoice_id)
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'payment', pid, ts[:10], f"Payment {pno}", 'Petty Cash', 'Accounts Receivable', amount, data.get('currency') or 'USD', ts, agency_id, 'agency' if agency_id else 'backoffice'))
            log_audit(conn, tenant_id, user_id, 'create', 'payment', pid, {'payment_no': pno})
        return self._json({'ok': True, 'payment_id': pid, 'payment_no': pno})

    def api_finance_summary(self, tenant_id, sess=None):
        with db() as conn:
            agency_id = (sess or {}).get('agency_id')
            if agency_id:
                ar = q(conn, "SELECT COALESCE(SUM(balance),0) balance, COALESCE(SUM(total),0) total, COALESCE(SUM(amount_paid),0) paid FROM invoices WHERE tenant_id=? AND kind='invoice' AND agency_id=?", (tenant_id, agency_id), one=True)
                ap = q(conn, "SELECT COALESCE(SUM(balance),0) balance, COALESCE(SUM(total),0) total, COALESCE(SUM(amount_paid),0) paid FROM bills WHERE tenant_id=? AND agency_id=?", (tenant_id, agency_id), one=True)
                expenses = q(conn, "SELECT COALESCE(SUM(amount),0) total, COUNT(*) c FROM expenses WHERE tenant_id=? AND agency_id=?", (tenant_id, agency_id), one=True)
                rev = q(conn, "SELECT COALESCE(SUM(total_sell),0) sell, COALESCE(SUM(total_cost),0) cost, COALESCE(SUM(margin),0) margin FROM orders WHERE tenant_id=? AND agency_id=?", (tenant_id, agency_id), one=True)
            else:
                ar = q(conn, "SELECT COALESCE(SUM(balance),0) balance, COALESCE(SUM(total),0) total, COALESCE(SUM(amount_paid),0) paid FROM invoices WHERE tenant_id=? AND kind='invoice'", (tenant_id,), one=True)
                ap = q(conn, "SELECT COALESCE(SUM(balance),0) balance, COALESCE(SUM(total),0) total, COALESCE(SUM(amount_paid),0) paid FROM bills WHERE tenant_id=?", (tenant_id,), one=True)
                expenses = q(conn, "SELECT COALESCE(SUM(amount),0) total, COUNT(*) c FROM expenses WHERE tenant_id=?", (tenant_id,), one=True)
                rev = q(conn, "SELECT COALESCE(SUM(total_sell),0) sell, COALESCE(SUM(total_cost),0) cost, COALESCE(SUM(margin),0) margin FROM orders WHERE tenant_id=?", (tenant_id,), one=True)
            pnl = q(conn, """SELECT COALESCE(SUM(CASE WHEN account_type='income' THEN amount ELSE -amount END),0) amount
                             FROM journals j LEFT JOIN chart_of_accounts a ON a.code=j.debit_account AND a.tenant_id=j.tenant_id
                             WHERE j.tenant_id=?""", (tenant_id,), one=True)
            if agency_id:
                journals = q(conn, "SELECT * FROM journals WHERE tenant_id=? AND agency_id=? ORDER BY id DESC LIMIT 50", (tenant_id, agency_id))
            else:
                journals = q(conn, "SELECT * FROM journals WHERE tenant_id=? ORDER BY id DESC LIMIT 50", (tenant_id,))
            if agency_id:
                aging = q(conn, """SELECT i.invoice_no, i.balance, i.due_date, c.name customer,
                                   CASE WHEN i.due_date IS NULL OR i.due_date>=date('now') THEN 'current'
                                        WHEN julianday('now')-julianday(i.due_date)<=30 THEN '1-30'
                                        WHEN julianday('now')-julianday(i.due_date)<=60 THEN '31-60'
                                        WHEN julianday('now')-julianday(i.due_date)<=90 THEN '61-90' ELSE '90+' END bucket
                                   FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.tenant_id=? AND i.agency_id=? AND i.balance>0 ORDER BY i.due_date IS NULL, i.due_date LIMIT 100""", (tenant_id, agency_id))
            else:
                aging = q(conn, """SELECT i.invoice_no, i.balance, i.due_date, c.name customer,
                                   CASE WHEN i.due_date IS NULL OR i.due_date>=date('now') THEN 'current'
                                        WHEN julianday('now')-julianday(i.due_date)<=30 THEN '1-30'
                                        WHEN julianday('now')-julianday(i.due_date)<=60 THEN '31-60'
                                        WHEN julianday('now')-julianday(i.due_date)<=90 THEN '61-90' ELSE '90+' END bucket
                                   FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.tenant_id=? AND i.balance>0 ORDER BY i.due_date IS NULL, i.due_date LIMIT 100""", (tenant_id,))
            bill_aging = q(conn, """SELECT bill_no supplier_bill, balance, due_date, supplier,
                                    CASE WHEN due_date IS NULL OR due_date>=date('now') THEN 'current'
                                         WHEN julianday('now')-julianday(due_date)<=30 THEN '1-30'
                                         WHEN julianday('now')-julianday(due_date)<=60 THEN '31-60'
                                         WHEN julianday('now')-julianday(due_date)<=90 THEN '61-90' ELSE '90+' END bucket
                                    FROM bills WHERE tenant_id=? AND balance>0 ORDER BY due_date IS NULL, due_date LIMIT 100""", (tenant_id,))
            accounts = q(conn, "SELECT code,name,account_type,account_group,normal_balance,is_active FROM chart_of_accounts WHERE tenant_id=? ORDER BY code", (tenant_id,))
            cash = q(conn, "SELECT COALESCE(SUM(amount),0) c FROM journals WHERE tenant_id=? AND (debit_account='Petty Cash' OR credit_account='Petty Cash')", (tenant_id,), one=True)
        return self._json({'ar': ar, 'ap': ap, 'revenue': rev, 'expenses': expenses, 'pnl': pnl, 'cash': cash,
                           'journals': journals, 'aging': aging, 'bill_aging': bill_aging, 'accounts': accounts})

    def api_tasks(self, tenant_id, sess=None):
        with db() as conn:
            sql = "SELECT t.*, o.pnr, u.full_name assignee FROM tasks t LEFT JOIN orders o ON o.id=t.order_id LEFT JOIN users u ON u.id=t.assignee_user_id WHERE t.tenant_id=?"
            args = [tenant_id]
            if (sess or {}).get('agency_id'):
                # agency: show tasks assigned to their users or unscoped tasks (tenant control-centre) - keep simple
                sql += " AND (t.assignee_user_id=? OR t.assignee_user_id IS NULL)"
                args.append((sess or {}).get('user_id'))
            sql += " ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, t.due_at IS NULL, t.due_at, t.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'tasks': rows})

    def api_task_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        title = (data.get('title') or '').strip()
        if not title:
            return self._json({'error': 'title required'}, 400)
        with db() as conn:
            cur = conn.execute("INSERT INTO tasks(tenant_id,order_id,type,title,description,assignee_user_id,status,priority,due_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (tenant_id, data.get('order_id'), data.get('type') or 'general', title, data.get('description'), data.get('assignee_user_id'), 'open', data.get('priority') or 'medium', data.get('due_at'), now_iso()))
            tid = cur.lastrowid
        return self._json({'ok': True, 'task_id': tid})

    def api_task_update(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        tid = data.get('task_id')
        status = data.get('status')
        if not tid or not status:
            return self._json({'error': 'task_id and status required'}, 400)
        with db() as conn:
            closed = now_iso() if status == 'closed' else None
            conn.execute("UPDATE tasks SET status=?, closed_at=COALESCE(?,closed_at) WHERE id=? AND tenant_id=?", (status, closed, tid, tenant_id))
        return self._json({'ok': True})

    def api_notifications(self, tenant_id, sess=None):
        with db() as conn:
            sql = "SELECT * FROM notifications WHERE tenant_id=?"
            args = [tenant_id]
            uid = (sess or {}).get('user_id')
            if (sess or {}).get('agency_id') and uid:
                sql += " AND (user_id=? OR user_id IS NULL)"
                args.append(uid)
            elif uid:
                # show tenant-wide (null) and user-specific
                sql += " AND (user_id=? OR user_id IS NULL)"
                args.append(uid)
            sql += " ORDER BY id DESC LIMIT 200"
            rows = q(conn, sql, args)
        return self._json({'notifications': rows})

    def api_notification_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        title = (data.get('title') or '').strip()
        if not title:
            return self._json({'error': 'title required'}, 400)
        with db() as conn:
            cur = conn.execute("INSERT INTO notifications(tenant_id,user_id,notification_type,title,message,entity_type,entity_id,is_read,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (tenant_id, user_id, data.get('notification_type') or 'info', title, data.get('message'), data.get('entity_type'), self._oid(data.get('entity_id')), 0, now_iso()))
        return self._json({'ok': True, 'notification_id': cur.lastrowid})

    def api_notification_read(self, tenant_id, sess=None):
        data = read_json(self)
        nid = self._oid(data.get('id'))
        uid = (sess or {}).get('user_id')
        with db() as conn:
            if nid:
                if uid:
                    conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND tenant_id=? AND (user_id=? OR user_id IS NULL)", (nid, tenant_id, uid))
                else:
                    conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND tenant_id=?", (nid, tenant_id))
            else:
                if uid:
                    conn.execute("UPDATE notifications SET is_read=1 WHERE tenant_id=? AND (user_id=? OR user_id IS NULL)", (tenant_id, uid))
                else:
                    conn.execute("UPDATE notifications SET is_read=1 WHERE tenant_id=?", (tenant_id,))
        return self._json({'ok': True})

    def api_admin_overview(self, tenant_id):
        with db() as conn:
            plan = plan_for_tenant(conn, tenant_id)
            usage = usage_counts(conn, tenant_id)
            sub = q(conn, "SELECT s.*, p.name plan_name, p.code plan_code, p.price_monthly FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.tenant_id=? ORDER BY s.id DESC LIMIT 1", (tenant_id,), one=True)
            plans = q(conn, "SELECT * FROM plans ORDER BY price_monthly")
            branches = q(conn, "SELECT * FROM branches WHERE tenant_id=?", (tenant_id,))
            users = q(conn, "SELECT id,username,full_name,email,role,status,created_at FROM users WHERE tenant_id=? ORDER BY id", (tenant_id,))
            audits = q(conn, "SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id WHERE a.tenant_id=? ORDER BY a.id DESC LIMIT 50", (tenant_id,))
            gw = get_billing_gateway(conn, tenant_id=tenant_id, provider='flutterwave')
            sub_invoices = q(conn, "SELECT * FROM subscription_invoices WHERE tenant_id=? ORDER BY id DESC LIMIT 20", (tenant_id,))
        gw_safe = None
        if gw:
            gw_safe = {k: gw.get(k) for k in ('id','scope','name','provider','is_active','mode','updated_at')}
            gw_safe['public_key_masked'] = mask_secret(gw.get('public_key'))
            gw_safe['secret_key_set'] = bool(gw.get('secret_key'))
            gw_safe['encryption_key_set'] = bool(gw.get('encryption_key'))
            gw_safe['webhook_secret_set'] = bool(gw.get('webhook_secret'))
            gw_safe['config'] = gw.get('config') or {}
        return self._json({'plan': plan, 'usage': usage, 'subscription': sub, 'plans': plans, 'branches': branches, 'users': users, 'audits': audits, 'billing_gateway': gw_safe, 'subscription_invoices': sub_invoices})

    def api_billing_overview(self, tenant_id):
        with db() as conn:
            plan = plan_for_tenant(conn, tenant_id)
            sub = q(conn, "SELECT s.*, p.name plan_name, p.code plan_code, p.price_monthly FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.tenant_id=? ORDER BY s.id DESC LIMIT 1", (tenant_id,), one=True)
            plans = q(conn, "SELECT * FROM plans ORDER BY price_monthly")
            gw = get_billing_gateway(conn, tenant_id=tenant_id, provider='flutterwave')
            invoices = q(conn, "SELECT * FROM subscription_invoices WHERE tenant_id=? ORDER BY id DESC LIMIT 50", (tenant_id,))
            payments = q(conn, "SELECT p.*, i.invoice_no FROM subscription_payments p LEFT JOIN subscription_invoices i ON i.id=p.subscription_invoice_id WHERE p.tenant_id=? ORDER BY p.id DESC LIMIT 50", (tenant_id,))
        gw_safe = None
        if gw:
            gw_safe = {k: gw.get(k) for k in ('id','scope','tenant_id','name','provider','is_active','mode','updated_at')}
            gw_safe['public_key'] = gw.get('public_key') or ''
            gw_safe['public_key_masked'] = mask_secret(gw.get('public_key'))
            gw_safe['secret_key_set'] = bool(gw.get('secret_key'))
            gw_safe['secret_key_masked'] = mask_secret(gw.get('secret_key'))
            gw_safe['encryption_key_set'] = bool(gw.get('encryption_key'))
            gw_safe['webhook_secret_set'] = bool(gw.get('webhook_secret'))
            gw_safe['config'] = gw.get('config') or {}
        return self._json({'plan': plan, 'subscription': sub, 'plans': plans, 'gateway': gw_safe, 'invoices': invoices, 'payments': payments})

    def api_billing_gateway_save(self, tenant_id, user_id, role):
        if role != 'admin':
            return self._json({'error': 'Admin role required'}, 403)
        data = read_json(self)
        provider = (data.get('provider') or 'flutterwave').strip().lower()
        with db() as conn:
            gw = get_billing_gateway(conn, tenant_id=tenant_id, provider=provider)
            ts = now_iso()
            cfg = data.get('config') or {}
            if isinstance(cfg, str):
                try: cfg = json.loads(cfg)
                except Exception: cfg = {}
            existing_cfg = {}
            if gw and gw.get('config_json'):
                try: existing_cfg = json.loads(gw.get('config_json') or '{}')
                except Exception: existing_cfg = {}
            merged_cfg = {**existing_cfg, **cfg}
            # sensible defaults for flutterwave mobile money
            merged_cfg.setdefault('methods', ['mobilemoney', 'card', 'banktransfer', 'ussd'])
            merged_cfg.setdefault('primary', 'mobilemoney')
            merged_cfg.setdefault('currency', 'UGX')
            merged_cfg.setdefault('country', 'UG')
            pub = data.get('public_key') if data.get('public_key') is not None else (gw.get('public_key') if gw else '')
            sec = data.get('secret_key') if data.get('secret_key') not in (None, '') else (gw.get('secret_key') if gw else '')
            enc = data.get('encryption_key') if data.get('encryption_key') not in (None, '') else (gw.get('encryption_key') if gw else '')
            whs = data.get('webhook_secret') if data.get('webhook_secret') not in (None, '') else (gw.get('webhook_secret') if gw else '')
            is_active = 1 if str(data.get('is_active', (gw.get('is_active') if gw else 0))).lower() in ('1','true','yes','on','active') or data.get('is_active') is True else 0
            mode = (data.get('mode') or (gw.get('mode') if gw else 'test') or 'test')
            name = data.get('name') or (gw.get('name') if gw else 'Flutterwave')
            if gw:
                conn.execute("""UPDATE billing_gateways SET name=?, public_key=?, secret_key=?, encryption_key=?, webhook_secret=?, config_json=?, is_active=?, mode=?, updated_at=? WHERE id=?""",
                             (name, pub or '', sec or '', enc or '', whs or '', json.dumps(merged_cfg, default=str), is_active, mode, ts, gw['id']))
                gid = gw['id']
            else:
                cur = conn.execute("""INSERT INTO billing_gateways(scope,tenant_id,name,provider,public_key,secret_key,encryption_key,webhook_secret,config_json,is_active,mode,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   ('tenant', tenant_id, name, provider, pub or '', sec or '', enc or '', whs or '', json.dumps(merged_cfg, default=str), is_active, mode, ts, ts))
                gid = cur.lastrowid
            log_audit(conn, tenant_id, user_id, 'save', 'billing_gateway', gid, {'provider': provider, 'is_active': bool(is_active), 'mode': mode})
        return self._json({'ok': True, 'gateway_id': gid})

    def api_billing_subscription_invoice(self, tenant_id, user_id):
        data = read_json(self)
        plan_id = data.get('plan_id')
        cycle = (data.get('billing_cycle') or 'monthly').lower()
        if cycle not in ('monthly', 'yearly'): cycle = 'monthly'
        with db() as conn:
            if not plan_id:
                sub = q(conn, "SELECT plan_id FROM subscriptions WHERE tenant_id=? ORDER BY id DESC LIMIT 1", (tenant_id,), one=True)
                t = q(conn, "SELECT plan_id FROM tenants WHERE id=?", (tenant_id,), one=True)
                plan_id = (sub or {}).get('plan_id') or (t or {}).get('plan_id') or 2
            plan = q(conn, "SELECT * FROM plans WHERE id=?", (int(plan_id),), one=True)
            if not plan:
                return self._json({'error': 'Plan not found'}, 404)
            amount = float(plan.get('price_monthly') or 0) * (12 if cycle == 'yearly' else 1)
            currency = (data.get('currency') or 'USD').upper()
            # allow override if Flutterwave MoMo currency differs (e.g. UGX) by passing amount/currency
            if data.get('amount') is not None:
                try: amount = float(data.get('amount'))
                except Exception: pass
            ts = now_iso()
            n = q(conn, "SELECT COUNT(*) c FROM subscription_invoices WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            inv_no = f"SUB-{ts[:4]}-{tenant_id:03d}-{n:05d}"
            sub = q(conn, "SELECT id FROM subscriptions WHERE tenant_id=? ORDER BY id DESC LIMIT 1", (tenant_id,), one=True)
            sub_id = sub['id'] if sub else None
            start = ts[:10]
            try:
                end = (datetime.utcnow() + timedelta(days=365 if cycle=='yearly' else 30)).date().isoformat()
            except Exception:
                end = None
            cur = conn.execute("""INSERT INTO subscription_invoices(tenant_id,subscription_id,plan_id,invoice_no,amount,currency,billing_cycle,period_start,period_end,status,meta_json,created_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, sub_id, int(plan_id), inv_no, amount, currency, cycle, start, end, 'pending', json.dumps({'plan_name': plan.get('name'), 'plan_code': plan.get('code')}), ts))
            iid = cur.lastrowid
            log_audit(conn, tenant_id, user_id, 'create', 'subscription_invoice', iid, {'invoice_no': inv_no, 'amount': amount, 'currency': currency})
        return self._json({'ok': True, 'invoice': {'id': iid, 'invoice_no': inv_no, 'amount': amount, 'currency': currency, 'plan': plan['name'], 'billing_cycle': cycle}})

    def api_billing_subscription_pay_flutterwave(self, tenant_id, user_id):
        data = read_json(self)
        inv_id = data.get('invoice_id')
        if not inv_id:
            return self._json({'error': 'invoice_id required'}, 400)
        with db() as conn:
            inv = q(conn, "SELECT * FROM subscription_invoices WHERE id=? AND tenant_id=?", (int(inv_id), tenant_id), one=True)
            if not inv:
                return self._json({'error': 'Invoice not found'}, 404)
            if inv.get('status') == 'paid':
                return self._json({'ok': True, 'already_paid': True, 'message': 'Invoice already paid'})
            gw = get_billing_gateway(conn, tenant_id=tenant_id, provider='flutterwave')
            if not gw or not gw.get('is_active'):
                return self._json({'error': 'Flutterwave gateway is not configured/active'}, 400)
            if not gw.get('secret_key'):
                return self._json({'error': 'Flutterwave secret key missing'}, 400)
            cfg = gw.get('config') or {}
            currency = (data.get('currency') or inv.get('currency') or cfg.get('currency') or 'USD').upper()
            amount = float(data.get('amount') or inv.get('amount') or 0)
            if amount <= 0:
                return self._json({'error': 'Invalid amount'}, 400)
            tx_ref = data.get('tx_ref') or f"SUB-{tenant_id}-{inv['id']}-{uuid.uuid4().hex[:10]}"
            tenant = q(conn, "SELECT * FROM tenants WHERE id=?", (tenant_id,), one=True)
            user = q(conn, "SELECT * FROM users WHERE id=?", (user_id,), one=True)
            email = data.get('email') or (user or {}).get('email') or f"billing+{(tenant or {}).get('code','tenant')}@example.com"
            phone = data.get('phone') or data.get('phonenumber') or ''
            name = data.get('name') or (user or {}).get('full_name') or (tenant or {}).get('name') or 'Customer'
            redirect_url = data.get('redirect_url') or cfg.get('redirect_url') or ''
            payment_options = data.get('payment_options') or cfg.get('payment_options') or 'mobilemoney,card,banktransfer,ussd'
            payload = {
                'tx_ref': tx_ref,
                'amount': amount,
                'currency': currency,
                'redirect_url': redirect_url,
                'payment_options': payment_options,
                'customer': {'email': email, 'phonenumber': phone, 'name': name},
                'customizations': {
                    'title': (tenant or {}).get('name') or 'Travel ERP Subscription',
                    'description': f"Subscription payment for {inv.get('invoice_no')} ({inv.get('billing_cycle','monthly')})",
                },
                'meta': {
                    'tenant_id': tenant_id,
                    'subscription_invoice_id': inv['id'],
                    'invoice_no': inv.get('invoice_no'),
                    'plan_id': inv.get('plan_id'),
                    'purpose': 'subscription',
                    'primary_method': cfg.get('primary', 'mobilemoney')
                }
            }
            if data.get('network'):
                payload['meta']['network'] = data.get('network')
            status, resp = flutterwave_request('POST', '/payments', gw.get('secret_key'), payload)
            ts = now_iso()
            if status not in (200, 201) or (resp or {}).get('status') != 'success':
                err = (resp or {}).get('message') or f'Flutterwave error (HTTP {status})'
                conn.execute("INSERT INTO subscription_payments(tenant_id,subscription_invoice_id,gateway,tx_ref,amount,currency,status,payment_method,customer_phone,customer_email,network,raw_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (tenant_id, inv['id'], 'flutterwave', tx_ref, amount, currency, 'failed', 'mobilemoney', phone, email, data.get('network'), json.dumps({'request': payload, 'response': resp, 'http_status': status}, default=str), ts))
                conn.execute("UPDATE subscription_invoices SET status='failed', gateway='flutterwave', tx_ref=?, meta_json=? WHERE id=?",
                             (tx_ref, json.dumps({'last_error': err, 'response': resp}, default=str), inv['id']))
                return self._json({'error': err, 'details': resp}, 400)
            link = ((resp or {}).get('data') or {}).get('link')
            conn.execute("UPDATE subscription_invoices SET status='pending_payment', gateway='flutterwave', tx_ref=?, payment_link=?, meta_json=? WHERE id=?",
                         (tx_ref, link, json.dumps({'init_response': resp, 'payment_options': payment_options}, default=str), inv['id']))
            conn.execute("INSERT INTO subscription_payments(tenant_id,subscription_invoice_id,gateway,tx_ref,amount,currency,status,payment_method,customer_phone,customer_email,network,raw_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, inv['id'], 'flutterwave', tx_ref, amount, currency, 'initiated', 'mobilemoney', phone, email, data.get('network'), json.dumps({'request': payload, 'response': resp}, default=str), ts))
            log_audit(conn, tenant_id, user_id, 'initiate', 'subscription_payment', inv['id'], {'gateway': 'flutterwave', 'tx_ref': tx_ref, 'mobile_money': True})
        return self._json({'ok': True, 'tx_ref': tx_ref, 'payment_link': link, 'invoice_id': inv['id'], 'gateway': 'flutterwave', 'message': 'Open payment link to complete Mobile Money payment, then verify.'})

    def api_billing_subscription_verify(self, tenant_id, user_id):
        data = read_json(self)
        inv_id = data.get('invoice_id')
        tx_ref = data.get('tx_ref')
        transaction_id = data.get('transaction_id')
        with db() as conn:
            inv = None
            if inv_id:
                inv = q(conn, "SELECT * FROM subscription_invoices WHERE id=? AND tenant_id=?", (int(inv_id), tenant_id), one=True)
            elif tx_ref:
                inv = q(conn, "SELECT * FROM subscription_invoices WHERE tx_ref=? AND tenant_id=? ORDER BY id DESC LIMIT 1", (tx_ref, tenant_id), one=True)
            elif transaction_id:
                pay = q(conn, "SELECT subscription_invoice_id FROM subscription_payments WHERE transaction_id=? AND tenant_id=? ORDER BY id DESC LIMIT 1", (str(transaction_id), tenant_id), one=True)
                if pay:
                    inv = q(conn, "SELECT * FROM subscription_invoices WHERE id=? AND tenant_id=?", (pay['subscription_invoice_id'], tenant_id), one=True)
            if not inv:
                return self._json({'error': 'Subscription invoice not found'}, 404)
            gw = get_billing_gateway(conn, tenant_id=tenant_id, provider='flutterwave')
            if not gw or not gw.get('secret_key'):
                return self._json({'error': 'Flutterwave gateway not configured'}, 400)
            tx_ref = tx_ref or inv.get('tx_ref')
            if not tx_ref and not transaction_id:
                return self._json({'error': 'tx_ref or transaction_id required'}, 400)
            status, resp = flutterwave_verify(gw.get('secret_key'), tx_ref=tx_ref, transaction_id=transaction_id)
            if status not in (200, 201) or (resp or {}).get('status') != 'success':
                err = (resp or {}).get('message') or f'Verification failed (HTTP {status})'
                return self._json({'ok': False, 'error': err, 'details': resp}, 400)
            data_obj = (resp or {}).get('data') or {}
            tx_status = (data_obj.get('status') or '').lower()
            amount_ok = float(data_obj.get('amount') or 0) >= float(inv.get('amount') or 0)
            currency_ok = (str(data_obj.get('currency') or '').upper() == str(inv.get('currency') or '').upper())
            pay = q(conn, "SELECT * FROM subscription_payments WHERE tx_ref=? AND tenant_id=? ORDER BY id DESC LIMIT 1", (tx_ref or inv.get('tx_ref'), tenant_id), one=True)
            if tx_status == 'successful' and amount_ok and currency_ok:
                ok, msg = apply_subscription_payment_success(conn, tenant_id, inv['id'], pay, data_obj, actor_user_id=user_id)
                return self._json({'ok': ok, 'message': msg, 'transaction': data_obj, 'invoice_status': 'paid'})
            else:
                if pay:
                    conn.execute("UPDATE subscription_payments SET status=?, transaction_id=?, flw_ref=?, raw_json=?, verified_at=? WHERE id=?",
                                 (tx_status or 'failed', str(data_obj.get('id') or ''), str(data_obj.get('flw_ref') or ''), json.dumps(resp, default=str), now_iso(), pay['id']))
                conn.execute("UPDATE subscription_invoices SET status=? WHERE id=?", ('pending' if tx_status in ('pending','') else 'failed', inv['id']))
                return self._json({'ok': False, 'message': f'Transaction not successful (status={tx_status}, amount_ok={amount_ok}, currency_ok={currency_ok})', 'transaction': data_obj}, 400)

    def api_flutterwave_webhook(self):
        payload = read_json(self)
        # try to identify tenant/invoice from payload
        data_obj = payload.get('data') or {}
        meta = data_obj.get('meta') or {}
        tx_ref = data_obj.get('tx_ref') or payload.get('tx_ref')
        if tx_ref and str(tx_ref).upper().startswith('PSS-'):
            with db() as conn:
                self._apply_pss_flw(conn, str(tx_ref), payload, 'successful')
            return self._json({'ok': True, 'pss': True})
        inv_id = meta.get('subscription_invoice_id')
        tenant_id = meta.get('tenant_id')
        with db() as conn:
            inv = None
            if inv_id and tenant_id:
                inv = q(conn, "SELECT * FROM subscription_invoices WHERE id=? AND tenant_id=?", (int(inv_id), int(tenant_id)), one=True)
            elif tx_ref:
                inv = q(conn, "SELECT * FROM subscription_invoices WHERE tx_ref=? ORDER BY id DESC LIMIT 1", (tx_ref,), one=True)
            if not inv:
                # acknowledge to avoid retries storms, but report not found
                return self._json({'ok': True, 'ignored': True, 'reason': 'invoice not found'})
            tenant_id = inv['tenant_id']
            gw = get_billing_gateway(conn, tenant_id=tenant_id, provider='flutterwave')
            # verify webhook signature/hash if configured
            if gw and gw.get('webhook_secret'):
                sent = self.headers.get('verif-hash') or self.headers.get('Verif-Hash') or ''
                if not hmac.compare_digest(str(sent), str(gw.get('webhook_secret'))):
                    return self._json({'error': 'Invalid webhook signature'}, 401)
            if not gw or not gw.get('secret_key'):
                return self._json({'error': 'Gateway not configured'}, 400)
            # verify with Flutterwave for authenticity
            txid = data_obj.get('id') or payload.get('id')
            tx_ref = tx_ref or inv.get('tx_ref')
            status, resp = flutterwave_verify(gw.get('secret_key'), tx_ref=tx_ref, transaction_id=txid)
            if status in (200,201) and (resp or {}).get('status') == 'success':
                vdata = (resp or {}).get('data') or {}
                tx_status = (vdata.get('status') or '').lower()
                amount_ok = float(vdata.get('amount') or 0) >= float(inv.get('amount') or 0)
                currency_ok = (str(vdata.get('currency') or '').upper() == str(inv.get('currency') or '').upper())
                pay = q(conn, "SELECT * FROM subscription_payments WHERE tx_ref=? AND tenant_id=? ORDER BY id DESC LIMIT 1", (tx_ref or inv.get('tx_ref'), tenant_id), one=True)
                if tx_status == 'successful' and amount_ok and currency_ok:
                    apply_subscription_payment_success(conn, tenant_id, inv['id'], pay, vdata, actor_user_id=None)
                else:
                    if pay:
                        conn.execute("UPDATE subscription_payments SET status=?, raw_json=?, verified_at=? WHERE id=?", (tx_status or 'failed', json.dumps(resp, default=str), now_iso(), pay['id']))
            return self._json({'ok': True})

    def _oid(self, value):
        try:
            return int(value)
        except Exception:
            return None

    def _branding(self, conn, tenant_id):
        row = q(conn, """SELECT t.name tenant_name, t.settings_json, b.*,
                                a.company_name a_company_name, a.legal_name a_legal_name, a.address a_address,
                                a.city a_city, a.state a_state, a.postal_code a_postal, a.country a_country,
                                a.phone a_phone, a.email a_email, a.website a_website, a.tax_id a_tax_id,
                                a.default_currency a_currency
                         FROM tenants t
                         LEFT JOIN tenant_branding b ON b.tenant_id=t.id
                         LEFT JOIN agency_profile a ON a.tenant_id=t.id
                         WHERE t.id=?""", (tenant_id,), one=True)
        return branding_from_row(row)

    def api_agency_profile_get(self, tenant_id):
        with db() as conn:
            row = q(conn, "SELECT * FROM agency_profile WHERE tenant_id=?", (tenant_id,), one=True)
            branding = q(conn, "SELECT * FROM tenant_branding WHERE tenant_id=?", (tenant_id,), one=True)
        return self._json({'profile': row or {}, 'branding': branding or {}})

    def api_agency_profile_save(self, tenant_id):
        data = read_json(self)
        with db() as conn:
            ts = now_iso()
            existing = q(conn, "SELECT * FROM agency_profile WHERE tenant_id=?", (tenant_id,), one=True)
            exists = bool(existing)
            fields = ['company_name','legal_name','registration_no','tax_id','vat_no','address','city','state','postal_code','country','phone','email','website','default_currency','timezone','fiscal_year_start','iata_code','notes']
            vals = [data.get(f) if f in data else (existing or {}).get(f) for f in fields]
            if exists:
                conn.execute(f"UPDATE agency_profile SET {','.join(f+'=?' for f in fields)},updated_at=? WHERE tenant_id=?", vals+[ts, tenant_id])
            else:
                conn.execute(f"INSERT INTO agency_profile(tenant_id,{','.join(fields)},updated_at) VALUES ({','.join(['?']*(len(fields)+2))})", [tenant_id]+vals+[ts])
            company = data.get('company_name') or data.get('legal_name') or (existing or {}).get('company_name') or (existing or {}).get('legal_name') or 'Fayaka Airline'
            settings_in = json.loads((q(conn, "SELECT settings_json FROM tenants WHERE id=?", (tenant_id,), one=True) or {}).get('settings_json') or '{}')
            for key, field in (('default_currency','default_currency'), ('timezone','timezone'), ('fiscal_year_start','fiscal_year_start')):
                if field in data:
                    settings_in[key] = data.get(field)
            conn.execute("UPDATE tenants SET name=?, settings_json=? WHERE id=?", (company, json.dumps(settings_in), tenant_id))
            addr_parts = [p for p in [data.get('address'), data.get('city'), data.get('state'), data.get('postal_code'), data.get('country')] if p]
            full_addr = ', '.join(str(p) for p in addr_parts)
            location = data.get('city') or data.get('country') or data.get('address') or ''
            brand = q(conn, "SELECT * FROM tenant_branding WHERE tenant_id=?", (tenant_id,), one=True) or {}
            logo_data = data.get('logo_data') if 'logo_data' in data else brand.get('logo_data')
            logo_data = logo_data or ''
            logo_text = data.get('logo_text') or brand.get('logo_text') or (company[:3].upper() if company else 'TE')
            primary = data.get('primary_color') or brand.get('primary_color') or '#0f2742'
            accent = data.get('accent_color') or brand.get('accent_color') or '#0b6efd'
            footer = data.get('footer_note') if 'footer_note' in data else (brand.get('footer_note') or 'Generated by Travel ERP - Confidential')
            conn.execute("""INSERT INTO tenant_branding(tenant_id,company_name,legal_name,location,address,phone,email,website,tax_id,logo_text,logo_data,primary_color,accent_color,footer_note,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(tenant_id) DO UPDATE SET company_name=excluded.company_name, legal_name=excluded.legal_name, location=excluded.location,
                            address=excluded.address, phone=excluded.phone, email=excluded.email, website=excluded.website, tax_id=excluded.tax_id,
                            logo_text=excluded.logo_text, logo_data=excluded.logo_data, primary_color=excluded.primary_color, accent_color=excluded.accent_color,
                            footer_note=excluded.footer_note, updated_at=excluded.updated_at""",
                         (tenant_id, company, data.get('legal_name') or '', location, full_addr or (data.get('address') or ''), data.get('phone') or '', data.get('email') or '', data.get('website') or '', data.get('tax_id') or '', logo_text, logo_data, primary, accent, footer, ts))
        return self._json({'ok': True})

    def _setup_columns(self, table):
        return {
            'users': ['username','full_name','email','role','status','branch_id'],
            'chart_of_accounts': ['code','name','account_type','account_group','normal_balance','tips','opening_balance','fcy','is_active','description'],
            'products': ['sku','name','description','category','product_type','supplier','with_inventory','unit','sale_price','cost_price','tax_code','is_active'],
            'currencies': ['code','name','symbol','exchange_rate','decimal_places','is_base','is_active'],
            'contacts': ['contact_type','tips','name','type','supplier_type','balance','status','fcy','company','email','phone','address','terms','currency','reconciliation_format','contact_person','is_core','notes','is_active'],
            'business_rules': ['rule_key','rule_name','description','value','value_type','is_active'],
            'preferences': ['preference_key','preference_value'],
            'mail_templates': ['code','subject','body_html','is_active'],
            'integrations': ['provider','name','integration_type','config_json','status'],
            'partners': ['name','partner_type','iata_code','country','contact_person','email','phone','commission_rate','status','notes'],
            'unused_tickets': ['ticket_no','pnr','traveler_name','airline','issue_date','travel_date','expiry_date','ticket_status','refund_status','value','currency','notes'],
            'disruptions': ['disruption_type','severity','affected_pnr','traveler_name','description','resolution','status'],
            'refunds': ['refund_no','order_id','pnr','ticket_no','traveler_name','source','reason','amount','currency','policy_check','auto_void','status','requested_at','processed_at','notes'],
            'vouchers': ['voucher_no','voucher_type','supplier','pnr','traveler_name','issue_date','service_date','confirmation_no','amount','currency','status','notes'],
            'coupons': ['coupon_no','pnr','ticket_no','traveler_name','airline','issue_date','cancel_date','status','cancel_reason','pre_billed','refund_amount','currency','notes'],
            'bank_accounts': ['account_name','account_type','account_no','bank_name','currency','opening_balance','current_balance','is_active','notes'],
            'bank_transactions': ['txn_no','account_id','txn_date','description','txn_type','amount','currency','reference','reconciliation_status','notes'],
            'reconciliations': ['recon_no','source','statement_ref','period','expected_amount','actual_amount','variance','status','resolution','resolved_at','notes'],
            'pss_airports': ['code','icao','name','city','country','timezone','terminal','region','status','is_hub'],
            'pss_channels': ['code','name','channel_type','commission_rate','allowed_airports','currency','settlement_days','status'],
            'pss_stock_pools': ['pool_no','airline_code','prefix','start_serial','end_serial','next_serial','issued_count','void_count','location_type','location_code','status','notes'],
            'pss_ancillaries': ['code','name','category','description','price','currency','unit','capacity','flight_id','status'],
            'pss_baggage_rules': ['flight_id','cabin_class','free_kg','free_pieces','excess_rate_per_kg','max_kg','notes'],
            'pss_airport_docs': ['airport_code','doc_type','title','description','requirement','applies_to','status'],
            'pss_weather': ['airport_code','condition','temperature_c','wind_kph','visibility_km','advisory','observed_at','status'],
            'branches': ['name','code','city','country']
        }[table]

    def _safe_config(self, raw):
        try:
            cfg = json.loads(raw or '{}')
        except Exception:
            cfg = {}
        out = {}
        for k, v in cfg.items():
            if any(x in k.lower() for x in ('secret','token','password','key')):
                out[k] = '********' if v else ''
            else:
                out[k] = v
        return out

    def api_setup_resource(self, tenant_id, slug, table, key, rid, method):
        cols = self._setup_columns(table)
        with db() as conn:
            if method == 'GET':
                rows = q(conn, f"SELECT * FROM {table} WHERE tenant_id=? ORDER BY id DESC LIMIT 1000", (tenant_id,))
                if table == 'chart_of_accounts':
                    for r in rows:
                        dr = q(conn, "SELECT COALESCE(SUM(amount),0) v FROM journals WHERE tenant_id=? AND debit_account IN (?,?)", (tenant_id, r['name'], r['code']), one=True)['v']
                        cr = q(conn, "SELECT COALESCE(SUM(amount),0) v FROM journals WHERE tenant_id=? AND credit_account IN (?,?)", (tenant_id, r['name'], r['code']), one=True)['v']
                        bal = (r['opening_balance'] or 0) + dr - cr
                        r['balance'] = round(bal, 2)
                        r['account_number'] = r['code']
                        r['account_name'] = r['name']
                        r['account_type_label'] = r['account_group'] or r['account_type']
                if table == 'integrations':
                    for r in rows:
                        r['config'] = self._safe_config(r['config_json'])
                if table == 'users':
                    for r in rows:
                        r.pop('password', None)
                return self._json({key: rows})
            rid = self._oid(rid)
            if method == 'DELETE':
                if table == 'users':
                    user_row = q(conn, "SELECT role FROM users WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
                    if user_row and user_row.get('role') == 'owner':
                        return self._json({'error': 'Owner account cannot be deleted'}, 400)
                    _invalidate_user_sessions(rid)
                conn.execute(f"DELETE FROM {table} WHERE id=? AND tenant_id=?", (rid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            available_types = {r['name']: (r['type'] or '').upper() for r in q(conn, f"PRAGMA table_info({table})")}
            vals = {}
            for c in cols:
                v = data.get(c)
                if v == '' and any(t in available_types.get(c, '') for t in ('INT', 'REAL', 'FLOAT', 'DOUBLE', 'NUMERIC')):
                    v = None
                vals[c] = v
            if 'is_active' in vals and vals['is_active'] is None:
                vals['is_active'] = 1 if str(vals.get('status') or 'active').lower() in ('active','1','true') else 0
            if table == 'users':
                sess_obj = require_auth(self)
                caller_group = role_group(sess_obj.get('role')) if sess_obj else 'airline_admin'
                password = data.get('password')
                if password:
                    err = _password_policy_error(password)
                    if err:
                        return self._json({'error': err}, 400)
                if rid is None:
                    if not password:
                        return self._json({'error': 'password required'}, 400)
                    vals['password'] = _hash_password(password)
                    vals['role'] = vals.get('role') or 'agent'
                    if role_group(vals['role']) == 'airline_admin' and caller_group != 'airline_admin':
                        vals['role'] = 'agent'
                else:
                    if password:
                        vals['password'] = _hash_password(password)
                    else:
                        vals.pop('password', None)
                    if caller_group != 'airline_admin':
                        existing = q(conn, "SELECT role FROM users WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
                        if existing:
                            vals['role'] = existing['role']
            elif table == 'integrations':
                cfg = data.get('config') if isinstance(data.get('config'), dict) else {}
                old = q(conn, "SELECT config_json FROM integrations WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) if rid else None
                old_cfg = self._safe_config(old['config_json']) if old else {}
                for k, v in cfg.items():
                    if isinstance(v, str) and v.startswith('********') and k in old_cfg:
                        cfg[k] = json.loads(old['config_json'] or '{}').get(k, '')
                vals['config_json'] = json.dumps(cfg)
                vals.pop('config', None)
            active_cols = list(vals.keys())
            available = {r['name'] for r in q(conn, f"PRAGMA table_info({table})")}
            ts = now_iso()
            if rid:
                sets = [c+'=?' for c in active_cols]
                params = [vals[c] for c in active_cols]
                if 'updated_at' in available:
                    sets.append('updated_at=?'); params.append(ts)
                params += [rid, tenant_id]
                conn.execute(f"UPDATE {table} SET {','.join(sets)} WHERE id=? AND tenant_id=?", params)
            else:
                insert_cols = ['tenant_id']+active_cols
                params = [tenant_id]+[vals[c] for c in active_cols]
                if 'created_at' in available:
                    insert_cols.append('created_at'); params.append(ts)
                if 'updated_at' in available:
                    insert_cols.append('updated_at'); params.append(ts)
                conn.execute(f"INSERT INTO {table}({','.join(insert_cols)}) VALUES ({','.join(['?']*len(insert_cols))})", params)
                new_id = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
                return self._json({'ok': True, 'id': new_id})
        return self._json({'ok': True})

    def api_integration_test(self, tenant_id, iid):
        iid = self._oid(iid)
        with db() as conn:
            row = q(conn, "SELECT * FROM integrations WHERE id=? AND tenant_id=?", (iid, tenant_id), one=True)
        if not row:
            return self._json({'error': 'Integration not found'}, 404)
        cfg = self._safe_config(row['config_json'])
        checks = {
            'flutterwave': ['public_key','secret_key'],
            'smtp': ['host','from_email'],
            'amadeus_air': ['folder','pattern'],
            'gds': ['provider'],
            'accounting': ['target'],
            'eticket_forwarding': ['forward_to']
        }
        missing = [x for x in checks.get(row['provider'], []) if not cfg.get(x)]
        ok = not missing
        status = 'connected' if ok else 'disconnected'
        with db() as conn:
            conn.execute("UPDATE integrations SET status=?,last_sync_at=?,updated_at=? WHERE id=? AND tenant_id=?", (status, now_iso(), now_iso(), iid, tenant_id))
        return self._json({'ok': ok, 'status': status, 'message': 'Connection ready' if ok else 'Missing: '+', '.join(missing)})

    def api_mail_template_preview(self, tenant_id, tid):
        tid = self._oid(tid)
        with db() as conn:
            row = q(conn, "SELECT * FROM mail_templates WHERE id=? AND tenant_id=?", (tid, tenant_id), one=True)
        if not row:
            return self._json({'error': 'Template not found'}, 404)
        samples = {'customer_name':'Sample Customer','pnr':'ABC123','invoice_no':'INV-00001','credit_note_no':'CN-00001','estimate_no':'EST-00001','origin':'KLA','destination':'DXB','travel_start':'2026-12-01','total':'1250.00','currency':'USD','amount':'1250.00','title':'Ticketing follow-up','due_at':'2026-11-25'}
        subj = row['subject'] or ''
        body = row['body_html'] or ''
        for k, v in samples.items():
            subj = subj.replace('{{'+k+'}}', v); body = body.replace('{{'+k+'}}', v)
        return self._json({'subject': subj, 'body_html': body})

    TRAVELER_FIELDS = ('customer_id','full_name','first_name','last_name','title','traveler_type','email','phone','mobile','doc_no','doc_type','doc_issued','doc_expiry','dob','marital_status','gender','nationality','display_name','address','city','state','postal_code','country','notes','status','smoking','seat_pref','meal_pref','flight_request','hotel_bedding','hotel_request','car_size','car_request','preferred_airlines','preferred_hotels','preferred_car_companies')


    def _ensure_customer_from_traveler(self, conn, tenant_id, tid, override=None):
        row = q(conn, "SELECT * FROM travelers WHERE id=? AND tenant_id=?", (tid, tenant_id), one=True)
        if not row:
            return None
        override = override or {}
        name = (override.get('full_name') or row.get('display_name') or row.get('full_name') or '').strip()
        email = override.get('email', row.get('email')) or None
        phone = override.get('phone', row.get('phone')) or override.get('mobile', row.get('mobile')) or None
        if not name:
            return None
        cid = self._oid(override.get('customer_id', row.get('customer_id')))
        found = None
        if email:
            found = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND email=? LIMIT 1", (tenant_id, email), one=True)
        if not found:
            found = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND name=? LIMIT 1", (tenant_id, name), one=True)
        if found and not cid:
            cid = found['id']
        if cid and not q(conn, "SELECT 1 FROM customers WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True):
            cid = None
        if not cid:
            cur = conn.execute("INSERT INTO customers(tenant_id,type,name,email,phone,created_at) VALUES (?,?,?,?,?,?)",
                               (tenant_id, 'B2C', name, email, phone, now_iso()))
            cid = cur.lastrowid
        else:
            conn.execute("""UPDATE customers SET name=COALESCE(NULLIF(?,''),name), email=COALESCE(?,email),
                            phone=COALESCE(?,phone) WHERE id=? AND tenant_id=?""",
                         (name, email, phone, cid, tenant_id))
        conn.execute("UPDATE travelers SET customer_id=?, email=COALESCE(?,email), phone=COALESCE(?,phone) WHERE id=? AND tenant_id=?",
                     (cid, email, phone, tid, tenant_id))
        return cid

    def _traveler_values(self, data, name=None):
        name = (name or data.get('full_name') or '').strip()
        parts = name.split(' ', 1)
        values = {
            'customer_id': self._oid(data.get('customer_id')),
            'full_name': name,
            'first_name': data.get('first_name') or parts[0] if name else None,
            'last_name': data.get('last_name') or (parts[1] if len(parts) > 1 else ''),
            'title': data.get('title'),
            'traveler_type': data.get('traveler_type') or 'ADT',
            'email': data.get('email'),
            'phone': data.get('phone'),
            'mobile': data.get('mobile'),
            'doc_no': data.get('doc_no'),
            'doc_type': data.get('doc_type') or 'Passport',
            'doc_issued': data.get('doc_issued'),
            'doc_expiry': data.get('doc_expiry'),
            'dob': data.get('dob'),
            'marital_status': data.get('marital_status'),
            'gender': data.get('gender'),
            'nationality': data.get('nationality'),
            'display_name': data.get('display_name') or name,
            'address': data.get('address'),
            'city': data.get('city'),
            'state': data.get('state'),
            'postal_code': data.get('postal_code'),
            'country': data.get('country'),
            'notes': data.get('notes'),
            'status': data.get('status') or 'active',
            'smoking': 1 if str(data.get('smoking')).lower() in ('1','true','yes','on') else 0,
            'seat_pref': data.get('seat_pref'),
            'meal_pref': data.get('meal_pref'),
            'flight_request': data.get('flight_request'),
            'hotel_bedding': data.get('hotel_bedding'),
            'hotel_request': data.get('hotel_request'),
            'car_size': data.get('car_size'),
            'car_request': data.get('car_request'),
            'preferred_airlines': data.get('preferred_airlines'),
            'preferred_hotels': data.get('preferred_hotels'),
            'preferred_car_companies': data.get('preferred_car_companies'),
        }
        return values

    def _insert_traveler(self, conn, tenant_id, data):
        values = self._traveler_values(data)
        if not values['full_name']:
            return None
        cols = [c for c in self.TRAVELER_FIELDS if c in values]
        sql = "INSERT INTO travelers(tenant_id," + ','.join(cols) + ",created_at) VALUES (" + ','.join(['?'] * (len(cols) + 2)) + ")"
        cur = conn.execute(sql, (tenant_id,) + tuple(values[c] for c in cols) + (now_iso(),))
        return cur.lastrowid

    def _days_to_date(self, value):
        if not value:
            return None
        try:
            d = datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
            return (d - datetime.utcnow().date()).days
        except Exception:
            return None

    def _events_for_traveler(self, row, orders=None):
        events = []
        if row.get('doc_expiry'):
            days = self._days_to_date(row.get('doc_expiry'))
            if days is not None and 0 <= days <= 90:
                events.append({'type': 'Passport expiry', 'date': row.get('doc_expiry'), 'days': days, 'label': f"Document expires in {days} days"})
        if row.get('dob'):
            try:
                dob = datetime.strptime(str(row['dob'])[:10], '%Y-%m-%d').date()
                today = datetime.utcnow().date()
                next_bday = dob.replace(year=today.year)
                if next_bday < today:
                    next_bday = dob.replace(year=today.year + 1)
                days = (next_bday - today).days
                if 0 <= days <= 30:
                    events.append({'type': 'Birthday', 'date': next_bday.isoformat(), 'days': days, 'label': f"Birthday in {days} days"})
            except Exception:
                pass
        for o in (orders or []):
            days = self._days_to_date(o.get('travel_start'))
            if days is not None and 0 <= days <= 14:
                events.append({'type': 'Travel', 'date': o.get('travel_start'), 'days': days, 'label': f"{o.get('product_type') or 'Travel'} to {o.get('destination') or '-'} in {days} days"})
        return events

    def _traveler_list_rows(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT t.*, c.name customer_name,
                              (SELECT COUNT(*) FROM order_passengers p WHERE p.traveler_id=t.id) trips,
                              (SELECT MIN(o.travel_start) FROM order_passengers p JOIN orders o ON o.id=p.order_id WHERE p.traveler_id=t.id AND o.travel_start>=date('now')) next_travel,
                              (SELECT COUNT(*) FROM order_passengers p JOIN orders o ON o.id=p.order_id WHERE p.traveler_id=t.id AND o.travel_start<=date('now') AND (o.travel_end IS NULL OR o.travel_end>=date('now'))) on_trip,
                              (SELECT COALESCE(SUM(o.total_sell-o.total_cost),0) FROM order_passengers p JOIN orders o ON o.id=p.order_id WHERE p.traveler_id=t.id) collection,
                              (SELECT COALESCE(SUM(o.total_sell),0) FROM order_passengers p JOIN orders o ON o.id=p.order_id WHERE p.traveler_id=t.id) spend
                              FROM travelers t LEFT JOIN customers c ON c.id=t.customer_id WHERE t.tenant_id=? ORDER BY t.id DESC LIMIT 500""", (tenant_id,))
        for r in rows:
            r['days_to_next'] = self._days_to_date(r.get('next_travel'))
            r['travel_status'] = 'On trip' if r.get('on_trip') else ('Scheduled' if r.get('next_travel') else 'No Plan')
            r['event_count'] = len(self._events_for_traveler(r))
        return rows

    def api_travelers_list(self, tenant_id):
        return self._json({'travelers': self._traveler_list_rows(tenant_id)})

    def api_traveler_detail(self, tenant_id, tid):
        tid = self._oid(tid)
        rows = [r for r in self._traveler_list_rows(tenant_id) if r['id'] == tid]
        if not rows:
            return self._json({'error': 'Traveler not found'}, 404)
        traveler = rows[0]
        with db() as conn:
            orders = q(conn, """SELECT o.* FROM orders o JOIN order_passengers p ON p.order_id=o.id
                               WHERE o.tenant_id=? AND p.traveler_id=? ORDER BY COALESCE(o.travel_start,o.booking_date) DESC""", (tenant_id, tid))
            invoice_ids = []
            for o in orders:
                invoice_ids.extend([i['id'] for i in q(conn, "SELECT id FROM invoices WHERE tenant_id=? AND order_id=?", (tenant_id, o['id']))])
            invoices = []
            payments = []
            if invoice_ids:
                marks = ','.join(['?'] * len(invoice_ids))
                invoices = q(conn, f"SELECT * FROM invoices WHERE tenant_id=? AND id IN ({marks}) ORDER BY id DESC", (tenant_id,) + tuple(invoice_ids))
                payments = q(conn, f"SELECT * FROM payments WHERE tenant_id=? AND invoice_id IN ({marks}) ORDER BY id DESC", (tenant_id,) + tuple(invoice_ids))
        collection = []
        for ptype in ['Flight', 'Hotel', 'Car', 'Misc']:
            subset = [o for o in orders if (o.get('product_type') or 'Flight') == ptype]
            collection.append({'type': ptype, 'bookings': len(subset), 'sell': sum(float(o.get('total_sell') or 0) for o in subset), 'cost': sum(float(o.get('total_cost') or 0) for o in subset), 'margin': sum(float(o.get('margin') or (float(o.get('total_sell') or 0) - float(o.get('total_cost') or 0))) for o in subset)})
        events = self._events_for_traveler(traveler, orders)
        return self._json({'traveler': traveler, 'bookings': orders, 'invoices': invoices, 'payments': payments, 'events': events, 'collection': collection})

    def api_traveler_create(self, tenant_id):
        data = read_json(self)
        if not (data.get('full_name') or '').strip():
            return self._json({'error': 'full_name required'}, 400)
        with db() as conn:
            tid = self._insert_traveler(conn, tenant_id, data)
            if tid:
                self._ensure_customer_from_traveler(conn, tenant_id, tid, data)
        return self._json({'ok': True, 'traveler_id': tid})

    def api_traveler_mutate(self, tenant_id, tid, method):
        tid = self._oid(tid)
        with db() as conn:
            trips = q(conn, "SELECT COUNT(*) c FROM order_passengers WHERE traveler_id=?", (tid,), one=True)['c']
            if method == 'DELETE':
                if trips:
                    return self._json({'error': 'Traveler has travel items attached and cannot be deleted'}, 400)
                conn.execute("DELETE FROM travelers WHERE id=? AND tenant_id=?", (tid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            values = self._traveler_values(data)
            fields = [c for c in self.TRAVELER_FIELDS if c in values and (c in data or c in ('status','doc_type','traveler_type','smoking','display_name'))]
            if trips and any(c in data for c in ('full_name','doc_no','passport','dob')):
                pass
            cols = [c for c in fields if c != 'customer_id' or 'customer_id' in data]
            if cols:
                sql = "UPDATE travelers SET " + ','.join(f"{c}=?" for c in cols) + " WHERE id=? AND tenant_id=?"
                conn.execute(sql, tuple(values[c] for c in cols) + (tid, tenant_id))
            if any(k in data for k in ('email', 'phone', 'mobile', 'full_name', 'display_name', 'customer_id')):
                self._ensure_customer_from_traveler(conn, tenant_id, tid, data)
        return self._json({'ok': True})

    def api_travelers_reconcile_customers(self, tenant_id):
        with db() as conn:
            rows = q(conn, "SELECT id FROM travelers WHERE tenant_id=? ORDER BY id", (tenant_id,))
            linked = 0
            for r in rows:
                if self._ensure_customer_from_traveler(conn, tenant_id, r['id'], {'customer_id': None}):
                    linked += 1
        return self._json({'ok': True, 'linked': linked})

    def api_traveler_status(self, tenant_id, tid):
        tid = self._oid(tid)
        data = read_json(self)
        status = 'inactive' if str(data.get('status')).lower() in ('inactive','0','false','off') else 'active'
        with db() as conn:
            conn.execute("UPDATE travelers SET status=? WHERE id=? AND tenant_id=?", (status, tid, tenant_id))
        return self._json({'ok': True, 'status': status})

    def api_traveler_duplicate(self, tenant_id, tid):
        tid = self._oid(tid)
        with db() as conn:
            row = q(conn, "SELECT * FROM travelers WHERE id=? AND tenant_id=?", (tid, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Traveler not found'}, 404)
            data = dict(row)
            data['full_name'] = f"{row.get('full_name') or 'Traveler'} (copy)"
            data['display_name'] = data['full_name']
            data.pop('id', None); data.pop('created_at', None)
            new_id = self._insert_traveler(conn, tenant_id, data)
        return self._json({'ok': True, 'traveler_id': new_id})

    def api_travelers_export(self, tenant_id):
        rows = self._traveler_list_rows(tenant_id)
        cols = ['full_name','display_name','title','first_name','last_name','traveler_type','email','phone','mobile','doc_type','doc_no','doc_issued','doc_expiry','dob','marital_status','gender','nationality','address','city','state','postal_code','country','status','notes','trips','spend','collection','travel_status','next_travel']
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c) for c in cols})
        return self._send(buf.getvalue().encode('utf-8'), 200, 'text/csv; charset=utf-8', {'Content-Disposition': 'attachment; filename="travelers.csv"'})

    def api_travelers_import(self, tenant_id):
        data = read_json(self)
        rows = data.get('rows') or []
        if not rows and data.get('text'):
            reader = csv.DictReader(io.StringIO(str(data.get('text'))))
            rows = list(reader)
        if not isinstance(rows, list) or not rows:
            return self._json({'error': 'No traveler rows supplied'}, 400)
        count = 0
        with db() as conn:
            for row in rows:
                if isinstance(row, dict) and self._insert_traveler(conn, tenant_id, row):
                    count += 1
        return self._json({'ok': True, 'imported': count})

    def api_travelers_bulk_status(self, tenant_id):
        data = read_json(self)
        ids = [self._oid(x) for x in (data.get('ids') or [])]
        ids = [x for x in ids if x]
        if not ids:
            return self._json({'error': 'No travelers selected'}, 400)
        status = 'inactive' if str(data.get('status')).lower() in ('inactive','0','false','off') else 'active'
        marks = ','.join(['?'] * len(ids))
        with db() as conn:
            conn.execute(f"UPDATE travelers SET status=? WHERE tenant_id=? AND id IN ({marks})", (status, tenant_id) + tuple(ids))
        return self._json({'ok': True, 'updated': len(ids), 'status': status})

    def api_travelers_bulk_delete(self, tenant_id):
        data = read_json(self)
        ids = [self._oid(x) for x in (data.get('ids') or [])]
        ids = [x for x in ids if x]
        if not ids:
            return self._json({'error': 'No travelers selected'}, 400)
        marks = ','.join(['?'] * len(ids))
        with db() as conn:
            attached = q(conn, f"SELECT traveler_id,COUNT(*) c FROM order_passengers WHERE traveler_id IN ({marks}) GROUP BY traveler_id", tuple(ids))
            if attached:
                return self._json({'error': 'Some travelers have travel items attached and cannot be deleted'}, 400)
            conn.execute(f"DELETE FROM travelers WHERE tenant_id=? AND id IN ({marks})", (tenant_id,) + tuple(ids))
        return self._json({'ok': True, 'deleted': len(ids)})

    def _traveler_statement_context(self, tenant_id, tid, date_from=None, date_to=None):
        rows = [r for r in self._traveler_list_rows(tenant_id) if r['id'] == tid]
        if not rows:
            return {}
        traveler = rows[0]
        with db() as conn:
            sql = """SELECT o.* FROM orders o JOIN order_passengers p ON p.order_id=o.id
                     WHERE o.tenant_id=? AND p.traveler_id=?"""
            args = [tenant_id, tid]
            if date_from:
                sql += " AND COALESCE(o.travel_start,o.booking_date)>=?"; args.append(date_from)
            if date_to:
                sql += " AND COALESCE(o.travel_start,o.booking_date)<=?"; args.append(date_to)
            sql += " ORDER BY COALESCE(o.travel_start,o.booking_date) DESC"
            orders = q(conn, sql, args)
            invoice_ids = []
            for o in orders:
                invoice_ids.extend([i['id'] for i in q(conn, "SELECT id FROM invoices WHERE tenant_id=? AND order_id=?", (tenant_id, o['id']))])
            invoices = []
            payments = []
            if invoice_ids:
                marks = ','.join(['?'] * len(invoice_ids))
                invoices = q(conn, f"SELECT * FROM invoices WHERE tenant_id=? AND id IN ({marks}) ORDER BY id DESC", (tenant_id,) + tuple(invoice_ids))
                payments = q(conn, f"SELECT * FROM payments WHERE tenant_id=? AND invoice_id IN ({marks}) ORDER BY id DESC", (tenant_id,) + tuple(invoice_ids))
        return {'traveler': traveler, 'orders': orders, 'invoices': invoices, 'payments': payments,
                'date_from': date_from or '', 'date_to': date_to or now_iso()[:10],
                'summary': {'bookings': len(orders), 'sell': sum(float(o.get('total_sell') or 0) for o in orders), 'cost': sum(float(o.get('total_cost') or 0) for o in orders), 'margin': sum(float(o.get('margin') or 0) for o in orders), 'invoiced': sum(float(i.get('total') or 0) for i in invoices), 'paid': sum(float(p.get('amount') or 0) for p in payments)}}

    def api_traveler_statement_email(self, tenant_id, tid):
        tid = self._oid(tid)
        data = read_json(self)
        context = self._traveler_statement_context(tenant_id, tid, data.get('from') or data.get('date_from'), data.get('to') or data.get('date_to'))
        if not context:
            return self._json({'error': 'Traveler not found'}, 404)
        traveler = context['traveler']
        recipient = (data.get('to_email') or traveler.get('email') or '').strip()
        if not recipient:
            return self._json({'error': 'No email address recorded for this traveler'}, 400)
        with db() as conn:
            smtp = q(conn, "SELECT * FROM integrations WHERE tenant_id=? AND provider='smtp' LIMIT 1", (tenant_id,), one=True)
            branding = self._branding(conn, tenant_id)
            template = q(conn, "SELECT * FROM mail_templates WHERE tenant_id=? AND code='traveler_statement' LIMIT 1", (tenant_id,), one=True)
        cfg = {}
        if smtp:
            try:
                cfg = json.loads(smtp.get('config_json') or '{}')
            except Exception:
                cfg = {}
        host = cfg.get('host') or cfg.get('smtp_host')
        port = int(cfg.get('port') or cfg.get('smtp_port') or 587)
        user = cfg.get('user') or cfg.get('username')
        password = cfg.get('password')
        from_email = cfg.get('from_email') or user
        if not host or not from_email:
            return self._json({'error': 'SMTP integration is not configured (host and from_email required)'}, 400)
        subject = template.get('subject') if template else 'Traveler statement {{full_name}}'
        body = template.get('body_html') if template else '<p>Dear {{full_name}}, please find your traveler statement attached.</p>'
        for k, v in {'full_name': traveler.get('display_name') or traveler.get('full_name'), 'from_date': context['date_from'], 'to_date': context['date_to'], 'email': recipient}.items():
            subject = str(subject).replace('{{'+k+'}}', str(v or ''))
            body = str(body).replace('{{'+k+'}}', str(v or ''))
        pdf = build_pdf('traveler', context, branding)
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = from_email
        msg['To'] = recipient
        msg.set_content('Please find your traveler statement attached.')
        msg.add_alternative(body, subtype='html')
        msg.add_attachment(pdf, maintype='application', subtype='pdf', filename=f"traveler-statement-{tid}.pdf")
        try:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        except Exception as exc:
            print('SMTP error:', exc, file=sys.stderr)
            return self._json({'error': 'Email sending failed - check SMTP settings'}, 500)
        return self._json({'ok': True, 'sent_to': recipient})

    def api_estimates_list(self, tenant_id, sess=None):
        with db() as conn:
            sql = """SELECT e.*, c.name customer_name FROM estimates e LEFT JOIN customers c ON c.id=e.customer_id
                     WHERE e.tenant_id=?"""
            args = [tenant_id]
            if (sess or {}).get('agency_id'):
                sql += " AND e.agency_id=?"; args.append(sess['agency_id'])
            sql += " ORDER BY e.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'estimates': rows})

    def api_estimate_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        title = (data.get('title') or '').strip() or 'Travel estimate'
        subtotal = float(data.get('subtotal') or 0)
        tax = float(data.get('tax') or 0)
        total = float(data.get('total') or subtotal + tax)
        ts = now_iso()
        with db() as conn:
            n = q(conn, "SELECT COUNT(*) c FROM estimates WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            est_no = data.get('estimate_no') or f"EST-{ts[:4]}-{n:05d}"
            agency_id = (sess or {}).get('agency_id')
            cur = conn.execute("""INSERT INTO estimates(tenant_id,branch_id,estimate_no,customer_id,title,description,currency,subtotal,tax,total,status,valid_until,created_by,created_at,updated_at,agency_id)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('branch_id'), est_no, self._oid(data.get('customer_id')), title, data.get('description'), data.get('currency') or 'USD', subtotal, tax, total, data.get('status') or 'draft', data.get('valid_until'), user_id, ts, ts, agency_id))
            eid = cur.lastrowid
            log_audit(conn, tenant_id, user_id, 'create', 'estimate', eid, {'estimate_no': est_no})
        return self._json({'ok': True, 'estimate_id': eid, 'estimate_no': est_no})

    def api_estimate_mutate(self, tenant_id, eid, method, sess=None):
        eid = self._oid(eid)
        with db() as conn:
            agency_id = (sess or {}).get('agency_id')
            if agency_id:
                own = q(conn, "SELECT 1 FROM estimates WHERE id=? AND tenant_id=? AND agency_id=?", (eid, tenant_id, agency_id), one=True)
                if not own: return self._json({'error': 'Estimate not found'}, 404)
            if method == 'DELETE':
                conn.execute("DELETE FROM estimates WHERE id=? AND tenant_id=?" + (" AND agency_id=?" if agency_id else ""), (eid, tenant_id) + ((agency_id,) if agency_id else ()))
                return self._json({'ok': True})
            data = read_json(self)
            subtotal = float(data.get('subtotal') or 0); tax = float(data.get('tax') or 0)
            total = float(data.get('total') or subtotal + tax)
            conn.execute("""UPDATE estimates SET customer_id=?,title=?,description=?,currency=?,subtotal=?,tax=?,total=?,status=?,valid_until=?,updated_at=? WHERE id=? AND tenant_id=?""",
                         (self._oid(data.get('customer_id')), data.get('title'), data.get('description'), data.get('currency'), subtotal, tax, total, data.get('status'), data.get('valid_until'), now_iso(), eid, tenant_id))
        return self._json({'ok': True})

    def api_estimate_convert(self, tenant_id, user_id, eid, sess=None):
        eid = self._oid(eid)
        with db() as conn:
            est = q(conn, "SELECT * FROM estimates WHERE id=? AND tenant_id=?", (eid, tenant_id), one=True)
            if not est:
                return self._json({'error': 'Estimate not found'}, 404)
            ts = now_iso()
            n = q(conn, "SELECT COUNT(*) c FROM invoices WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            inv_no = f"INV-{ts[:4]}-{n:05d}"
            cur = conn.execute("""INSERT INTO invoices(tenant_id,branch_id,customer_id,invoice_no,kind,status,issue_date,due_date,currency,subtotal,tax,total,amount_paid,balance,notes,created_at,agency_id,source)
                                  VALUES (?,?,?,?, 'invoice','issued',?,?,?,?,?,?,0,?,?,?, ?,?)""",
                               (tenant_id, est.get('branch_id'), est.get('customer_id'), inv_no, ts[:10], est.get('valid_until'), est.get('currency') or 'USD', est.get('subtotal') or 0, est.get('tax') or 0, est.get('total') or 0, est.get('total') or 0, est.get('description'), ts, est.get('agency_id'), 'estimate'))
            iid = cur.lastrowid
            conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                         (tenant_id, iid, est.get('title') or 'Estimate conversion', 1, 0, est.get('subtotal') or 0, est.get('tax') or 0, est.get('total') or 0))
            conn.execute("UPDATE estimates SET status='converted', updated_at=? WHERE id=? AND tenant_id=?", (ts, eid, tenant_id))
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'invoice', iid, ts[:10], f"Invoice {inv_no}", 'Accounts Receivable', 'Sales', est.get('total') or 0, est.get('currency') or 'USD', ts, est.get('agency_id'), 'estimate'))
            log_audit(conn, tenant_id, user_id, 'convert', 'estimate', eid, {'invoice_no': inv_no})
        return self._json({'ok': True, 'invoice_id': iid, 'invoice_no': inv_no})

    def api_credit_notes_list(self, tenant_id, sess=None):
        with db() as conn:
            sql = """SELECT n.*, c.name customer_name, i.invoice_no FROM credit_notes n
                     LEFT JOIN customers c ON c.id=n.customer_id LEFT JOIN invoices i ON i.id=n.invoice_id
                     WHERE n.tenant_id=?"""
            args = [tenant_id]
            if (sess or {}).get('agency_id'):
                sql += " AND n.agency_id=?"; args.append(sess['agency_id'])
            sql += " ORDER BY n.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'credit_notes': rows})

    def api_credit_note_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        ts = now_iso()
        with db() as conn:
            n = q(conn, "SELECT COUNT(*) c FROM credit_notes WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            cn_no = data.get('credit_note_no') or f"CN-{ts[:4]}-{n:05d}"
            agency_id = (sess or {}).get('agency_id')
            cur = conn.execute("""INSERT INTO credit_notes(tenant_id,branch_id,credit_note_no,customer_id,invoice_id,reason,description,currency,amount,status,issue_date,created_by,created_at,agency_id)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('branch_id'), cn_no, self._oid(data.get('customer_id')), self._oid(data.get('invoice_id')), data.get('reason') or 'refund', data.get('description'), data.get('currency') or 'USD', amount, 'issued', data.get('issue_date') or ts[:10], user_id, ts, agency_id))
            cid = cur.lastrowid
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'credit_note', cid, ts[:10], f"Credit note {cn_no}", 'Sales', 'Accounts Receivable', amount, data.get('currency') or 'USD', ts, agency_id, 'credit_note'))
            log_audit(conn, tenant_id, user_id, 'create', 'credit_note', cid, {'credit_note_no': cn_no})
        return self._json({'ok': True, 'credit_note_id': cid, 'credit_note_no': cn_no})

    def api_credit_note_mutate(self, tenant_id, cid, method, sess=None):
        cid = self._oid(cid)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("DELETE FROM credit_notes WHERE id=? AND tenant_id=?", (cid, tenant_id))
                conn.execute("DELETE FROM journals WHERE tenant_id=? AND ref_type='credit_note' AND ref_id=?", (tenant_id, cid))
                return self._json({'ok': True})
            data = read_json(self)
            conn.execute("""UPDATE credit_notes SET customer_id=?,invoice_id=?,reason=?,description=?,currency=?,amount=?,status=?,issue_date=? WHERE id=? AND tenant_id=?""",
                         (self._oid(data.get('customer_id')), self._oid(data.get('invoice_id')), data.get('reason'), data.get('description'), data.get('currency'), float(data.get('amount') or 0), data.get('status'), data.get('issue_date'), cid, tenant_id))
        return self._json({'ok': True})

    def api_chart_accounts_list(self, tenant_id, sess=None):
        with db() as conn:
            rows = q(conn, "SELECT * FROM chart_of_accounts WHERE tenant_id=? ORDER BY code", (tenant_id,))
        return self._json({'accounts': rows})

    def api_chart_account_create(self, tenant_id):
        data = read_json(self)
        code = (data.get('code') or '').strip()
        name = (data.get('name') or '').strip()
        if not code or not name:
            return self._json({'error': 'code and name required'}, 400)
        with db() as conn:
            exists = q(conn, "SELECT 1 FROM chart_of_accounts WHERE tenant_id=? AND code=?", (tenant_id, code), one=True)
            if exists:
                return self._json({'error': 'Account code already exists'}, 400)
            conn.execute("""INSERT INTO chart_of_accounts(tenant_id,code,name,account_type,account_group,normal_balance,parent_id,is_active,description,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, code, name, data.get('account_type') or 'expense', data.get('account_group'), data.get('normal_balance') or 'debit', self._oid(data.get('parent_id')), 1 if data.get('is_active', 1) else 0, data.get('description'), now_iso()))
        return self._json({'ok': True})

    def api_chart_account_mutate(self, tenant_id, aid, method):
        aid = self._oid(aid)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("UPDATE chart_of_accounts SET is_active=0 WHERE id=? AND tenant_id=?", (aid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            conn.execute("""UPDATE chart_of_accounts SET code=?,name=?,account_type=?,account_group=?,normal_balance=?,parent_id=?,is_active=?,description=? WHERE id=? AND tenant_id=?""",
                         (data.get('code'), data.get('name'), data.get('account_type'), data.get('account_group'), data.get('normal_balance'), self._oid(data.get('parent_id')), 1 if data.get('is_active', 1) else 0, data.get('description'), aid, tenant_id))
        return self._json({'ok': True})

    def api_bills_list(self, tenant_id, sess=None):
        with db() as conn:
            rows = q(conn, """SELECT b.*, o.pnr FROM bills b LEFT JOIN orders o ON o.id=b.order_id WHERE b.tenant_id=? ORDER BY b.id DESC LIMIT 500""", (tenant_id,))
            pays = q(conn, "SELECT bill_id,COUNT(*) c FROM bill_payments WHERE tenant_id=? GROUP BY bill_id", (tenant_id,))
        pay_map = {p['bill_id']: p['c'] for p in pays}
        for row in rows:
            row['payments_count'] = pay_map.get(row['id'], 0)
        return self._json({'bills': rows})

    def api_bill_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        supplier = (data.get('supplier') or '').strip()
        if not supplier:
            return self._json({'error': 'supplier required'}, 400)
        subtotal = float(data.get('subtotal') or 0)
        tax = float(data.get('tax') or 0)
        total = float(data.get('total') or subtotal + tax)
        ts = now_iso()
        with db() as conn:
            n = q(conn, "SELECT COUNT(*) c FROM bills WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            bill_no = data.get('bill_no') or f"BILL-{ts[:4]}-{n:05d}"
            cur = conn.execute("""INSERT INTO bills(tenant_id,branch_id,bill_no,supplier,description,order_id,account_code,category,currency,subtotal,tax,total,amount_paid,balance,bill_date,due_date,status,created_by,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('branch_id'), bill_no, supplier, data.get('description'), self._oid(data.get('order_id')), data.get('account_code') or '1150', data.get('category') or 'supplier', data.get('currency') or 'USD', subtotal, tax, total, total, data.get('bill_date') or ts[:10], data.get('due_date'), 'open', user_id, ts, ts))
            bid = cur.lastrowid
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'bill', bid, ts[:10], f"Bill {bill_no} - {supplier}", data.get('account_code') or '1150', 'Accounts Payable', total, data.get('currency') or 'USD', ts))
            log_audit(conn, tenant_id, user_id, 'create', 'bill', bid, {'bill_no': bill_no})
        return self._json({'ok': True, 'bill_id': bid, 'bill_no': bill_no})

    def api_bill_pay(self, tenant_id, user_id, bid, sess=None):
        data = read_json(self)
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        ts = now_iso()
        with db() as conn:
            bill = q(conn, "SELECT * FROM bills WHERE id=? AND tenant_id=?", (self._oid(bid), tenant_id), one=True)
            if not bill:
                return self._json({'error': 'Bill not found'}, 404)
            applied = min(amount, float(bill.get('balance') or 0))
            n = q(conn, "SELECT COUNT(*) c FROM bill_payments WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            pno = data.get('payment_no') or f"BPMT-{ts[:4]}-{n:05d}"
            conn.execute("""INSERT INTO bill_payments(tenant_id,bill_id,payment_no,amount,method,reference,paid_at,created_by,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, bill['id'], pno, applied, data.get('method') or 'bank', data.get('reference'), ts[:10], user_id, ts))
            amount_paid = float(bill.get('amount_paid') or 0) + applied
            balance = max(0, float(bill.get('total') or 0) - amount_paid)
            conn.execute("UPDATE bills SET amount_paid=?,balance=?,status=?,updated_at=? WHERE id=? AND tenant_id=?",
                         (amount_paid, balance, 'paid' if balance <= 0 else 'partial', ts, bill['id'], tenant_id))
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'bill_payment', bill['id'], ts[:10], f"Bill payment {pno}", 'Accounts Payable', 'Petty Cash', applied, bill.get('currency') or 'USD', ts))
            log_audit(conn, tenant_id, user_id, 'pay', 'bill', bill['id'], {'amount': applied, 'payment_no': pno})
        return self._json({'ok': True, 'payment_no': pno, 'applied': applied, 'balance': balance})

    def _delete_order_tree(self, conn, tenant_id, oid):
        invoice_ids = [r['id'] for r in q(conn, "SELECT id FROM invoices WHERE tenant_id=? AND order_id=?", (tenant_id, oid))]
        for iid in invoice_ids:
            conn.execute("DELETE FROM allocations WHERE tenant_id=? AND invoice_id=?", (tenant_id, iid))
            conn.execute("DELETE FROM invoice_lines WHERE tenant_id=? AND invoice_id=?", (tenant_id, iid))
            conn.execute("DELETE FROM payments WHERE tenant_id=? AND invoice_id=?", (tenant_id, iid))
            conn.execute("DELETE FROM credit_notes WHERE tenant_id=? AND invoice_id=?", (tenant_id, iid))
        conn.execute("DELETE FROM payments WHERE tenant_id=? AND order_id=?", (tenant_id, oid))
        for table in ['order_status_history','segments','order_passengers','order_services','tasks','supplier_bills','commissions']:
            conn.execute(f"DELETE FROM {table} WHERE tenant_id=? AND order_id=?", (tenant_id, oid))
        conn.execute("DELETE FROM invoices WHERE tenant_id=? AND order_id=?", (tenant_id, oid))
        bill_ids = [r['id'] for r in q(conn, "SELECT id FROM bills WHERE tenant_id=? AND order_id=?", (tenant_id, oid))]
        for bid in bill_ids:
            conn.execute("DELETE FROM bill_payments WHERE tenant_id=? AND bill_id=?", (tenant_id, bid))
        conn.execute("DELETE FROM bills WHERE tenant_id=? AND order_id=?", (tenant_id, oid))

    def api_bill_mutate(self, tenant_id, bid, method, sess=None):
        bid = self._oid(bid)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("DELETE FROM bill_payments WHERE tenant_id=? AND bill_id=?", (tenant_id, bid))
                conn.execute("DELETE FROM bills WHERE id=? AND tenant_id=?", (bid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            total = float(data.get('total') or 0)
            bill = q(conn, "SELECT * FROM bills WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not bill:
                return self._json({'error': 'Not found'}, 404)
            conn.execute("""UPDATE bills SET supplier=?,description=?,account_code=?,category=?,currency=?,subtotal=?,tax=?,total=?,balance=?,bill_date=?,due_date=?,status=?,updated_at=? WHERE id=? AND tenant_id=?""",
                         (data.get('supplier') or bill['supplier'], data.get('description'), data.get('account_code') or bill['account_code'], data.get('category') or bill['category'], data.get('currency') or bill['currency'], float(data.get('subtotal') or 0), float(data.get('tax') or 0), total, max(0, total - float(bill.get('amount_paid') or 0)), data.get('bill_date'), data.get('due_date'), data.get('status') or bill['status'], now_iso(), bid, tenant_id))
        return self._json({'ok': True})

    def api_expenses_list(self, tenant_id, sess=None):
        with db() as conn:
            rows = q(conn, "SELECT e.*, b.name branch_name FROM expenses e LEFT JOIN branches b ON b.id=e.branch_id WHERE e.tenant_id=? ORDER BY e.id DESC LIMIT 500", (tenant_id,))
        return self._json({'expenses': rows})

    def api_expense_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        ts = now_iso()
        with db() as conn:
            n = q(conn, "SELECT COUNT(*) c FROM expenses WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            exp_no = data.get('expense_no') or f"EXP-{ts[:4]}-{n:05d}"
            cur = conn.execute("""INSERT INTO expenses(tenant_id,branch_id,expense_no,category,vendor,description,account_code,amount,currency,expense_date,status,payment_method,reference,created_by,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('branch_id'), exp_no, data.get('category') or 'operating', data.get('vendor'), data.get('description'), data.get('account_code') or '1300', amount, data.get('currency') or 'USD', data.get('expense_date') or ts[:10], data.get('status') or 'posted', data.get('payment_method') or 'cash', data.get('reference'), user_id, ts, ts))
            eid = cur.lastrowid
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'expense', eid, ts[:10], f"Expense {exp_no}", data.get('account_code') or '1300', 'Petty Cash', amount, data.get('currency') or 'USD', ts))
            log_audit(conn, tenant_id, user_id, 'create', 'expense', eid, {'expense_no': exp_no})
        return self._json({'ok': True, 'expense_id': eid, 'expense_no': exp_no})

    def api_expense_mutate(self, tenant_id, eid, method, sess=None):
        eid = self._oid(eid)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("DELETE FROM expenses WHERE id=? AND tenant_id=?", (eid, tenant_id))
                conn.execute("DELETE FROM journals WHERE tenant_id=? AND ref_type='expense' AND ref_id=?", (tenant_id, eid))
                return self._json({'ok': True})
            data = read_json(self)
            conn.execute("""UPDATE expenses SET category=?,vendor=?,description=?,account_code=?,amount=?,currency=?,expense_date=?,status=?,payment_method=?,reference=?,updated_at=? WHERE id=? AND tenant_id=?""",
                         (data.get('category'), data.get('vendor'), data.get('description'), data.get('account_code'), float(data.get('amount') or 0), data.get('currency'), data.get('expense_date'), data.get('status'), data.get('payment_method'), data.get('reference'), now_iso(), eid, tenant_id))
        return self._json({'ok': True})

    def api_ledger(self, tenant_id, qs, sess=None):
        account = (qs.get('account') or [''])[0]
        sql = "SELECT * FROM journals WHERE tenant_id=?"
        args = [tenant_id]
        if account:
            sql += " AND (debit_account LIKE ? OR credit_account LIKE ?)"; args += [f'%{account}%', f'%{account}%']
        sql += " ORDER BY id DESC LIMIT 500"
        with db() as conn:
            rows = q(conn, sql, args)
        return self._json({'journals': rows})

    def api_trial_balance(self, tenant_id, sess=None):
        with db() as conn:
            rows = q(conn, """SELECT account, SUM(debit) debit, SUM(credit) credit FROM (
                              SELECT debit_account account, SUM(amount) debit, 0 credit FROM journals WHERE tenant_id=? GROUP BY debit_account
                              UNION ALL
                              SELECT credit_account account, 0 debit, SUM(amount) credit FROM journals WHERE tenant_id=? GROUP BY credit_account)
                              GROUP BY account ORDER BY account""", (tenant_id, tenant_id))
        return self._json({'rows': rows})

    def api_manual_journal(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        amount = float(data.get('amount') or 0)
        if amount <= 0 or not data.get('debit_account') or not data.get('credit_account'):
            return self._json({'error': 'debit_account, credit_account and amount required'}, 400)
        ts = now_iso()
        with db() as conn:
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'manual', None, data.get('entry_date') or ts[:10], data.get('memo') or 'Manual journal', data.get('debit_account'), data.get('credit_account'), amount, data.get('currency') or 'USD', ts))
            log_audit(conn, tenant_id, user_id, 'create', 'journal', None, {'amount': amount})
        return self._json({'ok': True})

    def api_order_mutate(self, tenant_id, user_id, oid, method):
        oid = self._oid(oid)
        with db() as conn:
            order = q(conn, "SELECT * FROM orders WHERE id=? AND tenant_id=?", (oid, tenant_id), one=True)
            if not order:
                return self._json({'error': 'Not found'}, 404)
            if method == 'DELETE':
                self._delete_order_tree(conn, tenant_id, oid)
                conn.execute("DELETE FROM orders WHERE id=? AND tenant_id=?", (oid, tenant_id))
                log_audit(conn, tenant_id, user_id, 'delete', 'order', oid, {})
                return self._json({'ok': True})
            data = read_json(self)
            sell = float(data.get('total_sell') if data.get('total_sell') is not None else order.get('total_sell') or 0)
            cost = float(data.get('total_cost') if data.get('total_cost') is not None else order.get('total_cost') or 0)
            conn.execute("""UPDATE orders SET pnr=?,order_number=?,source_type=?,product_type=?,status=?,ticket_status=?,tktl=?,travel_start=?,travel_end=?,origin=?,destination=?,currency=?,total_sell=?,total_cost=?,margin=?,notes=?,updated_at=? WHERE id=? AND tenant_id=?""",
                         (data.get('pnr') or order['pnr'], data.get('order_number') or order['order_number'], data.get('source_type') or order['source_type'], data.get('product_type') or order['product_type'] or 'Flight', data.get('status') or order['status'], data.get('ticket_status') or order['ticket_status'], data.get('tktl') if data.get('tktl') is not None else order['tktl'], data.get('travel_start') if data.get('travel_start') is not None else order['travel_start'], data.get('travel_end') if data.get('travel_end') is not None else order['travel_end'], data.get('origin') or order['origin'], data.get('destination') or order['destination'], data.get('currency') or order['currency'], sell, cost, sell - cost, data.get('notes') if data.get('notes') is not None else order['notes'], now_iso(), oid, tenant_id))
            log_audit(conn, tenant_id, user_id, 'update', 'order', oid, {})
        return self._json({'ok': True})

    def api_order_item_create(self, tenant_id, oid):
        data = read_json(self)
        oid = self._oid(oid)
        with db() as conn:
            exists = q(conn, "SELECT 1 FROM orders WHERE id=? AND tenant_id=?", (oid, tenant_id), one=True)
            if not exists:
                return self._json({'error': 'Not found'}, 404)
            cur = conn.execute("INSERT INTO order_services(tenant_id,order_id,kind,code,text,raw_json) VALUES (?,?,?,?,?,?)",
                               (tenant_id, oid, data.get('kind') or 'service', data.get('code'), data.get('text') or data.get('description'), json.dumps(data.get('meta') or {}, default=str)))
        return self._json({'ok': True, 'item_id': cur.lastrowid})

    def api_order_item_mutate(self, tenant_id, iid, method):
        iid = self._oid(iid)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("DELETE FROM order_services WHERE id=? AND tenant_id=?", (iid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            conn.execute("UPDATE order_services SET kind=?,code=?,text=? WHERE id=? AND tenant_id=?",
                         (data.get('kind'), data.get('code'), data.get('text'), iid, tenant_id))
        return self._json({'ok': True})

    def api_customer_mutate(self, tenant_id, cid, method, sess=None):
        cid = self._oid(cid)
        with db() as conn:
            agency_id = (sess or {}).get('agency_id')
            if agency_id:
                own = q(conn, "SELECT 1 FROM customers WHERE id=? AND tenant_id=? AND agency_id=?", (cid, tenant_id, agency_id), one=True)
                if not own:
                    return self._json({'error': 'Customer not found'}, 404)
            if method == 'DELETE':
                orders = q(conn, "SELECT COUNT(*) c FROM orders WHERE customer_id=? AND tenant_id=?", (cid, tenant_id), one=True)['c']
                invoices = q(conn, "SELECT COUNT(*) c FROM invoices WHERE customer_id=? AND tenant_id=?", (cid, tenant_id), one=True)['c']
                if orders or invoices:
                    return self._json({'error': 'Customer has bookings or invoices and cannot be deleted'}, 400)
                conn.execute("DELETE FROM customers WHERE id=? AND tenant_id=?", (cid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            conn.execute("UPDATE customers SET type=?,name=?,company=?,email=?,phone=?,loyalty_id=?,notes=? WHERE id=? AND tenant_id=?",
                         (data.get('type'), data.get('name'), data.get('company'), data.get('email'), data.get('phone'), data.get('loyalty_id'), data.get('notes'), cid, tenant_id))
        return self._json({'ok': True})

    def api_raw_docs(self, tenant_id):
        with db() as conn:
            rows = q(conn, "SELECT id, connector_id, source_type, source_ref, filename, received_at, length(content_text) size, meta_json FROM raw_documents WHERE tenant_id=? ORDER BY id DESC LIMIT 200", (tenant_id,))
        return self._json({'documents': rows})


    def _activity(self, conn, tenant_id, entity_type, entity_id, action, detail=None, actor=None):
        conn.execute("""INSERT INTO document_activity(tenant_id,entity_type,entity_id,action,detail,actor,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (tenant_id, entity_type, entity_id, action, detail, str(actor or 'system'), now_iso()))

    def _entity_context(self, conn, tenant_id, entity_type, entity_id, date_from=None, date_to=None):
        eid = self._oid(entity_id)
        if not eid:
            return {}
        if entity_type in ('booking', 'order'):
            order = q(conn, """SELECT o.*, c.name customer_name, c.email customer_email, c.phone customer_phone
                              FROM orders o LEFT JOIN customers c ON c.id=o.customer_id
                              WHERE o.tenant_id=? AND o.id=?""", (tenant_id, eid), one=True)
            if not order:
                return {}
            return {'order': order,
                    'customer': {'name': order.get('customer_name'), 'email': order.get('customer_email'), 'phone': order.get('customer_phone')},
                    'segments': q(conn, "SELECT carrier,flight_no,origin,destination,depart_at,arrive_at FROM segments WHERE order_id=? ORDER BY id", (eid,)),
                    'passengers': q(conn, "SELECT full_name,traveler_type,ticket_numbers FROM order_passengers WHERE order_id=? ORDER BY id", (eid,)),
                    'invoices': q(conn, "SELECT invoice_no,issue_date,total,amount_paid,balance FROM invoices WHERE order_id=? ORDER BY id DESC", (eid,))}
        if entity_type == 'invoice':
            inv = q(conn, """SELECT i.*, c.name customer_name, c.email customer_email, o.pnr
                             FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id
                             LEFT JOIN orders o ON o.id=i.order_id
                             WHERE i.tenant_id=? AND i.id=?""", (tenant_id, eid), one=True)
            if not inv:
                return {}
            return {'invoice': inv, 'customer': {'name': inv.get('customer_name'), 'email': inv.get('customer_email')},
                    'order': {'pnr': inv.get('pnr')},
                    'lines': q(conn, "SELECT description,qty,unit_price,line_total FROM invoice_lines WHERE invoice_id=? ORDER BY id", (eid,))}
        if entity_type == 'bill':
            bill = q(conn, "SELECT * FROM bills WHERE tenant_id=? AND id=?", (tenant_id, eid), one=True)
            return {'bill': bill} if bill else {}
        if entity_type == 'traveler':
            return self._traveler_statement_context(tenant_id, eid, date_from, date_to)
        if entity_type == 'payment':
            pay = q(conn, """SELECT p.*, i.invoice_no, i.total invoice_total, i.balance invoice_balance,
                                    c.name customer_name, c.email customer_email, o.pnr
                             FROM payments p LEFT JOIN invoices i ON i.id=p.invoice_id
                             LEFT JOIN customers c ON c.id=p.customer_id
                             LEFT JOIN orders o ON o.id=p.order_id
                             WHERE p.tenant_id=? AND p.id=?""", (tenant_id, eid), one=True)
            if not pay:
                return {}
            return {'payment': pay,
                    'invoice': {'invoice_no': pay.get('invoice_no'), 'total': pay.get('invoice_total'), 'balance': pay.get('invoice_balance')},
                    'customer': {'name': pay.get('customer_name'), 'email': pay.get('customer_email')},
                    'order': {'pnr': pay.get('pnr')}}
        if entity_type == 'customer':
            cust = q(conn, """SELECT c.*, (SELECT COALESCE(SUM(total_sell),0) FROM orders o WHERE o.customer_id=c.id) spend
                              FROM customers c WHERE c.tenant_id=? AND c.id=?""", (tenant_id, eid), one=True)
            if not cust:
                return {}
            return {'customer': cust,
                    'orders': q(conn, "SELECT pnr,origin,destination,travel_start,total_sell,ticket_status FROM orders WHERE tenant_id=? AND customer_id=? ORDER BY id DESC", (tenant_id, eid)),
                    'invoices': q(conn, "SELECT invoice_no,issue_date,total,amount_paid,balance FROM invoices WHERE tenant_id=? AND customer_id=? ORDER BY id DESC", (tenant_id, eid)),
                    'documents': q(conn, "SELECT title,category,file_name FROM documents WHERE tenant_id=? AND entity_type='customer' AND entity_id=? ORDER BY id DESC", (tenant_id, eid))}
        if entity_type == 'estimate':
            est = q(conn, """SELECT e.*, c.name customer_name, c.email customer_email FROM estimates e
                             LEFT JOIN customers c ON c.id=e.customer_id WHERE e.tenant_id=? AND e.id=?""", (tenant_id, eid), one=True)
            if not est:
                return {}
            return {'estimate': est, 'customer': {'name': est.get('customer_name'), 'email': est.get('customer_email')},
                    'lines': [{'description': est.get('title') or 'Travel services', 'qty': 1,
                               'unit_price': est.get('subtotal') or est.get('total') or 0, 'line_total': est.get('total') or 0}]}
        if entity_type == 'credit_note':
            note = q(conn, """SELECT n.*, c.name customer_name, i.invoice_no FROM credit_notes n
                              LEFT JOIN customers c ON c.id=n.customer_id LEFT JOIN invoices i ON i.id=n.invoice_id
                              WHERE n.tenant_id=? AND n.id=?""", (tenant_id, eid), one=True)
            if not note:
                return {}
            return {'credit_note': note, 'customer': {'name': note.get('customer_name')}, 'invoice': {'invoice_no': note.get('invoice_no')}}
        if entity_type in ('pss_booking', 'eticket', 'journeypass'):
            b = q(conn, """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.gate, f.terminal, f.aircraft,
                                  c.name channel_name FROM pss_bookings b
                           LEFT JOIN pss_flights f ON f.id=b.flight_id LEFT JOIN pss_channels c ON c.id=b.channel_id
                           WHERE b.tenant_id=? AND b.id=?""", (tenant_id, eid), one=True)
            if not b:
                return {}
            airports = [a for a in [b.get('origin'), b.get('destination')] if a]
            marks = ','.join(['?'] * len(airports)) if airports else "''"
            adargs = [tenant_id] + airports
            return {'pss_booking': b, 'booking': b,
                    'payments': q(conn, "SELECT * FROM pss_payments WHERE tenant_id=? AND booking_id=? ORDER BY id DESC", (tenant_id, eid)),
                    'checkins': q(conn, "SELECT * FROM pss_checkins WHERE tenant_id=? AND booking_id=? ORDER BY id DESC", (tenant_id, eid)),
                    'ancillaries': q(conn, "SELECT sale_no,description,quantity,amount,currency,status FROM pss_ancillary_sales WHERE tenant_id=? AND booking_id=? ORDER BY id DESC", (tenant_id, eid)),
                    'tax_lines': q(conn, "SELECT tax_code,description,amount,currency FROM pss_tax_lines WHERE tenant_id=? AND booking_id=? ORDER BY id", (tenant_id, eid)),
                    'baggage': q(conn, "SELECT cabin_class,free_kg,excess_rate_per_kg,max_kg FROM pss_baggage_rules WHERE tenant_id=? AND (flight_id=? OR flight_id IS NULL) ORDER BY flight_id DESC LIMIT 1", (tenant_id, b.get('flight_id'))),
                    'airport_docs': q(conn, f"SELECT airport_code,doc_type,title,requirement FROM pss_airport_docs WHERE tenant_id=? AND airport_code IN ({marks}) AND status='active' ORDER BY airport_code", adargs) if airports else [],
                    'weather': q(conn, f"SELECT airport_code,condition,temperature_c,wind_kph,advisory FROM pss_weather WHERE tenant_id=? AND airport_code IN ({marks}) ORDER BY airport_code", adargs) if airports else []}
        if entity_type in ('pss_checkin', 'boardingpass'):
            ci = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.gate, f.terminal
                             FROM pss_checkins ci LEFT JOIN pss_flights f ON f.id=ci.flight_id
                             WHERE ci.tenant_id=? AND ci.id=?""", (tenant_id, eid), one=True)
            if not ci:
                return {}
            b = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND id=?", (tenant_id, ci['booking_id']), one=True) or {}
            return {'checkin': ci, 'pss_checkin': ci, 'pss_booking': b, 'booking': b}
        return {}

    def api_pdf_document(self, kind, oid, qs):
        allowed = {'booking': 'booking', 'order': 'booking', 'invoice': 'invoice', 'customer': 'customer',
                   'bill': 'bill', 'traveler': 'traveler', 'payment': 'payment', 'receipt': 'payment',
                   'estimate': 'estimate', 'credit_note': 'credit_note',
                   'eticket': 'pss_booking', 'boardingpass': 'pss_checkin', 'journeypass': 'pss_booking'}
        if kind not in allowed:
            return self._json({'error': 'Unsupported document type'}, 400)
        entity_type = allowed[kind]
        with db() as conn:
            token = (qs.get('token') or [''])[0]
            if token:
                share = q(conn, "SELECT * FROM document_shares WHERE token=? AND status='active'", (token,), one=True)
                if share:
                    setype = str(share.get('entity_type') or '')
                    valid = (setype == entity_type or
                             (entity_type == 'booking' and setype == 'order') or
                             (entity_type == 'pss_booking' and setype in ('pss_booking', 'eticket', 'journeypass')) or
                             (entity_type == 'pss_checkin' and setype in ('pss_checkin', 'boardingpass')))
                    if not valid or str(share.get('entity_id')) != str(oid):
                        return self._json({'error': 'Invalid share token'}, 403)
                    tenant_id = share['tenant_id']
                else:
                    offer = q(conn, "SELECT * FROM pss_eticket_directs WHERE token=? AND status='active'", (token,), one=True)
                    booking = q(conn, "SELECT id,offer_id FROM pss_bookings WHERE id=?", (self._oid(oid),), one=True)
                    if not offer or not booking or str(booking.get('offer_id')) != str(offer['id']) or entity_type != 'pss_booking':
                        return self._json({'error': 'Invalid share token'}, 403)
                    tenant_id = offer['tenant_id']
            else:
                sess = require_auth(self)
                if not sess:
                    return self._json({'error': 'Unauthorized'}, 401)
                tenant_id = sess['tenant_id']
            date_from = (qs.get('from') or qs.get('date_from') or [''])[0]
            date_to = (qs.get('to') or qs.get('date_to') or [''])[0]
            context = self._entity_context(conn, tenant_id, entity_type, oid, date_from, date_to)
            if not context:
                return self._json({'error': 'Document not found'}, 404)
            branding = self._branding(conn, tenant_id)
        pdf_kind = 'booking' if kind == 'order' else kind
        with db() as conn:
            ref = self._psn('DOC')
            subject = context.get('pss_booking') or context.get('checkin') or context.get('invoice') or {}
            self._doc_audit(conn, tenant_id, pdf_kind, ref, subject.get('id'), subject.get('pnr'),
                            'document_generated', sess.get('username') if sess else 'client',
                            subject.get('trace_id'), {'kind': pdf_kind})
        pdf = build_pdf(pdf_kind, context, branding)
        name = f"{kind}-{oid}.pdf"
        return self._send(pdf, 200, 'application/pdf', {'Content-Disposition': f'inline; filename="{name}"'})

    def api_public_share(self, token):
        with db() as conn:
            share = q(conn, "SELECT * FROM document_shares WHERE token=? AND status='active'", (token,), one=True)
            if not share:
                return self._json({'error': 'Share link not found or revoked'}, 404)
            if share.get('expires_at') and str(share['expires_at']) < now_iso():
                return self._json({'error': 'Share link expired'}, 410)
            conn.execute("UPDATE document_shares SET views=COALESCE(views,0)+1, last_viewed_at=? WHERE id=?", (now_iso(), share['id']))
            self._activity(conn, share['tenant_id'], share['entity_type'], share['entity_id'], 'share_viewed', f"token={token}", 'client')
            context = self._entity_context(conn, share['tenant_id'], share['entity_type'], share['entity_id'])
            branding = self._branding(conn, share['tenant_id'])
            activity = q(conn, """SELECT action,detail,actor,created_at FROM document_activity
                                  WHERE tenant_id=? AND entity_type=? AND entity_id=? ORDER BY id DESC LIMIT 20""",
                         (share['tenant_id'], share['entity_type'], share['entity_id']))
        return self._json({'share': {'token': token, 'recipient': share.get('recipient'),
                                     'permission': share.get('permission'), 'views': (share.get('views') or 0) + 1,
                                     'created_at': share.get('created_at')},
                           'branding': branding, 'context': context, 'activity': activity,
                           'entity_type': share['entity_type'], 'entity_id': share['entity_id']})

    def api_documents_list(self, tenant_id, qs):
        with db() as conn:
            rows = q(conn, """SELECT id,entity_type,entity_id,title,category,file_name,mime_type,size,visibility,created_at
                              FROM documents WHERE tenant_id=? ORDER BY id DESC LIMIT 500""", (tenant_id,))
        return self._json({'documents': rows})

    def api_document_create(self, tenant_id, user_id):
        data = read_json(self)
        file_data = None
        if data.get('file_base64'):
            raw = data['file_base64']
            if isinstance(raw, str) and raw.startswith('data:'):
                raw = raw.split(',', 1)[-1]
            try:
                file_data = base64.b64decode(raw)
            except Exception:
                file_data = None
        ts = now_iso()
        with db() as conn:
            cur = conn.execute("""INSERT INTO documents(tenant_id,entity_type,entity_id,title,category,file_name,mime_type,file_data,content_text,size,visibility,uploaded_by,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('entity_type') or 'general', self._oid(data.get('entity_id')),
                                data.get('title') or 'Untitled document', data.get('category') or 'general',
                                data.get('file_name'), data.get('mime_type'), file_data, data.get('content_text'),
                                int(data.get('size') or (len(file_data) if file_data else 0)),
                                data.get('visibility') or 'internal', user_id, ts, ts))
            did = cur.lastrowid
            self._activity(conn, tenant_id, data.get('entity_type') or 'general', self._oid(data.get('entity_id')),
                           'document_uploaded', data.get('title') or '', user_id)
            log_audit(conn, tenant_id, user_id, 'upload', 'document', did, {'title': data.get('title')})
        return self._json({'ok': True, 'document_id': did})

    def api_document_download(self, tenant_id, did):
        did = self._oid(did)
        with db() as conn:
            row = q(conn, "SELECT * FROM documents WHERE id=? AND tenant_id=?", (did, tenant_id), one=True)
        if not row:
            return self._json({'error': 'Document not found'}, 404)
        payload = row.get('file_data') if row.get('file_data') else (row.get('content_text') or '').encode('utf-8')
        fname = (row.get('file_name') or (row.get('title') or 'document')).replace('"', '')
        return self._send(payload, 200, row.get('mime_type') or 'application/octet-stream',
                          {'Content-Disposition': f'attachment; filename="{fname}"'})

    def api_document_mutate(self, tenant_id, did, method):
        did = self._oid(did)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("DELETE FROM documents WHERE id=? AND tenant_id=?", (did, tenant_id))
                conn.execute("DELETE FROM document_shares WHERE document_id=? AND tenant_id=?", (did, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            conn.execute("UPDATE documents SET title=?,category=?,visibility=?,updated_at=? WHERE id=? AND tenant_id=?",
                         (data.get('title'), data.get('category'), data.get('visibility') or 'internal', now_iso(), did, tenant_id))
        return self._json({'ok': True})

    def api_shares_list(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT s.*, d.title document_title, d.file_name FROM document_shares s
                              LEFT JOIN documents d ON d.id=s.document_id
                              WHERE s.tenant_id=? ORDER BY s.id DESC LIMIT 200""", (tenant_id,))
        for row in rows:
            row['url'] = f"/share/{row['token']}"
        return self._json({'shares': rows})

    def api_share_create(self, tenant_id, user_id):
        data = read_json(self)
        entity_type = (data.get('entity_type') or 'booking').strip()
        entity_id = self._oid(data.get('entity_id'))
        if not entity_id:
            return self._json({'error': 'entity_id required'}, 400)
        token = make_token()
        with db() as conn:
            conn.execute("""INSERT INTO document_shares(tenant_id,document_id,entity_type,entity_id,token,recipient,permission,status,views,expires_at,created_by,created_at)
                            VALUES (?,?,?,?,?,?,?,'active',0,?,?,?)""",
                         (tenant_id, self._oid(data.get('document_id')), entity_type, entity_id, token,
                          data.get('recipient'), data.get('permission') or 'view', data.get('expires_at'), user_id, now_iso()))
            self._activity(conn, tenant_id, entity_type, entity_id, 'share_created',
                           f"recipient={data.get('recipient') or 'client'}", user_id)
            log_audit(conn, tenant_id, user_id, 'share', entity_type, entity_id, {'token': token})
        return self._json({'ok': True, 'token': token, 'url': f'/share/{token}'})

    def api_share_delete(self, tenant_id, sid):
        sid = self._oid(sid)
        with db() as conn:
            conn.execute("DELETE FROM document_shares WHERE id=? AND tenant_id=?", (sid, tenant_id))
        return self._json({'ok': True})

    def api_branding_get(self, tenant_id):
        with db() as conn:
            branding = self._branding(conn, tenant_id)
        branding['logo_data_set'] = bool(branding.get('logo_data'))
        return self._json({'branding': branding})

    def api_branding_save(self, tenant_id, user_id):
        data = read_json(self)
        with db() as conn:
            current = q(conn, "SELECT * FROM tenant_branding WHERE tenant_id=?", (tenant_id,), one=True) or {}
            logo_data = data.get('logo_data') if 'logo_data' in data else current.get('logo_data')
            logo_data = logo_data or ''
            def pick(key):
                if key in data and data.get(key) not in (None, ''):
                    return data.get(key)
                return current.get(key) or ''
            company_name = pick('company_name')
            legal_name = pick('legal_name')
            location = pick('location')
            address = pick('address')
            phone = pick('phone')
            email = pick('email')
            website = pick('website')
            tax_id = pick('tax_id')
            logo_text = pick('logo_text') or (company_name[:3].upper() if company_name else 'TE')
            primary_color = data.get('primary_color') or current.get('primary_color') or '#0f2742'
            accent_color = data.get('accent_color') or current.get('accent_color') or '#0b6efd'
            footer_note = data.get('footer_note') if 'footer_note' in data else (current.get('footer_note') or 'Generated by Travel ERP - Confidential')
            ts = now_iso()
            values = (tenant_id, company_name, legal_name, location, address, phone, email, website, tax_id,
                      logo_text, logo_data, primary_color, accent_color, footer_note, ts)
            conn.execute("""INSERT INTO tenant_branding(tenant_id,company_name,legal_name,location,address,phone,email,website,tax_id,logo_text,logo_data,primary_color,accent_color,footer_note,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(tenant_id) DO UPDATE SET company_name=excluded.company_name, legal_name=excluded.legal_name,
                            location=excluded.location, address=excluded.address, phone=excluded.phone, email=excluded.email,
                            website=excluded.website, tax_id=excluded.tax_id, logo_text=excluded.logo_text,
                            logo_data=excluded.logo_data, primary_color=excluded.primary_color, accent_color=excluded.accent_color,
                            footer_note=excluded.footer_note, updated_at=excluded.updated_at""", values)
            if company_name:
                conn.execute("UPDATE tenants SET name=? WHERE id=?", (company_name, tenant_id))
            prof = q(conn, "SELECT 1 FROM agency_profile WHERE tenant_id=?", (tenant_id,), one=True)
            if prof:
                conn.execute("""UPDATE agency_profile SET company_name=COALESCE(NULLIF(?,''),company_name),
                                legal_name=COALESCE(NULLIF(?,''),legal_name), address=COALESCE(NULLIF(?,''),address),
                                phone=COALESCE(NULLIF(?,''),phone), email=COALESCE(NULLIF(?,''),email),
                                website=COALESCE(NULLIF(?,''),website), tax_id=COALESCE(NULLIF(?,''),tax_id),
                                updated_at=? WHERE tenant_id=?""",
                             (company_name, legal_name, address, phone, email, website, tax_id, ts, tenant_id))
            log_audit(conn, tenant_id, user_id, 'save', 'branding', tenant_id, {})
        return self._json({'ok': True})


    # ---------------- Airline PSS ----------------
    def _iata_name(self, name, title=None):
        value = str(name or '').strip().upper()
        if '/' in value:
            last, _, first = value.partition('/')
            first = first.strip()
            if title and title.upper() not in first:
                first = (first + ' ' + title.upper()).strip()
            return f"{last.strip()}/{first}".strip('/')
        parts = value.split()
        if len(parts) >= 2:
            last = parts[-1]
            first = ' '.join(parts[:-1])
            if title and title.upper() not in first:
                first = (first + ' ' + title.upper()).strip()
            return f"{last}/{first}"
        return value + (('/' + title.upper()) if title else '')

    def _seed_tax_lines(self, conn, tenant_id, bid, booking):
        taxes = float(booking.get('taxes') or 0)
        if taxes <= 0:
            return
        cur = booking.get('currency') or 'USD'
        lines = [('YQ', 'Carrier-imposed surcharge', round(taxes * 0.6, 2)),
                 ('BP', 'Passenger service charge', round(taxes * 0.4, 2))]
        for code, desc, amount in lines:
            conn.execute("""INSERT INTO pss_tax_lines(tenant_id,booking_id,tax_code,description,amount,currency,created_at)
                            VALUES (?,?,?,?,?,?,?)""", (tenant_id, bid, code, desc, amount, cur, now_iso()))

    def _doc_audit(self, conn, tenant_id, doc_type, doc_no, bid, pnr, action, actor, trace_id, details=None):
        conn.execute("""INSERT INTO pss_document_audit(tenant_id,document_type,document_no,booking_id,pnr,action,actor,trace_id,details_json,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (tenant_id, doc_type, doc_no, bid, pnr, action, str(actor or 'system'), trace_id,
                      json.dumps(details or {}, default=str), now_iso()))
        log_audit(conn, tenant_id, None, action, doc_type, bid, details or {})

    def _psn(self, prefix):
        return f"{prefix}-{os.urandom(3).hex().upper()}"

    def _pss_next_ticket(self, conn, tenant_id, channel_id=None, airport_code=None):
        allocations = q(conn, """SELECT a.*, p.prefix, p.status pool_status FROM pss_stock_allocations a
                                 JOIN pss_stock_pools p ON p.id=a.pool_id
                                 WHERE a.tenant_id=? AND a.status='active' AND p.status='active'
                                   AND a.next_serial<=a.end_serial ORDER BY a.id""", (tenant_id,))
        alloc = None
        if channel_id:
            alloc = next((a for a in allocations if a['channel_id'] == channel_id), None)
        if not alloc and airport_code:
            alloc = next((a for a in allocations if a['airport_code'] == airport_code), None)
        if not alloc:
            alloc = allocations[0] if allocations else None
        if not alloc:
            return None
        serial = int(alloc['next_serial'])
        ticket_no = f"{alloc['prefix']}{serial:08d}"
        conn.execute("UPDATE pss_stock_allocations SET next_serial=?, updated_at=? WHERE id=?", (serial + 1, now_iso(), alloc['id']))
        conn.execute("UPDATE pss_stock_pools SET issued_count=issued_count+1, updated_at=? WHERE id=?", (now_iso(), alloc['pool_id']))
        return ticket_no

    def api_pss_overview(self, tenant_id):
        with db() as conn:
            def one(sql, args=()):
                return q(conn, sql, args, one=True) or {}
            flights = one("SELECT COUNT(*) c FROM pss_flights WHERE tenant_id=?", (tenant_id,)).get('c', 0)
            seats = one("SELECT COALESCE(SUM(available_seats),0) v FROM pss_flights WHERE tenant_id=? AND status='scheduled'", (tenant_id,)).get('v', 0)
            bookings = one("SELECT COUNT(*) c FROM pss_bookings WHERE tenant_id=?", (tenant_id,)).get('c', 0)
            unissued = one("SELECT COUNT(*) c FROM pss_bookings WHERE tenant_id=? AND ticket_status='unissued'", (tenant_id,)).get('c', 0)
            checked_in = one("SELECT COUNT(*) c FROM pss_bookings WHERE tenant_id=? AND booking_status='checked_in'", (tenant_id,)).get('c', 0)
            pending_pay = one("SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE tenant_id=? AND status='pending'", (tenant_id,)).get('v', 0)
            captured = one("SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE tenant_id=? AND status='captured'", (tenant_id,)).get('v', 0)
            stock = one("SELECT COALESCE(SUM(end_serial-next_serial+1),0) v FROM pss_stock_allocations WHERE tenant_id=? AND status='active'", (tenant_id,)).get('v', 0)
            low_stock = q(conn, """SELECT p.*, (p.end_serial-p.next_serial+1) remaining FROM pss_stock_pools p
                                   WHERE p.tenant_id=? AND p.status='active' AND (p.end_serial-p.next_serial+1)<=1000 ORDER BY remaining""", (tenant_id,))
            channels = q(conn, """SELECT c.name, COUNT(b.id) bookings, COALESCE(SUM(b.total_amount),0) revenue
                                  FROM pss_channels c LEFT JOIN pss_bookings b ON b.channel_id=c.id AND b.tenant_id=c.tenant_id
                                  WHERE c.tenant_id=? GROUP BY c.id ORDER BY revenue DESC""", (tenant_id,))
            departures = q(conn, """SELECT flight_no, origin, destination, flight_date, dep_time, available_seats, status
                                    FROM pss_flights WHERE tenant_id=? ORDER BY flight_date, dep_time LIMIT 8""", (tenant_id,))
        return self._json({'kpis': {'flights': flights, 'seats': seats, 'bookings': bookings, 'unissued': unissued,
                                    'checked_in': checked_in, 'pending_payment': pending_pay, 'captured': captured,
                                    'stock_remaining': stock},
                           'low_stock': low_stock, 'channels': channels, 'departures': departures})

    def api_pss_flights(self, tenant_id, method, fid=None):
        with db() as conn:
            if method == 'GET' and not fid:
                rows = q(conn, "SELECT * FROM pss_flights WHERE tenant_id=? ORDER BY flight_date, dep_time LIMIT 500", (tenant_id,))
                return self._json({'flights': rows})
            fid = self._oid(fid)
            if method == 'GET':
                row = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id), one=True)
                if not row:
                    return self._json({'error': 'Flight not found'}, 404)
                bookings = q(conn, "SELECT * FROM pss_bookings WHERE flight_id=? AND tenant_id=? ORDER BY id DESC", (fid, tenant_id))
                return self._json({'flight': row, 'bookings': bookings})
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if data.get('status') == 'cancelled':
                conn.execute("UPDATE pss_flights SET status='cancelled', updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), fid, tenant_id))
                return self._json({'ok': True})
            fields = ['flight_no','origin','destination','flight_date','dep_time','arr_time','aircraft','total_seats','available_seats','base_fare','currency','gate','terminal','status']
            vals = [data.get(f) for f in fields]
            if fid:
                conn.execute("UPDATE pss_flights SET " + ','.join(f + '=?' for f in fields) + ",updated_at=? WHERE id=? AND tenant_id=?", vals + [now_iso(), fid, tenant_id])
            else:
                if not data.get('flight_no') or not data.get('origin') or not data.get('destination') or not data.get('flight_date'):
                    return self._json({'error': 'flight_no, origin, destination and flight_date are required'}, 400)
                vals = [tenant_id] + vals + [now_iso(), now_iso()]
                conn.execute("INSERT INTO pss_flights(tenant_id," + ','.join(fields) + ",created_at,updated_at) VALUES (" + ','.join(['?'] * (len(fields) + 3)) + ")", vals)
                fid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            log_audit(conn, tenant_id, None, 'save', 'pss_flight', fid, {})
        return self._json({'ok': True, 'flight_id': fid})

    def api_pss_bookings(self, tenant_id, method, bid=None, user_id=None, sess=None):
        with db() as conn:
            if method == 'GET' and not bid:
                sql = """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, c.name channel_name
                          FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                          LEFT JOIN pss_channels c ON c.id=b.channel_id WHERE b.tenant_id=?"""
                args = [tenant_id]
                sess = sess or {}
                if sess.get('agency_id'):
                    sql += " AND b.agency_id=?"; args.append(sess['agency_id'])
                elif sess.get('station_code'):
                    sql += " AND (b.airport_code=? OR f.origin=? OR f.destination=?)"
                    args += [sess['station_code'], sess['station_code'], sess['station_code']]
                sql += " ORDER BY b.id DESC LIMIT 500"
                rows = q(conn, sql, args)
                return self._json({'bookings': rows})
            bid = self._oid(bid)
            if (sess or {}).get('agency_id'):
                own = q(conn, "SELECT 1 FROM pss_bookings WHERE id=? AND tenant_id=? AND agency_id=?", (bid, tenant_id, sess['agency_id']), one=True)
                if not own:
                    return self._json({'error': 'Booking not found'}, 404)
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if method == 'PUT':
                fields = ['passenger_name','passenger_type','contact_email','contact_phone','fare_class','fare_amount','taxes',
                          'total_amount','booking_status','payment_status','ticket_status','seat_no','airport_code','channel_id',
                          'fare_basis','service_fee','commission','baggage_allowance_kg','doc_no','issue_date','issued_by','currency']
                vals = []
                for f in fields:
                    v = data.get(f)
                    if f in ('channel_id',) and v in ('', None):
                        vals.append(None)
                    else:
                        vals.append(v)
                fare = float(data.get('fare_amount') or 0)
                taxes = float(data.get('taxes') or 0)
                if data.get('total_amount') in ('', None):
                    vals[fields.index('total_amount')] = fare + taxes
                conn.execute("UPDATE pss_bookings SET " + ','.join(f + '=?' for f in fields) + ",updated_at=? WHERE id=? AND tenant_id=?",
                             vals + [now_iso(), bid, tenant_id])
                return self._json({'ok': True})
            fid = self._oid(data.get('flight_id'))
            flight = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id), one=True)
            if not flight:
                return self._json({'error': 'Flight not found'}, 400)
            reserved = conn.execute("UPDATE pss_flights SET available_seats=available_seats-1, updated_at=? WHERE id=? AND tenant_id=? AND available_seats>0",
                                    (now_iso(), fid, tenant_id))
            if reserved.rowcount < 1:
                return self._json({'error': 'No seats available on this flight'}, 400)
            name = (data.get('passenger_name') or '').strip()
            if not name:
                return self._json({'error': 'passenger_name required'}, 400)
            fare = float(data.get('fare_amount') or flight.get('base_fare') or 0)
            taxes = float(data.get('taxes') or 0)
            total = float(data.get('total_amount') or (fare + taxes))
            pnr = (data.get('pnr') or self._psn('')).upper().replace('-', '')[:6]
            cur = data.get('currency') or flight.get('currency') or 'USD'
            conn.execute("""INSERT INTO pss_bookings(tenant_id,pnr,flight_id,passenger_name,passenger_type,contact_email,contact_phone,channel_id,airport_code,fare_class,fare_amount,taxes,total_amount,currency,ticket_status,payment_status,booking_status,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unissued','pending','confirmed',?,?,?)""",
                         (tenant_id, pnr, fid, name, data.get('passenger_type') or 'ADT', data.get('contact_email'),
                          data.get('contact_phone'), self._oid(data.get('channel_id')), data.get('airport_code') or flight.get('origin'),
                          data.get('fare_class') or 'Y', fare, taxes, total, cur, user_id, now_iso(), now_iso()))
            bid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._seed_tax_lines(conn, tenant_id, bid, {'taxes': taxes, 'currency': cur})
            trace_id = self._new_trace_id()
            agency_id = (sess or {}).get('agency_id')
            conn.execute("UPDATE pss_bookings SET trace_id=?, booking_source=COALESCE(booking_source,'counter'), agency_id=COALESCE(agency_id,?) WHERE id=? AND tenant_id=?",
                         (trace_id, agency_id, bid, tenant_id))
            self._activity(conn, tenant_id, 'pss_booking', bid, 'booking_created', f"PNR {pnr}", user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'booking_created', user_id, f"PNR {pnr}", trace_id, self)
            try:
                self._erp_sync_pss_booking(conn, tenant_id, bid, actor=user_id or 'counter', source='counter')
            except Exception:
                self._log_exception(Exception('erp sync booking failed'))
            log_audit(conn, tenant_id, user_id, 'create', 'pss_booking', bid, {'pnr': pnr})
        return self._json({'ok': True, 'booking_id': bid, 'pnr': pnr})

    def api_pss_booking_detail(self, tenant_id, bid, sess=None):
        bid = self._oid(bid)
        with db() as conn:
            row = q(conn, """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.gate, f.terminal, f.aircraft,
                                    c.name channel_name FROM pss_bookings b
                             LEFT JOIN pss_flights f ON f.id=b.flight_id LEFT JOIN pss_channels c ON c.id=b.channel_id
                             WHERE b.id=? AND b.tenant_id=?""", (bid, tenant_id), one=True)
            if row and (sess or {}).get('agency_id') and row.get('agency_id') != sess.get('agency_id'):
                row = None
            if not row:
                return self._json({'error': 'Booking not found'}, 404)
            checkins = q(conn, "SELECT * FROM pss_checkins WHERE booking_id=? AND tenant_id=? ORDER BY id DESC", (bid, tenant_id))
            payments = q(conn, "SELECT * FROM pss_payments WHERE booking_id=? AND tenant_id=? ORDER BY id DESC", (bid, tenant_id))
            events = q(conn, "SELECT previous_status,ticket_status,reason,actor,trace_id,created_at FROM pss_ticket_events WHERE tenant_id=? AND booking_id=? ORDER BY id DESC", (tenant_id, bid))
            tax_lines = q(conn, "SELECT tax_code,description,amount,currency FROM pss_tax_lines WHERE tenant_id=? AND booking_id=? ORDER BY id", (tenant_id, bid))
        return self._json({'booking': row, 'checkins': checkins, 'payments': payments, 'events': events, 'tax_lines': tax_lines})

    def api_pss_booking_ticket(self, tenant_id, user_id, bid, sess=None):
        bid = self._oid(bid)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not row or ((sess or {}).get('agency_id') and row.get('agency_id') != sess.get('agency_id')):
                return self._json({'error': 'Booking not found'}, 404)
            if row.get('ticket_status') == 'issued':
                return self._json({'error': 'Ticket already issued'}, 400)
            ticket = self._pss_next_ticket(conn, tenant_id, row.get('channel_id'), row.get('airport_code'))
            if not ticket:
                return self._json({'error': 'No ticket stock available - allocate stock first'}, 400)
            conn.execute("UPDATE pss_bookings SET ticket_number=?, ticket_status='issued', updated_at=? WHERE id=? AND tenant_id=?",
                         (ticket, now_iso(), bid, tenant_id))
            self._activity(conn, tenant_id, 'pss_booking', bid, 'ticket_issued', ticket, user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'ticket_issued', user_id, ticket, row.get('trace_id'), self)
            self._doc_audit(conn, tenant_id, 'eticket', ticket, bid, row.get('pnr'), 'ticket_issued', user_id, row.get('trace_id'), {'ticket_number': ticket})
            log_audit(conn, tenant_id, user_id, 'issue_ticket', 'pss_booking', bid, {'ticket_number': ticket})
        return self._json({'ok': True, 'ticket_number': ticket})

    def api_pss_booking_checkin(self, tenant_id, user_id, bid, sess=None):
        bid = self._oid(bid)
        data = read_json(self)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not row or ((sess or {}).get('agency_id') and row.get('agency_id') != sess.get('agency_id')):
                return self._json({'error': 'Booking not found'}, 404)
            if row.get('ticket_status') != 'issued':
                return self._json({'error': 'Ticket must be issued before check-in'}, 400)
            existing = q(conn, "SELECT id FROM pss_checkins WHERE booking_id=? AND tenant_id=? AND status='checked_in'", (bid, tenant_id), one=True)
            if existing:
                return self._json({'error': 'Passenger already checked in'}, 400)
            checkin_no = self._psn('CI')
            bag_count = int(data.get('bag_count') or 0)
            bags = data.get('bag_tags') or (', '.join(self._psn('BG') for _ in range(bag_count)) if bag_count else '')
            conn.execute("""INSERT INTO pss_checkins(tenant_id,checkin_no,booking_id,pnr,flight_id,passenger_name,seat_no,bag_count,bag_tags,checkin_method,counter,agent,status,checked_in_at,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'checked_in',?,?,?)""",
                         (tenant_id, checkin_no, bid, row.get('pnr'), row.get('flight_id'), row.get('passenger_name'),
                          data.get('seat_no'), bag_count, bags, data.get('checkin_method') or 'counter',
                          data.get('counter'), user_id, now_iso(), now_iso(), now_iso()))
            conn.execute("UPDATE pss_bookings SET booking_status='checked_in', seat_no=COALESCE(?,seat_no), updated_at=? WHERE id=? AND tenant_id=?",
                         (data.get('seat_no'), now_iso(), bid, tenant_id))
            self._activity(conn, tenant_id, 'pss_booking', bid, 'checked_in', checkin_no, user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'checked_in', user_id, checkin_no, row.get('trace_id'), self)
            log_audit(conn, tenant_id, user_id, 'checkin', 'pss_booking', bid, {'checkin_no': checkin_no})
        return self._json({'ok': True, 'checkin_no': checkin_no, 'bag_tags': bags})

    def api_pss_booking_payment(self, tenant_id, user_id, bid, sess=None):
        bid = self._oid(bid)
        data = read_json(self)
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not row or ((sess or {}).get('agency_id') and row.get('agency_id') != sess.get('agency_id')):
                return self._json({'error': 'Booking not found'}, 404)
            payment_no = self._psn('PAY')
            status = data.get('status') or 'captured'
            paid_at = data.get('paid_at') or now_iso()
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,channel_id,amount,currency,method,reference,gateway_ref,status,paid_at,captured_at,notes,created_by,created_at,updated_at,booking_source)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, payment_no, bid, row.get('pnr'), row.get('channel_id') or data.get('channel_id'), amount,
                          data.get('currency') or row.get('currency') or 'USD', data.get('method') or 'cash',
                          data.get('reference'), data.get('gateway_ref'), status, paid_at,
                          now_iso() if status in ('captured','paid') else None, data.get('notes'), user_id, now_iso(), now_iso(), 'counter'))
            ppid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            paid_total = (q(conn, "SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE booking_id=? AND tenant_id=? AND status IN ('captured','paid')", (bid, tenant_id), one=True) or {}).get('v', 0)
            pay_status = 'paid' if paid_total >= float(row.get('total_amount') or 0) else ('partial' if paid_total > 0 else 'pending')
            conn.execute("UPDATE pss_bookings SET payment_status=?, updated_at=? WHERE id=? AND tenant_id=?", (pay_status, now_iso(), bid, tenant_id))
            self._activity(conn, tenant_id, 'pss_booking', bid, 'payment_recorded', payment_no, user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'payment_recorded', user_id, f"{payment_no} - {amount}", row.get('trace_id'), self)
            try:
                self._erp_sync_pss_booking(conn, tenant_id, bid, actor=user_id or 'counter', source='counter')
                self._erp_sync_pss_payment(conn, tenant_id, ppid, actor=user_id or 'counter', source='counter')
            except Exception:
                self._log_exception(Exception('erp sync pss payment failed'))
            log_audit(conn, tenant_id, user_id, 'payment', 'pss_booking', bid, {'payment_no': payment_no, 'amount': amount})
        return self._json({'ok': True, 'payment_no': payment_no, 'payment_status': pay_status})

    def api_pss_checkins(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination FROM pss_checkins ci
                              LEFT JOIN pss_flights f ON f.id=ci.flight_id WHERE ci.tenant_id=? ORDER BY ci.id DESC LIMIT 500""", (tenant_id,))
        return self._json({'checkins': rows})

    def api_pss_payments(self, tenant_id, sess=None):
        with db() as conn:
            sql = """SELECT p.*, c.name channel_name FROM pss_payments p LEFT JOIN pss_channels c ON c.id=p.channel_id
                     WHERE p.tenant_id=?"""
            args = [tenant_id]
            aid = (sess or {}).get('agency_id')
            if aid:
                sql += " AND p.booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND agency_id=?)"
                args += [tenant_id, aid]
            sql += " ORDER BY p.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'payments': rows})

    def api_pss_stock(self, tenant_id):
        with db() as conn:
            pools = q(conn, "SELECT * FROM pss_stock_pools WHERE tenant_id=? ORDER BY id DESC", (tenant_id,))
            for p in pools:
                p['remaining'] = int(p.get('end_serial') or 0) - int(p.get('next_serial') or 0) + 1
            allocs = q(conn, """SELECT a.*, p.prefix FROM pss_stock_allocations a JOIN pss_stock_pools p ON p.id=a.pool_id
                               WHERE a.tenant_id=? ORDER BY a.id DESC LIMIT 300""", (tenant_id,))
            for a in allocs:
                a['remaining'] = int(a.get('end_serial') or 0) - int(a.get('next_serial') or 0) + 1
        return self._json({'pools': pools, 'allocations': allocs})

    def api_pss_allocations(self, tenant_id):
        return self.api_pss_stock(tenant_id)

    def api_pss_stock_allocate(self, tenant_id, user_id):
        data = read_json(self)
        pid = self._oid(data.get('pool_id'))
        qty = int(data.get('quantity') or 0)
        if qty <= 0:
            return self._json({'error': 'quantity required'}, 400)
        with db() as conn:
            pool = q(conn, "SELECT * FROM pss_stock_pools WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
            if not pool:
                return self._json({'error': 'Stock pool not found'}, 404)
            start = int(pool.get('next_serial') or pool.get('start_serial'))
            end = start + qty - 1
            if end > int(pool.get('end_serial')):
                return self._json({'error': 'Not enough stock in pool'}, 400)
            alloc_no = self._psn('AL')
            conn.execute("""INSERT INTO pss_stock_allocations(tenant_id,allocation_no,pool_id,channel_id,airport_code,quantity,start_serial,end_serial,next_serial,allocated_to,status,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
                         (tenant_id, alloc_no, pid, self._oid(data.get('channel_id')), data.get('airport_code'),
                          qty, start, end, start, data.get('allocated_to'), now_iso(), now_iso()))
            conn.execute("UPDATE pss_stock_pools SET next_serial=?, updated_at=? WHERE id=? AND tenant_id=?", (end + 1, now_iso(), pid, tenant_id))
            self._trace(conn, tenant_id, 'counter', 'pss_stock_allocation', None, 'stock_allocated', user_id, alloc_no, None, self)
            log_audit(conn, tenant_id, user_id, 'allocate_stock', 'pss_stock_pool', pid, {'quantity': qty, 'allocation_no': alloc_no})
        return self._json({'ok': True, 'allocation_no': alloc_no, 'start_serial': start, 'end_serial': end})

    def api_pss_stock_issue(self, tenant_id, user_id):
        data = read_json(self)
        with db() as conn:
            ticket = self._pss_next_ticket(conn, tenant_id, self._oid(data.get('channel_id')), data.get('airport_code'))
            if not ticket:
                return self._json({'error': 'No stock available'}, 400)
            log_audit(conn, tenant_id, user_id, 'issue_stock', 'pss_stock', None, {'ticket_number': ticket})
        return self._json({'ok': True, 'ticket_number': ticket})

    def api_pss_stock_create(self, tenant_id, user_id):
        data = read_json(self)
        pool_no = (data.get('pool_no') or '').strip() or self._psn('POOL')
        airline_code = (data.get('airline_code') or 'UG').strip().upper() or 'UG'
        prefix = (data.get('prefix') or airline_code).strip().upper() or airline_code
        try:
            start = int(data.get('start_serial') or 0)
            end = int(data.get('end_serial') or 0)
        except Exception:
            return self._json({'error': 'start_serial and end_serial must be numbers'}, 400)
        if start <= 0 or end < start:
            return self._json({'error': 'Invalid serial range'}, 400)
        loc_type = (data.get('location_type') or 'airport').strip() or 'airport'
        loc_code = (data.get('location_code') or '').strip() or None
        status = (data.get('status') or 'active').strip() or 'active'
        notes = data.get('notes')
        ts = now_iso()
        with db() as conn:
            try:
                cur = conn.execute("""INSERT INTO pss_stock_pools(tenant_id,pool_no,airline_code,prefix,start_serial,end_serial,next_serial,issued_count,void_count,location_type,location_code,status,notes,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,0,0,?,?,?,?,?,?)""",
                                   (tenant_id, pool_no, airline_code, prefix, start, end, start, loc_type, loc_code, status, notes, ts, ts))
                pid = cur.lastrowid
            except Exception as e:
                if 'UNIQUE' in str(e).upper():
                    return self._json({'error': 'Pool number already exists'}, 400)
                raise
            log_audit(conn, tenant_id, user_id, 'create', 'pss_stock_pool', pid, {'pool_no': pool_no})
        return self._json({'ok': True, 'pool_id': pid, 'pool_no': pool_no})

    def api_pss_stock_mutate(self, tenant_id, user_id, pid, method):
        pid = self._oid(pid)
        if not pid:
            return self._json({'error': 'Invalid pool id'}, 400)
        with db() as conn:
            pool = q(conn, "SELECT * FROM pss_stock_pools WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
            if not pool:
                return self._json({'error': 'Stock pool not found'}, 404)
            if method == 'DELETE':
                used = q(conn, "SELECT COUNT(*) c FROM pss_stock_allocations WHERE pool_id=? AND tenant_id=?", (pid, tenant_id), one=True)['c']
                if used or int(pool.get('issued_count') or 0) > 0:
                    return self._json({'error': 'Cannot delete a pool with allocations or issued tickets'}, 400)
                conn.execute("DELETE FROM pss_stock_pools WHERE id=? AND tenant_id=?", (pid, tenant_id))
                log_audit(conn, tenant_id, user_id, 'delete', 'pss_stock_pool', pid, {})
                return self._json({'ok': True})
            data = read_json(self)
            status = (data.get('status') or pool.get('status') or 'active').strip() or 'active'
            loc_type = (data.get('location_type') or pool.get('location_type') or 'airport').strip() or 'airport'
            loc_code = (data.get('location_code') if 'location_code' in data else pool.get('location_code'))
            notes = data.get('notes') if 'notes' in data else pool.get('notes')
            airline_code = (data.get('airline_code') or pool.get('airline_code') or 'UG').strip().upper()
            prefix = (data.get('prefix') or pool.get('prefix') or airline_code).strip().upper()
            pool_no = (data.get('pool_no') or pool.get('pool_no') or '').strip() or pool.get('pool_no')
            conn.execute("""UPDATE pss_stock_pools SET pool_no=?, airline_code=?, prefix=?, location_type=?, location_code=?, status=?, notes=?, updated_at=?
                            WHERE id=? AND tenant_id=?""",
                         (pool_no, airline_code, prefix, loc_type, (loc_code or None), status, notes, now_iso(), pid, tenant_id))
            log_audit(conn, tenant_id, user_id, 'update', 'pss_stock_pool', pid, {'pool_no': pool_no, 'status': status})
        return self._json({'ok': True})


    # ---------------- Traceability & public booking channels ----------------
    def _sess_agency_id(self, sess):
        return (sess or {}).get('agency_id')

    def _agency_where(self, sess, alias=''):
        aid = self._sess_agency_id(sess)
        if not aid:
            return '', []
        p = (alias + '.') if alias else ''
        return f" AND {p}agency_id=?", [aid]

    def _erp_sync_pss_booking(self, conn, tenant_id, bid, actor='system', source=None):
        b = q(conn, """SELECT b.*, f.flight_no,f.origin,f.destination,f.flight_date,f.dep_time
                       FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                       WHERE b.id=? AND b.tenant_id=?""", (bid, tenant_id), one=True)
        if not b:
            return None
        ts = now_iso()
        name = (b.get('passenger_name') or 'PSS Passenger').strip()
        email = (b.get('contact_email') or '').strip() or None
        phone = (b.get('contact_phone') or '').strip() or None
        agency_id = b.get('agency_id')
        source = source or b.get('booking_source') or 'counter'
        cid = b.get('customer_id')
        if cid:
            c = q(conn, "SELECT * FROM customers WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True)
            if not c:
                cid = None
        if not cid and email:
            c = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND lower(email)=lower(?)", (tenant_id, email), one=True)
            cid = c['id'] if c else None
        if not cid:
            cur = conn.execute(
                "INSERT INTO customers(tenant_id,type,name,company,email,phone,notes,created_at,agency_id,source,source_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, 'B2C', name, None, email, phone, f"Auto-created from PSS booking {b.get('pnr')}", ts, agency_id, source, b.get('pnr')))
            cid = cur.lastrowid
        else:
            conn.execute("UPDATE customers SET name=COALESCE(?,name), email=COALESCE(?,email), phone=COALESCE(?,phone), agency_id=COALESCE(?,agency_id) WHERE id=? AND tenant_id=?",
                         (name, email, phone, agency_id, cid, tenant_id))
        # order
        oid = b.get('order_id')
        if not oid:
            o = q(conn, "SELECT id FROM orders WHERE tenant_id=? AND pss_booking_id=?", (tenant_id, bid), one=True)
            oid = o['id'] if o else None
        total = float(b.get('total_amount') or 0)
        pnr = b.get('pnr') or f"PSS-{bid}"
        src = f"pss_booking:{bid}"
        if not oid:
            conn.execute("""INSERT INTO orders(tenant_id,branch_id,source_type,source_id,connector_id,raw_document_id,pnr,order_number,office_id,account_code,customer_id,owner_user_id,product_type,status,ticket_status,tktl,booking_date,travel_start,travel_end,origin,destination,currency,total_sell,total_cost,margin,balance_due,notes,created_at,updated_at,agency_id,pss_booking_id,booking_source,trace_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, None, 'pss_booking', src, None, None, pnr, pnr, b.get('airport_code'), None, cid, b.get('created_by'),
                          'Flight', b.get('booking_status') or 'booked', b.get('ticket_status') or 'unissued', None,
                          (b.get('created_at') or ts)[:10], b.get('flight_date'), b.get('flight_date'),
                          b.get('origin'), b.get('destination'), b.get('currency') or 'USD',
                          total, 0, total, total, f"PSS booking {pnr}", ts, ts, agency_id, bid, source, b.get('trace_id')))
            oid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
        else:
            conn.execute("""UPDATE orders SET customer_id=?, pnr=COALESCE(?,pnr), origin=?, destination=?, currency=?,
                            total_sell=?, margin=?, balance_due=CASE WHEN balance_due=0 THEN 0 ELSE ? END,
                            booking_date=COALESCE(?,booking_date), travel_start=?, travel_end=?, updated_at=?,
                            agency_id=COALESCE(?,agency_id), booking_source=?, trace_id=?, ticket_status=?
                            WHERE id=? AND tenant_id=?""",
                         (cid, pnr, b.get('origin'), b.get('destination'), b.get('currency') or 'USD', total, total, total,
                          (b.get('created_at') or ts)[:10], b.get('flight_date'), b.get('flight_date'), ts,
                          agency_id, source, b.get('trace_id'), b.get('ticket_status') or 'unissued', oid, tenant_id))
        # invoice
        iid = b.get('invoice_id')
        if not iid:
            inv = q(conn, "SELECT id FROM invoices WHERE tenant_id=? AND pss_booking_id=?", (tenant_id, bid), one=True)
            iid = inv['id'] if inv else None
        fare = float(b.get('fare_amount') or 0)
        taxes = float(b.get('taxes') or 0)
        inv_total = float(b.get('total_amount') or (fare + taxes))
        cur = b.get('currency') or 'USD'
        if not iid:
            n = q(conn, "SELECT COUNT(*) c FROM invoices WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            inv_no = f"INV-PSS-{ts[:4]}-{n:05d}"
            conn.execute("""INSERT INTO invoices(tenant_id,branch_id,order_id,customer_id,invoice_no,kind,status,issue_date,due_date,currency,subtotal,tax,fees,discount,total,amount_paid,balance,notes,created_at,agency_id,pss_booking_id,source)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?)""",
                         (tenant_id, None, oid, cid, inv_no, 'invoice', 'issued', ts[:10], ts[:10], cur, fare, taxes, 0, 0, inv_total, 0, inv_total,
                          f"Auto invoice for PSS booking {pnr}", ts, agency_id, bid, source))
            iid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            desc = f"Flight {b.get('flight_no') or ''} {b.get('origin') or ''}-{b.get('destination') or ''} {b.get('flight_date') or ''} ({pnr})".strip()
            conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                         (tenant_id, iid, desc, 1, 0, fare, taxes, inv_total))
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,pss_booking_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'invoice', iid, ts[:10], f"Invoice {inv_no}", 'Accounts Receivable', 'Sales', inv_total, cur, ts, agency_id, bid, source))
        # update links
        conn.execute("UPDATE pss_bookings SET customer_id=?, order_id=?, invoice_id=? WHERE id=? AND tenant_id=?", (cid, oid, iid, bid, tenant_id))
        try:
            from ingest import recompute_invoice as _ri
            _ri(conn, iid)
        except Exception:
            pass
        try:
            self._activity(conn, tenant_id, 'pss_booking', bid, 'erp_synced', f"customer={cid} order={oid} invoice={iid}", actor)
        except Exception:
            pass
        return {'customer_id': cid, 'order_id': oid, 'invoice_id': iid}

    def _erp_sync_pss_payment(self, conn, tenant_id, pss_payment_id, actor='system', source=None):
        p = q(conn, "SELECT * FROM pss_payments WHERE id=? AND tenant_id=?", (pss_payment_id, tenant_id), one=True)
        if not p:
            return None
        if p.get('status') not in ('captured', 'paid', 'received', 'successful'):
            return None
        existing = p.get('erp_payment_id')
        if existing and q(conn, "SELECT 1 FROM payments WHERE id=? AND tenant_id=?", (existing, tenant_id), one=True):
            return existing
        # resolve booking/customer/invoice/agency
        bid = p.get('booking_id')
        b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True) if bid else None
        if not b and p.get('pnr'):
            b = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?) ORDER BY id DESC LIMIT 1",
                  (tenant_id, p.get('pnr'), p.get('pnr')), one=True)
            if b: bid = b['id']
        if b and not b.get('customer_id'):
            self._erp_sync_pss_booking(conn, tenant_id, b['id'], actor, source or p.get('booking_source'))
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (b['id'], tenant_id), one=True)
        cid = p.get('customer_id') or (b.get('customer_id') if b else None)
        iid = p.get('invoice_id') or (b.get('invoice_id') if b else None)
        agency_id = (b.get('agency_id') if b else None)
        ts = now_iso()
        amount = float(p.get('amount') or 0)
        cur = p.get('currency') or 'USD'
        pno = p.get('payment_no') or f"PMT-PSS-{pss_payment_id}"
        ref = p.get('reference') or p.get('gateway_ref') or pno
        try:
            cur = cur or 'USD'
            r = conn.execute("""INSERT INTO payments(tenant_id,branch_id,invoice_id,order_id,customer_id,payment_no,method,reference,amount,currency,status,paid_at,notes,created_at,agency_id,pss_payment_id,pss_booking_id,source)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (tenant_id, None, iid, b.get('order_id') if b else None, cid, pno, p.get('method') or 'card', ref, amount, cur,
                              'received', (p.get('paid_at') or p.get('captured_at') or ts)[:10], p.get('notes'), ts,
                              agency_id, pss_payment_id, bid, source or p.get('booking_source') or 'portal'))
            epid = r.lastrowid
        except Exception:
            ex = q(conn, "SELECT id FROM payments WHERE tenant_id=? AND pss_payment_id=?", (tenant_id, pss_payment_id), one=True)
            if not ex: return None
            epid = ex['id']
        if iid:
            try:
                conn.execute("INSERT INTO allocations(tenant_id,payment_id,invoice_id,amount,created_at) VALUES (?,?,?,?,?)", (tenant_id, epid, iid, amount, ts))
            except Exception:
                pass
            try:
                from ingest import recompute_invoice as _ri
                _ri(conn, iid)
            except Exception:
                pass
        try:
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,pss_booking_id,pss_payment_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'payment', epid, ts[:10], f"Payment {pno}", 'Petty Cash', 'Accounts Receivable', amount, cur, ts, agency_id, bid, pss_payment_id, source or p.get('booking_source') or 'portal'))
        except Exception:
            pass
        conn.execute("UPDATE pss_payments SET erp_payment_id=?, customer_id=?, invoice_id=? WHERE id=? AND tenant_id=?", (epid, cid, iid, pss_payment_id, tenant_id))
        return epid

    def _new_trace_id(self):
        return 'TR-' + os.urandom(6).hex().upper()

    def _trace(self, conn, tenant_id, source, entity_type, entity_id, action, actor='system', detail=None,
               trace_id=None, handler=None):
        tid = trace_id or self._new_trace_id()
        ip, ua = '', ''
        if handler is not None:
            try:
                ip = handler.headers.get('X-Forwarded-For') or (handler.client_address[0] if getattr(handler, 'client_address', None) else '')
                ua = handler.headers.get('User-Agent') or ''
            except Exception:
                pass
        conn.execute("""INSERT INTO pss_trace(tenant_id,trace_id,source,entity_type,entity_id,action,actor,detail,ip,user_agent,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (tenant_id, tid, source, entity_type, entity_id, action, str(actor or 'system'),
                      detail, (ip or '')[:120], (ua or '')[:250], now_iso()))
        return tid

    def _portal_tenant(self, code):
        code = (code or '').strip()
        if not code:
            return None
        if code.lower() == 'fayaka':
            code = 'demo'
        with db() as conn:
            return q(conn, "SELECT id,code,name FROM tenants WHERE code=? AND status='active'", (code,), one=True)

    def api_portal_tenants(self):
        with db() as conn:
            rows = q(conn, "SELECT code,name FROM tenants WHERE status='active' ORDER BY id")
        return self._json({'tenants': rows})

    def api_portal_flights(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        origin = (qs.get('origin') or [''])[0].strip().upper()
        dest = (qs.get('destination') or [''])[0].strip().upper()
        date = (qs.get('date') or [''])[0].strip()
        sql = "SELECT id,flight_no,origin,destination,flight_date,dep_time,arr_time,aircraft,available_seats,base_fare,currency,gate,terminal,status FROM pss_flights WHERE tenant_id=? AND LOWER(COALESCE(status,'scheduled'))='scheduled'"
        args = [tenant['id']]
        if origin:
            sql += " AND UPPER(origin)=?"; args.append(origin)
        if dest:
            sql += " AND UPPER(destination)=?"; args.append(dest)
        if date:
            sql += " AND flight_date=?"; args.append(date)
        sql += " ORDER BY flight_date,dep_time LIMIT 100"
        with db() as conn:
            rows = q(conn, sql, args)
        return self._json({'airline': tenant, 'flights': rows})


    def _portal_cabin_multiplier(self, cabin):
        return {'Y': 1.0, 'P': 1.6, 'C': 2.5, 'F': 4.0}.get((cabin or 'Y').upper(), 1.0)

    def api_portal_search(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        def val(key, default=''):
            return (qs.get(key) or [default])[0].strip()
        trip = val('trip_type', 'oneway').lower()
        origin, dest, date = val('origin').upper(), val('destination').upper(), val('date')
        return_date = val('return_date')
        cabin = val('cabin', 'Y').upper()
        try:
            adults = max(1, int(val('adults', '1') or 1))
            children = max(0, int(val('children', '0') or 0))
            infants = max(0, int(val('infants', '0') or 0))
        except Exception:
            adults, children, infants = 1, 0, 0
        segments = []
        raw_segments = val('segments')
        if raw_segments:
            try: segments = json.loads(raw_segments)
            except Exception: segments = []
        multiplier = self._portal_cabin_multiplier(cabin)
        with db() as conn:
            def flights(o, d, dt):
                sql = """SELECT id,flight_no,origin,destination,flight_date,dep_time,arr_time,aircraft,
                                available_seats,base_fare,currency,gate,terminal,status
                         FROM pss_flights WHERE tenant_id=? AND LOWER(COALESCE(status,'scheduled')) IN ('scheduled','boarding')"""
                args = [tenant['id']]
                if o: sql += " AND UPPER(origin)=?"; args.append(o)
                if d: sql += " AND UPPER(destination)=?"; args.append(d)
                if dt: sql += " AND flight_date=?"; args.append(dt)
                sql += " AND COALESCE(available_seats, total_seats, 0)>0 ORDER BY flight_date,dep_time LIMIT 100"
                rows = q(conn, sql, args)
                for r in rows:
                    r['fare'] = round(float(r.get('base_fare') or 0) * multiplier, 2)
                    r['cabin'] = cabin
                return rows
            if trip == 'multicity' and segments:
                seg_results = [flights(str(x.get('origin') or '').upper(), str(x.get('destination') or '').upper(),
                                       str(x.get('date') or '')) for x in segments[:5]]
            else:
                seg_results = [flights(origin, dest, date)]
                if trip == 'return' and return_date:
                    seg_results.append(flights(dest, origin, return_date))
        return self._json({'airline': tenant, 'trip_type': trip, 'cabin': cabin,
                           'passengers': {'adults': adults, 'children': children, 'infants': infants,
                                          'total': adults + children + infants},
                           'segments': seg_results, 'outbound': seg_results[0] if seg_results else [],
                           'return': seg_results[1] if len(seg_results) > 1 else []})


    def _agency_offer(self, conn, token):
        return q(conn, """SELECT o.*, a.name agency_name, a.code agency_code, a.commission_rate,
                                 f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.available_seats
                          FROM pss_eticket_directs o JOIN pss_agencies a ON a.id=o.agency_id
                          LEFT JOIN pss_flights f ON f.id=o.flight_id
                          WHERE o.token=? AND o.status='active'""", (token,), one=True)

    def api_portal_charter(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        if not data.get('client_name'):
            return self._json({'error': 'client_name required'}, 400)
        with db() as conn:
            request_no = self._psn('CHR')
            trace_id = self._new_trace_id()
            conn.execute("""INSERT INTO pss_charter_requests(tenant_id,request_no,client_name,contact_email,contact_phone,
                            origin,destination,flight_date,pax_count,aircraft_type,requirements,status,quoted_amount,currency,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',0,?,NULL,?,?)""",
                         (tenant['id'], request_no, data.get('client_name'), data.get('email'), data.get('phone'),
                          data.get('origin'), data.get('destination'), data.get('flight_date'),
                          data.get('pax_count'), data.get('aircraft_type'), data.get('requirements'),
                          data.get('currency') or 'USD', now_iso(), now_iso()))
            rid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant['id'], 'portal', 'pss_charter_request', rid, 'charter_requested',
                        data.get('client_name'), f"{request_no} · {data.get('origin')}-{data.get('destination')}", trace_id, self)
        return self._json({'ok': True, 'request_no': request_no, 'trace_id': trace_id})

    def api_portal_book(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        flight_ids = data.get('flights') or []
        if isinstance(flight_ids, (str, int)):
            flight_ids = [flight_ids]
        flight_ids = [self._oid(x) for x in flight_ids if self._oid(x)]
        if not flight_ids and data.get('flight_id'):
            flight_ids = [self._oid(data.get('flight_id'))]
        passengers = data.get('passengers') or []
        if not passengers and data.get('passenger_name'):
            passengers = [{'full_name': data.get('passenger_name'), 'type': data.get('passenger_type') or 'ADT',
                           'doc_no': None, 'email': data.get('email'), 'phone': data.get('phone')}]
        if not flight_ids or not passengers:
            return self._json({'error': 'Select at least one flight and one passenger'}, 400)
        trip = (data.get('trip_type') or 'oneway').lower()
        cabin = (data.get('cabin') or 'Y').upper()
        multiplier = self._portal_cabin_multiplier(cabin)
        web_channel = None
        contact = data.get('contact') or {}
        pnr = os.urandom(3).hex().upper()
        itinerary_id = os.urandom(8).hex()
        trace_id = self._new_trace_id()
        psess = get_portal_session(self)
        portal_customer_id = psess['customer_id'] if psess else None
        with db() as conn:
            web_channel_row = q(conn, "SELECT id FROM pss_channels WHERE tenant_id=? AND code='WEB'", (tenant['id'],), one=True)
            web_channel = web_channel_row['id'] if web_channel_row else None
            conn.execute("SAVEPOINT portal_book")
            try:
                flights = []
                for fid in flight_ids:
                    f = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=? AND status='scheduled'", (fid, tenant['id']), one=True)
                    if not f:
                        raise ValueError('Flight not found')
                    reserved = conn.execute("UPDATE pss_flights SET available_seats=available_seats-1, updated_at=? WHERE id=? AND tenant_id=? AND available_seats>0",
                                            (now_iso(), fid, tenant['id']))
                    if reserved.rowcount < 1:
                        raise ValueError('A selected flight is full - no booking was made')
                    flights.append(f)
                booking_ids, total = [], 0.0
                for pax in passengers:
                    name = str(pax.get('full_name') or pax.get('name') or '').strip()
                    if not name:
                        raise ValueError('Passenger name required')
                    ptype = str(pax.get('type') or pax.get('passenger_type') or 'ADT').upper()
                    for idx, f in enumerate(flights):
                        fare = round(float(f.get('base_fare') or 0) * multiplier, 2)
                        taxes = round(fare * 0.10, 2) if ptype != 'INF' else 0.0
                        amount = fare + taxes
                        total += amount
                        row_pnr = pnr if not booking_ids else f"{pnr}-{len(booking_ids)+1}"
                        cols = ['tenant_id','pnr','trip_pnr','flight_id','passenger_name','passenger_type','doc_no',
                                'contact_email','contact_phone','channel_id','airport_code','fare_class','fare_amount',
                                'taxes','total_amount','currency','ticket_status','payment_status','booking_status',
                                'booking_source','trace_id','itinerary_id','trip_type','segment_index','created_at','updated_at','portal_customer_id']
                        vals = [tenant['id'], row_pnr, pnr, f['id'], name, ptype, pax.get('doc_no'),
                                contact.get('email') or pax.get('email'), contact.get('phone') or pax.get('phone'), web_channel,
                                f.get('origin'), cabin, fare, taxes, amount, f.get('currency') or 'USD',
                                'unissued', 'pending', 'confirmed', 'portal', trace_id, itinerary_id, trip, idx,
                                now_iso(), now_iso(), portal_customer_id]
                        conn.execute("INSERT INTO pss_bookings(" + ','.join(cols) + ") VALUES (" + ','.join(['?'] * len(cols)) + ")", vals)
                        booking_ids.append(q(conn, "SELECT last_insert_rowid() v", (), one=True)['v'])
                        self._seed_tax_lines(conn, tenant['id'], booking_ids[-1], {'taxes': taxes, 'currency': f.get('currency') or 'USD'})
                for item in (data.get('ancillaries') or []):
                    aid = self._oid(item.get('ancillary_id'))
                    anc = q(conn, "SELECT * FROM pss_ancillaries WHERE id=? AND tenant_id=?", (aid, tenant['id']), one=True)
                    if not anc:
                        continue
                    qty = int(item.get('quantity') or 1)
                    amount = float(anc.get('price') or 0) * qty
                    total += amount
                    conn.execute("""INSERT INTO pss_ancillary_sales(tenant_id,sale_no,booking_id,ancillary_id,description,quantity,unit_price,amount,currency,status,payment_status,trace_id,created_by,created_at,updated_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,'sold','pending',?,NULL,?,?)""",
                                 (tenant['id'], self._psn('ANC'), booking_ids[0], aid, anc.get('name'), qty,
                                  anc.get('price') or 0, amount, anc.get('currency') or 'USD', trace_id, now_iso(), now_iso()))
                self._trace(conn, tenant['id'], 'portal', 'pss_booking', booking_ids[0], 'booking_created',
                            contact.get('email') or passengers[0].get('full_name'),
                            f"PNR {pnr} · {len(passengers)} pax · {len(flights)} segment(s)", trace_id, self)
                for _bid in booking_ids:
                    try:
                        self._erp_sync_pss_booking(conn, tenant['id'], _bid, actor='portal', source='portal')
                    except Exception:
                        pass
                conn.execute("RELEASE portal_book")
            except Exception as exc:
                self._log_exception(exc)
                conn.execute("ROLLBACK TO portal_book")
                return self._json({'error': 'Booking failed - please try again'}, 400)
        return self._json({'ok': True, 'pnr': pnr, 'itinerary_id': itinerary_id, 'trace_id': trace_id,
                           'booking_ids': booking_ids, 'total': round(total, 2),
                           'segments': len(flights), 'passengers': len(passengers)})

    def api_portal_pay(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        pnr = (data.get('pnr') or '').strip().upper()
        last = (data.get('last_name') or '').strip().lower()
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        with db() as conn:
            rows = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (tenant['id'], pnr, pnr))
            if not rows or not any(last in (r.get('passenger_name') or '').lower() for r in rows):
                return self._json({'error': 'PNR or passenger name does not match'}, 404)
            first = rows[0]
            trace_id = first.get('trace_id') or self._new_trace_id()
            payment_no = self._psn('PAY')
            status = data.get('status') or 'captured'
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,amount,currency,method,reference,status,paid_at,captured_at,notes,booking_source,trace_id,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'portal',?,?,?)""",
                         (tenant['id'], payment_no, first['id'], pnr, amount, data.get('currency') or first.get('currency') or 'USD',
                          data.get('method') or 'card', data.get('reference'), status, now_iso(),
                          now_iso() if status in ('captured', 'paid') else None, data.get('notes'), trace_id, now_iso(), now_iso()))
            _ppid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            paid = (q(conn, "SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE tenant_id=? AND pnr=? AND status IN ('captured','paid')", (tenant['id'], pnr), one=True) or {}).get('v', 0)
            total_due = sum(float(r.get('total_amount') or 0) for r in rows)
            ps = 'paid' if paid >= total_due else ('partial' if paid > 0 else 'pending')
            conn.execute("UPDATE pss_bookings SET payment_status=?, updated_at=? WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (ps, now_iso(), tenant['id'], pnr, pnr))
            self._trace(conn, tenant['id'], 'portal', 'pss_booking', first['id'], 'payment_recorded',
                        first.get('passenger_name'), f"{payment_no} - {amount}", trace_id, self)
            for _b in rows:
                try:
                    self._erp_sync_pss_booking(conn, tenant['id'], _b['id'], actor='portal', source='portal')
                except Exception:
                    pass
            try:
                self._erp_sync_pss_payment(conn, tenant['id'], _ppid, actor='portal', source='portal')
            except Exception:
                pass
        return self._json({'ok': True, 'payment_no': payment_no, 'payment_status': ps, 'trace_id': trace_id})

    def api_portal_booking(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        pnr = (qs.get('pnr') or [''])[0].strip().upper()
        last = (qs.get('last_name') or [''])[0].strip().lower()
        with db() as conn:
            rows = q(conn, """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.gate
                              FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                              WHERE b.tenant_id=? AND (b.pnr=? OR b.trip_pnr=?) ORDER BY b.segment_index, b.id""", (tenant['id'], pnr, pnr))
            if not rows or not any(last in (r.get('passenger_name') or '').lower() for r in rows):
                return self._json({'error': 'PNR or passenger name does not match'}, 404)
            pays = q(conn, "SELECT payment_no,amount,status,method,paid_at FROM pss_payments WHERE tenant_id=? AND pnr=? ORDER BY id DESC", (tenant['id'], pnr))
            cis = q(conn, "SELECT ci.checkin_no,ci.pnr,ci.seat_no,ci.status,ci.checked_in_at FROM pss_checkins ci WHERE ci.tenant_id=? AND ci.pnr=? ORDER BY ci.id DESC", (tenant['id'], pnr))
            anc = q(conn, "SELECT sale_no,description,quantity,amount,currency FROM pss_ancillary_sales WHERE tenant_id=? AND booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)) ORDER BY id DESC", (tenant['id'], tenant['id'], pnr, pnr))
            traces = q(conn, "SELECT action,source,actor,detail,created_at FROM pss_trace WHERE tenant_id=? AND trace_id=? ORDER BY id", (tenant['id'], rows[0].get('trace_id') or ''))
        return self._json({'booking': rows[0], 'bookings': rows, 'payments': pays, 'checkins': cis,
                           'ancillaries': anc, 'trace': traces, 'airline': tenant})


    def api_agency_offer(self, token):
        with db() as conn:
            offer = self._agency_offer(conn, token)
            if not offer:
                return self._json({'error': 'Direct e-ticket link not found or expired'}, 404)
            tenant = q(conn, "SELECT code,name FROM tenants WHERE id=?", (offer['tenant_id'],), one=True) or {}
        return self._json({'offer': offer, 'airline': tenant})

    def api_agency_offer_book(self, token):
        data = read_json(self)
        with db() as conn:
            offer = self._agency_offer(conn, token)
            if not offer:
                return self._json({'error': 'Direct e-ticket link not found or expired'}, 404)
            claimed = conn.execute("UPDATE pss_eticket_directs SET used_count=used_count+1, updated_at=? WHERE id=? AND tenant_id=? AND used_count<quantity",
                                   (now_iso(), offer['id'], offer['tenant_id']))
            if claimed.rowcount < 1:
                return self._json({'error': 'No e-tickets left on this direct offer'}, 400)
            if offer.get('flight_id'):
                conn.execute("UPDATE pss_flights SET available_seats=MAX(0,available_seats-1), updated_at=? WHERE id=? AND tenant_id=? AND available_seats>0",
                             (now_iso(), offer['flight_id'], offer['tenant_id']))
            name = (data.get('passenger_name') or '').strip()
            if not name:
                return self._json({'error': 'passenger_name required'}, 400)
            fare = float(offer.get('fare_amount') or 0)
            taxes = float(offer.get('taxes') or 0)
            pnr = os.urandom(3).hex().upper()
            trace_id = self._new_trace_id()
            channel = q(conn, "SELECT id FROM pss_channels WHERE tenant_id=? AND code='GDS'", (offer['tenant_id'],), one=True)
            cols = ['tenant_id', 'pnr', 'trip_pnr', 'flight_id', 'passenger_name', 'passenger_type', 'doc_no',
                    'contact_email', 'contact_phone', 'channel_id', 'airport_code', 'fare_class', 'fare_amount',
                    'taxes', 'total_amount', 'currency', 'ticket_status', 'payment_status', 'booking_status',
                    'agency_id', 'offer_id', 'booking_source', 'trace_id', 'created_at', 'updated_at']
            vals = [offer['tenant_id'], pnr, pnr, offer.get('flight_id'), name,
                    data.get('passenger_type') or 'ADT', data.get('doc_no'), data.get('email'), data.get('phone'),
                    channel['id'] if channel else None, offer.get('origin'), 'Y', fare, taxes, fare + taxes,
                    offer.get('currency') or 'USD', 'unissued', 'pending', 'confirmed',
                    offer['agency_id'], offer['id'], 'agency_direct', trace_id, now_iso(), now_iso()]
            conn.execute("INSERT INTO pss_bookings(" + ','.join(cols) + ") VALUES (" + ','.join(['?'] * len(cols)) + ")", vals)
            bid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._seed_tax_lines(conn, offer['tenant_id'], bid, {'taxes': taxes, 'currency': offer.get('currency') or 'USD'})
            ticket = self._pss_next_ticket(conn, offer['tenant_id'], channel['id'] if channel else None, offer.get('origin'))
            if not ticket:
                pool = q(conn, "SELECT * FROM pss_stock_pools WHERE tenant_id=? AND status='active' AND end_serial>=next_serial ORDER BY id LIMIT 1", (offer['tenant_id'],), one=True)
                if pool:
                    start = int(pool.get('next_serial') or pool.get('start_serial'))
                    end = min(start + 9, int(pool.get('end_serial')))
                    alloc_sql = "INSERT INTO pss_stock_allocations(tenant_id,allocation_no,pool_id,channel_id,airport_code,quantity,start_serial,end_serial,next_serial,allocated_to,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?)"
                    conn.execute(alloc_sql, (offer['tenant_id'], self._psn('AL'), pool['id'], channel['id'] if channel else None,
                                             offer.get('origin'), end - start + 1, start, end, start,
                                             'Agency auto-allocation', now_iso(), now_iso()))
                    conn.execute("UPDATE pss_stock_pools SET next_serial=?, updated_at=? WHERE id=?", (end + 1, now_iso(), pool['id']))
                    ticket = self._pss_next_ticket(conn, offer['tenant_id'], channel['id'] if channel else None, offer.get('origin'))
            if ticket:
                conn.execute("UPDATE pss_bookings SET ticket_number=?, ticket_status='issued', updated_at=? WHERE id=?", (ticket, now_iso(), bid))
            self._trace(conn, offer['tenant_id'], 'agency_direct', 'pss_booking', bid, 'booking_created',
                        offer.get('agency_name'), f"PNR {pnr} · offer {offer['offer_no']}", trace_id, self)
            try:
                self._erp_sync_pss_booking(conn, offer['tenant_id'], bid, actor='agency_direct', source='agency_direct')
            except Exception:
                pass
        return self._json({'ok': True, 'booking_id': bid, 'pnr': pnr, 'trip_pnr': pnr,
                           'ticket_number': ticket, 'trace_id': trace_id, 'source': 'agency_direct',
                           'payment_status': 'pending', 'total_amount': fare + taxes, 'currency': offer.get('currency') or 'USD'})

    def api_agency_offer_bookings(self, token):
        with db() as conn:
            offer = self._agency_offer(conn, token)
            if not offer:
                return self._json({'error': 'Direct e-ticket link not found or expired'}, 404)
            rows = q(conn, "SELECT id,pnr,passenger_name,ticket_number,total_amount,currency,payment_status,booking_status,trace_id,created_at FROM pss_bookings WHERE tenant_id=? AND offer_id=? ORDER BY id DESC",
                     (offer['tenant_id'], offer['id']))
            tenant = q(conn, "SELECT code,name FROM tenants WHERE id=?", (offer['tenant_id'],), one=True) or {}
        return self._json({'offer': offer, 'bookings': rows, 'airline': tenant})

    def api_pss_agencies(self, tenant_id, method, aid=None):
        with db() as conn:
            if method == 'GET' and not aid:
                return self._json({'agencies': q(conn, "SELECT * FROM pss_agencies WHERE tenant_id=? ORDER BY id DESC", (tenant_id,))})
            aid = self._oid(aid)
            if method == 'GET':
                row = q(conn, "SELECT * FROM pss_agencies WHERE id=? AND tenant_id=?", (aid, tenant_id), one=True)
                if not row:
                    return self._json({'error': 'Agency not found'}, 404)
                rows = q(conn, "SELECT pnr,passenger_name,ticket_number,total_amount,booking_status FROM pss_bookings WHERE tenant_id=? AND agency_id=? ORDER BY id DESC", (tenant_id, aid))
                return self._json({'agency': row, 'bookings': rows})
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_agencies WHERE id=? AND tenant_id=?", (aid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            fields = ['code','name','contact_email','contact_phone','commission_rate','allowed_channels','status','login_token','notes']
            vals = [data.get(f) for f in fields]
            if aid:
                conn.execute("UPDATE pss_agencies SET " + ','.join(f + '=?' for f in fields) + ",updated_at=? WHERE id=? AND tenant_id=?", vals + [now_iso(), aid, tenant_id])
            else:
                if not data.get('name'):
                    return self._json({'error': 'name required'}, 400)
                code = data.get('code') or ('AGY-' + os.urandom(3).hex().upper())
                token = data.get('login_token') or ('AGY-' + os.urandom(8).hex())
                vals[fields.index('login_token')] = token
                conn.execute("INSERT INTO pss_agencies(tenant_id," + ','.join(fields) + ",created_at,updated_at) VALUES (" + ','.join(['?'] * (len(fields) + 3)) + ")",
                             [tenant_id] + vals + [now_iso(), now_iso()])
                aid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
        return self._json({'ok': True, 'agency_id': aid})

    def api_pss_directs(self, tenant_id, method, did=None, user_id=None):
        with db() as conn:
            if method == 'GET' and not did:
                rows = q(conn, """SELECT o.*, a.name agency_name, f.flight_no FROM pss_eticket_directs o
                                  LEFT JOIN pss_agencies a ON a.id=o.agency_id LEFT JOIN pss_flights f ON f.id=o.flight_id
                                  WHERE o.tenant_id=? ORDER BY o.id DESC""", (tenant_id,))
                return self._json({'offers': rows})
            did = self._oid(did)
            if method == 'GET':
                row = q(conn, "SELECT * FROM pss_eticket_directs WHERE id=? AND tenant_id=?", (did, tenant_id), one=True)
                if not row:
                    return self._json({'error': 'Offer not found'}, 404)
                return self._json({'offer': row})
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_eticket_directs WHERE id=? AND tenant_id=?", (did, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if method == 'PUT':
                conn.execute("UPDATE pss_eticket_directs SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                             (data.get('status') or 'active', now_iso(), did, tenant_id))
                return self._json({'ok': True})
            agency = self._oid(data.get('agency_id'))
            flight = self._oid(data.get('flight_id'))
            if not agency:
                return self._json({'error': 'agency_id required'}, 400)
            offer_no = data.get('offer_no') or self._psn('DIR')
            token = data.get('token') or ('DIR-' + os.urandom(10).hex())
            conn.execute("""INSERT INTO pss_eticket_directs(tenant_id,offer_no,agency_id,flight_id,fare_amount,taxes,currency,quantity,used_count,valid_until,status,token,notes,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                         (tenant_id, offer_no, agency, flight, float(data.get('fare_amount') or 0), float(data.get('taxes') or 0),
                          data.get('currency') or 'USD', int(data.get('quantity') or 1), data.get('valid_until'),
                          data.get('status') or 'active', token, data.get('notes'), user_id, now_iso(), now_iso()))
            did = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'counter', 'pss_eticket_direct', did, 'offer_created', user_id, offer_no, None, self)
        return self._json({'ok': True, 'offer_id': did, 'token': token, 'url': f'/agency/{token}'})

    def api_pss_charters(self, tenant_id, method, rid=None, user_id=None):
        with db() as conn:
            if method == 'GET' and not rid:
                rows = q(conn, """SELECT r.*, (SELECT COUNT(*) FROM pss_charter_flights f WHERE f.request_id=r.id) flights
                                  FROM pss_charter_requests r WHERE r.tenant_id=? ORDER BY r.id DESC""", (tenant_id,))
                return self._json({'requests': rows})
            rid = self._oid(rid)
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_charter_requests WHERE id=? AND tenant_id=?", (rid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if method == 'PUT':
                conn.execute("""UPDATE pss_charter_requests SET client_name=?,contact_email=?,contact_phone=?,origin=?,destination=?,flight_date=?,pax_count=?,aircraft_type=?,requirements=?,status=?,quoted_amount=?,currency=?,assigned_to=?,notes=?,updated_at=?
                                WHERE id=? AND tenant_id=?""",
                             (data.get('client_name'), data.get('contact_email'), data.get('contact_phone'), data.get('origin'),
                              data.get('destination'), data.get('flight_date'), data.get('pax_count'), data.get('aircraft_type'),
                              data.get('requirements'), data.get('status'), data.get('quoted_amount'), data.get('currency'),
                              data.get('assigned_to'), data.get('notes'), now_iso(), rid, tenant_id))
                return self._json({'ok': True})
            if not data.get('client_name'):
                return self._json({'error': 'client_name required'}, 400)
            request_no = data.get('request_no') or self._psn('CHR')
            conn.execute("""INSERT INTO pss_charter_requests(tenant_id,request_no,client_name,contact_email,contact_phone,origin,destination,flight_date,pax_count,aircraft_type,requirements,status,quoted_amount,currency,assigned_to,notes,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',0,?,?,?,?,?,?)""",
                         (tenant_id, request_no, data.get('client_name'), data.get('contact_email'), data.get('contact_phone'),
                          data.get('origin'), data.get('destination'), data.get('flight_date'), data.get('pax_count'),
                          data.get('aircraft_type'), data.get('requirements'), data.get('currency') or 'USD', data.get('assigned_to'),
                          data.get('notes'), user_id, now_iso(), now_iso()))
            rid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'charter', 'pss_charter_request', rid, 'request_created', user_id, request_no, None, self)
        return self._json({'ok': True, 'request_id': rid, 'request_no': request_no})

    def api_pss_charter_detail(self, tenant_id, rid):
        rid = self._oid(rid)
        with db() as conn:
            req = q(conn, "SELECT * FROM pss_charter_requests WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
            if not req:
                return self._json({'error': 'Charter request not found'}, 404)
            flights = q(conn, "SELECT * FROM pss_charter_flights WHERE request_id=? AND tenant_id=? ORDER BY id DESC", (rid, tenant_id))
        return self._json({'request': req, 'flights': flights})

    def api_pss_charter_quote(self, tenant_id, user_id, rid):
        rid = self._oid(rid)
        data = read_json(self)
        amount = float(data.get('quoted_amount') or 0)
        with db() as conn:
            conn.execute("UPDATE pss_charter_requests SET quoted_amount=?,status='quoted',updated_at=? WHERE id=? AND tenant_id=?",
                         (amount, now_iso(), rid, tenant_id))
            self._trace(conn, tenant_id, 'charter', 'pss_charter_request', rid, 'quote_issued', user_id, str(amount), None, self)
        return self._json({'ok': True})

    def api_pss_charter_confirm(self, tenant_id, user_id, rid):
        rid = self._oid(rid)
        data = read_json(self)
        with db() as conn:
            req = q(conn, "SELECT * FROM pss_charter_requests WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
            if not req:
                return self._json({'error': 'Charter request not found'}, 404)
            charter_no = data.get('charter_no') or self._psn('CF')
            total = float(data.get('total_amount') or req.get('quoted_amount') or 0)
            conn.execute("""INSERT INTO pss_charter_flights(tenant_id,charter_no,request_id,flight_no,origin,destination,flight_date,dep_time,arr_time,aircraft,seats,charterer,contract_no,total_amount,currency,status,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scheduled',?,?,?)""",
                         (tenant_id, charter_no, rid, data.get('flight_no') or charter_no, data.get('origin') or req.get('origin'),
                          data.get('destination') or req.get('destination'), data.get('flight_date') or req.get('flight_date'),
                          data.get('dep_time'), data.get('arr_time'), data.get('aircraft') or req.get('aircraft_type'),
                          data.get('seats') or req.get('pax_count'), req.get('client_name'), data.get('contract_no'),
                          total, req.get('currency') or 'USD', user_id, now_iso(), now_iso()))
            cid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            conn.execute("UPDATE pss_charter_requests SET status='confirmed', updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), rid, tenant_id))
            self._trace(conn, tenant_id, 'charter', 'pss_charter_flight', cid, 'charter_confirmed', user_id, charter_no, None, self)
        return self._json({'ok': True, 'charter_id': cid, 'charter_no': charter_no})

    def api_pss_charter_status(self, tenant_id, user_id, rid, status):
        rid = self._oid(rid)
        with db() as conn:
            conn.execute("UPDATE pss_charter_flights SET status=?, updated_at=? WHERE id=? AND tenant_id=?", (status, now_iso(), rid, tenant_id))
            conn.execute("UPDATE pss_charter_requests SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                         ('completed' if status == 'completed' else status, now_iso(),
                          (q(conn, "SELECT request_id FROM pss_charter_flights WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) or {}).get('request_id'), tenant_id))
            self._trace(conn, tenant_id, 'charter', 'pss_charter_flight', rid, 'status_' + status, user_id, status, None, self)
        return self._json({'ok': True})

    def api_pss_charter_passenger(self, tenant_id, user_id, cid):
        cid = self._oid(cid)
        data = read_json(self)
        if not data.get('name'):
            return self._json({'error': 'name required'}, 400)
        with db() as conn:
            if not q(conn, "SELECT 1 FROM pss_charter_flights WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True):
                first = q(conn, "SELECT id FROM pss_charter_flights WHERE request_id=? AND tenant_id=? ORDER BY id DESC LIMIT 1", (cid, tenant_id), one=True)
                if not first:
                    return self._json({'error': 'Confirm the charter before adding passengers'}, 400)
                cid = first['id']
            conn.execute("""INSERT INTO pss_charter_passengers(tenant_id,charter_id,name,doc_no,seat_no,meal_pref,ticket_number,checkin_status,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
                         (tenant_id, cid, data.get('name'), data.get('doc_no'), data.get('seat_no'), data.get('meal_pref'),
                          data.get('ticket_number'), now_iso(), now_iso()))
            pid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'charter', 'pss_charter_passenger', pid, 'passenger_added', user_id, data.get('name'), None, self)
        return self._json({'ok': True, 'passenger_id': pid})

    def api_pss_charter_flights(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT f.*, r.client_name FROM pss_charter_flights f
                              LEFT JOIN pss_charter_requests r ON r.id=f.request_id
                              WHERE f.tenant_id=? ORDER BY f.id DESC""", (tenant_id,))
        return self._json({'charter_flights': rows})

    def api_pss_trace(self, tenant_id, qs):
        source = (qs.get('source') or [''])[0]
        action = (qs.get('action') or [''])[0]
        sql = "SELECT * FROM pss_trace WHERE tenant_id=?"
        args = [tenant_id]
        if source:
            sql += " AND source=?"; args.append(source)
        if action:
            sql += " AND action LIKE ?"; args.append('%' + action + '%')
        sql += " ORDER BY id DESC LIMIT 500"
        with db() as conn:
            rows = q(conn, sql, args)
        return self._json({'trace': rows})

    def api_pss_trace_detail(self, tenant_id, trace_id):
        with db() as conn:
            rows = q(conn, "SELECT * FROM pss_trace WHERE tenant_id=? AND trace_id=? ORDER BY id", (tenant_id, trace_id))
        return self._json({'trace_id': trace_id, 'events': rows})


    # ---------------- Ancillary services, airport info, after-sales ----------------
    def api_pss_ancillary_sales(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT s.*, a.name ancillary_name, a.category, b.pnr
                              FROM pss_ancillary_sales s LEFT JOIN pss_ancillaries a ON a.id=s.ancillary_id
                              LEFT JOIN pss_bookings b ON b.id=s.booking_id
                              WHERE s.tenant_id=? ORDER BY s.id DESC LIMIT 500""", (tenant_id,))
        return self._json({'sales': rows})

    def api_pss_booking_ancillaries(self, tenant_id, bid):
        bid = self._oid(bid)
        with db() as conn:
            rows = q(conn, """SELECT s.*, a.name ancillary_name, a.category FROM pss_ancillary_sales s
                              LEFT JOIN pss_ancillaries a ON a.id=s.ancillary_id
                              WHERE s.tenant_id=? AND s.booking_id=? ORDER BY s.id DESC""", (tenant_id, bid))
        return self._json({'sales': rows})

    def api_pss_sell_ancillary(self, tenant_id, user_id, bid):
        bid = self._oid(bid)
        data = read_json(self)
        qty = int(data.get('quantity') or 1)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not b:
                return self._json({'error': 'Booking not found'}, 404)
            aid = self._oid(data.get('ancillary_id'))
            item = q(conn, "SELECT * FROM pss_ancillaries WHERE id=? AND tenant_id=?", (aid, tenant_id), one=True)
            if not item:
                return self._json({'error': 'Ancillary not found'}, 404)
            unit = float(data.get('unit_price') or item.get('price') or 0)
            amount = unit * qty
            sale_no = self._psn('ANC')
            trace_id = b.get('trace_id') or self._new_trace_id()
            conn.execute("""INSERT INTO pss_ancillary_sales(tenant_id,sale_no,booking_id,ancillary_id,description,quantity,unit_price,amount,currency,status,payment_status,trace_id,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,'sold',?,?,?,?,?)""",
                         (tenant_id, sale_no, bid, aid, item.get('name'), qty, unit, amount,
                          item.get('currency') or 'USD', data.get('payment_status') or 'pending', trace_id, user_id, now_iso(), now_iso()))
            sid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'counter', 'pss_ancillary_sale', sid, 'ancillary_sold', user_id,
                        f"{item.get('name')} x{qty} · {amount}", trace_id, self)
        return self._json({'ok': True, 'sale_id': sid, 'sale_no': sale_no, 'amount': amount})

    def api_pss_add_baggage(self, tenant_id, user_id, bid):
        bid = self._oid(bid)
        data = read_json(self)
        weight = float(data.get('weight_kg') or 0)
        pieces = int(data.get('pieces') or 1)
        if weight <= 0:
            return self._json({'error': 'weight_kg required'}, 400)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not b:
                return self._json({'error': 'Booking not found'}, 404)
            rule = q(conn, """SELECT * FROM pss_baggage_rules WHERE tenant_id=? AND (flight_id=? OR flight_id IS NULL)
                              ORDER BY flight_id DESC LIMIT 1""", (tenant_id, b.get('flight_id')), one=True) or {}
            free_kg = float(rule.get('free_kg') or 23) * pieces
            rate = float(rule.get('excess_rate_per_kg') or 12)
            max_kg = float(rule.get('max_kg') or 90) * pieces
            if weight > max_kg:
                return self._json({'error': f'Weight exceeds maximum of {max_kg} kg'}, 400)
            excess = max(0.0, weight - free_kg)
            charge = round(excess * rate, 2)
            desc = f"Baggage {weight}kg ({pieces} piece(s)) · free {free_kg}kg · excess {excess}kg"
            sale_no = self._psn('BAG')
            trace_id = b.get('trace_id') or self._new_trace_id()
            conn.execute("""INSERT INTO pss_ancillary_sales(tenant_id,sale_no,booking_id,ancillary_id,description,quantity,unit_price,amount,currency,status,payment_status,trace_id,created_by,created_at,updated_at)
                            VALUES (?,?,?,NULL,?,1,?,?,?,'sold',?,?,?,?,?)""",
                         (tenant_id, sale_no, bid, desc, rate, charge, b.get('currency') or 'USD',
                          data.get('payment_status') or 'pending', trace_id, user_id, now_iso(), now_iso()))
            sid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'counter', 'pss_ancillary_sale', sid, 'baggage_allocated', user_id,
                        f"{desc} · charge {charge}", trace_id, self)
        return self._json({'ok': True, 'sale_id': sid, 'sale_no': sale_no, 'free_kg': free_kg,
                           'excess_kg': excess, 'rate_per_kg': rate, 'charge': charge})

    def api_pss_airport_info(self, tenant_id, qs):
        code = (qs.get('airport') or qs.get('code') or [''])[0].strip().upper()
        with db() as conn:
            docs = q(conn, "SELECT doc_type,title,requirement,applies_to FROM pss_airport_docs WHERE tenant_id=? AND airport_code=? AND status='active' ORDER BY id", (tenant_id, code))
            weather = q(conn, "SELECT condition,temperature_c,wind_kph,visibility_km,advisory,observed_at FROM pss_weather WHERE tenant_id=? AND airport_code=? ORDER BY id DESC LIMIT 1", (tenant_id, code))
            airports = q(conn, "SELECT code,name,city,terminal,timezone FROM pss_airports WHERE tenant_id=? AND code=?", (tenant_id, code))
        return self._json({'airport': airports[0] if airports else {'code': code}, 'documents': docs,
                           'weather': weather[0] if weather else {}})

    def api_pss_service_cases(self, tenant_id, method, cid=None, user_id=None):
        with db() as conn:
            if method == 'GET' and not cid:
                rows = q(conn, """SELECT c.*, b.pnr FROM pss_service_cases c LEFT JOIN pss_bookings b ON b.id=c.booking_id
                                  WHERE c.tenant_id=? ORDER BY c.id DESC LIMIT 500""", (tenant_id,))
                return self._json({'cases': rows})
            cid = self._oid(cid)
            if method == 'GET':
                row = q(conn, "SELECT * FROM pss_service_cases WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True)
                if not row:
                    return self._json({'error': 'Case not found'}, 404)
                return self._json({'case': row})
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_service_cases WHERE id=? AND tenant_id=?", (cid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if method == 'PUT':
                fields = ['case_type','subject','description','priority','status','satisfaction_score','assigned_to','channel','passenger_name']
                conn.execute("UPDATE pss_service_cases SET " + ','.join(f + '=?' for f in fields) + ",updated_at=? WHERE id=? AND tenant_id=?",
                             [data.get(f) for f in fields] + [now_iso(), cid, tenant_id])
                return self._json({'ok': True})
            case_no = data.get('case_no') or self._psn('CASE')
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (self._oid(data.get('booking_id')), tenant_id), one=True)
            trace_id = (b or {}).get('trace_id') or self._new_trace_id()
            conn.execute("""INSERT INTO pss_service_cases(tenant_id,case_no,booking_id,passenger_name,channel,case_type,subject,description,priority,status,satisfaction_score,resolution,assigned_to,trace_id,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,'open',NULL,NULL,?,?,?,?,?)""",
                         (tenant_id, case_no, self._oid(data.get('booking_id')), data.get('passenger_name') or (b or {}).get('passenger_name'),
                          data.get('channel') or (b or {}).get('booking_source') or 'counter', data.get('case_type') or 'feedback',
                          data.get('subject'), data.get('description'), data.get('priority') or 'medium',
                          data.get('assigned_to'), trace_id, user_id, now_iso(), now_iso()))
            cid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'after_sales', 'pss_service_case', cid, 'case_opened', user_id, case_no, trace_id, self)
        return self._json({'ok': True, 'case_id': cid, 'case_no': case_no})

    def api_pss_case_resolve(self, tenant_id, user_id, cid):
        cid = self._oid(cid)
        data = read_json(self)
        score = data.get('satisfaction_score')
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_service_cases WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Case not found'}, 404)
            conn.execute("UPDATE pss_service_cases SET status='resolved',resolution=?,satisfaction_score=?,resolved_at=?,updated_at=? WHERE id=? AND tenant_id=?",
                         (data.get('resolution'), score, now_iso(), now_iso(), cid, tenant_id))
            self._trace(conn, tenant_id, 'after_sales', 'pss_service_case', cid, 'case_resolved', user_id,
                        str(data.get('resolution') or '')[:80], row.get('trace_id'), self)
        return self._json({'ok': True})

    def api_pss_eticket_tracker(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT b.id,b.pnr,b.passenger_name,f.flight_no,b.ticket_number,b.ticket_status,b.payment_status,
                                     b.booking_status,b.booking_source,b.trace_id,b.created_at,
                                     (SELECT COUNT(*) FROM pss_trace t WHERE t.trace_id=b.trace_id) trace_events
                              FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                              WHERE b.tenant_id=? ORDER BY b.id DESC LIMIT 500""", (tenant_id,))
            issued = sum(1 for r in rows if r.get('ticket_status') == 'issued')
            unissued = sum(1 for r in rows if r.get('ticket_status') != 'issued')
        return self._json({'bookings': rows, 'issued': issued, 'unissued': unissued})



    def api_pss_ticket_status(self, tenant_id, user_id, bid):
        bid = self._oid(bid)
        data = read_json(self)
        status = (data.get('ticket_status') or data.get('status') or '').strip().lower()
        allowed = ('instant', 'issued', 'open_for_use', 'flown', 'no_show', 'void', 'refunded', 'reissued', 'cancelled')
        if status not in allowed:
            return self._json({'error': 'Invalid ticket status. Use: ' + ', '.join(allowed)}, 400)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not b:
                return self._json({'error': 'Booking not found'}, 404)
            previous = b.get('ticket_status') or 'unissued'
            if status == 'instant' and not b.get('ticket_number'):
                ticket = self._pss_next_ticket(conn, tenant_id, b.get('channel_id'), b.get('airport_code'))
                if ticket:
                    conn.execute("UPDATE pss_bookings SET ticket_number=?, updated_at=? WHERE id=?", (ticket, now_iso(), bid))
                    b['ticket_number'] = ticket
            conn.execute("UPDATE pss_bookings SET ticket_status=?, updated_at=? WHERE id=? AND tenant_id=?", (status, now_iso(), bid, tenant_id))
            trace_id = b.get('trace_id') or self._new_trace_id()
            conn.execute("""INSERT INTO pss_ticket_events(tenant_id,booking_id,pnr,previous_status,ticket_status,reason,actor,trace_id,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, bid, b.get('pnr'), previous, status, data.get('reason'), user_id, trace_id, now_iso()))
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'status_' + status, user_id,
                        f"{previous} -> {status}", trace_id, self)
            self._doc_audit(conn, tenant_id, 'ticket_status', b.get('ticket_number') or b.get('pnr'), bid, b.get('pnr'),
                            'status_' + status, user_id, trace_id, {'from': previous, 'to': status})
        return self._json({'ok': True, 'ticket_status': status, 'trace_id': trace_id})


    def api_portal_disruptions(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        with db() as conn:
            rows = q(conn, """SELECT disruption_type,severity,affected_pnr,traveler_name,description,status,created_at
                              FROM disruptions WHERE tenant_id=? AND status NOT IN ('resolved','closed')
                              ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, id DESC LIMIT 50""", (tenant['id'],))
        return self._json({'disruptions': rows})


    def api_pss_partner_workspace(self, tenant_id, sess):
        sess = sess or {}
        with db() as conn:
            ctx = {'role': sess.get('role'), 'agency_id': sess.get('agency_id'),
                   'station_code': sess.get('station_code'), 'branch_id': sess.get('branch_id')}
            agency = q(conn, "SELECT * FROM pss_agencies WHERE tenant_id=? AND id=?", (tenant_id, ctx['agency_id']), one=True) if ctx['agency_id'] else None
            branch = q(conn, "SELECT * FROM branches WHERE tenant_id=? AND id=?", (tenant_id, ctx['branch_id']), one=True) if ctx['branch_id'] else None
            bookings = q(conn, """SELECT b.id,b.pnr,b.passenger_name,b.ticket_number,b.ticket_status,b.payment_status,b.booking_status,b.total_amount,b.currency,b.created_at
                                  FROM pss_bookings b WHERE b.tenant_id=? AND (? IS NULL OR b.agency_id=?) AND (? IS NULL OR b.airport_code=?)
                                  ORDER BY b.id DESC LIMIT 100""",
                         (tenant_id, ctx['agency_id'], ctx['agency_id'], ctx['station_code'], ctx['station_code']))
            checkins = q(conn, """SELECT ci.checkin_no,ci.pnr,ci.passenger_name,ci.seat_no,ci.status,ci.checked_in_at
                                  FROM pss_checkins ci WHERE ci.tenant_id=? AND (? IS NULL OR ci.pnr IN
                                  (SELECT pnr FROM pss_bookings WHERE tenant_id=? AND airport_code=?)) ORDER BY ci.id DESC LIMIT 100""",
                         (tenant_id, ctx['station_code'], tenant_id, ctx['station_code']))
            channels = q(conn, "SELECT code,name,channel_type,commission_rate,allowed_airports,status FROM pss_channels WHERE tenant_id=?", (tenant_id,))
        return self._json({'context': ctx, 'agency': agency, 'branch': branch,
                           'bookings': bookings, 'checkins': checkins, 'channels': channels})

    def api_pss_partner_users(self, tenant_id, method, uid=None, sess=None):
        sess = sess or {}
        with db() as conn:
            if method == 'GET':
                rows = q(conn, """SELECT u.id,u.username,u.full_name,u.email,u.role,u.status,u.agency_id,u.station_code,
                                         b.name branch_name, a.name agency_name
                                  FROM users u LEFT JOIN branches b ON b.id=u.branch_id
                                  LEFT JOIN pss_agencies a ON a.id=u.agency_id
                                  WHERE u.tenant_id=? AND u.role IN ('agency','station','checkin') ORDER BY u.id DESC""", (tenant_id,))
                return self._json({'users': rows})
            uid = self._oid(uid)
            if method == 'DELETE':
                _invalidate_user_sessions(uid)
                conn.execute("DELETE FROM users WHERE id=? AND tenant_id=?", (uid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if method == 'PUT':
                if data.get('password'):
                    err = _password_policy_error(data.get('password'))
                    if err:
                        return self._json({'error': err}, 400)
                conn.execute("UPDATE users SET full_name=?,email=?,role=?,status=?,agency_id=?,station_code=?,branch_id=? WHERE id=? AND tenant_id=?",
                             (data.get('full_name'), data.get('email'), data.get('role'), data.get('status'),
                              self._oid(data.get('agency_id')), data.get('station_code'), self._oid(data.get('branch_id')), uid, tenant_id))
                return self._json({'ok': True})
            username = (data.get('username') or '').strip()
            password = data.get('password') or secrets.token_urlsafe(10)
            err = _password_policy_error(password)
            if not username or err:
                return self._json({'error': err or 'username required'}, 400)
            exists = q(conn, "SELECT 1 FROM users WHERE tenant_id=? AND username=?", (tenant_id, username), one=True)
            if exists:
                return self._json({'error': 'Username already exists'}, 400)
            conn.execute("""INSERT INTO users(tenant_id,branch_id,username,password,full_name,email,role,status,agency_id,station_code,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, self._oid(data.get('branch_id')), username,
                          _hash_password(password), data.get('full_name'), data.get('email'),
                          data.get('role') or 'agency', 'active', self._oid(data.get('agency_id')),
                          data.get('station_code'), now_iso()))
            new_id = q(conn, "SELECT last_insert_rowid() v", one=True)['v']
            return self._json({'ok': True, 'id': new_id, 'password': password})


    # ---------------- Portal customers, documents, Flutterwave ----------------
    def api_portal_register(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        pw_err = _password_policy_error(password)
        if not email or '@' not in email or pw_err:
            return self._json({'error': pw_err or 'Valid email and password required'}, 400)
        with db() as conn:
            if q(conn, "SELECT 1 FROM portal_customers WHERE tenant_id=? AND email=?", (tenant['id'], email), one=True):
                return self._json({'error': 'Account already exists - please sign in'}, 400)
            conn.execute("""INSERT INTO portal_customers(tenant_id,full_name,email,phone,password_hash,doc_no,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?)""",
                         (tenant['id'], (data.get('full_name') or email.split('@')[0]).strip(), email,
                          data.get('phone'), _hash_password(password),
                          data.get('doc_no'), now_iso(), now_iso()))
        return self._json({'ok': True, 'message': 'Account created - please sign in'})

    def api_portal_login(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        ip = self.client_address[0] if self.client_address else 'unknown'
        key = 'portal:' + hashlib.sha256(str(ip).encode()).hexdigest()[:16]
        locked_until = _login_locked_until(key)
        if locked_until and locked_until > time.time():
            return self._json({'error': 'Too many failed attempts. Try again later.'}, 401)
        with db() as conn:
            c = q(conn, "SELECT * FROM portal_customers WHERE tenant_id=? AND email=? AND status='active'",
                  (tenant['id'], email), one=True)
            ok, _ = _verify_password((c or {}).get('password_hash'), password)
            if not c or not ok:
                _note_login_fail(key)
                return self._json({'error': 'Invalid email or password'}, 401)
            token = make_token()
            conn.execute("INSERT INTO portal_sessions(token,tenant_id,customer_id,created_at,expires_at,client_hash) VALUES (?,?,?,?,?,?)",
                         (token, tenant['id'], c['id'], now_iso(), _session_expiry(), _client_hash(self)))
        _note_login_success(key)
        flags = '; Secure' if _use_secure_cookie(self) else ''
        return self._json({'ok': True, 'token': token, 'customer': {'id': c['id'], 'full_name': c['full_name'], 'email': c['email']}},
                          200, headers={'Set-Cookie': f'psid={token}; Path=/; HttpOnly; SameSite=Lax{flags}'})

    def api_portal_logout(self):
        sess = get_portal_session(self)
        if sess:
            with db() as conn:
                conn.execute("DELETE FROM portal_sessions WHERE token=?", (sess['token'],))
        return self._json({'ok': True}, 200, headers={'Set-Cookie': 'psid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'})

    def api_portal_me(self):
        sess = get_portal_session(self)
        if not sess:
            return self._json({'error': 'Not signed in'}, 401)
        with db() as conn:
            rows = q(conn, """SELECT b.id,b.pnr,b.trip_pnr,b.flight_no,b.origin,b.destination,b.flight_date,b.dep_time,
                                     b.passenger_name,b.ticket_number,b.ticket_status,b.payment_status,b.booking_status,b.total_amount,b.currency
                              FROM pss_bookings b WHERE b.tenant_id=? AND b.portal_customer_id=? ORDER BY b.id DESC LIMIT 100""",
                     (sess['tenant_id'], sess['customer_id']))
        return self._json({'customer': {'id': sess['customer_id'], 'full_name': sess['full_name'], 'email': sess['email']},
                           'bookings': rows})

    def api_portal_documents_get(self, qs):
        pnr = (qs.get('pnr') or [''])[0].strip().upper()
        last = (qs.get('last_name') or [''])[0].strip().lower()
        sess = get_portal_session(self)
        with db() as conn:
            if pnr and last:
                rows = q(conn, """SELECT d.id,d.doc_type,d.doc_no,d.title,d.file_name,d.created_at FROM pss_travel_documents d
                                  WHERE d.pnr=? AND EXISTS (SELECT 1 FROM pss_bookings b WHERE b.id=d.booking_id AND LOWER(b.passenger_name) LIKE ?)
                                  ORDER BY d.id DESC""", (pnr, '%' + last + '%'))
            elif sess:
                rows = q(conn, """SELECT d.id,d.doc_type,d.doc_no,d.title,d.file_name,d.created_at FROM pss_travel_documents d
                                  WHERE d.tenant_id=? AND d.booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND portal_customer_id=?)
                                  ORDER BY d.id DESC""", (sess['tenant_id'], sess['tenant_id'], sess['customer_id']))
            else:
                return self._json({'error': 'Sign in or provide PNR and surname'}, 401)
        return self._json({'documents': rows})

    def api_portal_documents_post(self):
        data = read_json(self)
        pnr = (data.get('pnr') or '').strip().upper()
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE pnr=? OR trip_pnr=? ORDER BY id LIMIT 1", (pnr, pnr), one=True)
            if not b:
                return self._json({'error': 'PNR not found'}, 404)
            file_data = None
            if data.get('file_base64'):
                raw = data['file_base64']
                if isinstance(raw, str) and raw.startswith('data:'):
                    raw = raw.split(',', 1)[-1]
                try:
                    file_data = base64.b64decode(raw)
                except Exception:
                    file_data = None
            conn.execute("""INSERT INTO pss_travel_documents(tenant_id,booking_id,pnr,doc_type,doc_no,title,file_name,mime_type,file_data,notes,uploaded_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,'portal',?,?)""",
                         (b['tenant_id'], b['id'], b['pnr'], data.get('doc_type') or 'passport', data.get('doc_no'),
                          data.get('title') or data.get('doc_type') or 'Travel document', data.get('file_name'),
                          data.get('mime_type'), file_data, data.get('notes'), now_iso(), now_iso()))
        return self._json({'ok': True})

    def _pss_flw_gateway(self, conn, tenant_id):
        return get_billing_gateway(conn, tenant_id=tenant_id, provider='flutterwave')

    def api_pss_flw_init(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        sess = get_portal_session(self)
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        pnr = (data.get('pnr') or '').strip().upper()
        with db() as conn:
            bookings = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (tenant['id'], pnr, pnr))
            if not bookings:
                return self._json({'error': 'PNR not found'}, 404)
            amount = float(data.get('amount') or sum(float(b.get('total_amount') or 0) for b in bookings))
            cur = data.get('currency') or bookings[0].get('currency') or 'USD'
            gw = self._pss_flw_gateway(conn, tenant['id'])
            secret = (gw or {}).get('secret_key') or ''
            tx_ref = 'PSS-' + os.urandom(6).hex().upper()
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,channel_id,amount,currency,method,reference,gateway_ref,status,notes,booking_source,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,'portal',?,?)""",
                         (tenant['id'], tx_ref, bookings[0]['id'], pnr, bookings[0].get('channel_id'), amount, cur,
                          'flutterwave', data.get('reference'), tx_ref,
                          f"{data.get('email') or ''} {data.get('phone') or ''}", now_iso(), now_iso()))
            if not secret:
                return self._json({'ok': True, 'demo': True, 'tx_ref': tx_ref, 'amount': amount,
                                   'url': f'/api/pss/payments/flutterwave/verify?tx_ref={tx_ref}&status=successful'})
            cfg = gw.get('config') or {}
            payload = {'tx_ref': tx_ref, 'amount': amount, 'currency': cur,
                       'email': data.get('email') or bookings[0].get('contact_email') or 'passenger@fayaka.aero',
                       'phonenumber': data.get('phone') or bookings[0].get('contact_phone') or '',
                       'name': bookings[0].get('passenger_name') or 'Fayaka passenger',
                       'payment_options': cfg.get('methods', 'mobilemoney,card,banktransfer,ussd'),
                       'redirect_url': data.get('redirect_url') or f"{cfg.get('site_url','')}/portal/fayaka",
                       'meta': {'pnr': pnr, 'source': 'portal'},
                       'customizations': {'title': 'Fayaka Airline', 'description': f'Flight {pnr}'}}
            status, resp = flutterwave_request('POST', '/payments', secret, payload)
            link = (resp or {}).get('data', {}).get('link') if status in (200, 201) else None
            return self._json({'ok': bool(link), 'tx_ref': tx_ref, 'url': link,
                               'error': (resp or {}).get('message') if not link else None}, 200 if link else 502)

    def _apply_pss_flw(self, conn, tx_ref, gateway_data=None, status='successful'):
        pay = q(conn, "SELECT * FROM pss_payments WHERE gateway_ref=? OR payment_no=?", (tx_ref, tx_ref), one=True)
        if not pay or pay.get('status') in ('captured', 'paid'):
            return False
        conn.execute("UPDATE pss_payments SET status='captured', captured_at=?, paid_at=COALESCE(paid_at,?), gateway_ref=?, raw_json=? WHERE id=?",
                     (now_iso(), tx_ref, json.dumps(gateway_data or {}, default=str), pay['id']))
        rows = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (pay['tenant_id'], pay['pnr'], pay['pnr']))
        total = sum(float(r.get('total_amount') or 0) for r in rows)
        paid = (q(conn, "SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE tenant_id=? AND pnr=? AND status IN ('captured','paid')", (pay['tenant_id'], pay['pnr']), one=True) or {}).get('v', 0)
        ps = 'paid' if paid >= total else 'partial'
        conn.execute("UPDATE pss_bookings SET payment_status=?,updated_at=? WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)",
                     (ps, now_iso(), pay['tenant_id'], pay['pnr'], pay['pnr']))
        if rows:
            self._trace(conn, pay['tenant_id'], 'flutterwave', 'pss_booking', rows[0]['id'], 'payment_captured',
                        'flutterwave', tx_ref, rows[0].get('trace_id'), None)
        for _b in rows:
            try:
                self._erp_sync_pss_booking(conn, pay['tenant_id'], _b['id'], actor='flutterwave', source=_b.get('booking_source') or 'portal')
            except Exception:
                pass
        try:
            self._erp_sync_pss_payment(conn, pay['tenant_id'], pay['id'], actor='flutterwave', source=pay.get('booking_source') or 'portal')
        except Exception:
            pass
        return True

    def api_pss_flw_verify(self, qs):
        tx_ref = (qs.get('tx_ref') or [''])[0]
        status = (qs.get('status') or [''])[0]
        with db() as conn:
            pay = q(conn, "SELECT * FROM pss_payments WHERE gateway_ref=? OR payment_no=?", (tx_ref, tx_ref), one=True)
            if not pay:
                return self._json({'error': 'Payment reference not found'}, 404)
            gw = self._pss_flw_gateway(conn, pay['tenant_id'])
            secret = (gw or {}).get('secret_key') or ''
            verified = status.lower() in ('successful', 'success') or qs.get('status') == ['successful']
            gateway_data = {'tx_ref': tx_ref, 'status': status}
            if secret and not verified:
                vstatus, vresp = flutterwave_verify(secret, tx_ref=tx_ref)
                verified = vstatus == 200 and str((vresp or {}).get('data', {}).get('status')).lower() == 'successful'
                gateway_data = vresp or gateway_data
            if verified:
                self._apply_pss_flw(conn, tx_ref, gateway_data, 'successful')
        return self._json({'ok': True, 'verified': bool(verified), 'tx_ref': tx_ref})

    def api_pss_travel_documents(self, tenant_id, qs):
        pnr = (qs.get('pnr') or [''])[0]
        with db() as conn:
            sql = "SELECT id,booking_id,pnr,doc_type,doc_no,title,file_name,created_at FROM pss_travel_documents WHERE tenant_id=?"
            args = [tenant_id]
            if pnr:
                sql += " AND pnr=?"; args.append(pnr)
            sql += " ORDER BY id DESC LIMIT 200"
            rows = q(conn, sql, args)
        return self._json({'documents': rows})

    def api_pss_partner_clients(self, tenant_id, sess):
        agency_id = (sess or {}).get('agency_id')
        with db() as conn:
            rows = q(conn, """SELECT b.id,b.pnr,b.passenger_name,b.contact_email,b.contact_phone,b.ticket_number,
                                     b.ticket_status,b.payment_status,b.booking_status,b.total_amount,b.currency,b.flight_id,b.created_at
                              FROM pss_bookings b WHERE b.tenant_id=? AND b.agency_id=? ORDER BY b.id DESC LIMIT 300""",
                     (tenant_id, agency_id))
        return self._json({'clients': rows})

    def api_pss_partner_amend(self, tenant_id, sess, bid):
        bid = self._oid(bid)
        data = read_json(self)
        agency_id = (sess or {}).get('agency_id')
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=? AND agency_id=?", (bid, tenant_id, agency_id), one=True)
            if not b:
                return self._json({'error': 'Booking not found for your agency'}, 404)
            fields = ['passenger_name', 'contact_email', 'contact_phone', 'doc_no', 'fare_class']
            vals = [data.get(f) for f in fields if f in data]
            sets = [f + '=?' for f in fields if f in data]
            if sets:
                conn.execute("UPDATE pss_bookings SET " + ','.join(sets) + ",updated_at=? WHERE id=? AND tenant_id=?",
                             vals + [now_iso(), bid, tenant_id])
            self._trace(conn, tenant_id, 'agency_direct', 'pss_booking', bid, 'amended', sess.get('username'),
                        ', '.join(f for f in fields if f in data), b.get('trace_id'), None)
        return self._json({'ok': True})

    def api_pss_partner_flw(self, tenant_id, sess):
        data = read_json(self)
        bid = self._oid(data.get('booking_id'))
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=? AND agency_id=?", (bid, tenant_id, (sess or {}).get('agency_id')), one=True)
            if not b:
                return self._json({'error': 'Booking not found for your agency'}, 404)
            gw = self._pss_flw_gateway(conn, tenant_id)
            secret = (gw or {}).get('secret_key') or ''
            amount = float(data.get('amount') or b.get('total_amount') or 0)
            tx_ref = 'PSS-' + os.urandom(6).hex().upper()
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,channel_id,amount,currency,method,reference,gateway_ref,status,booking_source,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,'flutterwave',?,?,'pending','agency_direct',?,?)""",
                         (tenant_id, tx_ref, bid, b['pnr'], b.get('channel_id'), amount,
                          data.get('currency') or b.get('currency') or 'USD', data.get('reference'), tx_ref, now_iso(), now_iso()))
            if not secret:
                return self._json({'ok': True, 'demo': True, 'tx_ref': tx_ref,
                                   'url': f'/api/pss/payments/flutterwave/verify?tx_ref={tx_ref}&status=successful'})
            channel = q(conn, "SELECT name FROM pss_channels WHERE id=?", (b.get('channel_id'),), one=True) if b.get('channel_id') else None
            payload = {'tx_ref': tx_ref, 'amount': amount, 'currency': data.get('currency') or b.get('currency') or 'USD',
                       'email': data.get('email') or b.get('contact_email') or 'agency@fayaka.aero',
                       'phonenumber': data.get('phone') or b.get('contact_phone') or '',
                       'name': b.get('passenger_name') or 'Fayaka passenger',
                       'payment_options': 'mobilemoney,card,banktransfer,ussd',
                       'redirect_url': data.get('redirect_url') or '',
                       'meta': {'pnr': b['pnr'], 'source': 'agency_direct', 'channel': (channel or {}).get('name', '')},
                       'customizations': {'title': 'Fayaka Airline', 'description': f'Agency payment {b["pnr"]}'}}
            status, resp = flutterwave_request('POST', '/payments', secret, payload)
            link = (resp or {}).get('data', {}).get('link') if status in (200, 201) else None
            return self._json({'ok': bool(link), 'tx_ref': tx_ref, 'url': link}, 200 if link else 502)


    def api_pss_financials(self, tenant_id, user_id, bid, method):
        bid = self._oid(bid)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            if not b:
                return self._json({'error': 'Booking not found'}, 404)
            if method == 'GET':
                return self._json({'booking': b,
                                   'tax_lines': q(conn, "SELECT * FROM pss_tax_lines WHERE tenant_id=? AND booking_id=? ORDER BY id", (tenant_id, bid))})
            data = read_json(self)
            fields = [f for f in ('fare_basis', 'commission', 'service_fee', 'baggage_allowance_kg', 'taxes') if f in data]
            if fields:
                conn.execute("UPDATE pss_bookings SET " + ','.join(f + '=?' for f in fields) + ",updated_at=? WHERE id=? AND tenant_id=?",
                             [data.get(f) for f in fields] + [now_iso(), bid, tenant_id])
            for item in (data.get('tax_lines') or []):
                conn.execute("""INSERT INTO pss_tax_lines(tenant_id,booking_id,tax_code,description,amount,currency,created_at)
                                VALUES (?,?,?,?,?,?,?)""",
                             (tenant_id, bid, item.get('tax_code') or 'OTH', item.get('description'),
                              float(item.get('amount') or 0), item.get('currency') or b.get('currency') or 'USD', now_iso()))
            self._doc_audit(conn, tenant_id, 'financials', b.get('pnr'), bid, b.get('pnr'),
                            'financials_updated', user_id, b.get('trace_id'), data)
        return self._json({'ok': True})

    def api_pss_document_audit(self, tenant_id, qs):
        pnr = (qs.get('pnr') or [''])[0]
        with db() as conn:
            sql = "SELECT * FROM pss_document_audit WHERE tenant_id=?"
            args = [tenant_id]
            if pnr:
                sql += " AND pnr=?"; args.append(pnr)
            sql += " ORDER BY id DESC LIMIT 300"
            rows = q(conn, sql, args)
        return self._json({'audit': rows})


WEAK_SEEDS = {'admin', 'agent', 'partner123', 'agency123', 'desk123', 'team123',
              'office-kla', 'office-dxb', 'office-nbo', 'KLA'}


def _rotate_legacy_credentials():
    try:
        conn = get_conn()
    except Exception:
        return
    try:
        legacy_hashes = {hashlib.sha256(s.encode()).hexdigest() for s in WEAK_SEEDS}
        for u in q(conn, "SELECT id, tenant_id, username, password FROM users"):
            pw = u.get('password') or ''
            if not pw or pw.startswith('v2$'):
                continue
            if pw in WEAK_SEEDS or pw in legacy_hashes:
                new = secrets.token_urlsafe(14)
                conn.execute("UPDATE users SET password=? WHERE id=?",
                             (_hash_password(new), u['id']))
                print(f"[security] Rotated seeded credentials - tenant={u.get('tenant_id')} user={u['username']}: new password={new}",
                      file=sys.stderr)
            else:
                conn.execute("UPDATE users SET password=? WHERE id=?",
                             (_hash_password(pw), u['id']))
        for c in q(conn, "SELECT id, email, password_hash FROM portal_customers"):
            pw = c.get('password_hash') or ''
            if not pw or pw.startswith('v2$'):
                continue
            if pw in legacy_hashes:
                new = secrets.token_urlsafe(10)
                conn.execute("UPDATE portal_customers SET password_hash=?, updated_at=? WHERE id=?",
                             (_hash_password(new), now_iso(), c['id']))
                print(f"[security] Rotated seeded portal account - {c.get('email')}: new password={new}", file=sys.stderr)
            else:
                conn.execute("UPDATE portal_customers SET password_hash=?, updated_at=? WHERE id=?",
                             (_hash_password(pw), now_iso(), c['id']))
        conn.commit()
    finally:
        conn.close()


def run():
    init_db()
    _ensure_portal_session_cols()
    _rotate_legacy_credentials()
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8000'))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Airline PSS & Travel ERP running on http://{host}:{port}")
    print(f"Workspace root for .AIR scan: {WORKSPACE_ROOT}")
    if _SECURE_COOKIES_ENV is not None and _SECURE_COOKIES_ENV not in ('0', 'false', 'no'):
        print("Session cookies use the Secure flag (set SECURE_COOKIES=0 for plain-http internal use)")
    elif _SECURE_COOKIES_ENV is None:
        print("Session cookies: Secure auto-detected (loopback plain HTTP allowed; set SECURE_COOKIES=1 to force)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    run()
