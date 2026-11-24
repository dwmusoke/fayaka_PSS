import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, date

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.pool import ThreadedConnectionPool
except Exception:
    psycopg = None
    ThreadedConnectionPool = None

DB_PATH = os.environ.get("TRAVEL_ERP_DB", os.path.join(os.path.dirname(__file__), "data", "erp.db"))
_raw_db_url = (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or "").strip()
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = "postgresql://" + _raw_db_url[len("postgres://"):]
if _raw_db_url.count("postgresql://") > 1:
    _raw_db_url = _raw_db_url[:_raw_db_url.find("postgresql://", len("postgresql://"))].rstrip("/")
if _raw_db_url and "sslmode=" not in _raw_db_url and ("supabase.co" in _raw_db_url or "supabase.com" in _raw_db_url):
    _raw_db_url += ("&" if "?" in _raw_db_url else "?") + "sslmode=require"
DATABASE_URL = _raw_db_url
USING_POSTGRES = DATABASE_URL.startswith("postgresql://")
REQUIRE_POSTGRES = (os.environ.get("REQUIRE_POSTGRES") or "").strip().lower() in ("1", "true", "yes", "on") or bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))

PG_POOL = None


def _init_pg_pool():
    global PG_POOL
    if USING_POSTGRES and ThreadedConnectionPool is not None and PG_POOL is None:
        try:
            PG_POOL = ThreadedConnectionPool(minconn=2, maxconn=20, conninfo=DATABASE_URL)
        except Exception:
            PG_POOL = None


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, 210_000)
    return f"v2${salt.hex()}${dk.hex()}"


def _bootstrap_admin_password(conn):
    password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    if not password:
        return
    tenant = q(conn, "SELECT id FROM tenants WHERE code='demo'", one=True) or q(conn, "SELECT id FROM tenants ORDER BY id LIMIT 1", one=True)
    if not tenant:
        print("[bootstrap] ADMIN_PASSWORD set but no tenant found", file=sys.stderr)
        return
    admin = q(conn, "SELECT id FROM users WHERE tenant_id=? AND username='admin'", (tenant["id"],), one=True)
    hashed = _hash_password(password)
    if admin:
        conn.execute("UPDATE users SET password=?, status='active' WHERE id=?", (hashed, admin["id"]))
        print("[bootstrap] admin password updated from ADMIN_PASSWORD", file=sys.stderr)
        return
    branch = q(conn, "SELECT id FROM branches WHERE tenant_id=? ORDER BY id LIMIT 1", (tenant["id"],), one=True)
    conn.execute("""INSERT INTO users(tenant_id,branch_id,username,password,full_name,email,role,status,created_at)
                    VALUES (?,?,?,?,?,?,?,'active',?)""",
                 (tenant["id"], branch["id"] if branch else None, "admin", hashed,
                  "Demo Admin", "admin@demo.local", "admin", now_iso()))
    print("[bootstrap] admin user created from ADMIN_PASSWORD", file=sys.stderr)


def _pg_table_info(sql):
    m = re.search(r"PRAGMA\s+table_info\(([\w\"']+)\)", sql, re.IGNORECASE)
    if not m:
        return None, None
    table = m.group(1).strip().strip("\"'")
    return (
        "SELECT column_name AS name, udt_name AS type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    )


def _adapt_sql(sql, is_pg):
    if not is_pg or sql is None:
        return sql
    s = str(sql)
    # table/column introspection
    if re.search(r"PRAGMA\s+table_info", s, re.IGNORECASE):
        q, _ = _pg_table_info(s)
        return q or "SELECT NULL AS name, NULL AS type WHERE FALSE"
    if re.search(r"PRAGMA\s+foreign_keys", s, re.IGNORECASE):
        return "SELECT 1"
    # dialect conversions (DDL + DML)
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "SERIAL PRIMARY KEY", s, flags=re.IGNORECASE)
    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", s, flags=re.IGNORECASE)
    s = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", s, flags=re.IGNORECASE)
    s = re.sub(r"\bdate\(\s*'now'\s*\)", "CURRENT_DATE::text", s, flags=re.IGNORECASE)
    s = re.sub(r"\bdatetime\(\s*'now'\s*\)", "NOW()::text", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBLOB\b", "BYTEA", s)
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", str(sql), re.IGNORECASE):
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # placeholders: SQLite ? -> psycopg %s
    s = s.replace("?", "%s")
    return s


class _PGCursor:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def execute(self, sql, args=None):
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class _PGConn:
    def __init__(self, conn):
        self._conn = conn

    def _prepare(self, sql, args=None):
        raw = str(sql) if sql is not None else sql
        params = args
        if raw is not None and re.search(r"PRAGMA\s+table_info", raw, re.IGNORECASE):
            adapted, params = _pg_table_info(raw)
            return adapted, params or ()
        adapted = _adapt_sql(raw, True)
        if params is None:
            params = ()
        return adapted, params

    def _has_id_column(self, table):
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s AND column_name='id' LIMIT 1",
                (table.lower(),),
            )
            return cur.fetchone() is not None
        finally:
            cur.close()

    def execute(self, sql, args=None):
        adapted, params = self._prepare(sql, args)
        wants_id = False
        if isinstance(adapted, str):
            st = adapted.lstrip()
            m = re.match(r"INSERT\s+INTO\s+([A-Za-z_][\w]*)", st, re.IGNORECASE)
            if m and "RETURNING" not in st.upper() and self._has_id_column(m.group(1)):
                wants_id = True
        cur = self._conn.cursor()
        if wants_id:
            stmt = adapted.rstrip().rstrip(";") + " RETURNING id"
            try:
                cur.execute("SAVEPOINT _insret")
                cur.execute(stmt, params)
                wrapper = _PGCursor(cur)
                row = cur.fetchone()
                if row and "id" in row:
                    wrapper.lastrowid = row["id"]
                elif row:
                    wrapper.lastrowid = next(iter(row.values()))
                cur.execute("RELEASE SAVEPOINT _insret")
                return wrapper
            except Exception:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT _insret")
                    cur.execute("RELEASE SAVEPOINT _insret")
                except Exception:
                    pass
                cur = self._conn.cursor()
                cur.execute(adapted, params)
                return _PGCursor(cur)
        cur.execute(adapted, params)
        return _PGCursor(cur)

    def executemany(self, sql, seq_of_params):
        adapted = _adapt_sql(str(sql), True)
        cur = self._conn.cursor()
        cur.executemany(adapted, seq_of_params)
        return _PGCursor(cur)

    def executescript(self, sql_script):
        # naive split for simple DDL/scripts
        for stmt in str(sql_script).split(";"):
            s = stmt.strip()
            if not s:
                continue
            self.execute(s)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def cursor(self):
        return _PGCursor(self._conn.cursor())


