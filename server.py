import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import signal
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
import zipfile
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, date

from db import init_db, db, q, now_iso, log_audit, get_conn, seed_pss, USING_POSTGRES, REQUIRE_POSTGRES, db_set_session, db_get_session, db_delete_session, db_delete_user_sessions
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


REPORT_SOURCES = {
    'pss-bookings': 'pss_bookings', 'pss-flights': 'pss_flights', 'flight-inventory': 'pss_inventory_legs',
    'pss-stock': 'pss_stock_pools', 'pss-checkins': 'pss_checkins', 'pss-payments': 'pss_payments',
    'payments': 'payments', 'travelers': 'travelers', 'orders': 'orders', 'crm': 'customers',
    'invoicing': 'invoices', 'estimates': 'estimates', 'credit-notes': 'credit_notes',
    'bills': 'bills', 'expenses': 'expenses', 'journals': 'journals', 'documents': 'documents',
    'shares': 'document_shares', 'notifications': 'notifications', 'refunds': 'refunds',
    'coupons': 'coupons', 'vouchers': 'vouchers', 'bank-accounts': 'bank_accounts',
    'bank-transactions': 'bank_transactions', 'reconciliations': 'reconciliations',
    'pss-agencies': 'pss_agencies', 'pss-ancillaries': 'pss_ancillaries',
    'pss-ancillary-sales': 'pss_ancillary_sales', 'pss-charters': 'pss_charter_requests',
    'pss-charter-flights': 'pss_charter_flights', 'pss-trace': 'pss_trace',
    'pss-direct': 'pss_eticket_directs', 'pss-eticket-tracker': 'pss_bookings',
    'pss-ticket-status': 'pss_bookings', 'pss-aftersales': 'pss_service_cases',
    'pss-airports': 'pss_airports', 'pss-baggage': 'pss_baggage_rules',
    'pss-airport-docs': 'pss_airport_docs', 'pss-weather': 'pss_weather',
    'pss-branches': 'branches', 'pss-workspace': 'pss_agencies', 'pss-partner-users': 'users',
    'partners': 'partners', 'suppliers': 'contacts', 'setup-contacts': 'contacts',
    'sources': 'connectors', 'backoffice': 'tasks', 'tasks': 'tasks', 'setup-users': 'users',
    'setup-chart': 'chart_of_accounts', 'setup-products': 'products', 'setup-currencies': 'currencies',
    'setup-rules': 'business_rules', 'setup-preferences': 'preferences', 'setup-mail': 'mail_templates',
    'setup-integrations': 'integrations', 'loyalty': 'portal_customers', 'partner-reports': 'pss_agencies'
}
REPORT_EXCLUDE_COLUMNS = {'password', 'password_hash', 'file_data', 'raw_payload', 'payload', 'token',
                          'secret', 'api_key', 'webhook_secret', 'session_id', 'csrf', 'access_token',
                          'refresh_token', 'private_key', 'login_token', 'config_json', 'raw_json',
                          'secret_key', 'signature_secret', 'password_reset_token', 'remember_token'}

REPORT_ACCOUNTING = {'payments', 'invoicing', 'credit-notes', 'bills', 'expenses', 'journals', 'refunds',
                     'coupons', 'vouchers', 'bank-accounts', 'bank-transactions', 'reconciliations',
                     'loyalty', 'documents', 'shares', 'notifications', 'setup-chart'}
REPORT_AGENCY = {'pss-bookings', 'pss-flights', 'flight-inventory', 'pss-checkins', 'pss-payments',
                 'payments', 'orders', 'crm', 'invoicing', 'estimates', 'credit-notes', 'documents',
                 'tasks', 'notifications', 'shares', 'partner-reports', 'pss-agencies', 'pss-trace',
                 'pss-ancillaries', 'pss-charter-flights', 'pss-direct', 'pss-eticket-tracker'}
REPORT_RESTRICTED = {'setup-users', 'pss-partner-users', 'sources', 'setup-integrations', 'setup-mail',
                     'setup-rules', 'setup-preferences', 'admin', 'developer', 'onboarding'}


def _report_allowed(entity, group):
    if group == 'airline_admin':
        return True
    if group == 'airline_accountant':
        return entity in REPORT_ACCOUNTING
    if group in ('airline_reservations', 'airline_operations'):
        return entity not in REPORT_RESTRICTED
    if group in ('agency_admin', 'agency_agent'):
        return entity in REPORT_AGENCY
    return False


def _report_value(v):
    if v is None or isinstance(v, (bytes, bytearray)):
        return ''
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return v


def _report_label(c):
    return str(c).replace('_', ' ').strip().title()


def _xml_text(v):
    s = ''.join(ch for ch in str(v) if ch in '\t\n\r' or ord(ch) >= 32)
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&apos;'))


def _xlsx_column(i):
    s = ''
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _build_xlsx(headers, rows):
    def row_xml(values, number):
        cells = [f'<row r="{number}">']
        for i, v in enumerate(values, 1):
            if v is None or isinstance(v, (bytes, bytearray)):
                continue
            ref = f'{_xlsx_column(i)}{number}'
            if isinstance(v, bool):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{_xml_text(str(v))}</t></is></c>')
            elif isinstance(v, (int, float)):
                if isinstance(v, float) and (v != v or v == float('inf') or v == float('-inf')):
                    v = str(v)
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{_xml_text(v)}</t></is></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_text(v)}</t></is></c>')
        cells.append('</row>')
        return ''.join(cells)
    sheet = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>',
             row_xml(headers, 1)]
    for idx, row in enumerate(rows, 2):
        sheet.append(row_xml(row, idx))
    sheet.append('</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   '</Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                   '</Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', ''.join(sheet))
    return buf.getvalue()



USER_ROLES = ('owner','admin','accountant','reservations','agent','operations','checkin','station',
              'agency_admin','agency_agent','agency')


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
    'airline_accountant': ('/api/dashboard', '/api/analytics', '/api/revenue/', '/api/reports/', '/api/finance/', '/api/invoices',
                            '/api/credit-notes', '/api/payments', '/api/bills', '/api/expenses', '/api/loyalty/',
                             '/api/setup/chart-of-accounts', '/api/setup/refunds', '/api/setup/coupons',
                            '/api/setup/vouchers', '/api/setup/bank-accounts', '/api/setup/bank-transactions',
                            '/api/setup/reconciliations', '/api/setup/unused-tickets',
                             '/api/setup/fare-optimization', '/api/automation/run', '/api/documents',
                             '/api/shares', '/api/tasks', '/api/notifications', '/api/session', '/api/logout'),
    'airline_reservations': ('/api/dashboard', '/api/analytics', '/api/revenue/', '/api/reports/', '/api/global-search', '/api/orders',
                             '/api/customers', '/api/travelers', '/api/pss/', '/api/documents',
                                                            '/api/shares', '/api/tasks', '/api/notifications', '/api/session', '/api/logout',
                               '/api/setup/unused-tickets', '/api/setup/fare-optimization', '/api/setup/disruptions',
                               '/api/setup/pss-weather', '/api/automation/run'),
    'airline_operations': ('/api/dashboard', '/api/analytics', '/api/revenue/', '/api/reports/', '/api/global-search', '/api/pss/',
                            '/api/orders', '/api/travelers', '/api/tasks', '/api/notifications',
                                                          '/api/documents', '/api/session', '/api/logout',
                                                             '/api/setup/unused-tickets', '/api/setup/fare-optimization', '/api/setup/disruptions',
                               '/api/setup/pss-weather', '/api/automation/run'),
    'agency_admin': ('/api/session', '/api/logout', '/api/dashboard', '/api/reports/', '/api/pss/partner-workspace',
                     '/api/pss/partner-clients', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                     '/api/pss/partner-users', '/api/pss/bookings', '/api/pss/flights', '/api/pss/stock', '/api/pss/allocations', '/api/pss/wallets',
                     '/api/customers', '/api/estimates', '/api/invoices', '/api/payments',
                     '/api/finance/summary', '/api/credit-notes', '/api/tasks', '/api/notifications',
                     '/api/documents', '/api/shares', '/api/public/share/', '/api/pss/airport-info'),
    'agency_agent': ('/api/session', '/api/logout', '/api/dashboard', '/api/reports/', '/api/pss/partner-workspace',
                     '/api/pss/partner-clients', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                     '/api/pss/bookings', '/api/pss/flights', '/api/pss/stock', '/api/pss/allocations', '/api/pss/wallets',
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


def _today():
    return date.today().isoformat()


WEBHOOK_EVENTS = (
    'offer.created', 'order.created', 'order.cancelled', 'booking.created',
    'payment.received', 'disruption.created', 'rebooking.confirmed', 'tenant.onboarded'
)


def _json_load(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _event_list(value):
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or '').split(',') if v.strip()]


def _hash_api_key(token):
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def _webhook_event(tenant_id, event, payload, object_id=None):
    try:
        with db() as conn:
            subscribers = q(conn, "SELECT * FROM pss_webhooks WHERE tenant_id=? AND status='active'", (tenant_id,))
            body = json.dumps({
                'id': str(uuid.uuid4()),
                'type': event,
                'created_at': now_iso(),
                'data': payload,
            }, default=str)
            for hook in subscribers:
                events = _event_list(hook.get('events'))
                if event not in events and '*' not in events:
                    continue
                conn.execute("""INSERT INTO pss_webhook_deliveries
                    (tenant_id,webhook_id,event,object_id,payload,status,attempts,created_at)
                    VALUES (?,?,?,?,?,'pending',0,?)""",
                    (tenant_id, hook['id'], event, object_id or '', body, now_iso()))
    except Exception as exc:
        print(f"[webhook] queue failed: {exc}", file=sys.stderr)
    try:
        _deliver_webhooks(tenant_id)
    except Exception as exc:
        print(f"[webhook] delivery failed: {exc}", file=sys.stderr)


def _deliver_webhooks(tenant_id=None):
    params = [] if tenant_id is None else [tenant_id]
    where = "WHERE d.status IN ('pending','failed') AND (d.next_retry_at IS NULL OR d.next_retry_at<=?)"
    args = [now_iso()] + params
    if tenant_id is not None:
        where += " AND d.tenant_id=?"
    with db() as conn:
        rows = q(conn, f"""SELECT d.*, w.url, w.secret FROM pss_webhook_deliveries d
                           JOIN pss_webhooks w ON w.id=d.webhook_id {where}
                           ORDER BY d.id LIMIT 25""", tuple(args))
        for row in rows:
            attempts = int(row.get('attempts') or 0) + 1
            signature = hmac.new((row.get('secret') or '').encode('utf-8'),
                                 (row.get('payload') or '').encode('utf-8'), hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                row.get('url') or '',
                data=(row.get('payload') or '').encode('utf-8'),
                headers={'Content-Type': 'application/json', 'X-Fayaka-Event': row.get('event') or '',
                         'X-Fayaka-Signature': f'sha256={signature}'},
                method='POST')
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    code = getattr(resp, 'status', 200)
                    body = resp.read(500).decode('utf-8', 'ignore')
                conn.execute("""UPDATE pss_webhook_deliveries SET status='delivered',attempts=?,response_code=?,
                                response_body=?,delivered_at=?,next_retry_at=NULL WHERE id=?""",
                             (attempts, code, body, now_iso(), row['id']))
            except Exception as exc:
                delay = min(300, 15 * (2 ** min(attempts, 5)))
                retry_at = (datetime.utcnow() + timedelta(seconds=delay)).replace(microsecond=0).isoformat() + 'Z'
                conn.execute("""UPDATE pss_webhook_deliveries SET status='failed',attempts=?,response_body=?,
                                next_retry_at=? WHERE id=?""",
                             (attempts, str(exc)[:500], retry_at, row['id']))


def _webhook_worker():
    while True:
        time.sleep(20)
        try:
            _deliver_webhooks()
        except Exception as exc:
            print(f"[webhook] worker error: {exc}", file=sys.stderr)


class PayloadTooLarge(Exception):
    pass


SESSION_TTL = 7 * 86400
SESSION_IDLE = 25 * 60
_SECURE_COOKIES_ENV = os.environ.get('SECURE_COOKIES')
PBKDF2_ITERATIONS = 210_000
LOGIN_LOCKOUT_MAX = int(os.environ.get('LOGIN_LOCKOUT_MAX', '5'))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get('LOGIN_LOCKOUT_SECONDS', '900'))
LOGIN_LOCKOUT_ENABLED = os.environ.get('LOGIN_LOCKOUT_DISABLED', '').lower() not in ('1', 'true', 'yes') and LOGIN_LOCKOUT_MAX > 0
MAX_BODY = int(os.environ.get('MAX_BODY_MB', '10')) * 1024 * 1024
PORT = int(os.environ.get('PORT', 8000))
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY and REQUIRE_POSTGRES:
    raise RuntimeError("SECRET_KEY is required in production (Railway). Set SECRET_KEY env variable.")
if not SECRET_KEY:
    SECRET_KEY = 'dev-change-me-in-prod'
DEFAULT_TENANT = os.environ.get('DEFAULT_TENANT', 'fayaka')
API_BASE = os.environ.get('API_BASE', '')
if REQUIRE_POSTGRES and not API_BASE:
    API_BASE = ''


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


def _client_ip(handler):
    headers = getattr(handler, 'headers', None)
    for name in ('CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-For'):
        val = (headers.get(name) or '').strip() if headers else ''
        if not val:
            continue
        val = val.split(',')[0].strip().strip('"').strip("'")
        if val:
            return val
    try:
        return handler.client_address[0] if handler.client_address else 'unknown'
    except Exception:
        return 'unknown'


def _login_ident(handler, scope, account):
    ip = _client_ip(handler)
    return scope + ':' + hashlib.sha256(f"{ip}|{(account or '').lower()}".encode()).hexdigest()[:16]


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
    if not LOGIN_LOCKOUT_ENABLED:
        return
    with _LOGIN_FAIL_LOCK:
        lst = _LOGIN_FAILS.setdefault(key, [])
        lst.append(time.time())
        del lst[:-LOGIN_LOCKOUT_MAX]


def _login_locked_until(key):
    if not LOGIN_LOCKOUT_ENABLED:
        return None
    with _LOGIN_FAIL_LOCK:
        lst = _LOGIN_FAILS.get(key) or []
        cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
        recent = [t for t in lst if t >= cutoff]
        return recent[-1] + LOGIN_LOCKOUT_SECONDS if len(recent) >= LOGIN_LOCKOUT_MAX else None


def _note_login_success(key):
    with _LOGIN_FAIL_LOCK:
        _LOGIN_FAILS.pop(key, None)


