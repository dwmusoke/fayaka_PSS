-- Fayaka Airline PSS - Supabase / PostgreSQL bootstrap

-- Paste into Supabase SQL Editor. The app also runs init_db() on startup.

CREATE EXTENSION IF NOT EXISTS pgcrypto;



CREATE TABLE IF NOT EXISTS tenants (
  id SERIAL PRIMARY KEY ,
  name TEXT NOT NULL,
  code TEXT UNIQUE NOT NULL,
  status TEXT DEFAULT 'active',
  plan_id INTEGER,
  settings_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
  id SERIAL PRIMARY KEY ,
  name TEXT NOT NULL,
  code TEXT UNIQUE NOT NULL,
  price_monthly DOUBLE PRECISION DEFAULT 0,
  max_users INTEGER DEFAULT 5,
  max_branches INTEGER DEFAULT 1,
  max_orders_per_month INTEGER DEFAULT 500,
  max_connectors INTEGER DEFAULT 2,
  features_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  plan_id INTEGER NOT NULL,
  status TEXT DEFAULT 'active',
  start_date TEXT,
  end_date TEXT,
  billing_cycle TEXT DEFAULT 'monthly',
  mrr DOUBLE PRECISION DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(plan_id) REFERENCES plans(id)
);

CREATE TABLE IF NOT EXISTS branches (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  code TEXT,
  city TEXT,
  country TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  username TEXT NOT NULL,
  password TEXT NOT NULL,
  full_name TEXT,
  email TEXT,
  role TEXT DEFAULT 'agent',
  status TEXT DEFAULT 'active',
  agency_id INTEGER,
  station_code TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, username),
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(branch_id) REFERENCES branches(id)
);