class _SQLiteConn:
    """Thin wrapper so we can normalize behavior if needed."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, args=None):
        if args is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, args)

    def executemany(self, sql, seq_of_params):
        return self._conn.executemany(sql, seq_of_params)

    def executescript(self, sql_script):
        return self._conn.executescript(sql_script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()


def get_conn():
    if USING_POSTGRES:
        _init_pg_pool()
        if PG_POOL is not None:
            try:
                conn = PG_POOL.getconn()
                return _PGConn(conn)
            except Exception:
                PG_POOL = None
        conn = psycopg.connect(DATABASE_URL, autocommit=False, row_factory=dict_row)
        return _PGConn(conn)
    if REQUIRE_POSTGRES:
        raise RuntimeError(
            "DATABASE_URL/SUPABASE_DB_URL is required in Railway/production. "
            "Refusing SQLite fallback to prevent ephemeral data loss."
        )
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return _SQLiteConn(conn)


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  code TEXT UNIQUE NOT NULL,
  status TEXT DEFAULT 'active',
  plan_id INTEGER,
  settings_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  code TEXT UNIQUE NOT NULL,
  price_monthly REAL DEFAULT 0,
  max_users INTEGER DEFAULT 5,
  max_branches INTEGER DEFAULT 1,
  max_orders_per_month INTEGER DEFAULT 500,
  max_connectors INTEGER DEFAULT 2,
  features_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  plan_id INTEGER NOT NULL,
  status TEXT DEFAULT 'active',
  start_date TEXT,
  end_date TEXT,
  billing_cycle TEXT DEFAULT 'monthly',
  mrr REAL DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(plan_id) REFERENCES plans(id)
);
CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  code TEXT,
  city TEXT,
  country TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  username TEXT NOT NULL,
  password TEXT NOT NULL,
  full_name TEXT,
  email TEXT,
  role TEXT DEFAULT 'agent',
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, username),
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(branch_id) REFERENCES branches(id)
);
CREATE TABLE IF NOT EXISTS connectors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  config_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'active',
  last_sync_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS raw_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  connector_id INTEGER,
  source_type TEXT NOT NULL,
  source_ref TEXT,
  filename TEXT,
  content_text TEXT,
  content_hash TEXT,
  received_at TEXT NOT NULL,
  meta_json TEXT DEFAULT '{}',
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(connector_id) REFERENCES connectors(id)
);
CREATE TABLE IF NOT EXISTS import_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  connector_id INTEGER,
  source_type TEXT,
  status TEXT DEFAULT 'completed',
  total INTEGER DEFAULT 0,
  imported INTEGER DEFAULT 0,
  updated INTEGER DEFAULT 0,
  skipped INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0,
  log_json TEXT DEFAULT '[]',
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  type TEXT DEFAULT 'B2C',
  name TEXT NOT NULL,
  company TEXT,
  email TEXT,
  phone TEXT,
  loyalty_id TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS travelers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  customer_id INTEGER,
  full_name TEXT NOT NULL,
  first_name TEXT,
  last_name TEXT,
  title TEXT,
  traveler_type TEXT DEFAULT 'ADT',
  email TEXT,
  phone TEXT,
  doc_no TEXT,
  nationality TEXT,
  display_name TEXT,
  mobile TEXT,
  doc_type TEXT DEFAULT 'Passport',
  doc_issued TEXT,
  doc_expiry TEXT,
  dob TEXT,
  marital_status TEXT,
  gender TEXT,
  address TEXT,
  city TEXT,
  state TEXT,
  postal_code TEXT,
  country TEXT,
  notes TEXT,
  status TEXT DEFAULT 'active',
  smoking INTEGER DEFAULT 0,
  seat_pref TEXT,
  meal_pref TEXT,
  flight_request TEXT,
  hotel_bedding TEXT,
  hotel_request TEXT,
  car_size TEXT,
  car_request TEXT,
  preferred_airlines TEXT,
  preferred_hotels TEXT,
  preferred_car_companies TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  source_type TEXT NOT NULL,
  source_id TEXT,
  connector_id INTEGER,
  raw_document_id INTEGER,
  pnr TEXT,
  order_number TEXT,
  office_id TEXT,
  account_code TEXT,
  customer_id INTEGER,
  owner_user_id INTEGER,
  product_type TEXT DEFAULT 'Flight',
  status TEXT DEFAULT 'booked',
  ticket_status TEXT DEFAULT 'unticketed',
  tktl TEXT,
  booking_date TEXT,
  travel_start TEXT,
  travel_end TEXT,
  origin TEXT,
  destination TEXT,
  currency TEXT DEFAULT 'USD',
  total_sell REAL DEFAULT 0,
  total_cost REAL DEFAULT 0,
  margin REAL DEFAULT 0,
  balance_due REAL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, source_type, source_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(connector_id) REFERENCES connectors(id),
  FOREIGN KEY(raw_document_id) REFERENCES raw_documents(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(owner_user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS order_status_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  status TEXT,
  note TEXT,
  source TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  seg_ref TEXT,
  carrier TEXT,
  flight_no TEXT,
  class_code TEXT,
  cabin TEXT,
  origin TEXT,
  origin_name TEXT,
  destination TEXT,
  destination_name TEXT,
  depart_at TEXT,
  arrive_at TEXT,
  status_code TEXT,
  status_pax TEXT,
  equipment TEXT,
  baggage TEXT,
  eticket TEXT,
  is_void INTEGER DEFAULT 0,
  raw_json TEXT,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS order_passengers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  traveler_id INTEGER,
  pax_ref TEXT,
  full_name TEXT,
  traveler_type TEXT,
  phone TEXT,
  email TEXT,
  loyalty_id TEXT,
  ticket_numbers TEXT,
  raw_json TEXT,
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(traveler_id) REFERENCES travelers(id)
);
CREATE TABLE IF NOT EXISTS order_services (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  kind TEXT,
  code TEXT,
  text TEXT,
  raw_json TEXT,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER,
  type TEXT,
  title TEXT NOT NULL,
  description TEXT,
  assignee_user_id INTEGER,
  status TEXT DEFAULT 'open',
  priority TEXT DEFAULT 'medium',
  due_at TEXT,
  sla_status TEXT DEFAULT 'ok',
  created_at TEXT NOT NULL,
  closed_at TEXT,
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(assignee_user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  order_id INTEGER,
  customer_id INTEGER,
  invoice_no TEXT NOT NULL,
  kind TEXT DEFAULT 'invoice',
  status TEXT DEFAULT 'issued',
  issue_date TEXT,
  due_date TEXT,
  currency TEXT DEFAULT 'USD',
  subtotal REAL DEFAULT 0,
  tax REAL DEFAULT 0,
  fees REAL DEFAULT 0,
  discount REAL DEFAULT 0,
  total REAL DEFAULT 0,
  amount_paid REAL DEFAULT 0,
  balance REAL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, invoice_no),
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  invoice_id INTEGER NOT NULL,
  description TEXT,
  qty REAL DEFAULT 1,
  unit_cost REAL DEFAULT 0,
  unit_price REAL DEFAULT 0,
  tax REAL DEFAULT 0,
  line_total REAL DEFAULT 0,
  FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  invoice_id INTEGER,
  order_id INTEGER,
  customer_id INTEGER,
  payment_no TEXT,
  method TEXT,
  reference TEXT,
  amount REAL NOT NULL,
  currency TEXT DEFAULT 'USD',
  status TEXT DEFAULT 'received',
  paid_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(invoice_id) REFERENCES invoices(id),
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS allocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  payment_id INTEGER NOT NULL,
  invoice_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(payment_id) REFERENCES payments(id),
  FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS supplier_bills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  supplier_name TEXT,
  order_id INTEGER,
  bill_no TEXT,
  source_type TEXT,
  currency TEXT DEFAULT 'USD',
  amount REAL DEFAULT 0,
  status TEXT DEFAULT 'open',
  issue_date TEXT,
  due_date TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS commissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER,
  agent_user_id INTEGER,
  basis_amount REAL DEFAULT 0,
  rate REAL DEFAULT 0,
  amount REAL DEFAULT 0,
  status TEXT DEFAULT 'accrued',
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(agent_user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS journals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  ref_type TEXT,
  ref_id INTEGER,
  entry_date TEXT,
  memo TEXT,
  debit_account TEXT,
  credit_account TEXT,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER,
  user_id INTEGER,
  action TEXT,
  entity TEXT,
  entity_id INTEGER,
  detail_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders(tenant_id);
CREATE INDEX IF NOT EXISTS idx_orders_pnr ON orders(tenant_id, pnr);
CREATE INDEX IF NOT EXISTS idx_segments_order ON segments(order_id);
CREATE INDEX IF NOT EXISTS idx_pax_order ON order_passengers(order_id);
CREATE INDEX IF NOT EXISTS idx_inv_tenant ON invoices(tenant_id);
CREATE INDEX IF NOT EXISTS idx_pay_tenant ON payments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_raw_hash ON raw_documents(tenant_id, content_hash);

CREATE TABLE IF NOT EXISTS billing_gateways (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT DEFAULT 'platform',
  tenant_id INTEGER,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  public_key TEXT,
  secret_key TEXT,
  encryption_key TEXT,
  webhook_secret TEXT,
  config_json TEXT DEFAULT '{}',
  is_active INTEGER DEFAULT 1,
  mode TEXT DEFAULT 'test',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  subscription_id INTEGER,
  plan_id INTEGER,
  invoice_no TEXT NOT NULL,
  amount REAL NOT NULL,
  currency TEXT DEFAULT 'USD',
  billing_cycle TEXT DEFAULT 'monthly',
  period_start TEXT,
  period_end TEXT,
  status TEXT DEFAULT 'pending',
  gateway TEXT,
  gateway_ref TEXT,
  tx_ref TEXT,
  payment_link TEXT,
  meta_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  paid_at TEXT,
  UNIQUE(tenant_id, invoice_no)
);
CREATE TABLE IF NOT EXISTS subscription_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  subscription_invoice_id INTEGER,
  gateway TEXT,
  transaction_id TEXT,
  tx_ref TEXT,
  flw_ref TEXT,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  status TEXT DEFAULT 'initiated',
  payment_method TEXT,
  customer_phone TEXT,
  customer_email TEXT,
  network TEXT,
  raw_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_subinv_tenant ON subscription_invoices(tenant_id);
CREATE INDEX IF NOT EXISTS idx_subpay_inv ON subscription_payments(subscription_invoice_id);

CREATE TABLE IF NOT EXISTS chart_of_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  account_type TEXT NOT NULL,
  account_group TEXT,
  normal_balance TEXT DEFAULT 'debit',
  parent_id INTEGER,
  is_active INTEGER DEFAULT 1,
  description TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, code),
  FOREIGN KEY(parent_id) REFERENCES chart_of_accounts(id)
);
CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  bill_no TEXT NOT NULL,
  supplier TEXT NOT NULL,
  description TEXT,
  order_id INTEGER,
  account_code TEXT,
  category TEXT DEFAULT 'supplier',
  currency TEXT DEFAULT 'USD',
  subtotal REAL DEFAULT 0,
  tax REAL DEFAULT 0,
  total REAL DEFAULT 0,
  amount_paid REAL DEFAULT 0,
  balance REAL DEFAULT 0,
  bill_date TEXT,
  due_date TEXT,
  status TEXT DEFAULT 'open',
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, bill_no),
  FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS bill_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  bill_id INTEGER NOT NULL,
  payment_no TEXT NOT NULL,
  amount REAL NOT NULL,
  method TEXT,
  reference TEXT,
  paid_at TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(bill_id) REFERENCES bills(id)
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  expense_no TEXT NOT NULL,
  category TEXT DEFAULT 'operating',
  vendor TEXT,
  description TEXT,
  account_code TEXT,
  amount REAL NOT NULL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  expense_date TEXT,
  status TEXT DEFAULT 'posted',
  payment_method TEXT,
  reference TEXT,
  receipt_no TEXT,
  agency_id INTEGER,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, expense_no)
);
CREATE TABLE IF NOT EXISTS estimates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  estimate_no TEXT NOT NULL,
  customer_id INTEGER,
  title TEXT,
  description TEXT,
  currency TEXT DEFAULT 'USD',
  subtotal REAL DEFAULT 0,
  tax REAL DEFAULT 0,
  total REAL DEFAULT 0,
  status TEXT DEFAULT 'draft',
  valid_until TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, estimate_no),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS credit_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  credit_note_no TEXT NOT NULL,
  customer_id INTEGER,
  invoice_id INTEGER,
  reason TEXT,
  description TEXT,
  currency TEXT DEFAULT 'USD',
  amount REAL DEFAULT 0,
  status TEXT DEFAULT 'issued',
  issue_date TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, credit_note_no),
  FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  title TEXT NOT NULL,
  category TEXT DEFAULT 'general',
  file_name TEXT,
  mime_type TEXT,
  file_data BLOB,
  content_text TEXT,
  size INTEGER DEFAULT 0,
  visibility TEXT DEFAULT 'internal',
  uploaded_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  document_id INTEGER,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  token TEXT NOT NULL UNIQUE,
  recipient TEXT,
  permission TEXT DEFAULT 'view',
  status TEXT DEFAULT 'active',
  views INTEGER DEFAULT 0,
  expires_at TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  last_viewed_at TEXT,
  FOREIGN KEY(document_id) REFERENCES documents(id)
);
CREATE TABLE IF NOT EXISTS document_activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  entity_type TEXT,
  entity_id INTEGER,
  share_id INTEGER,
  action TEXT,
  detail TEXT,
  actor TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(share_id) REFERENCES document_shares(id)
);
CREATE TABLE IF NOT EXISTS tenant_branding (
  tenant_id INTEGER PRIMARY KEY,
  company_name TEXT,
  legal_name TEXT,
  location TEXT,
  address TEXT,
  phone TEXT,
  email TEXT,
  website TEXT,
  tax_id TEXT,
  logo_text TEXT,
  logo_data TEXT,
  primary_color TEXT DEFAULT '#206090',
  accent_color TEXT DEFAULT '#e02020',
  footer_note TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS agency_profile (
  tenant_id INTEGER PRIMARY KEY,
  company_name TEXT,
  legal_name TEXT,
  registration_no TEXT,
  tax_id TEXT,
  vat_no TEXT,
  address TEXT,
  city TEXT,
  state TEXT,
  postal_code TEXT,
  country TEXT,
  phone TEXT,
  email TEXT,
  website TEXT,
  default_currency TEXT DEFAULT 'USD',
  timezone TEXT DEFAULT 'UTC',
  fiscal_year_start TEXT DEFAULT '01-01',
  iata_code TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  category TEXT,
  product_type TEXT DEFAULT 'Misc',
  supplier TEXT,
  with_inventory INTEGER DEFAULT 0,
  unit TEXT DEFAULT 'service',
  sale_price REAL DEFAULT 0,
  cost_price REAL DEFAULT 0,
  tax_code TEXT,
  is_active INTEGER DEFAULT 1,
  is_core INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, sku)
);
CREATE TABLE IF NOT EXISTS currencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT,
  symbol TEXT,
  exchange_rate REAL DEFAULT 1,
  decimal_places INTEGER DEFAULT 2,
  is_base INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  contact_type TEXT DEFAULT 'supplier',
  tips TEXT,
  name TEXT NOT NULL,
  type TEXT,
  supplier_type TEXT,
  balance REAL DEFAULT 0,
  status TEXT DEFAULT 'Active',
  fcy INTEGER DEFAULT 0,
  company TEXT,
  email TEXT,
  phone TEXT,
  address TEXT,
  terms TEXT,
  currency TEXT,
  reconciliation_format TEXT,
  contact_person TEXT,
  is_core INTEGER DEFAULT 0,
  notes TEXT,
  is_active INTEGER DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  rule_key TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  description TEXT,
  value TEXT,
  value_type TEXT DEFAULT 'text',
  is_active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, rule_key)
);
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  preference_key TEXT NOT NULL,
  preference_value TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, preference_key)
);
CREATE TABLE IF NOT EXISTS mail_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  subject TEXT NOT NULL,
  body_html TEXT,
  is_active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS integrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  name TEXT NOT NULL,
  integration_type TEXT NOT NULL,
  config_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'disconnected',
  last_sync_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, provider)
);

CREATE TABLE IF NOT EXISTS partners (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  partner_type TEXT DEFAULT 'non_iata',
  iata_code TEXT,
  country TEXT,
  contact_person TEXT,
  email TEXT,
  phone TEXT,
  commission_rate REAL DEFAULT 0,
  status TEXT DEFAULT 'active',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unused_tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  ticket_no TEXT NOT NULL,
  pnr TEXT,
  traveler_name TEXT,
  airline TEXT,
  issue_date TEXT,
  travel_date TEXT,
  expiry_date TEXT,
  ticket_status TEXT DEFAULT 'unused',
  refund_status TEXT DEFAULT 'pending',
  value REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disruptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  disruption_type TEXT DEFAULT 'schedule_change',
  severity TEXT DEFAULT 'medium',
  affected_pnr TEXT,
  traveler_name TEXT,
  description TEXT,
  resolution TEXT,
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  user_id INTEGER,
  notification_type TEXT DEFAULT 'info',
  title TEXT NOT NULL,
  message TEXT,
  entity_type TEXT,
  entity_id INTEGER,
  is_read INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bills_tenant ON bills(tenant_id);
CREATE INDEX IF NOT EXISTS idx_expenses_tenant ON expenses(tenant_id);
CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  refund_no TEXT NOT NULL,
  order_id INTEGER,
  pnr TEXT,
  ticket_no TEXT,
  traveler_name TEXT,
  source TEXT DEFAULT 'manual',
  reason TEXT DEFAULT 'cancellation',
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  policy_check TEXT DEFAULT 'pending',
  auto_void INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',
  requested_at TEXT,
  processed_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, refund_no),
  FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS vouchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  voucher_no TEXT NOT NULL,
  voucher_type TEXT DEFAULT 'hotel',
  supplier TEXT,
  pnr TEXT,
  traveler_name TEXT,
  issue_date TEXT,
  service_date TEXT,
  confirmation_no TEXT,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  status TEXT DEFAULT 'issued',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, voucher_no)
);
CREATE TABLE IF NOT EXISTS coupons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  coupon_no TEXT NOT NULL,
  pnr TEXT,
  ticket_no TEXT,
  traveler_name TEXT,
  airline TEXT,
  issue_date TEXT,
  cancel_date TEXT,
  status TEXT DEFAULT 'active',
  cancel_reason TEXT,
  pre_billed INTEGER DEFAULT 0,
  refund_amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, coupon_no)
);
CREATE TABLE IF NOT EXISTS bank_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  account_name TEXT NOT NULL,
  account_type TEXT DEFAULT 'bank',
  account_no TEXT,
  bank_name TEXT,
  currency TEXT DEFAULT 'USD',
  opening_balance REAL DEFAULT 0,
  current_balance REAL DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, account_name)
);
CREATE TABLE IF NOT EXISTS bank_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  txn_no TEXT NOT NULL,
  account_id INTEGER,
  txn_date TEXT,
  description TEXT,
  txn_type TEXT DEFAULT 'receipt',
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  reference TEXT,
  reconciliation_status TEXT DEFAULT 'unmatched',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, txn_no),
  FOREIGN KEY(account_id) REFERENCES bank_accounts(id)
);
CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  recon_no TEXT NOT NULL,
  source TEXT DEFAULT 'bank',
  statement_ref TEXT,
  period TEXT,
  expected_amount REAL DEFAULT 0,
  actual_amount REAL DEFAULT 0,
  variance REAL DEFAULT 0,
  status TEXT DEFAULT 'open',
  resolution TEXT,
  resolved_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, recon_no)
);

CREATE INDEX IF NOT EXISTS idx_docs_entity ON documents(tenant_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_shares_token ON document_shares(token);
"""


