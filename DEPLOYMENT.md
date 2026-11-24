# Deployment (Fayaka Airline PSS)

Current local setup: Python stdlib + SQLite (`data/erp.db`), served by `python server.py`.

Recommended production setup ("better than one box"):

- **Database:** Railway Postgres or Supabase PostgreSQL. Set `DATABASE_URL` (or `SUPABASE_DB_URL`). `db.py` detects `postgres://`/`postgresql://` and uses psycopg; schema/seed/migrations run on startup. On Railway (`RAILWAY_ENVIRONMENT`/`RAILWAY_PROJECT_ID`) or when `REQUIRE_POSTGRES=1`, startup **fails fast** if Postgres is not configured, preventing ephemeral SQLite data loss.
- **API / backend:** Railway/container host running `python server.py` (Dockerfile/Procfile included). Vercel does not run long-lived Python HTTP servers, so the API stays on a container host.
- **Frontend:** static files in `static/` deployed to Vercel (`vercel.json` included). Set `window.API_BASE = 'https://<your-api-host>'` in a small inline script before the app scripts, or keep same-origin if serving everything from the API host.
- **Public URLs:** `/portal/<tenant-code>` (online booking), `/agency/<token>` (direct e-ticket desk), `/share/<token>` (document share).

All PSS writes are tenant-scoped, traced in `pss_trace`, and seat allocation uses a conditional atomic decrement (`available_seats > 0`) so two channels cannot oversell the same flight.

Login: tenant `fayaka` (alias of `demo`), user `admin`, pass `admin`.