def _session_expiry(days=90):
    from datetime import timedelta
    return (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat() + "Z"


def _invalidate_user_sessions(user_id):
    if not user_id:
        return
    if USING_POSTGRES:
        try:
            with db() as conn:
                db_delete_user_sessions(conn, user_id)
        except Exception:
            pass
        return
    with SESS_LOCK:
        for sid in [t for t, s in SESSIONS.items() if s.get('user_id') == user_id]:
            SESSIONS.pop(sid, None)


def get_session(handler):
    if USING_POSTGRES:
        try:
            cookie = SimpleCookie(handler.headers.get('Cookie'))
        except Exception:
            return None
        if 'sid' not in cookie:
            return None
        sid = cookie['sid'].value
        now = time.time()
        with db() as conn:
            row = db_get_session(conn, sid)
            if not row:
                return None
            import json as _json
            sess = _json.loads(row['data']) if isinstance(row['data'], str) else dict(row)
            if sess.get('client_hash') and sess['client_hash'] != _client_hash(handler):
                pass  # allow session across proxy IP changes on Railway
            sess['last_activity'] = now
            db_set_session(conn, sid, sess.get('tenant_id'), sess.get('user_id'),
                           _json.dumps(sess), sess.get('expires_at', now + SESSION_TTL),
                           sess.get('created_at', now_iso()), now_iso())
            return sess
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
        currency = (gateway_data or {}).get('currency') or payment_row.get('currency') or inv.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD'
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
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
                          "base-uri 'self'; frame-ancestors 'self'; form-action 'self'")
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
        h = dict(headers or {})
        origin = self.headers.get('Origin')
        allow_origin = os.environ.get('CORS_ALLOW_ORIGIN')
        if not allow_origin and not REQUIRE_POSTGRES:
            allow_origin = '*'
        if allow_origin:
            h['Access-Control-Allow-Origin'] = origin if allow_origin == '*' and origin else allow_origin
            h['Access-Control-Allow-Headers'] = 'Content-Type, X-CSRF-Token, Authorization'
            h['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            h['Access-Control-Allow-Credentials'] = 'true'
        self._send(b, st, ct, headers=h)

    def do_OPTIONS(self):
        origin = self.headers.get('Origin')
        allow_origin = os.environ.get('CORS_ALLOW_ORIGIN')
        if not allow_origin and not REQUIRE_POSTGRES:
            allow_origin = '*'
        h = {}
        if allow_origin:
            h['Access-Control-Allow-Origin'] = origin if allow_origin == '*' and origin else allow_origin
            h['Access-Control-Allow-Headers'] = 'Content-Type, X-CSRF-Token, Authorization'
            h['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            h['Access-Control-Allow-Credentials'] = 'true'
        self._send(b'', 204, '', headers=h)

    def _serve_static(self, path):
        if path == '/':
            path = '/index.html'
        static_root = os.path.normpath(STATIC_DIR) + os.sep
        fs = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip('/')))
        if not fs.startswith(static_root) or not os.path.isfile(fs):
            self._json({'error': 'Not found'}, 404)
            return
        ext = os.path.splitext(fs)[1].lower()
        explicit_types = {
            '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
            '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.ico': 'image/x-icon',
            '.pdf': 'application/pdf', '.txt': 'text/plain; charset=utf-8',
            '.woff': 'font/woff', '.woff2': 'font/woff2',
        }
        ctype = explicit_types.get(ext)
        if not ctype:
            guessed = mimetypes.guess_type(fs)[0] or 'application/octet-stream'
            ctype = f'{guessed}; charset=utf-8' if guessed.startswith('text/') else guessed
        with open(fs, 'rb') as f:
            b = f.read()
        headers = None
        if ext in ('.html', '.htm'):
            headers = {'Set-Cookie': f'csrftoken={make_token()}; Path=/; SameSite=Lax'}
        self._send(b, 200, ctype, headers=headers)

    def _serve_docs(self):
        html = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fayaka Partner API</title><style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}main{max-width:1040px;margin:auto}h1{color:#7dd3fc}pre{background:#020617;padding:16px;border-radius:10px;overflow:auto}.card{background:#1e293b;padding:18px;border-radius:12px;margin:14px 0}code{color:#fbbf24}.badge{background:#0ea5e9;color:#04121f;padding:3px 8px;border-radius:999px;font-size:12px}</style></head>
<body><main><h1>Fayaka Partner API</h1><p>Offer, order, payment and disruption workflows for small airlines.</p><div id="root">Loading API catalog...</div></main>
<script>fetch('/api/public/v1/docs').then(r=>r.json()).then(d=>{document.getElementById('root').innerHTML='<div class="card"><span class="badge">v'+d.version+'</span><p><b>Auth:</b> '+d.auth+'</p><p><b>Base URL:</b> <code>'+location.origin+'</code></p></div>'+d.endpoints.map(e=>'<div class="card"><h3><code>'+e.method+' '+e.path+'</code></h3><p>'+e.description+'</p><pre>'+JSON.stringify(e.example,null,2)+'</pre></div>').join('')+'<div class="card"><h2>Webhook events</h2><pre>'+JSON.stringify(d.webhook_events,null,2)+'</pre><p>Signature header: <code>X-Fayaka-Signature: sha256=&lt;HMAC-SHA256 of raw body&gt;</code></p></div>';}).catch(e=>document.getElementById('root').textContent=e.message);</script></body></html>"""
        self._send(html.encode('utf-8'), 200, 'text/html; charset=utf-8',
                   headers={'Set-Cookie': f'csrftoken={make_token()}; Path=/; SameSite=Lax'})

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
        if path == '/docs' or path == '/docs/':
            return self._serve_docs()
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
            if (method in ('POST', 'PUT', 'DELETE', 'PATCH')
                    and path != '/api/billing/flutterwave/webhook'
                    and not path.startswith('/api/public/v1/')):
                if not self._csrf_ok(path, qs):
                    return self._json({'error': 'CSRF token missing or invalid'}, 403)
            if path == '/api/health':
                return self._json({'status': 'ok', 'tenant': DEFAULT_TENANT, 'db': 'postgres' if USING_POSTGRES else 'sqlite'})
            if path == '/api/public/v1/docs' and method == 'GET':
                return self.api_public_docs()
            if path == '/api/public/v1/offers' and method == 'POST':
                return self.api_public_offer_create()
            if path.startswith('/api/public/v1/offers/') and path.endswith('/orders') and method == 'POST':
                return self.api_public_order_create(path.split('/')[5])
            if path.startswith('/api/public/v1/offers/') and method == 'GET':
                return self.api_public_offer_get(path.split('/')[5])
            if path.startswith('/api/public/v1/orders/') and path.endswith('/cancel') and method == 'POST':
                return self.api_public_order_cancel(path.split('/')[5])
            if path.startswith('/api/public/v1/orders/') and method == 'GET':
                return self.api_public_order_get(path.split('/')[5])
            if path == '/api/portal/disruptions' and method == 'GET':
                return self.api_portal_disruptions(qs)
            if path == '/api/portal/unused-tickets' and method == 'GET':
                return self.api_portal_unused_tickets(qs)
            if path == '/api/portal/ticket-status' and method == 'GET':
                return self.api_portal_ticket_status(qs)
            if path == '/api/portal/suppliers' and method == 'GET':
                return self.api_portal_suppliers(qs)
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
            if path.startswith('/api/portal/documents/') and path.endswith('/download') and method == 'GET':
                return self.api_portal_document_download(path.strip('/').split('/')[3], qs)
            if path == '/api/portal/documents' and method == 'POST':
                return self.api_portal_documents_post()
            if path == '/api/pss/payments/flutterwave/init' and method == 'POST':
                return self.api_pss_flw_init()
            if path == '/api/pss/payments/flutterwave/verify' and method == 'GET':
                return self.api_pss_flw_verify(qs)
            if path == '/api/portal/branding' and method == 'GET':
                return self.api_portal_branding(qs)
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
                        '/api/pss/partner-clients/', '/api/pss/bookings/', '/api/pss/wallets/topup', '/api/customers', '/api/estimates', '/api/invoices',
                        '/api/payments', '/api/credit-notes', '/api/tasks', '/api/notifications', '/api/documents', '/api/shares')):
                    return self._json({'error': 'Access denied for your role'}, 403)
                if group == 'agency_admin' and method != 'GET' and not path.startswith((
                        '/api/login', '/api/logout', '/api/pss/partner-bookings/', '/api/pss/partner-payments/',
                        '/api/pss/partner-clients/', '/api/pss/partner-users', '/api/pss/bookings/', '/api/pss/wallets/topup', '/api/customers', '/api/estimates',
                        '/api/invoices', '/api/payments', '/api/credit-notes', '/api/tasks', '/api/notifications',
                        '/api/documents', '/api/shares')):
                    return self._json({'error': 'Access denied for your role'}, 403)
                if group == 'viewer' and method != 'GET' and not path.startswith('/api/notifications/read'):
                    return self._json({'error': 'View-only access'}, 403)

            if path == '/api/dashboard' and method == 'GET':
                return self.api_dashboard(tenant_id)
            if path == '/api/analytics' and method == 'GET':
                return self.api_analytics(tenant_id)
            if path == '/api/revenue/dashboard' and method == 'GET':
                return self.api_revenue_dashboard(tenant_id, qs)
            if path == '/api/reports/export' and method == 'GET':
                return self.api_report_export(tenant_id, qs, user_id)
            if path == '/api/loyalty/report' and method == 'GET':
                return self.api_loyalty_report(tenant_id)
            if path == '/api/pss/partner-reports' and method == 'GET':
                return self.api_partner_reports(tenant_id)
            if path == '/api/developer/api-keys' and method == 'GET':
                return self.api_developer_api_keys(tenant_id, 'GET')
            if path == '/api/developer/api-keys' and method == 'POST':
                return self.api_developer_api_keys(tenant_id, 'POST', user_id=user_id)
            if path.startswith('/api/developer/api-keys/') and method == 'DELETE':
                return self.api_developer_api_keys(tenant_id, 'DELETE', path.rsplit('/', 1)[-1])
            if path == '/api/developer/webhooks' and method == 'GET':
                return self.api_developer_webhooks(tenant_id, 'GET')
            if path == '/api/developer/webhooks' and method == 'POST':
                return self.api_developer_webhooks(tenant_id, 'POST', user_id=user_id)
            if path.startswith('/api/developer/webhooks/') and path.endswith('/retry') and method == 'POST':
                return self.api_developer_webhook_retry(tenant_id, path.split('/')[4])
            if path.startswith('/api/developer/webhooks/') and method in ('PUT', 'DELETE'):
                return self.api_developer_webhooks(tenant_id, method, path.rsplit('/', 1)[-1], user_id=user_id)
            if path == '/api/developer/deliveries' and method == 'GET':
                return self.api_developer_deliveries(tenant_id)
            if path == '/api/developer/docs' and method == 'GET':
                return self.api_public_docs()
            if path == '/api/onboarding/status' and method in ('GET', 'POST'):
                return self.api_onboarding_status(tenant_id, method, user_id=user_id)
            if path == '/api/onboarding/create-tenant' and method == 'POST':
                return self.api_onboarding_create_tenant(tenant_id, user_id)
            if path == '/api/pss/disruptions/impact' and method == 'GET':
                return self.api_disruption_impact(tenant_id, qs)
            if path == '/api/pss/disruptions/rebook' and method == 'POST':
                return self.api_disruption_rebook(tenant_id, user_id)
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
            if path == '/api/pss/charter-flights' and method == 'POST':
                return self.api_pss_charter_flight_mutate(tenant_id, None, 'POST', user_id)
            if path.startswith('/api/pss/charter-flights/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_pss_charter_flight_mutate(tenant_id, path.rsplit('/', 1)[-1], method, user_id)
            if path == '/api/pss/trace' and method == 'GET':
                return self.api_pss_trace(tenant_id, qs)
            if path.startswith('/api/pss/trace/') and method == 'GET':
                return self.api_pss_trace_detail(tenant_id, path.rsplit('/', 1)[-1])
            if path == '/api/pss/travel-documents' and method == 'GET':
                return self.api_pss_travel_documents(tenant_id, qs)
            if path == '/api/pss/travel-documents' and method == 'POST':
                return self.api_pss_travel_document_create(tenant_id, user_id)
            if path.startswith('/api/pss/travel-documents/') and path.endswith('/download') and method == 'GET':
                return self.api_pss_travel_document_download(tenant_id, path.strip('/').split('/')[3], qs)
            if path.startswith('/api/pss/travel-documents/') and method in ('PUT', 'DELETE'):
                return self.api_pss_travel_document_mutate(tenant_id, user_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/pss/travel-documents/reminders' and method == 'POST':
                return self.api_pss_travel_expiry_reminders(tenant_id, user_id)
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
            if path.startswith('/api/pss/bookings/') and method in ('PUT','PATCH'):
                return self.api_pss_bookings(tenant_id, 'PUT', bid=path.rsplit('/',1)[-1], user_id=user_id, sess=sess)
            if path == '/api/pss/seat-holds' and method == 'GET':
                return self.api_pss_seat_holds(tenant_id)
            if path == '/api/pss/seat-holds' and method == 'POST':
                return self.api_pss_seat_hold_create(tenant_id, user_id)
            if path.startswith('/api/pss/seat-holds/') and method == 'DELETE':
                return self.api_pss_seat_hold_release(tenant_id, user_id, path.rsplit('/', 1)[-1])
            if path == '/api/pss/inventory/dates' and method == 'GET':
                return self.api_inventory_dates(tenant_id, user_id)
            if path == '/api/pss/inventory/legs' and method == 'POST':
                return self.api_inventory_legs(tenant_id, user_id, 'POST')
            if path.startswith('/api/pss/inventory/legs/') and method in ('PUT', 'DELETE'):
                return self.api_inventory_legs(tenant_id, user_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/inventory/segments' and method == 'POST':
                return self.api_inventory_segments(tenant_id, user_id, 'POST')
            if path.startswith('/api/pss/inventory/segments/') and method in ('PUT', 'DELETE'):
                return self.api_inventory_segments(tenant_id, user_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/inventory/availability' and method == 'GET':
                return self.api_inventory_availability(tenant_id, qs)
            if path == '/api/pss/inventory/availability/publish' and method == 'POST':
                return self.api_inventory_availability_publish(tenant_id, user_id)
            if path == '/api/pss/inventory/rules' and method == 'GET':
                return self.api_inventory_rules(tenant_id)
            if path == '/api/pss/inventory/rules' and method == 'POST':
                return self.api_inventory_rules(tenant_id, user_id, 'POST')
            if path.startswith('/api/pss/inventory/rules/') and method in ('PUT', 'DELETE'):
                return self.api_inventory_rules(tenant_id, user_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/inventory/mass-update' and method == 'POST':
                return self.api_inventory_mass_update(tenant_id, user_id)
            if path == '/api/pss/inventory/overbooking' and method == 'GET':
                return self.api_inventory_overbooking(tenant_id)
            if path == '/api/pss/inventory/overbooking/resolve' and method == 'POST':
                return self.api_inventory_overbooking_resolve(tenant_id, user_id)
            if path == '/api/pss/inventory/history' and method == 'GET':
                return self.api_inventory_history(tenant_id)
            if path == '/api/pss/inventory/queue' and method == 'GET':
                return self.api_inventory_queue(tenant_id)
            if path.startswith('/api/pss/inventory/queue/') and path.endswith('/resolve') and method == 'POST':
                return self.api_inventory_queue_resolve(tenant_id, user_id, path.split('/')[5])
            if path.startswith('/api/pss/inventory/queue/') and method == 'PUT':
                return self.api_inventory_queue(tenant_id, 'PUT', path.split('/')[5], user_id)
            if path == '/api/pss/inventory/valuation' and method == 'GET':
                return self.api_inventory_valuation(tenant_id)
            if path == '/api/pss/inventory/valuation' and method == 'POST':
                return self.api_inventory_valuation(tenant_id, user_id, 'POST')
            if path.startswith('/api/pss/inventory/valuation/') and method in ('PUT', 'DELETE'):
                return self.api_inventory_valuation(tenant_id, user_id, method, path.rsplit('/', 1)[-1])
            if path == '/api/pss/inventory/uncommitted' and method == 'GET':
                return self.api_inventory_uncommitted(tenant_id, user_id)
            if path.startswith('/api/pss/inventory/uncommitted/') and path.endswith('/release') and method == 'POST':
                return self.api_inventory_uncommitted_action(tenant_id, user_id, path.split('/')[5], 'release')
            if path.startswith('/api/pss/inventory/uncommitted/') and path.endswith('/confirm') and method == 'POST':
                return self.api_inventory_uncommitted_action(tenant_id, user_id, path.split('/')[5], 'confirm')
            if path == '/api/pss/inventory/market' and method == 'GET':
                return self.api_inventory_market(tenant_id)
            if path == '/api/pss/inventory/market/refresh' and method == 'POST':
                return self.api_inventory_market_refresh(tenant_id)
            if path == '/api/pss/inventory/help' and method == 'GET':
                return self.api_inventory_help()
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
            if path == '/api/pss/baggage/tracking' and method == 'GET':
                return self.api_pss_baggage_tracking(tenant_id, qs)
            if path.startswith('/api/pss/baggage/') and path.endswith('/scan') and method == 'POST':
                return self.api_pss_baggage_scan(tenant_id, path.split('/')[4], user_id)
            if path.startswith('/api/pss/checkins/') and method == 'GET' and path.count('/') == 4:
                return self.api_pss_checkin_baggage(tenant_id, path.split('/')[4])
            if path.startswith('/api/pss/checkins/') and path.endswith('/baggage') and method == 'GET':
                return self.api_pss_checkin_baggage(tenant_id, path.split('/')[4])
            if path.startswith('/api/pss/checkins/') and path.endswith('/baggage') and method == 'POST':
                return self.api_pss_checkin_baggage_mutate(tenant_id, path.split('/')[4], user_id)
            if path.startswith('/api/pss/checkins/') and path.endswith('/status') and method == 'POST':
                return self.api_pss_checkin_status(tenant_id, path.split('/')[4], user_id)
            if path == '/api/pss/checkins' and method == 'GET':
                return self.api_pss_checkins(tenant_id)
            if path == '/api/pss/checkins' and method == 'POST':
                return self.api_pss_checkin_create(tenant_id, user_id, sess=sess)
            if path == '/api/pss/payments' and method == 'GET':
                return self.api_pss_payments(tenant_id, sess=sess)
            if path == '/api/pss/payments' and method == 'POST':
                return self.api_pss_payment_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/pss/payments/') and method in ('GET', 'PUT'):
                return self.api_pss_payment_mutate(tenant_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/pss/stock' and method == 'GET':
                return self.api_pss_stock(tenant_id, sess=sess)
            if path == '/api/pss/stock' and method == 'POST':
                return self.api_pss_stock_create(tenant_id, user_id)
            if path == '/api/pss/allocations' and method == 'GET':
                return self.api_pss_allocations(tenant_id, sess=sess)
            if path == '/api/pss/stock/allocate' and method == 'POST':
                return self.api_pss_stock_allocate(tenant_id, user_id, sess=sess)
            if path == '/api/pss/stock/issue' and method == 'POST':
                return self.api_pss_stock_issue(tenant_id, user_id, sess=sess)
            if path == '/api/pss/wallets' and method == 'GET':
                return self.api_pss_wallets(tenant_id, sess=sess)
            if path == '/api/pss/wallets/topup' and method == 'POST':
                return self.api_pss_wallet_topup_init(tenant_id, user_id, sess=sess)
            if path == '/api/pss/wallets/topup/verify' and method == 'GET':
                return self.api_pss_wallet_topup_verify(tenant_id, qs, sess=sess)
            if path == '/api/pss/wallets/transactions' and method == 'GET':
                return self.api_pss_wallet_transactions(tenant_id, qs, sess=sess)
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
            if path.startswith('/api/invoices/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_invoice_mutate(tenant_id, user_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path == '/api/payments' and method == 'GET':
                return self.api_payments(tenant_id, sess=sess)
            if path == '/api/payments' and method == 'POST':
                return self.api_payment_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/payments/') and method in ('GET', 'PUT', 'DELETE'):
                return self.api_payment_mutate(tenant_id, user_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path.startswith('/api/credit-notes/') and method == 'GET':
                return self.api_generic_detail(tenant_id, 'credit_notes', path.rsplit('/', 1)[-1], sess=sess)
            if path.startswith('/api/estimates/') and method == 'GET':
                return self.api_generic_detail(tenant_id, 'estimates', path.rsplit('/', 1)[-1], sess=sess)
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
            if path.startswith('/api/customers/') and method == 'GET':
                with db() as conn:
                    cust = q(conn, "SELECT * FROM customers WHERE id=? AND tenant_id=?", (self._oid(path.rsplit('/', 1)[-1]), tenant_id), one=True)
                if not cust or not self._row_allowed(cust, sess):
                    return self._json({'error': 'Not found'}, 404)
                return self._json({'customer': cust})
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
            if path == '/api/setup/unused-tickets' and method == 'GET':
                return self.api_unused_tickets(tenant_id)
            if path == '/api/setup/unused-tickets/refund' and method == 'POST':
                return self.api_unused_ticket_refund(tenant_id, user_id)
            if path == '/api/setup/fare-optimization' and method in ('GET', 'POST'):
                return self.api_fare_optimization(tenant_id, user_id, apply=method == 'POST')
            if path == '/api/automation/run' and method == 'POST':
                return self.api_run_automation(tenant_id, user_id)
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
                return self.api_document_download(tenant_id, path.split('/')[3], qs)
            if path.startswith('/api/documents/') and method in ('PUT', 'DELETE'):
                return self.api_document_mutate(tenant_id, path.rsplit('/', 1)[-1], method)
            if path == '/api/shares' and method == 'GET':
                return self.api_shares_list(tenant_id)
            if path == '/api/shares' and method == 'POST':
                return self.api_share_create(tenant_id, user_id)
            if path.startswith('/api/shares/') and method in ('PUT', 'PATCH', 'DELETE'):
                return self.api_share_mutate(tenant_id, path.rsplit('/', 1)[-1], user_id, method)
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
            if path == '/api/bills' and method == 'GET':
                return self.api_bills_list(tenant_id, sess=sess)
            if path == '/api/bills' and method == 'POST':
                return self.api_bill_create(tenant_id, user_id, sess=sess)
            if path == '/api/expenses' and method == 'GET':
                return self.api_expenses_list(tenant_id, sess=sess)
            if path == '/api/expenses' and method == 'POST':
                return self.api_expense_create(tenant_id, user_id, sess=sess)
            if path.startswith('/api/bills/') and path.endswith('/pay') and method == 'POST':
                return self.api_bill_pay(tenant_id, user_id, path.split('/')[3], sess=sess)
            if path.startswith('/api/bills/') and method in ('PUT', 'DELETE'):
                return self.api_bill_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)
            if path.startswith('/api/expenses/') and method in ('PUT', 'DELETE'):
                return self.api_expense_mutate(tenant_id, path.rsplit('/', 1)[-1], method, sess=sess)
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
        fail_key = _login_ident(self, 'airline', f"{tenant_code}|{username}")
        locked_until = _login_locked_until(fail_key)
        if locked_until and locked_until > time.time():
            print(f"[login] locked key={fail_key}", file=sys.stderr)
            retry = max(1, int(locked_until - time.time()))
            return self._json({'error': 'Too many failed attempts. Try again later.', 'retry_after': retry},
                              401, headers={'Retry-After': str(retry)})
        with db() as conn:
            t = q(conn, "SELECT * FROM tenants WHERE code=? AND status='active'", (tenant_code,), one=True)
            if not t:
                print(f"[login] failed tenant={tenant_code} reason=unknown_tenant", file=sys.stderr)
                _note_login_fail(fail_key)
                return self._json({'error': 'Invalid tenant'}, 401)
            u = q(conn, "SELECT * FROM users WHERE tenant_id=? AND username=? AND status='active'",
                  (t['id'], username), one=True)
            ok, upgraded = _verify_password((u or {}).get('password'), password)
            if upgraded:
                conn.execute("UPDATE users SET password=? WHERE id=?", (upgraded, u['id']))
            if not u or not ok:
                print(f"[login] failed tenant={tenant_code} username={username} reason={'no_user' if not u else 'bad_password'}", file=sys.stderr)
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
        if USING_POSTGRES:
            import json as _json
            db_set_session(conn, token, t['id'], u['id'], _json.dumps(sess), now + SESSION_TTL,
                           now_iso(), now_iso())
        else:
            with SESS_LOCK:
                SESSIONS[token] = sess
        flags = '; Secure' if _use_secure_cookie(self) else ''
        headers = {'Set-Cookie': f'sid={token}; Path=/; HttpOnly; SameSite=Lax{flags}'}
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
            if USING_POSTGRES:
                try:
                    with db() as conn:
                        db_delete_session(conn, sid)
                except Exception:
                    pass
            else:
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
        currency = (branding or {}).get('currency') or self._tenant_currency(conn, sess['tenant_id'])
        sess_out = dict(sess)
        sess_out['currency'] = currency
        return self._json({'session': sess_out, 'plan': plan, 'usage': usage, 'branding': branding, 'currency': currency})

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
                               (tenant_id, src, source_id, pnr, pnr, data.get('office_id'), data.get('customer_id'), data.get('product_type') or 'Flight', 'booked', data.get('ticket_status') or 'unticketed', data.get('tktl'), ts[:10], data.get('travel_start'), data.get('travel_end'), data.get('origin'), data.get('destination'), data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', sell, cost, sell-cost, sell, data.get('notes'), ts, ts))
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
                                    (tenant_id, source_type, str(source_id), connector_id, raw_id, pnr, pnr, payload.get('status') or 'booked', payload.get('ticket_status') or 'unticketed', payload.get('origin'), payload.get('destination'), payload.get('travel_start'), payload.get('travel_end'), payload.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', total_sell, total_cost, total_sell-total_cost, total_sell, payload.get('notes'), ts, ts))
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
                                (tenant_id, data.get('branch_id'), order_id, customer_id, inv_no, data.get('kind') or 'invoice', 'issued', ts[:10], due_date, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', subtotal, tax, fees, discount, total, 0, total, data.get('notes'), ts, agency_id, 'agency' if agency_id else 'backoffice'))

            iid = cur.lastrowid
            lines = data.get('lines') or [{"description": data.get('description') or 'Travel services', 'qty':1, 'unit_cost': float(data.get('unit_cost') or 0), 'unit_price': subtotal, 'tax': tax, 'line_total': total}]
            for ln in lines:
                conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                             (tenant_id, iid, ln.get('description'), float(ln.get('qty') or 1), float(ln.get('unit_cost') or 0), float(ln.get('unit_price') or 0), float(ln.get('tax') or 0), float(ln.get('line_total') or 0)))
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'invoice', iid, ts[:10], f"Invoice {inv_no}", 'Accounts Receivable', 'Sales', total, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', ts, agency_id, 'agency' if agency_id else 'backoffice'))
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
                               (tenant_id, data.get('branch_id'), invoice_id, order_id, customer_id, pno, data.get('method') or 'cash', data.get('reference'), amount, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', 'received', ts[:10], data.get('notes'), ts, agency_id, 'agency' if agency_id else 'backoffice'))
            pid = cur.lastrowid
            if invoice_id:
                conn.execute("INSERT INTO allocations(tenant_id,payment_id,invoice_id,amount,created_at) VALUES (?,?,?,?,?)", (tenant_id, pid, invoice_id, amount, ts))
                recompute_invoice(conn, invoice_id)
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'payment', pid, ts[:10], f"Payment {pno}", 'Petty Cash', 'Accounts Receivable', amount, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', ts, agency_id, 'agency' if agency_id else 'backoffice'))
            log_audit(conn, tenant_id, user_id, 'create', 'payment', pid, {'payment_no': pno})
        return self._json({'ok': True, 'payment_id': pid, 'payment_no': pno})

    def api_invoice_mutate(self, tenant_id, user_id, iid, method, sess=None):
        from ingest import recompute_invoice
        iid = self._oid(iid)
        with db() as conn:
            row = q(conn, "SELECT * FROM invoices WHERE id=? AND tenant_id=?", (iid, tenant_id), one=True)
            if not row or not self._row_allowed(row, sess):
                return self._json({'error': 'Invoice not found'}, 404)
            if method == 'GET':
                return self._json({'invoice': row})
            if method == 'DELETE':
                conn.execute("DELETE FROM invoice_lines WHERE tenant_id=? AND invoice_id=?", (tenant_id, iid))
                conn.execute("DELETE FROM journals WHERE tenant_id=? AND ref_type='invoice' AND ref_id=?", (tenant_id, iid))
                conn.execute("DELETE FROM invoices WHERE id=? AND tenant_id=?", (iid, tenant_id))
                log_audit(conn, tenant_id, user_id, 'delete', 'invoice', iid, {'invoice_no': row.get('invoice_no')})
                return self._json({'ok': True})
            data = read_json(self)
            allowed = ('branch_id', 'order_id', 'customer_id', 'invoice_no', 'kind', 'status', 'issue_date',
                       'due_date', 'currency', 'subtotal', 'tax', 'fees', 'discount', 'total', 'notes')
            sets, args = [], []
            for key in allowed:
                if key in data:
                    sets.append(f"{key}=?")
                    args.append(self._oid(data[key]) if key.endswith('_id') else data[key])
            if not sets:
                return self._json({'invoice': row})
            args += [iid, tenant_id]
            conn.execute(f"UPDATE invoices SET {','.join(sets)} WHERE id=? AND tenant_id=?", args)
            recompute_invoice(conn, iid)
            updated = q(conn, "SELECT * FROM invoices WHERE id=? AND tenant_id=?", (iid, tenant_id), one=True)
            log_audit(conn, tenant_id, user_id, 'update', 'invoice', iid, {'invoice_no': updated.get('invoice_no') if updated else row.get('invoice_no')})
        return self._json({'ok': True, 'invoice': updated})

    def api_payment_mutate(self, tenant_id, user_id, pid, method, sess=None):
        from ingest import recompute_invoice
        pid = self._oid(pid)
        with db() as conn:
            row = q(conn, "SELECT * FROM payments WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
            if not row or not self._row_allowed(row, sess):
                return self._json({'error': 'Payment not found'}, 404)
            if method == 'GET':
                return self._json({'payment': row})
            if method == 'DELETE':
                conn.execute("DELETE FROM allocations WHERE tenant_id=? AND payment_id=?", (tenant_id, pid))
                conn.execute("DELETE FROM journals WHERE tenant_id=? AND ref_type='payment' AND ref_id=?", (tenant_id, pid))
                conn.execute("DELETE FROM payments WHERE id=? AND tenant_id=?", (pid, tenant_id))
                if row.get('invoice_id'):
                    recompute_invoice(conn, row['invoice_id'])
                log_audit(conn, tenant_id, user_id, 'delete', 'payment', pid, {'payment_no': row.get('payment_no')})
                return self._json({'ok': True})
            data = read_json(self)
            allowed = ('branch_id', 'invoice_id', 'order_id', 'customer_id', 'payment_no', 'method', 'reference',
                       'amount', 'currency', 'status', 'paid_at', 'notes')
            sets, args = [], []
            for key in allowed:
                if key in data:
                    sets.append(f"{key}=?")
                    args.append(self._oid(data[key]) if key.endswith('_id') else data[key])
            if not sets:
                return self._json({'payment': row})
            args += [pid, tenant_id]
            conn.execute(f"UPDATE payments SET {','.join(sets)} WHERE id=? AND tenant_id=?", args)
            if row.get('invoice_id'):
                recompute_invoice(conn, row['invoice_id'])
            updated = q(conn, "SELECT * FROM payments WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
            log_audit(conn, tenant_id, user_id, 'update', 'payment', pid, {'payment_no': updated.get('payment_no') if updated else row.get('payment_no')})
        return self._json({'ok': True, 'payment': updated})

    def api_generic_detail(self, tenant_id, table, rid, sess=None):
        if table not in ('credit_notes', 'estimates'):
            return self._json({'error': 'Not found'}, 404)
        rid = self._oid(rid)
        with db() as conn:
            row = q(conn, f"SELECT * FROM {table} WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
        if not row or not self._row_allowed(row, sess):
            return self._json({'error': 'Not found'}, 404)
        return self._json({table[:-1]: row})

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
            pnl_sql = """SELECT COALESCE(SUM(CASE WHEN account_type='income' THEN amount ELSE -amount END),0) amount
                         FROM journals j LEFT JOIN chart_of_accounts a ON a.code=j.debit_account AND a.tenant_id=j.tenant_id
                         WHERE j.tenant_id=?"""
            pnl_args = [tenant_id]
            if agency_id:
                pnl_sql += " AND j.agency_id=?"
                pnl_args.append(agency_id)
            pnl = q(conn, pnl_sql, pnl_args, one=True)
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
            bill_sql = """SELECT bill_no supplier_bill, balance, due_date, supplier,
                                 CASE WHEN due_date IS NULL OR due_date>=date('now') THEN 'current'
                                      WHEN julianday('now')-julianday(due_date)<=30 THEN '1-30'
                                      WHEN julianday('now')-julianday(due_date)<=60 THEN '31-60'
                                      WHEN julianday('now')-julianday(due_date)<=90 THEN '61-90' ELSE '90+' END bucket
                                 FROM bills WHERE tenant_id=? AND balance>0"""
            bill_args = [tenant_id]
            if agency_id:
                bill_sql += " AND agency_id=?"
                bill_args.append(agency_id)
            bill_sql += " ORDER BY due_date IS NULL, due_date LIMIT 100"
            bill_aging = q(conn, bill_sql, bill_args)
            accounts = q(conn, "SELECT code,name,account_type,account_group,normal_balance,is_active FROM chart_of_accounts WHERE tenant_id=? ORDER BY code", (tenant_id,))
            cash_sql = "SELECT COALESCE(SUM(amount),0) c FROM journals WHERE tenant_id=? AND (debit_account='Petty Cash' OR credit_account='Petty Cash')"
            cash_args = [tenant_id]
            if agency_id:
                cash_sql += " AND agency_id=?"
                cash_args.append(agency_id)
            cash = q(conn, cash_sql, cash_args, one=True)
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
            currency = (data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD').upper()
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
        if tx_ref and str(tx_ref).upper().startswith('WTX-'):
            with db() as conn:
                tx = q(conn, "SELECT * FROM pss_wallet_transactions WHERE gateway_ref=? OR tx_no=? ORDER BY id DESC LIMIT 1",
                       (str(tx_ref), str(tx_ref)), one=True)
                if not tx:
                    return self._json({'ok': True, 'ignored': True, 'reason': 'wallet transaction not found'})
                gw = get_billing_gateway(conn, tenant_id=tx['tenant_id'], provider='flutterwave')
                if not gw or not gw.get('secret_key'):
                    return self._json({'ok': False, 'message': 'Gateway not configured'}, 400)
                if gw.get('webhook_secret'):
                    sent = self.headers.get('verif-hash') or self.headers.get('Verif-Hash') or ''
                    if not hmac.compare_digest(str(sent), str(gw.get('webhook_secret'))):
                        return self._json({'error': 'Invalid webhook signature'}, 401)
                status, resp = flutterwave_verify(gw.get('secret_key'), tx_ref=str(tx_ref),
                                                  transaction_id=data_obj.get('id') or payload.get('id'))
                vdata = (resp or {}).get('data') or {}
                amount_ok = float(vdata.get('amount') or 0) >= float(tx.get('amount') or 0)
                currency_ok = str(vdata.get('currency') or '').upper() == str(tx.get('currency') or '').upper()
                if (status not in (200, 201) or str(vdata.get('status') or '').lower() != 'successful'
                        or not amount_ok or not currency_ok):
                    return self._json({'ok': False, 'message': 'Payment verification failed'}, 400)
                self._apply_wallet_topup(conn, str(tx_ref), resp)
            return self._json({'ok': True, 'wallet': True})
        if tx_ref and str(tx_ref).upper().startswith('PSS-'):
            with db() as conn:
                pay = q(conn, "SELECT * FROM pss_payments WHERE gateway_ref=? OR payment_no=? OR reference=? ORDER BY id DESC LIMIT 1",
                        (str(tx_ref), str(tx_ref), str(tx_ref)), one=True)
                if not pay:
                    return self._json({'ok': True, 'ignored': True, 'reason': 'payment not found'})
                gw = get_billing_gateway(conn, tenant_id=pay['tenant_id'], provider='flutterwave')
                if not gw or not gw.get('secret_key'):
                    return self._json({'ok': False, 'message': 'Gateway not configured'}, 400)
                if gw.get('webhook_secret'):
                    sent = self.headers.get('verif-hash') or self.headers.get('Verif-Hash') or ''
                    if not hmac.compare_digest(str(sent), str(gw.get('webhook_secret'))):
                        return self._json({'error': 'Invalid webhook signature'}, 401)
                status, resp = flutterwave_verify(gw.get('secret_key'), tx_ref=str(tx_ref),
                                                  transaction_id=data_obj.get('id') or payload.get('id'))
                vdata = (resp or {}).get('data') or {}
                amount_ok = float(vdata.get('amount') or 0) >= float(pay.get('amount') or 0)
                currency_ok = str(vdata.get('currency') or '').upper() == str(pay.get('currency') or '').upper()
                if (status not in (200, 201) or str(vdata.get('status') or '').lower() != 'successful'
                        or not amount_ok or not currency_ok):
                    return self._json({'ok': False, 'message': 'Payment verification failed'}, 400)
                self._apply_pss_flw(conn, str(tx_ref), resp, 'successful')
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

    def _row_allowed(self, row, sess=None):
        agency_id = (sess or {}).get('agency_id')
        return not agency_id or row.get('agency_id') == agency_id

    def _clean_logo_data(self, value):
        raw = str(value or '').strip()
        if not raw:
            return ''
        mimetype = 'image/png'
        if raw.startswith('data:'):
            if ';base64,' not in raw:
                raise ValueError('Logo must be a base64 image')
            header, raw = raw.split(',', 1)
            mimetype = header[5:].split(';')[0].lower()
        allowed = ('image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/svg+xml')
        if mimetype not in allowed:
            raise ValueError('Unsupported logo image format')
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception:
            raise ValueError('Logo image is not valid base64')
        if not decoded:
            raise ValueError('Logo image is empty')
        if len(decoded) > 2 * 1024 * 1024:
            raise ValueError('Logo must be smaller than 2 MB')
        signatures = (b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF8', b'RIFF')
        if not (decoded.startswith(signatures) or decoded.lstrip().startswith((b'<svg', b'<?xml'))):
            raise ValueError('Unsupported logo image format')
        return f"data:{mimetype};base64,{base64.b64encode(decoded).decode('ascii')}"

    def _last_name_matches(self, full_name, last):
        last = str(last or '').strip(' ,.-').lower()
        if len(last) < 3:
            return False
        parts = [p.strip(' ,.-') for p in str(full_name or '').lower().replace(',', ' ').split()]
        return any(p == last or (len(p) > 3 and len(last) > 3 and p.endswith(last) and len(p) - len(last) <= 2) for p in parts[-2:])

    def _bag_tag_list(self, raw, count=0):
        values = []
        if isinstance(raw, (list, tuple, set)):
            values = [str(x).strip() for x in raw]
        elif raw:
            values = [x.strip() for x in re.split(r'[,;\s]+', str(raw)) if x.strip()]
        tags, seen = [], set()
        for value in values:
            key = value.upper()
            if key not in seen:
                seen.add(key)
                tags.append(key)
        try:
            target = max(int(count or 0), len(tags))
        except Exception:
            target = len(tags)
        while len(tags) < target:
            tag = self._psn('BG')
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
        return tags[:target]

    def _baggage_event(self, conn, tenant_id, bag, checkin_id, event_type, location=None, actor=None, note=None, trace_id=None):
        ts = now_iso()
        cur = conn.execute("""INSERT INTO pss_baggage_events(tenant_id,baggage_id,checkin_id,event_type,location,actor,note,created_at)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           (tenant_id, bag.get('id') if isinstance(bag, dict) else bag, checkin_id, event_type,
                            location, actor, note, ts))
        event_id = cur.lastrowid
        if trace_id:
            tag = bag.get('bag_tag') if isinstance(bag, dict) else ''
            self._trace(conn, tenant_id, 'counter', 'pss_baggage_item', bag.get('id') if isinstance(bag, dict) else bag,
                        event_type, actor or 'system', f"{tag or ''} {location or ''} {note or ''}".strip(), trace_id, self)
        return event_id

    def _sync_baggage_items(self, conn, tenant_id, checkin, raw_tags, count=None, user_id=None, weight_kg=0, sale_no=None, trace_id=None):
        tags = self._bag_tag_list(raw_tags, count if count is not None else checkin.get('bag_count'))
        existing = q(conn, "SELECT * FROM pss_baggage_items WHERE tenant_id=? AND checkin_id=?", (tenant_id, checkin.get('id')))
        by_tag = {str(r.get('bag_tag') or '').upper(): r for r in existing}
        ts = now_iso()
        pieces = max(len(tags), 1)
        unit_weight = round(float(weight_kg or 0) / pieces, 2)
        keep_ids = []
        for idx, tag in enumerate(tags, 1):
            while q(conn, "SELECT 1 FROM pss_baggage_items WHERE tenant_id=? AND UPPER(bag_tag)=UPPER(?) AND checkin_id<>?",
                    (tenant_id, tag, checkin.get('id')), one=True):
                tag = self._psn('BG')
            tags[idx - 1] = tag
            row = by_tag.get(tag.upper())
            if not row:
                cur = conn.execute("""INSERT INTO pss_baggage_items(tenant_id,checkin_id,booking_id,pnr,bag_tag,sequence_no,weight_kg,status,location,sale_no,flight_no,destination,carousel,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,'tagged','check-in',?,?,?,?,?,?)""",
                                   (tenant_id, checkin.get('id'), checkin.get('booking_id'), checkin.get('pnr'), tag, idx,
                                    unit_weight, sale_no, checkin.get('flight_no'), checkin.get('destination'), None, ts, ts))
                bag = {'id': cur.lastrowid, 'bag_tag': tag}
                keep_ids.append(bag['id'])
                self._baggage_event(conn, tenant_id, bag, checkin.get('id'), 'bag_tagged', 'check-in', user_id,
                                    f"Bag {tag} tagged for {checkin.get('pnr') or checkin.get('checkin_no') or ''}", trace_id)
            else:
                bag = row
                keep_ids.append(bag['id'])
                conn.execute("""UPDATE pss_baggage_items SET sequence_no=?,weight_kg=CASE WHEN ?>0 THEN ? ELSE weight_kg END,
                                sale_no=COALESCE(?,sale_no),flight_no=COALESCE(?,flight_no),destination=COALESCE(?,destination),updated_at=?
                                WHERE id=? AND tenant_id=?""",
                             (idx, unit_weight, unit_weight, sale_no, checkin.get('flight_no'), checkin.get('destination'), ts, bag['id'], tenant_id))
        if keep_ids:
            marks = ','.join('?' for _ in keep_ids)
            orphans = q(conn, f"SELECT id FROM pss_baggage_items WHERE tenant_id=? AND checkin_id=? AND id NOT IN ({marks})",
                        [tenant_id, checkin.get('id')] + keep_ids)
        else:
            orphans = q(conn, "SELECT id FROM pss_baggage_items WHERE tenant_id=? AND checkin_id=?", (tenant_id, checkin.get('id')))
        for orphan in orphans:
            conn.execute("DELETE FROM pss_baggage_events WHERE tenant_id=? AND baggage_id=?", (tenant_id, orphan['id']))
            conn.execute("DELETE FROM pss_baggage_items WHERE tenant_id=? AND id=?", (tenant_id, orphan['id']))
        conn.execute("UPDATE pss_checkins SET bag_count=?,bag_tags=?,updated_at=? WHERE id=? AND tenant_id=?",
                     (len(tags), ', '.join(tags), ts, checkin.get('id'), tenant_id))
        return tags

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
        if 'logo_data' in data:
            try:
                data['logo_data'] = self._clean_logo_data(data.get('logo_data'))
            except ValueError as exc:
                return self._json({'error': str(exc)}, 400)
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
            logo_data = data.get('logo_data') if 'logo_data' in data else (brand.get('logo_data') or '')
            logo_text = data.get('logo_text') or brand.get('logo_text') or (company[:3].upper() if company else 'TE')
            primary = data.get('primary_color') or brand.get('primary_color') or '#206090'
            accent = data.get('accent_color') or brand.get('accent_color') or '#e02020'
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

    def _as_date(self, value):
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except Exception:
            return None

    def _expiry_from_issue(self, value):
        d = self._as_date(value) or date.today()
        try:
            return d.replace(year=d.year + 1).isoformat()
        except ValueError:
            return d.replace(year=d.year + 1, day=28).isoformat()

    def _sync_unused_tickets(self, conn, tenant_id):
        ts = now_iso()
        today = ts[:10]
        candidates = q(conn, """SELECT b.id,b.pnr,b.passenger_name,b.ticket_number,b.ticket_status,
                                       b.issue_date,b.total_amount,b.fare_amount,b.taxes,b.currency,b.updated_at,
                                       f.flight_no,f.origin,f.destination,f.flight_date
                                FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                                WHERE b.tenant_id=? AND b.ticket_number IS NOT NULL AND b.ticket_number<>''
                                  AND COALESCE(b.booking_status,'') NOT IN ('checked_in','flown')
                                  AND NOT EXISTS (SELECT 1 FROM pss_checkins ci
                                                  WHERE ci.booking_id=b.id AND ci.status IN ('checked_in','flown'))
                                  AND LOWER(COALESCE(b.ticket_status,'issued')) IN
                                      ('issued','instant','ticketed','open_for_use','refunded','partially_refunded','void','no_show')""", (tenant_id,))
        created = 0
        updated = 0
        for b in candidates:
            ticket = (b.get('ticket_number') or '').strip().upper()
            if not ticket:
                continue
            issue = (b.get('issue_date') or b.get('updated_at') or ts)[:10]
            travel = (b.get('flight_date') or '')[:10] or None
            expiry = self._expiry_from_issue(issue)
            value = float(b.get('total_amount') or 0) or (float(b.get('fare_amount') or 0) + float(b.get('taxes') or 0))
            raw_status = (b.get('ticket_status') or 'issued').lower()
            if raw_status in ('refunded', 'partially_refunded'):
                ticket_status, refund_status = 'refunded', 'refunded'
            elif raw_status == 'void':
                ticket_status, refund_status = 'voided', 'not_applicable'
            else:
                ticket_status, refund_status = 'unused', 'pending'
            route = ' · '.join(x for x in [b.get('origin'), b.get('destination')] if x)
            notes = f"Auto-created from booking {b.get('pnr') or b.get('id')}"
            if route:
                notes += f" ({route})"
            existing = q(conn, "SELECT id,ticket_status,refund_status FROM unused_tickets WHERE tenant_id=? AND UPPER(ticket_no)=?",
                         (tenant_id, ticket), one=True)
            if not existing:
                conn.execute("""INSERT INTO unused_tickets(tenant_id,ticket_no,pnr,traveler_name,airline,issue_date,travel_date,
                                                          expiry_date,ticket_status,refund_status,value,currency,notes,created_at,updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (tenant_id, ticket, b.get('pnr'), b.get('passenger_name'), None, issue, travel,
                              expiry, ticket_status, refund_status, value, b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', notes, ts, ts))
                created += 1
            elif (existing.get('ticket_status') != ticket_status or existing.get('refund_status') != refund_status) and ticket_status != 'unused':
                conn.execute("UPDATE unused_tickets SET ticket_status=?,refund_status=?,updated_at=? WHERE id=? AND tenant_id=?",
                             (ticket_status, refund_status, ts, existing['id'], tenant_id))
                updated += 1
        expired = conn.execute("""UPDATE unused_tickets SET ticket_status='expired', updated_at=?
                                  WHERE tenant_id=? AND LOWER(COALESCE(ticket_status,''))='unused'
                                    AND expiry_date IS NOT NULL AND expiry_date<?""", (ts, tenant_id, today))
        refunded = self._sync_refunds_from_bookings(conn, tenant_id, ts)
        return {'scanned': len(candidates), 'created': created, 'updated': updated,
                'expired': getattr(expired, 'rowcount', 0) or 0, 'refunds': refunded}

    def _sync_refunds_from_bookings(self, conn, tenant_id, ts):
        rows = q(conn, """SELECT b.pnr,b.ticket_number,b.passenger_name,b.total_amount,b.currency,
                                 b.fare_amount,b.taxes
                          FROM pss_bookings b
                          WHERE b.tenant_id=? AND b.ticket_number IS NOT NULL AND b.ticket_number<>''
                            AND LOWER(COALESCE(b.ticket_status,'')) IN ('refunded','partially_refunded')""", (tenant_id,))
        made = 0
        for b in rows:
            ticket = (b.get('ticket_number') or '').strip().upper()
            refund_no = 'RF-' + ticket.replace('-', '')[:20]
            if q(conn, "SELECT 1 FROM refunds WHERE tenant_id=? AND refund_no=?", (tenant_id, refund_no), one=True):
                continue
            amount = float(b.get('total_amount') or 0) or (float(b.get('fare_amount') or 0) + float(b.get('taxes') or 0))
            conn.execute("""INSERT INTO refunds(tenant_id,refund_no,pnr,ticket_no,traveler_name,source,reason,amount,currency,
                                                policy_check,auto_void,status,requested_at,processed_at,notes,created_at,updated_at)
                            VALUES (?,?,?,?,?,'unused_ticket','ticket_not_used',?,?,'passed',?,?,?,?,?,?,?)""",
                         (tenant_id, refund_no, b.get('pnr'), ticket, b.get('passenger_name'), amount,
                          b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', 1, 'processed', ts, ts, 'Auto-created from refunded e-ticket', ts, ts))
            made += 1
        return made

    def _auto_optimize_fares(self, conn, tenant_id, apply=True):
        ts = now_iso()
        today = date.today()
        flights = q(conn, """SELECT id,flight_no,base_fare,base_fare_reference,fare_optimized_at,total_seats,
                                    available_seats,flight_date
                             FROM pss_flights
                             WHERE tenant_id=? AND LOWER(COALESCE(status,'scheduled'))='scheduled'
                               AND flight_date>=? ORDER BY flight_date LIMIT 200""", (tenant_id, today.isoformat()))
        suggestions = []
        updated = 0
        for f in flights:
            total = float(f.get('total_seats') or 0)
            available = float(f.get('available_seats') or 0)
            sold = max(0.0, total - available)
            load = round(sold / total, 3) if total else 0.0
            fd = self._as_date(f.get('flight_date'))
            days = (fd - today).days if fd else 999
            base = float(f.get('base_fare_reference') or f.get('base_fare') or 0)
            current = float(f.get('base_fare') or 0)
            if base <= 0:
                continue
            if load >= 0.85:
                factor, action = 1.12, 'high demand +12%'
            elif load >= 0.65:
                factor, action = 1.07, 'strong demand +7%'
            elif load >= 0.40:
                factor, action = 1.03, 'steady demand +3%'
            elif load < 0.25 and days <= 14:
                factor, action = 0.92, 'low demand, close departure -8%'
            else:
                factor, action = 1.0, 'hold'
            target = round(base * factor, 2)
            if abs(target - current) < 0.01:
                continue
            suggestions.append({'flight_id': f['id'], 'flight_no': f.get('flight_no'), 'load_factor': load,
                                'days_to_departure': days, 'current_fare': current, 'recommended_fare': target,
                                'action': action})
            if apply:
                conn.execute("""UPDATE pss_flights
                                SET base_fare=?, base_fare_reference=COALESCE(base_fare_reference,base_fare),
                                    fare_optimized_at=?, updated_at=? WHERE id=? AND tenant_id=?""",
                             (target, ts, ts, f['id'], tenant_id))
                updated += 1
        return {'updated': updated, 'suggestions': suggestions}

    def api_unused_tickets(self, tenant_id):
        with db() as conn:
            automation = self._sync_unused_tickets(conn, tenant_id)
            rows = q(conn, """SELECT * FROM unused_tickets WHERE tenant_id=?
                              ORDER BY CASE LOWER(COALESCE(ticket_status,'')) WHEN 'unused' THEN 0 WHEN 'expired' THEN 1 ELSE 2 END,
                                       expiry_date ASC, id DESC LIMIT 500""", (tenant_id,))
        return self._json({'unused_tickets': rows, 'automation': automation})

    def api_unused_ticket_refund(self, tenant_id, user_id):
        data = read_json(self)
        rid = self._oid(data.get('id'))
        ticket = (data.get('ticket_no') or '').strip().upper()
        with db() as conn:
            row = None
            if rid:
                row = q(conn, "SELECT * FROM unused_tickets WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
            elif ticket:
                row = q(conn, "SELECT * FROM unused_tickets WHERE tenant_id=? AND UPPER(ticket_no)=?", (tenant_id, ticket), one=True)
            if not row:
                return self._json({'error': 'Unused ticket not found'}, 404)
            amount = float(data.get('amount') or row.get('value') or 0)
            if amount <= 0:
                return self._json({'error': 'Refund amount must be greater than zero'}, 400)
            refund_no = 'RF-' + (row.get('ticket_no') or str(row['id'])).replace('-', '')[:20]
            if not q(conn, "SELECT 1 FROM refunds WHERE tenant_id=? AND refund_no=?", (tenant_id, refund_no), one=True):
                ts = now_iso()
                conn.execute("""INSERT INTO refunds(tenant_id,refund_no,pnr,ticket_no,traveler_name,source,reason,amount,currency,
                                                    policy_check,auto_void,status,requested_at,notes,created_at,updated_at)
                                VALUES (?,?,?,?,?,'unused_ticket','unused_ticket_refund',?,?,'passed',?,?,?,?,?,?)""",
                             (tenant_id, refund_no, row.get('pnr'), row.get('ticket_no'), row.get('traveler_name'),
                              amount, row.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', 0, 'pending', ts,
                              data.get('notes') or 'Refund requested from unused tickets', ts, ts))
            conn.execute("UPDATE unused_tickets SET refund_status='requested',updated_at=? WHERE id=? AND tenant_id=?",
                         (now_iso(), row['id'], tenant_id))
            log_audit(conn, tenant_id, user_id, 'request_unused_ticket_refund', 'unused_ticket', row['id'], {'refund_no': refund_no})
        return self._json({'ok': True, 'refund_no': refund_no})

    def api_fare_optimization(self, tenant_id, user_id, apply=False):
        with db() as conn:
            result = self._auto_optimize_fares(conn, tenant_id, apply=apply)
            if apply and result.get('updated'):
                log_audit(conn, tenant_id, user_id, 'optimize_fares', 'pss_flights', None, {'updated': result['updated']})
        return self._json(result)

    def api_run_automation(self, tenant_id, user_id):
        with db() as conn:
            unused = self._sync_unused_tickets(conn, tenant_id)
            fares = self._auto_optimize_fares(conn, tenant_id, apply=True)
            log_audit(conn, tenant_id, user_id, 'run_automation', 'system', None,
                      {'unused_created': unused.get('created'), 'fares_updated': fares.get('updated')})
        return self._json({'unused_tickets': unused, 'fare_optimization': fares})

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
                if c not in data:
                    continue
                v = data.get(c)
                if v == '' and any(t in available_types.get(c, '') for t in ('INT', 'REAL', 'FLOAT', 'DOUBLE', 'NUMERIC')):
                    v = None
                vals[c] = v
            if 'is_active' in vals and vals['is_active'] is None:
                vals['is_active'] = 1 if str(vals.get('status') or 'active').lower() in ('active','1','true') else 0
            if table == 'users':
                sess_obj = require_auth(self)
                caller_group = role_group(sess_obj.get('role')) if sess_obj else 'airline_admin'
                current_user = q(conn, "SELECT role,username FROM users WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) if rid else None
                password = data.get('password')
                if password:
                    err = _password_policy_error(password)
                    if err:
                        return self._json({'error': err}, 400)
                if 'username' in vals:
                    vals['username'] = str(vals.get('username') or '').strip()
                username = vals.get('username') or (current_user or {}).get('username') or ''
                if not username:
                    return self._json({'error': 'username required'}, 400)
                duplicate = q(conn, "SELECT id FROM users WHERE tenant_id=? AND username=? AND id<>?",
                              (tenant_id, username, rid or 0), one=True)
                if duplicate:
                    return self._json({'error': 'Username already exists'}, 400)
                if 'role' in vals:
                    role = str(vals.get('role') or '').strip().lower()
                    if role and role not in USER_ROLES and role != (current_user or {}).get('role'):
                        return self._json({'error': 'Invalid role'}, 400)
                    vals['role'] = role or ((current_user or {}).get('role') or 'agent')
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
                    if caller_group != 'airline_admin' and current_user:
                        vals['role'] = current_user['role']
            elif table == 'integrations':
                cfg = data.get('config') if isinstance(data.get('config'), dict) else {}
                old = q(conn, "SELECT config_json FROM integrations WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) if rid else None
                old_cfg = self._safe_config(old['config_json']) if old else {}
                for k, v in cfg.items():
                    if isinstance(v, str) and v.startswith('********') and k in old_cfg:
                        cfg[k] = json.loads(old['config_json'] or '{}').get(k, '')
                vals['config_json'] = json.dumps(cfg)
                vals.pop('config', None)
            if rid and not vals:
                if not q(conn, f"SELECT 1 FROM {table} WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True):
                    return self._json({'error': 'Not found'}, 404)
                return self._json({'ok': True})
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
                cur = conn.execute(f"INSERT INTO {table}({','.join(insert_cols)}) VALUES ({','.join(['?']*len(insert_cols))})", params)
                new_id = cur.lastrowid
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

    def api_report_export(self, tenant_id, qs, user_id=None):
        entity = (qs.get('entity') or qs.get('view') or [''])[0]
        table = REPORT_SOURCES.get(entity)
        if not table:
            return self._json({'error': 'Unsupported report'}, 400)
        sess = get_session(self) or {}
        if not _report_allowed(entity, role_group(sess.get('role'))):
            return self._json({'error': 'Access denied'}, 403)
        fmt = (qs.get('format') or ['csv'])[0].lower()
        if fmt not in ('csv', 'xlsx'):
            return self._json({'error': 'Unsupported format'}, 400)
        try:
            limit = max(1, min(int((qs.get('limit') or ['10000'])[0]), 50000))
        except (TypeError, ValueError):
            limit = 10000
        df = (qs.get('from') or qs.get('date_from') or [''])[0].strip()
        dt = (qs.get('to') or qs.get('date_to') or [''])[0].strip()
        status = (qs.get('status') or [''])[0].strip()
        term = (qs.get('q') or [''])[0].strip().lower()
        with db() as conn:
            try:
                info = q(conn, f"PRAGMA table_info({table})")
            except Exception:
                return self._json({'error': 'Report source unavailable'}, 404)
            if not info:
                return self._json({'error': 'Report source unavailable'}, 404)
            names = [c['name'] for c in info]
            if 'tenant_id' not in names:
                return self._json({'error': 'Report not available'}, 400)
            cols = [c for c in names if c not in REPORT_EXCLUDE_COLUMNS]
            where = ['tenant_id=?']
            params = [tenant_id]
            date_col = next((c for c in ('created_at', 'issue_date', 'booking_date', 'flight_date',
                                         'travel_date', 'paid_at', 'departure_at') if c in names), None)
            if date_col and df:
                where.append(f'{date_col}>=?')
                params.append(df)
            if date_col and dt:
                where.append(f'{date_col}<=?')
                params.append(dt + 'T23:59:59.999999')
            if status and 'status' in names:
                where.append('status=?')
                params.append(status)
            if term:
                text_cols = [c for c in cols if any(k in c for k in ('name', 'no', 'email', 'phone',
                                                                      'pnr', 'code', 'number', 'title'))][:8]
                if text_cols:
                    where.append('(' + ' OR '.join(f"LOWER(CAST({c} AS TEXT)) LIKE ?" for c in text_cols) + ')')
                    params.extend([f'%{term}%'] * len(text_cols))
            order = ''
            if 'id' in names:
                order = ' ORDER BY id DESC'
            elif date_col:
                order = f' ORDER BY {date_col} DESC'
            sql = f"SELECT {', '.join(cols)} FROM {table} WHERE {' AND '.join(where)}{order} LIMIT ?"
            params.append(limit)
            try:
                records = q(conn, sql, tuple(params))
            except Exception:
                return self._json({'error': 'Report query failed'}, 400)
        headers = [_report_label(c) for c in cols]
        rows = [[_report_value(r.get(c)) for c in cols] for r in records]
        safe = re.sub(r'[^a-z0-9_-]+', '-', entity.lower()).strip('-') or 'report'
        if fmt == 'xlsx':
            payload = _build_xlsx(headers, rows)
            ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        else:
            buf = io.StringIO(newline='')
            writer = csv.writer(buf)
            writer.writerow(headers)
            writer.writerows(rows)
            payload = b'\xef\xbb\xbf' + buf.getvalue().encode('utf-8')
            ctype = 'text/csv; charset=utf-8'
        return self._send(payload, 200, ctype,
                          {'Content-Disposition': f'attachment; filename="{safe}-{date.today().isoformat()}.{fmt}"'})

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
                               (tenant_id, data.get('branch_id'), est_no, self._oid(data.get('customer_id')), title, data.get('description'), data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', subtotal, tax, total, data.get('status') or 'draft', data.get('valid_until'), user_id, ts, ts, agency_id))
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
            if not est or not self._row_allowed(est, sess):
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
                               (tenant_id, data.get('branch_id'), cn_no, self._oid(data.get('customer_id')), self._oid(data.get('invoice_id')), data.get('reason') or 'refund', data.get('description'), data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', amount, 'issued', data.get('issue_date') or ts[:10], user_id, ts, agency_id))
            cid = cur.lastrowid
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'credit_note', cid, ts[:10], f"Credit note {cn_no}", 'Sales', 'Accounts Receivable', amount, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', ts, agency_id, 'credit_note'))
            log_audit(conn, tenant_id, user_id, 'create', 'credit_note', cid, {'credit_note_no': cn_no})
        return self._json({'ok': True, 'credit_note_id': cid, 'credit_note_no': cn_no})

    def api_credit_note_mutate(self, tenant_id, cid, method, sess=None):
        cid = self._oid(cid)
        with db() as conn:
            row = q(conn, "SELECT * FROM credit_notes WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True)
            if not row or not self._row_allowed(row, sess):
                return self._json({'error': 'Credit note not found'}, 404)
            if method == 'GET':
                return self._json({'credit_note': row})
            if method == 'DELETE':
                conn.execute("DELETE FROM credit_notes WHERE id=? AND tenant_id=?", (cid, tenant_id))
                conn.execute("DELETE FROM journals WHERE tenant_id=? AND ref_type='credit_note' AND ref_id=?", (tenant_id, cid))
                return self._json({'ok': True})
            data = read_json(self)
            allowed = ('branch_id', 'credit_note_no', 'customer_id', 'invoice_id', 'reason', 'description',
                       'currency', 'amount', 'status', 'issue_date')
            sets, args = [], []
            for key in allowed:
                if key in data:
                    sets.append(f"{key}=?")
                    args.append(self._oid(data[key]) if key.endswith('_id') else data[key])
            if not sets:
                return self._json({'credit_note': row})
            args += [cid, tenant_id]
            conn.execute(f"UPDATE credit_notes SET {','.join(sets)} WHERE id=? AND tenant_id=?", args)
            updated = q(conn, "SELECT * FROM credit_notes WHERE id=? AND tenant_id=?", (cid, tenant_id), one=True)
        return self._json({'ok': True, 'credit_note': updated})

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
                               (tenant_id, data.get('branch_id'), bill_no, supplier, data.get('description'), self._oid(data.get('order_id')), data.get('account_code') or '1150', data.get('category') or 'supplier', data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', subtotal, tax, total, total, data.get('bill_date') or ts[:10], data.get('due_date'), 'open', user_id, ts, ts))
            bid = cur.lastrowid
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'bill', bid, ts[:10], f"Bill {bill_no} - {supplier}", data.get('account_code') or '1150', 'Accounts Payable', total, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', ts))
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
            cur = conn.execute("""INSERT INTO expenses(tenant_id,branch_id,expense_no,category,vendor,description,account_code,amount,currency,expense_date,status,payment_method,reference,receipt_no,agency_id,created_by,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, data.get('branch_id'), exp_no, data.get('category') or 'operating', data.get('vendor'), data.get('description'), data.get('account_code') or '1300', amount, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', data.get('expense_date') or ts[:10], data.get('status') or 'posted', data.get('payment_method') or 'cash', data.get('reference'), data.get('receipt_no'), (sess or {}).get('agency_id'), user_id, ts, ts))
            eid = cur.lastrowid
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'expense', eid, ts[:10], f"Expense {exp_no}", data.get('account_code') or '1300', 'Petty Cash', amount, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', ts))
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
            allowed = ('category', 'vendor', 'description', 'account_code', 'amount', 'currency',
                       'expense_date', 'status', 'payment_method', 'reference', 'receipt_no')
            vals = {k: data[k] for k in allowed if k in data}
            if not vals:
                return self._json({'ok': True})
            vals['updated_at'] = now_iso()
            conn.execute(f"UPDATE expenses SET {','.join(k+'=?' for k in vals)} WHERE id=? AND tenant_id=?",
                         list(vals.values()) + [eid, tenant_id])
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
                         (tenant_id, 'manual', None, data.get('entry_date') or ts[:10], data.get('memo') or 'Manual journal', data.get('debit_account'), data.get('credit_account'), amount, data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', ts))
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

    def _entity_context(self, conn, tenant_id, entity_type, entity_id, date_from=None, date_to=None, bag_index=0):
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
        if entity_type in ('pss_checkin', 'boardingpass', 'upliftcoupon', 'uplift-coupon'):
            ci = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.gate, f.terminal
                             FROM pss_checkins ci LEFT JOIN pss_flights f ON f.id=ci.flight_id
                             WHERE ci.tenant_id=? AND ci.id=?""", (tenant_id, eid), one=True)
            if not ci:
                return {}
            b = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND id=?", (tenant_id, ci['booking_id']), one=True) or {}
            bags = q(conn, "SELECT * FROM pss_baggage_items WHERE tenant_id=? AND checkin_id=? ORDER BY sequence_no, id", (tenant_id, eid))
            events = q(conn, """SELECT e.*, bg.bag_tag FROM pss_baggage_events e
                                LEFT JOIN pss_baggage_items bg ON bg.id=e.baggage_id
                                WHERE e.tenant_id=? AND e.checkin_id=? ORDER BY e.id DESC LIMIT 100""", (tenant_id, eid))
            uplift_sale = q(conn, """SELECT * FROM pss_ancillary_sales WHERE tenant_id=? AND booking_id=?
                                     AND (sale_no LIKE 'BAG-%' OR description LIKE 'Baggage %')
                                     ORDER BY id DESC LIMIT 1""", (tenant_id, ci.get('booking_id')), one=True) or {}
            return {'checkin': ci, 'pss_checkin': ci, 'pss_booking': b, 'booking': b,
                    'baggage': bags, 'baggage_events': events, 'bag_index': bag_index, 'uplift_sale': uplift_sale}
        return {}

    def api_pdf_document(self, kind, oid, qs):
        allowed = {'booking': 'booking', 'order': 'booking', 'invoice': 'invoice', 'customer': 'customer',
                   'bill': 'bill', 'traveler': 'traveler', 'payment': 'payment', 'receipt': 'payment',
                    'estimate': 'estimate', 'credit_note': 'credit_note', 'credit-note': 'credit_note',
                    'statement': 'customer', 'journey-document': 'pss_booking',
                      'eticket': 'pss_booking', 'boardingpass': 'pss_checkin', 'journeypass': 'pss_booking',
                    'baggagetag': 'pss_checkin', 'baggage-tag': 'pss_checkin', 'baggage_tag': 'pss_checkin',
                    'upliftcoupon': 'pss_checkin', 'uplift-coupon': 'pss_checkin',
                    'quotation': 'estimate',
                    'itinerary': 'pss_booking', 'booking-confirmation': 'pss_booking'}
        if kind not in allowed:
            return self._json({'error': 'Unsupported document type'}, 400)
        entity_type = allowed[kind]
        sess = None
        with db() as conn:
            token = (qs.get('token') or [''])[0]
            if token:
                share = q(conn, "SELECT * FROM document_shares WHERE token=? AND status='active'", (token,), one=True)
                if share:
                    if share.get('expires_at') and str(share['expires_at']) < now_iso():
                        return self._json({'error': 'Share link expired'}, 410)
                    setype = str(share.get('entity_type') or '')
                    valid = (setype == entity_type or
                             (entity_type == 'booking' and setype == 'order') or
                             (entity_type == 'pss_booking' and setype in ('pss_booking', 'eticket', 'journeypass')) or
                               (entity_type == 'pss_checkin' and setype in ('pss_checkin', 'boardingpass', 'baggagetag', 'baggage-tag', 'baggage_tag', 'upliftcoupon', 'uplift-coupon')))
                    if not valid or str(share.get('entity_id')) != str(oid):
                        return self._json({'error': 'Invalid share token'}, 403)
                    tenant_id = share['tenant_id']
                else:
                    offer = q(conn, "SELECT * FROM pss_eticket_directs WHERE token=? AND status='active' AND (valid_until IS NULL OR valid_until>?)",
                              (token, now_iso()), one=True)
                    booking = q(conn, "SELECT id,offer_id FROM pss_bookings WHERE id=?", (self._oid(oid),), one=True)
                    if not offer or not booking or str(booking.get('offer_id')) != str(offer['id']) or entity_type != 'pss_booking':
                        return self._json({'error': 'Invalid share token'}, 403)
                    tenant_id = offer['tenant_id']
            else:
                sess = require_auth(self)
                if not sess:
                    sess = get_portal_session(self)
                    pnr = (qs.get('pnr') or [''])[0].strip().upper()
                    last = (qs.get('last_name') or [''])[0].strip().lower()
                    tcode = (qs.get('tenant') or [''])[0].strip()
                    if not sess:
                        if not pnr or not last or entity_type not in ('pss_booking', 'invoice', 'payment'):
                            return self._json({'error': 'Unauthorized'}, 401)
                        trow = self._portal_tenant(tcode) if tcode else None
                        tid = trow['id'] if trow else None
                        bid = self._oid(oid)
                        if entity_type == 'invoice':
                            inv = q(conn, "SELECT tenant_id,pss_booking_id FROM invoices WHERE id=?", (self._oid(oid),), one=True)
                            if not inv:
                                return self._json({'error': 'Document not found'}, 404)
                            tid = tid or inv.get('tenant_id')
                            bid = inv.get('pss_booking_id')
                        elif entity_type == 'payment':
                            pay = q(conn, "SELECT tenant_id,pss_booking_id FROM payments WHERE id=?", (self._oid(oid),), one=True)
                            if not pay:
                                return self._json({'error': 'Document not found'}, 404)
                            tid = tid or pay.get('tenant_id')
                            bid = pay.get('pss_booking_id')
                        if not tid or not bid:
                            return self._json({'error': 'Access denied'}, 403)
                        trip = q(conn, """SELECT id,passenger_name FROM pss_bookings
                                          WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)""", (tid, pnr, pnr))
                        if (not any(self._last_name_matches(x.get('passenger_name'), last) for x in trip)
                                or not any(str(x['id']) == str(bid) for x in trip)):
                            return self._json({'error': 'Access denied'}, 403)
                        tenant_id = tid
                    else:
                        tenant_id = sess['tenant_id']
                        if entity_type not in ('pss_booking', 'pss_checkin', 'invoice', 'payment'):
                            return self._json({'error': 'Access denied'}, 403)
                        bid = self._oid(oid)
                        if entity_type == 'pss_checkin':
                            ci = q(conn, "SELECT booking_id FROM pss_checkins WHERE id=? AND tenant_id=?", (self._oid(oid), tenant_id), one=True)
                            bid = ci.get('booking_id') if ci else None
                        if entity_type == 'invoice':
                            inv = q(conn, "SELECT pss_booking_id FROM invoices WHERE id=? AND tenant_id=?", (self._oid(oid), tenant_id), one=True)
                            bid = inv.get('pss_booking_id') if inv else None
                        if entity_type == 'payment':
                            pay = q(conn, "SELECT pss_booking_id FROM payments WHERE id=? AND tenant_id=?", (self._oid(oid), tenant_id), one=True)
                            bid = pay.get('pss_booking_id') if pay else None
                        if not bid:
                            return self._json({'error': 'Access denied'}, 403)
                        booking = q(conn, "SELECT id,passenger_name,portal_customer_id FROM pss_bookings WHERE id=? AND tenant_id=?",
                                    (bid, tenant_id,), one=True)
                        allowed = bool(booking and booking.get('portal_customer_id') == sess.get('customer_id'))
                        if not allowed and booking and last:
                            allowed = self._last_name_matches(booking.get('passenger_name'), last)
                        if not allowed:
                            return self._json({'error': 'Access denied'}, 403)
                else:
                    group = role_group(sess.get('role'))
                    api_allow = ROLE_API.get(group)
                    if api_allow is not None and not any('/api/documents'.startswith(prefix) for prefix in api_allow):
                        return self._json({'error': 'Access denied'}, 403)
                    tenant_id = sess['tenant_id']
            date_from = (qs.get('from') or qs.get('date_from') or [''])[0]
            date_to = (qs.get('to') or qs.get('date_to') or [''])[0]
            bag_index = self._oid((qs.get('bag') or [''])[0]) or 0
            context = self._entity_context(conn, tenant_id, entity_type, oid, date_from, date_to, bag_index)
            if not context:
                return self._json({'error': 'Document not found'}, 404)
            branding = self._branding(conn, tenant_id)
        pdf_kind = {'order': 'booking', 'itinerary': 'journeypass', 'booking-confirmation': 'journeypass',
                    'journey-document': 'journeypass', 'statement': 'customer', 'credit-note': 'credit_note',
                    'baggage-tag': 'baggagetag', 'baggage_tag': 'baggagetag', 'uplift-coupon': 'upliftcoupon',
                    'quotation': 'estimate'}.get(kind, kind)
        with db() as conn:
            ref = self._psn('DOC')
            subject = context.get('pss_booking') or context.get('checkin') or context.get('invoice') or {}
            actor = sess.get('username') if sess else 'client'
            self._doc_audit(conn, tenant_id, pdf_kind, ref, subject.get('id'), subject.get('pnr'),
                            'document_generated', actor,
                            subject.get('trace_id'), {'kind': pdf_kind})
        pdf = build_pdf(pdf_kind, context, branding)
        name = f"{kind}-{oid}.pdf"
        download = (qs.get('download') or [''])[0].lower() in ('1', 'true', 'yes')
        disp = 'attachment' if download else 'inline'
        return self._send(pdf, 200, 'application/pdf', {'Content-Disposition': f'{disp}; filename="{name}"'})

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

    def api_document_download(self, tenant_id, did, qs=None):
        did = self._oid(did)
        with db() as conn:
            row = q(conn, "SELECT * FROM documents WHERE id=? AND tenant_id=?", (did, tenant_id), one=True)
        if not row:
            return self._json({'error': 'Document not found'}, 404)
        payload = row.get('file_data') if row.get('file_data') else (row.get('content_text') or '').encode('utf-8')
        fname = (row.get('file_name') or (row.get('title') or 'document')).replace('"', '')
        force_dl = str((qs or {}).get('download', ['0'])[0]).lower() in ('1','true','yes')
        disp = 'attachment' if force_dl else 'inline'
        return self._send(payload, 200, row.get('mime_type') or 'application/octet-stream',
                          {'Content-Disposition': f'{disp}; filename="{fname}"'})

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

    def api_share_mutate(self, tenant_id, sid, user_id, method):
        sid = self._oid(sid)
        with db() as conn:
            row = q(conn, "SELECT * FROM document_shares WHERE id=? AND tenant_id=?", (sid, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Share link not found'}, 404)
            if method == 'DELETE':
                conn.execute("DELETE FROM document_shares WHERE id=? AND tenant_id=?", (sid, tenant_id))
                log_audit(conn, tenant_id, user_id, 'share_delete', row['entity_type'], row['entity_id'], {'share_id': sid})
                return self._json({'ok': True})
            data = read_json(self)
            fields = {}
            for key in ('recipient', 'permission', 'status', 'expires_at'):
                if key in data:
                    fields[key] = data[key]
            if 'permission' in fields and fields['permission'] not in ('view', 'download'):
                return self._json({'error': 'Invalid permission'}, 400)
            if 'status' in fields and fields['status'] not in ('active', 'revoked'):
                return self._json({'error': 'Invalid status'}, 400)
            if fields:
                sets = ','.join(f'{k}=?' for k in fields)
                conn.execute(f"UPDATE document_shares SET {sets} WHERE id=? AND tenant_id=?", (*fields.values(), sid, tenant_id))
            self._activity(conn, tenant_id, row['entity_type'], row['entity_id'], 'share_updated',
                           f"recipient={fields.get('recipient') or row.get('recipient') or 'client'}", user_id)
            log_audit(conn, tenant_id, user_id, 'share_update', row['entity_type'], row['entity_id'], {'share_id': sid, **fields})
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
            try:
                logo_data = self._clean_logo_data(data.get('logo_data')) if 'logo_data' in data else (current.get('logo_data') or '')
            except ValueError as exc:
                return self._json({'error': str(exc)}, 400)
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

    def _pss_next_ticket(self, conn, tenant_id, channel_id=None, airport_code=None, agency_id=None, strict_agency=False):
        agency_id = self._oid(agency_id)
        allocations = q(conn, """SELECT a.*, p.prefix, p.status pool_status FROM pss_stock_allocations a
                                 JOIN pss_stock_pools p ON p.id=a.pool_id
                                 WHERE a.tenant_id=? AND a.status='active' AND p.status='active'
                                   AND a.next_serial<=a.end_serial ORDER BY a.id""", (tenant_id,))
        scoped = [a for a in allocations if (not agency_id or a.get('agency_id') == agency_id)] if (agency_id or strict_agency) else allocations
        if agency_id and strict_agency and not scoped:
            return None
        pool = scoped if agency_id else allocations
        alloc = None
        if channel_id:
            alloc = next((a for a in pool if a.get('channel_id') == channel_id), None)
        if not alloc and airport_code:
            alloc = next((a for a in pool if a.get('airport_code') == airport_code), None)
        if not alloc:
            alloc = pool[0] if pool else None
        if not alloc and agency_id and not strict_agency:
            # Fallback to non-agency pool only when not strict
            for fallback in allocations:
                if fallback.get('channel_id') == channel_id or fallback.get('airport_code') == airport_code:
                    alloc = fallback; break
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
                _, committed = self._inv_counts(conn, tenant_id, fid)
                if committed:
                    return self._json({'error': f'Cannot delete flight with {committed} active reservation(s)'}, 400)
                conn.execute("DELETE FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if data.get('status') == 'cancelled':
                conn.execute("UPDATE pss_flights SET status='cancelled', updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), fid, tenant_id))
                return self._json({'ok': True})
            fields = ['flight_no','origin','destination','flight_date','dep_time','arr_time','aircraft','total_seats','available_seats','base_fare','currency','gate','terminal','status',
                      'fare_basis','fare_terms','cabin_class','baggage_allowance','change_fee','refund_fee','min_stay','max_stay','advance_purchase_days','schedule_group','frequency']
            total_seats = int(data.get('total_seats') or 0)
            available_seats = int(data.get('available_seats') or 0)
            if available_seats < 0 or available_seats > total_seats:
                return self._json({'error': 'available_seats must be between 0 and total_seats'}, 400)
            if fid:
                _, committed = self._inv_counts(conn, tenant_id, fid)
                if total_seats < committed:
                    return self._json({'error': f'total_seats cannot be below {committed} active reservation(s)'}, 400)
            vals = [data.get(f) for f in fields]
            repeat_dates = data.get('repeat_dates') or []
            if isinstance(repeat_dates, str):
                repeat_dates = [d.strip() for d in repeat_dates.replace(',', ' ').split() if d.strip()]
            save_dates = [data.get('flight_date')] + [d for d in repeat_dates if d and d != data.get('flight_date')]
            created_ids = []
            if fid:
                conn.execute("UPDATE pss_flights SET " + ','.join(f + '=?' for f in fields) + ",updated_at=? WHERE id=? AND tenant_id=?", vals + [now_iso(), fid, tenant_id])
                created_ids.append(fid)
            else:
                if not data.get('flight_no') or not data.get('origin') or not data.get('destination') or not data.get('flight_date'):
                    return self._json({'error': 'flight_no, origin, destination and flight_date are required'}, 400)
                for fd in save_dates:
                    row_vals = [tenant_id] + [fd if f == 'flight_date' else data.get(f) for f in fields] + [now_iso(), now_iso()]
                    try:
                        cur = conn.execute("INSERT INTO pss_flights(tenant_id," + ','.join(fields) + ",created_at,updated_at) VALUES (" + ','.join(['?'] * (len(fields) + 3)) + ")", row_vals)
                        created_ids.append(cur.lastrowid)
                    except Exception as e:
                        if 'UNIQUE' in str(e).upper() or 'unique' in str(e).lower():
                            continue
                        raise
                if not created_ids:
                    return self._json({'error': 'No flights created. Check for duplicate flight_no+date or invalid data.'}, 400)
                fid = created_ids[0]
            log_audit(conn, tenant_id, None, 'save', 'pss_flight', fid, {'created': len(created_ids)})
        return self._json({'ok': True, 'flight_id': fid, 'flight_ids': created_ids, 'created': len(created_ids)})

    def api_pss_seat_holds(self, tenant_id):
        with db() as conn:
            holds = q(conn, """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time
                               FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                               WHERE b.tenant_id=? AND b.booking_status='hold' ORDER BY b.id DESC LIMIT 200""", (tenant_id,))
            flights = q(conn, """SELECT id, flight_no, origin, destination, flight_date, dep_time, available_seats
                                 FROM pss_flights WHERE tenant_id=? AND LOWER(COALESCE(status,'scheduled'))='scheduled'
                                 ORDER BY flight_date, dep_time LIMIT 200""", (tenant_id,))
        return self._json({'holds': holds, 'flights': flights})

    def api_pss_seat_hold_create(self, tenant_id, user_id):
        data = read_json(self)
        fid = self._oid(data.get('flight_id'))
        name = (data.get('passenger_name') or '').strip()
        if not name:
            return self._json({'error': 'passenger_name required'}, 400)
        with db() as conn:
            self._inv_release_expired(conn, tenant_id, user_id)
            hold_minutes = self._inv_hold_minutes(conn, tenant_id)
            hold_expires = (datetime.utcnow() + timedelta(minutes=hold_minutes)).replace(microsecond=0).isoformat() + 'Z'
            flight = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id), one=True)
            if not flight:
                return self._json({'error': 'Flight not found'}, 404)
            requested_pnr = (data.get('pnr') or '').strip().upper().replace('-', '').replace(' ', '')[:6]
            pnr = requested_pnr
            while True:
                if not pnr:
                    pnr = self._psn('').replace('-', '').upper()[:6]
                if not q(conn, "SELECT 1 FROM pss_bookings WHERE tenant_id=? AND pnr=?", (tenant_id, pnr), one=True):
                    break
                if requested_pnr:
                    return self._json({'error': 'PNR already exists'}, 400)
                pnr = ''
            reserved = conn.execute("UPDATE pss_flights SET available_seats=available_seats-1, updated_at=? WHERE id=? AND tenant_id=? AND available_seats>0",
                                    (now_iso(), fid, tenant_id))
            if reserved.rowcount < 1:
                return self._json({'error': 'No seats available on this flight'}, 400)
            cur = conn.execute("""INSERT INTO pss_bookings(tenant_id,pnr,flight_id,passenger_name,passenger_type,contact_email,contact_phone,channel_id,airport_code,fare_class,fare_amount,taxes,total_amount,currency,ticket_status,payment_status,booking_status,hold_expires_at,seat_no,created_by,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unissued','pending','hold',?,?,?,?,?)""",
                               (tenant_id, pnr, fid, name, data.get('passenger_type') or 'ADT', data.get('contact_email'),
                                data.get('contact_phone'), self._oid(data.get('channel_id')), data.get('airport_code') or flight.get('origin'),
                                data.get('fare_class') or 'Y', 0, 0, 0, flight.get('currency') or 'USD', hold_expires,
                                data.get('seat_no'), user_id, now_iso(), now_iso()))
            self._trace(conn, tenant_id, 'counter', 'pss_booking', cur.lastrowid, 'seat_held', user_id, pnr, None, self)
            log_audit(conn, tenant_id, user_id, 'hold_seat', 'pss_booking', cur.lastrowid, {'pnr': pnr, 'flight_id': fid})
        return self._json({'ok': True, 'id': cur.lastrowid, 'pnr': pnr})

    def api_pss_seat_hold_release(self, tenant_id, user_id, hid):
        hid = self._oid(hid)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=? AND booking_status='hold'", (hid, tenant_id), one=True)
            if not b:
                return self._json({'error': 'Seat hold not found'}, 404)
            conn.execute("DELETE FROM pss_bookings WHERE id=? AND tenant_id=?", (hid, tenant_id))
            if b.get('flight_id'):
                conn.execute("UPDATE pss_flights SET available_seats=available_seats+1, updated_at=? WHERE id=? AND tenant_id=?",
                             (now_iso(), b['flight_id'], tenant_id))
            self._trace(conn, tenant_id, 'counter', 'pss_booking', hid, 'seat_released', user_id, b.get('pnr'), None, self)
            log_audit(conn, tenant_id, user_id, 'release_seat', 'pss_booking', hid, {'pnr': b.get('pnr')})
        return self._json({'ok': True})

    def _inv_log(self, conn, tenant_id, entity_type, entity_id, action, before, after, actor=None):
        conn.execute("""INSERT INTO pss_inventory_history
                        (tenant_id,entity_type,entity_id,action,before_json,after_json,actor,created_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (tenant_id, entity_type, str(entity_id or ''), action,
                      json.dumps(before or {}, default=str), json.dumps(after or {}, default=str),
                      str(actor or 'system'), now_iso()))

    def _inv_queue(self, conn, tenant_id, issue_type, severity, flight_id, message):
        existing = q(conn, """SELECT id FROM pss_inventory_queue
                              WHERE tenant_id=? AND issue_type=? AND flight_id=? AND status='open'""",
                     (tenant_id, issue_type, flight_id or 0), one=True)
        if existing:
            return existing['id']
        cur = conn.execute("""INSERT INTO pss_inventory_queue
                              (tenant_id,issue_type,severity,flight_id,message,status,created_at)
                              VALUES (?,?,?,?,?,'open',?)""",
                           (tenant_id, issue_type, severity, flight_id or 0, message, now_iso()))
        return cur.lastrowid

    def _inv_hold_minutes(self, conn, tenant_id):
        rule = q(conn, """SELECT value FROM pss_inventory_rules
                          WHERE tenant_id=? AND rule_code='HOLD_EXPIRY' AND status='active'""", (tenant_id,), one=True)
        try:
            return max(1, int(float(rule['value'] if rule else 20)))
        except Exception:
            return 20

    def _inv_release_expired(self, conn, tenant_id, actor=None):
        expired = q(conn, """SELECT * FROM pss_bookings
                             WHERE tenant_id=? AND booking_status='hold' AND hold_expires_at IS NOT NULL
                             AND hold_expires_at < ?""", (tenant_id, now_iso()))
        for b in expired:
            conn.execute("DELETE FROM pss_bookings WHERE id=? AND tenant_id=?", (b['id'], tenant_id))
            if b.get('flight_id'):
                conn.execute("UPDATE pss_flights SET available_seats=available_seats+1, updated_at=? WHERE id=? AND tenant_id=?",
                             (now_iso(), b['flight_id'], tenant_id))
            self._inv_log(conn, tenant_id, 'pss_booking', b['id'], 'hold_expired_released',
                          dict(b), {'booking_status': 'released'}, actor or 'system')
            log_audit(conn, tenant_id, actor, 'inventory_hold_expired', 'pss_booking', b['id'], {'pnr': b.get('pnr')})
        return len(expired)

    def _inv_baseline(self, conn, tenant_id):
        flights = q(conn, """SELECT * FROM pss_flights WHERE tenant_id=?
                             ORDER BY flight_date, dep_time, flight_no""", (tenant_id,))
        ts = now_iso()
        for f in flights:
            leg = q(conn, "SELECT * FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=? ORDER BY leg_no LIMIT 1",
                    (tenant_id, f['id']), one=True)
            if not leg:
                cur = conn.execute("""INSERT INTO pss_inventory_legs
                                      (tenant_id,flight_id,leg_no,origin,destination,flight_date,dep_time,arr_time,aircraft,status,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
                                   (tenant_id, f['id'], 1, f['origin'], f['destination'], f['flight_date'],
                                    f.get('dep_time'), f.get('arr_time'), f.get('aircraft'), ts, ts))
                leg = {'id': cur.lastrowid}
            seg = q(conn, "SELECT id FROM pss_inventory_segments WHERE tenant_id=? AND leg_id=? ORDER BY segment_no LIMIT 1",
                    (tenant_id, leg['id']), one=True)
            if not seg:
                conn.execute("""INSERT INTO pss_inventory_segments
                                (tenant_id,leg_id,segment_no,origin,destination,dep_time,arr_time,cabin,subclass,seats,status,supplementary_info,created_at,updated_at)
                                VALUES (?,?,?,?,?,?,?,'economy','Y',?, 'active','',?,?)""",
                             (tenant_id, leg['id'], 1, f['origin'], f['destination'], f.get('dep_time'),
                              f.get('arr_time'), f.get('total_seats') or 0, ts, ts))
        return flights

    def _inv_counts(self, conn, tenant_id, flight_id):
        rows = q(conn, """SELECT booking_status, COUNT(*) n FROM pss_bookings
                          WHERE tenant_id=? AND flight_id=? GROUP BY booking_status""", (tenant_id, flight_id))
        counts = {str(r['booking_status'] or 'unknown'): int(r['n'] or 0) for r in rows}
        committed = sum(v for k, v in counts.items() if k not in ('cancelled', 'refunded', 'void'))
        return counts, committed

    def _inv_capacity(self, conn, tenant_id, flight):
        seg = q(conn, """SELECT COALESCE(SUM(seats),0) n FROM pss_inventory_segments
                         WHERE tenant_id=? AND leg_id IN (SELECT id FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=?)
                         AND status='active'""", (tenant_id, tenant_id, flight['id']), one=True)
        return int((seg or {}).get('n') or 0) or int(flight.get('total_seats') or 0)

    def _inv_compute(self, conn, tenant_id, flight, pos, subclass, segment_id=0, _nest_ctx=None):
        """Compute availability for a flight/POS/subclass/segment.
        Nesting semantics:
        - serial: hierarchical buckets; higher-value subclass consumes from shared nest capacity, lower subclasses limited by remaining after higher-priority protected seats.
        - parallel: independent buckets per subclass (each limited by its own bucket/cap).
        - mixed: subclass gets min(serial bucket, parallel bucket) style shared/independent blend.
        Rules with nest_type apply to that mode; nest_bucket can target subclass list (e.g. Y,J).
        """
        capacity = self._inv_capacity(conn, tenant_id, flight)
        seg_id = self._oid(segment_id) or 0
        subclasses = ['Y','J','F']
        # committed per subclass
        committed_by = {}
        for sc in subclasses:
            params = [tenant_id, flight['id'], sc]
            sql = """SELECT COUNT(*) n FROM pss_bookings b
                     LEFT JOIN pss_channels c ON c.id=b.channel_id
                     WHERE b.tenant_id=? AND b.flight_id=? AND COALESCE(b.booking_status,'confirmed') NOT IN ('cancelled','refunded','void')
                       AND b.fare_class=?"""
            if pos != 'ALL':
                sql += " AND c.code=?"; params.append(pos)
            committed_by[sc] = int((q(conn, sql, params, one=True) or {}).get('n') or 0)
        committed_all = sum(committed_by.values()) if subclass=='ALL' else committed_by.get(subclass,0)

        rules = q(conn, """SELECT * FROM pss_inventory_rules
                           WHERE tenant_id=? AND status='active'
                           AND (pos='ALL' OR pos=?) AND (subclass='ALL' OR subclass=? OR nest_type IN ('serial','parallel','mixed'))
                           AND (flight_id IS NULL OR flight_id=?)
                           AND (flight_date IS NULL OR flight_date='' OR flight_date=?)
                           AND (segment_id IS NULL OR segment_id=?)
                           ORDER BY priority ASC, id ASC""",
                  (tenant_id, pos, subclass, flight['id'], flight.get('flight_date'), seg_id or None))

        def _rule_applies(r, sc):
            rsc = str(r.get('subclass') or 'ALL').upper()
            bucket = [x.strip().upper() for x in str(r.get('nest_bucket') or '').split(',') if x.strip()]
            if bucket:
                return sc.upper() in bucket or rsc=='ALL'
            return rsc=='ALL' or rsc==sc.upper()

        # base caps by subclass
        base = {sc: capacity for sc in subclasses}
        mode = 'none'
        # determine dominant nesting mode for requested scope
        modes = [str(r.get('nest_type') or 'none').lower() for r in rules if str(r.get('nest_type') or 'none').lower() in ('serial','parallel','mixed')]
        if modes:
            # priority: explicit rule for subclass > any; pick most common/highest priority already ordered
            mode = modes[0]

        # apply non-nesting limits/gates/TIA as base adjustments
        for sc in subclasses:
            cap = capacity
            for r in rules:
                if not _rule_applies(r, sc):
                    continue
                nest = str(r.get('nest_type') or 'none').lower()
                if nest in ('serial','parallel','mixed'):
                    continue
                try: value = float(r.get('value') or 0)
                except Exception: value = 0
                rtype = str(r.get('rule_type') or '')
                if rtype == 'limit' and value > 0:
                    cap = min(cap, int(value))
                elif rtype == 'gate' and value > 0:
                    threshold = value / 100 if value > 1 else value
                    if capacity and committed_by.get(sc,0) / capacity >= threshold:
                        cap = min(cap, committed_by.get(sc,0))
                elif rtype == 'pos_subclass' and value > 0:
                    cap = max(0, cap - int(value))
                if r.get('tia_enabled') and r.get('tia_percent'):
                    cap += int(capacity * float(r.get('tia_percent') or 0) / 100)
            base[sc] = max(0, int(cap))

        # nesting buckets
        nest_caps = {sc: base[sc] for sc in subclasses}
        if mode in ('serial','parallel','mixed'):
            # collect per-mode bucket limits
            serial_limits = {sc: base[sc] for sc in subclasses}
            parallel_limits = {sc: base[sc] for sc in subclasses}
            mixed_limits = {sc: base[sc] for sc in subclasses}
            for r in rules:
                nest = str(r.get('nest_type') or 'none').lower()
                if nest not in ('serial','parallel','mixed'):
                    continue
                try: value = float(r.get('value') or 0)
                except Exception: value = 0
                rtype = str(r.get('rule_type') or '')
                for sc in subclasses:
                    if not _rule_applies(r, sc):
                        continue
                    target = serial_limits if nest=='serial' else parallel_limits if nest=='parallel' else mixed_limits
                    if rtype == 'limit' and value > 0:
                        target[sc] = min(target[sc], int(value))
                    elif rtype == 'pos_subclass' and value > 0:
                        target[sc] = max(0, target[sc] - int(value))
                    elif rtype == 'gate' and value > 0:
                        threshold = value / 100 if value > 1 else value
                        if capacity and committed_by.get(sc,0) / capacity >= threshold:
                            target[sc] = min(target[sc], committed_by.get(sc,0))
                    if r.get('tia_enabled') and r.get('tia_percent'):
                        target[sc] += int(capacity * float(r.get('tia_percent') or 0) / 100)
                        target[sc] = max(0, int(target[sc]))

            if mode == 'parallel':
                nest_caps = {sc: max(0, int(parallel_limits[sc])) for sc in subclasses}
            elif mode == 'serial':
                # Serial nesting: Y highest priority, then J, then F from remaining capacity.
                # Each class bucket cannot exceed its limit; cumulative commitment from higher classes reduces availability.
                remaining = capacity
                # optionally a shared serial cap rule for ALL can set remaining
                for r in rules:
                    if str(r.get('nest_type') or '').lower()=='serial' and str(r.get('subclass') or 'ALL').upper()=='ALL' and str(r.get('rule_type') or '')=='limit' and float(r.get('value') or 0)>0:
                        remaining = min(remaining, int(float(r.get('value') or 0)))
                for sc in ['Y','J','F']:
                    limit_sc = max(0, int(serial_limits[sc]))
                    # available for this class = min(class limit, remaining capacity) but cannot steal seats already committed by higher classes except accounting
                    nest_caps[sc] = min(limit_sc, remaining)
                    remaining = max(0, remaining - max(committed_by.get(sc,0), 0))
                    # ensure remaining doesn't go below 0
                # serial protection: lower class cannot exceed remaining after higher committed
                nest_caps['J'] = min(nest_caps['J'], max(0, capacity - committed_by.get('Y',0)))
                nest_caps['F'] = min(nest_caps['F'], max(0, capacity - committed_by.get('Y',0) - committed_by.get('J',0)))
            elif mode == 'mixed':
                # Mixed: parallel per-class limits intersected with serial shared protection.
                remaining = capacity
                for r in rules:
                    if str(r.get('nest_type') or '').lower()=='mixed' and str(r.get('subclass') or 'ALL').upper()=='ALL' and str(r.get('rule_type') or '')=='limit' and float(r.get('value') or 0)>0:
                        remaining = min(remaining, int(float(r.get('value') or 0)))
                serial_view = {}
                for sc in ['Y','J','F']:
                    serial_view[sc] = min(max(0,int(mixed_limits[sc])), remaining)
                    remaining = max(0, remaining - committed_by.get(sc,0))
                nest_caps['Y'] = max(0, min(int(parallel_limits['Y']), int(serial_view['Y'])))
                nest_caps['J'] = max(0, min(int(parallel_limits['J']), int(serial_view['J']), capacity - committed_by.get('Y',0)))
                nest_caps['F'] = max(0, min(int(parallel_limits['F']), int(serial_view['F']), capacity - committed_by.get('Y',0) - committed_by.get('J',0)))

        # final cap/available
        if subclass == 'ALL':
            # ALL reflects total sellable: sum of nested buckets capped by capacity
            total_bucket = sum(nest_caps.get(sc,0) for sc in subclasses)
            cap = min(capacity, total_bucket) if mode in ('parallel','mixed') else min(capacity, max(nest_caps.values()) if nest_caps else capacity)
            if mode == 'serial':
                cap = min(capacity, nest_caps.get('Y',capacity))
            committed = sum(committed_by.values())
        else:
            sc = subclass.upper()
            cap = min(capacity, int(nest_caps.get(sc, base.get(sc, capacity))))
            committed = committed_by.get(sc, 0)
        available = max(0, int(cap) - int(committed))
        return {
            'capacity': int(capacity), 'adjusted_capacity': int(cap), 'committed': int(committed), 'available': int(available),
            'nesting': {'mode': mode, 'buckets': nest_caps, 'committed_by_subclass': committed_by}
        }


    def _inv_publish(self, conn, tenant_id, actor=None):
        flights = self._inv_baseline(conn, tenant_id)
        channels = ['ALL'] + [str(r['code']) for r in q(conn, "SELECT code FROM pss_channels WHERE tenant_id=? AND status='active'", (tenant_id,))]
        subclasses = ['ALL', 'Y', 'J', 'F']
        published = 0
        ts = now_iso()
        for f in flights:
            legs = q(conn, "SELECT id FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=?", (tenant_id, f['id']))
            segs = q(conn, """SELECT id FROM pss_inventory_segments
                              WHERE tenant_id=? AND leg_id IN (SELECT id FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=?)""",
                     (tenant_id, tenant_id, f['id']))
            seg_ids = ([0] + [int(s['id']) for s in segs]) if segs else [0]
            for pos in channels:
                for sid in seg_ids:
                    for subclass in subclasses:
                        seg_vals = self._inv_compute(conn, tenant_id, f, pos, subclass, sid)
                        existing = q(conn, """SELECT id,version FROM pss_inventory_availability
                                              WHERE tenant_id=? AND flight_id=? AND segment_id=? AND pos=? AND pos_subclass=?""",
                                     (tenant_id, f['id'], sid, pos, subclass), one=True)
                        if existing:
                            conn.execute("""UPDATE pss_inventory_availability
                                            SET leg_id=?,available=?,capacity=?,committed=?,published_at=?,version=version+1,updated_at=?
                                            WHERE id=?""",
                                         (int(legs[0]['id']) if legs else 0, seg_vals['available'], seg_vals['adjusted_capacity'],
                                          seg_vals['committed'], ts, ts, existing['id']))
                        else:
                            conn.execute("""INSERT INTO pss_inventory_availability
                                            (tenant_id,flight_id,leg_id,segment_id,pos,pos_subclass,available,capacity,committed,published_at,version,created_at,updated_at)
                                            VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                                         (tenant_id, f['id'], int(legs[0]['id']) if legs else 0, sid, pos, subclass,
                                          seg_vals['available'], seg_vals['adjusted_capacity'], seg_vals['committed'], ts, ts, ts))
                        published += 1
        if actor:
            self._inv_log(conn, tenant_id, 'availability', 'ALL', 'published', {}, {'rows': published}, actor)
        return published

    def api_inventory_dates(self, tenant_id, user_id):
        with db() as conn:
            self._inv_release_expired(conn, tenant_id, user_id)
            flights = self._inv_baseline(conn, tenant_id)
            output = []
            dates = []
            for f in flights:
                counts, committed = self._inv_counts(conn, tenant_id, f['id'])
                capacity = self._inv_capacity(conn, tenant_id, f)
                legs = q(conn, "SELECT * FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=? ORDER BY leg_no", (tenant_id, f['id']))
                segs = q(conn, """SELECT s.*, l.leg_no FROM pss_inventory_segments s
                                  JOIN pss_inventory_legs l ON l.id=s.leg_id
                                  WHERE s.tenant_id=? AND l.flight_id=? ORDER BY l.leg_no, s.segment_no""", (tenant_id, f['id']))
                row = dict(f)
                row['reservation_counts'] = counts
                row['reservation_committed'] = committed
                row['reservation_holds'] = counts.get('hold', 0)
                row['capacity'] = capacity
                row['overbooked'] = committed > capacity or int(f.get('available_seats') or 0) < 0
                row['legs'] = legs
                row['segments'] = segs
                output.append(row)
                if f.get('flight_date') not in dates:
                    dates.append(f.get('flight_date'))
        return self._json({'dates': dates, 'flights': output, 'flight_date': (dates[0] if dates else '')})

    def api_inventory_legs(self, tenant_id, user_id, method, rid=None):
        data = read_json(self) if method in ('POST', 'PUT') else {}
        rid = self._oid(rid)
        with db() as conn:
            current = q(conn, "SELECT * FROM pss_inventory_legs WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) if rid else None
            if method == 'DELETE':
                if not current:
                    return self._json({'error': 'Leg not found'}, 404)
                conn.execute("DELETE FROM pss_inventory_segments WHERE tenant_id=? AND leg_id=?", (tenant_id, rid))
                conn.execute("DELETE FROM pss_inventory_legs WHERE id=? AND tenant_id=?", (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'leg', rid, 'deleted', dict(current), {}, user_id)
                log_audit(conn, tenant_id, user_id, 'inventory_leg_deleted', 'pss_inventory_legs', rid, {})
                return self._json({'ok': True})
            if not data.get('flight_id') or not data.get('origin') or not data.get('destination'):
                return self._json({'error': 'flight_id, origin and destination are required'}, 400)
            fid = self._oid(data.get('flight_id'))
            try:
                leg_no = int(data.get('leg_no') or 0)
            except Exception:
                leg_no = 0
            if leg_no <= 0:
                leg_no = (q(conn, "SELECT COALESCE(MAX(leg_no),0)+1 n FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=?",
                            (tenant_id, fid), one=True) or {}).get('n', 1)
            duplicate = q(conn, "SELECT id FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=? AND leg_no=? AND id<>?",
                          (tenant_id, fid, leg_no, rid or 0), one=True)
            if duplicate:
                return self._json({'error': f'Leg {leg_no} already exists for this flight'}, 400)
            values = (tenant_id, fid, leg_no,
                      str(data.get('origin') or '').upper(), str(data.get('destination') or '').upper(),
                      data.get('flight_date'), data.get('dep_time'), data.get('arr_time'), data.get('aircraft'),
                      data.get('status') or 'active', now_iso())
            if current:
                conn.execute("""UPDATE pss_inventory_legs SET flight_id=?,leg_no=?,origin=?,destination=?,flight_date=?,dep_time=?,arr_time=?,aircraft=?,status=?,updated_at=?
                                WHERE id=? AND tenant_id=?""", values[1:] + (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'leg', rid, 'updated', dict(current), data, user_id)
            else:
                cur = conn.execute("""INSERT INTO pss_inventory_legs
                                      (tenant_id,flight_id,leg_no,origin,destination,flight_date,dep_time,arr_time,aircraft,status,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", values + (now_iso(),))
                rid = cur.lastrowid
                self._inv_log(conn, tenant_id, 'leg', rid, 'created', {}, data, user_id)
            log_audit(conn, tenant_id, user_id, 'inventory_leg_saved', 'pss_inventory_legs', rid, data)
        return self._json({'ok': True, 'id': rid})

    def api_inventory_segments(self, tenant_id, user_id, method, rid=None):
        data = read_json(self) if method in ('POST', 'PUT') else {}
        rid = self._oid(rid)
        with db() as conn:
            current = q(conn, "SELECT * FROM pss_inventory_segments WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) if rid else None
            if method == 'DELETE':
                if not current:
                    return self._json({'error': 'Segment not found'}, 404)
                conn.execute("DELETE FROM pss_inventory_segments WHERE id=? AND tenant_id=?", (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'segment', rid, 'deleted', dict(current), {}, user_id)
                log_audit(conn, tenant_id, user_id, 'inventory_segment_deleted', 'pss_inventory_segments', rid, {})
                return self._json({'ok': True})
            if not data.get('leg_id') or not data.get('origin') or not data.get('destination'):
                return self._json({'error': 'leg_id, origin and destination are required'}, 400)
            lid = self._oid(data.get('leg_id'))
            try:
                segment_no = int(data.get('segment_no') or 0)
            except Exception:
                segment_no = 0
            if segment_no <= 0:
                segment_no = (q(conn, "SELECT COALESCE(MAX(segment_no),0)+1 n FROM pss_inventory_segments WHERE tenant_id=? AND leg_id=?",
                                (tenant_id, lid), one=True) or {}).get('n', 1)
            duplicate = q(conn, "SELECT id FROM pss_inventory_segments WHERE tenant_id=? AND leg_id=? AND segment_no=? AND id<>?",
                          (tenant_id, lid, segment_no, rid or 0), one=True)
            if duplicate:
                return self._json({'error': f'Segment {segment_no} already exists for this leg'}, 400)
            try:
                seats = int(data.get('seats') or 0)
            except Exception:
                seats = 0
            values = (tenant_id, lid, segment_no,
                      str(data.get('origin') or '').upper(), str(data.get('destination') or '').upper(),
                      data.get('dep_time'), data.get('arr_time'), data.get('cabin') or 'economy',
                      str(data.get('subclass') or 'Y').upper(), seats,
                      data.get('status') or 'active', data.get('supplementary_info') or '', now_iso())
            if current:
                conn.execute("""UPDATE pss_inventory_segments SET leg_id=?,segment_no=?,origin=?,destination=?,dep_time=?,arr_time=?,cabin=?,subclass=?,seats=?,status=?,supplementary_info=?,updated_at=?
                                WHERE id=? AND tenant_id=?""", values[1:] + (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'segment', rid, 'updated', dict(current), data, user_id)
            else:
                cur = conn.execute("""INSERT INTO pss_inventory_segments
                                      (tenant_id,leg_id,segment_no,origin,destination,dep_time,arr_time,cabin,subclass,seats,status,supplementary_info,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values + (now_iso(),))
                rid = cur.lastrowid
                self._inv_log(conn, tenant_id, 'segment', rid, 'created', {}, data, user_id)
            log_audit(conn, tenant_id, user_id, 'inventory_segment_saved', 'pss_inventory_segments', rid, data)
        return self._json({'ok': True, 'id': rid})

    def api_inventory_availability(self, tenant_id, qs):
        pos = (qs.get('pos') or ['ALL'])[0]
        subclass = (qs.get('subclass') or ['ALL'])[0]
        flight_id = self._oid((qs.get('flight_id') or [''])[0])
        segment_id = self._oid((qs.get('segment_id') or [''])[0])
        with db() as conn:
            self._inv_publish(conn, tenant_id)
            sql = """SELECT a.*, f.flight_no, f.origin, f.destination, f.flight_date
                     FROM pss_inventory_availability a JOIN pss_flights f ON f.id=a.flight_id
                     WHERE a.tenant_id=? AND (a.pos=? OR a.pos='ALL') AND (a.pos_subclass=? OR a.pos_subclass='ALL')"""
            args = [tenant_id, pos, subclass]
            if flight_id:
                sql += " AND a.flight_id=?"; args.append(flight_id)
            if segment_id is not None:
                sql += " AND a.segment_id=?"; args.append(segment_id or 0)
            sql += " ORDER BY f.flight_date, f.flight_no, a.segment_id, a.pos, a.pos_subclass LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'availability': rows, 'pos': pos, 'subclass': subclass,
                           'poll_key': max([str(r.get('published_at') or '') for r in rows] or [''])})

    def api_inventory_availability_publish(self, tenant_id, user_id):
        with db() as conn:
            count = self._inv_publish(conn, tenant_id, user_id)
        return self._json({'ok': True, 'published': count})

    def api_inventory_rules(self, tenant_id, user_id=None, method='GET', rid=None):
        data = read_json(self) if method in ('POST', 'PUT') else {}
        rid = self._oid(rid)
        with db() as conn:
            if method == 'GET':
                return self._json({'rules': q(conn, "SELECT * FROM pss_inventory_rules WHERE tenant_id=? ORDER BY rule_code", (tenant_id,))})
            current = q(conn, "SELECT * FROM pss_inventory_rules WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True) if rid else None
            if method == 'DELETE':
                if not current:
                    return self._json({'error': 'Rule not found'}, 404)
                conn.execute("DELETE FROM pss_inventory_rules WHERE id=? AND tenant_id=?", (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'rule', rid, 'deleted', dict(current), {}, user_id)
                return self._json({'ok': True})
            if not data.get('rule_code') or not data.get('rule_name'):
                return self._json({'error': 'rule_code and rule_name are required'}, 400)
            flight_id = self._oid(data.get('flight_id'))
            segment_id = self._oid(data.get('segment_id'))
            values = (tenant_id, str(data.get('rule_code') or '').upper(), data.get('rule_name'),
                      data.get('rule_type') or 'limit', data.get('pos') or 'ALL', data.get('subclass') or 'ALL',
                      float(data.get('value') or 0), 1 if data.get('tia_enabled') else 0,
                      float(data.get('tia_percent') or 0), data.get('status') or 'active',
                      data.get('effective_from'), data.get('effective_to'),
                      flight_id, data.get('flight_date') or None, segment_id,
                      data.get('nest_type') or 'none', data.get('nest_bucket') or None, int(data.get('priority') or 100),
                      now_iso())
            if current:
                conn.execute("""UPDATE pss_inventory_rules SET rule_code=?,rule_name=?,rule_type=?,pos=?,subclass=?,value=?,tia_enabled=?,tia_percent=?,status=?,effective_from=?,effective_to=?,flight_id=?,flight_date=?,segment_id=?,nest_type=?,nest_bucket=?,priority=?,updated_at=?
                                WHERE id=? AND tenant_id=?""", values[1:] + (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'rule', rid, 'updated', dict(current), data, user_id)
            else:
                cur = conn.execute("""INSERT INTO pss_inventory_rules
                                      (tenant_id,rule_code,rule_name,rule_type,pos,subclass,value,tia_enabled,tia_percent,status,effective_from,effective_to,flight_id,flight_date,segment_id,nest_type,nest_bucket,priority,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values + (now_iso(),))
                rid = cur.lastrowid
                self._inv_log(conn, tenant_id, 'rule', rid, 'created', {}, data, user_id)
            log_audit(conn, tenant_id, user_id, 'inventory_rule_saved', 'pss_inventory_rules', rid, data)
        return self._json({'ok': True, 'id': rid})

    def api_inventory_mass_update(self, tenant_id, user_id):
        data = read_json(self)
        ids = data.get('flight_ids') or []
        if isinstance(ids, str):
            ids = [x for x in ids.split(',') if x.strip()]
        ids = [self._oid(x) for x in ids if self._oid(x)]
        with db() as conn:
            if not ids:
                ids = [int(r['id']) for r in q(conn, "SELECT id FROM pss_flights WHERE tenant_id=?", (tenant_id,))]
            preview = []
            changed = 0
            for fid in ids:
                f = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id), one=True)
                if not f:
                    continue
                _, committed = self._inv_counts(conn, tenant_id, fid)
                after = dict(f)
                if 'available_seats' in data:
                    after['available_seats'] = int(data.get('available_seats') or 0)
                if data.get('status'):
                    after['status'] = data['status']
                if data.get('gate'):
                    after['gate'] = data['gate']
                if data.get('base_fare') not in (None, ''):
                    after['base_fare'] = float(data['base_fare'])
                    after['base_fare_reference'] = after['base_fare_reference'] or after['base_fare']
                preview.append({'flight_id': fid, 'flight_no': f['flight_no'], 'committed': committed,
                                'before': {k: f.get(k) for k in ('available_seats', 'status', 'gate', 'base_fare')},
                                'after': {k: after.get(k) for k in ('available_seats', 'status', 'gate', 'base_fare')}})
                if data.get('preview'):
                    continue
                conn.execute("""UPDATE pss_flights SET available_seats=?,status=?,gate=?,base_fare=?,base_fare_reference=?,updated_at=?
                                WHERE id=? AND tenant_id=?""",
                             (after.get('available_seats') or 0, after.get('status') or 'scheduled', after.get('gate'),
                              after.get('base_fare') or 0, after.get('base_fare_reference'), now_iso(), fid, tenant_id))
                self._inv_log(conn, tenant_id, 'flight', fid, 'mass_updated', dict(f), after, user_id)
                changed += 1
                if int(after.get('available_seats') or 0) < 0 or committed > int(f.get('total_seats') or 0):
                    self._inv_queue(conn, tenant_id, 'overbooking', 'critical', fid,
                                    f"{f['flight_no']} has {committed} committed against {f.get('total_seats') or 0} seats")
        return self._json({'preview': preview, 'changed': changed})

    def api_inventory_overbooking(self, tenant_id):
        rows = []
        with db() as conn:
            for f in q(conn, "SELECT * FROM pss_flights WHERE tenant_id=? ORDER BY flight_date", (tenant_id,)):
                counts, committed = self._inv_counts(conn, tenant_id, f['id'])
                capacity = self._inv_capacity(conn, tenant_id, f)
                if committed > capacity or int(f.get('available_seats') or 0) < 0:
                    rows.append({'flight_id': f['id'], 'flight_no': f['flight_no'], 'flight_date': f['flight_date'],
                                 'capacity': capacity, 'committed': committed, 'available_seats': f.get('available_seats'),
                                 'excess': max(0, committed - capacity), 'counts': counts})
        return self._json({'overbookings': rows})

    def api_inventory_overbooking_resolve(self, tenant_id, user_id):
        data = read_json(self)
        fid = self._oid(data.get('flight_id'))
        action = (data.get('action') or 'raise_capacity').strip().lower()
        with db() as conn:
            f = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (fid, tenant_id), one=True)
            if not f:
                return self._json({'error': 'Flight not found'}, 404)
            _, committed = self._inv_counts(conn, tenant_id, fid)
            result = {'action': action, 'committed': committed}
            if action == 'raise_capacity':
                new_total = max(int(f.get('total_seats') or 0), committed)
                conn.execute("UPDATE pss_flights SET total_seats=?,available_seats=?,updated_at=? WHERE id=? AND tenant_id=?",
                             (new_total, max(0, new_total - committed), now_iso(), fid, tenant_id))
                self._inv_baseline(conn, tenant_id)
                conn.execute("""UPDATE pss_inventory_segments SET seats=?,updated_at=?
                                WHERE tenant_id=? AND status='active' AND leg_id IN
                                (SELECT id FROM pss_inventory_legs WHERE tenant_id=? AND flight_id=?)""",
                             (new_total, now_iso(), tenant_id, tenant_id, fid))
                result['total_seats'] = new_total
            elif action == 'deny_sale':
                conn.execute("UPDATE pss_flights SET status='closed',updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), fid, tenant_id))
                result['status'] = 'closed'
            elif action == 'reaccommodate':
                alt = q(conn, """SELECT id,flight_no,available_seats FROM pss_flights
                                 WHERE tenant_id=? AND id<>? AND origin=? AND destination=? AND flight_date>=?
                                   AND COALESCE(status,'scheduled') NOT IN ('cancelled','closed') AND available_seats>0
                                 ORDER BY flight_date, dep_time LIMIT 1""",
                        (tenant_id, fid, f.get('origin'), f.get('destination'), f.get('flight_date')), one=True)
                if not alt:
                    return self._json({'error': 'No alternate flight with seats found'}, 409)
                hold = q(conn, """SELECT id,pnr FROM pss_bookings
                                  WHERE tenant_id=? AND flight_id=? AND COALESCE(booking_status,'confirmed')='hold'
                                  ORDER BY id DESC LIMIT 1""", (tenant_id, fid), one=True)
                if hold:
                    conn.execute("UPDATE pss_bookings SET flight_id=?,updated_at=? WHERE id=? AND tenant_id=?",
                                 (alt['id'], now_iso(), hold['id'], tenant_id))
                    conn.execute("UPDATE pss_flights SET available_seats=available_seats+1,updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), fid, tenant_id))
                    conn.execute("UPDATE pss_flights SET available_seats=available_seats-1,updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), alt['id'], tenant_id))
                    result['rebooked_pnr'] = hold.get('pnr')
                result['alternate_flight'] = alt.get('flight_no')
            else:
                return self._json({'error': 'action must be raise_capacity, deny_sale, or reaccommodate'}, 400)

            conn.execute("""UPDATE pss_inventory_queue SET status='resolved',resolved_at=?,notes=COALESCE(notes,'')||?
                            WHERE tenant_id=? AND flight_id=? AND issue_type='overbooking' AND status='open'""",
                         (now_iso(), f" [{action} by {user_id}]", tenant_id, fid))
            self._inv_log(conn, tenant_id, 'flight', fid, 'overbooking_' + action, dict(f), result, user_id)
            log_audit(conn, tenant_id, user_id, 'inventory_overbooking_' + action, 'pss_flight', fid, result)
        return self._json({'ok': True, **result})

    def api_inventory_history(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT * FROM pss_inventory_history WHERE tenant_id=?
                              ORDER BY id DESC LIMIT 300""", (tenant_id,))
        return self._json({'history': rows})

    def api_inventory_queue(self, tenant_id, method='GET', qid=None, user_id=None):
        qid = self._oid(qid)
        with db() as conn:
            if method == 'PUT' and qid:
                data = read_json(self)
                row = q(conn, "SELECT * FROM pss_inventory_queue WHERE id=? AND tenant_id=?", (qid, tenant_id), one=True)
                if not row:
                    return self._json({'error': 'Queue item not found'}, 404)
                conn.execute("""UPDATE pss_inventory_queue SET assignee=?,severity=?,notes=?,status=?,resolved_at=?
                                WHERE id=? AND tenant_id=?""",
                             (data.get('assignee', row.get('assignee')), data.get('severity', row.get('severity')),
                              data.get('notes', row.get('notes')), data.get('status', row.get('status')),
                              now_iso() if data.get('status') == 'resolved' else row.get('resolved_at'), qid, tenant_id))
                self._inv_log(conn, tenant_id, 'queue', qid, 'queue_updated', dict(row), data, user_id)
                return self._json({'ok': True})
            rows = q(conn, """SELECT q.*, f.flight_no, f.flight_date FROM pss_inventory_queue q
                              LEFT JOIN pss_flights f ON f.id=q.flight_id
                              WHERE q.tenant_id=? ORDER BY CASE q.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, q.id DESC""",
                     (tenant_id,))
        return self._json({'queue': rows})

    def api_inventory_queue_resolve(self, tenant_id, user_id, rid):
        rid = self._oid(rid)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_inventory_queue WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Queue item not found'}, 404)
            conn.execute("UPDATE pss_inventory_queue SET status='resolved',resolved_at=? WHERE id=? AND tenant_id=?",
                         (now_iso(), rid, tenant_id))
            self._inv_log(conn, tenant_id, 'queue', rid, 'resolved', dict(row), {'status': 'resolved'}, user_id)
        return self._json({'ok': True})

    def api_inventory_valuation(self, tenant_id, user_id=None, method='GET', rid=None):
        if method == 'GET':
            with db() as conn:
                stored = q(conn, """SELECT v.*, f.flight_no, f.origin, f.destination, f.flight_date
                                    FROM pss_inventory_valuation v JOIN pss_flights f ON f.id=v.flight_id
                                    WHERE v.tenant_id=? ORDER BY f.flight_date, f.flight_no""", (tenant_id,))
                computed = []
                for f in self._inv_baseline(conn, tenant_id):
                    _, committed = self._inv_counts(conn, tenant_id, f['id'])
                    load = committed / max(1, int(f.get('total_seats') or 0))
                    computed.append({'flight_id': f['id'], 'flight_no': f['flight_no'], 'flight_date': f['flight_date'],
                                     'committed': committed, 'load_factor': round(load, 3),
                                     'valuation': round(float(f.get('base_fare') or 0) * committed * (1 + load), 2)})
            return self._json({'valuations': stored, 'computed': computed})
        data = read_json(self)
        rid = self._oid(rid)
        with db() as conn:
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_inventory_valuation WHERE id=? AND tenant_id=?", (rid, tenant_id))
                self._inv_log(conn, tenant_id, 'valuation', rid, 'deleted', {}, {}, user_id)
                return self._json({'ok': True})
            if method == 'PUT':
                current = q(conn, "SELECT * FROM pss_inventory_valuation WHERE id=? AND tenant_id=?", (rid, tenant_id), one=True)
                if not current:
                    return self._json({'error': 'Valuation not found'}, 404)
                existing = {'id': current['id']}
                key = (tenant_id, current['flight_id'], current['segment_id'], current['pos'], current['subclass'])
            else:
                if not data.get('flight_id'):
                    return self._json({'error': 'flight_id required'}, 400)
                key = (tenant_id, self._oid(data.get('flight_id')), self._oid(data.get('segment_id')) or 0,
                       data.get('pos') or 'ALL', data.get('subclass') or 'ALL')
                existing = q(conn, """SELECT id FROM pss_inventory_valuation
                                      WHERE tenant_id=? AND flight_id=? AND segment_id=? AND pos=? AND subclass=?""", key, one=True)
            values = (float(data.get('valuation') or 0), 1 if data.get('active', True) else 0, now_iso())
            if existing:
                conn.execute("""UPDATE pss_inventory_valuation SET valuation=?,active=?,updated_at=? WHERE id=?""", values + (existing['id'],))
                rid = existing['id']
            else:
                cur = conn.execute("""INSERT INTO pss_inventory_valuation
                                      (tenant_id,flight_id,segment_id,pos,subclass,valuation,active,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?)""", key + values)
                rid = cur.lastrowid
            self._inv_log(conn, tenant_id, 'valuation', rid, 'saved', {}, data, user_id)
        return self._json({'ok': True, 'id': rid})

    def api_inventory_uncommitted(self, tenant_id, user_id):
        with db() as conn:
            released = self._inv_release_expired(conn, tenant_id, user_id)
            hold_minutes = self._inv_hold_minutes(conn, tenant_id)
            rows = q(conn, """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, c.code pos
                              FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                              LEFT JOIN pss_channels c ON c.id=b.channel_id
                              WHERE b.tenant_id=? AND b.booking_status='hold' ORDER BY b.hold_expires_at, b.id""", (tenant_id,))
        return self._json({'holds': rows, 'hold_minutes': hold_minutes, 'expired_released': released})

    def api_inventory_uncommitted_action(self, tenant_id, user_id, rid, action):
        rid = self._oid(rid)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=? AND booking_status='hold'", (rid, tenant_id), one=True)
            if not b:
                return self._json({'error': 'Uncommitted booking not found'}, 404)
            if action == 'release':
                conn.execute("DELETE FROM pss_bookings WHERE id=? AND tenant_id=?", (rid, tenant_id))
                if b.get('flight_id'):
                    conn.execute("UPDATE pss_flights SET available_seats=available_seats+1,updated_at=? WHERE id=? AND tenant_id=?",
                                 (now_iso(), b['flight_id'], tenant_id))
                self._inv_log(conn, tenant_id, 'pss_booking', rid, 'hold_released', dict(b), {}, user_id)
                self._trace(conn, tenant_id, 'counter', 'pss_booking', rid, 'hold_released', user_id, b.get('pnr'), None, self)
            else:
                conn.execute("""UPDATE pss_bookings SET booking_status='confirmed',hold_expires_at=NULL,updated_at=?
                                WHERE id=? AND tenant_id=?""", (now_iso(), rid, tenant_id))
                self._inv_log(conn, tenant_id, 'pss_booking', rid, 'hold_confirmed', dict(b),
                              {'booking_status': 'confirmed'}, user_id)
                self._trace(conn, tenant_id, 'counter', 'pss_booking', rid, 'hold_confirmed', user_id, b.get('pnr'), None, self)
            log_audit(conn, tenant_id, user_id, 'inventory_hold_' + action, 'pss_booking', rid, {'pnr': b.get('pnr')})
        return self._json({'ok': True})

    def api_inventory_market(self, tenant_id):
        with db() as conn:
            live = q(conn, """SELECT f.origin, f.destination, f.flight_date, COUNT(DISTINCT f.id) flights,
                                     COUNT(b.id) bookings, COALESCE(SUM(b.total_amount),0) revenue
                              FROM pss_flights f LEFT JOIN pss_bookings b ON b.flight_id=f.id AND b.tenant_id=f.tenant_id
                              WHERE f.tenant_id=?
                              GROUP BY f.origin,f.destination,f.flight_date ORDER BY f.flight_date DESC""", (tenant_id,))
            stored = q(conn, "SELECT * FROM pss_inventory_market WHERE tenant_id=? ORDER BY flight_date DESC", (tenant_id,))
        return self._json({'live': live, 'market': stored})

    def api_inventory_market_refresh(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT f.origin, f.destination, f.flight_date, COUNT(b.id) bookings,
                                     COALESCE(SUM(b.total_amount),0) revenue
                              FROM pss_flights f LEFT JOIN pss_bookings b ON b.flight_id=f.id AND b.tenant_id=f.tenant_id
                              WHERE f.tenant_id=? GROUP BY f.origin,f.destination,f.flight_date""", (tenant_id,))
            ts = now_iso()
            for r in rows:
                market = f"{r['origin']}-{r['destination']}"
                existing = q(conn, """SELECT id FROM pss_inventory_market
                                      WHERE tenant_id=? AND market=? AND pos='ALL' AND flight_date=?""",
                             (tenant_id, market, r['flight_date']), one=True)
                demand = int(r['bookings'] or 0) * 2
                if existing:
                    conn.execute("UPDATE pss_inventory_market SET demand=?,bookings=?,revenue=?,updated_at=? WHERE id=?",
                                 (demand, r['bookings'] or 0, r['revenue'] or 0, ts, existing['id']))
                else:
                    conn.execute("""INSERT INTO pss_inventory_market
                                    (tenant_id,market,pos,origin,destination,flight_date,demand,bookings,revenue,updated_at)
                                    VALUES (?,?,'ALL',?,?,?,?,?,?,?)""",
                                 (tenant_id, market, r['origin'], r['destination'], r['flight_date'],
                                  demand, r['bookings'] or 0, r['revenue'] or 0, ts))
        return self._json({'ok': True, 'refreshed': len(rows)})

    def api_inventory_help(self):
        return self._json({
            'articles': [
                {'title': 'Flight date inventory', 'body': 'Open Flight Inventory, choose a flight date tab, then inspect legs, segments, reservation impact and capacity.'},
                {'title': 'Leg and Segment controls', 'body': 'Use Legs and Segments tabs to add, edit or delete itinerary structure. Segment-level availability is published per segment; rule scope can target flight/date/segment.'},
                {'title': 'Availability and POS', 'body': 'Choose Point of Sale and subclass, then Publish availability. Availability is calculated as capacity-committed with Segment limit, Gating, POS subclass logic, TIA and nesting adjustments. Screen polls every 15 seconds.'},
                {'title': 'Business rules and TIA', 'body': 'Rules support Segment limit, Gating, POS subclass logic, Hold expiry. Scope by flight/date/segment, POS/subclass, nesting (serial/parallel/mixed), priority, and TIA % adjustment.'},
                {'title': 'Overbooking', 'body': 'Overbooking tab lists flights where committed reservations exceed capacity. Actions: raise capacity, deny sale, or reaccommodate latest hold to alternate flight. Use History/Queue for audit.'},
                {'title': 'Uncommitted bookings', 'body': 'Holds expire automatically when accessed after expiry. Confirm converts a hold to a reservation; release restores one seat.'},
                {'title': 'Market data & valuation', 'body': 'Market data snapshots internal route/date demand and average fare. Active valuation computes load-factor value; manual valuations can be stored as active.'},
                {'title': 'Self-service support', 'body': 'Use this Help tab first. If an action fails, copy the error/toast and check docs or contact your administrator.'}
            ],
            'docs': [
                {'title': 'README', 'url': '/README.md'},
                {'title': 'Deployment', 'url': '/DEPLOYMENT.md'},
                {'title': 'API Docs', 'url': '/api/public/v1/docs'}
            ]
        })

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
                old = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
                if not old:
                    return self._json({'error': 'Booking not found'}, 404)
                if old.get('flight_id') and str(old.get('booking_status') or '') not in ('cancelled', 'refunded', 'void'):
                    conn.execute("UPDATE pss_flights SET available_seats=available_seats+1,updated_at=? WHERE id=? AND tenant_id=?",
                                 (now_iso(), old['flight_id'], tenant_id))
                conn.execute("DELETE FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            if method == 'PUT':
                fields = ['passenger_name','passenger_type','contact_email','contact_phone','fare_class','fare_amount','taxes',
                          'total_amount','booking_status','payment_status','ticket_status','seat_no','airport_code','channel_id',
                          'fare_basis','service_fee','commission','baggage_allowance_kg','doc_no','issue_date','issued_by','currency',
                          'dob','gender','nationality','passport_no','passport_expiry','passport_country','title','segments_json','ssr_codes','special_requests']
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
            conn.execute("""INSERT INTO pss_bookings(tenant_id,pnr,flight_id,passenger_name,passenger_type,contact_email,contact_phone,channel_id,airport_code,fare_class,fare_amount,taxes,total_amount,currency,ticket_status,payment_status,booking_status,created_by,created_at,updated_at,dob,gender,nationality,passport_no,passport_expiry,passport_country,title,segments_json,ssr_codes,special_requests)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unissued','pending','confirmed',?,?,?, ?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, pnr, fid, name, data.get('passenger_type') or 'ADT', data.get('contact_email'),
                          data.get('contact_phone'), self._oid(data.get('channel_id')), data.get('airport_code') or flight.get('origin'),
                          data.get('fare_class') or 'Y', fare, taxes, total, cur, user_id, now_iso(), now_iso(),
                          data.get('dob'), data.get('gender'), data.get('nationality'), data.get('passport_no'),
                          data.get('passport_expiry'), data.get('passport_country'), data.get('title'),
                          data.get('segments_json'), data.get('ssr_codes'), data.get('special_requests')))
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
            ticket = self._pss_next_ticket(conn, tenant_id, row.get('channel_id'), row.get('airport_code'),
                                           agency_id=row.get('agency_id'), strict_agency=bool(row.get('agency_id')))
            if not ticket:
                return self._json({'error': 'No ticket stock available - allocate stock first'}, 400)
            conn.execute("UPDATE pss_bookings SET ticket_number=?, ticket_status='issued', updated_at=? WHERE id=? AND tenant_id=?",
                         (ticket, now_iso(), bid, tenant_id))
            self._activity(conn, tenant_id, 'pss_booking', bid, 'ticket_issued', ticket, user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'ticket_issued', user_id, ticket, row.get('trace_id'), self)
            self._doc_audit(conn, tenant_id, 'eticket', ticket, bid, row.get('pnr'), 'ticket_issued', user_id, row.get('trace_id'), {'ticket_number': ticket})
            try:
                self._erp_sync_pss_booking(conn, tenant_id, bid, actor=user_id or 'counter', source='counter')
            except Exception:
                pass
            log_audit(conn, tenant_id, user_id, 'issue_ticket', 'pss_booking', bid, {'ticket_number': ticket})
        return self._json({'ok': True, 'ticket_number': ticket})

    def api_pss_booking_checkin(self, tenant_id, user_id, bid, sess=None, data=None):
        bid = self._oid(bid)
        data = data if data is not None else read_json(self)
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
            bags = ', '.join(self._bag_tag_list(data.get('bag_tags') or '', bag_count))
            cur = conn.execute("""INSERT INTO pss_checkins(tenant_id,checkin_no,booking_id,pnr,flight_id,passenger_name,seat_no,bag_count,bag_tags,checkin_method,counter,agent,status,checked_in_at,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'checked_in',?,?,?)""",
                         (tenant_id, checkin_no, bid, row.get('pnr'), row.get('flight_id'), row.get('passenger_name'),
                          data.get('seat_no'), bag_count, bags, data.get('checkin_method') or 'counter',
                          data.get('counter'), user_id, now_iso(), now_iso(), now_iso()))
            checkin_id = cur.lastrowid
            ci = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination FROM pss_checkins ci
                            LEFT JOIN pss_flights f ON f.id=ci.flight_id WHERE ci.id=? AND ci.tenant_id=?""",
                   (checkin_id, tenant_id), one=True) or {}
            prior_sale = q(conn, "SELECT sale_no FROM pss_ancillary_sales WHERE tenant_id=? AND booking_id=? AND description LIKE 'Baggage %' ORDER BY id DESC LIMIT 1", (tenant_id, bid), one=True)
            conn.execute("SAVEPOINT checkin_bags")
            try:
                bags = self._sync_baggage_items(conn, tenant_id, ci, bags, bag_count, user_id,
                                                sale_no=data.get('sale_no') or (prior_sale or {}).get('sale_no'),
                                                trace_id=row.get('trace_id'))
                conn.execute("RELEASE checkin_bags")
            except Exception as exc:
                conn.execute("ROLLBACK TO checkin_bags")
                conn.execute("RELEASE checkin_bags")
                self._log_exception(Exception(f"baggage sync failed for check-in {checkin_no}: {exc}"))
                bags = self._bag_tag_list(bags, bag_count)
            conn.execute("UPDATE pss_bookings SET booking_status='checked_in', seat_no=COALESCE(?,seat_no), updated_at=? WHERE id=? AND tenant_id=?",
                         (data.get('seat_no'), now_iso(), bid, tenant_id))
            self._activity(conn, tenant_id, 'pss_booking', bid, 'checked_in', checkin_no, user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_booking', bid, 'checked_in', user_id, checkin_no, row.get('trace_id'), self)
            log_audit(conn, tenant_id, user_id, 'checkin', 'pss_booking', bid, {'checkin_no': checkin_no, 'bag_tags': bags})
        return self._json({'ok': True, 'checkin_id': checkin_id, 'checkin_no': checkin_no, 'bag_tags': bags})

    def api_pss_checkin_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        return self.api_pss_booking_checkin(tenant_id, user_id, data.get('booking_id'), sess=sess, data=data)

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
                          data.get('currency') or row.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', data.get('method') or 'cash',
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
        if status in ('captured', 'paid'):
            _webhook_event(tenant_id, 'payment.received', {'payment_no': payment_no, 'amount': amount, 'pnr': row.get('pnr'),
                                                           'payment_status': pay_status}, payment_no)
        return self._json({'ok': True, 'payment_no': payment_no, 'payment_status': pay_status})

    def api_pss_checkins(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination, f.gate, f.terminal,
                              (SELECT COUNT(*) FROM pss_baggage_items bi WHERE bi.tenant_id=ci.tenant_id AND bi.checkin_id=ci.id) bag_items
                              FROM pss_checkins ci
                              LEFT JOIN pss_flights f ON f.id=ci.flight_id WHERE ci.tenant_id=? ORDER BY ci.id DESC LIMIT 500""", (tenant_id,))
        return self._json({'checkins': rows})

    def api_pss_checkin_baggage(self, tenant_id, cid):
        cid = self._oid(cid)
        with db() as conn:
            ci = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination, f.gate, f.terminal, bk.trace_id
                            FROM pss_checkins ci LEFT JOIN pss_flights f ON f.id=ci.flight_id
                            LEFT JOIN pss_bookings bk ON bk.id=ci.booking_id
                            WHERE ci.tenant_id=? AND ci.id=?""", (tenant_id, cid), one=True)
            if not ci:
                return self._json({'error': 'Check-in not found'}, 404)
            bags = q(conn, """SELECT bi.id,bi.tenant_id,bi.checkin_id,bi.booking_id,bi.bag_tag,bi.sequence_no,
                              bi.weight_kg,bi.status,bi.location,bi.sale_no,bi.carousel,bi.created_at,bi.updated_at,
                              ci.pnr checkin_pnr, f.flight_no, f.origin, f.destination
                              FROM pss_baggage_items bi JOIN pss_checkins ci ON ci.id=bi.checkin_id
                              LEFT JOIN pss_flights f ON f.id=ci.flight_id
                              WHERE bi.tenant_id=? AND bi.checkin_id=? ORDER BY bi.sequence_no, bi.id""", (tenant_id, cid))
            events = q(conn, """SELECT e.*, b.bag_tag FROM pss_baggage_events e
                                LEFT JOIN pss_baggage_items b ON b.id=e.baggage_id
                                WHERE e.tenant_id=? AND e.checkin_id=? ORDER BY e.id DESC LIMIT 300""", (tenant_id, cid))
        return self._json({'checkin': ci, 'baggage': bags, 'events': events})

    def api_pss_checkin_baggage_mutate(self, tenant_id, cid, user_id):
        cid = self._oid(cid)
        data = read_json(self)
        weight = float(data.get('weight_kg') or 0)
        if weight < 0:
            return self._json({'error': 'Invalid weight'}, 400)
        with db() as conn:
            ci = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination, bk.trace_id FROM pss_checkins ci
                            LEFT JOIN pss_flights f ON f.id=ci.flight_id
                            LEFT JOIN pss_bookings bk ON bk.id=ci.booking_id
                            WHERE ci.tenant_id=? AND ci.id=?""", (tenant_id, cid), one=True)
            if not ci:
                return self._json({'error': 'Check-in not found'}, 404)
            raw = data.get('bag_tags') or data.get('bag_tag') or ''
            pieces = int(data.get('pieces') or 1)
            tags = self._sync_baggage_items(conn, tenant_id, ci, raw, pieces, user_id, weight,
                                            data.get('sale_no'), ci.get('trace_id'))
            self._activity(conn, tenant_id, 'pss_checkin', cid, 'baggage_recorded', ', '.join(tags), user_id)
            self._trace(conn, tenant_id, 'counter', 'pss_checkin', ci.get('booking_id'), 'baggage_recorded', user_id,
                        f"{ci.get('pnr') or ci.get('checkin_no')} · {', '.join(tags)}", ci.get('trace_id'), self)
        return self._json({'ok': True, 'bag_tags': tags})

    def api_pss_baggage_scan(self, tenant_id, bid, user_id):
        bid = self._oid(bid)
        data = read_json(self)
        event_type = str(data.get('event_type') or 'bag_screened').strip()
        event_status = {'bag_scan': 'screened', 'bag_tagged': 'tagged', 'bag_screened': 'screened', 'bag_loaded': 'loaded',
                        'bag_unloaded': 'in_transit', 'bag_arrived': 'arrived', 'bag_claimed': 'claimed',
                        'bag_missing': 'missing'}
        if event_type not in event_status:
            return self._json({'error': 'Unsupported baggage event'}, 400)
        with db() as conn:
            bag = q(conn, """SELECT b.*, ci.trace_id booking_trace, ci.id checkin_id, ci.pnr FROM pss_baggage_items b
                             JOIN pss_checkins ci ON ci.id=b.checkin_id
                             WHERE b.tenant_id=? AND b.id=?""", (tenant_id, bid), one=True)
            if not bag:
                return self._json({'error': 'Bag not found'}, 404)
            location = data.get('location') or {'bag_scan': 'security', 'bag_screened': 'security', 'bag_loaded': 'aircraft',
                                                'bag_unloaded': 'arrival', 'bag_arrived': 'baggage hall',
                                                'bag_claimed': 'exit'}.get(event_type, bag.get('location') or 'counter')
            carousel = data.get('carousel') if data.get('carousel') is not None else bag.get('carousel')
            conn.execute("UPDATE pss_baggage_items SET status=?,location=?,carousel=?,updated_at=? WHERE id=? AND tenant_id=?",
                         (event_status[event_type], location, carousel, now_iso(), bid, tenant_id))
            self._baggage_event(conn, tenant_id, bag, bag['checkin_id'], event_type, location, user_id,
                                data.get('note'), bag.get('booking_trace'))
            if event_type == 'bag_arrived':
                conn.execute("UPDATE pss_checkins SET arrival_status='arrived',carousel=?,updated_at=? WHERE id=? AND tenant_id=?",
                             (carousel, now_iso(), bag['checkin_id'], tenant_id))
        return self._json({'ok': True, 'status': event_status[event_type], 'location': location})

    def api_pss_checkin_status(self, tenant_id, cid, user_id):
        cid = self._oid(cid)
        data = read_json(self)
        status = str(data.get('status') or '').strip().lower()
        allowed = ('boarded', 'gate_closed', 'no_show', 'arrived', 'checked_in')
        if status not in allowed:
            return self._json({'error': 'Unsupported check-in status'}, 400)
        ts = now_iso()
        vals = {'status': status, 'updated_at': ts}
        if status == 'boarded':
            vals.update({'boarding_status': 'boarded', 'boarded_at': ts})
        elif status == 'gate_closed':
            vals['gate_closed_at'] = ts
        elif status == 'arrived':
            vals.update({'arrival_status': 'arrived', 'carousel': data.get('carousel')})
        with db() as conn:
            ci = q(conn, """SELECT ci.*, bk.trace_id booking_trace FROM pss_checkins ci
                            LEFT JOIN pss_bookings bk ON bk.id=ci.booking_id
                            WHERE ci.tenant_id=? AND ci.id=?""", (tenant_id, cid), one=True)
            if not ci:
                return self._json({'error': 'Check-in not found'}, 404)
            conn.execute(f"UPDATE pss_checkins SET {','.join(k+'=?' for k in vals)} WHERE id=? AND tenant_id=?",
                         list(vals.values()) + [cid, tenant_id])
            if status == 'boarded':
                conn.execute("UPDATE pss_bookings SET booking_status='boarded',updated_at=? WHERE id=? AND tenant_id=?",
                             (ts, ci.get('booking_id'), tenant_id))
            self._activity(conn, tenant_id, 'pss_checkin', cid, 'checkin_' + status, data.get('note') or status, user_id)
            self._trace(conn, tenant_id, 'gate', 'pss_checkin', ci.get('booking_id'), 'checkin_' + status, user_id,
                        f"{ci.get('pnr') or ci.get('checkin_no')} · {status}", ci.get('booking_trace'), self)
        return self._json({'ok': True, 'status': status})

    def api_pss_baggage_tracking(self, tenant_id, qs):
        pnr = str((qs.get('pnr') or [''])[0] or '').strip()
        tag = str((qs.get('bag_tag') or [''])[0] or '').strip()
        bid = self._oid((qs.get('bag_id') or [''])[0])
        where = ['bi.tenant_id=?']
        args = [tenant_id]
        if pnr:
            where.append('(UPPER(bi.pnr)=UPPER(?) OR UPPER(ci.pnr)=UPPER(?))')
            args.extend([pnr, pnr])
        if tag:
            where.append('UPPER(bi.bag_tag)=UPPER(?)')
            args.append(tag)
        if bid:
            where.append('bi.id=?')
            args.append(bid)
        with db() as conn:
            bags = q(conn, f"""SELECT bi.id,bi.tenant_id,bi.checkin_id,bi.booking_id,bi.bag_tag,bi.sequence_no,
                               bi.weight_kg,bi.status,bi.location,bi.sale_no,bi.carousel bag_carousel,bi.created_at,bi.updated_at,
                               ci.checkin_no, ci.pnr, ci.passenger_name, ci.seat_no, ci.status checkin_status,
                               ci.boarding_status, ci.gate_closed_at, ci.boarded_at, ci.arrival_status, ci.carousel,
                               f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.gate, f.terminal
                               FROM pss_baggage_items bi JOIN pss_checkins ci ON ci.id=bi.checkin_id
                               LEFT JOIN pss_flights f ON f.id=ci.flight_id
                               WHERE {' AND '.join(where)} ORDER BY bi.id DESC LIMIT 200""", args)
            ids = [r['id'] for r in bags]
            events = []
            if ids:
                marks = ','.join('?' for _ in ids)
                events = q(conn, f"""SELECT e.*, b.bag_tag, b.status bag_status, b.location bag_location
                                     FROM pss_baggage_events e JOIN pss_baggage_items b ON b.id=e.baggage_id
                                     WHERE e.tenant_id=? AND e.baggage_id IN ({marks}) ORDER BY e.id DESC LIMIT 300""",
                           [tenant_id] + ids)
        return self._json({'bags': bags, 'events': events})

    def api_pss_payments(self, tenant_id, sess=None):
        with db() as conn:
            sql = """SELECT p.*, c.name channel_name, b.passenger_name, b.ticket_number, b.booking_status, b.payment_status booking_payment_status
                     FROM pss_payments p
                     LEFT JOIN pss_channels c ON c.id=p.channel_id
                     LEFT JOIN pss_bookings b ON b.id=p.booking_id AND b.tenant_id=p.tenant_id
                     WHERE p.tenant_id=?"""
            args = [tenant_id]
            aid = (sess or {}).get('agency_id')
            if aid:
                sql += " AND p.booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND agency_id=?)"
                args += [tenant_id, aid]
            sql += " ORDER BY p.id DESC LIMIT 500"
            rows = q(conn, sql, args)
        return self._json({'payments': rows})

    def api_pss_payment_create(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        bid = self._oid(data.get('booking_id'))
        pnr = (data.get('pnr') or '').strip().upper()
        amount = float(data.get('amount') or 0)
        if amount <= 0:
            return self._json({'error': 'amount required'}, 400)
        with db() as conn:
            if bid:
                row = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (bid, tenant_id), one=True)
            elif pnr:
                row = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND pnr=? ORDER BY id DESC LIMIT 1", (tenant_id, pnr), one=True)
            else:
                return self._json({'error': 'booking_id or pnr required'}, 400)
            if not row or ((sess or {}).get('agency_id') and row.get('agency_id') != sess.get('agency_id')):
                return self._json({'error': 'Booking not found'}, 404)
            bid = row['id']
            payment_no = self._psn('PAY')
            status = data.get('status') or 'captured'
            paid_at = data.get('paid_at') or now_iso()
            currency = (data.get('currency') or row.get('currency') or self._wallet_currency(conn, tenant_id) or 'USD').upper()
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,channel_id,amount,currency,method,reference,gateway_ref,status,paid_at,captured_at,notes,created_by,created_at,updated_at,booking_source)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, payment_no, bid, row.get('pnr'), row.get('channel_id') or data.get('channel_id'), amount,
                          currency, data.get('method') or 'cash',
                          data.get('reference'), data.get('gateway_ref'), status, paid_at,
                          now_iso() if status in ('captured','paid') else None, data.get('notes'), user_id, now_iso(), now_iso(), data.get('booking_source') or 'counter'))
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
                pass
            log_audit(conn, tenant_id, user_id, 'payment', 'pss_booking', bid, {'payment_no': payment_no, 'amount': amount})
        return self._json({'ok': True, 'payment_no': payment_no, 'payment_status': pay_status, 'booking_id': bid})

    def api_pss_payment_mutate(self, tenant_id, pid, method):
        pid = self._oid(pid)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_payments WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Payment not found'}, 404)
            if method == 'GET':
                return self._json({'payment': row})
            data = read_json(self)
            allowed = ('amount', 'method', 'reference', 'status', 'paid_at', 'captured_at', 'notes',
                       'erp_payment_id', 'customer_id', 'invoice_id')
            sets, args = [], []
            for key in allowed:
                if key in data:
                    sets.append(f"{key}=?")
                    args.append(self._oid(data[key]) if key.endswith('_id') else data[key])
            if not sets:
                return self._json({'payment': row})
            args += [now_iso(), pid, tenant_id]
            conn.execute(f"UPDATE pss_payments SET {','.join(sets)}, updated_at=? WHERE id=? AND tenant_id=?", args)
            updated = q(conn, "SELECT * FROM pss_payments WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
        return self._json({'ok': True, 'payment': updated})

    def _tenant_currency(self, conn, tenant_id):
        row = q(conn, "SELECT settings_json FROM tenants WHERE id=?", (tenant_id,), one=True) or {}
        try:
            settings = json.loads(row.get('settings_json') or '{}')
        except Exception:
            settings = {}
        cur = (settings.get('default_currency') or settings.get('currency') or '').strip().upper()
        if cur:
            return cur
        ap = q(conn, "SELECT default_currency FROM agency_profile WHERE tenant_id=?", (tenant_id,), one=True) or {}
        cur = (ap.get('default_currency') or '').strip().upper()
        if cur:
            return cur
        br = q(conn, "SELECT currency FROM tenant_branding WHERE tenant_id=?", (tenant_id,), one=True) if False else {}
        return 'USD'

    def _wallet_currency(self, conn, tenant_id):
        return self._tenant_currency(conn, tenant_id)

    def _ensure_agency_wallet(self, conn, tenant_id, agency_id, currency=None):
        agency_id = self._oid(agency_id)
        if not agency_id:
            return None
        cur = (currency or self._wallet_currency(conn, tenant_id) or 'USD').upper()
        ts = now_iso()
        conn.execute("""INSERT OR IGNORE INTO pss_agency_wallets(tenant_id,agency_id,currency,balance,status,created_at,updated_at)
                        VALUES (?,?,?,0,'active',?,?)""", (tenant_id, agency_id, cur, ts, ts))
        return q(conn, "SELECT * FROM pss_agency_wallets WHERE tenant_id=? AND agency_id=? AND currency=?",
                 (tenant_id, agency_id, cur), one=True)

    def _wallet_summary(self, conn, tenant_id, agency_id=None):
        where = "WHERE w.tenant_id=?"
        args = [tenant_id]
        if agency_id:
            where += " AND w.agency_id=?"; args.append(self._oid(agency_id))
        return q(conn, f"""SELECT w.*, a.code agency_code, a.name agency_name,
                                  (SELECT COUNT(*) FROM pss_stock_allocations al WHERE al.tenant_id=w.tenant_id AND al.agency_id=w.agency_id) allocations,
                                  (SELECT COALESCE(SUM(al.total_amount),0) FROM pss_stock_allocations al WHERE al.tenant_id=w.tenant_id AND al.agency_id=w.agency_id) allocated_amount
                           FROM pss_agency_wallets w
                           LEFT JOIN pss_agencies a ON a.id=w.agency_id AND a.tenant_id=w.tenant_id
                           {where} ORDER BY a.name, w.id""", args)

    def _apply_wallet_topup(self, conn, tx_ref, gateway_data=None):
        tx = q(conn, "SELECT * FROM pss_wallet_transactions WHERE gateway_ref=? OR tx_no=?", (tx_ref, tx_ref), one=True)
        if not tx or tx.get('status') in ('captured', 'paid', 'applied'):
            return False
        wallet = self._ensure_agency_wallet(conn, tx['tenant_id'], tx['agency_id'], tx.get('currency'))
        before = float((wallet or {}).get('balance') or 0)
        amount = float(tx.get('amount') or 0)
        after = round(before + amount, 2)
        ts = now_iso()
        conn.execute("UPDATE pss_agency_wallets SET balance=?, updated_at=? WHERE id=?", (after, ts, wallet['id']))
        conn.execute("""UPDATE pss_wallet_transactions SET status='captured', balance_before=?, balance_after=?, raw_json=?, updated_at=?
                        WHERE id=?""", (before, after, json.dumps(gateway_data or {}, default=str), ts, tx['id']))
        self._trace(conn, tx['tenant_id'], 'flutterwave', 'pss_agency_wallet', tx['agency_id'], 'wallet_topup_captured',
                    str(tx.get('created_by') or 'flutterwave'), tx_ref, None, self)
        return True

    def api_pss_stock(self, tenant_id, sess=None):
        with db() as conn:
            pools = q(conn, "SELECT * FROM pss_stock_pools WHERE tenant_id=? ORDER BY id DESC", (tenant_id,))
            for p in pools:
                p['remaining'] = int(p.get('end_serial') or 0) - int(p.get('next_serial') or 0) + 1
            agency_id = (sess or {}).get('agency_id')
            alloc_sql = """SELECT a.*, p.prefix, ag.name agency_name, ag.code agency_code
                               FROM pss_stock_allocations a
                               JOIN pss_stock_pools p ON p.id=a.pool_id
                               LEFT JOIN pss_agencies ag ON ag.id=a.agency_id AND ag.tenant_id=a.tenant_id
                               WHERE a.tenant_id=?"""
            alloc_args = [tenant_id]
            if agency_id:
                alloc_sql += " AND a.agency_id=?"; alloc_args.append(agency_id)
            alloc_sql += " ORDER BY a.id DESC LIMIT 300"
            allocs = q(conn, alloc_sql, tuple(alloc_args))
            for a in allocs:
                a['remaining'] = int(a.get('end_serial') or 0) - int(a.get('next_serial') or 0) + 1
            wallets = self._wallet_summary(conn, tenant_id, agency_id=agency_id)
            tx_where = "WHERE t.tenant_id=?"
            tx_args = [tenant_id]
            if agency_id:
                tx_where += " AND t.agency_id=?"; tx_args.append(agency_id)
            txs = q(conn, f"""SELECT t.*, a.name agency_name FROM pss_wallet_transactions t
                              LEFT JOIN pss_agencies a ON a.id=t.agency_id AND a.tenant_id=t.tenant_id
                              {tx_where} ORDER BY t.id DESC LIMIT 100""", tx_args)
        return self._json({'pools': pools, 'allocations': allocs, 'wallets': wallets, 'wallet_transactions': txs})

    def api_pss_allocations(self, tenant_id, sess=None):
        return self.api_pss_stock(tenant_id, sess=sess)

    def api_pss_wallets(self, tenant_id, sess=None):
        agency_id = (sess or {}).get('agency_id')
        with db() as conn:
            wallets = self._wallet_summary(conn, tenant_id, agency_id=agency_id)
            for w in wallets:
                w['recent_transactions'] = q(conn, """SELECT * FROM pss_wallet_transactions
                                                      WHERE tenant_id=? AND agency_id=? AND currency=?
                                                      ORDER BY id DESC LIMIT 10""",
                                             (tenant_id, w['agency_id'], w.get('currency') or 'USD'))
        return self._json({'wallets': wallets})

    def api_pss_wallet_transactions(self, tenant_id, qs, sess=None):
        agency_id = (sess or {}).get('agency_id') or self._oid((qs.get('agency_id') or [''])[0])
        with db() as conn:
            where = "WHERE t.tenant_id=?"
            args = [tenant_id]
            if agency_id:
                where += " AND t.agency_id=?"; args.append(agency_id)
            rows = q(conn, f"""SELECT t.*, a.name agency_name, a.code agency_code
                               FROM pss_wallet_transactions t
                               LEFT JOIN pss_agencies a ON a.id=t.agency_id AND a.tenant_id=t.tenant_id
                               {where} ORDER BY t.id DESC LIMIT 300""", args)
        return self._json({'transactions': rows})

    def api_pss_wallet_topup_init(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        agency_id = self._oid((sess or {}).get('agency_id') or data.get('agency_id'))
        amount = float(data.get('amount') or 0)
        if not agency_id or amount <= 0:
            return self._json({'error': 'agency_id and positive amount required'}, 400)
        with db() as conn:
            agency = q(conn, "SELECT * FROM pss_agencies WHERE id=? AND tenant_id=?", (agency_id, tenant_id), one=True)
            if not agency:
                return self._json({'error': 'Agency not found'}, 404)
            currency = (data.get('currency') or self._wallet_currency(conn, tenant_id) or 'USD').upper()
            wallet = self._ensure_agency_wallet(conn, tenant_id, agency_id, currency)
            gw = self._pss_flw_gateway(conn, tenant_id)
            secret = (gw or {}).get('secret_key') or ''
            tx_ref = 'WTX-' + os.urandom(6).hex().upper()
            ts = now_iso()
            notes = f"Agency wallet top-up {agency.get('name')}" + (' DEMO' if not secret else '')
            conn.execute("""INSERT INTO pss_wallet_transactions
                (tenant_id,agency_id,wallet_id,tx_no,tx_type,direction,amount,currency,balance_before,balance_after,reference,gateway_ref,status,method,notes,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,'credit',?,?,?,?,'wallet_topup',?,'pending','flutterwave',?,?,?,?)""",
                (tenant_id, agency_id, wallet['id'], tx_ref, 'wallet_topup', amount, currency, float(wallet.get('balance') or 0), float(wallet.get('balance') or 0),
                 tx_ref, notes, user_id, ts, ts))
            if not secret:
                return self._json({'ok': True, 'demo': True, 'tx_ref': tx_ref, 'amount': amount, 'currency': currency,
                                   'url': f'/api/pss/wallets/topup/verify?tx_ref={tx_ref}&status=successful'})
            cfg = gw.get('config') or {}
            redirect = data.get('redirect_url') or f"{cfg.get('site_url','')}/index.html#agency-wallet"
            payload = {'tx_ref': tx_ref, 'amount': amount, 'currency': currency,
                       'email': data.get('email') or agency.get('contact_email') or 'agency@fayaka.aero',
                       'phonenumber': data.get('phone') or agency.get('contact_phone') or '',
                       'name': agency.get('name') or 'Agency',
                       'payment_options': cfg.get('methods', 'mobilemoney,card,banktransfer,ussd'),
                       'redirect_url': redirect,
                       'meta': {'source': 'agency_wallet_topup', 'agency_id': agency_id, 'tenant_id': tenant_id, 'tx_ref': tx_ref},
                       'customizations': {'title': 'Fayaka Airline', 'description': f'Agency wallet top-up {agency.get("code") or agency_id}'}}
            status, resp = flutterwave_request('POST', '/payments', secret, payload)
            link = (resp or {}).get('data', {}).get('link') if status in (200, 201) else None
            if not link:
                conn.execute("UPDATE pss_wallet_transactions SET status='failed', raw_json=?, updated_at=? WHERE tenant_id=? AND tx_no=?",
                             (json.dumps(resp or {}, default=str), now_iso(), tenant_id, tx_ref))
                return self._json({'error': (resp or {}).get('message') or 'Flutterwave init failed'}, 502)
            conn.execute("UPDATE pss_wallet_transactions SET raw_json=?, updated_at=? WHERE tenant_id=? AND tx_no=?",
                         (json.dumps(resp or {}, default=str), now_iso(), tenant_id, tx_ref))
        return self._json({'ok': True, 'tx_ref': tx_ref, 'url': link, 'amount': amount, 'currency': currency})

    def api_pss_wallet_topup_verify(self, tenant_id, qs, sess=None):
        tx_ref = (qs.get('tx_ref') or [''])[0]
        status = (qs.get('status') or ['successful'])[0]
        if not tx_ref:
            return self._json({'error': 'tx_ref required'}, 400)
        with db() as conn:
            tx = q(conn, "SELECT * FROM pss_wallet_transactions WHERE tenant_id=? AND (tx_no=? OR gateway_ref=?)", (tenant_id, tx_ref, tx_ref), one=True)
            if not tx:
                return self._json({'error': 'Wallet transaction not found'}, 404)
            gw = self._pss_flw_gateway(conn, tenant_id)
            secret = (gw or {}).get('secret_key') or ''
            verified = False
            gateway_data = {'tx_ref': tx_ref, 'status': status}
            if secret:
                vstatus, vresp = flutterwave_verify(secret, tx_ref=tx_ref)
                vdata = (vresp or {}).get('data') or {}
                verified = vstatus == 200 and str(vdata.get('status') or '').lower() == 'successful'
                verified = verified and float(vdata.get('amount') or 0) >= float(tx.get('amount') or 0)
                verified = verified and str(vdata.get('currency') or '').upper() == str(tx.get('currency') or '').upper()
                gateway_data = vresp or gateway_data
            else:
                verified = 'DEMO' in str(tx.get('notes') or '') and status.lower() in ('successful', 'success')
            if verified:
                self._apply_wallet_topup(conn, tx_ref, gateway_data)
            wallet_balance = None
            if tx.get('wallet_id'):
                wrow = q(conn, "SELECT balance FROM pss_agency_wallets WHERE id=?", (tx.get('wallet_id'),), one=True)
                wallet_balance = float((wrow or {}).get('balance') or 0)
        return self._json({'ok': True, 'verified': bool(verified), 'tx_ref': tx_ref, 'wallet_balance': wallet_balance})

    def api_pss_stock_allocate(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        pid = self._oid(data.get('pool_id'))
        qty = int(data.get('quantity') or 0)
        if qty <= 0:
            return self._json({'error': 'quantity required'}, 400)
        agency_id = self._oid((sess or {}).get('agency_id') or data.get('agency_id'))
        unit_price = float(data.get('unit_price') or 0)
        if unit_price < 0:
            return self._json({'error': 'unit_price must be >= 0'}, 400)
        with db() as conn:
            pool = q(conn, "SELECT * FROM pss_stock_pools WHERE id=? AND tenant_id=?", (pid, tenant_id), one=True)
            if not pool:
                return self._json({'error': 'Stock pool not found'}, 404)
            agency = q(conn, "SELECT * FROM pss_agencies WHERE id=? AND tenant_id=?", (agency_id, tenant_id), one=True) if agency_id else None
            if agency_id and not agency:
                return self._json({'error': 'Agency not found'}, 404)
            start = int(pool.get('next_serial') or pool.get('start_serial'))
            end = start + qty - 1
            if end > int(pool.get('end_serial')):
                return self._json({'error': 'Not enough stock in pool'}, 400)
            currency = (data.get('currency') or self._wallet_currency(conn, tenant_id) or 'USD').upper()
            total_amount = round(qty * unit_price, 2)
            wallet_tx_id = None
            if agency and total_amount > 0:
                wallet = self._ensure_agency_wallet(conn, tenant_id, agency['id'], currency)
                balance = float((wallet or {}).get('balance') or 0)
                if balance < total_amount:
                    return self._json({'error': 'Insufficient wallet credit', 'balance': balance, 'required': total_amount, 'currency': currency}, 402)
                after = round(balance - total_amount, 2)
                tx_no = self._psn('WTX')
                ts = now_iso()
                cur = conn.execute("""INSERT INTO pss_wallet_transactions
                    (tenant_id,agency_id,wallet_id,tx_no,tx_type,direction,amount,currency,balance_before,balance_after,reference,gateway_ref,status,method,notes,created_by,created_at,updated_at)
                    VALUES (?,?,?,?,?,'debit',?,?,?,?,?,?,'captured','wallet',?,?,?)""",
                    (tenant_id, agency['id'], wallet['id'], tx_no, 'stock_allocation', total_amount, currency, balance, after,
                     f"ALLOC-{pid}", None, f"Stock allocation from pool {pool.get('pool_no')}", user_id, ts, ts))
                wallet_tx_id = int(cur.lastrowid)
                conn.execute("UPDATE pss_agency_wallets SET balance=?, updated_at=? WHERE id=?", (after, ts, wallet['id']))
            alloc_no = self._psn('AL')
            allocated_to = data.get('allocated_to') or (agency.get('name') if agency else None)
            cur = conn.execute("""INSERT INTO pss_stock_allocations
                (tenant_id,allocation_no,pool_id,channel_id,airport_code,quantity,start_serial,end_serial,next_serial,allocated_to,agency_id,unit_price,total_amount,currency,wallet_tx_id,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (tenant_id, alloc_no, pid, self._oid(data.get('channel_id')), data.get('airport_code'),
                 qty, start, end, start, allocated_to, agency['id'] if agency else None, unit_price, total_amount, currency, wallet_tx_id, now_iso(), now_iso()))
            allocation_id = int(cur.lastrowid)
            if wallet_tx_id:
                conn.execute("UPDATE pss_wallet_transactions SET allocation_id=?, reference=? WHERE id=?", (allocation_id, alloc_no, wallet_tx_id))
            conn.execute("UPDATE pss_stock_pools SET next_serial=?, updated_at=? WHERE id=? AND tenant_id=?", (end + 1, now_iso(), pid, tenant_id))
            self._trace(conn, tenant_id, 'counter', 'pss_stock_allocation', allocation_id, 'stock_allocated', user_id, alloc_no, None, self)
            log_audit(conn, tenant_id, user_id, 'allocate_stock', 'pss_stock_pool', pid, {'quantity': qty, 'allocation_no': alloc_no, 'agency_id': agency['id'] if agency else None, 'total_amount': total_amount})
        return self._json({'ok': True, 'allocation_id': allocation_id, 'allocation_no': alloc_no, 'start_serial': start, 'end_serial': end, 'total_amount': total_amount, 'currency': currency})

    def api_pss_stock_issue(self, tenant_id, user_id, sess=None):
        data = read_json(self)
        sess_agency = self._oid((sess or {}).get('agency_id'))
        req_agency = self._oid(data.get('agency_id'))
        if sess_agency and req_agency and req_agency != sess_agency:
            return self._json({'error': 'Forbidden'}, 403)
        agency_id = req_agency or sess_agency
        with db() as conn:
            ticket = self._pss_next_ticket(conn, tenant_id, self._oid(data.get('channel_id')), data.get('airport_code'),
                                           agency_id=agency_id, strict_agency=bool(agency_id))
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
        trip_key = b.get('trip_pnr') or b.get('pnr')
        trip_rows = q(conn, """SELECT tb.*, f.flight_no,f.origin,f.destination,f.flight_date,f.dep_time
                               FROM pss_bookings tb LEFT JOIN pss_flights f ON f.id=tb.flight_id
                               WHERE tb.tenant_id=? AND (tb.trip_pnr=? OR tb.itinerary_id=?)
                               ORDER BY tb.segment_index, tb.id""",
                      (tenant_id, trip_key, b.get('itinerary_id') or ''), one=False)
        trip_rows = trip_rows or [b]
        trip_ids = [r['id'] for r in trip_rows]
        trip_marks = ','.join(['?'] * len(trip_ids))
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
        if not cid and not email and name:
            c = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND lower(trim(COALESCE(name,'')))=lower(trim(?))",
                  (tenant_id, name), one=True)
            cid = c['id'] if c else None
        if not cid:
            cur = conn.execute(
                "INSERT INTO customers(tenant_id,type,name,company,email,phone,notes,created_at,agency_id,source,source_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, 'B2C', name, None, email, phone, f"Auto-created from PSS booking {b.get('pnr')}", ts, agency_id, source, b.get('pnr')))
            cid = cur.lastrowid
        else:
            conn.execute("UPDATE customers SET name=COALESCE(?,name), email=COALESCE(?,email), phone=COALESCE(?,phone), agency_id=COALESCE(?,agency_id) WHERE id=? AND tenant_id=?",
                         (name, email, phone, agency_id, cid, tenant_id))
        if b.get('portal_customer_id'):
            try:
                conn.execute("UPDATE customers SET portal_customer_id=? WHERE id=? AND tenant_id=? AND portal_customer_id IS NULL",
                             (b.get('portal_customer_id'), cid, tenant_id))
            except Exception:
                pass
        # order
        oid = b.get('order_id')
        if not oid:
            o = q(conn, f"SELECT id FROM orders WHERE tenant_id=? AND pss_booking_id IN ({trip_marks}) ORDER BY id LIMIT 1",
                  [tenant_id] + trip_ids, one=True)
            oid = o['id'] if o else None
        total = round(sum(float(r.get('total_amount') or 0) for r in trip_rows), 2)
        paid_total = (q(conn, f"SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE tenant_id=? AND booking_id IN ({trip_marks}) AND status IN ('captured','paid')", [tenant_id] + trip_ids, one=True) or {}).get('v', 0)
        balance_due = round(max(total - float(paid_total or 0), 0), 2)
        order_ticket_status = 'ticketed' if (b.get('ticket_status') or '') == 'issued' else (b.get('ticket_status') or 'unissued')
        pnr = trip_key or f"PSS-{bid}"
        src = f"pss_booking:{bid}"
        if not oid:
            conn.execute("""INSERT INTO orders(tenant_id,branch_id,source_type,source_id,connector_id,raw_document_id,pnr,order_number,office_id,account_code,customer_id,owner_user_id,product_type,status,ticket_status,tktl,booking_date,travel_start,travel_end,origin,destination,currency,total_sell,total_cost,margin,balance_due,notes,created_at,updated_at,agency_id,pss_booking_id,booking_source,trace_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, None, 'pss_booking', src, None, None, pnr, pnr, b.get('airport_code'), None, cid, b.get('created_by'),
                          'Flight', b.get('booking_status') or 'booked', order_ticket_status, None,
                          (b.get('created_at') or ts)[:10], b.get('flight_date'), b.get('flight_date'),
                          b.get('origin'), b.get('destination'), b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD',
                          total, 0, total, balance_due, f"PSS booking {pnr}", ts, ts, agency_id, bid, source, b.get('trace_id')))
            oid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
        else:
            conn.execute("""UPDATE orders SET customer_id=?, pnr=COALESCE(?,pnr), origin=?, destination=?, currency=?,
                            total_sell=?, margin=?, balance_due=?,
                            booking_date=COALESCE(?,booking_date), travel_start=?, travel_end=?, updated_at=?,
                            agency_id=COALESCE(?,agency_id), booking_source=?, trace_id=?, ticket_status=?
                            WHERE id=? AND tenant_id=?""",
                         (cid, pnr, b.get('origin'), b.get('destination'), b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', total, total, balance_due,
                          (b.get('created_at') or ts)[:10], b.get('flight_date'), b.get('flight_date'), ts,
                          agency_id, source, b.get('trace_id'), order_ticket_status, oid, tenant_id))
        # invoice
        iid = b.get('invoice_id')
        inv_no = ''
        if not iid:
            inv = q(conn, f"SELECT id FROM invoices WHERE tenant_id=? AND pss_booking_id IN ({trip_marks}) ORDER BY id LIMIT 1",
                    [tenant_id] + trip_ids, one=True)
            iid = inv['id'] if inv else None
        if iid:
            inv_no = (q(conn, "SELECT invoice_no FROM invoices WHERE id=? AND tenant_id=?", (iid, tenant_id), one=True) or {}).get('invoice_no') or ''
        fare = round(sum(float(r.get('fare_amount') or 0) for r in trip_rows), 2)
        taxes = round(sum(float(r.get('taxes') or 0) for r in trip_rows), 2)
        flight_total = round(sum(float(r.get('total_amount') or 0) for r in trip_rows), 2)
        cur = b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD'
        anc_rows = q(conn, f"""SELECT id,description,quantity,unit_price,amount FROM pss_ancillary_sales
                               WHERE tenant_id=? AND booking_id IN ({trip_marks}) AND LOWER(COALESCE(status,'sold'))<>'void'""",
                     [tenant_id] + trip_ids)
        anc_total = round(sum(float(r.get('amount') or 0) for r in anc_rows), 2)
        inv_total = round(flight_total + anc_total, 2)

        def anc_line_desc(r):
            return f"Ancillary: {(r.get('description') or 'Service').strip()}"

        if not iid:
            n = q(conn, "SELECT COUNT(*) c FROM invoices WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
            inv_no = f"INV-PSS-{ts[:4]}-{n:05d}"
            conn.execute("""INSERT INTO invoices(tenant_id,branch_id,order_id,customer_id,invoice_no,kind,status,issue_date,due_date,currency,subtotal,tax,fees,discount,total,amount_paid,balance,notes,created_at,agency_id,pss_booking_id,source)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?)""",
                         (tenant_id, None, oid, cid, inv_no, 'invoice', 'issued', ts[:10], ts[:10], cur, fare, taxes, anc_total, 0, inv_total, 0, inv_total,
                          f"Auto invoice for PSS booking {pnr}", ts, agency_id, bid, source))
            iid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            for tr in trip_rows:
                t_fare = float(tr.get('fare_amount') or 0)
                t_tax = float(tr.get('taxes') or 0)
                t_total = float(tr.get('total_amount') or (t_fare + t_tax))
                desc = f"Flight {tr.get('flight_no') or ''} {tr.get('origin') or ''}-{tr.get('destination') or ''} {tr.get('flight_date') or ''} ({tr.get('pnr') or pnr}) · {tr.get('passenger_name') or name}".strip()
                conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                             (tenant_id, iid, desc, 1, 0, t_fare, t_tax, t_total))
            for r in anc_rows:
                qty = max(1, int(r.get('quantity') or 1))
                amt = round(float(r.get('amount') or 0), 2)
                conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                             (tenant_id, iid, anc_line_desc(r), qty, 0, round(amt / qty, 2), 0, amt))
            conn.execute("INSERT INTO journals(tenant_id,ref_type,ref_id,entry_date,memo,debit_account,credit_account,amount,currency,created_at,agency_id,pss_booking_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tenant_id, 'invoice', iid, ts[:10], f"Invoice {inv_no}", 'Accounts Receivable', 'Sales', inv_total, cur, ts, agency_id, bid, source))
        else:
            changed = False
            for r in anc_rows:
                d = anc_line_desc(r)
                if q(conn, "SELECT 1 FROM invoice_lines WHERE tenant_id=? AND invoice_id=? AND description=?", (tenant_id, iid, d), one=True):
                    continue
                qty = max(1, int(r.get('quantity') or 1))
                amt = round(float(r.get('amount') or 0), 2)
                conn.execute("INSERT INTO invoice_lines(tenant_id,invoice_id,description,qty,unit_cost,unit_price,tax,line_total) VALUES (?,?,?,?,?,?,?,?)",
                             (tenant_id, iid, d, qty, 0, round(amt / qty, 2), 0, amt))
                changed = True
            lines_sum = round(float((q(conn, "SELECT COALESCE(SUM(line_total),0) s FROM invoice_lines WHERE tenant_id=? AND invoice_id=?",
                                       (tenant_id, iid), one=True) or {}).get('s') or 0), 2)
            if lines_sum > 0:
                inv_row = q(conn, "SELECT total,fees FROM invoices WHERE id=? AND tenant_id=?", (iid, tenant_id), one=True) or {}
                if changed or abs(lines_sum - float(inv_row.get('total') or 0)) > 0.009 or abs(anc_total - float(inv_row.get('fees') or 0)) > 0.009:
                    conn.execute("UPDATE invoices SET fees=?, total=? WHERE id=? AND tenant_id=?",
                                 (anc_total, lines_sum, iid, tenant_id))
        # update links
        conn.execute(f"UPDATE pss_bookings SET customer_id=?, order_id=?, invoice_id=? WHERE tenant_id=? AND id IN ({trip_marks})",
                     [cid, oid, iid, tenant_id] + trip_ids)
        try:
            from ingest import recompute_invoice as _ri
            _ri(conn, iid)
        except Exception:
            pass
        if iid and inv_no and not q(conn, "SELECT 1 FROM pss_trace WHERE tenant_id=? AND entity_type='invoice' AND entity_id=? AND action='invoice_created'", (tenant_id, iid), one=True):
            self._trace(conn, tenant_id, source or 'erp', 'invoice', iid, 'invoice_created', actor,
                        f"{inv_no} · PNR {pnr}", b.get('trace_id'), self)
        try:
            self._activity(conn, tenant_id, 'pss_booking', bid, 'erp_synced', f"customer={cid} order={oid} invoice={iid}", actor)
        except Exception:
            pass
        return {'customer_id': cid, 'order_id': oid, 'invoice_id': iid, 'invoice_no': inv_no}

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
                if not q(conn, "SELECT 1 FROM allocations WHERE tenant_id=? AND payment_id=? AND invoice_id=?", (tenant_id, epid, iid), one=True):
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

    def api_portal_branding(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        with db() as conn:
            branding = self._branding(conn, tenant['id'])
            tenant_row = q(conn, "SELECT code,name FROM tenants WHERE id=?", (tenant['id'],), one=True) or {}
        name = branding.get('company_name') or tenant_row.get('name') or 'Fayaka Airline'
        return self._json({
            'branding': branding,
            'airline': {
                'code': (tenant_row.get('code') or tenant.get('code') or 'FA').upper()[:3],
                'name': name,
                'tagline': branding.get('tagline') or branding.get('location') or 'Fly with confidence',
                'logo_data': branding.get('logo_data') or '',
                'logo_text': branding.get('logo_text') or name[:3].upper(),
                'primary_color': branding.get('primary_color') or '#0f2742',
                'accent_color': branding.get('accent_color') or '#f59e0b',
                'currency': branding.get('currency') or self._tenant_currency(conn, tenant['id'])
            }
        })

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

    def _portal_duration(self, dep, arr):
        try:
            dh, dm = [int(x) for x in str(dep or '').split(':')[:2]]
            ah, am = [int(x) for x in str(arr or '').split(':')[:2]]
            start, end = dh * 60 + dm, ah * 60 + am
            if end < start:
                end += 1440
            mins = max(0, end - start)
            return f"{mins // 60}h {mins % 60:02d}m", mins
        except Exception:
            return '', 0

    def _portal_fare_family(self, cabin, baggage=None):
        profiles = {
            'Y': {'code': 'ECONOMY SAVER', 'name': 'Economy Saver', 'refundable': False,
                  'change_policy': 'Changes permitted with fee', 'change_fee': 50,
                  'carry_on': '7 kg', 'seat_selection': 'Paid', 'priority_boarding': False,
                  'lounge': False, 'tagline': 'Lowest published fare for budget travel'},
            'P': {'code': 'PREMIUM FLEX', 'name': 'Premium Flex', 'refundable': True,
                  'refund_fee': 25, 'change_policy': 'One free change', 'change_fee': 0,
                  'carry_on': '10 kg', 'seat_selection': 'Included', 'priority_boarding': True,
                  'lounge': False, 'tagline': 'Extra comfort with flexible conditions'},
            'C': {'code': 'BUSINESS FLEX', 'name': 'Business Flex', 'refundable': True,
                  'refund_fee': 0, 'change_policy': 'Free changes', 'change_fee': 0,
                  'carry_on': '14 kg', 'seat_selection': 'Included', 'priority_boarding': True,
                  'lounge': True, 'tagline': 'Flexible business travel with priority service'},
            'F': {'code': 'FIRST SIGNATURE', 'name': 'First Signature', 'refundable': True,
                  'refund_fee': 0, 'change_policy': 'Free changes', 'change_fee': 0,
                  'carry_on': '18 kg', 'seat_selection': 'Included', 'priority_boarding': True,
                  'lounge': True, 'tagline': 'Premium cabin with the most flexible conditions'}
        }
        out = dict(profiles.get((cabin or 'Y').upper(), profiles['Y']))
        if isinstance(baggage, dict) and baggage.get('free_kg'):
            kg = str(baggage['free_kg']).strip()
            out['checked_baggage'] = kg if 'kg' in kg.lower() else f"{kg} kg"
        else:
            out['checked_baggage'] = {'Y': '23 kg', 'P': '30 kg', 'C': '40 kg', 'F': '50 kg'}.get((cabin or 'Y').upper(), '23 kg')
        out['conditions'] = [
            f"Refundable: {'Yes' if out.get('refundable') else 'No'}" + (f" ({out['refund_fee']} fee)" if out.get('refund_fee') else ''),
            out['change_policy'] + (f" ({out['change_fee']} fee)" if out.get('change_fee') else ''),
            f"Carry-on {out['carry_on']} · Checked {out['checked_baggage']}",
            f"Seat selection {str(out['seat_selection']).lower()}",
            'Fare conditions shown are indicative and must be confirmed at ticketing.'
        ]
        return out

    def _portal_equipment(self, aircraft, total_seats, available_seats):
        code = str(aircraft or '').upper()
        if '787' in code or '777' in code or 'A330' in code:
            layout, seats_per_row, pitch, wifi = '2-3-2', 7, '81 cm', True
        elif 'A320' in code or 'B737' in code or 'CRJ' in code or 'E190' in code:
            layout, seats_per_row, pitch, wifi = '3-3', 6, '76 cm', False
        else:
            layout, seats_per_row, pitch, wifi = '3-3', 6, '79 cm', True
        total = max(0, int(total_seats or 0))
        available = max(0, min(int(available_seats or 0), total))
        occupied = max(0, total - available)
        letters = ['A', 'B', 'C', 'D', 'E', 'F'] if layout == '3-3' else ['A', 'B', 'D', 'E', 'F', 'G', 'H']
        rows = max(6, min(40, (total or 180) // seats_per_row))
        seat_rows = []
        taken = 0
        for row_no in range(1, rows + 1):
            cells = []
            for letter in letters:
                status = 'occupied' if taken < occupied else 'available'
                taken += 1 if status == 'occupied' else 0
                cells.append({'label': f"{row_no}{letter}", 'status': status})
            seat_rows.append({'row': row_no, 'seats': cells})
        return {
            'aircraft': aircraft or 'Not published',
            'layout': layout,
            'seat_pitch': pitch,
            'wifi': 'Available' if wifi else 'Not available',
            'seat_rows': seat_rows,
            'legend': {'available': 'Available', 'occupied': 'Occupied'}
        }

    def _portal_enrich(self, conn, tenant_id, seg_results, cabin):
        flights = [f for seg in seg_results for f in seg]
        ids = [f.get('id') for f in flights if f.get('id') is not None]
        codes = sorted({str(f.get(k) or '').upper() for f in flights for k in ('origin', 'destination') if f.get(k)})
        id_marks = ','.join(['?'] * len(ids)) if ids else 'NULL'
        code_marks = ','.join(['?'] * len(codes)) if codes else 'NULL'

        bag_rows = q(conn, f"""SELECT flight_id,cabin_class,free_kg,free_pieces,excess_rate_per_kg,max_kg,notes
                               FROM pss_baggage_rules
                               WHERE tenant_id=? AND (flight_id IS NULL OR flight_id IN ({id_marks}))""", [tenant_id] + ids)
        anc_rows = q(conn, f"""SELECT id,code,name,category,description,price,currency,unit,capacity,flight_id,status
                               FROM pss_ancillaries
                               WHERE tenant_id=? AND status='active' AND (flight_id IS NULL OR flight_id IN ({id_marks}))
                               ORDER BY id""", [tenant_id] + ids)
        airport_rows = q(conn, f"""SELECT code,icao,name,city,country,timezone,terminal,region,is_hub
                                   FROM pss_airports WHERE tenant_id=? AND code IN ({code_marks})""", [tenant_id] + codes) if codes else []
        weather_rows = q(conn, f"""SELECT airport_code,condition,temperature_c,wind_kph,visibility_km,advisory,observed_at
                                   FROM pss_weather WHERE tenant_id=? AND status='current'
                                   AND airport_code IN ({code_marks}) ORDER BY airport_code,id DESC""", [tenant_id] + codes) if codes else []
        doc_rows = q(conn, f"""SELECT airport_code,doc_type,title,description,requirement,applies_to
                               FROM pss_airport_docs WHERE tenant_id=? AND status='active'
                               AND airport_code IN ({code_marks}) ORDER BY airport_code,id""", [tenant_id] + codes) if codes else []
        ndc_rows = q(conn, "SELECT status FROM connectors WHERE tenant_id=? AND LOWER(COALESCE(type,''))='ndc'", (tenant_id,))
        plan = plan_for_tenant(conn, tenant_id) or {}
        features = plan.get('features_json') or {}
        if isinstance(features, str):
            try: features = json.loads(features)
            except Exception: features = {}
        if not isinstance(features, dict):
            features = {}
        ndc_active = any(str(r.get('status') or '').lower() in ('active', 'connected', 'enabled') for r in ndc_rows)
        capabilities = {
            'ndc': bool(features.get('ndc') or ndc_active),
            'online_booking': True,
            'e_ticket': True,
            'seat_map': True,
            'ancillaries': True
        }

        def grouped(rows, key):
            out = {}
            for row in rows:
                out.setdefault(row.get(key), []).append(row)
            return out

        bmap = grouped(bag_rows, 'flight_id')
        wmap = {}
        for row in weather_rows:
            wmap.setdefault(row.get('airport_code'), row)
        dmap = grouped(doc_rows, 'airport_code')
        pmap = {str(row.get('code') or '').upper(): row for row in airport_rows}
        catalog, seen = [], set()
        for row in anc_rows:
            aid = row.get('id')
            if aid in seen:
                continue
            seen.add(aid)
            catalog.append(row)

        def pick_bag(pool):
            if not pool:
                return None
            return (next((r for r in pool if str(r.get('cabin_class') or '').upper() == str(cabin or 'Y').upper()), None)
                    or next((r for r in pool if str(r.get('cabin_class') or 'Y').upper() == 'Y'), None)
                    or pool[0])

        for f in flights:
            rule = pick_bag(bmap.get(f.get('id'))) or pick_bag(bmap.get(None))
            family = self._portal_fare_family(cabin, rule)
            duration, minutes = self._portal_duration(f.get('dep_time'), f.get('arr_time'))
            total = int(f.get('total_seats') or 0)
            avail = int(f.get('available_seats') or 0)
            f['seats_total'] = total
            f['seats_available'] = avail
            f['duration'] = duration
            f['duration_minutes'] = minutes
            f['stops'] = 'Nonstop'
            f['baggage'] = rule or {'free_kg': family.get('checked_baggage'), 'free_pieces': 1}
            f['fare_family'] = family
            f['refundable'] = family.get('refundable')
            f['terms'] = family.get('conditions')
            f['ancillaries'] = [a for a in catalog if a.get('flight_id') in (None, f.get('id'))]
            f['equipment'] = self._portal_equipment(f.get('aircraft'), total, avail)
            f['airports'] = {k: pmap.get(str(f.get(k) or '').upper()) for k in ('origin', 'destination')}
            f['weather'] = {k: wmap.get(str(f.get(k) or '').upper()) for k in ('origin', 'destination')}
            f['documents'] = {k: dmap.get(str(f.get(k) or '').upper(), []) for k in ('origin', 'destination')}
            f['ndc'] = {'enabled': capabilities['ndc'], 'offer': f"NDC-{f.get('flight_no')}" if capabilities['ndc'] else ''}
        return catalog, capabilities

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
                                available_seats,total_seats,base_fare,currency,gate,terminal,status
                         FROM pss_flights WHERE tenant_id=? AND LOWER(COALESCE(status,'scheduled'))='scheduled'"""
                args = [tenant['id']]
                if o: sql += " AND UPPER(origin)=?"; args.append(o)
                if d: sql += " AND UPPER(destination)=?"; args.append(d)
                if dt: sql += " AND flight_date=?"; args.append(dt)
                seats_needed = max(1, adults + children)
                sql += " AND COALESCE(available_seats,0)>=? ORDER BY flight_date,dep_time LIMIT 100"
                args.append(seats_needed)
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
            catalog, capabilities = self._portal_enrich(conn, tenant['id'], seg_results, cabin)
        return self._json({'airline': tenant, 'trip_type': trip, 'cabin': cabin,
                           'passengers': {'adults': adults, 'children': children, 'infants': infants,
                                          'total': adults + children + infants},
                           'segments': seg_results, 'outbound': seg_results[0] if seg_results else [],
                           'return': seg_results[1] if len(seg_results) > 1 else [],
                           'ancillaries': catalog, 'capabilities': capabilities})


    def _agency_offer(self, conn, token):
        return q(conn, """SELECT o.*, a.name agency_name, a.code agency_code, a.commission_rate,
                                 f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.available_seats
                          FROM pss_eticket_directs o JOIN pss_agencies a ON a.id=o.agency_id
                          LEFT JOIN pss_flights f ON f.id=o.flight_id
                          WHERE o.token=? AND o.status='active' AND (o.valid_until IS NULL OR o.valid_until>?)""",
                       (token, now_iso()), one=True)

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
                          data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', now_iso(), now_iso()))
            rid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant['id'], 'portal', 'pss_charter_request', rid, 'charter_requested',
                        data.get('client_name'), f"{request_no} · {data.get('origin')}-{data.get('destination')}", trace_id, self)
            email = (data.get('email') or '').strip() or None
            client = (data.get('client_name') or '').strip()
            cid = None
            if email:
                hit = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND lower(email)=lower(?)", (tenant['id'], email), one=True)
                cid = hit['id'] if hit else None
            if not cid and client:
                hit = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND lower(trim(COALESCE(name,'')))=lower(trim(?))",
                        (tenant['id'], client), one=True)
                cid = hit['id'] if hit else None
            if not cid:
                cur = conn.execute("""INSERT INTO customers(tenant_id,type,name,company,email,phone,notes,created_at,source,source_ref)
                                      VALUES (?,?,?,?,?,?,?,?, 'portal', ?)""",
                                   (tenant['id'], 'B2B', client, client, email, data.get('phone'),
                                    f"Charter request {request_no}", now_iso(), request_no))
                cid = cur.lastrowid
            else:
                conn.execute("""UPDATE customers SET email=COALESCE(email,?), phone=COALESCE(phone,?),
                                notes=COALESCE(NULLIF(notes,''), ?) WHERE id=? AND tenant_id=?""",
                             (email, data.get('phone'), f"Charter request {request_no}", cid, tenant['id']))
            try:
                self._activity(conn, tenant['id'], 'customer', cid, 'charter_request',
                               f"{request_no} · {client}", 'portal')
            except Exception:
                pass
        return self._json({'ok': True, 'request_no': request_no, 'trace_id': trace_id})

    def api_portal_book(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        booking_currency = (data.get('currency') or '').strip().upper() or 'USD'
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
        erp = {}
        sync_errors = 0
        with db() as conn:
            web_channel_row = q(conn, "SELECT id FROM pss_channels WHERE tenant_id=? AND code='WEB'", (tenant['id'],), one=True)
            web_channel = web_channel_row['id'] if web_channel_row else None
            sess_email = sess_phone = None
            if portal_customer_id:
                pc = q(conn, "SELECT email,phone FROM portal_customers WHERE id=? AND tenant_id=?",
                       (portal_customer_id, tenant['id']), one=True)
                if pc:
                    sess_email = (pc.get('email') or '').strip() or None
                    sess_phone = (pc.get('phone') or '').strip() or None
            conn.execute("SAVEPOINT portal_book")
            try:
                flights = []
                for fid in flight_ids:
                    f = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=? AND LOWER(COALESCE(status,'scheduled'))='scheduled'", (fid, tenant['id']), one=True)
                    if not f:
                        raise ValueError('Flight not found')
                    needed = len(passengers)
                    reserved = conn.execute("UPDATE pss_flights SET available_seats=available_seats-?, updated_at=? WHERE id=? AND tenant_id=? AND available_seats>=?",
                                            (needed, now_iso(), fid, tenant['id'], needed))
                    if reserved.rowcount < 1:
                        raise ValueError('A selected flight does not have enough seats - no booking was made')
                    flights.append(f)
                booking_ids, rows_meta, total = [], [], 0.0
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
                                contact.get('email') or pax.get('email') or sess_email,
                                contact.get('phone') or pax.get('phone') or sess_phone, web_channel,
                                f.get('origin'), cabin, fare, taxes, amount, booking_currency,
                                'unissued', 'pending', 'confirmed', 'portal', trace_id, itinerary_id, trip, idx,
                                now_iso(), now_iso(), portal_customer_id]
                        conn.execute("INSERT INTO pss_bookings(" + ','.join(cols) + ") VALUES (" + ','.join(['?'] * len(cols)) + ")", vals)
                        bid = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
                        booking_ids.append(bid)
                        rows_meta.append((bid, f['id']))
                        self._seed_tax_lines(conn, tenant['id'], bid, {'taxes': taxes, 'currency': booking_currency})
                seat_by_flight = {}
                for s in (data.get('seat_selections') or []):
                    fid = self._oid(s.get('flight_id'))
                    seat = str(s.get('seat') or '').strip().upper()
                    if fid and seat:
                        seat_by_flight.setdefault(fid, []).append(seat)
                for fid, seats in seat_by_flight.items():
                    for bid, row_fid in rows_meta:
                        if row_fid != fid or not seats:
                            continue
                        conn.execute("UPDATE pss_bookings SET seat_no=? WHERE id=? AND tenant_id=? AND seat_no IS NULL",
                                     (seats.pop(0), bid, tenant['id']))
                selected_flight_ids = set(flight_ids)
                for item in (data.get('ancillaries') or []):
                    aid = self._oid(item.get('ancillary_id'))
                    anc = q(conn, "SELECT * FROM pss_ancillaries WHERE id=? AND tenant_id=?", (aid, tenant['id']), one=True)
                    if not anc:
                        continue
                    if str(anc.get('status') or 'active').lower() not in ('active', 'sold'):
                        continue
                    anc_flight = self._oid(anc.get('flight_id'))
                    if anc_flight and anc_flight not in selected_flight_ids:
                        continue
                    qty = max(1, int(item.get('quantity') or 1))
                    amount = float(anc.get('price') or 0) * qty
                    total += amount
                    conn.execute("""INSERT INTO pss_ancillary_sales(tenant_id,sale_no,booking_id,ancillary_id,description,quantity,unit_price,amount,currency,status,payment_status,trace_id,created_by,created_at,updated_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,'sold','pending',?,NULL,?,?)""",
                                 (tenant['id'], self._psn('ANC'), booking_ids[0], aid, anc.get('name'), qty,
                                  anc.get('price') or 0, amount, anc.get('currency') or 'USD', trace_id, now_iso(), now_iso()))
                self._trace(conn, tenant['id'], 'portal', 'pss_booking', booking_ids[0], 'booking_created',
                            contact.get('email') or passengers[0].get('full_name'),
                            f"PNR {pnr} · {len(passengers)} pax · {len(flights)} segment(s)", trace_id, self)
                if booking_ids:
                    try:
                        erp = self._erp_sync_pss_booking(conn, tenant['id'], booking_ids[0], actor='portal', source='portal') or {}
                    except Exception as exc:
                        sync_errors += 1
                        self._log_exception(exc)
                conn.execute("RELEASE portal_book")
            except Exception as exc:
                self._log_exception(exc)
                conn.execute("ROLLBACK TO portal_book")
                return self._json({'error': 'Booking failed - please try again'}, 400)
        if not erp.get('invoice_no') and booking_ids:
            with db() as conn:
                inv = q(conn, "SELECT id,invoice_no FROM invoices WHERE tenant_id=? AND pss_booking_id=? ORDER BY id DESC LIMIT 1",
                        (tenant['id'], booking_ids[0]), one=True) or {}
            if inv:
                erp = {**erp, 'invoice_id': inv.get('id'), 'invoice_no': inv.get('invoice_no')}
        return self._json({'ok': True, 'pnr': pnr, 'itinerary_id': itinerary_id, 'trace_id': trace_id,
                           'booking_ids': booking_ids, 'total': round(total, 2),
                           'segments': len(flights), 'passengers': len(passengers),
                           'invoice_id': erp.get('invoice_id'), 'invoice_no': erp.get('invoice_no'),
                           'customer_id': erp.get('customer_id'), 'order_id': erp.get('order_id'),
                           'sync_error': bool(sync_errors)})

    def api_portal_pay(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        pnr = (data.get('pnr') or '').strip().upper()
        last = (data.get('last_name') or '').strip().lower()
        amount = float(data.get('amount') or 0)
        if amount <= 0 or amount != amount or amount > 10000000:
            return self._json({'error': 'Enter a valid payment amount'}, 400)
        psess = get_portal_session(self)
        with db() as conn:
            rows = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (tenant['id'], pnr, pnr))
            if psess:
                rows = [r for r in rows if r.get('portal_customer_id') == psess.get('customer_id')]
                if not rows:
                    return self._json({'error': 'PNR or passenger name does not match'}, 404)
            elif not any(self._last_name_matches(r.get('passenger_name'), last) for r in rows):
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
            anc_due = 0.0
            if rows:
                marks = ','.join(['?'] * len(rows))
                anc_due = float((q(conn, f"""SELECT COALESCE(SUM(amount),0) s FROM pss_ancillary_sales
                                             WHERE tenant_id=? AND booking_id IN ({marks})
                                               AND LOWER(COALESCE(status,'sold'))<>'void'""",
                                   [tenant['id']] + [r['id'] for r in rows], one=True) or {}).get('s') or 0)
            total_due = round(sum(float(r.get('total_amount') or 0) for r in rows) + anc_due, 2)
            ps = 'paid' if paid >= total_due - 0.01 else ('partial' if paid > 0 else 'pending')
            conn.execute("UPDATE pss_bookings SET payment_status=?, updated_at=? WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (ps, now_iso(), tenant['id'], pnr, pnr))
            if ps == 'paid' and rows:
                marks = ','.join(['?'] * len(rows))
                conn.execute(f"UPDATE pss_ancillary_sales SET payment_status='paid', updated_at=? WHERE tenant_id=? AND booking_id IN ({marks})",
                             [now_iso(), tenant['id']] + [r['id'] for r in rows])
            self._trace(conn, tenant['id'], 'portal', 'pss_booking', first['id'], 'payment_recorded',
                        first.get('passenger_name'), f"{payment_no} - {amount}", trace_id, self)
            for _b in rows:
                try:
                    self._erp_sync_pss_booking(conn, tenant['id'], _b['id'], actor='portal', source='portal')
                except Exception as exc:
                    self._log_exception(exc)
            try:
                self._erp_sync_pss_payment(conn, tenant['id'], _ppid, actor='portal', source='portal')
            except Exception as exc:
                self._log_exception(exc)
            ticketed = []
            if status in ('captured', 'paid') and ps == 'paid':
                ticketed = self._auto_ticket_paid_bookings(conn, tenant['id'], rows, 'portal', trace_id,
                                                           f"Auto-ticket after {payment_no}")
        return self._json({'ok': True, 'payment_no': payment_no, 'payment_status': ps, 'trace_id': trace_id,
                           'ticket_numbers': ticketed})

    def api_portal_booking(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        pnr = (qs.get('pnr') or [''])[0].strip().upper()
        last = (qs.get('last_name') or [''])[0].strip().lower()
        psess = get_portal_session(self)
        with db() as conn:
            rows = q(conn, """SELECT b.*, f.flight_no, f.origin, f.destination, f.flight_date, f.dep_time, f.arr_time, f.gate
                              FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                              WHERE b.tenant_id=? AND (b.pnr=? OR b.trip_pnr=?) ORDER BY b.segment_index, b.id""", (tenant['id'], pnr, pnr))
            if psess:
                rows = [r for r in rows if r.get('portal_customer_id') == psess.get('customer_id')]
                if not rows:
                    return self._json({'error': 'PNR or passenger name does not match'}, 404)
            elif not any(self._last_name_matches(r.get('passenger_name'), last) for r in rows):
                return self._json({'error': 'PNR or passenger name does not match'}, 404)
            pays = q(conn, "SELECT id,erp_payment_id,payment_no,amount,status,method,currency,paid_at FROM pss_payments WHERE tenant_id=? AND pnr=? ORDER BY id DESC", (tenant['id'], pnr))
            cis = q(conn, "SELECT ci.checkin_no,ci.pnr,ci.seat_no,ci.status,ci.checked_in_at FROM pss_checkins ci WHERE ci.tenant_id=? AND ci.pnr=? ORDER BY ci.id DESC", (tenant['id'], pnr))
            anc = q(conn, "SELECT sale_no,description,quantity,amount,currency FROM pss_ancillary_sales WHERE tenant_id=? AND booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)) ORDER BY id DESC", (tenant['id'], tenant['id'], pnr, pnr))
            invoice_rows = q(conn, "SELECT id,invoice_no,status,total,amount_paid,balance,currency,issue_date FROM invoices WHERE tenant_id=? AND pss_booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)) ORDER BY id", (tenant['id'], tenant['id'], pnr, pnr))
            invoices, seen_invoices = [], set()
            for inv in invoice_rows:
                key = inv.get('invoice_no') or inv.get('id')
                if key in seen_invoices:
                    continue
                seen_invoices.add(key)
                invoices.append(inv)
            traces = q(conn, "SELECT action,source,actor,detail,created_at FROM pss_trace WHERE tenant_id=? AND trace_id=? ORDER BY id", (tenant['id'], rows[0].get('trace_id') or ''))
        return self._json({'booking': rows[0], 'bookings': rows, 'payments': pays, 'checkins': cis,
                           'ancillaries': anc, 'invoices': invoices, 'trace': traces, 'airline': tenant,
                           'trip_pnr': rows[0].get('trip_pnr') or pnr, 'itinerary_id': rows[0].get('itinerary_id')})


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
            ticket = self._pss_next_ticket(conn, offer['tenant_id'], channel['id'] if channel else None, offer.get('origin'),
                                           agency_id=agency_id, strict_agency=bool(agency_id))
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
            ticket = self._pss_next_ticket(conn, offer['tenant_id'], channel['id'] if channel else None, offer.get('origin'),
                                           agency_id=agency_id, strict_agency=bool(agency_id))
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
                          data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', int(data.get('quantity') or 1), data.get('valid_until'),
                          data.get('status') or 'active', token, data.get('notes'), user_id, now_iso(), now_iso()))
            did = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
            self._trace(conn, tenant_id, 'counter', 'pss_eticket_direct', did, 'offer_created', user_id, offer_no, None, self)
        return self._json({'ok': True, 'offer_id': did, 'token': token, 'url': f'/agency/{token}'})

    def api_pss_charters(self, tenant_id, method, rid=None, user_id=None):
        with db() as conn:
            if method == 'GET' and not rid:
                rows = q(conn, """SELECT r.*, (SELECT COUNT(*) FROM pss_charter_flights f WHERE f.request_id=r.id AND f.tenant_id=r.tenant_id) flights
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
                          data.get('aircraft_type'), data.get('requirements'), data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', data.get('assigned_to'),
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
                              LEFT JOIN pss_charter_requests r ON r.id=f.request_id AND r.tenant_id=f.tenant_id
                              WHERE f.tenant_id=? ORDER BY f.id DESC""", (tenant_id,))
        return self._json({'charter_flights': rows})

    def api_pss_charter_flight_mutate(self, tenant_id, fid, method, user_id=None):
        allowed = ('charter_no', 'request_id', 'flight_no', 'origin', 'destination', 'flight_date',
                   'dep_time', 'arr_time', 'aircraft', 'seats', 'charterer', 'contract_no',
                   'total_amount', 'amount_paid', 'currency', 'pss_flight_id', 'status', 'notes')
        with db() as conn:
            if method == 'GET':
                row = q(conn, "SELECT * FROM pss_charter_flights WHERE id=? AND tenant_id=?", (self._oid(fid), tenant_id), one=True)
                return self._json({'charter_flight': row}) if row else self._json({'error': 'Charter flight not found'}, 404)
            if method == 'DELETE':
                fid = self._oid(fid)
                conn.execute("DELETE FROM pss_charter_passengers WHERE tenant_id=? AND charter_id=?", (tenant_id, fid))
                conn.execute("DELETE FROM pss_charter_flights WHERE id=? AND tenant_id=?", (fid, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            vals = {}
            for key in allowed:
                if key in data:
                    vals[key] = self._oid(data[key]) if key.endswith('_id') else data[key]
            if vals.get('request_id') and not q(conn, "SELECT 1 FROM pss_charter_requests WHERE id=? AND tenant_id=?", (vals['request_id'], tenant_id), one=True):
                return self._json({'error': 'Charter request not found'}, 404)
            if vals.get('pss_flight_id') and not q(conn, "SELECT 1 FROM pss_flights WHERE id=? AND tenant_id=?", (vals['pss_flight_id'], tenant_id), one=True):
                return self._json({'error': 'Flight not found'}, 404)
            ts = now_iso()
            if method == 'POST':
                if not vals.get('charter_no'):
                    n = q(conn, "SELECT COUNT(*) c FROM pss_charter_flights WHERE tenant_id=?", (tenant_id,), one=True)['c'] + 1
                    vals['charter_no'] = f"CHF-{ts[:4]}-{n:05d}"
                vals.setdefault('status', 'scheduled')
                vals.setdefault('currency', 'USD')
                cols = list(vals.keys()) + ['tenant_id', 'created_by', 'created_at', 'updated_at']
                params = list(vals.values()) + [tenant_id, user_id, ts, ts]
                conn.execute(f"INSERT INTO pss_charter_flights({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", params)
                new_id = q(conn, "SELECT last_insert_rowid() v", (), one=True)['v']
                return self._json({'ok': True, 'id': new_id})
            fid = self._oid(fid)
            if not q(conn, "SELECT 1 FROM pss_charter_flights WHERE id=? AND tenant_id=?", (fid, tenant_id), one=True):
                return self._json({'error': 'Charter flight not found'}, 404)
            if not vals:
                return self._json({'ok': True})
            vals['updated_at'] = ts
            conn.execute(f"UPDATE pss_charter_flights SET {','.join(c+'=?' for c in vals)} WHERE id=? AND tenant_id=?",
                         list(vals.values()) + [fid, tenant_id])
            return self._json({'ok': True})

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
            cur = conn.execute("""INSERT INTO pss_ancillary_sales(tenant_id,sale_no,booking_id,ancillary_id,description,quantity,unit_price,amount,currency,status,payment_status,trace_id,created_by,created_at,updated_at)
                            VALUES (?,?,?,NULL,?,1,?,?,?,'sold',?,?,?,?,?)""",
                         (tenant_id, sale_no, bid, desc, rate, charge, b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD',
                          data.get('payment_status') or 'pending', trace_id, user_id, now_iso(), now_iso()))
            sid = cur.lastrowid
            self._trace(conn, tenant_id, 'counter', 'pss_ancillary_sale', sid, 'baggage_allocated', user_id,
                        f"{desc} · charge {charge}", trace_id, self)
            ci = q(conn, """SELECT ci.*, f.flight_no, f.origin, f.destination, bk.trace_id booking_trace
                            FROM pss_checkins ci LEFT JOIN pss_flights f ON f.id=ci.flight_id
                            LEFT JOIN pss_bookings bk ON bk.id=ci.booking_id
                            WHERE ci.tenant_id=? AND ci.booking_id=? ORDER BY ci.id DESC LIMIT 1""", (tenant_id, bid), one=True)
            tags = self._sync_baggage_items(conn, tenant_id, ci, data.get('bag_tags') or data.get('bag_tag') or '',
                                            pieces, user_id, weight, sale_no, ci.get('booking_trace') or trace_id) if ci else []
        return self._json({'ok': True, 'sale_id': sid, 'sale_no': sale_no, 'free_kg': free_kg,
                           'excess_kg': excess, 'rate_per_kg': rate, 'charge': charge, 'bag_tags': tags})

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
                ticket = self._pss_next_ticket(conn, tenant_id, b.get('channel_id'), b.get('airport_code'),
                                               agency_id=b.get('agency_id'), strict_agency=bool(b.get('agency_id')))
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

    def api_portal_unused_tickets(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        with db() as conn:
            self._sync_unused_tickets(conn, tenant['id'])
            rows = q(conn, """SELECT ticket_no, pnr, traveler_name, airline, issue_date, travel_date,
                                     expiry_date, ticket_status, refund_status, value, currency
                              FROM unused_tickets WHERE tenant_id=?
                              ORDER BY CASE ticket_status WHEN 'unused' THEN 0 WHEN 'expired' THEN 1 ELSE 2 END,
                                       expiry_date ASC, id DESC LIMIT 100""", (tenant['id'],))
        return self._json({'unused_tickets': rows})

    def api_portal_ticket_status(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        with db() as conn:
            rows = q(conn, """SELECT b.ticket_number ticket_no, b.passenger_name, b.pnr,
                                     f.flight_no, b.ticket_status, b.issue_date, b.updated_at
                              FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                              WHERE b.tenant_id=? AND b.ticket_number IS NOT NULL AND b.ticket_number<>''
                              ORDER BY b.id DESC LIMIT 100""", (tenant['id'],))
        return self._json({'ticket_status': rows})

    def api_portal_suppliers(self, qs):
        tenant = self._portal_tenant((qs.get('tenant') or [''])[0])
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        with db() as conn:
            rows = q(conn, """SELECT name supplier_name, COALESCE(supplier_type, type, 'General') category,
                                     contact_person, email, phone, balance, currency, status
                              FROM contacts
                              WHERE tenant_id=? AND LOWER(COALESCE(contact_type,'supplier'))='supplier'
                                AND COALESCE(is_active,1)=1 ORDER BY name LIMIT 100""", (tenant['id'],))
        return self._json({'suppliers': rows})

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
        key = _login_ident(self, 'portal', f"{tenant['code']}|{email}")
        locked_until = _login_locked_until(key)
        if locked_until and locked_until > time.time():
            retry = max(1, int(locked_until - time.time()))
            return self._json({'error': 'Too many failed attempts. Try again later.', 'retry_after': retry},
                              401, headers={'Retry-After': str(retry)})
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
            try:
                conn.execute("""UPDATE pss_bookings SET portal_customer_id=?, updated_at=?
                                WHERE tenant_id=? AND portal_customer_id IS NULL AND lower(COALESCE(contact_email,''))=?""",
                             (c['id'], now_iso(), tenant['id'], email))
            except Exception:
                pass
        _note_login_success(key)
        flags = '; Secure' if _use_secure_cookie(self) else ''
        return self._json({'ok': True, 'token': token, 'customer': {'id': c['id'], 'full_name': c['full_name'], 'email': c['email'], 'phone': c.get('phone')}},
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
            rows = q(conn, """SELECT b.id,b.pnr,b.trip_pnr,b.itinerary_id,f.flight_no,f.origin,f.destination,f.flight_date,f.dep_time,
                                     b.passenger_name,b.ticket_number,b.ticket_status,b.payment_status,b.booking_status,b.total_amount,b.currency,
                                     t.code tenant_code
                              FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                              LEFT JOIN tenants t ON t.id=b.tenant_id
                              WHERE b.tenant_id=? AND b.portal_customer_id=? ORDER BY b.id DESC LIMIT 100""",
                     (sess['tenant_id'], sess['customer_id']))
            pc = q(conn, "SELECT full_name,email,phone FROM portal_customers WHERE id=? AND tenant_id=?",
                   (sess['customer_id'], sess['tenant_id']), one=True) or {}
        return self._json({'customer': {'id': sess['customer_id'], 'full_name': pc.get('full_name') or sess['full_name'],
                                        'email': pc.get('email') or sess['email'], 'phone': pc.get('phone')},
                           'bookings': rows})

    def api_portal_documents_get(self, qs):
        pnr = (qs.get('pnr') or [''])[0].strip().upper()
        last = (qs.get('last_name') or [''])[0].strip().lower()
        tcode = (qs.get('tenant') or [''])[0].strip()
        sess = get_portal_session(self)
        tenant = self._portal_tenant(tcode) if tcode else None
        with db() as conn:
            if pnr and (last or sess):
                if not tenant:
                    tr = q(conn, "SELECT tenant_id FROM pss_bookings WHERE pnr=? OR trip_pnr=? ORDER BY id LIMIT 1", (pnr, pnr), one=True)
                    tenant = q(conn, "SELECT * FROM tenants WHERE id=?", (tr['tenant_id'],), one=True) if tr else None
                if not tenant:
                    return self._json({'documents': []})
                tid = tenant['id']
                bookings = q(conn, """SELECT id,pnr,passenger_name,portal_customer_id FROM pss_bookings
                                      WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)""", (tid, pnr, pnr))
                allowed = [b['id'] for b in bookings
                           if (sess and b.get('portal_customer_id') == sess.get('customer_id'))
                           or (last and self._last_name_matches(b.get('passenger_name'), last))]
                if not allowed:
                    return self._json({'documents': []})
                marks = ','.join(['?'] * len(allowed))
                rows = q(conn, f"""SELECT d.id,d.doc_type,d.doc_no,d.title,d.file_name,d.mime_type,d.created_at,
                                          (d.file_data IS NOT NULL) has_file
                                   FROM pss_travel_documents d WHERE d.tenant_id=? AND d.booking_id IN ({marks})
                                   ORDER BY d.id DESC""", [tid] + allowed)
            elif sess:
                rows = q(conn, """SELECT d.id,d.doc_type,d.doc_no,d.title,d.file_name,d.mime_type,d.created_at,
                                         (d.file_data IS NOT NULL) has_file
                                  FROM pss_travel_documents d
                                  WHERE d.tenant_id=? AND d.booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND portal_customer_id=?)
                                  ORDER BY d.id DESC""", (sess['tenant_id'], sess['tenant_id'], sess['customer_id']))
            else:
                return self._json({'error': 'Sign in or provide PNR and surname'}, 401)
        return self._json({'documents': rows})

    def api_portal_document_download(self, did, qs):
        sess = get_portal_session(self)
        pnr = (qs.get('pnr') or [''])[0].strip().upper()
        last = (qs.get('last_name') or [''])[0].strip().lower()
        with db() as conn:
            doc = q(conn, "SELECT * FROM pss_travel_documents WHERE id=?", (self._oid(did),), one=True)
            if not doc:
                return self._json({'error': 'Document not found'}, 404)
            b = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (doc['booking_id'], doc['tenant_id']), one=True)
            allowed = bool(sess and sess.get('tenant_id') == doc['tenant_id'] and b
                           and b.get('portal_customer_id') == sess.get('customer_id'))
            if not allowed and pnr and last and b:
                trip = q(conn, """SELECT id,passenger_name FROM pss_bookings
                                  WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)""",
                         (doc['tenant_id'], pnr, pnr))
                allowed = any(x['id'] == doc['booking_id'] and self._last_name_matches(x.get('passenger_name'), last)
                              for x in trip)
            if not allowed:
                return self._json({'error': 'Access denied'}, 403)
            if not doc.get('file_data'):
                return self._json({'error': 'No file attached to this document'}, 404)
            ctype = doc.get('mime_type') or 'application/octet-stream'
            fname = str(doc.get('file_name') or 'travel-document').replace('"', '').replace('\r', '').replace('\n', '')
            inline = (qs.get('inline') or [''])[0].lower() in ('1', 'true', 'yes')
            disp = 'inline' if inline else 'attachment'
            return self._send(bytes(doc['file_data']), 200, ctype,
                              {'Content-Disposition': f'{disp}; filename="{fname}"'})

    def api_portal_documents_post(self):
        data = read_json(self)
        tenant = self._portal_tenant(data.get('tenant'))
        if not tenant:
            return self._json({'error': 'Unknown airline portal'}, 404)
        pnr = (data.get('pnr') or '').strip().upper()
        last = (data.get('last_name') or '').strip().lower()
        sess = get_portal_session(self)
        with db() as conn:
            b = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?) ORDER BY id LIMIT 1",
                  (tenant['id'], pnr, pnr), one=True)
            if not b:
                return self._json({'error': 'PNR not found'}, 404)
            if sess:
                if b.get('portal_customer_id') != sess.get('customer_id'):
                    return self._json({'error': 'Access denied'}, 403)
            elif last:
                if not self._last_name_matches(b.get('passenger_name'), last):
                    return self._json({'error': 'PNR or passenger name does not match'}, 404)
            else:
                return self._json({'error': 'Sign in or provide surname'}, 401)
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
        last = (data.get('last_name') or '').strip().lower()
        with db() as conn:
            bookings = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (tenant['id'], pnr, pnr))
            if not bookings:
                return self._json({'error': 'PNR not found'}, 404)
            if sess:
                bookings = [b for b in bookings if b.get('portal_customer_id') == sess.get('customer_id')]
                if not bookings:
                    return self._json({'error': 'Access denied'}, 403)
            elif last:
                if not any(self._last_name_matches(b.get('passenger_name'), last) for b in bookings):
                    return self._json({'error': 'PNR or passenger name does not match'}, 404)
            else:
                return self._json({'error': 'Passenger last name required'}, 403)
            amount = float(data.get('amount') or sum(float(b.get('total_amount') or 0) for b in bookings))
            cur = data.get('currency') or bookings[0].get('currency') or 'USD'
            gw = self._pss_flw_gateway(conn, tenant['id'])
            secret = (gw or {}).get('secret_key') or ''
            tx_ref = 'PSS-' + os.urandom(6).hex().upper()
            notes = f"{data.get('email') or ''} {data.get('phone') or ''}" + (' DEMO' if not secret else '')
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,channel_id,amount,currency,method,reference,gateway_ref,status,notes,booking_source,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,'portal',?,?)""",
                         (tenant['id'], tx_ref, bookings[0]['id'], pnr, bookings[0].get('channel_id'), amount, cur,
                          'flutterwave', data.get('reference'), tx_ref, notes, now_iso(), now_iso()))
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

    def _auto_ticket_paid_bookings(self, conn, tenant_id, rows, actor, trace_id, reason):
        ticketed = []
        for booking in rows or []:
            if booking.get('ticket_status') == 'issued':
                continue
            ticket = self._pss_next_ticket(conn, tenant_id, booking.get('channel_id'), booking.get('airport_code'),
                                           agency_id=booking.get('agency_id'), strict_agency=bool(booking.get('agency_id')))
            if not ticket:
                continue
            conn.execute("UPDATE pss_bookings SET ticket_number=?,ticket_status='issued',updated_at=? WHERE id=? AND tenant_id=?",
                         (ticket, now_iso(), booking['id'], tenant_id))
            conn.execute("""INSERT INTO pss_ticket_events(tenant_id,booking_id,pnr,previous_status,ticket_status,reason,actor,trace_id,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (tenant_id, booking['id'], booking.get('pnr'), booking.get('ticket_status'), 'issued', reason, actor, trace_id, now_iso()))
            self._activity(conn, tenant_id, 'pss_booking', booking['id'], 'ticket_issued', ticket, actor)
            self._trace(conn, tenant_id, actor, 'pss_booking', booking['id'], 'ticket_issued', actor, ticket, trace_id, self)
            ticketed.append(ticket)
        return ticketed

    def _apply_pss_flw(self, conn, tx_ref, gateway_data=None, status='successful'):
        pay = q(conn, "SELECT * FROM pss_payments WHERE gateway_ref=? OR payment_no=?", (tx_ref, tx_ref), one=True)
        if not pay or pay.get('status') in ('captured', 'paid'):
            return False
        conn.execute("UPDATE pss_payments SET status='captured', captured_at=?, paid_at=COALESCE(paid_at,?), gateway_ref=?, raw_json=? WHERE id=?",
                     (now_iso(), now_iso(), tx_ref, json.dumps(gateway_data or {}, default=str), pay['id']))
        rows = q(conn, "SELECT * FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)", (pay['tenant_id'], pay['pnr'], pay['pnr']))
        total = sum(float(r.get('total_amount') or 0) for r in rows)
        paid = (q(conn, "SELECT COALESCE(SUM(amount),0) v FROM pss_payments WHERE tenant_id=? AND pnr=? AND status IN ('captured','paid')", (pay['tenant_id'], pay['pnr']), one=True) or {}).get('v', 0)
        ps = 'paid' if paid >= total else 'partial'
        conn.execute("UPDATE pss_bookings SET payment_status=?,updated_at=? WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)",
                     (ps, now_iso(), pay['tenant_id'], pay['pnr'], pay['pnr']))
        if rows:
            trace_id = rows[0].get('trace_id') or self._new_trace_id()
            self._trace(conn, pay['tenant_id'], 'flutterwave', 'pss_booking', rows[0]['id'], 'payment_captured',
                        'flutterwave', tx_ref, trace_id, None)
            if ps == 'paid':
                self._auto_ticket_paid_bookings(conn, pay['tenant_id'], rows, 'flutterwave', trace_id,
                                                f"Auto-ticket after {tx_ref}")
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
            verified = False
            gateway_data = {'tx_ref': tx_ref, 'status': status}
            if secret:
                vstatus, vresp = flutterwave_verify(secret, tx_ref=tx_ref)
                verified = vstatus == 200 and str((vresp or {}).get('data', {}).get('status')).lower() == 'successful'
                gateway_data = vresp or gateway_data
            else:
                demo_payment = (str(pay.get('method') or '') == 'flutterwave'
                                and str(pay.get('gateway_ref') or '') == str(tx_ref)
                                and 'DEMO' in str(pay.get('notes') or ''))
                verified = demo_payment and status.lower() in ('successful', 'success')
            if verified:
                self._apply_pss_flw(conn, tx_ref, gateway_data, 'successful')
        return self._json({'ok': True, 'verified': bool(verified), 'tx_ref': tx_ref})

    def api_pss_travel_documents(self, tenant_id, qs):
        pnr = (qs.get('pnr') or [''])[0]
        booking_id = (qs.get('booking_id') or [''])[0]
        customer_id = (qs.get('customer_id') or [''])[0]
        with db() as conn:
            sql = """SELECT d.id,d.booking_id,d.pnr,d.doc_type,d.doc_no,d.title,d.file_name,d.mime_type,
                            d.expiry_date,d.reminder_sent_at,d.notes,d.created_at,
                            (CASE WHEN d.file_data IS NOT NULL THEN 1 ELSE 0 END) AS has_file
                     FROM pss_travel_documents d WHERE d.tenant_id=?"""
            args = [tenant_id]
            if pnr:
                sql += " AND d.pnr=?"; args.append(pnr)
            if booking_id:
                sql += " AND d.booking_id=?"; args.append(self._oid(booking_id))
            if customer_id:
                sql += " AND d.booking_id IN (SELECT id FROM pss_bookings WHERE tenant_id=? AND customer_id=?)"
                args.extend([tenant_id, self._oid(customer_id)])
            sql += " ORDER BY d.id DESC LIMIT 200"
            rows = q(conn, sql, args)
        return self._json({'documents': rows})

    def api_pss_travel_document_create(self, tenant_id, user_id):
        data = read_json(self)
        booking_id = self._oid(data.get('booking_id'))
        pnr = (data.get('pnr') or '').strip().upper()
        if not booking_id and not pnr:
            return self._json({'error': 'booking_id or pnr required'}, 400)
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
            if booking_id and not pnr:
                b = q(conn, "SELECT pnr FROM pss_bookings WHERE id=? AND tenant_id=?", (booking_id, tenant_id), one=True)
                if b:
                    pnr = b.get('pnr') or ''
            cur = conn.execute("""INSERT INTO pss_travel_documents(tenant_id,booking_id,pnr,doc_type,doc_no,title,file_name,mime_type,file_data,notes,expiry_date,uploaded_by,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tenant_id, booking_id or None, pnr or None, data.get('doc_type') or 'passport',
                                data.get('doc_no'), data.get('title') or data.get('doc_type') or 'Travel document',
                                data.get('file_name'), data.get('mime_type'), file_data, data.get('notes'),
                                data.get('expiry_date') or None, user_id, ts, ts))
            did = cur.lastrowid
            self._activity(conn, tenant_id, 'pss_booking', booking_id or did, 'travel_document_added',
                           f"{data.get('doc_type') or 'document'} {data.get('doc_no') or ''}", user_id)
        return self._json({'ok': True, 'document_id': did})

    def api_pss_travel_document_download(self, tenant_id, did, qs=None):
        did = self._oid(did)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_travel_documents WHERE id=? AND tenant_id=?", (did, tenant_id), one=True)
        if not row:
            return self._json({'error': 'Travel document not found'}, 404)
        payload = row.get('file_data')
        if not payload:
            return self._json({'error': 'No file attached'}, 404)
        fname = (row.get('file_name') or row.get('title') or 'document').replace('"', '')
        force = str(((qs or {}).get('download') or [''])[0]).lower() in ('1', 'true', 'yes')
        return self._send(payload, 200, row.get('mime_type') or 'application/octet-stream',
                          {'Content-Disposition': f"{'attachment' if force else 'inline'}; filename=\"{fname}\""})

    def api_pss_travel_document_mutate(self, tenant_id, user_id, did, method):
        did = self._oid(did)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_travel_documents WHERE id=? AND tenant_id=?", (did, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Travel document not found'}, 404)
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_travel_documents WHERE id=? AND tenant_id=?", (did, tenant_id))
                return self._json({'ok': True})
            data = read_json(self)
            fields = {}
            for key in ('doc_type', 'doc_no', 'title', 'notes', 'expiry_date'):
                if key in data:
                    fields[key] = data[key] or None
            if fields:
                sets = ','.join(f'{k}=?' for k in fields)
                conn.execute(f"UPDATE pss_travel_documents SET {sets},updated_at=? WHERE id=? AND tenant_id=?",
                             (*fields.values(), now_iso(), did, tenant_id))
        return self._json({'ok': True})

    def api_pss_travel_expiry_reminders(self, tenant_id, user_id):
        days = 30
        data = read_json(self) if (self.headers.get('Content-Length') or '0') not in ('', '0', None) else {}
        try:
            days = max(1, min(365, int(data.get('days') or 30)))
        except Exception:
            days = 30
        created = 0
        with db() as conn:
            rows = q(conn, """SELECT d.id,d.title,d.doc_no,d.expiry_date,d.booking_id,b.passenger_name,b.pnr,b.contact_email,b.contact_phone
                              FROM pss_travel_documents d LEFT JOIN pss_bookings b ON b.id=d.booking_id
                              WHERE d.tenant_id=? AND d.expiry_date IS NOT NULL AND d.expiry_date<>''
                                AND d.reminder_sent_at IS NULL""", (tenant_id,))
            today = date.today()
            for row in rows:
                try:
                    exp = datetime.strptime(str(row['expiry_date'])[:10], '%Y-%m-%d').date()
                except Exception:
                    continue
                diff = (exp - today).days
                if diff > days:
                    continue
                status = 'expired' if diff < 0 else ('expires today' if diff == 0 else f'expires in {diff} day(s)')
                who = row.get('passenger_name') or row.get('pnr') or 'Passenger'
                title = f"Travel document expiry: {row.get('title') or row.get('doc_type') or 'Document'}"
                msg = f"{who}'s {row.get('title') or 'travel document'} {status} on {row['expiry_date']}. Doc no: {row.get('doc_no') or '-'}. Contact: {row.get('contact_email') or row.get('contact_phone') or 'on file'}."
                conn.execute("""INSERT INTO notifications(tenant_id,user_id,notification_type,title,message,entity_type,entity_id,is_read,created_at)
                                VALUES (?,?,?,?,?,?,?,?,?)""",
                             (tenant_id, user_id, 'warning', title, msg, 'pss_booking', row.get('booking_id') or row['id'], 0, now_iso()))
                conn.execute("UPDATE pss_travel_documents SET reminder_sent_at=? WHERE id=? AND tenant_id=?", (now_iso(), row['id'], tenant_id))
                created += 1
        return self._json({'ok': True, 'created': created})

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
            conn.execute("""INSERT INTO pss_payments(tenant_id,payment_no,booking_id,pnr,channel_id,amount,currency,method,reference,gateway_ref,status,notes,booking_source,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,'flutterwave',?,?,'pending',?,'agency_direct',?,?)""",
                         (tenant_id, tx_ref, bid, b['pnr'], b.get('channel_id'), amount,
                          data.get('currency') or b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', data.get('reference'), tx_ref,
                          ('DEMO' if not secret else ''), now_iso(), now_iso()))
            if not secret:
                return self._json({'ok': True, 'demo': True, 'tx_ref': tx_ref,
                                   'url': f'/api/pss/payments/flutterwave/verify?tx_ref={tx_ref}&status=successful'})
            channel = q(conn, "SELECT name FROM pss_channels WHERE id=?", (b.get('channel_id'),), one=True) if b.get('channel_id') else None
            payload = {'tx_ref': tx_ref, 'amount': amount, 'currency': data.get('currency') or b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD',
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
                              float(item.get('amount') or 0), item.get('currency') or b.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD', now_iso()))
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


class PartnerAPIHandler(Handler):

    def _public_api_key(self):
        token = (self.headers.get('X-API-Key') or '').strip()
        if not token:
            auth = (self.headers.get('Authorization') or '').strip()
            if auth.lower().startswith('bearer '):
                token = auth[7:].strip()
        if not token:
            return None
        digest = _hash_api_key(token)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_api_keys WHERE tenant_id IS NOT NULL AND token_hash=? AND status='active'", (digest,), one=True)
            if not row:
                return None
            expires = row.get('expires_at')
            if expires and str(expires) < now_iso():
                return None
            conn.execute("UPDATE pss_api_keys SET last_used_at=? WHERE id=?", (now_iso(), row['id']))
            return dict(row)

    def api_public_docs(self):
        return self._json({
            'version': '1.0',
            'auth': 'Send X-API-Key (or Authorization: Bearer) on every offer/order request.',
            'base_url': '/api/public/v1',
            'endpoints': [
                {'method': 'POST', 'path': '/api/public/v1/offers', 'description': 'Quote flights and create a timed offer.',
                 'example': {'flights': [{'flight_id': 1, 'cabin': 'Y'}], 'passenger_count': 1, 'pos': 'DIRECT', 'currency': 'USD', 'ttl_minutes': 30}},
                {'method': 'GET', 'path': '/api/public/v1/offers/{token}', 'description': 'Retrieve an offer and check expiry.', 'example': {}},
                {'method': 'POST', 'path': '/api/public/v1/offers/{token}/orders', 'description': 'Convert an active offer into bookings and an order.',
                 'example': {'passengers': [{'name': 'John Doe', 'type': 'ADT'}], 'contact': {'email': 'john@example.com', 'phone': '+256700000000'}, 'idempotency_key': 'booking-1001'}},
                {'method': 'GET', 'path': '/api/public/v1/orders/{order_no}', 'description': 'Retrieve order and linked PNRs.', 'example': {}},
                {'method': 'POST', 'path': '/api/public/v1/orders/{order_no}/cancel', 'description': 'Cancel an order and restore seats.', 'example': {'reason': 'Customer request'}},
            ],
            'webhook_events': list(WEBHOOK_EVENTS),
            'webhook_headers': ['X-Fayaka-Event', 'X-Fayaka-Signature: sha256=<HMAC-SHA256 body signature>'],
            'errors': {'401': 'Missing or invalid API key', '404': 'Offer/order not found', '409': 'Expired offer, insufficient seats, or duplicate idempotency key', '422': 'Validation error'},
        })

    def api_public_offer_create(self):
        key = self._public_api_key()
        if not key:
            return self._json({'error': 'Invalid API key'}, 401)
        data = read_json(self)
        flights = data.get('flights') or []
        if isinstance(flights, dict):
            flights = [flights]
        try:
            pax_count = max(1, min(9, int(data.get('passenger_count') or 1)))
        except Exception:
            return self._json({'error': 'passenger_count must be a number'}, 422)
        if not flights:
            return self._json({'error': 'At least one flight is required'}, 422)
        currency = str(data.get('currency') or self._tenant_currency(conn, tenant_id) or 'USD').upper()
        pos = str(data.get('pos') or 'DIRECT').upper()
        channel = str(data.get('channel') or 'api')
        try:
            ttl = int(data.get('ttl_minutes') or 30)
            if ttl < 1 or ttl > 1440:
                raise ValueError('ttl_minutes must be between 1 and 1440')
            expires_at = (datetime.utcnow() + timedelta(minutes=ttl)).replace(microsecond=0).isoformat() + 'Z'
            with db() as conn:
                priced = []
                total = 0.0
                for item in flights:
                    fid = int(item.get('flight_id') or item.get('id') or 0)
                    flight = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (fid, key['tenant_id']), one=True)
                    if not flight:
                        raise ValueError(f"Flight {fid} was not found")
                    if str(flight.get('status') or '').lower() in ('cancelled', 'closed'):
                        raise ValueError(f"Flight {flight.get('flight_no')} is not available")
                    if int(flight.get('available_seats') or 0) < pax_count:
                        raise ValueError(f"Flight {flight.get('flight_no')} has insufficient seats")
                    cabin = str(item.get('cabin') or item.get('fare_class') or 'Y').upper()
                    unit = round(float(flight.get('base_fare') or 0) * self._portal_cabin_multiplier(cabin), 2)
                    taxes = round(unit * 0.05, 2)
                    amount = round((unit + taxes) * pax_count, 2)
                    total += amount
                    priced.append({'flight_id': fid, 'flight_no': flight.get('flight_no'), 'flight_date': flight.get('flight_date'),
                                   'origin': flight.get('origin'), 'destination': flight.get('destination'), 'dep_time': flight.get('dep_time'),
                                   'arr_time': flight.get('arr_time'), 'cabin': cabin, 'unit_price': unit, 'taxes': taxes, 'amount': amount})
                offer_no = self._psn('OFF')
                token = make_token()
                pricing = {'segments': priced, 'currency': currency, 'passenger_count': pax_count, 'taxes': round(sum(float(s['taxes']) for s in priced) * pax_count, 2)}
                conn.execute("""INSERT INTO pss_offers
                    (tenant_id,offer_no,token,api_key_id,channel,pos,currency,passenger_count,segments_json,pricing_json,total_amount,status,expires_at,created_by,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)""",
                    (key['tenant_id'], offer_no, token, key['id'], channel, pos, currency, pax_count,
                     json.dumps(priced), json.dumps(pricing), round(total, 2), expires_at, None, now_iso(), now_iso()))
                row = q(conn, "SELECT * FROM pss_offers WHERE token=?", (token,), one=True)
                self._trace(conn, key['tenant_id'], 'api', 'offer', offer_no, 'offer_created', actor=key.get('name') or 'api',
                            detail=json.dumps({'pos': pos, 'passenger_count': pax_count}), handler=self)
            payload = {'offer_no': offer_no, 'token': token, 'status': 'active', 'total_amount': round(total, 2), 'currency': currency}
            _webhook_event(key['tenant_id'], 'offer.created', payload, offer_no)
            return self._json({'offer': {'offer_no': offer_no, 'token': token, 'status': 'active', 'expires_at': expires_at,
                                         'currency': currency, 'passenger_count': pax_count, 'total_amount': round(total, 2),
                                         'segments': priced, 'pricing': pricing}}, 201)
        except ValueError as exc:
            return self._json({'error': str(exc)}, 422)
        except Exception as exc:
            return self._json({'error': f'Could not create offer: {exc}'}, 500)

    def api_public_offer_get(self, token):
        key = self._public_api_key()
        if not key:
            return self._json({'error': 'Invalid API key'}, 401)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_offers WHERE token=? AND tenant_id=?", (token, key['tenant_id']), one=True)
            if not row:
                return self._json({'error': 'Offer not found'}, 404)
            if row.get('status') == 'active' and row.get('expires_at') and str(row['expires_at']) < now_iso():
                conn.execute("UPDATE pss_offers SET status='expired',updated_at=? WHERE id=?", (now_iso(), row['id']))
                row['status'] = 'expired'
            pricing = _json_load(row.get('pricing_json'), {})
            segments = _json_load(row.get('segments_json'), [])
        return self._json({'offer': {'offer_no': row.get('offer_no'), 'token': row.get('token'), 'status': row.get('status'),
                                     'expires_at': row.get('expires_at'), 'total_amount': row.get('total_amount'),
                                     'currency': row.get('currency'), 'passenger_count': row.get('passenger_count'),
                                     'segments': segments, 'pricing': pricing}})

    def api_public_order_create(self, token):
        key = self._public_api_key()
        if not key:
            return self._json({'error': 'Invalid API key'}, 401)
        data = read_json(self)
        with db() as conn:
            offer = q(conn, "SELECT * FROM pss_offers WHERE token=? AND tenant_id=?", (token, key['tenant_id']), one=True)
            if not offer:
                return self._json({'error': 'Offer not found'}, 404)
            if offer.get('status') == 'ordered' and offer.get('order_id'):
                order = q(conn, "SELECT * FROM pss_orders WHERE id=?", (offer['order_id'],), one=True)
                return self._json({'order': {'order_no': order.get('order_no'), 'pnr': order.get('pnr'), 'status': order.get('status')}, 'duplicate': True})
            if offer.get('status') != 'active':
                return self._json({'error': f"Offer is {offer.get('status')}"}, 409)
            if offer.get('expires_at') and str(offer['expires_at']) < now_iso():
                conn.execute("UPDATE pss_offers SET status='expired',updated_at=? WHERE id=?", (now_iso(), offer['id']))
                return self._json({'error': 'Offer has expired'}, 409)
            pax_count = int(offer.get('passenger_count') or 1)
            passengers = data.get('passengers') or []
            if isinstance(passengers, dict):
                passengers = [passengers]
            while len(passengers) < pax_count:
                passengers.append({'name': f'PAX {len(passengers)+1}', 'type': 'ADT'})
            passengers = passengers[:pax_count]
            contact = data.get('contact') or {}
            idem = str(data.get('idempotency_key') or '').strip()
            order_no = self._psn('ORD')
            if idem:
                order_no = 'ORD-' + hashlib.sha256(f"{key['id']}:{idem}".encode('utf-8')).hexdigest()[:10].upper()
                existing = q(conn, "SELECT * FROM pss_orders WHERE tenant_id=? AND order_no=?", (key['tenant_id'], order_no), one=True)
                if existing:
                    return self._json({'order': {'order_no': existing.get('order_no'), 'pnr': existing.get('pnr'), 'status': existing.get('status')}, 'duplicate': True})
            def alloc_pnr():
                for _ in range(30):
                    candidate = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
                    used = q(conn, """SELECT 1 used FROM pss_bookings WHERE tenant_id=? AND (pnr=? OR trip_pnr=?)
                                      UNION SELECT 1 FROM pss_orders WHERE tenant_id=? AND pnr=? LIMIT 1""",
                             (key['tenant_id'], candidate, candidate, key['tenant_id'], candidate), one=True)
                    if not used:
                        return candidate
                raise ValueError('Could not allocate PNR')

            segments = _json_load(offer.get('segments_json'), [])
            if not segments:
                return self._json({'error': 'Offer has no segments'}, 422)
            trip_pnr = alloc_pnr()
            booking_ids = []
            total_amount = 0.0
            conn.execute('SAVEPOINT api_order_create')
            try:
                for pax in passengers:
                    pax_name = str((pax.get('name') if isinstance(pax, dict) else pax) or f'PAX {len(booking_ids)+1}')
                    pax_type = str((pax.get('type') if isinstance(pax, dict) else 'ADT') or 'ADT')
                    for seg in segments:
                        fid = int(seg.get('flight_id') or 0)
                        cur = conn.execute("""UPDATE pss_flights SET available_seats=available_seats-1,updated_at=?
                                              WHERE id=? AND tenant_id=? AND available_seats>0
                                                AND COALESCE(status,'scheduled') NOT IN ('cancelled','closed')""",
                                           (now_iso(), fid, key['tenant_id']))
                        if cur.rowcount != 1:
                            raise ValueError(f"Flight {seg.get('flight_no') or fid} no longer has enough seats")
                        fare = float(seg.get('unit_price') or 0)
                        taxes = float(seg.get('taxes') or 0)
                        amount = round(fare + taxes, 2)
                        total_amount += amount
                        cur = conn.execute("""INSERT INTO pss_bookings
                            (tenant_id,pnr,trip_pnr,flight_id,passenger_name,passenger_type,contact_email,contact_phone,airport_code,fare_class,
                             fare_amount,taxes,total_amount,currency,ticket_status,payment_status,booking_status,created_by,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unissued','pending','confirmed',?,?,?)""",
                            (key['tenant_id'], alloc_pnr(), trip_pnr, fid, pax_name, pax_type, str(contact.get('email') or ''), str(contact.get('phone') or ''),
                             str(seg.get('origin') or ''), str(seg.get('cabin') or 'Y'), fare, taxes, amount, str(offer.get('currency') or 'USD'),
                             None, now_iso(), now_iso()))
                        booking_id = int(cur.lastrowid)
                        self._seed_tax_lines(conn, key['tenant_id'], booking_id,
                                             {'taxes': taxes, 'currency': str(offer.get('currency') or 'USD')})
                        booking_ids.append(booking_id)
                cur = conn.execute("""INSERT INTO pss_orders
                    (tenant_id,offer_id,order_no,status,pnr,passenger_count,amount,currency,contact_json,booking_ids_json,payment_status,created_by,created_at,updated_at)
                    VALUES (?,?,?,'confirmed',?,?,?,?,?,?,'pending',?,?,?)""",
                    (key['tenant_id'], offer['id'], order_no, trip_pnr, pax_count, round(total_amount, 2), str(offer.get('currency') or 'USD'),
                     json.dumps(contact), json.dumps(booking_ids), None, now_iso(), now_iso()))
                order_id = int(cur.lastrowid)
                conn.execute("UPDATE pss_offers SET status='ordered',order_id=?,updated_at=? WHERE id=?", (order_id, now_iso(), offer['id']))
                self._trace(conn, key['tenant_id'], 'api', 'order', order_no, 'order_created', actor=key.get('name') or 'api',
                            detail=json.dumps({'pnr': trip_pnr, 'passenger_count': pax_count, 'bookings': booking_ids}), handler=self)
                conn.execute('RELEASE SAVEPOINT api_order_create')
            except ValueError as exc:
                conn.execute('ROLLBACK TO SAVEPOINT api_order_create')
                conn.execute('RELEASE SAVEPOINT api_order_create')
                return self._json({'error': str(exc)}, 409)
            except Exception as exc:
                conn.execute('ROLLBACK TO SAVEPOINT api_order_create')
                conn.execute('RELEASE SAVEPOINT api_order_create')
                return self._json({'error': f'Could not create order: {exc}'}, 500)
            order_row = q(conn, "SELECT * FROM pss_orders WHERE id=?", (order_id,), one=True)
        payload = {'order_no': order_no, 'pnr': trip_pnr, 'status': 'confirmed', 'amount': round(total_amount, 2), 'booking_ids': booking_ids}
        _webhook_event(key['tenant_id'], 'order.created', payload, order_no)
        _webhook_event(key['tenant_id'], 'booking.created', {'pnr': trip_pnr, 'booking_ids': booking_ids}, trip_pnr)
        return self._json({'order': {'order_no': order_row.get('order_no'), 'pnr': order_row.get('pnr'),
                                     'status': order_row.get('status'), 'amount': order_row.get('amount'),
                                     'currency': order_row.get('currency'), 'booking_ids': booking_ids,
                                     'payment_status': order_row.get('payment_status')}}, 201)

    def api_public_order_get(self, order_no):
        key = self._public_api_key()
        if not key:
            return self._json({'error': 'Invalid API key'}, 401)
        with db() as conn:
            row = q(conn, "SELECT * FROM pss_orders WHERE order_no=? AND tenant_id=?", (order_no, key['tenant_id']), one=True)
            if not row:
                return self._json({'error': 'Order not found'}, 404)
            bookings = q(conn, """SELECT id,pnr,trip_pnr,passenger_name,flight_id,ticket_status,booking_status
                                  FROM pss_bookings WHERE tenant_id=? AND (trip_pnr=? OR pnr=?)""",
                         (key['tenant_id'], row.get('pnr') or '', row.get('pnr') or ''))
        return self._json({'order': {'order_no': row.get('order_no'), 'pnr': row.get('pnr'), 'status': row.get('status'),
                                     'amount': row.get('amount'), 'currency': row.get('currency'), 'payment_status': row.get('payment_status'),
                                     'created_at': row.get('created_at'), 'bookings': bookings}})

    def api_public_order_cancel(self, order_no):
        key = self._public_api_key()
        if not key:
            return self._json({'error': 'Invalid API key'}, 401)
        data = read_json(self)
        with db() as conn:
            order = q(conn, "SELECT * FROM pss_orders WHERE order_no=? AND tenant_id=?", (order_no, key['tenant_id']), one=True)
            if not order:
                return self._json({'error': 'Order not found'}, 404)
            if order.get('status') == 'cancelled':
                return self._json({'order': {'order_no': order_no, 'status': 'cancelled'}, 'duplicate': True})
            bookings = q(conn, """SELECT * FROM pss_bookings WHERE tenant_id=? AND (trip_pnr=? OR pnr=?)
                                  AND booking_status NOT IN ('cancelled','refunded')""",
                         (key['tenant_id'], order.get('pnr') or '', order.get('pnr') or ''))
            for booking in bookings:
                conn.execute("UPDATE pss_flights SET available_seats=available_seats+1,updated_at=? WHERE id=? AND tenant_id=?",
                             (now_iso(), booking.get('flight_id'), key['tenant_id']))
                conn.execute("UPDATE pss_bookings SET booking_status='cancelled',ticket_status='void',updated_at=? WHERE id=?",
                             (now_iso(), booking['id']))
            conn.execute("UPDATE pss_orders SET status='cancelled',updated_at=? WHERE id=?", (now_iso(), order['id']))
            conn.execute("UPDATE pss_offers SET status='cancelled',updated_at=? WHERE id=?", (now_iso(), order.get('offer_id')))
            self._trace(conn, key['tenant_id'], 'api', 'order', order_no, 'order_cancelled',
                        actor=key.get('name') or 'api', detail=json.dumps({'reason': data.get('reason') or ''}), handler=self)
        _webhook_event(key['tenant_id'], 'order.cancelled', {'order_no': order_no, 'reason': data.get('reason') or ''}, order_no)
        return self._json({'order': {'order_no': order_no, 'status': 'cancelled', 'seats_restored': len(bookings)}})

    def api_developer_api_keys(self, tenant_id, method, identifier=None, user_id=None):
        with db() as conn:
            if method == 'GET':
                rows = q(conn, "SELECT id,name,token_prefix,scopes,status,expires_at,last_used_at,revoked_at,created_at FROM pss_api_keys WHERE tenant_id=? ORDER BY id DESC", (tenant_id,))
                return self._json({'keys': rows})
            if method == 'POST':
                data = read_json(self)
                name = str(data.get('name') or '').strip()
                if not name:
                    return self._json({'error': 'Key name is required'}, 422)
                token = 'fk_live_' + make_token()
                scopes = ','.join(_event_list(data.get('scopes') or 'offers:write,orders:write'))
                expires = data.get('expires_at') or None
                conn.execute("""INSERT INTO pss_api_keys
                    (tenant_id,name,token_prefix,token_hash,scopes,status,expires_at,created_by,created_at)
                    VALUES (?,?,?,?,?,'active',?,?,?)""",
                    (tenant_id, name, token[:16], _hash_api_key(token), scopes, expires, user_id, now_iso()))
                log_audit(conn, tenant_id, user_id, 'api_key_created', 'api_key', name, {'scopes': scopes})
                return self._json({'token': token, 'message': 'Copy this token now; it is not shown again.'}, 201)
            if method == 'DELETE':
                conn.execute("UPDATE pss_api_keys SET status='revoked',revoked_at=? WHERE id=? AND tenant_id=?", (now_iso(), identifier, tenant_id))
                log_audit(conn, tenant_id, user_id, 'api_key_revoked', 'api_key', identifier, {})
                return self._json({'ok': True})

    def api_developer_webhooks(self, tenant_id, method, identifier=None, user_id=None):
        with db() as conn:
            if method == 'GET':
                rows = q(conn, "SELECT id,name,url,events,status,created_at,updated_at FROM pss_webhooks WHERE tenant_id=? ORDER BY id DESC", (tenant_id,))
                for row in rows:
                    row['secret_hint'] = 'whsec_...' if row.get('id') else ''
                secrets = q(conn, "SELECT id,secret FROM pss_webhooks WHERE tenant_id=?", (tenant_id,))
                hints = {s['id']: s.get('secret') or '' for s in secrets}
                for row in rows:
                    row['secret_hint'] = (hints.get(row['id'])[:12] + '...') if hints.get(row['id']) else ''
                return self._json({'webhooks': rows, 'events': list(WEBHOOK_EVENTS)})
            if method == 'POST':
                data = read_json(self)
                name = str(data.get('name') or '').strip()
                url = str(data.get('url') or '').strip()
                events = ','.join(_event_list(data.get('events') or 'order.created,booking.created'))
                if not name or not url.startswith(('http://', 'https://')):
                    return self._json({'error': 'Webhook name and a valid http(s) URL are required'}, 422)
                secret = 'whsec_' + make_token()
                conn.execute("""INSERT INTO pss_webhooks
                    (tenant_id,name,url,secret,events,status,created_by,created_at,updated_at)
                    VALUES (?,?,?,?,?,'active',?,?,?)""",
                    (tenant_id, name, url, secret, events, user_id, now_iso(), now_iso()))
                log_audit(conn, tenant_id, user_id, 'webhook_created', 'webhook', name, {'events': events, 'url': url})
                return self._json({'id': q(conn, "SELECT id FROM pss_webhooks WHERE tenant_id=? AND name=?", (tenant_id, name), one=True)['id'],
                                   'secret': secret, 'message': 'Copy this signing secret now.'}, 201)
            if method == 'PUT':
                data = read_json(self)
                url = str(data.get('url') or '').strip()
                events = ','.join(_event_list(data.get('events') or ''))
                secret = 'whsec_' + make_token() if data.get('rotate_secret') else None
                fields, values = [], []
                for column, value in (('url', url), ('events', events), ('status', data.get('status')), ('secret', secret)):
                    if value not in (None, ''):
                        fields.append(f'{column}=?')
                        values.append(value)
                if data.get('name'):
                    fields.append('name=?')
                    values.append(str(data['name']).strip())
                if not fields:
                    return self._json({'error': 'Nothing to update'}, 422)
                fields.append('updated_at=?')
                values.extend([now_iso(), identifier, tenant_id])
                conn.execute(f"UPDATE pss_webhooks SET {','.join(fields)} WHERE id=? AND tenant_id=?", tuple(values))
                return self._json({'ok': True, **({'secret': secret} if secret else {})})
            if method == 'DELETE':
                conn.execute("DELETE FROM pss_webhooks WHERE id=? AND tenant_id=?", (identifier, tenant_id))
                return self._json({'ok': True})

    def api_developer_webhook_retry(self, tenant_id, delivery_id):
        with db() as conn:
            row = q(conn, "SELECT id FROM pss_webhook_deliveries WHERE id=? AND tenant_id=?", (delivery_id, tenant_id), one=True)
            if not row:
                return self._json({'error': 'Delivery not found'}, 404)
            conn.execute("UPDATE pss_webhook_deliveries SET status='pending',next_retry_at=NULL WHERE id=?", (delivery_id,))
        _deliver_webhooks(tenant_id)
        return self._json({'ok': True})

    def api_developer_deliveries(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT d.id,d.event,d.object_id,d.status,d.attempts,d.response_code,d.response_body,
                                     d.next_retry_at,d.created_at,d.delivered_at,w.name webhook,w.url
                              FROM pss_webhook_deliveries d LEFT JOIN pss_webhooks w ON w.id=d.webhook_id
                              WHERE d.tenant_id=? ORDER BY d.id DESC LIMIT 200""", (tenant_id,))
        return self._json({'deliveries': rows})

    def api_revenue_dashboard(self, tenant_id, qs):
        try:
            end = datetime.strptime((qs.get('to') or [''])[0], '%Y-%m-%d').date() if qs.get('to') else date.today()
        except Exception:
            end = date.today()
        to_date = end.isoformat()
        try:
            start = datetime.strptime((qs.get('from') or [''])[0], '%Y-%m-%d').date() if qs.get('from') else end - timedelta(days=29)
        except Exception:
            start = end - timedelta(days=29)
        if start > end:
            start = end - timedelta(days=29)
        from_date = start.isoformat()
        with db() as conn:
            bookings = q(conn, """SELECT * FROM pss_bookings WHERE tenant_id=?
                                  AND substr(created_at,1,10) BETWEEN ? AND ?""", (tenant_id, from_date, to_date))
            payments = q(conn, """SELECT * FROM pss_payments WHERE tenant_id=? AND status IN ('paid','received','captured')
                                  AND substr(COALESCE(paid_at,created_at),1,10) BETWEEN ? AND ?""", (tenant_id, from_date, to_date))
            ancillaries = q(conn, """SELECT * FROM pss_ancillary_sales WHERE tenant_id=? AND status='sold'
                                     AND substr(created_at,1,10) BETWEEN ? AND ?""", (tenant_id, from_date, to_date))
            refunds = []
            try:
                refunds = q(conn, """SELECT * FROM refunds WHERE tenant_id=?
                                     AND substr(created_at,1,10) BETWEEN ? AND ?""", (tenant_id, from_date, to_date))
            except Exception:
                pass
            flights = q(conn, "SELECT id,flight_no,origin,destination,flight_date FROM pss_flights WHERE tenant_id=?", (tenant_id,))
            channels = q(conn, "SELECT id,name,code FROM pss_channels WHERE tenant_id=?", (tenant_id,))
        def _map_int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        flight_map = {_map_int(f['id']): f for f in flights if _map_int(f['id']) is not None}
        channel_map = {_map_int(c['id']): c for c in channels if _map_int(c['id']) is not None}
        gross = round(sum(float(b.get('total_amount') or 0) for b in bookings), 2)
        collected = round(sum(float(p.get('amount') or 0) for p in payments), 2)
        ancillary = round(sum(float(a.get('amount') or 0) for a in ancillaries), 2)
        refunded = round(sum(float(r.get('amount') or 0) for r in refunds), 2)
        by_date = {}
        for b in bookings:
            day = str(b.get('created_at') or '')[:10]
            if day:
                by_date[day] = round(by_date.get(day, 0) + float(b.get('total_amount') or 0), 2)
        by_channel = {}
        for b in bookings:
            name = (channel_map.get(_map_int(b.get('channel_id'))) or {}).get('name') or 'Direct/Unassigned'
            by_channel[name] = round(by_channel.get(name, 0) + float(b.get('total_amount') or 0), 2)
        by_route = {}
        for b in bookings:
            flight = flight_map.get(_map_int(b.get('flight_id'))) or {}
            route = f"{flight.get('origin') or '?'} - {flight.get('destination') or '?'}"
            by_route[route] = round(by_route.get(route, 0) + float(b.get('total_amount') or 0), 2)
        return self._json({
            'period': {'from': from_date, 'to': to_date},
            'kpis': {'gross_revenue': gross, 'cash_collected': collected, 'ancillary_revenue': ancillary,
                     'refunds': refunded, 'net_revenue': round(gross + ancillary - refunded, 2),
                     'bookings': len(bookings), 'average_booking_value': round(gross / len(bookings), 2) if bookings else 0},
            'series': [{'date': d, 'revenue': v} for d, v in sorted(by_date.items())],
            'channels': [{'name': k, 'revenue': v} for k, v in sorted(by_channel.items(), key=lambda x: -x[1])],
            'routes': [{'route': k, 'revenue': v} for k, v in sorted(by_route.items(), key=lambda x: -x[1])],
            'recent_bookings': bookings[-20:][::-1],
        })

    def api_onboarding_status(self, tenant_id, method, user_id=None):
        if method == 'POST':
            data = read_json(self)
            with db() as conn:
                row = q(conn, "SELECT data_json FROM onboarding_progress WHERE tenant_id=?", (tenant_id,), one=True)
                merged = _json_load(row.get('data_json') if row else None, {})
                merged.update(data.get('data') or data)
                conn.execute("""INSERT INTO onboarding_progress(tenant_id,step,data_json,completed,updated_at)
                                VALUES (?,?,?,?,?)
                                ON CONFLICT(tenant_id) DO UPDATE SET step=excluded.step,data_json=excluded.data_json,
                                completed=excluded.completed,updated_at=excluded.updated_at""",
                             (tenant_id, str(data.get('step') or 'profile'), json.dumps(merged),
                              1 if data.get('completed') else 0, now_iso()))
        with db() as conn:
            progress = q(conn, "SELECT step,data_json,completed,updated_at FROM onboarding_progress WHERE tenant_id=?", (tenant_id,), one=True) or {}
            counts = {
                'users': q(conn, "SELECT COUNT(*) c FROM users WHERE tenant_id=?", (tenant_id,), one=True)['c'],
                'branding': q(conn, "SELECT COUNT(*) c FROM tenant_branding WHERE tenant_id=?", (tenant_id,), one=True)['c'],
                'flights': q(conn, "SELECT COUNT(*) c FROM pss_flights WHERE tenant_id=?", (tenant_id,), one=True)['c'],
                'channels': q(conn, "SELECT COUNT(*) c FROM pss_channels WHERE tenant_id=?", (tenant_id,), one=True)['c'],
                'stock': q(conn, "SELECT COUNT(*) c FROM pss_stock_pools WHERE tenant_id=?", (tenant_id,), one=True)['c'],
                'api_keys': q(conn, "SELECT COUNT(*) c FROM pss_api_keys WHERE tenant_id=? AND status='active'", (tenant_id,), one=True)['c'],
                'webhooks': q(conn, "SELECT COUNT(*) c FROM pss_webhooks WHERE tenant_id=? AND status='active'", (tenant_id,), one=True)['c'],
            }
        checklist = [
            {'key': 'profile', 'label': 'Airline profile and branding', 'done': int(counts['branding']) > 0, 'path': 'agency-profile'},
            {'key': 'users', 'label': 'Admin and team users', 'done': int(counts['users']) > 0, 'path': 'setup-users'},
            {'key': 'schedule', 'label': 'At least one flight', 'done': int(counts['flights']) > 0, 'path': 'pss-flights'},
            {'key': 'distribution', 'label': 'Sales channel or direct stock', 'done': int(counts['channels']) > 0 or int(counts['stock']) > 0, 'path': 'pss-channels'},
            {'key': 'api', 'label': 'Partner API key', 'done': int(counts['api_keys']) > 0, 'path': 'developer'},
            {'key': 'webhooks', 'label': 'Webhook subscription', 'done': int(counts['webhooks']) > 0, 'path': 'developer'},
        ]
        return self._json({'progress': progress, 'counts': counts, 'checklist': checklist,
                           'completed': all(item['done'] for item in checklist)})

    def api_onboarding_create_tenant(self, current_tenant_id, user_id):
        data = read_json(self)
        name = str(data.get('airline_name') or '').strip()
        code = str(data.get('code') or '').strip().upper()
        if not name or not code or not code.isalnum():
            return self._json({'error': 'Airline name and an alphanumeric code are required'}, 422)
        username = str(data.get('admin_username') or 'admin').strip()
        email = str(data.get('admin_email') or '').strip()
        alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#'
        password = str(data.get('admin_password') or ('Aa1!' + ''.join(secrets.choice(alphabet) for _ in range(12))))
        try:
            with db() as conn:
                if q(conn, "SELECT id FROM tenants WHERE code=?", (code,), one=True):
                    return self._json({'error': 'Tenant code already exists'}, 409)
                cur = conn.execute("INSERT INTO tenants(name,code,status,settings_json,created_at) VALUES (?,?,'active',?,?)",
                                   (name, code, json.dumps({'airline_name': name}), now_iso()))
                tenant_id = int(cur.lastrowid)
                plan = q(conn, "SELECT id FROM plans ORDER BY id LIMIT 1", one=True)
                if plan:
                    conn.execute("INSERT INTO subscriptions(tenant_id,plan_id,status,start_date,billing_cycle,mrr,created_at) VALUES (?,?,'active',?,'monthly',0,?)",
                                 (tenant_id, plan['id'], _today(), now_iso()))
                conn.execute("""INSERT INTO users(tenant_id,username,password,full_name,email,role,status,created_at)
                                VALUES (?,?,?,?,?,'admin','active',?)""",
                             (tenant_id, username, _hash_password(password), name, email, now_iso()))
                conn.execute("INSERT INTO onboarding_progress(tenant_id,step,data_json,completed,updated_at) VALUES (?,'profile',?,0,?)",
                             (tenant_id, json.dumps({'airline_name': name, 'code': code}), now_iso()))
                seed_pss(conn)
                log_audit(conn, current_tenant_id, user_id, 'tenant_onboarded', 'tenant', code, {'tenant_id': tenant_id})
        except Exception as exc:
            return self._json({'error': f'Could not create tenant: {exc}'}, 500)
        _webhook_event(current_tenant_id, 'tenant.onboarded', {'tenant_id': tenant_id, 'code': code, 'name': name}, code)
        return self._json({'tenant': {'id': tenant_id, 'code': code, 'name': name},
                           'admin': {'username': username, 'password': password},
                           'message': 'Save these credentials now; the password is not shown again.'}, 201)

    def api_disruption_impact(self, tenant_id, qs):
        disruption_id = (qs.get('disruption_id') or [''])[0]
        flight_id = (qs.get('flight_id') or [''])[0]
        flight_no = (qs.get('flight_no') or [''])[0]
        with db() as conn:
            disruption = None
            if disruption_id:
                disruption = q(conn, "SELECT * FROM disruptions WHERE id=? AND tenant_id=?", (disruption_id, tenant_id), one=True)
            where = ["b.tenant_id=?", "b.booking_status NOT IN ('cancelled','refunded')"]
            args = [tenant_id]
            if flight_id:
                where.append("b.flight_id=?")
                args.append(flight_id)
            elif flight_no:
                where.append("f.flight_no=?")
                args.append(flight_no)
            elif disruption and disruption.get('affected_pnr'):
                where.append("b.pnr=?")
                args.append(disruption['affected_pnr'])
            else:
                return self._json({'error': 'Provide flight_id, flight_no, or disruption_id with an affected PNR'}, 422)
            affected = q(conn, f"""SELECT b.*,f.flight_no,f.origin,f.destination,f.flight_date,f.dep_time,f.arr_time,f.status flight_status
                                  FROM pss_bookings b LEFT JOIN pss_flights f ON f.id=b.flight_id
                                  WHERE {' AND '.join(where)} ORDER BY b.id DESC LIMIT 300""", tuple(args))
            alternates = []
            if affected:
                first = affected[0]
                alternates = q(conn, """SELECT id,flight_no,origin,destination,flight_date,dep_time,arr_time,available_seats,base_fare,currency
                                        FROM pss_flights WHERE tenant_id=? AND origin=? AND destination=? AND id<>?
                                        AND available_seats>0 AND status NOT IN ('cancelled','closed')
                                        ORDER BY flight_date,dep_time LIMIT 30""",
                               (tenant_id, first.get('origin'), first.get('destination'), first.get('flight_id')))
            history = q(conn, "SELECT * FROM pss_rebookings WHERE tenant_id=? ORDER BY id DESC LIMIT 50", (tenant_id,))
        return self._json({'disruption': disruption, 'affected': affected, 'alternatives': alternates, 'history': history})

    def api_disruption_rebook(self, tenant_id, user_id):
        data = read_json(self)
        try:
            booking_id = int(data.get('booking_id') or 0)
            new_flight_id = int(data.get('new_flight_id') or 0)
        except Exception:
            return self._json({'error': 'booking_id and new_flight_id are required'}, 422)
        if not booking_id or not new_flight_id:
            return self._json({'error': 'booking_id and new_flight_id are required'}, 422)
        waiver = 1 if data.get('waiver', True) else 0
        try:
            fare_difference = float(data.get('fare_difference') or 0)
        except Exception:
            return self._json({'error': 'fare_difference must be a number'}, 422)
        with db() as conn:
            booking = q(conn, "SELECT * FROM pss_bookings WHERE id=? AND tenant_id=?", (booking_id, tenant_id), one=True)
            new_flight = q(conn, "SELECT * FROM pss_flights WHERE id=? AND tenant_id=?", (new_flight_id, tenant_id), one=True)
            if not booking or not new_flight:
                return self._json({'error': 'Booking or new flight not found'}, 404)
            if int(new_flight.get('available_seats') or 0) < 1:
                return self._json({'error': 'New flight has no available seats'}, 409)
            old_flight_id = booking.get('flight_id')
            cur = conn.execute("UPDATE pss_flights SET available_seats=available_seats-1,updated_at=? WHERE id=? AND tenant_id=? AND available_seats>0",
                               (now_iso(), new_flight_id, tenant_id))
            if cur.rowcount != 1:
                return self._json({'error': 'New flight has no available seats'}, 409)
            if old_flight_id:
                conn.execute("UPDATE pss_flights SET available_seats=available_seats+1,updated_at=? WHERE id=? AND tenant_id=?",
                             (now_iso(), old_flight_id, tenant_id))
            new_total = round(float(booking.get('total_amount') or 0) + (0 if waiver else fare_difference), 2)
            conn.execute("""UPDATE pss_bookings SET flight_id=?,total_amount=?,booking_status='rebooked',
                            ticket_status=CASE WHEN ticket_number IS NOT NULL THEN 'reissued' ELSE ticket_status END,updated_at=?
                            WHERE id=? AND tenant_id=?""", (new_flight_id, new_total, now_iso(), booking_id, tenant_id))
            cur = conn.execute("""INSERT INTO pss_rebookings
                (tenant_id,disruption_id,booking_id,old_flight_id,new_flight_id,reason,fare_difference,waiver,status,notify_sent,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'confirmed',0,?,?,?)""",
                (tenant_id, data.get('disruption_id') or None, booking_id, old_flight_id, new_flight_id,
                 str(data.get('reason') or 'involuntary schedule change'), fare_difference, waiver, user_id, now_iso(), now_iso()))
            rebooking_id = int(cur.lastrowid)
            if data.get('disruption_id'):
                conn.execute("UPDATE disruptions SET resolution=?,status='resolved',updated_at=? WHERE id=? AND tenant_id=?",
                             (f"Rebooked PNR {booking.get('pnr')} to {new_flight.get('flight_no')}", now_iso(), data['disruption_id'], tenant_id))
            channel = 'email' if booking.get('contact_email') else ('sms' if booking.get('contact_phone') else 'log')
            recipient = booking.get('contact_email') or booking.get('contact_phone') or booking.get('pnr')
            body = f"Your flight was rebooked to {new_flight.get('flight_no')} on {new_flight.get('flight_date')} at {new_flight.get('dep_time') or 'TBC'}. PNR {booking.get('pnr')}."
            conn.execute("""INSERT INTO pss_notifications_sent(tenant_id,channel,recipient,subject,body,status,created_at)
                            VALUES (?,?,?,?,?,'logged',?)""",
                         (tenant_id, channel, recipient, 'Flight rebooking confirmation', body, now_iso()))
            conn.execute("UPDATE pss_rebookings SET notify_sent=1 WHERE id=?", (rebooking_id,))
            self._trace(conn, tenant_id, 'disruption', 'booking', booking.get('pnr'), 'rebooked', actor=user_id,
                        detail=json.dumps({'old_flight_id': old_flight_id, 'new_flight_id': new_flight_id, 'waiver': bool(waiver)}), handler=self)
            new_flight_data = {'flight_no': new_flight.get('flight_no'), 'flight_date': new_flight.get('flight_date')}
        _webhook_event(tenant_id, 'rebooking.confirmed', {'rebooking_id': rebooking_id, 'pnr': booking.get('pnr'),
                                                          'booking_id': booking_id, 'new_flight': new_flight_data}, booking.get('pnr'))
        return self._json({'ok': True, 'rebooking_id': rebooking_id, 'pnr': booking.get('pnr'),
                           'new_flight': new_flight_data, 'notification': {'channel': channel, 'recipient': recipient}})

    def api_loyalty_report(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT c.id customer_id, c.loyalty_id, c.name customer, c.type, c.email,
                                     (SELECT COUNT(*) FROM coupons cp WHERE cp.tenant_id=c.tenant_id
                                       AND (cp.traveler_name=c.name OR (cp.pnr IS NOT NULL AND cp.pnr IN
                                       (SELECT pnr FROM orders o WHERE o.tenant_id=c.tenant_id AND o.customer_id=c.id)))) coupons,
                                     (SELECT COALESCE(SUM(cp.refund_amount),0) FROM coupons cp WHERE cp.tenant_id=c.tenant_id
                                       AND cp.traveler_name=c.name) refunded,
                                     (SELECT COALESCE(SUM(o.total_sell),0) FROM orders o
                                       WHERE o.tenant_id=c.tenant_id AND o.customer_id=c.id) spend
                              FROM customers c
                              WHERE c.tenant_id=? AND c.loyalty_id IS NOT NULL AND c.loyalty_id<>''
                              ORDER BY spend DESC""", (tenant_id,))
        for r in rows:
            r['coupon_value'] = r.get('refunded') or 0
        return self._json({
            'kpis': {'members': len(rows), 'coupons': sum(int(r.get('coupons') or 0) for r in rows),
                     'coupon_value': round(sum(float(r.get('refunded') or 0) for r in rows), 2),
                     'spend': round(sum(float(r.get('spend') or 0) for r in rows), 2)},
            'rows': rows})

    def api_partner_reports(self, tenant_id):
        with db() as conn:
            rows = q(conn, """SELECT a.id agency_id, a.name agency,
                                      (SELECT COUNT(*) FROM users u WHERE u.tenant_id=a.tenant_id
                                        AND u.agency_id=a.id AND u.role IN ('agency','agency_admin','agency_agent','station','checkin')) users,
                                     (SELECT COUNT(*) FROM pss_bookings b WHERE b.tenant_id=a.tenant_id
                                       AND b.agency_id=a.id) bookings,
                                      (SELECT COUNT(*) FROM pss_payments p JOIN pss_bookings b ON b.id=p.booking_id AND b.tenant_id=p.tenant_id
                                        WHERE p.tenant_id=a.tenant_id AND b.tenant_id=a.tenant_id AND b.agency_id=a.id) payments,
                                      (SELECT COALESCE(SUM(p.amount),0) FROM pss_payments p
                                        JOIN pss_bookings b ON b.id=p.booking_id AND b.tenant_id=p.tenant_id
                                        WHERE p.tenant_id=a.tenant_id AND b.tenant_id=a.tenant_id AND b.agency_id=a.id
                                          AND p.status IN ('paid','received','captured')) collected
                              FROM pss_agencies a WHERE a.tenant_id=? ORDER BY bookings DESC""", (tenant_id,))
        return self._json({
            'kpis': {'agencies': len(rows),
                     'users': sum(int(r.get('users') or 0) for r in rows),
                     'bookings': sum(int(r.get('bookings') or 0) for r in rows),
                     'collected': round(sum(float(r.get('collected') or 0) for r in rows), 2)},
            'rows': rows})


Handler = PartnerAPIHandler


def run():
    init_db()
    _ensure_portal_session_cols()
    _rotate_legacy_credentials()
    threading.Thread(target=_webhook_worker, daemon=True).start()
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '8000'))
    httpd = ThreadingHTTPServer((host, port), Handler)
    display_host = '127.0.0.1' if host in ('0.0.0.0', '::', '') else host
    print(f"Airline PSS & Travel ERP running on http://{display_host}:{port}")
    print(f"Workspace root for .AIR scan: {WORKSPACE_ROOT}")
    if _SECURE_COOKIES_ENV is not None and _SECURE_COOKIES_ENV not in ('0', 'false', 'no'):
        print("Session cookies use the Secure flag (set SECURE_COOKIES=0 for plain-http internal use)")
    elif _SECURE_COOKIES_ENV is None:
        print("Session cookies: Secure auto-detected (loopback plain HTTP allowed; set SECURE_COOKIES=1 to force)")
    _shutdown = {'flag': False}

    def _handle_sigterm(signum, frame):
        _shutdown['flag'] = True
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    run()