SCHEMA_PSS = r"""
CREATE TABLE IF NOT EXISTS pss_airports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  icao TEXT,
  name TEXT NOT NULL,
  city TEXT,
  country TEXT,
  timezone TEXT,
  terminal TEXT,
  region TEXT,
  status TEXT DEFAULT 'active',
  is_hub INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS pss_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  channel_type TEXT DEFAULT 'web',
  commission_rate REAL DEFAULT 0,
  allowed_airports TEXT,
  currency TEXT DEFAULT 'USD',
  settlement_days INTEGER DEFAULT 7,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS pss_stock_pools (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  pool_no TEXT NOT NULL,
  airline_code TEXT NOT NULL,
  prefix TEXT NOT NULL,
  start_serial INTEGER NOT NULL,
  end_serial INTEGER NOT NULL,
  next_serial INTEGER NOT NULL,
  issued_count INTEGER DEFAULT 0,
  void_count INTEGER DEFAULT 0,
  location_type TEXT DEFAULT 'airport',
  location_code TEXT,
  status TEXT DEFAULT 'active',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, pool_no)
);
CREATE TABLE IF NOT EXISTS pss_stock_allocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  allocation_no TEXT NOT NULL,
  pool_id INTEGER NOT NULL,
  channel_id INTEGER,
  airport_code TEXT,
  quantity INTEGER NOT NULL,
  start_serial INTEGER,
  end_serial INTEGER,
  next_serial INTEGER,
  allocated_to TEXT,
  agency_id INTEGER,
  unit_price REAL DEFAULT 0,
  total_amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  wallet_tx_id INTEGER,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, allocation_no),
  FOREIGN KEY(pool_id) REFERENCES pss_stock_pools(id),
  FOREIGN KEY(channel_id) REFERENCES pss_channels(id)
);

CREATE TABLE IF NOT EXISTS pss_agency_wallets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  agency_id INTEGER NOT NULL,
  currency TEXT DEFAULT 'USD',
  balance REAL DEFAULT 0,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, agency_id, currency)
);

CREATE TABLE IF NOT EXISTS pss_wallet_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  agency_id INTEGER NOT NULL,
  wallet_id INTEGER,
  tx_no TEXT NOT NULL,
  tx_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  balance_before REAL DEFAULT 0,
  balance_after REAL DEFAULT 0,
  reference TEXT,
  gateway_ref TEXT,
  status TEXT DEFAULT 'pending',
  method TEXT,
  allocation_id INTEGER,
  notes TEXT,
  raw_json TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, tx_no)
);
CREATE TABLE IF NOT EXISTS pss_flights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flight_no TEXT NOT NULL,
  airline_code TEXT DEFAULT 'UG',
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  flight_date TEXT NOT NULL,
  dep_time TEXT,
  arr_time TEXT,
  aircraft TEXT,
  total_seats INTEGER DEFAULT 0,
  available_seats INTEGER DEFAULT 0,
  base_fare REAL DEFAULT 0,
  base_fare_reference REAL,
  fare_optimized_at TEXT,
  currency TEXT DEFAULT 'USD',
  gate TEXT,
  terminal TEXT,
  status TEXT DEFAULT 'scheduled',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, flight_no, flight_date)
);
CREATE TABLE IF NOT EXISTS pss_bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  pnr TEXT NOT NULL,
  flight_id INTEGER,
  passenger_name TEXT NOT NULL,
  passenger_type TEXT DEFAULT 'ADT',
  contact_email TEXT,
  contact_phone TEXT,
  channel_id INTEGER,
  airport_code TEXT,
  fare_class TEXT DEFAULT 'Y',
  fare_amount REAL DEFAULT 0,
  taxes REAL DEFAULT 0,
  total_amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  ticket_number TEXT,
  ticket_status TEXT DEFAULT 'unissued',
  payment_status TEXT DEFAULT 'pending',
  booking_status TEXT DEFAULT 'confirmed',
  hold_expires_at TEXT,
  seat_no TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, pnr),
  FOREIGN KEY(flight_id) REFERENCES pss_flights(id),
  FOREIGN KEY(channel_id) REFERENCES pss_channels(id)
);
CREATE TABLE IF NOT EXISTS pss_inventory_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flight_id INTEGER NOT NULL,
  leg_no INTEGER DEFAULT 1,
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  flight_date TEXT,
  dep_time TEXT,
  arr_time TEXT,
  aircraft TEXT,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, flight_id, leg_no)
);
CREATE TABLE IF NOT EXISTS pss_inventory_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  leg_id INTEGER NOT NULL,
  segment_no INTEGER DEFAULT 1,
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  dep_time TEXT,
  arr_time TEXT,
  cabin TEXT DEFAULT 'economy',
  subclass TEXT DEFAULT 'Y',
  seats INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  supplementary_info TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, leg_id, segment_no)
);
CREATE TABLE IF NOT EXISTS pss_inventory_availability (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flight_id INTEGER NOT NULL,
  leg_id INTEGER,
  segment_id INTEGER,
  pos TEXT NOT NULL,
  pos_subclass TEXT DEFAULT 'ALL',
  available INTEGER DEFAULT 0,
  capacity INTEGER DEFAULT 0,
  committed INTEGER DEFAULT 0,
  published_at TEXT,
  version INTEGER DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, flight_id, segment_id, pos, pos_subclass)
);
CREATE TABLE IF NOT EXISTS pss_inventory_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  rule_code TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  rule_type TEXT DEFAULT 'limit',
  pos TEXT DEFAULT 'ALL',
  subclass TEXT DEFAULT 'ALL',
  value REAL DEFAULT 0,
  tia_enabled INTEGER DEFAULT 0,
  tia_percent REAL DEFAULT 0,
  status TEXT DEFAULT 'active',
  effective_from TEXT,
  effective_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, rule_code)
);
CREATE TABLE IF NOT EXISTS pss_inventory_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT,
  action TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  actor TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pss_inventory_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  issue_type TEXT NOT NULL,
  severity TEXT DEFAULT 'warning',
  flight_id INTEGER,
  message TEXT,
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS pss_inventory_valuation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flight_id INTEGER NOT NULL,
  segment_id INTEGER,
  pos TEXT DEFAULT 'ALL',
  subclass TEXT DEFAULT 'ALL',
  valuation REAL DEFAULT 0,
  active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, flight_id, segment_id, pos, subclass)
);
CREATE TABLE IF NOT EXISTS pss_inventory_market (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  market TEXT NOT NULL,
  pos TEXT DEFAULT 'ALL',
  origin TEXT,
  destination TEXT,
  flight_date TEXT,
  demand INTEGER DEFAULT 0,
  bookings INTEGER DEFAULT 0,
  revenue REAL DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, market, pos, flight_date)
);
CREATE TABLE IF NOT EXISTS pss_api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  token_prefix TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  scopes TEXT DEFAULT 'offers:write,orders:write',
  status TEXT DEFAULT 'active',
  expires_at TEXT,
  last_used_at TEXT,
  created_by INTEGER,
  revoked_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, name)
);
CREATE TABLE IF NOT EXISTS pss_webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  events TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, name)
);
CREATE TABLE IF NOT EXISTS pss_webhook_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  webhook_id INTEGER NOT NULL,
  event TEXT NOT NULL,
  object_id TEXT,
  payload TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  response_code INTEGER,
  response_body TEXT,
  next_retry_at TEXT,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS pss_offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  offer_no TEXT NOT NULL,
  token TEXT NOT NULL,
  api_key_id INTEGER,
  channel TEXT DEFAULT 'api',
  pos TEXT DEFAULT 'DIRECT',
  currency TEXT DEFAULT 'USD',
  passenger_count INTEGER DEFAULT 1,
  segments_json TEXT NOT NULL,
  pricing_json TEXT NOT NULL,
  total_amount REAL DEFAULT 0,
  status TEXT DEFAULT 'active',
  expires_at TEXT,
  order_id INTEGER,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, offer_no),
  UNIQUE(token)
);
CREATE TABLE IF NOT EXISTS pss_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_no TEXT NOT NULL,
  offer_id INTEGER,
  status TEXT DEFAULT 'confirmed',
  pnr TEXT,
  passenger_count INTEGER DEFAULT 1,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  contact_json TEXT DEFAULT '{}',
  booking_ids_json TEXT DEFAULT '[]',
  payment_status TEXT DEFAULT 'pending',
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, order_no)
);
CREATE TABLE IF NOT EXISTS pss_rebookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  disruption_id INTEGER,
  booking_id INTEGER NOT NULL,
  old_flight_id INTEGER,
  new_flight_id INTEGER NOT NULL,
  reason TEXT,
  fare_difference REAL DEFAULT 0,
  waiver INTEGER DEFAULT 1,
  status TEXT DEFAULT 'confirmed',
  notify_sent INTEGER DEFAULT 0,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pss_notifications_sent (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  channel TEXT NOT NULL,
  recipient TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT DEFAULT 'logged',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onboarding_progress (
  tenant_id INTEGER PRIMARY KEY,
  step TEXT DEFAULT 'profile',
  data_json TEXT DEFAULT '{}',
  completed INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pss_checkins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  checkin_no TEXT NOT NULL,
  booking_id INTEGER NOT NULL,
  pnr TEXT,
  flight_id INTEGER,
  passenger_name TEXT,
  seat_no TEXT,
  bag_count INTEGER DEFAULT 0,
  bag_tags TEXT,
  checkin_method TEXT DEFAULT 'counter',
  counter TEXT,
  agent TEXT,
  status TEXT DEFAULT 'checked_in',
  checked_in_at TEXT,
  gate_closed_at TEXT,
  boarding_status TEXT DEFAULT 'not_boarded',
  boarded_at TEXT,
  arrival_status TEXT,
  carousel TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, checkin_no),
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);
CREATE TABLE IF NOT EXISTS pss_baggage_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  checkin_id INTEGER NOT NULL,
  booking_id INTEGER,
  pnr TEXT,
  bag_tag TEXT NOT NULL,
  sequence_no INTEGER DEFAULT 0,
  weight_kg REAL DEFAULT 0,
  status TEXT DEFAULT 'tagged',
  location TEXT,
  sale_no TEXT,
  flight_no TEXT,
  destination TEXT,
  carousel TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, bag_tag),
  FOREIGN KEY(checkin_id) REFERENCES pss_checkins(id)
);
CREATE TABLE IF NOT EXISTS pss_baggage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  baggage_id INTEGER NOT NULL,
  checkin_id INTEGER,
  event_type TEXT NOT NULL,
  location TEXT,
  actor TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(baggage_id) REFERENCES pss_baggage_items(id)
);
CREATE TABLE IF NOT EXISTS pss_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  payment_no TEXT NOT NULL,
  booking_id INTEGER,
  pnr TEXT,
  channel_id INTEGER,
  amount REAL NOT NULL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  method TEXT DEFAULT 'cash',
  reference TEXT,
  gateway_ref TEXT,
  status TEXT DEFAULT 'pending',
  paid_at TEXT,
  captured_at TEXT,
  notes TEXT,
  raw_json TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, payment_no),
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id),
  FOREIGN KEY(channel_id) REFERENCES pss_channels(id)
);
CREATE INDEX IF NOT EXISTS idx_pss_flights_route ON pss_flights(tenant_id, flight_date, origin, destination);
CREATE INDEX IF NOT EXISTS idx_pss_bookings_pnr ON pss_bookings(tenant_id, pnr);
CREATE INDEX IF NOT EXISTS idx_pss_checkins_booking ON pss_checkins(tenant_id, booking_id);
CREATE INDEX IF NOT EXISTS idx_pss_payments_booking ON pss_payments(tenant_id, booking_id);

CREATE TABLE IF NOT EXISTS pss_agencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  contact_email TEXT,
  contact_phone TEXT,
  commission_rate REAL DEFAULT 0,
  allowed_channels TEXT,
  status TEXT DEFAULT 'active',
  login_token TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS pss_eticket_directs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  offer_no TEXT NOT NULL,
  agency_id INTEGER NOT NULL,
  flight_id INTEGER,
  fare_amount REAL DEFAULT 0,
  taxes REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  quantity INTEGER DEFAULT 1,
  used_count INTEGER DEFAULT 0,
  valid_until TEXT,
  status TEXT DEFAULT 'active',
  token TEXT,
  notes TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, offer_no),
  FOREIGN KEY(agency_id) REFERENCES pss_agencies(id),
  FOREIGN KEY(flight_id) REFERENCES pss_flights(id)
);
CREATE TABLE IF NOT EXISTS pss_trace (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  trace_id TEXT NOT NULL,
  source TEXT DEFAULT 'counter',
  entity_type TEXT,
  entity_id INTEGER,
  action TEXT,
  actor TEXT,
  detail TEXT,
  ip TEXT,
  user_agent TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pss_charter_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  request_no TEXT NOT NULL,
  client_name TEXT NOT NULL,
  contact_email TEXT,
  contact_phone TEXT,
  origin TEXT,
  destination TEXT,
  flight_date TEXT,
  pax_count INTEGER DEFAULT 0,
  aircraft_type TEXT,
  requirements TEXT,
  status TEXT DEFAULT 'new',
  quoted_amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  assigned_to TEXT,
  notes TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, request_no)
);
CREATE TABLE IF NOT EXISTS pss_charter_flights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  charter_no TEXT NOT NULL,
  request_id INTEGER,
  flight_no TEXT,
  origin TEXT,
  destination TEXT,
  flight_date TEXT,
  dep_time TEXT,
  arr_time TEXT,
  aircraft TEXT,
  seats INTEGER DEFAULT 0,
  charterer TEXT,
  contract_no TEXT,
  total_amount REAL DEFAULT 0,
  amount_paid REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  pss_flight_id INTEGER,
  status TEXT DEFAULT 'scheduled',
  notes TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, charter_no),
  FOREIGN KEY(request_id) REFERENCES pss_charter_requests(id)
);
CREATE TABLE IF NOT EXISTS pss_charter_passengers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  charter_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  doc_no TEXT,
  seat_no TEXT,
  meal_pref TEXT,
  ticket_number TEXT,
  checkin_status TEXT DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(charter_id) REFERENCES pss_charter_flights(id)
);
CREATE INDEX IF NOT EXISTS idx_pss_trace_trace ON pss_trace(tenant_id, trace_id);
CREATE INDEX IF NOT EXISTS idx_pss_agency_token ON pss_agencies(tenant_id, login_token);
CREATE INDEX IF NOT EXISTS idx_pss_direct_token ON pss_eticket_directs(tenant_id, token);
CREATE INDEX IF NOT EXISTS idx_pss_charter_request ON pss_charter_requests(tenant_id, status);

CREATE TABLE IF NOT EXISTS pss_ancillaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT DEFAULT 'other',
  description TEXT,
  price REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  unit TEXT DEFAULT 'per passenger',
  capacity INTEGER DEFAULT 0,
  flight_id INTEGER,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS pss_ancillary_sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  sale_no TEXT NOT NULL,
  booking_id INTEGER NOT NULL,
  ancillary_id INTEGER,
  description TEXT,
  quantity INTEGER DEFAULT 1,
  unit_price REAL DEFAULT 0,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  status TEXT DEFAULT 'sold',
  payment_status TEXT DEFAULT 'pending',
  trace_id TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, sale_no),
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);
CREATE TABLE IF NOT EXISTS pss_baggage_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flight_id INTEGER,
  cabin_class TEXT DEFAULT 'Y',
  free_kg REAL DEFAULT 23,
  free_pieces INTEGER DEFAULT 1,
  excess_rate_per_kg REAL DEFAULT 12,
  max_kg REAL DEFAULT 90,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pss_airport_docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  airport_code TEXT NOT NULL,
  doc_type TEXT DEFAULT 'passport',
  title TEXT NOT NULL,
  description TEXT,
  requirement TEXT,
  applies_to TEXT DEFAULT 'all',
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pss_weather (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  airport_code TEXT NOT NULL,
  condition TEXT,
  temperature_c REAL DEFAULT 0,
  wind_kph REAL DEFAULT 0,
  visibility_km REAL DEFAULT 10,
  advisory TEXT,
  observed_at TEXT,
  status TEXT DEFAULT 'current',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portal_customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  full_name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT,
  password_hash TEXT NOT NULL,
  doc_no TEXT,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, email)
);
CREATE TABLE IF NOT EXISTS portal_sessions (
  token TEXT PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  customer_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT
);

CREATE TABLE IF NOT EXISTS pss_tax_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  booking_id INTEGER NOT NULL,
  tax_code TEXT NOT NULL,
  description TEXT,
  amount REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  created_at TEXT NOT NULL,
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);
CREATE TABLE IF NOT EXISTS pss_document_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  document_type TEXT NOT NULL,
  document_no TEXT,
  booking_id INTEGER,
  pnr TEXT,
  action TEXT,
  actor TEXT,
  trace_id TEXT,
  details_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pss_travel_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  booking_id INTEGER,
  pnr TEXT,
  doc_type TEXT DEFAULT 'passport',
  doc_no TEXT,
  title TEXT,
  file_name TEXT,
  mime_type TEXT,
  file_data BLOB,
  notes TEXT,
  uploaded_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);

CREATE TABLE IF NOT EXISTS pss_ticket_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  booking_id INTEGER NOT NULL,
  pnr TEXT,
  previous_status TEXT,
  ticket_status TEXT NOT NULL,
  reason TEXT,
  actor TEXT,
  trace_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);
CREATE TABLE IF NOT EXISTS pss_service_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  case_no TEXT NOT NULL,
  booking_id INTEGER,
  passenger_name TEXT,
  channel TEXT,
  case_type TEXT DEFAULT 'feedback',
  subject TEXT,
  description TEXT,
  priority TEXT DEFAULT 'medium',
  status TEXT DEFAULT 'open',
  satisfaction_score INTEGER,
  resolution TEXT,
  assigned_to TEXT,
  resolved_at TEXT,
  trace_id TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, case_no)
);
CREATE INDEX IF NOT EXISTS idx_pss_anc_sale_booking ON pss_ancillary_sales(tenant_id, booking_id);
CREATE INDEX IF NOT EXISTS idx_pss_docs_airport ON pss_airport_docs(tenant_id, airport_code);
CREATE INDEX IF NOT EXISTS idx_pss_weather_airport ON pss_weather(tenant_id, airport_code);
CREATE INDEX IF NOT EXISTS idx_pss_cases_status ON pss_service_cases(tenant_id, status);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  data TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_user ON sessions(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);



"""