CREATE TABLE IF NOT EXISTS connectors (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  type TEXT DEFAULT 'B2C',
  name TEXT NOT NULL,
  company TEXT,
  email TEXT,
  phone TEXT,
  loyalty_id TEXT,
  agency_id INTEGER,
  portal_customer_id INTEGER,
  source TEXT,
  source_ref TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS travelers (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  agency_id INTEGER,
  pss_booking_id INTEGER,
  booking_source TEXT,
  trace_id TEXT,
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
  total_sell DOUBLE PRECISION DEFAULT 0,
  total_cost DOUBLE PRECISION DEFAULT 0,
  margin DOUBLE PRECISION DEFAULT 0,
  balance_due DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  status TEXT,
  note TEXT,
  source TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS segments (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  kind TEXT,
  code TEXT,
  text TEXT,
  raw_json TEXT,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS tasks (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  subtotal DOUBLE PRECISION DEFAULT 0,
  tax DOUBLE PRECISION DEFAULT 0,
  fees DOUBLE PRECISION DEFAULT 0,
  discount DOUBLE PRECISION DEFAULT 0,
  total DOUBLE PRECISION DEFAULT 0,
  amount_paid DOUBLE PRECISION DEFAULT 0,
  balance DOUBLE PRECISION DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, invoice_no),
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS invoice_lines (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  invoice_id INTEGER NOT NULL,
  description TEXT,
  qty DOUBLE PRECISION DEFAULT 1,
  unit_cost DOUBLE PRECISION DEFAULT 0,
  unit_price DOUBLE PRECISION DEFAULT 0,
  tax DOUBLE PRECISION DEFAULT 0,
  line_total DOUBLE PRECISION DEFAULT 0,
  FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);

CREATE TABLE IF NOT EXISTS payments (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  invoice_id INTEGER,
  order_id INTEGER,
  customer_id INTEGER,
  payment_no TEXT,
  method TEXT,
  reference TEXT,
  amount DOUBLE PRECISION NOT NULL,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  payment_id INTEGER NOT NULL,
  invoice_id INTEGER NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(payment_id) REFERENCES payments(id),
  FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);

CREATE TABLE IF NOT EXISTS supplier_bills (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  supplier_name TEXT,
  order_id INTEGER,
  bill_no TEXT,
  source_type TEXT,
  currency TEXT DEFAULT 'USD',
  amount DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'open',
  issue_date TEXT,
  due_date TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS commissions (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER,
  agent_user_id INTEGER,
  basis_amount DOUBLE PRECISION DEFAULT 0,
  rate DOUBLE PRECISION DEFAULT 0,
  amount DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'accrued',
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id),
  FOREIGN KEY(agent_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS journals (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  ref_type TEXT,
  ref_id INTEGER,
  entry_date TEXT,
  memo TEXT,
  debit_account TEXT,
  credit_account TEXT,
  amount DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  subscription_id INTEGER,
  plan_id INTEGER,
  invoice_no TEXT NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  subscription_invoice_id INTEGER,
  gateway TEXT,
  transaction_id TEXT,
  tx_ref TEXT,
  flw_ref TEXT,
  amount DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  bill_no TEXT NOT NULL,
  supplier TEXT NOT NULL,
  description TEXT,
  order_id INTEGER,
  account_code TEXT,
  category TEXT DEFAULT 'supplier',
  currency TEXT DEFAULT 'USD',
  subtotal DOUBLE PRECISION DEFAULT 0,
  tax DOUBLE PRECISION DEFAULT 0,
  total DOUBLE PRECISION DEFAULT 0,
  amount_paid DOUBLE PRECISION DEFAULT 0,
  balance DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  bill_id INTEGER NOT NULL,
  payment_no TEXT NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
  method TEXT,
  reference TEXT,
  paid_at TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(bill_id) REFERENCES bills(id)
);

CREATE TABLE IF NOT EXISTS expenses (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  expense_no TEXT NOT NULL,
  category TEXT DEFAULT 'operating',
  vendor TEXT,
  description TEXT,
  account_code TEXT,
  amount DOUBLE PRECISION NOT NULL DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  estimate_no TEXT NOT NULL,
  customer_id INTEGER,
  title TEXT,
  description TEXT,
  currency TEXT DEFAULT 'USD',
  subtotal DOUBLE PRECISION DEFAULT 0,
  tax DOUBLE PRECISION DEFAULT 0,
  total DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'draft',
  valid_until TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, estimate_no),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS credit_notes (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  branch_id INTEGER,
  credit_note_no TEXT NOT NULL,
  customer_id INTEGER,
  invoice_id INTEGER,
  reason TEXT,
  description TEXT,
  currency TEXT DEFAULT 'USD',
  amount DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'issued',
  issue_date TEXT,
  agency_id INTEGER,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, credit_note_no),
  FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);

CREATE TABLE IF NOT EXISTS documents (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  title TEXT NOT NULL,
  category TEXT DEFAULT 'general',
  file_name TEXT,
  mime_type TEXT,
  file_data BYTEA,
  content_text TEXT,
  size INTEGER DEFAULT 0,
  visibility TEXT DEFAULT 'internal',
  uploaded_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_shares (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  tenant_id SERIAL PRIMARY KEY,
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
  primary_color TEXT DEFAULT '#0f2742',
  accent_color TEXT DEFAULT '#0b6efd',
  footer_note TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS agency_profile (
  tenant_id SERIAL PRIMARY KEY,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  category TEXT,
  product_type TEXT DEFAULT 'Misc',
  supplier TEXT,
  with_inventory INTEGER DEFAULT 0,
  unit TEXT DEFAULT 'service',
  sale_price DOUBLE PRECISION DEFAULT 0,
  cost_price DOUBLE PRECISION DEFAULT 0,
  tax_code TEXT,
  is_active INTEGER DEFAULT 1,
  is_core INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, sku)
);

CREATE TABLE IF NOT EXISTS currencies (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT,
  symbol TEXT,
  exchange_rate DOUBLE PRECISION DEFAULT 1,
  decimal_places INTEGER DEFAULT 2,
  is_base INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);

CREATE TABLE IF NOT EXISTS contacts (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  contact_type TEXT DEFAULT 'supplier',
  tips TEXT,
  name TEXT NOT NULL,
  type TEXT,
  supplier_type TEXT,
  balance DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  preference_key TEXT NOT NULL,
  preference_value TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, preference_key)
);

CREATE TABLE IF NOT EXISTS mail_templates (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  subject TEXT NOT NULL,
  body_html TEXT,
  is_active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);

CREATE TABLE IF NOT EXISTS integrations (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  partner_type TEXT DEFAULT 'non_iata',
  iata_code TEXT,
  country TEXT,
  contact_person TEXT,
  email TEXT,
  phone TEXT,
  commission_rate DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'active',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unused_tickets (
  id SERIAL PRIMARY KEY ,
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
  value DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disruptions (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  refund_no TEXT NOT NULL,
  order_id INTEGER,
  pnr TEXT,
  ticket_no TEXT,
  traveler_name TEXT,
  source TEXT DEFAULT 'manual',
  reason TEXT DEFAULT 'cancellation',
  amount DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  voucher_no TEXT NOT NULL,
  voucher_type TEXT DEFAULT 'hotel',
  supplier TEXT,
  pnr TEXT,
  traveler_name TEXT,
  issue_date TEXT,
  service_date TEXT,
  confirmation_no TEXT,
  amount DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  status TEXT DEFAULT 'issued',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, voucher_no)
);

CREATE TABLE IF NOT EXISTS coupons (
  id SERIAL PRIMARY KEY ,
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
  refund_amount DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, coupon_no)
);

CREATE TABLE IF NOT EXISTS bank_accounts (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  account_name TEXT NOT NULL,
  account_type TEXT DEFAULT 'bank',
  account_no TEXT,
  bank_name TEXT,
  currency TEXT DEFAULT 'USD',
  opening_balance DOUBLE PRECISION DEFAULT 0,
  current_balance DOUBLE PRECISION DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, account_name)
);

CREATE TABLE IF NOT EXISTS bank_transactions (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  txn_no TEXT NOT NULL,
  account_id INTEGER,
  txn_date TEXT,
  description TEXT,
  txn_type TEXT DEFAULT 'receipt',
  amount DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  recon_no TEXT NOT NULL,
  source TEXT DEFAULT 'bank',
  statement_ref TEXT,
  period TEXT,
  expected_amount DOUBLE PRECISION DEFAULT 0,
  actual_amount DOUBLE PRECISION DEFAULT 0,
  variance DOUBLE PRECISION DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS pss_airports (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  channel_type TEXT DEFAULT 'web',
  commission_rate DOUBLE PRECISION DEFAULT 0,
  allowed_airports TEXT,
  currency TEXT DEFAULT 'USD',
  settlement_days INTEGER DEFAULT 7,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);

CREATE TABLE IF NOT EXISTS pss_stock_pools (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, allocation_no),
  FOREIGN KEY(pool_id) REFERENCES pss_stock_pools(id),
  FOREIGN KEY(channel_id) REFERENCES pss_channels(id)
);

CREATE TABLE IF NOT EXISTS pss_flights (
  id SERIAL PRIMARY KEY ,
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
  base_fare DOUBLE PRECISION DEFAULT 0,
  base_fare_reference DOUBLE PRECISION,
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
  id SERIAL PRIMARY KEY ,
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
  fare_amount DOUBLE PRECISION DEFAULT 0,
  taxes DOUBLE PRECISION DEFAULT 0,
  total_amount DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  ticket_number TEXT,
  ticket_status TEXT DEFAULT 'unissued',
  payment_status TEXT DEFAULT 'pending',
  booking_status TEXT DEFAULT 'confirmed',
  hold_expires_at TEXT,
  agency_id INTEGER,
  booking_source TEXT DEFAULT 'counter',
  trace_id TEXT,
  offer_id INTEGER,
  itinerary_id TEXT,
  trip_type TEXT DEFAULT 'oneway',
  segment_index INTEGER DEFAULT 0,
  doc_no TEXT,
  trip_pnr TEXT,
  portal_customer_id INTEGER,
  fare_basis TEXT DEFAULT 'Y',
  commission DOUBLE PRECISION DEFAULT 0,
  service_fee DOUBLE PRECISION DEFAULT 0,
  issue_date TEXT,
  issued_by TEXT,
  baggage_allowance_kg DOUBLE PRECISION DEFAULT 23,
  seat_no TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, pnr),
  FOREIGN KEY(flight_id) REFERENCES pss_flights(id),
  FOREIGN KEY(channel_id) REFERENCES pss_channels(id)
);

CREATE TABLE IF NOT EXISTS pss_inventory_legs (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  rule_code TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  rule_type TEXT DEFAULT 'limit',
  pos TEXT DEFAULT 'ALL',
  subclass TEXT DEFAULT 'ALL',
  value DOUBLE PRECISION DEFAULT 0,
  tia_enabled INTEGER DEFAULT 0,
  tia_percent DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'active',
  effective_from TEXT,
  effective_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, rule_code)
);

CREATE TABLE IF NOT EXISTS pss_inventory_history (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  flight_id INTEGER NOT NULL,
  segment_id INTEGER,
  pos TEXT DEFAULT 'ALL',
  subclass TEXT DEFAULT 'ALL',
  valuation DOUBLE PRECISION DEFAULT 0,
  active INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, flight_id, segment_id, pos, subclass)
);

