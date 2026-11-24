import glob
import json
import os
from datetime import datetime

from air_parser import parse_air_file, clean_text
from db import db, now_iso, q, log_audit


def upsert_customer(conn, tenant_id, pax):
    name = (pax.get('full_name') or '').strip()
    if not name:
        return None
    phone = pax.get('phone') or None
    email = pax.get('email') or None
    # Match by identity first (email, then exact name). Never merge distinct
    # travelers just because a shared booking contact phone was copied onto
    # every passenger line in the source file.
    row = None
    if email:
        row = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND email=? LIMIT 1", (tenant_id, email), one=True)
    if not row:
        row = q(conn, "SELECT id FROM customers WHERE tenant_id=? AND name=? LIMIT 1", (tenant_id, name), one=True)
    if row:
        cid = row['id']
        conn.execute("UPDATE customers SET name=COALESCE(NULLIF(?,''),name), phone=COALESCE(?,phone), email=COALESCE(?,email) WHERE id=?",
                     (name, phone, email, cid))
        return cid
    cur = conn.execute("INSERT INTO customers(tenant_id,type,name,email,phone,created_at) VALUES (?,?,?,?,?,?)",
                       (tenant_id, 'B2C', name, email, phone, now_iso()))
    return cur.lastrowid


def upsert_traveler(conn, tenant_id, customer_id, pax):
    name = (pax.get('full_name') or '').strip()
    if not name:
        return None
    row = q(conn, "SELECT id FROM travelers WHERE tenant_id=? AND full_name=? AND COALESCE(phone,'')=COALESCE(?,COALESCE(phone,'')) LIMIT 1",
            (tenant_id, name, pax.get('phone')), one=True)
    if row:
        tid = row['id']
        conn.execute("UPDATE travelers SET customer_id=COALESCE(?,customer_id), email=COALESCE(?,email), phone=COALESCE(?,phone) WHERE id=?",
                     (customer_id, pax.get('email'), pax.get('phone'), tid))
        return tid
    cur = conn.execute("INSERT INTO travelers(tenant_id,customer_id,full_name,traveler_type,email,phone,created_at) VALUES (?,?,?,?,?,?,?)",
                       (tenant_id, customer_id, name, pax.get('type') or 'ADT', pax.get('email'), pax.get('phone'), now_iso()))
    return cur.lastrowid