SEED_RUN_KEY = "seed_ran"


def db_set_session(conn, sid, tenant_id, user_id, data, expires_at, created_at, updated_at):
    conn.execute(
        """INSERT INTO sessions(id, tenant_id, user_id, data, expires_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id, user_id=EXCLUDED.user_id,
             data=EXCLUDED.data, expires_at=EXCLUDED.expires_at, created_at=EXCLUDED.created_at,
             updated_at=EXCLUDED.updated_at""",
        (sid, tenant_id, user_id, data, expires_at, created_at, updated_at),
    )


def db_get_session(conn, sid):
    row = q(conn, "SELECT * FROM sessions WHERE id=? AND expires_at > ?", (sid, time.time()), one=True)
    return row


def db_delete_session(conn, sid):
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))


def db_delete_user_sessions(conn, user_id):
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def db_cleanup_expired_sessions(conn):
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        ensure_billing(conn)
        ensure_accounting(conn)
        ensure_setup(conn)
        ensure_travelers(conn)
        ensure_pss(conn)
        ensure_erp_pss_link(conn)
        already = q(conn, "SELECT 1 FROM preferences WHERE key=? LIMIT 1", (SEED_RUN_KEY,), one=True)
        if not already:
            seed(conn)
            seed_accounting(conn)
            seed_setup(conn)
            seed_pss(conn)
            _bootstrap_admin_password(conn)
            conn.execute("INSERT INTO preferences(key, value, created_at, updated_at) VALUES (?,?,?,?)",
                         (SEED_RUN_KEY, '1', now_iso(), now_iso()))
        else:
            _bootstrap_admin_password(conn)


def _add_columns(conn, table, cols):
    try:
        existing = {r['name'] for r in q(conn, f"PRAGMA table_info({table})")}
    except Exception:
        return
    for col, ddl in cols:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except Exception:
                pass