CREATE TABLE IF NOT EXISTS pss_inventory_market (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  market TEXT NOT NULL,
  pos TEXT DEFAULT 'ALL',
  origin TEXT,
  destination TEXT,
  flight_date TEXT,
  demand INTEGER DEFAULT 0,
  bookings INTEGER DEFAULT 0,
  revenue DOUBLE PRECISION DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, market, pos, flight_date)
);

CREATE TABLE IF NOT EXISTS pss_api_keys (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  total_amount DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  order_no TEXT NOT NULL,
  offer_id INTEGER,
  status TEXT DEFAULT 'confirmed',
  pnr TEXT,
  passenger_count INTEGER DEFAULT 1,
  amount DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  disruption_id INTEGER,
  booking_id INTEGER NOT NULL,
  old_flight_id INTEGER,
  new_flight_id INTEGER NOT NULL,
  reason TEXT,
  fare_difference DOUBLE PRECISION DEFAULT 0,
  waiver INTEGER DEFAULT 1,
  status TEXT DEFAULT 'confirmed',
  notify_sent INTEGER DEFAULT 0,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pss_notifications_sent (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  checkin_id INTEGER NOT NULL,
  booking_id INTEGER,
  pnr TEXT,
  bag_tag TEXT NOT NULL,
  sequence_no INTEGER DEFAULT 0,
  weight_kg DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  payment_no TEXT NOT NULL,
  booking_id INTEGER,
  pnr TEXT,
  channel_id INTEGER,
  amount DOUBLE PRECISION NOT NULL DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  contact_email TEXT,
  contact_phone TEXT,
  commission_rate DOUBLE PRECISION DEFAULT 0,
  allowed_channels TEXT,
  status TEXT DEFAULT 'active',
  login_token TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, code)
);

