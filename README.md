# Fayaka Airline - Passenger Service System

Fayaka Airline PSS: multi-tenant airline reservations, distribution and travel ERP. The **Airline PSS** front end covers reservations, inventory, ticket stock, check-in, payments and distribution channels across airports; the existing travel ERP back office provides accounting, CRM, documents and subscriptions. Built with Python stdlib + SQLite.

## Online portal booking features
- **Trip types:** one-way, return and multi-city (dynamic segment rows), plus a charter quote request form.
- **Passengers:** adults, children and infants counters; per-passenger name, type and document number.
- **Fares:** economy/premium/business/first cabin multipliers, taxes and ancillary extras (baggage, seat, meal, legroom, flight time, lounge).
- **Availability:** only flights with seats are shown; seat decrement is atomic (`available_seats > 0`) so no overbooking across portal, agency or counter.
- **One PNR per trip:** all passengers and segments share a traceable PNR; manage by PNR + surname shows every segment, payment, ancillary, check-in and trace event.
- **Payments:** pay the whole PNR with Mobile Money / card / bank / cash reference.
- **Disruptions:** active disruptions appear as a banner on the portal.

## Roles & access
- **Airline admin** (`owner`) - full access to every module, setup and subscriptions.
- **Airline accountant** (`fayaka-accounts`) - finance, invoicing, bills, expenses, journals, analytics and documents only; no operational PSS writes.
- **Airline reservations** (`fayaka-reservations`) - flights, PNRs, stock, ticketing, charter, customers and documents.
- **Airline operations / check-in** (`fayaka-ops`, `desk-kla`, `office-kla`, `office-dxb`, `office-nbo`) - flights, bookings, check-in, weather, airport requirements, disruptions and traceability scoped to their station.
- **Agency admin** (`partner-agy-sky`) - own agency team, own bookings, workspace and shared e-tickets; cannot see accounting, finance or other agencies.
- **Agency team** (`partner-agy-sky-team`) - own agency bookings and workspace only.
Navigation and APIs are both gated by role; agency and station logins are additionally scoped to their agency or airport.

### Authentication & access notes
- Seeded credentials are **random, generated at first start** and printed to the server console (stderr) — change them immediately via **Change password** (sidebar) or `POST /api/auth/change-password`.
- All state-changing API calls require a `X-CSRF-Token` header that matches the `csrftoken` cookie (set when any page is fetched). curl/scripts: read the cookie and send it back as the header.
- Login is rate-limited and locked out after 5 failed attempts in 15 minutes.
- Airline sessions idle-timeout after 25 minutes (max 7 days) and are pinned to the client IP/User-Agent.
- Passwords are stored as PBKDF2-SHA256 with a per-user salt (210,000 iterations). Legacy seeded hashes are migrated at startup.
- Set `SECURE_COOKIES=0` only when running over plain http for internal dev; otherwise cookies are sent with the `Secure` flag.

## Access URLs & logins
- **Main PSS / back office:** `http://127.0.0.1:8000/` — tenant `fayaka`; admin credentials are the seeded ones printed at first start (change via Change password).
- **Public online booking portal:** `http://127.0.0.1:8000/portal/fayaka` (flight search, book, pay, manage PNR, disruptions).
- **Travel-agency direct e-ticket desk:** open **Direct E-tickets**, issue an offer to an agency, and share the generated `/agency/<token>` link. Agencies book directly against allocated stock; each booking is traced.
- **Partner agency login:** `partner-agy-sky` (admin) and `partner-agy-sky-team` (team) — seeded credentials printed at first start. Sees only that agency's bookings.
- **Airport / check-in desk logins:** `desk-kla`, `office-kla`, `office-dxb`, `office-nbo` — seeded credentials printed at first start. Scoped to their station.
- **Offices & branches:** manage airport offices, sales branches and check-in desks under **Offices & Branches**.

All bookings across portal, agency, counter and charter are written to `pss_trace` with a trace ID and appear in **Traceability**; ticket lifecycle states (instant, issued, flown, no_show, void, refunded) are managed under **Ticket Status**.