def ensure_erp_pss_link(conn):
    # Cross-channel ERP/PSS linkage and agency scoping for finance & CRM
    _add_columns(conn, 'customers', [
        ('agency_id', 'INTEGER'),
        ('portal_customer_id', 'INTEGER'),
        ('source', 'TEXT'),
        ('source_ref', 'TEXT'),
    ])
    _add_columns(conn, 'orders', [
        ('agency_id', 'INTEGER'),
        ('pss_booking_id', 'INTEGER'),
        ('booking_source', 'TEXT'),
        ('trace_id', 'TEXT'),
    ])
    _add_columns(conn, 'invoices', [
        ('agency_id', 'INTEGER'),
        ('pss_booking_id', 'INTEGER'),
        ('source', 'TEXT'),
    ])
    _add_columns(conn, 'payments', [
        ('agency_id', 'INTEGER'),
        ('pss_payment_id', 'INTEGER'),
        ('pss_booking_id', 'INTEGER'),
        ('source', 'TEXT'),
    ])
    _add_columns(conn, 'journals', [
        ('agency_id', 'INTEGER'),
        ('pss_booking_id', 'INTEGER'),
        ('pss_payment_id', 'INTEGER'),
        ('source', 'TEXT'),
    ])
    _add_columns(conn, 'estimates', [('agency_id', 'INTEGER')])
    _add_columns(conn, 'credit_notes', [('agency_id', 'INTEGER')])
    _add_columns(conn, 'bills', [('agency_id', 'INTEGER')])
    _add_columns(conn, 'expenses', [('agency_id', 'INTEGER'), ('receipt_no', 'TEXT')])
    _add_columns(conn, 'pss_bookings', [
        ('customer_id', 'INTEGER'),
        ('order_id', 'INTEGER'),
        ('invoice_id', 'INTEGER'),
    ])
    _add_columns(conn, 'pss_checkins', [
        ('boarding_status', "TEXT DEFAULT 'not_boarded'"),
        ('boarded_at', 'TEXT'),
        ('arrival_status', 'TEXT'),
        ('carousel', 'TEXT'),
    ])
    _add_columns(conn, 'pss_payments', [
        ('customer_id', 'INTEGER'),
        ('erp_payment_id', 'INTEGER'),
        ('invoice_id', 'INTEGER'),
        ('raw_json', 'TEXT'),
    ])
    try:
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_customers_agency ON customers(tenant_id, agency_id);
        CREATE INDEX IF NOT EXISTS idx_orders_pss_booking ON orders(tenant_id, pss_booking_id);
        CREATE INDEX IF NOT EXISTS idx_invoices_pss_booking ON invoices(tenant_id, pss_booking_id);
        CREATE INDEX IF NOT EXISTS idx_payments_pss ON payments(tenant_id, pss_payment_id);
        CREATE INDEX IF NOT EXISTS idx_journals_pss ON journals(tenant_id, pss_booking_id);
        CREATE INDEX IF NOT EXISTS idx_pss_bookings_links ON pss_bookings(tenant_id, customer_id, order_id, invoice_id);
        CREATE INDEX IF NOT EXISTS idx_pss_baggage_checkin ON pss_baggage_items(tenant_id, checkin_id);
        CREATE INDEX IF NOT EXISTS idx_pss_baggage_tag ON pss_baggage_items(tenant_id, bag_tag);
        CREATE INDEX IF NOT EXISTS idx_pss_baggage_events ON pss_baggage_events(tenant_id, baggage_id, created_at);
        """)
    except Exception:
        pass


def ensure_travelers(conn):
    traveler_cols = [r['name'] for r in q(conn, "PRAGMA table_info(travelers)")]
    for col, ddl in [
        ('display_name', 'TEXT'), ('mobile', 'TEXT'), ('doc_type', "TEXT DEFAULT 'Passport'"),
        ('doc_issued', 'TEXT'), ('doc_expiry', 'TEXT'), ('dob', 'TEXT'), ('marital_status', 'TEXT'),
        ('gender', 'TEXT'), ('address', 'TEXT'), ('city', 'TEXT'), ('state', 'TEXT'),
        ('postal_code', 'TEXT'), ('country', 'TEXT'), ('notes', 'TEXT'), ('status', "TEXT DEFAULT 'active'"),
        ('smoking', 'INTEGER DEFAULT 0'), ('seat_pref', 'TEXT'), ('meal_pref', 'TEXT'),
        ('flight_request', 'TEXT'), ('hotel_bedding', 'TEXT'), ('hotel_request', 'TEXT'),
        ('car_size', 'TEXT'), ('car_request', 'TEXT'), ('preferred_airlines', 'TEXT'),
        ('preferred_hotels', 'TEXT'), ('preferred_car_companies', 'TEXT'),
    ]:
        if col not in traveler_cols:
            conn.execute(f"ALTER TABLE travelers ADD COLUMN {col} {ddl}")
    order_cols = [r['name'] for r in q(conn, "PRAGMA table_info(orders)")]
    if 'product_type' not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN product_type TEXT DEFAULT 'Flight'")
    conn.execute("UPDATE travelers SET display_name=full_name WHERE display_name IS NULL OR display_name=''")
    conn.execute("UPDATE travelers SET status='active' WHERE status IS NULL OR status=''")
    conn.execute("UPDATE orders SET product_type='Flight' WHERE product_type IS NULL OR product_type=''")


def ensure_billing(conn):
    ts = now_iso()
    # ensure billing tables exist for pre-existing DBs
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS billing_gateways (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scope TEXT DEFAULT 'platform',
      tenant_id INTEGER,
      name TEXT NOT NULL,
      provider TEXT NOT NULL,
      public_key TEXT,
      secret_key TEXT,
      encryption_key TEXT,
      webhook_secret TEXT,
      config_json TEXT DEFAULT '{}',
      is_active INTEGER DEFAULT 1,
      mode TEXT DEFAULT 'test',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS subscription_invoices (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      subscription_id INTEGER,
      plan_id INTEGER,
      invoice_no TEXT NOT NULL,
      amount REAL NOT NULL,
      currency TEXT DEFAULT 'USD',
      billing_cycle TEXT DEFAULT 'monthly',
      period_start TEXT,
      period_end TEXT,
      status TEXT DEFAULT 'pending',
      gateway TEXT,
      gateway_ref TEXT,
      tx_ref TEXT,
      payment_link TEXT,
      meta_json TEXT DEFAULT '{}',
      created_at TEXT NOT NULL,
      paid_at TEXT,
      UNIQUE(tenant_id, invoice_no)
    );
    CREATE TABLE IF NOT EXISTS subscription_payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      subscription_invoice_id INTEGER,
      gateway TEXT,
      transaction_id TEXT,
      tx_ref TEXT,
      flw_ref TEXT,
      amount REAL DEFAULT 0,
      currency TEXT DEFAULT 'USD',
      status TEXT DEFAULT 'initiated',
      payment_method TEXT,
      customer_phone TEXT,
      customer_email TEXT,
      network TEXT,
      raw_json TEXT DEFAULT '{}',
      created_at TEXT NOT NULL,
      verified_at TEXT
    );
    """)
    row = q(conn, "SELECT id FROM billing_gateways WHERE provider='flutterwave' AND scope='platform' LIMIT 1", one=True)
    if not row:
        cfg = {"methods": ["mobilemoney", "card", "banktransfer", "ussd"], "primary": "mobilemoney", "currency": "UGX", "country": "UG", "redirect_url": "http://127.0.0.1:8000/"}
        conn.execute("""INSERT INTO billing_gateways(scope,tenant_id,name,provider,public_key,secret_key,encryption_key,webhook_secret,config_json,is_active,mode,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     ('platform', None, 'Flutterwave', 'flutterwave', '', '', '', '', json.dumps(cfg), 0, 'test', ts, ts))


ACCOUNTS = [
    ("1010", "Accounts Payable", "liability", "Other Current Liability", "credit", "Supplier and agency payables", 0),
    ("1020", "Accounts Receivable", "asset", "Other Current Asset", "debit", "Customer and agent receivables", 0),
    ("1030", "Advance Tax", "asset", "Other Current Asset", "debit", "Advance tax payments", 0),
    ("1040", "Advertising & Marketing", "expense", "Expense", "debit", "Marketing and promotional costs", 0),
    ("1050", "Automobile Expense", "expense", "Expense", "debit", "Vehicle and fuel costs", 0),
    ("1060", "Bad Debt", "expense", "Expense", "debit", "Uncollectible receivables", 0),
    ("1070", "Bank Fees & Charges", "expense", "Expense", "debit", "Bank transaction charges", 0),
    ("1080", "Commission", "income", "Income", "credit", "Commission revenue", 0),
    ("1090", "Consultant Expense", "expense", "Expense", "debit", "Consultancy fees", 0),
    ("1100", "Credit Card Charges", "expense", "Expense", "debit", "Card processing charges", 0),
    ("1110", "Depreciation Expense", "expense", "Expense", "debit", "Asset depreciation", 0),
    ("1120", "Discount", "income", "Income", "credit", "Discounts earned", 0),
    ("1130", "Drawings", "equity", "Equity", "debit", "Owner drawings", 0),
    ("1140", "Exchange Gain or Loss", "expense", "Other Expense", "debit", "Foreign exchange differences", 0),
    ("1150", "Expense Account", "expense", "Expense", "debit", "General expense fallback", 0),
    ("1160", "Fees", "income", "Income", "credit", "Service fees", 0),
    ("1170", "Furniture & Equipment", "asset", "Fixed Asset", "debit", "Office furniture and equipment", 0),
    ("1180", "General Income", "income", "Income", "credit", "Other operating income", 0),
    ("1190", "Income Account", "income", "Income", "credit", "General income fallback", 0),
    ("1200", "Interest Income", "income", "Income", "credit", "Interest earned", 0),
    ("1210", "IT & Internet Expense", "expense", "Expense", "debit", "IT and connectivity costs", 0),
    ("1220", "Janitorial Expense", "expense", "Expense", "debit", "Cleaning and janitorial costs", 0),
    ("1230", "Late Fee Income", "income", "Income", "credit", "Late payment fees", 0),
    ("1240", "Markup", "income", "Income", "credit", "Markup on travel sales", 0),
    ("1250", "Meals & Entertainment", "expense", "Expense", "debit", "Meals and client entertainment", 0),
    ("1260", "Office Supplies", "expense", "Expense", "debit", "Office stationery and supplies", 0),
    ("1270", "Opening Balance Adjustments", "liability", "Other Current Liability", "credit", "Opening balance control", 0),
    ("1280", "Opening Balance Offset", "equity", "Equity", "credit", "Opening balance offset", 0),
    ("1290", "Other Charges", "income", "Income", "credit", "Other customer charges", 0),
    ("1300", "Other Expenses", "expense", "Expense", "debit", "Miscellaneous expenses", 0),
    ("1310", "Other Payable", "liability", "Other Current Liability", "credit", "Other payables", 0),
    ("1320", "Other Receivable", "asset", "Other Current Asset", "debit", "Other receivables", 0),
    ("1330", "Owner's Equity", "equity", "Equity", "credit", "Owner capital", 0),
    ("1340", "Petty Cash", "asset", "Cash", "debit", "Petty cash float", 0),
    ("1350", "Postage", "expense", "Expense", "debit", "Postage and courier", 0),
    ("1360", "Printing & Stationary", "expense", "Expense", "debit", "Printing and stationery", 0),
    ("1370", "Remittance", "income", "Income", "credit", "Remittance income", 0),
    ("1380", "Rent Expense", "expense", "Expense", "debit", "Office rent", 0),
    ("1390", "Repairs & Maintenance", "expense", "Expense", "debit", "Repairs and maintenance", 0),
    ("1400", "Retained Earnings", "equity", "Equity", "credit", "Retained profits", 0),
    ("1410", "Salaries & Employees Wages", "expense", "Expense", "debit", "Employee compensation", 0),
    ("1420", "Sales", "income", "Income", "credit", "Travel sales revenue", 0),
    ("1430", "Shipping Charge", "income", "Income", "credit", "Shipping charges", 0),
    ("1440", "Target Account", "income", "Income", "credit", "Target / control account", 0),
    ("1450", "Tax Payable", "liability", "Other Current Liability", "credit", "Tax liabilities", 0),
    ("1460", "Telephone Expense", "expense", "Expense", "debit", "Telephone costs", 0),
    ("1470", "Travel Expense", "expense", "Expense", "debit", "Staff travel costs", 0),
    ("1480", "Undeposited Funds", "asset", "Cash", "debit", "Cash received not yet banked", 0),
    ("1490", "Unearned Revenue", "liability", "Other Current Liability", "credit", "Deferred revenue", 0),
]


def ensure_accounting(conn):
    ts = now_iso()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chart_of_accounts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      code TEXT NOT NULL,
      name TEXT NOT NULL,
      account_type TEXT NOT NULL,
      account_group TEXT,
      normal_balance TEXT DEFAULT 'debit',
      parent_id INTEGER,
      is_active INTEGER DEFAULT 1,
      description TEXT,
      tips TEXT,
      opening_balance REAL DEFAULT 0,
      fcy INTEGER DEFAULT 0,
      created_at TEXT NOT NULL,
      UNIQUE(tenant_id, code)
    );
    CREATE TABLE IF NOT EXISTS bills (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      branch_id INTEGER,
      bill_no TEXT NOT NULL,
      supplier TEXT NOT NULL,
      description TEXT,
      order_id INTEGER,
      account_code TEXT,
      category TEXT DEFAULT 'supplier',
      currency TEXT DEFAULT 'USD',
      subtotal REAL DEFAULT 0,
      tax REAL DEFAULT 0,
      total REAL DEFAULT 0,
      amount_paid REAL DEFAULT 0,
      balance REAL DEFAULT 0,
      bill_date TEXT,
      due_date TEXT,
      status TEXT DEFAULT 'open',
      created_by INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(tenant_id, bill_no)
    );
    CREATE TABLE IF NOT EXISTS bill_payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      bill_id INTEGER NOT NULL,
      payment_no TEXT NOT NULL,
      amount REAL NOT NULL,
      method TEXT,
      reference TEXT,
      paid_at TEXT,
      created_by INTEGER,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS expenses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      branch_id INTEGER,
      expense_no TEXT NOT NULL,
      category TEXT DEFAULT 'operating',
      vendor TEXT,
      description TEXT,
      account_code TEXT,
      amount REAL NOT NULL DEFAULT 0,
      currency TEXT DEFAULT 'USD',
      expense_date TEXT,
      status TEXT DEFAULT 'posted',
      payment_method TEXT,
      reference TEXT,
      created_by INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(tenant_id, expense_no)
    );
    CREATE TABLE IF NOT EXISTS estimates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      branch_id INTEGER,
      estimate_no TEXT NOT NULL,
      customer_id INTEGER,
      title TEXT,
      description TEXT,
      currency TEXT DEFAULT 'USD',
      subtotal REAL DEFAULT 0,
      tax REAL DEFAULT 0,
      total REAL DEFAULT 0,
      status TEXT DEFAULT 'draft',
      valid_until TEXT,
      created_by INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(tenant_id, estimate_no)
    );
    CREATE TABLE IF NOT EXISTS credit_notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      branch_id INTEGER,
      credit_note_no TEXT NOT NULL,
      customer_id INTEGER,
      invoice_id INTEGER,
      reason TEXT,
      description TEXT,
      currency TEXT DEFAULT 'USD',
      amount REAL DEFAULT 0,
      status TEXT DEFAULT 'issued',
      issue_date TEXT,
      created_by INTEGER,
      created_at TEXT NOT NULL,
      UNIQUE(tenant_id, credit_note_no)
    );
    CREATE TABLE IF NOT EXISTS documents (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      entity_type TEXT NOT NULL,
      entity_id INTEGER,
      title TEXT NOT NULL,
      category TEXT DEFAULT 'general',
      file_name TEXT,
      mime_type TEXT,
      file_data BLOB,
      content_text TEXT,
      size INTEGER DEFAULT 0,
      visibility TEXT DEFAULT 'internal',
      uploaded_by INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS document_shares (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      document_id INTEGER,
      entity_type TEXT NOT NULL,
      entity_id INTEGER NOT NULL,
      token TEXT NOT NULL UNIQUE,
      recipient TEXT,
      permission TEXT DEFAULT 'view',
      status TEXT DEFAULT 'active',
      views INTEGER DEFAULT 0,
      expires_at TEXT,
      created_by INTEGER,
      created_at TEXT NOT NULL,
      last_viewed_at TEXT,
      FOREIGN KEY(document_id) REFERENCES documents(id)
    );
    CREATE TABLE IF NOT EXISTS document_activity (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      entity_type TEXT,
      entity_id INTEGER,
      share_id INTEGER,
      action TEXT,
      detail TEXT,
      actor TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tenant_branding (
      tenant_id INTEGER PRIMARY KEY,
      company_name TEXT,
      legal_name TEXT,
      location TEXT,
      address TEXT,
      phone TEXT,
      email TEXT,
      website TEXT,
      tax_id TEXT,
      logo_text TEXT,
      logo_data TEXT,
      primary_color TEXT DEFAULT '#206090',
      accent_color TEXT DEFAULT '#e02020',
      footer_note TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_bills_tenant ON bills(tenant_id);
    CREATE INDEX IF NOT EXISTS idx_expenses_tenant ON expenses(tenant_id);
    CREATE INDEX IF NOT EXISTS idx_docs_entity ON documents(tenant_id, entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_shares_token ON document_shares(token);
    """)
    for col, ddl in [('tips','TEXT'),('opening_balance','REAL DEFAULT 0'),('fcy','INTEGER DEFAULT 0')]:
        cols = [r['name'] for r in q(conn, "PRAGMA table_info(chart_of_accounts)")]
        if col not in cols:
            conn.execute(f"ALTER TABLE chart_of_accounts ADD COLUMN {col} {ddl}")
    for t in q(conn, "SELECT id FROM tenants"):
        exists = q(conn, "SELECT 1 FROM tenant_branding WHERE tenant_id=? LIMIT 1", (t['id'],), one=True)
        if not exists:
            tenant = q(conn, "SELECT * FROM tenants WHERE id=?", (t['id'],), one=True)
            conn.execute("""INSERT INTO tenant_branding(tenant_id,company_name,legal_name,location,address,phone,email,website,tax_id,logo_text,primary_color,accent_color,footer_note,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (t['id'], tenant.get('name') or 'Fayaka Airline', tenant.get('name') or 'Fayaka Airline', 'Head Office', 'P.O. Box 123, Kampala, Uganda', '+256 700 000 000', 'billing@fayaka.aero', 'https://fayaka.aero', '', 'FA', '#0b2a4a', '#f59e0b', 'Fayaka Airline - Confidential', ts))


def ensure_setup(conn):
    ts = now_iso()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS agency_profile (
      tenant_id INTEGER PRIMARY KEY, company_name TEXT, legal_name TEXT, registration_no TEXT, tax_id TEXT,
      vat_no TEXT, address TEXT, city TEXT, state TEXT, postal_code TEXT, country TEXT, phone TEXT, email TEXT,
      website TEXT, default_currency TEXT DEFAULT 'USD', timezone TEXT DEFAULT 'UTC', fiscal_year_start TEXT DEFAULT '01-01',
      iata_code TEXT, notes TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL,
      description TEXT, category TEXT, product_type TEXT DEFAULT 'Misc', supplier TEXT, with_inventory INTEGER DEFAULT 0,
      unit TEXT DEFAULT 'service', sale_price REAL DEFAULT 0, cost_price REAL DEFAULT 0, tax_code TEXT,
      is_active INTEGER DEFAULT 1, is_core INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(tenant_id, sku)
    );
    CREATE TABLE IF NOT EXISTS currencies (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT, symbol TEXT,
      exchange_rate REAL DEFAULT 1, decimal_places INTEGER DEFAULT 2, is_base INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
      updated_at TEXT NOT NULL, UNIQUE(tenant_id, code)
    );
    CREATE TABLE IF NOT EXISTS contacts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, contact_type TEXT DEFAULT 'supplier', tips TEXT,
      name TEXT NOT NULL, type TEXT, supplier_type TEXT, balance REAL DEFAULT 0, status TEXT DEFAULT 'Active', fcy INTEGER DEFAULT 0,
      company TEXT, email TEXT, phone TEXT, address TEXT, terms TEXT, currency TEXT, reconciliation_format TEXT,
      contact_person TEXT, is_core INTEGER DEFAULT 0, notes TEXT, is_active INTEGER DEFAULT 1,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS business_rules (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, rule_key TEXT NOT NULL, rule_name TEXT NOT NULL,
      description TEXT, value TEXT, value_type TEXT DEFAULT 'text', is_active INTEGER DEFAULT 1,
      updated_at TEXT NOT NULL, UNIQUE(tenant_id, rule_key)
    );
    CREATE TABLE IF NOT EXISTS preferences (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, preference_key TEXT NOT NULL,
      preference_value TEXT, updated_at TEXT NOT NULL, UNIQUE(tenant_id, preference_key)
    );
    CREATE TABLE IF NOT EXISTS mail_templates (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, code TEXT NOT NULL, subject TEXT NOT NULL,
      body_html TEXT, is_active INTEGER DEFAULT 1, updated_at TEXT NOT NULL, UNIQUE(tenant_id, code)
    );
    CREATE TABLE IF NOT EXISTS integrations (
      id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, provider TEXT NOT NULL, name TEXT NOT NULL,
      integration_type TEXT NOT NULL, config_json TEXT DEFAULT '{}', status TEXT DEFAULT 'disconnected',
      last_sync_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(tenant_id, provider)
    );
    """)
    for table, additions in {
        'products': [('product_type',"TEXT DEFAULT 'Misc'"),('supplier','TEXT'),('with_inventory','INTEGER DEFAULT 0'),('is_core','INTEGER DEFAULT 0')],
        'contacts': [('tips','TEXT'),('type','TEXT'),('supplier_type','TEXT'),('balance','REAL DEFAULT 0'),('status',"TEXT DEFAULT 'Active'"),('fcy','INTEGER DEFAULT 0'),('terms','TEXT'),('currency','TEXT'),('reconciliation_format','TEXT'),('contact_person','TEXT'),('is_core','INTEGER DEFAULT 0')]
    }.items():
        cols = [r['name'] for r in q(conn, f"PRAGMA table_info({table})")]
        for col, ddl in additions:
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def seed_setup(conn):
    ts = now_iso()
    for t in q(conn, "SELECT id,name,code FROM tenants"):
        tenant_id = t['id']
        if not q(conn, "SELECT 1 FROM agency_profile WHERE tenant_id=?", (tenant_id,), one=True):
            conn.execute("""INSERT INTO agency_profile(tenant_id,company_name,legal_name,country,default_currency,timezone,fiscal_year_start,updated_at)
                            VALUES (?,?,?,?,?,?,?,?)""", (tenant_id, t['name'], t['name'], 'UG', 'UGX', 'Africa/Kampala', '01-01', ts))
        for sku, name, ptype, supplier, tips in [
            ('CORE-FLIGHTS', 'My Flights', 'Flight', 'My Flights Suppliers', 'Core flight product'),
            ('CORE-HOTELS', 'My Hotels', 'Hotel', 'My Hotels Suppliers', 'Core hotel product'),
            ('CORE-CARS', 'My Car Rentals', 'Car Rental', 'My Car-Rentals Suppliers', 'Core car rental product'),
            ('CORE-MISC', 'My Miscellaneous Travel', 'Misc. Travel', 'My Misc Travels Suppliers', 'Core miscellaneous product'),
            ('BSP', 'BSP', 'BILLING & SETTLEMENT PLAN', '', 'Billing & settlement plan')
        ]:
            conn.execute("""INSERT OR IGNORE INTO products(tenant_id,sku,name,product_type,supplier,description,category,is_core,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,1,?,?)""", (tenant_id, sku, name, ptype, supplier, tips, ptype, ts, ts))
        for name, ctype, supplier_type, tips in [
            ('Bsp', 'BILLING & SETTLEMENT PLAN', 'BSP', 'Billing & settlement plan'),
            ('Car Rental', 'My Car-Rentals Suppliers', 'Car Rental', 'Car rental supplier'),
            ('Flight', 'My Flights Suppliers', 'Flight', 'Flight supplier'),
            ('Hotel', 'My Hotels Suppliers', 'Hotel', 'Hotel supplier'),
            ('Misc. Travel', 'My Misc Travels Suppliers', 'Misc', 'Miscellaneous travel supplier')
        ]:
            if not q(conn, "SELECT 1 FROM contacts WHERE tenant_id=? AND name=?", (tenant_id, name), one=True):
                conn.execute("""INSERT INTO contacts(tenant_id,contact_type,tips,name,type,supplier_type,balance,status,fcy,is_core,created_at,updated_at)
                                VALUES (?,?,?,?,?,?,0,'Active',0,1,?,?)""", (tenant_id, 'supplier', tips, name, ctype, supplier_type, ts, ts))
        for code, name, symbol, rate, base in [('UGX','Uganda Shilling','UGX',1,1),('USD','US Dollar','$',1,0),('EUR','Euro','€',0.92,0),('GBP','British Pound','£',0.79,0),('KES','Kenyan Shilling','KSh',0.033,0)]:
            conn.execute("INSERT OR IGNORE INTO currencies(tenant_id,code,name,symbol,exchange_rate,decimal_places,is_base,is_active,updated_at) VALUES (?,?,?,?,?,2,?,1,?)", (tenant_id, code, name, symbol, rate, base, ts))
        for key, label, value, vtype in [
            ('auto_invoice','Auto invoice on ticketing','false','boolean'),
            ('auto_ticket_task','Create ticketing task on import','true','boolean'),
            ('default_markup','Default markup percent','0','number'),
            ('invoice_due_days','Invoice due days','14','number'),
            ('auto_reconcile_bsp','Auto reconcile BSP statements','false','boolean'),
            ('credit_control','Block invoicing on credit limit','false','boolean')
        ]:
            conn.execute("INSERT OR IGNORE INTO business_rules(tenant_id,rule_key,rule_name,description,value,value_type,updated_at) VALUES (?,?,?,?,?,?,?)", (tenant_id, key, label, label, value, vtype, ts))
        for key, value in [('date_format','YYYY-MM-DD'),('time_format','24h'),('timezone','Africa/Kampala'),('currency_decimals','2'),('items_per_page','100'),('pdf_theme','modern'),('dashboard_view','cards')]:
            conn.execute("INSERT OR IGNORE INTO preferences(tenant_id,preference_key,preference_value,updated_at) VALUES (?,?,?,?)", (tenant_id, key, value, ts))
        for code, subject, body in [
            ('booking_confirmation','Booking confirmation {{pnr}}','<p>Dear {{customer_name}},</p><p>Your booking <strong>{{pnr}}</strong> is confirmed.</p><p>{{origin}} → {{destination}} on {{travel_start}}.</p>'),
            ('invoice','Invoice {{invoice_no}}','<p>Dear {{customer_name}},</p><p>Please find invoice <strong>{{invoice_no}}</strong> attached. Amount due: {{total}} {{currency}}.</p>'),
            ('payment_receipt','Payment received','<p>Thank you. We received {{amount}} {{currency}} against {{invoice_no}}.</p>'),
            ('credit_note','Credit note {{credit_note_no}}','<p>Dear {{customer_name}},</p><p>A credit note for {{amount}} {{currency}} has been issued.</p>'),
            ('estimate','Estimate {{estimate_no}}','<p>Dear {{customer_name}},</p><p>Please review estimate <strong>{{estimate_no}}</strong>.</p>'),
            ('task_reminder','Task reminder: {{title}}','<p>{{title}} is due on {{due_at}}.</p>'),
            ('traveler_statement','Traveler statement {{full_name}}','<p>Dear {{full_name}},</p><p>Please find your travel statement for {{from_date}} to {{to_date}} attached.</p>')
        ]:
            conn.execute("INSERT OR IGNORE INTO mail_templates(tenant_id,code,subject,body_html,updated_at) VALUES (?,?,?,?,?)", (tenant_id, code, subject, body, ts))
        for provider, name, itype, cfg in [
            ('flutterwave','Flutterwave','payment','{"currency":"UGX","country":"UG","payment_options":"mobilemoney,card,banktransfer,ussd"}'),
            ('amadeus_air','Amadeus AIR Import','import','{"folder":"G:/My Drive/Amadeus","pattern":"*.AIR"}'),
            ('gds','GDS Interface','import','{"provider":"amadeus","mode":"scheduled"}'),
            ('smtp','SMTP Mail','mail','{"host":"","port":587,"username":"","from_email":""}'),
            ('accounting','Accounting Export','accounting','{"target":"sage","format":"json"}'),
            ('eticket_forwarding','E-ticket Forwarding','mail','{"forward_to":"","filter":"ticketed"}')
        ]:
            conn.execute("INSERT OR IGNORE INTO integrations(tenant_id,provider,name,integration_type,config_json,status,created_at,updated_at) VALUES (?,?,?,?,?,'disconnected',?,?)", (tenant_id, provider, name, itype, cfg, ts, ts))