CREATE TABLE IF NOT EXISTS pss_eticket_directs (
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  offer_no TEXT NOT NULL,
  agency_id INTEGER NOT NULL,
  flight_id INTEGER,
  fare_amount DOUBLE PRECISION DEFAULT 0,
  taxes DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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
  quoted_amount DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  assigned_to TEXT,
  notes TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, request_no)
);

CREATE TABLE IF NOT EXISTS pss_charter_flights (
  id SERIAL PRIMARY KEY ,
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
  total_amount DOUBLE PRECISION DEFAULT 0,
  amount_paid DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT DEFAULT 'other',
  description TEXT,
  price DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  sale_no TEXT NOT NULL,
  booking_id INTEGER NOT NULL,
  ancillary_id INTEGER,
  description TEXT,
  quantity INTEGER DEFAULT 1,
  unit_price DOUBLE PRECISION DEFAULT 0,
  amount DOUBLE PRECISION DEFAULT 0,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  flight_id INTEGER,
  cabin_class TEXT DEFAULT 'Y',
  free_kg DOUBLE PRECISION DEFAULT 23,
  free_pieces INTEGER DEFAULT 1,
  excess_rate_per_kg DOUBLE PRECISION DEFAULT 12,
  max_kg DOUBLE PRECISION DEFAULT 90,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pss_airport_docs (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  airport_code TEXT NOT NULL,
  condition TEXT,
  temperature_c DOUBLE PRECISION DEFAULT 0,
  wind_kph DOUBLE PRECISION DEFAULT 0,
  visibility_km DOUBLE PRECISION DEFAULT 10,
  advisory TEXT,
  observed_at TEXT,
  status TEXT DEFAULT 'current',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portal_customers (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  booking_id INTEGER NOT NULL,
  tax_code TEXT NOT NULL,
  description TEXT,
  amount DOUBLE PRECISION DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  created_at TEXT NOT NULL,
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);

CREATE TABLE IF NOT EXISTS pss_document_audit (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
  tenant_id INTEGER NOT NULL,
  booking_id INTEGER,
  pnr TEXT,
  doc_type TEXT DEFAULT 'passport',
  doc_no TEXT,
  title TEXT,
  file_name TEXT,
  mime_type TEXT,
  file_data BYTEA,
  notes TEXT,
  uploaded_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(booking_id) REFERENCES pss_bookings(id)
);

CREATE TABLE IF NOT EXISTS pss_ticket_events (
  id SERIAL PRIMARY KEY ,
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
  id SERIAL PRIMARY KEY ,
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

ALTER TABLE users ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS station_code TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS portal_customer_id INTEGER;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS source_ref TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS pss_booking_id INTEGER;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS booking_source TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE credit_notes ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS receipt_no TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pss_booking_id INTEGER;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS pss_payment_id INTEGER;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS pss_booking_id INTEGER;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE journals ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE journals ADD COLUMN IF NOT EXISTS pss_booking_id INTEGER;
ALTER TABLE journals ADD COLUMN IF NOT EXISTS pss_payment_id INTEGER;
ALTER TABLE journals ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE estimates ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS agency_id INTEGER;
ALTER TABLE pss_bookings ADD COLUMN IF NOT EXISTS customer_id INTEGER;
ALTER TABLE pss_bookings ADD COLUMN IF NOT EXISTS order_id INTEGER;
ALTER TABLE pss_bookings ADD COLUMN IF NOT EXISTS invoice_id INTEGER;
ALTER TABLE pss_payments ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE pss_payments ADD COLUMN IF NOT EXISTS booking_source TEXT;
ALTER TABLE pss_payments ADD COLUMN IF NOT EXISTS customer_id INTEGER;
ALTER TABLE pss_payments ADD COLUMN IF NOT EXISTS erp_payment_id TEXT;
ALTER TABLE pss_payments ADD COLUMN IF NOT EXISTS invoice_id INTEGER;
ALTER TABLE pss_payments ADD COLUMN IF NOT EXISTS raw_json TEXT;
ALTER TABLE chart_of_accounts ADD COLUMN IF NOT EXISTS tips TEXT;
ALTER TABLE chart_of_accounts ADD COLUMN IF NOT EXISTS opening_balance DOUBLE PRECISION DEFAULT 0;
ALTER TABLE chart_of_accounts ADD COLUMN IF NOT EXISTS fcy TEXT;
ALTER TABLE portal_sessions ADD COLUMN IF NOT EXISTS client_hash TEXT;
ALTER TABLE pss_checkins ADD COLUMN IF NOT EXISTS boarding_status TEXT DEFAULT 'not_boarded';
ALTER TABLE pss_checkins ADD COLUMN IF NOT EXISTS boarded_at TEXT;
ALTER TABLE pss_checkins ADD COLUMN IF NOT EXISTS arrival_status TEXT;
ALTER TABLE pss_checkins ADD COLUMN IF NOT EXISTS carousel TEXT;
CREATE INDEX IF NOT EXISTS idx_pss_baggage_checkin ON pss_baggage_items(tenant_id, checkin_id);
CREATE INDEX IF NOT EXISTS idx_pss_baggage_tag ON pss_baggage_items(tenant_id, bag_tag);
CREATE INDEX IF NOT EXISTS idx_pss_baggage_events ON pss_baggage_events(tenant_id, baggage_id, created_at);