def ensure_task(conn, tenant_id, order_id, task_type, title, due_at=None, priority='medium'):
    existing = q(conn, "SELECT id,status FROM tasks WHERE tenant_id=? AND order_id=? AND type=? AND status!='closed' LIMIT 1",
                 (tenant_id, order_id, task_type), one=True)
    if existing:
        conn.execute("UPDATE tasks SET title=?, due_at=?, priority=? WHERE id=?", (title, due_at, priority, existing['id']))
        return existing['id']
    cur = conn.execute("INSERT INTO tasks(tenant_id,order_id,type,title,status,priority,due_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (tenant_id, order_id, task_type, title, 'open', priority, due_at, now_iso()))
    return cur.lastrowid


def _recompute_order(conn, tenant_id, order_id):
    # margin/balance from invoices/payments
    inv = q(conn, "SELECT COALESCE(SUM(total),0) s, COALESCE(SUM(amount_paid),0) p, COALESCE(SUM(balance),0) b FROM invoices WHERE tenant_id=? AND order_id=? AND kind='invoice'", (tenant_id, order_id), one=True)
    bill = q(conn, "SELECT COALESCE(SUM(amount),0) c FROM supplier_bills WHERE tenant_id=? AND order_id=?", (tenant_id, order_id), one=True)
    sell = (inv or {}).get('s', 0) or 0
    cost = (bill or {}).get('c', 0) or 0
    # if no invoice, keep existing sell
    row = q(conn, "SELECT total_sell,total_cost,balance_due FROM orders WHERE id=?", (order_id,), one=True) or {}
    if sell == 0:
        sell = row.get('total_sell') or 0
    if cost == 0:
        cost = row.get('total_cost') or 0
    balance = (inv or {}).get('b', 0) if inv and (inv.get('s') or 0) > 0 else (sell - (inv or {}).get('p', 0) if inv else 0)
    margin = sell - cost
    conn.execute("UPDATE orders SET total_sell=?, total_cost=?, margin=?, balance_due=?, updated_at=? WHERE id=?",
                 (sell, cost, margin, max(balance or 0, 0), now_iso(), order_id))


def recompute_invoice(conn, invoice_id):
    inv = q(conn, "SELECT * FROM invoices WHERE id=?", (invoice_id,), one=True)
    if not inv:
        return
    paid = q(conn, """SELECT COALESCE(SUM(a.amount),0) s FROM allocations a
                      LEFT JOIN payments p ON p.id=a.payment_id
                      WHERE a.invoice_id=? AND (p.id IS NULL OR LOWER(COALESCE(p.status,'')) IN
                      ('received','captured','paid','successful','cleared'))""", (invoice_id,), one=True)['s'] or 0
    balance = max((inv.get('total') or 0) - paid, 0)
    status = inv.get('status') or 'issued'
    if status in ('void', 'cancelled', 'draft', 'refunded'):
        pass
    elif balance <= 0.01 and (inv.get('total') or 0) > 0:
        status = 'paid'
    elif paid > 0:
        status = 'partially_paid'
    conn.execute("UPDATE invoices SET amount_paid=?, balance=?, status=? WHERE id=?", (paid, balance, status, invoice_id))
    if inv.get('order_id'):
        _recompute_order(conn, inv['tenant_id'], inv['order_id'])


class ExternalImportDisabled(Exception):
    pass


EXTERNAL_IMPORT_MSG = 'External AIR import is disabled: bookings now flow via Fayaka Online Booking and Eticket Direct engines'


def import_air_parsed(conn, tenant_id, parsed, source_type='amadeus_air', connector_id=None, user_id=None, raw_document_id=None):
    raise ExternalImportDisabled(EXTERNAL_IMPORT_MSG)
    existing = q(conn, "SELECT id FROM orders WHERE tenant_id=? AND source_type=? AND source_id=?", (tenant_id, source_type, source_id), one=True)
    # choose primary customer from first passenger
    customer_id = None
    if parsed.get('passengers'):
        customer_id = upsert_customer(conn, tenant_id, parsed['passengers'][0])
    ts = now_iso()
    ticket_status = 'ticketed' if parsed.get('ticketed') else ('ticketing_pending' if parsed.get('tktl') else 'unticketed')
    pnr = parsed.get('pnr')
    notes = '; '.join(parsed.get('remarks', [])[:5]) if parsed.get('remarks') else None
    if existing:
        order_id = existing['id']
        conn.execute("""UPDATE orders SET connector_id=?, raw_document_id=?, pnr=?, office_id=?, account_code=?, customer_id=?, status=?, ticket_status=?,
                        tktl=?, booking_date=?, travel_start=?, travel_end=?, origin=?, destination=?, notes=?, updated_at=? WHERE id=?""",
                     (connector_id, raw_document_id, pnr, parsed.get('office_id'), parsed.get('account_code'), customer_id, parsed.get('status','booked'),
                      ticket_status, parsed.get('tktl'), parsed.get('booking_date'), parsed.get('travel_start'), parsed.get('travel_end'),
                      parsed.get('origin'), parsed.get('destination'), notes, ts, order_id))
        conn.execute("DELETE FROM segments WHERE order_id=?", (order_id,))
        conn.execute("DELETE FROM order_passengers WHERE order_id=?", (order_id,))
        conn.execute("DELETE FROM order_services WHERE order_id=?", (order_id,))
        action = 'updated'
    else:
        cur = conn.execute("""INSERT INTO orders(tenant_id,branch_id,source_type,source_id,connector_id,raw_document_id,pnr,order_number,office_id,account_code,
                              customer_id,owner_user_id,status,ticket_status,tktl,booking_date,travel_start,travel_end,origin,destination,currency,total_sell,total_cost,margin,balance_due,notes,created_at,updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (tenant_id, None, source_type, source_id, connector_id, raw_document_id, pnr, pnr, parsed.get('office_id'), parsed.get('account_code'),
                            customer_id, user_id, parsed.get('status','booked'), ticket_status, parsed.get('tktl'), parsed.get('booking_date'),
                            parsed.get('travel_start'), parsed.get('travel_end'), parsed.get('origin'), parsed.get('destination'), parsed.get('currency','USD'), 0,0,0,0, notes, ts, ts))
        order_id = cur.lastrowid
        conn.execute("INSERT INTO order_status_history(tenant_id,order_id,status,note,source,created_at) VALUES (?,?,?,?,?,?)",
                     (tenant_id, order_id, parsed.get('status','booked'), 'Imported from source', source_type, ts))
        action = 'imported'

    for seg in parsed.get('segments', []):
        conn.execute("""INSERT INTO segments(tenant_id,order_id,seg_ref,carrier,flight_no,class_code,cabin,origin,origin_name,destination,destination_name,
                        depart_at,arrive_at,status_code,status_pax,equipment,baggage,eticket,is_void,raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (tenant_id, order_id, seg.get('seg_ref'), seg.get('carrier'), seg.get('flight_no'), seg.get('class_code'), seg.get('cabin'),
                      seg.get('origin'), seg.get('origin_name'), seg.get('destination'), seg.get('destination_name'),
                      seg.get('depart_at'), seg.get('arrive_at'), seg.get('status_code'), seg.get('status_pax'), seg.get('equipment'), seg.get('baggage'),
                      seg.get('eticket'), seg.get('is_void',0), json.dumps(seg, default=str)))
    for pax in parsed.get('passengers', []):
        # Each passenger is a contact in their own right. Give the traveler a
        # customer record carrying that passenger's own email/phone, while the
        # booking keeps the first passenger as its primary customer.
        pax_customer_id = upsert_customer(conn, tenant_id, pax) or customer_id
        traveler_id = upsert_traveler(conn, tenant_id, pax_customer_id, pax)
        tnos = ','.join(parsed.get('ticket_numbers', [])) if parsed.get('ticket_numbers') else None
        conn.execute("""INSERT INTO order_passengers(tenant_id,order_id,traveler_id,pax_ref,full_name,traveler_type,phone,email,loyalty_id,ticket_numbers,raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (tenant_id, order_id, traveler_id, pax.get('pax_ref'), pax.get('full_name'), pax.get('type'), pax.get('phone'), pax.get('email'), pax.get('loyalty_id'), tnos, json.dumps(pax, default=str)))
    for svc in parsed.get('services', []):
        conn.execute("INSERT INTO order_services(tenant_id,order_id,kind,code,text,raw_json) VALUES (?,?,?,?,?,?)",
                     (tenant_id, order_id, svc.get('kind'), svc.get('code'), svc.get('text'), json.dumps(svc, default=str)))

    # auto tasks
    if ticket_status != 'ticketed':
        due = parsed.get('tktl')
        ensure_task(conn, tenant_id, order_id, 'ticketing', f"Ticket order {pnr or source_id}", due_at=due, priority='high' if due else 'medium')
    for seg in parsed.get('segments', []):
        if seg.get('depart_date'):
            ensure_task(conn, tenant_id, order_id, 'departure', f"Departure {seg.get('origin')}-{seg.get('destination')} {seg.get('depart_date')}", due_at=seg.get('depart_date'), priority='low')
            break
    _recompute_order(conn, tenant_id, order_id)
    return action, order_id


def import_air_file_path(conn, tenant_id, path, connector_id=None, user_id=None):
    return 'error', None, EXTERNAL_IMPORT_MSG
    try:
        parsed = parse_air_file(path)
    except Exception as e:
        return 'error', None, str(e)
    if not (parsed.get('raw_text') or '').strip():
        return 'skipped', None, None
    # raw vault
    with open(path, 'rb') as f:
        raw_bytes = f.read()
    text = clean_text(raw_bytes)
    h = parsed.get('content_hash')
    existing_raw = q(conn, "SELECT id FROM raw_documents WHERE tenant_id=? AND content_hash=? LIMIT 1", (tenant_id, h), one=True)
    if existing_raw:
        raw_id = existing_raw['id']
    else:
        cur = conn.execute("INSERT INTO raw_documents(tenant_id,connector_id,source_type,source_ref,filename,content_text,content_hash,received_at,meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
                           (tenant_id, connector_id, 'amadeus_air', parsed.get('pnr') or os.path.basename(path), os.path.basename(path), text, h, now_iso(), json.dumps({'path': path, 'size': len(raw_bytes)})))
        raw_id = cur.lastrowid
    action, order_id = import_air_parsed(conn, tenant_id, parsed, source_type='amadeus_air', connector_id=connector_id, user_id=user_id, raw_document_id=raw_id)
    return action, order_id, None


def scan_and_import_air(conn, tenant_id, folder, connector_id=None, user_id=None, limit=None):
    raw_files = glob.glob(os.path.join(folder, '*.AIR')) + glob.glob(os.path.join(folder, '*.air')) + glob.glob(os.path.join(folder, '*.Air'))
    seen = set()
    files = []
    for p in sorted(raw_files):
        k = os.path.normcase(os.path.abspath(p))
        if k in seen:
            continue
        seen.add(k)
        files.append(p)
    if limit:
        files = files[:limit]
    stats = {'total': len(files), 'imported': 0, 'updated': 0, 'skipped': 0, 'errors': 0, 'logs': []}
    ts = now_iso()
    cur = conn.execute("INSERT INTO import_jobs(tenant_id,connector_id,source_type,status,total,started_at) VALUES (?,?,?,?,?,?)",
                       (tenant_id, connector_id, 'amadeus_air', 'running', len(files), ts))
    job_id = cur.lastrowid
    for path in files:
        action, oid, err = import_air_file_path(conn, tenant_id, path, connector_id=connector_id, user_id=user_id)
        if err:
            stats['errors'] += 1
            stats['logs'].append({'file': os.path.basename(path), 'status': 'error', 'error': err})
        else:
            stats[action] = stats.get(action, 0) + 1
            stats['logs'].append({'file': os.path.basename(path), 'status': action, 'order_id': oid})
    if connector_id:
        conn.execute("UPDATE connectors SET last_sync_at=? WHERE id=?", (now_iso(), connector_id))
    conn.execute("UPDATE import_jobs SET status=?, imported=?, updated=?, skipped=?, errors=?, log_json=?, finished_at=? WHERE id=?",
                 ('completed', stats['imported'], stats['updated'], stats['skipped'], stats['errors'], json.dumps(stats['logs'][-200:], default=str), now_iso(), job_id))
    log_audit(conn, tenant_id, user_id, 'import_scan', 'connector', connector_id, stats)
    stats['job_id'] = job_id
    return stats
