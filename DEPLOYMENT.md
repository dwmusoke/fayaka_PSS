# Deployment (Fayaka Airline PSS)

Current local setup: Python stdlib + SQLite (`data/erp.db`), served by `python server.py`.

Recommended production setup ("better than one box"):

- **Database:** Supabase PostgreSQL. Set `DATABASE_URL` (or `SUPABASE_DB_URL`) to the Supabase pooled connection string. `db.py` detects a `postgres://`/`postgresql://` URL and switches from SQLite to psycopg automatically; the schema and seed run on startup. Install `pip install -r requirements.txt` (includes `psycopg[binary]`).
- **API / backend:** a container host (Railway, Render, Fly.io, or Cloud Run) running `python server.py` (Dockerfile/Procfile included). Vercel does not run long-lived Python HTTP servers, so the API stays on a container host.
- **Frontend:** static files in `static/` deployed to Vercel (`vercel.json` included). Set `window.API_BASE = 'https://<your-api-host>'` in a small inline script before the app scripts, or keep same-origin if serving everything from the API host.
- **Public URLs:** `/portal/<tenant-code>` (online booking), `/agency/<token>` (direct e-ticket desk), `/share/<token>` (document share).

All PSS writes are tenant-scoped, traced in `pss_trace`, and seat allocation uses a conditional atomic decrement (`available_seats > 0`) so two channels cannot oversell the same flight.

Login: tenant `fayaka` (alias of `demo`), user `admin`, pass `admin`.