def seed_accounting(conn):
    ts = now_iso()
    for t in q(conn, "SELECT id FROM tenants"):
        wanted = set()
        for code, name, atype, group, normal, tips, fcy in ACCOUNTS:
            wanted.add(code)
            exists = q(conn, "SELECT * FROM chart_of_accounts WHERE tenant_id=? AND code=?", (t['id'], code), one=True)
            if not exists:
                conn.execute("""INSERT INTO chart_of_accounts(tenant_id,code,name,account_type,account_group,normal_balance,is_active,tips,fcy,created_at)
                                VALUES (?,?,?,?,?,?,1,?,?,?)""", (t['id'], code, name, atype, group, normal, tips, fcy, ts))
            else:
                conn.execute("""UPDATE chart_of_accounts SET name=?,account_type=?,account_group=?,normal_balance=?,tips=?,fcy=?,is_active=1 WHERE tenant_id=? AND code=?""",
                             (name, atype, group, normal, tips, fcy, t['id'], code))
        for row in q(conn, "SELECT id,code FROM chart_of_accounts WHERE tenant_id=?", (t['id'],)):
            if row['code'] not in wanted and row['code'].isdigit() and int(row['code']) < 7000:
                conn.execute("UPDATE chart_of_accounts SET is_active=0 WHERE id=?", (row['id'],))