## Run
```bash
cd travel-erp
python server.py
# or: npm run dev
```
Open http://127.0.0.1:8000 (login: tenant `demo`; user `admin` — the seeded password is printed on first start).

## Navigation
- **Airline PSS** — PSS Overview, Flights & Inventory, PNR/Bookings, Ticket Stock, Check-in, Payment Tracking, Airports, Channels, Agency Direct, Direct E-tickets, Charter Business, Charter Flights and Traceability.
- **Dashboard** — live agency command center, KPIs, aging, departures and operations queue.
- **Travelers / Travel Items / Customers** — traveler profiles, all travel products and CRM.
- **Estimates / Invoices / Credit Notes / Payments / Bills / Expenses / Manual Journals** — complete sales-to-ledger workflow.
- **Agency Setup** — Agency Profile, Users, Chart Of Accounts, Products, Currencies, Contacts, Business Rules, Preferences, Mail Templates, Integrations.
- **Airline Finance Operations** — Refund Management, Coupon Reporting, Vouchers, Cash & Bank, Bank Transactions and Reconciliations.
- **Control Center** — document sharing, imports, operations, analytics and subscription admin.

Every list includes 3-dot row actions for edit/delete and, where relevant, PDF, share, pay, convert, test and preview.

## Distribution & charter channels
- **Agency Direct (Amadeus-style e-ticket sharing):** issue a direct e-ticket offer to a travel agency; it generates a private `/agency/<token>` booking desk where the agency books passengers, receives an e-ticket from allocated stock and sees all bookings on that offer.
- **Online Booking Portal:** public `/portal/demo` site for flight search, instant booking, payment and PNR + surname management. Every event carries a trace reference.
- **Charter Business:** charter requests, quotations, confirmation with contract/flight details, passenger manifest and completion/settlement, with a dedicated charter flight register.
- **Traceability:** one `pss_trace` log records booking, ticketing, payment, check-in, stock allocation, agency offers and charter events across portal, agency, counter and charter channels; trace IDs are visible on PNRs and in Traceability view.

## Airline PSS workflows
1. **Flights & Inventory** — create schedules with route, date, times, aircraft, seats and base fare; availability decrements on booking.
2. **Ticket Stock** — define stock pools with serial ranges; allocate ranges to channels/airports; issue tickets from the next available serial (`UG` + 8-digit serial).
3. **PNR / Bookings** — create a booking against a flight and channel, issue an e-ticket from allocated stock, then check in the passenger.
4. **Check-in** — counter/kiosk/online/mobile check-in with seat, bag tags and boarding pass PDF/share link.
5. **Payment Tracking** — record cash/card/Mobile Money/bank payments by channel; booking payment status updates from captured totals.
6. **Channel Management** — per-channel commission, settlement days, currency and allowed airports; channel revenue visible on the PSS overview.
7. **Airports** — IATA/ICAO station master with city, country, timezone, terminal, region and hub flag, used across stock, bookings and channels.
8. **Documents** — branded e-ticket and boarding pass PDFs plus public token-based share links with live activity.

## Core features
- **SaaS:** tenants, branches, users/roles, subscription plans & usage limits, audit log.
- **Subscription billing:** Flutterwave hosted checkout with Mobile Money first (`payment_options=mobilemoney,card,banktransfer,ussd`), verify and webhook handling (`/api/billing/flutterwave/webhook`), plan activation on success.
- **Sources/Connectors:** Amadeus `.AIR`, GDS, NDC, airline portal, BSP/ARC connectors, raw document vault and import jobs.
- **Bookings/Items:** canonical orders, segments, passengers, SSR/remarks, TKTL/TKOK and status history.
- **CRM:** customers, travelers, statements, documents and share links.
- **Finance:** COA, AR/AP aging, bills, expenses, journals, trial balance and P&L.
- **Backoffice:** ticketing and operations tasks, SLA queues, departures and unticketed queues.
- **Analytics:** KPIs by source/channel/airline/agent with drill-down to source records.
- **Airline finance operations:** refund pipeline with policy checks and 24h auto-void, coupon/cancellation reporting with pre-billing control, service vouchers, multi-account cash & bank register with transaction matching, and BSP/bank/card/supplier/LCC reconciliations with variance tracking. These feed the Intelligence dashboard alongside receivables, unused tickets and disruptions.
- **Traveler contacts:** every traveler is linked to their own customer record carrying their email and phone (imported from source or captured in the profile); a **Sync customers** action repairs legacy shared-customer links.

