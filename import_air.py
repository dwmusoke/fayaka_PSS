import os
import sys
from db import init_db, db, q
from ingest import scan_and_import_air


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(__file__))
    tenant_code = sys.argv[2] if len(sys.argv) > 2 else 'demo'
    folder = os.path.abspath(folder)
    init_db()
    with db() as conn:
        t = q(conn, "SELECT id FROM tenants WHERE code=?", (tenant_code,), one=True)
        if not t:
            print('Tenant not found:', tenant_code)
            sys.exit(1)
        tenant_id = t['id']
        c = q(conn, "SELECT id FROM connectors WHERE tenant_id=? AND type='amadeus_air' LIMIT 1", (tenant_id,), one=True)
        connector_id = c['id'] if c else None
        stats = scan_and_import_air(conn, tenant_id, folder, connector_id=connector_id, user_id=None)
    print('Import complete:', {k: v for k, v in stats.items() if k != 'logs'})
    print('Sample:', stats.get('logs', [])[:5])


if __name__ == '__main__':
    main()