def ensure_pss(conn):
    conn.executescript(SCHEMA_PSS)
    user_cols = {r['name'] for r in q(conn, "PRAGMA table_info(users)")}
    for col, ddl in [('agency_id', 'INTEGER'), ('station_code', 'TEXT')]:
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    for table, cols in [
        ('pss_bookings', [('agency_id', 'INTEGER'), ('booking_source', "TEXT DEFAULT 'counter'"),
                          ('trace_id', 'TEXT'), ('offer_id', 'INTEGER'), ('itinerary_id', 'TEXT'),
                          ('trip_type', "TEXT DEFAULT 'oneway'"), ('segment_index', 'INTEGER DEFAULT 0'),
                          ('doc_no', 'TEXT'), ('trip_pnr', 'TEXT'), ('portal_customer_id', 'INTEGER'),
                          ('fare_basis', "TEXT DEFAULT 'Y'"), ('commission', 'REAL DEFAULT 0'),
                          ('service_fee', 'REAL DEFAULT 0'), ('issue_date', 'TEXT'), ('issued_by', 'TEXT'),
                           ('baggage_allowance_kg', 'REAL DEFAULT 23'), ('hold_expires_at', 'TEXT')]),
        ('pss_payments', [('trace_id', 'TEXT'), ('booking_source', "TEXT DEFAULT 'counter'")]),
        ('pss_flights', [('base_fare_reference', 'REAL'), ('fare_optimized_at', 'TEXT')]),
        ('pss_stock_allocations', [('agency_id', 'INTEGER'), ('unit_price', 'REAL DEFAULT 0'), ('total_amount', 'REAL DEFAULT 0'),
                                   ('currency', "TEXT DEFAULT 'USD'"), ('wallet_tx_id', 'INTEGER')]),
    ]:
        existing = {r['name'] for r in q(conn, f"PRAGMA table_info({table})")}
        for col, ddl in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pss_wallet_tx_agency ON pss_wallet_transactions(tenant_id, agency_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pss_wallet_tx_ref ON pss_wallet_transactions(tenant_id, gateway_ref)")


def seed_pss(conn):
    ts = now_iso()
    seeded_creds = []
    tenants = [r['id'] for r in q(conn, "SELECT id FROM tenants")]
    airports = [
        ('KLA', 'HUKD', 'Entebbe International', 'Entebbe', 'Uganda', 'Africa/Kampala', 'Main', 'East Africa', 1),
        ('DXB', 'OMDB', 'Dubai International', 'Dubai', 'UAE', 'Asia/Dubai', 'T3', 'Middle East', 1),
        ('LHR', 'EGLL', 'London Heathrow', 'London', 'UK', 'Europe/London', 'T5', 'Europe', 1),
        ('JFK', 'KJFK', 'New York John F. Kennedy', 'New York', 'USA', 'America/New_York', 'T4', 'North America', 0),
        ('AMS', 'EHAM', 'Amsterdam Schiphol', 'Amsterdam', 'Netherlands', 'Europe/Amsterdam', 'Main', 'Europe', 0),
        ('DOH', 'OTHH', 'Doha Hamad International', 'Doha', 'Qatar', 'Asia/Qatar', 'Main', 'Middle East', 0),
        ('NBO', 'HKJK', 'Nairobi Jomo Kenyatta', 'Nairobi', 'Kenya', 'Africa/Nairobi', '1E', 'East Africa', 0),
        ('ADD', 'HAAB', 'Addis Ababa Bole', 'Addis Ababa', 'Ethiopia', 'Africa/Addis_Ababa', 'Main', 'East Africa', 0),
    ]
    channels = [
        ('WEB', 'Airline Website', 'web', 0, 'KLA,DXB,LHR,NBO', 'USD', 0),
        ('OTA', 'Online Travel Agencies', 'ota', 2.5, 'KLA,DXB,LHR,AMS,JFK', 'USD', 14),
        ('GDS', 'GDS / Travel Agents', 'gds', 1.5, 'KLA,DXB,LHR,AMS,DOH,JFK', 'USD', 14),
        ('COUNTER', 'Airport Ticket Counter', 'counter', 0, 'KLA,NBO,ADD', 'UGX', 0),
        ('CALL', 'Call Centre', 'call_center', 0.5, 'KLA,NBO', 'UGX', 0),
        ('MOBILE', 'Mobile App', 'mobile', 0, 'KLA,DXB,NBO', 'USD', 0),
    ]
    for tid in tenants:
        for code, icao, name, city, country, tz, terminal, region, hub in airports:
            conn.execute("""INSERT OR IGNORE INTO pss_airports
                (tenant_id,code,icao,name,city,country,timezone,terminal,region,status,is_hub,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (tid, code, icao, name, city, country, tz, terminal, region, hub, ts, ts))
        for code, name, ctype, rate, allowed, cur, settle in channels:
            conn.execute("""INSERT OR IGNORE INTO pss_channels
                (tenant_id,code,name,channel_type,commission_rate,allowed_airports,currency,settlement_days,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'active',?,?)""",
                (tid, code, name, ctype, rate, allowed, cur, settle, ts, ts))
        inventory_rules = [
            ('HOLD_EXPIRY', 'Seat hold expiry (minutes)', 'hold_expiry', 'ALL', 'ALL', 20, 0, 0),
            ('POS_GDS_GATE', 'GDS availability gate', 'gate', 'GDS', 'ALL', 85, 0, 0),
            ('TIA_EARLY', 'Temporary inventory adjustment', 'limit', 'ALL', 'ALL', 0, 1, 5)
        ]
        for code, name, rtype, pos, subclass, value, tia_enabled, tia_percent in inventory_rules:
            conn.execute("""INSERT OR IGNORE INTO pss_inventory_rules
                (tenant_id,rule_code,rule_name,rule_type,pos,subclass,value,tia_enabled,tia_percent,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (tid, code, name, rtype, pos, subclass, value, tia_enabled, tia_percent, ts, ts))
        pools = [
            (f'POOL-{tid}-001', 'UG', 'UG', 20000000, 20009999, 20000000, 'airport', 'KLA'),
            (f'POOL-{tid}-002', 'UG', 'UG', 30000000, 30009999, 30000000, 'channel', 'OTA'),
        ]
        for pool_no, ac, prefix, start, end, nxt, loc_type, loc_code in pools:
            conn.execute("""INSERT OR IGNORE INTO pss_stock_pools
                (tenant_id,pool_no,airline_code,prefix,start_serial,end_serial,next_serial,issued_count,void_count,location_type,location_code,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,0,0,?,?,'active',?,?)""",
                (tid, pool_no, ac, prefix, start, end, nxt, loc_type, loc_code, ts, ts))
        flights = [
            ('FA201', 'KLA', 'DXB', '2026-11-20', '09:30', '15:15', 'A320', 180, 'USD', '14:25', 'Main'),
            ('FA315', 'DXB', 'LHR', '2026-11-21', '02:10', '07:40', 'B787', 240, 'USD', 'T3', 'T3'),
            ('FA402', 'KLA', 'NBO', '2026-11-20', '07:00', '08:20', 'CRJ900', 90, 'UGX', 'Gate 4', 'Main'),
            ('FA202', 'DXB', 'KLA', '2026-11-25', '20:15', '22:05', 'A320', 180, 'USD', 'T3', 'T3'),
            ('FA316', 'LHR', 'KLA', '2026-11-26', '11:20', '20:10', 'B787', 240, 'USD', 'T5', 'T5'),
        ]
        for fno, orig, dest, fdate, dep, arr, aircraft, seats, cur, gate, terminal in flights:
            conn.execute("""INSERT OR IGNORE INTO pss_flights
                (tenant_id,flight_no,airline_code,origin,destination,flight_date,dep_time,arr_time,aircraft,total_seats,available_seats,base_fare,currency,gate,terminal,status,created_at,updated_at)
                VALUES (?,?,'FA',?,?,?,?,?,?,?,?,?,?,?,?,'scheduled',?,?)""",
                (tid, fno, orig, dest, fdate, dep, arr, aircraft, seats, seats, 250 if cur == 'USD' else 950000, cur, gate, terminal, ts, ts))


    for tid in tenants:
        agencies = [
            ('AGY-SKY', 'Sky Links Travel Agency', 'ops@skylinks.example', '+256 700 100 200', 2.0, 'WEB,GDS,COUNTER'),
            ('AGY-GLOBAL', 'Global Horizon Tours', 'bookings@globalhorizon.example', '+971 4 000 0000', 2.5, 'GDS,OTA'),
        ]
        for code, name, email, phone, rate, channels in agencies:
            conn.execute("""INSERT OR IGNORE INTO pss_agencies
                (tenant_id,code,name,contact_email,contact_phone,commission_rate,allowed_channels,status,login_token,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'active',?,?,?)""",
                (tid, code, name, email, phone, rate, channels, f"AGY-{tid}-{code[-4:]}", ts, ts))
        flight = q(conn, "SELECT id FROM pss_flights WHERE tenant_id=? ORDER BY id LIMIT 1", (tid,), one=True)
        agency = q(conn, "SELECT id FROM pss_agencies WHERE tenant_id=? ORDER BY id LIMIT 1", (tid,), one=True)
        if flight and agency and not q(conn, "SELECT 1 FROM pss_eticket_directs WHERE tenant_id=?", (tid,), one=True):
            conn.execute("""INSERT INTO pss_eticket_directs
                (tenant_id,offer_no,agency_id,flight_id,fare_amount,taxes,currency,quantity,used_count,valid_until,status,token,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'USD',10,0,'2026-12-31','active',?,NULL,?,?)""",
                (tid, f'DIR-{tid}-001', agency['id'], flight['id'], 250, 25, f'DIR-{tid}-001-TOKEN', ts, ts))
        if not q(conn, "SELECT 1 FROM pss_charter_requests WHERE tenant_id=?", (tid,), one=True):
            conn.execute("""INSERT INTO pss_charter_requests
                (tenant_id,request_no,client_name,contact_email,contact_phone,origin,destination,flight_date,pax_count,aircraft_type,requirements,status,quoted_amount,currency,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',0,'USD',NULL,?,?)""",
                (tid, f'CHR-{tid}-001', 'Nile Safari Company', 'charter@nilesafari.example', '+256 700 200 300',
                 'KLA', 'NBO', '2026-12-15', 40, 'A320', 'Full charter with catering', ts, ts))


    for tid in tenants:
        ancillaries = [
            ('ANC-BAG20', 'Extra baggage 20kg', 'baggage', 'Pre-purchase an additional 20kg checked allowance', 45, 0),
            ('ANC-SEAT', 'Seat selection', 'seat', 'Choose your preferred seat at booking', 15, 180),
            ('ANC-MEAL', 'Meal preference', 'meal', 'Hot meal with beverage service', 12, 200),
            ('ANC-LEGROOM', 'Extra legroom seat', 'legroom', 'Premium seat with additional legroom', 35, 24),
            ('ANC-FLEXTIME', 'Flexible flight time', 'flight_time', 'Change flight time within 24 hours', 60, 0),
            ('ANC-PRIORITY', 'Priority boarding', 'other', 'Board before general boarding', 10, 0),
            ('ANC-LOUNGE', 'Lounge access', 'other', 'Access to airport lounge before departure', 25, 40),
        ]
        for code, name, cat, desc, price, cap in ancillaries:
            conn.execute("""INSERT OR IGNORE INTO pss_ancillaries
                (tenant_id,code,name,category,description,price,currency,unit,capacity,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'USD','per passenger',?,'active',?,?)""",
                (tid, code, name, cat, desc, price, cap, ts, ts))
        for f in q(conn, "SELECT id FROM pss_flights WHERE tenant_id=?", (tid,)):
            conn.execute("""INSERT OR IGNORE INTO pss_baggage_rules
                (tenant_id,flight_id,cabin_class,free_kg,free_pieces,excess_rate_per_kg,max_kg,created_at,updated_at)
                VALUES (?,?,'Y',23,1,12,90,?,?)""", (tid, f['id'], ts, ts))
        docs = [
            ('KLA', 'passport', 'Valid passport', 'Passport valid for at least 6 months', 'Required for all international passengers', 'international'),
            ('KLA', 'visa', 'Entry visa / permit', 'Visa or electronic travel authorisation where applicable', 'Check destination requirements', 'international'),
            ('DXB', 'passport', 'Passport & Emirates ID', 'Passport required; residents carry Emirates ID', 'Required at check-in', 'all'),
            ('LHR', 'passport', 'Passport', 'Valid passport required for UK entry', 'Required for all passengers', 'all'),
        ]
        for code, dtype, title, desc, req, applies in docs:
            conn.execute("""INSERT OR IGNORE INTO pss_airport_docs
                (tenant_id,airport_code,doc_type,title,description,requirement,applies_to,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'active',?,?)""", (tid, code, dtype, title, desc, req, applies, ts, ts))
        weather = [
            ('KLA', 'Partly cloudy', 24, 12, 10, 'No significant weather expected'),
            ('DXB', 'Clear', 38, 18, 10, 'High temperature - hydration advised'),
            ('LHR', 'Light rain', 14, 22, 6, 'Allow extra time for taxiing'),
        ]
        for code, cond, temp, wind, vis, adv in weather:
            conn.execute("""INSERT OR IGNORE INTO pss_weather
                (tenant_id,airport_code,condition,temperature_c,wind_kph,visibility_km,advisory,observed_at,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'current',?,?)""", (tid, code, cond, temp, wind, vis, adv, ts, ts, ts))


    seeded_creds = []
    for tid in tenants:
        branches = [
            ('FA-HQ', 'Fayaka Head Office', 'Kampala', 'Uganda', 'HQ'),
            ('FA-KLA', 'Entebbe Airport Office', 'Entebbe', 'Uganda', 'KLA'),
            ('FA-KLA-CI', 'Entebbe Check-in Desk', 'Entebbe', 'Uganda', 'KLA'),
            ('FA-DXB', 'Dubai Station Office', 'Dubai', 'UAE', 'DXB'),
            ('FA-NBO', 'Nairobi Station Office', 'Nairobi', 'Kenya', 'NBO'),
        ]
        for code, name, city, country, station in branches:
            conn.execute("INSERT OR IGNORE INTO branches(tenant_id,name,code,city,country,created_at) VALUES (?,?,?,?,?,?)",
                         (tid, name, code, city, country, ts))
        hq = q(conn, "SELECT id FROM branches WHERE tenant_id=? AND code='FA-HQ'", (tid,), one=True)
        for idx, ag in enumerate(q(conn, "SELECT id,code,name FROM pss_agencies WHERE tenant_id=?", (tid,))):
            uname = 'partner-' + str(ag['code']).lower()
            arole = 'agency_admin' if idx == 0 else 'agency'
            if not q(conn, "SELECT 1 FROM users WHERE tenant_id=? AND username=?", (tid, uname), one=True):
                apw = secrets.token_urlsafe(12)
                conn.execute("""INSERT INTO users(tenant_id,branch_id,username,password,full_name,email,role,status,agency_id,created_at)
                                VALUES (?,?,?,?,?,?,?,'active',?,?)""",
                             (tid, hq['id'] if hq else None, uname, apw,
                              ag['name'], ag.get('contact_email') or '', arole, ag['id'], ts))
                seeded_creds.append(f"tenant_id={tid} user={uname} password={apw}")
            team_name = uname + '-team'
            if not q(conn, "SELECT 1 FROM users WHERE tenant_id=? AND username=?", (tid, team_name), one=True):
                tpw = secrets.token_urlsafe(12)
                conn.execute("""INSERT INTO users(tenant_id,branch_id,username,password,full_name,role,status,agency_id,created_at)
                                VALUES (?,?,?,?,?, 'agency_agent','active',?,?)""",
                             (tid, hq['id'] if hq else None, team_name, tpw,
                              str(ag['name']) + ' Team', ag['id'], ts))
                seeded_creds.append(f"tenant_id={tid} user={team_name} password={tpw}")
        desks = [
            ('desk-kla', 'KLA Check-in Desk', 'KLA', 'FA-KLA-CI', 'checkin'),
            ('office-kla', 'KLA Airport Office', 'KLA', 'FA-KLA', 'station'),
            ('office-dxb', 'DXB Station Office', 'DXB', 'FA-DXB', 'station'),
            ('office-nbo', 'NBO Station Office', 'NBO', 'FA-NBO', 'station'),
        ]
        for uname, full, station, bcode, role in desks:
            br = q(conn, "SELECT id FROM branches WHERE tenant_id=? AND code=?", (tid, bcode), one=True)
            if not q(conn, "SELECT 1 FROM users WHERE tenant_id=? AND username=?", (tid, uname), one=True):
                dpw = secrets.token_urlsafe(12)
                conn.execute("""INSERT INTO users(tenant_id,branch_id,username,password,full_name,role,status,station_code,created_at)
                                VALUES (?,?,?,?,?,?,'active',?,?)""",
                             (tid, br['id'] if br else None, uname, dpw, full, role, station, ts))
                seeded_creds.append(f"tenant_id={tid} user={uname} password={dpw}")
        airline_team = [
            ('fayaka-accounts', 'Fayaka Accounts Team', 'accountant'),
            ('fayaka-reservations', 'Fayaka Reservations Team', 'reservations'),
            ('fayaka-ops', 'Fayaka Operations Team', 'operations'),
        ]
        for uname, full, role in airline_team:
            if not q(conn, "SELECT 1 FROM users WHERE tenant_id=? AND username=?", (tid, uname), one=True):
                wpw = secrets.token_urlsafe(12)
                conn.execute("""INSERT INTO users(tenant_id,branch_id,username,password,full_name,role,status,created_at)
                                VALUES (?,?,?,?,?,?,'active',?)""",
                             (tid, hq['id'] if hq else None, uname, wpw, full, role, ts))
                seeded_creds.append(f"tenant_id={tid} user={uname} password={wpw}")
    if seeded_creds:
        print("[seed] PSS user credentials - change these immediately: " + ", ".join(seeded_creds), file=sys.stderr)


def seed(conn):
    seeded_creds = []

    def _note_seed(tenant_id, username, pw):
        seeded_creds.append(f"tenant_id={tenant_id} user={username} password={pw}")

    cur = conn.execute("SELECT COUNT(*) c FROM plans")
    if cur.fetchone()["c"] == 0:
        plans = [
            ("Starter", "starter", 49, 3, 1, 300, 2, json.dumps({"analytics": "basic", "gl": False, "api": False, "ndc": False})),
            ("Professional", "pro", 149, 15, 3, 2000, 6, json.dumps({"analytics": "standard", "gl": "lite", "api": False, "sla": True, "ndc": True})),
            ("Enterprise", "enterprise", 499, 999, 20, 999999, 99, json.dumps({"analytics": "advanced", "gl": True, "api": True, "sla": True, "sso": True, "ndc": True})),
        ]
        conn.executemany("INSERT INTO plans(name,code,price_monthly,max_users,max_branches,max_orders_per_month,max_connectors,features_json) VALUES (?,?,?,?,?,?,?,?)", plans)
    cur = conn.execute("SELECT COUNT(*) c FROM tenants")
    if cur.fetchone()["c"] == 0:
        ts = now_iso()
        conn.execute("INSERT INTO tenants(name,code,status,plan_id,settings_json,created_at) VALUES (?,?,?,?,?,?)",
                     ("Fayaka Airline", "demo", "active", 2, json.dumps({"currency": "USD", "branding": "Demo"}), ts))
        tenant_id = conn.execute("SELECT id FROM tenants WHERE code='demo'").fetchone()["id"]
        conn.execute("INSERT INTO branches(tenant_id,name,code,city,country,created_at) VALUES (?,?,?,?,?,?)",
                     (tenant_id, "Head Office", "HQ", "Kampala", "UG", ts))
        branch_id = conn.execute("SELECT id FROM branches WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone()["id"]
        admin_pw = secrets.token_urlsafe(12)
        agent_pw = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO users(tenant_id,branch_id,username,password,full_name,email,role,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (tenant_id, branch_id, "admin", admin_pw, "Demo Admin", "admin@demo.local", "admin", "active", ts))
        conn.execute("INSERT INTO users(tenant_id,branch_id,username,password,full_name,email,role,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (tenant_id, branch_id, "agent", agent_pw, "Demo Agent", "agent@demo.local", "agent", "active", ts))
        _note_seed(tenant_id, "admin", admin_pw)
        _note_seed(tenant_id, "agent", agent_pw)
        conn.execute("INSERT INTO subscriptions(tenant_id,plan_id,status,start_date,billing_cycle,mrr,created_at) VALUES (?,?,?,?,?,?,?)",
                     (tenant_id, 2, "active", date.today().isoformat(), "monthly", 149, ts))
        defaults = [
            (tenant_id, "Amadeus AIR Files", "amadeus_air", json.dumps({"path": "workspace", "mode": "file_scan"})),
            (tenant_id, "Amadeus GDS", "gds_amadeus", json.dumps({"mode": "api", "endpoint": ""})),
            (tenant_id, "Sabre GDS", "gds_sabre", json.dumps({"mode": "api", "endpoint": ""})),
            (tenant_id, "Travelport GDS", "gds_travelport", json.dumps({"mode": "api", "endpoint": ""})),
            (tenant_id, "NDC Aggregator", "ndc", json.dumps({"provider": "generic", "endpoint": ""})),
            (tenant_id, "Airline Portal", "airline_portal", json.dumps({"mode": "manual"})),
            (tenant_id, "BSP/ARC Settlement", "bsp_arc", json.dumps({"mode": "file"})),
        ]
        for t, n, ty, cfg in defaults:
            conn.execute("INSERT INTO connectors(tenant_id,name,type,config_json,status,created_at) VALUES (?,?,?,?,?,?)",
                         (t, n, ty, cfg, "active", ts))
    if seeded_creds:
        print("[seed] Seeded credentials - change these immediately via Setup > Users: "
              + ", ".join(seeded_creds), file=sys.stderr)


def q(conn, sql, args=(), one=False):
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def log_audit(conn, tenant_id, user_id, action, entity, entity_id=None, detail=None):
    conn.execute("INSERT INTO audit_logs(tenant_id,user_id,action,entity,entity_id,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                 (tenant_id, user_id, action, entity, entity_id, json.dumps(detail or {}, default=str), now_iso()))