## Travelers workflow
- Directory: All / No Plan / Scheduled / On trip filters, sorting by name/collection/nationality/next travel, free search and advanced search.
- Profile form: Main (identity, contact, passport, address, notes) and Preferences (flight, hotel, car and loyalty preferences) tabs.
- Overview: toolbar (edit, new travel item, email, duplicate, active/inactive, delete), traveler card, collection analysis by product and event indicators.
- List actions: bulk active/inactive/delete, CSV export/import and row-level statements.
- Transactions: add Flight/Hotel/Car/Misc items from the traveler, then save/print a branded statement PDF for a date range or email it via the configured SMTP integration.

## Agency Setup
All setup screens are persisted end to end through `/api/setup/*` CRUD endpoints.

- **Agency Profile:** legal/company details, registration, tax/VAT, address, currency, timezone, fiscal year and IATA code.
- **Users:** create/edit/delete users with roles, status and password updates. Existing seeded logins keep working.
- **Chart Of Accounts:** seeded with the supplied 49-account travel COA. Columns: Tips, Account Number, Name, Type, Balance, FCY. Balances are computed from journals.
- **Products:** core flight, hotel, car rental, miscellaneous travel and BSP products; supports supplier, price, type and inventory flag.
- **Currencies:** multi-currency setup with rates, decimals, base-currency marker and active state.
- **Contacts:** supplier/contact records seeded with Bsp, Car Rental, Flight, Hotel and Misc. Travel using the supplied types and Active/FCY columns.
- **Business Rules:** persisted policies. `invoice_due_days` controls default invoice due dates; `auto_ticket_task` creates a ticketing task when a manual order is created.
- **Preferences:** date/time formats, timezone, decimals, page size and dashboard/PDF display defaults.
- **Mail Templates:** seeded booking, invoice, receipt, credit note, estimate and task templates with live `{{placeholder}}` preview.
- **Integrations:** Flutterwave, Amadeus AIR, GDS, SMTP, accounting export and e-ticket forwarding. Credentials are masked in the UI; **Test connection** reports readiness and saves connection status.

## Documents, PDFs and sharing
- Custom branding: company name, legal name, location, address, phone, email, tax ID, logo, colors and footer note.
- Branded PDFs for bookings, invoices, bills and customer statements via `/api/documents/pdf/<kind>/<id>`.
- Token-based client share links with permission, expiry, view tracking and live activity at `/share/<token>`.
- Shared PDFs accept `?token=<token>` so clients can open branded documents without login.
- PDF generation requires `reportlab` (install with `pip install reportlab` if unavailable).

## Flutterwave Mobile Money (subscriptions)
1. Go to **Admin & Subscription → Payment Gateway - Flutterwave**.
2. Set Public/Secret (and optional Encryption/Webhook secret), mode (test/live), currency/country (e.g. UGX/UG), `payment_options` including `mobilemoney`, and enable gateway.
3. Use **Pay Subscription** to create an invoice and initialize payment (opens Flutterwave checkout with Mobile Money). After payment, click **Verify Payment** by `tx_ref` or rely on webhook.
4. Webhook: POST `/api/billing/flutterwave/webhook` with Flutterwave `verif-hash` matching your webhook secret.

## Import existing `.AIR` files
Use **Sources & Import → Scan & Import** (workspace) or:
```bash
python import_air.py ../
```

## Data & deployment
Local development uses SQLite. For Supabase/PostgreSQL set `DATABASE_URL` (see `.env.example` and `DEPLOYMENT.md`); psycopg is installed from `requirements.txt`. Static front end is ready for Vercel via `vercel.json`; the Python API runs on a container host (Dockerfile/Procfile).
