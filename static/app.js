const state = { session: null, plan: null, usage: null, branding: null, cache: {}, menuSeq: 0, pages: {} };
const LIST_PAGES = {};
const PAGE_SIZE = 12;
const INVENTORY_UI = { tab: 'dates', date: '', pos: 'ALL', subclass: 'ALL', poll: null, pollKey: '', cache: {} };
function stopInventoryPoll(){ if(INVENTORY_UI.poll){ clearInterval(INVENTORY_UI.poll); INVENTORY_UI.poll = null; } }

const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const fmt = (n) => (n===null||n===undefined||n==='') ? '-' : (typeof n==='number' ? n.toLocaleString(undefined,{maximumFractionDigits:2}) : n);
const defaultCurrency = ()=> (state.branding && state.branding.currency) || (state.session && state.session.currency) || 'USD';
const money = (n, c) => { const cur = c || defaultCurrency(); return (n===null||n===undefined) ? '-' : `${cur} ${Number(n).toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2})}`; };
const esc = (s) => String(s??'').replace(/[&<>"']/g, m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
const icon = (n) => `<svg class="icon"><use href="#i-${n}"></use></svg>`;
const today = () => new Date().toISOString().slice(0,10);

function getCsrf(){
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return m ? m[1] : '';
}

async function api(path, opts={}) {
  const res = await fetch((window.API_BASE||'')+path, { headers: {'Content-Type':'application/json', 'X-CSRF-Token': getCsrf(), ...(opts.headers||{})}, credentials:'same-origin', ...opts });
  const txt = await res.text();
  let data; try { data = txt ? JSON.parse(txt) : {}; } catch(e) { data = {raw: txt}; }
  if (!res.ok) { const err = new Error(`${data.error || res.statusText || 'Request failed'} (${path})`); err.status = res.status; throw err; }
  return data;
}

function toast(msg, type='ok'){
  const el = $('#global-alert');
  el.className = `global-alert ${type==='error'?'':'hidden'}`;
  el.textContent = msg;
  if(type==='ok'){ el.classList.remove('hidden'); setTimeout(()=>el.classList.add('hidden'), 3500); }
}

function showModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
function hideModal(){ $('#modal').classList.add('hidden'); }

function statusBadge(status){
  const s = String(status||'').toLowerCase();
  const good = ['paid','received','active','ticketed','completed','successful','posted','closed','checked_in','boarded','screened','loaded','arrived','claimed'];
  const warn = ['pending','partial','pending_payment','in_progress','open','ticketing_pending','initiated','gate_closed','not_boarded','in_transit','tagged'];
  const bad = ['failed','overdue','cancelled','void','unticketed','revoked','no_show','missing'];
  const cls = good.includes(s)?'good':warn.includes(s)?'warn':bad.includes(s)?'bad':'info';
  return `<span class="badge ${cls}">${esc(status||'-')}</span>`;
}
function badgeForTicket(st){
  if(st==='ticketed') return '<span class="badge good">ticketed</span>';
  if(st==='ticketing_pending') return '<span class="badge warn">pending</span>';
  return '<span class="badge bad">unticketed</span>';
}
function badgeForSource(st){
  const map={amadeus_air:'Amadeus AIR', gds_amadeus:'Amadeus GDS', gds_sabre:'Sabre', gds_travelport:'Travelport', ndc:'NDC', airline_portal:'Airline Portal', manual:'Manual', bsp_arc:'BSP/ARC'};
  return `<span class="badge info">${esc(map[st]||st||'-')}</span>`;
}

function actionMenu(items){
  const id = `act-${++state.menuSeq}`;
  return `<div class="menu action-cell"><button class="action-btn icon-btn" data-menu-toggle="${id}" aria-label="Actions">${icon('dots')}</button><div class="action-pop" id="${id}">${items.map(x=>`<button data-action="${x.action}" data-id="${esc(x.id??'')}" data-extra="${esc(x.extra??'')}" class="${x.danger?'danger':''}">${esc(x.label)}</button>`).join('')}</div></div>`;
}
function table(rows, cols, cellFn, colsExtra=''){
  if(!rows || !rows.length) return `<div class="empty">No records found</div>`;
  const head = `<tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}${colsExtra?'<th></th>':''}</tr>`;
  const body = rows.map(r=>`<tr>${cols.map(c=>`<td>${cellFn?cellFn(r,c):esc(r[c])}</td>`).join('')}${colsExtra?`<td>${colsExtra(r)}</td>`:''}</tr>`).join('');
  return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

function pager(view, page, total, count){
  if(total <= 1) return '';
  const buttons = [];
  for(let p=1;p<=total;p++){
    if(p===1 || p===total || Math.abs(p-page)<=2){
      buttons.push(`<button class="${p===page?'active':''}" data-action="page" data-id="${p}" data-extra="${view}">${p}</button>`);
    } else if(buttons.length && String(buttons[buttons.length-1]).indexOf('ellipsis') === -1) {
      buttons.push(`<span class="pager-ellipsis">...</span>`);
    }
  }
  return `<div class="pager"><span class="muted tiny">Page ${page} of ${total} - ${count} records</span><div class="pager-buttons">${buttons.join('')}</div></div>`;
}

function panel(title, body, actions='', subtitle=''){
  return `<section class="panel"><div class="panel-head"><div><h3>${title}</h3>${subtitle?`<div class="muted tiny">${subtitle}</div>`:''}</div><div>${actions}</div></div><div class="panel-body">${body}</div></section>`;
}
function kpi(label, value, ic, note='', warn=false, tone=''){
  return `<div class="kpi-card${tone?` tone-${tone}`:''}"><div class="kpi-top"><span class="kpi-label">${esc(label)}</span><span class="kpi-icon">${icon(ic)}</span></div><div class="kpi-value">${esc(value)}</div>${note?`<div class="trend ${warn?'warn':''}">${esc(note)}</div>`:''}</div>`;
}
function pageHead(title, subtitle, actions=''){
  return `<div class="page-head"><div><h2>${title}</h2><p>${subtitle}</p></div><div class="page-actions">${actions}</div></div>`;
}
const REPORT_VIEWS = new Set(['pss-bookings','pss-flights','flight-inventory','pss-stock','pss-checkins','pss-payments',
  'payments','travelers','orders','crm','invoicing','estimates','credit-notes','bills','expenses','journals',
  'documents','shares','notifications','refunds','coupons','vouchers','bank-accounts','bank-transactions',
  'reconciliations','pss-agencies','pss-ancillaries','pss-ancillary-sales','pss-charters','pss-charter-flights',
  'pss-trace','pss-direct','pss-eticket-tracker','pss-ticket-status','pss-aftersales','pss-airports','pss-baggage',
  'pss-airport-docs','pss-weather','pss-branches','pss-workspace','pss-partner-users','partners','suppliers',
  'setup-contacts','sources','backoffice','tasks','setup-users','setup-chart','setup-products','setup-currencies',
  'setup-rules','setup-preferences','setup-mail','setup-integrations','loyalty','partner-reports']);
function reportUrl(view, format='csv'){
  const qs = new URLSearchParams({entity:view, format});
  return (window.API_BASE||'') + '/api/reports/export?' + qs.toString();
}
function exportReport(view, format){
  if(!REPORT_VIEWS.has(view)) return;
  const a = document.createElement('a');
  a.href = reportUrl(view, format);
  a.style.display = 'none';
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}
function shareReport(view){
  if(!REPORT_VIEWS.has(view)) return;
  const title = (viewMeta[view]||[view])[0];
  const url = location.origin + reportUrl(view, 'xlsx');
  showModal('Share report', `<form class="form-grid"><div><label>Shareable report link</label><input value="${esc(url)}" readonly></div>
    <div class="muted tiny">The recipient must be signed in with permission to view this report.</div>
    ${shareButtons(url, title)}
    <div class="cta-row"><button type="button" class="secondary mini-btn" onclick="hideModal()">Close</button></div></form>`);
}
function addReportButtons(view, root){
  if(!REPORT_VIEWS.has(view)) return;
  const head = $('.page-head .page-actions', root||document);
  if(!head || $('[data-report-export]', head)) return;
  head.insertAdjacentHTML('beforeend',
    `<button class="mini-btn secondary" data-report-export="csv" title="Download CSV report">CSV</button>
     <button class="mini-btn secondary" data-report-export="xlsx" title="Download Excel report">Excel</button>
     <button class="mini-btn secondary" data-report-share title="Share report link">Share report</button>`);
  $$('[data-report-export]', head).forEach(btn=>{ btn.onclick = ()=>exportReport(view, btn.getAttribute('data-report-export')); });
  const share = $('[data-report-share]', head);
  if(share) share.onclick = ()=>shareReport(view);
}
function watchReportButtons(view, el){
  if(!el || el.__reportWatcher || !REPORT_VIEWS.has(view)) return;
  el.__reportWatcher = new MutationObserver(()=>addReportButtons(view, el));
  el.__reportWatcher.observe(el, {childList:true, subtree:true});
}

const viewMeta = {
  'pss-overview': ['PSS Overview','Airline reservations, inventory, stock and operations command center','Airline PSS'],
  'pss-flights': ['Flights & Inventory','Schedules, seats, fares and flight status','Airline PSS'],
  'flight-inventory': ['Flight Inventory','Flight-date legs, segments, availability, valuation and uncommitted bookings','Airline PSS'],
  'pss-bookings': ['PNR / Bookings','Passenger bookings, ticketing and e-tickets','Airline PSS'],
  'pss-stock': ['Ticket Stock','Ticket stock pools, channel allocations and serial control','Airline PSS'],
  'agency-wallet': ['Agency Wallet','Agency stock allocations, remaining ticket serials and settlement view','Airline PSS'],
  'seat-hold': ['Seat Hold','Temporary seat reservations with release and availability control','Airline PSS'],
  'pss-checkins': ['Check-in','Counter, kiosk and mobile check-in with boarding passes','Airline PSS'],
  'pss-payments': ['Payment Tracking','Payments, collections and channel settlement','Airline PSS'],
  'pss-airports': ['Airports','Station and airport master across your network','Airline PSS'],
  'pss-channels': ['Channels','Distribution channels, commissions and airport access','Airline PSS'],
  'pss-agencies': ['Agency Direct','Travel agencies booking directly from airline e-ticket stock','Airline PSS'],
  'pss-direct': ['Direct E-tickets','Airline-issued e-tickets shared directly with agencies','Airline PSS'],
  'pss-charters': ['Charter Business','Charter requests, quotes, contracts and passenger manifests','Airline PSS'],
  'pss-charter-flights': ['Charter Flights','Operating charter flights and settlement status','Airline PSS'],
  'pss-trace': ['Traceability','End-to-end trace log across portal, agency, counter and charter','Airline PSS'],
  'pss-ancillaries': ['Ancillary Catalog','Baggage, seats, meals, legroom, flight time and other auxiliary products','Airline PSS'],
  'pss-ancillary-sales': ['Ancillary Sales','Auxiliary revenue sold against bookings','Airline PSS'],
  'pss-baggage': ['Baggage Rules','Free allowance, excess rates and maximum weight per flight','Airline PSS'],
  'pss-airport-docs': ['Airport Requirements','Travel documents and airport-specific entry requirements','Airline PSS'],
  'pss-weather': ['Weather','Airport weather conditions and operational advisories','Airline PSS'],
  'pss-aftersales': ['After Sales / Reputation','Service cases, complaints, feedback and satisfaction tracking','Airline PSS'],
  'pss-eticket-tracker': ['E-ticketing Tracker','Ticket issuance status, stock and trace events per PNR','Airline PSS'],
  'pss-ticket-status': ['Ticket Status','Instant, issued, flown, no-show and other ticket lifecycle states','Airline PSS'],
  'pss-workspace': ['Partner Workspace','Agency, office and station context with live bookings','Airline PSS'],
  'pss-partner-users': ['Partner Logins','Travel-agency and airport desk user accounts','Airline PSS'],
  'partner-reports': ['Partner Reports','Agency login, booking and settlement performance','Airline PSS'],
  'pss-branches': ['Offices & Branches','Airport offices, sales branches and check-in desks','Airline PSS'],
  dashboard: ['Dashboard','Live command center for your travel business','Overview'],
  travelers: ['Travelers','Traveler profiles, documents and journey history','Travel'],
  orders: ['Travel Items','Unified flight, hotel, car and miscellaneous travel items','Travel'],
  'unused-tickets': ['Unused Tickets','Unused e-ticket tracking, refunds and expiry control','Travel'],
  'fare-optimization': ['Fare Optimization','Automatic fare adjustments from load factor and departure pressure','Revenue'],
  crm: ['Customers','Corporate accounts, B2B clients and traveler relationships','Customer & Sales'],
  estimates: ['Estimates','Quotes and proposals before ticket issuance','Customer & Sales'],
  partners: ['Partners','Travel partner and agency management','Customer & Sales'],
  suppliers: ['Suppliers','Supplier and billing entity contacts','Customer & Sales'],
  invoicing: ['Invoices','Auto-billing, AR, allocations and customer receipts','Finance'],
  'credit-notes': ['Credit Notes','Refunds, credits and invoice adjustments','Finance'],
  payments: ['Payments','Cash, bank, card and Mobile Money receipts','Finance'],
  bills: ['Bills','Supplier payables, reconciliation and payment control','Finance'],
  expenses: ['Expenses','Operating costs with receipts and GL postings','Finance'],
  journals: ['Manual Journals','Double-entry adjustments and transfers','Finance'],
  'agency-profile': ['Agency Profile','Company identity, branding and fiscal defaults','Agency Setup'],
  'setup-users': ['Users','Team access, roles and status','Agency Setup'],
  'setup-chart': ['Chart Of Accounts','Account numbers, names, types, balances and FCY','Agency Setup'],
  'setup-products': ['Products','Travel products, suppliers and inventory settings','Agency Setup'],
  'setup-currencies': ['Currencies','Multi-currency rates and base currency','Agency Setup'],
  'setup-contacts': ['Contacts','Suppliers, billing entities and contact records','Agency Setup'],
  'setup-rules': ['Business Rules','Automation and control policies','Agency Setup'],
  'setup-preferences': ['Preferences','Dates, formats, themes and workspace defaults','Agency Setup'],
  'setup-mail': ['Mail Templates','Reusable customer and supplier messages','Agency Setup'],
  'setup-integrations': ['Integrations','Payment, import, mail and accounting connections','Agency Setup'],
  finance: ['Chart of Accounts & Finance','Accounting structure, trial balance and financial control','Control Center'],
  documents: ['Documents & Sharing','Branded travel documents, files, client links and live activity','Control Center'],
  shares: ['Document Shares','Active client links, recipients, permissions and view activity','Control Center'],
  sources: ['Sources & Import','GDS, LCC, NDC, portal feeds and raw document vault','Control Center'],
  backoffice: ['Operations Queue','Ticketing, refunds, SLA tasks and daily workflow','Control Center'],
  notifications: ['Notifications','Alerts, reminders and operational notices','Control Center'],
  disruptions: ['Disruptions','Schedule changes, cancellations and recovery handling','Control Center'],
  dispatch: ['Dispatch','Airport check-in control, charter flight coordination, schedule changes and recovery','Control Center'],
  intelligence: ['Intelligence','Consolidated insights for revenue, partners, unused tickets and disruptions','Control Center'],
  analytics: ['MIS & Analytics','Profitability, productivity and channel intelligence','Control Center'],
  revenue: ['Revenue Dashboard','Gross revenue, cash collected, ancillary sales, refunds and channel performance','Finance'],
  developer: ['Developer Center','Partner API keys, webhooks, delivery logs and API documentation','Control Center'],
  onboarding: ['Airline Onboarding','Guided setup for a new airline tenant and readiness checklist','Setup'],
  refunds: ['Refund Management','GDS/LCC refunds, policy checks, auto-void and refund tracking','Airline Finance'],
  coupons: ['Coupon Reporting','Coupon creation, cancellations and pre-billing control','Airline Finance'],
  loyalty: ['Loyalty Reporting','Frequent traveler IDs, coupon activity and customer value','Airline Finance'],
  vouchers: ['Vouchers','Hotel, car, transfer and service vouchers with supplier data','Airline Finance'],
  'bank-accounts': ['Cash & Bank','Cash, bank and petty cash accounts with live balances','Airline Finance'],
  'bank-transactions': ['Bank Transactions','Receipts, payments, transfers and bank matching','Airline Finance'],
  reconciliations: ['Reconciliations','BSP, bank, card, supplier and LCC reconciliation control','Airline Finance'],
  admin: ['Admin & Subscription','Users, branches, plans, gateway and platform settings','Control Center']
};
const VIEW_API = {
  'pss-overview':'/api/pss/overview','pss-flights':'/api/pss/flights','pss-bookings':'/api/pss/bookings',
  'flight-inventory':'/api/pss/inventory/dates',
  'pss-checkins':'/api/pss/checkins','pss-payments':'/api/pss/payments','pss-agencies':'/api/pss/agencies','agency-wallet':'/api/pss/stock','seat-hold':'/api/pss/seat-holds',
  'pss-direct':'/api/pss/direct-etickets','pss-charters':'/api/pss/charters','pss-charter-flights':'/api/pss/charter-flights',
  'pss-trace':'/api/pss/trace','pss-eticket-tracker':'/api/pss/eticket-tracker','pss-aftersales':'/api/pss/service-cases',
  'pss-workspace':'/api/pss/partner-workspace','pss-partner-users':'/api/pss/partner-users','partner-reports':'/api/pss/partner-reports','dashboard':'/api/dashboard',
  travelers:'/api/travelers',orders:'/api/orders',crm:'/api/customers',estimates:'/api/estimates',invoicing:'/api/invoices',
  'credit-notes':'/api/credit-notes',bills:'/api/finance/bills',expenses:'/api/finance/expenses',journals:'/api/finance/journals',
  documents:'/api/documents',shares:'/api/shares',loyalty:'/api/loyalty/report',tasks:'/api/tasks',notifications:'/api/notifications',finance:'/api/finance/summary',
  analytics:'/api/analytics','bank-accounts':'/api/setup/bank-accounts','bank-transactions':'/api/setup/bank-transactions',
  reconciliations:'/api/setup/reconciliations',refunds:'/api/setup/refunds',coupons:'/api/setup/coupons',vouchers:'/api/setup/vouchers',
  intelligence:'/api/analytics',revenue:'/api/revenue/dashboard',developer:'/api/developer/api-keys',
  onboarding:'/api/onboarding/status',admin:'/api/admin/overview',sources:'/api/connectors',backoffice:'/api/tasks',
  disruptions:'/api/setup/disruptions',airports:'/api/setup/pss-airports',channels:'/api/setup/pss-channels','setup-users':'/api/setup/users',
  'setup-chart':'/api/setup/chart-of-accounts','setup-products':'/api/setup/products','setup-currencies':'/api/setup/currencies',
  'setup-contacts':'/api/setup/contacts','setup-rules':'/api/setup/business-rules','setup-preferences':'/api/setup/preferences',
  'setup-mail':'/api/setup/mail-templates','setup-integrations':'/api/setup/integrations','agency-profile':'/api/setup/agency-profile',
  'pss-airports':'/api/setup/pss-airports','pss-channels':'/api/setup/pss-channels','pss-ancillaries':'/api/pss/ancillary-sales',
  'pss-ancillary-sales':'/api/pss/ancillary-sales','pss-baggage':'/api/setup/pss-baggage-rules',
  'pss-airport-docs':'/api/pss/travel-documents','pss-weather':'/api/setup/pss-weather','pss-ticket-status':'/api/pss/eticket-tracker',
  'pss-branches':'/api/setup/branches','unused-tickets':'/api/setup/unused-tickets','fare-optimization':'/api/setup/fare-optimization',partners:'/api/setup/partners',suppliers:'/api/setup/contacts'
};
const DEFAULT_ACTIONS = [
  {label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Share',action:'share'},{label:'Delete',action:'delete',danger:true}
];
const VIEW_ACTIONS = {
  invoicing:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Duplicate',action:'duplicate'},{label:'PDF',action:'pdf',pdf:'invoice'},{label:'Share',action:'share'},{label:'Delete',action:'delete',danger:true}],
  payments:[{label:'View',action:'view'},{label:'Receipt',action:'receipt',pdf:'receipt'},{label:'Edit',action:'edit'},{label:'Share',action:'share'},{label:'Refund',action:'refund',danger:true}],
  'credit-notes':[{label:'View',action:'view'},{label:'PDF',action:'pdf',pdf:'credit_note'},{label:'Edit',action:'edit'},{label:'Share',action:'share'},{label:'Post',action:'post'}],
  estimates:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Duplicate',action:'duplicate'},{label:'PDF',action:'pdf',pdf:'estimate'},{label:'Send',action:'share'},{label:'Delete',action:'delete',danger:true}],
  documents:[{label:'View',action:'view'},{label:'Save',action:'edit'},{label:'Download',action:'download'},{label:'Share',action:'share'},{label:'Delete',action:'delete',danger:true}],
  shares:[{label:'Open',action:'open'},{label:'Copy link',action:'copy'},{label:'Revoke',action:'delete',danger:true}],
  refunds:[{label:'Review',action:'view'},{label:'Update',action:'edit'},{label:'Share',action:'share'},{label:'Approve',action:'approve'}],
  coupons:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Duplicate',action:'duplicate'},{label:'Share',action:'share'},{label:'Deactivate',action:'deactivate',danger:true}],
  vouchers:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Duplicate',action:'duplicate'},{label:'Share',action:'share'},{label:'Deactivate',action:'deactivate',danger:true}],
  'bank-accounts':[{label:'View',action:'view'},{label:'Update',action:'edit'},{label:'Share',action:'share'}],
  'bank-transactions':[{label:'View',action:'view'},{label:'Match',action:'match'},{label:'Reconcile',action:'reconcile'},{label:'Share',action:'share'}],
  reconciliations:[{label:'Review',action:'view'},{label:'Match',action:'match'},{label:'Post',action:'post'},{label:'Share',action:'share'}],
  orders:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Duplicate',action:'duplicate'},{label:'Invoice',action:'pdf',pdf:'order'},{label:'Share',action:'share'}],
  'pss-bookings':[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Ticket',action:'ticket'},{label:'Documents',action:'docs'},{label:'Share',action:'share'},{label:'Cancel',action:'cancel',danger:true}],
  'pss-payments':[{label:'View',action:'view'},{label:'Receipt',action:'receipt',pdf:'receipt'},{label:'Edit',action:'edit'},{label:'Share',action:'share'},{label:'Refund',action:'refund',danger:true}],
  crm:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Statement',action:'statement',pdf:'customer'},{label:'Share',action:'share'}],
  travelers:[{label:'View',action:'view'},{label:'Edit',action:'edit'},{label:'Statement',action:'statement',pdf:'traveler'},{label:'Share',action:'share'}]
};
const MODERN_RECORD_VIEWS = new Set(['invoicing','crm','orders','payments','pss-payments','refunds','suppliers','backoffice']);
const MODERN_RECORD_SPECS = {
  invoicing:{primary:['invoice_no','id'],secondary:['customer','customer_name','customer_email'],amount:['total','amount_due','balance'],status:['status','payment_status'],date:['issue_date','created_at'],chips:['currency','invoice_type']},
  crm:{primary:['name','company'],secondary:['email','phone','company','loyalty_id'],amount:['spend'],status:['type','status'],date:['created_at'],chips:['bookings','source']},
  orders:{primary:['pnr','order_no','id'],secondary:['customer_name','customer_email','flight_no'],amount:['total_sell','total'],status:['status','booking_status','ticket_status'],date:['travel_start','created_at'],route:['origin','destination'],chips:['source_type','order_number']},
  payments:{primary:['payment_no','reference','id'],secondary:['customer','payer','description','method'],amount:['amount'],status:['status','payment_status'],date:['paid_at','created_at'],chips:['method','currency']},
  'pss-payments':{primary:['payment_no','reference','id'],secondary:['customer','payer','description','method'],amount:['amount'],status:['status','payment_status'],date:['paid_at','created_at'],chips:['method','currency']},
  refunds:{primary:['refund_no','id'],secondary:['customer','invoice_no','reason','description'],amount:['amount'],status:['status'],date:['created_at','refunded_at'],chips:['method','currency']},
  suppliers:{primary:['name','contact_person'],secondary:['company','contact_person','email','phone'],status:['status','is_active'],date:['created_at'],chips:['contact_type','currency','terms']},
  backoffice:{primary:['title','task','name','id'],secondary:['description','customer','pnr','assignee'],status:['status','priority'],date:['due_at','created_at'],chips:['task_type','assignee','category']}
};
function pickValue(row, keys=[]){ for(const k of keys){ if(row && row[k]!==undefined && row[k]!==null && row[k]!=='') return row[k]; } return ''; }
function phoneDigits(value){ return String(value||'').replace(/\D/g,''); }
function recordInitials(value){ return String(value||'?').trim().split(/\s+/).filter(Boolean).slice(0,2).map(x=>x[0].toUpperCase()).join('') || '?'; }
function recordTone(row, spec){
  const s = String(pickValue(row, spec.status)||'').toLowerCase();
  if(['paid','received','active','ticketed','completed','successful','posted','closed','approved','matched','reconciled'].includes(s)) return 'good';
  if(['pending','partial','open','in_progress','initiated','draft','processing'].includes(s)) return 'warn';
  if(['failed','overdue','cancelled','void','refunded','missing','rejected'].includes(s)) return 'bad';
  return 'info';
}
function recordMode(name){
  try{ return localStorage.getItem('pss-record-mode-'+name)==='card' ? 'card' : 'list'; }catch(e){ return 'list'; }
}
function modernRecordRow(name, row, rid, ref, defs){
  const spec = MODERN_RECORD_SPECS[name] || {};
  const primary = pickValue(row, spec.primary) || ref || rid;
  const status = pickValue(row, spec.status);
  const statusHtml = status==='' ? '' : (typeof status==='boolean' ? `<span class="badge ${status?'good':'bad'}">${status?'active':'inactive'}</span>` : statusBadge(status));
  const secondary = (spec.secondary||[]).map(k=>row[k]).filter(v=>v!==undefined && v!==null && v!=='').map(v=>esc(v)).join(' · ');
  const rawAmount = pickValue(row, spec.amount);
  const amount = rawAmount==='' ? '' : Number(rawAmount);
  const amountHtml = amount!=='' && Number.isFinite(amount) ? `<div class="record-amount">${moneyCell(amount)}</div>` : '';
  const date = pickValue(row, spec.date);
  const route = spec.route && row[spec.route[0]] ? `${row[spec.route[0]]||''} → ${row[spec.route[1]]||''}` : '';
  const chips = [route, ...(spec.chips||[]).map(k=>row[k])].filter(v=>v!==undefined && v!==null && v!=='').map(v=>`<span>${esc(v)}</span>`).join('');
  const menuItems = defs.map(a=>`<button type="button" class="${a.danger?'danger':''}" data-action="${esc(a.action)}" data-id="${rid}" data-view="${esc(name)}" data-ref="${esc(ref)}"${a.pdf?` data-pdf="${esc(a.pdf)}"`:''}>${esc(a.label)}</button>`).join('');
  const act = `<div class="action-cell"><button type="button" class="icon-btn" data-action="menu" title="Actions" aria-label="Actions">⋮</button><div class="action-menu hidden">${menuItems}</div></div>`;
  return `<div class="record-row tone-${recordTone(row,spec)}" data-id="${rid}">
    <div class="record-mark">${esc(recordInitials(primary))}</div>
    <div class="record-main"><div class="record-top"><b>${esc(primary)}</b>${statusHtml}</div>
      <div class="record-sub">${secondary||'<span class="muted">No additional details</span>'}</div>
      ${chips?`<div class="record-chips">${chips}${date?`<span>${esc(date)}</span>`:''}</div>`:(date?`<div class="record-chips"><span>${esc(date)}</span></div>`:'')}
    </div>${amountHtml}${act}</div>`;
}
function modernRecordCard(name, row, rid, ref, defs){
  const spec = MODERN_RECORD_SPECS[name] || {};
  const primary = pickValue(row, spec.primary) || ref || rid;
  const status = pickValue(row, spec.status);
  const statusHtml = status==='' ? '' : (typeof status==='boolean' ? `<span class="badge ${status?'good':'bad'}">${status?'active':'inactive'}</span>` : statusBadge(status));
  const secondary = (spec.secondary||[]).map(k=>row[k]).filter(v=>v!==undefined && v!==null && v!=='').map(v=>esc(v)).join(' · ');
  const rawAmount = pickValue(row, spec.amount);
  const amount = rawAmount==='' ? '' : Number(rawAmount);
  const amountHtml = amount!=='' && Number.isFinite(amount) ? `<div class="record-amount">${moneyCell(amount)}</div>` : '';
  const date = pickValue(row, spec.date);
  const route = spec.route && row[spec.route[0]] ? `${row[spec.route[0]]||''} → ${row[spec.route[1]]||''}` : '';
  const chips = [route, ...(spec.chips||[]).map(k=>row[k])].filter(v=>v!==undefined && v!==null && v!=='').map(v=>`<span>${esc(v)}</span>`).join('');
  const menuItems = defs.map(a=>`<button type="button" class="${a.danger?'danger':''}" data-action="${esc(a.action)}" data-id="${rid}" data-view="${esc(name)}" data-ref="${esc(ref)}"${a.pdf?` data-pdf="${esc(a.pdf)}"`:''}>${esc(a.label)}</button>`).join('');
  const act = `<div class="action-cell"><button type="button" class="icon-btn" data-action="menu" title="Actions" aria-label="Actions">⋮</button><div class="action-menu hidden">${menuItems}</div></div>`;
  return `<div class="record-card tone-${recordTone(row,spec)}" data-id="${rid}">
    <div class="record-card-head"><div class="record-mark">${esc(recordInitials(primary))}</div><div class="record-card-title"><b>${esc(primary)}</b><span class="muted tiny">${esc(ref||rid)}</span></div>${act}</div>
    <div class="record-card-meta">${statusHtml}${amountHtml}</div>
    <div class="record-card-details">${secondary||'<span class="muted">No additional details</span>'}</div>
    ${chips||date?`<div class="record-chips">${chips}${date?`<span>${esc(date)}</span>`:''}</div>`:''}
  </div>`;
}
const CREATE_LABELS = {
  invoicing:'New invoice',payments:'Record payment','credit-notes':'New credit note',estimates:'New estimate',
  documents:'Add document',refunds:'New refund',coupons:'New coupon',vouchers:'New voucher',
  'bank-accounts':'Add account','bank-transactions':'Add transaction',reconciliations:'New reconciliation',
  orders:'New travel item','pss-bookings':'New booking','pss-payments':'Record payment',crm:'New customer',
  travelers:'New traveler','setup-users':'Add user'
};
const EDIT_LABELS = {
  invoicing:'Update invoice',payments:'Update payment','credit-notes':'Update credit note',estimates:'Update estimate',
  documents:'Save document',refunds:'Update refund',coupons:'Update coupon',vouchers:'Update voucher',
  'bank-accounts':'Update account','bank-transactions':'Update transaction',reconciliations:'Update reconciliation',
  orders:'Update travel item','pss-bookings':'Update booking','pss-payments':'Update payment',crm:'Update customer',
  travelers:'Update traveler','setup-users':'Update user'
};
const ACTION_STATUS = {post:'posted',approve:'approved',match:'matched',reconcile:'reconciled',deactivate:'inactive',refund:'refunded',cancel:'cancelled'};
function actionsFor(view){ return VIEW_ACTIONS[view] || DEFAULT_ACTIONS; }
async function fetchRowDetail(path, id, fallback, idKey='id', listData=null){
  try{
    const rd = await api(path+'/'+encodeURIComponent(id));
    if(rd && typeof rd==='object' && !Array.isArray(rd)){
      if(rd[idKey]!==undefined) return rd;
      const nested = Object.values(rd).find(v=>v && typeof v==='object' && !Array.isArray(v) && v[idKey]!==undefined);
      if(nested) return nested;
    }
  }catch(err){
    if(err.status >= 500) throw err;
  }
  const listKey = listData && typeof listData==='object' && !Array.isArray(listData) ? Object.keys(listData).find(k=>Array.isArray(listData[k])) : null;
  return (listKey && listData[listKey] ? listData[listKey].find(x=>String(x[idKey])===String(id)) : null) || fallback || {};
}
const FIELD_TYPES = {
  id:'hidden', created_at:'hidden', updated_at:'hidden', tenant_id:'hidden',
  agency_id:'hidden', channel_id:'hidden', airport_id:'hidden', customer_id:'hidden',
  booking_id:'hidden', order_id:'hidden', pool_id:'hidden', invoice_id:'hidden',
  payment_id:'hidden', task_id:'hidden', notification_id:'hidden', connector_id:'hidden',
  config_json:'hidden', password:'password', old_password:'password', new_password:'password',
  confirm_password:'password', start_date:'date', end_date:'date', travel_start:'date',
  travel_end:'date', due_date:'date', departure:'date', arrival:'date', dob:'date',
  issued_at:'date', valid_until:'date', expiry_date:'date', amount:'number', total:'number',
  balance:'number', fare:'number', tax:'number', commission:'number', margin:'number',
  sell:'number', cost:'number', paid:'number', limit:'number', seats:'number',
  available_seats:'number', capacity:'number', duration:'number', decimal:'number',
  rate:'number', percentage:'number', qty:'number', quantity:'number', min:'number',
  max:'number', step:'number', start_serial:'number', end_serial:'number', next_serial:'number',
  remaining:'number', is_active:'checkbox', active:'checkbox', status:'select',
  type:'select', location_type:'select', cabin:'select', currency:'select',
  payment_method:'select', source_type:'select', channel:'select', role:'select',
  priority:'select', urgency:'select', country:'select', city:'text', code:'text',
  name:'text', title:'text', subject:'text', description:'textarea', notes:'textarea',
  remarks:'textarea', address:'textarea', email:'email', phone:'tel', website:'url',
  iata:'text', icao:'text', prefix:'text', serial:'text', pool_no:'text',
  allocation_no:'text', invoice_no:'text', payment_no:'text', pnr:'text',
  ticket_no:'text', confirmation:'text', reference:'text', logo:'url'
};
const SELECT_OPTS = {
  status:['active','inactive','draft','pending','approved','rejected','cancelled','completed'],
  type:['flight','hotel','car','misc','ancillary'],
  location_type:['airport','channel','office'],
  cabin:['economy','premium','business','first'],
  currency:['USD','UGX','KES','GBP','EUR','NGN'],
  payment_method:['cash','card','mobile_money','bank','transfer'],
  source_type:['amadeus_air','gds_amadeus','gds_sabre','gds_travelport','ndc','airline_portal','manual','bsp_arc'],
  channel:['airport','channel','office','web','gds'],
  role:['owner','admin','accountant','reservations','agent','operations','checkin','station','agency_admin','agency_agent','agency'],
  priority:['low','medium','high','urgent'],
  urgency:['low','medium','high','critical'],
  account_type:['asset','liability','equity','revenue','expense'],
  location_type:['airport','channel','office']
};
const fld = (name, type='text', required=false, opts=null, def='') => ({name,type,required,opts,def});
const txt = (name, required=false, def='') => fld(name,'text',required,null,def);
const num = (name, required=false, def='') => fld(name,'number',required,null,def);
const dat = (name, required=false, def='') => fld(name,'date',required,null,def);
const area = (name, required=false, def='') => fld(name,'textarea',required,null,def);
const sel = (name, opts, required=false, def='') => fld(name,'select',required,opts,def);
const chk = (name, def=0) => fld(name,'checkbox',false,null,def);
const pwd = (name, required=false) => fld(name,'password',required);
const col = (name, def='') => fld(name,'color',false,null,def);
const cfgFor = view => CREATE_SCHEMAS[view] || {fields:[]};
function schemaValue(spec,row={}){
  const v = row[spec.name];
  if(v!==undefined && v!==null && v!=='') return v;
  return typeof spec.def==='function' ? spec.def() : (spec.def ?? '');
}
function schemaControl(spec,row={}){
  const v = schemaValue(spec,row);
  const n = esc(spec.name);
  const req = spec.required?' required':'';
  if(spec.type==='hidden') return `<input type="hidden" name="${n}" value="${esc(v)}">`;
  if(spec.type==='checkbox'){
    const on = v===true||v===1||v==='1'||v==='true';
    return `<label class="checkbox-row"><input type="checkbox" name="${n}"${on?' checked':''}> ${fieldLabel(spec.name)}</label>`;
  }
  if(spec.type==='select'){
    const opts = spec.opts||[];
    const current = v===''?'':String(v);
    const placeholder = spec.required?'<option value="">Select</option>':'';
    const extra = current && !opts.some(o=>String(o)===current)?`<option value="${esc(current)}" selected>${esc(current)}</option>`:'';
    return `<select name="${n}"${req}>${placeholder}${opts.map(o=>`<option value="${esc(o)}"${String(o)===current?' selected':''}>${esc(o)}</option>`).join('')}${extra}</select>`;
  }
  if(spec.type==='textarea') return `<textarea name="${n}" rows="3"${req}>${esc(v)}</textarea>`;
  if(spec.type==='number') return `<input type="number" step="any" name="${n}" value="${esc(v)}"${req}>`;
  return `<input type="${esc(spec.type||'text')}" name="${n}" value="${esc(v)}"${req}>`;
}
function schemaFields(schema,row={}){
  return schema.map(s=>s.type==='hidden'?schemaControl(s,row):`<div><label>${fieldLabel(s.name)}${s.required?' *':''}</label>${schemaControl(s,row)}</div>`).join('');
}
function schemaPayload(form,schema){
  const data = Object.fromEntries(new FormData(form).entries());
  schema.forEach(s=>{
    if(s.type==='checkbox') data[s.name] = form.elements[s.name]?.checked ? 1 : 0;
    if(s.type==='number' && data[s.name]==='') delete data[s.name];
  });
  return data;
}
const STATUS_ACTIVE = ['active','inactive'];
const STATUS_DOC = ['draft','issued','posted','void','cancelled'];
const CURRENCIES = SELECT_OPTS.currency;
const AGENCY_PROFILE_FIELDS = [
  txt('company_name',true),txt('legal_name'),txt('registration_no'),txt('tax_id'),txt('vat_no'),
  area('address'),txt('city'),txt('state'),txt('postal_code'),txt('country'),txt('phone'),txt('email'),txt('website'),
  sel('default_currency',CURRENCIES,false,'USD'),txt('timezone'),txt('fiscal_year_start'),txt('iata_code'),
  txt('logo_text'),col('primary_color','#0f2742'),col('accent_color','#0b6efd'),area('footer_note'),area('notes')
];
const CREATE_SCHEMAS = {
  documents:{fields:[
    sel('entity_type',['general','booking','invoice','payment','customer','traveler','order'],false,'general'),
    num('entity_id'),txt('title',true),txt('category'),txt('file_name'),txt('mime_type'),
    area('content_text'),sel('visibility',['internal','public'],false,'internal')
  ]},
  shares:{fields:[
    num('document_id'),sel('entity_type',['booking','invoice','payment','customer','traveler','order','pss_booking'],false,'booking'),
    num('entity_id',true),txt('recipient'),sel('permission',['view','download'],false,'view'),dat('expires_at')
  ]},
  reconciliations:{fields:[
    txt('recon_no',true),txt('source'),txt('statement_ref'),txt('period'),
    num('expected_amount'),num('actual_amount'),sel('status',['open','matched','posted'],false,'open'),
    area('resolution'),area('notes')
  ]},
  'bank-transactions':{fields:[
    txt('txn_no',true),num('account_id'),dat('txn_date',false,()=>today()),txt('description'),
    sel('txn_type',['receipt','payment','transfer','fee','adjustment'],false,'receipt'),num('amount'),
    sel('currency',CURRENCIES,false,'USD'),txt('reference'),
    sel('reconciliation_status',['unmatched','pending','matched','reconciled'],false,'pending'),area('notes')
  ]},
  'bank-accounts':{fields:[
    txt('account_name',true),sel('account_type',['cash','bank','petty_cash','mobile_money','card'],false,'bank'),
    txt('account_no'),txt('bank_name'),sel('currency',CURRENCIES,false,'USD'),num('opening_balance'),
    chk('is_active',1),area('notes')
  ]},
  expenses:{apiPath:'/api/finance/expenses',fields:[
    txt('expense_no'),txt('category'),txt('vendor'),area('description'),txt('account_code'),
    num('amount',true),sel('currency',CURRENCIES,false,'USD'),dat('expense_date',false,()=>today()),
    sel('payment_method',SELECT_OPTS.payment_method,false,'cash'),txt('reference'),
    sel('status',STATUS_DOC,false,'posted'),txt('receipt_no')
  ]},
  vouchers:{fields:[
    txt('voucher_no',true),sel('voucher_type',['hotel','car','transfer','service'],false,'hotel'),txt('supplier'),
    txt('pnr'),txt('traveler_name'),dat('issue_date',false,()=>today()),dat('service_date'),
    txt('confirmation_no'),num('amount'),sel('currency',CURRENCIES,false,'USD'),
    sel('status',STATUS_ACTIVE,false,'active'),area('notes')
  ]},
  coupons:{fields:[
    txt('coupon_no',true),txt('pnr'),txt('ticket_no'),txt('traveler_name'),txt('airline'),
    dat('issue_date',false,()=>today()),dat('cancel_date'),sel('status',STATUS_ACTIVE,false,'active'),
    txt('cancel_reason'),chk('pre_billed'),num('refund_amount'),sel('currency',CURRENCIES,false,'USD'),area('notes')
  ]},
  refunds:{fields:[
    txt('refund_no',true),num('order_id'),txt('pnr'),txt('ticket_no'),txt('traveler_name'),txt('source'),
    area('reason'),num('amount'),sel('currency',CURRENCIES,false,'USD'),
    sel('status',['pending','approved','rejected','processed'],false,'pending'),
    dat('requested_at',false,()=>today()),dat('processed_at'),txt('policy_check'),chk('auto_void'),area('notes')
  ]},
  'credit-notes':{fields:[
    txt('credit_note_no'),num('customer_id'),num('invoice_id'),txt('reason'),area('description'),
    sel('currency',CURRENCIES,false,'USD'),num('amount',true),dat('issue_date',false,()=>today()),
    sel('status',STATUS_DOC,false,'issued')
  ]},
  orders:{createPath:'/api/orders/create',fields:[
    txt('pnr'),num('customer_id'),num('traveler_id'),txt('product_type'),txt('office_id'),
    txt('origin'),txt('destination'),dat('travel_start'),dat('travel_end'),dat('tktl'),
    sel('currency',CURRENCIES,false,'USD'),num('total_sell'),num('total_cost'),area('notes')
  ]},
  travelers:{fields:[
    txt('full_name',true),num('customer_id'),txt('email'),txt('phone'),txt('doc_type'),txt('doc_no'),
    dat('doc_issued'),dat('doc_expiry'),dat('dob'),txt('nationality'),txt('city'),txt('country'),
    area('address'),area('notes'),sel('status',STATUS_ACTIVE,false,'active')
  ]},
  crm:{fields:[
    sel('type',['B2C','B2B','Corporate'],false,'B2C'),txt('name',true),txt('company'),txt('email'),
    txt('phone'),txt('loyalty_id'),area('notes')
  ]},
  'setup-users':{fields:[
    txt('username',true),pwd('password',true),txt('full_name'),txt('email'),
    sel('role',SELECT_OPTS.role,true,'agent'),sel('status',STATUS_ACTIVE,false,'active'),num('branch_id')
  ]},
  'pss-partner-users':{fields:[
    txt('username',true),pwd('password'),txt('full_name'),txt('email'),
    sel('role',['agency','station','checkin'],false,'agency'),sel('status',STATUS_ACTIVE,false,'active'),
    num('agency_id'),txt('station_code'),num('branch_id')
  ]},
  'pss-flights':{fields:[
    txt('flight_no',true),txt('origin',true),txt('destination',true),
    dat('flight_date',true,()=>today()),txt('dep_time'),txt('arr_time'),txt('aircraft'),
    num('total_seats'),num('available_seats'),num('base_fare'),sel('currency',CURRENCIES,false,'USD'),
    txt('gate'),txt('terminal'),sel('status',['scheduled','delayed','cancelled','completed','boarding'],false,'scheduled'),
    txt('cabin_class'),txt('fare_basis'),area('fare_terms'),
    txt('baggage_allowance'),num('change_fee'),num('refund_fee'),txt('min_stay'),txt('max_stay'),num('advance_purchase_days'),
    txt('schedule_group'),sel('frequency',['once','daily','weekly','weekdays','weekends','custom'],false,'once'),
    area('repeat_dates','Repeat dates (comma/space separated YYYY-MM-DD, for same-time extra flights)')
  ]},
  'pss-bookings':{fields:[
    num('flight_id',true),txt('passenger_name',true),sel('passenger_type',['ADT','CHD','INF'],false,'ADT'),sel('title',['Mr','Ms','Mrs','Mstr','Miss','Dr'],false,''),
    dat('dob'),sel('gender',['M','F','X'],false,''),txt('nationality'),
    txt('passport_no'),dat('passport_expiry'),txt('passport_country'),txt('doc_no'),
    txt('contact_email'),txt('contact_phone'),
    txt('fare_class'),num('fare_amount'),num('taxes'),num('total_amount'),sel('currency',CURRENCIES,false,'USD'),
    num('channel_id'),txt('airport_code'),txt('seat_no'),txt('fare_basis'),
    num('service_fee'),num('commission'),num('baggage_allowance_kg'),
    sel('booking_status',['confirmed','hold','cancelled','completed'],false,'confirmed'),
    sel('payment_status',['pending','partial','paid','refunded'],false,'pending'),
    sel('ticket_status',['unissued','issued','void','refunded','exchanged'],false,'unissued'),
    txt('ssr_codes'),area('special_requests'),
    area('segments_json','Segments JSON (optional: [{"origin":"EBB","destination":"NBO","flight_no":"FA402","date":"2026-12-01","dep_time":"08:00","arr_time":"10:00"}])')
  ]},
  'pss-charter-flights':{fields:[
    txt('charter_no',true),num('request_id'),txt('flight_no',true),txt('origin',true),txt('destination',true),
    dat('flight_date',true,()=>today()),txt('dep_time'),txt('arr_time'),txt('aircraft'),num('seats'),
    txt('charterer'),txt('contract_no'),num('total_amount'),sel('currency',CURRENCIES,false,'USD'),
    sel('status',['scheduled','confirmed','completed','cancelled'],false,'scheduled'),area('notes')
  ]},
  bills:{apiPath:'/api/finance/bills',fields:[
    txt('bill_no'),txt('supplier',true),area('description'),num('order_id'),txt('account_code'),
    sel('currency',CURRENCIES,false,'USD'),num('subtotal'),num('tax'),num('total'),
    dat('bill_date',false,()=>today()),dat('due_date'),sel('status',['open','partial','paid'],false,'open')
  ]},
  payments:{fields:[
    txt('payment_no'),num('invoice_id'),num('customer_id'),num('order_id'),
    sel('method',SELECT_OPTS.payment_method,false,'cash'),txt('reference'),num('amount',true),
    sel('currency',CURRENCIES,false,'USD'),dat('paid_at',false,()=>today()),area('notes')
  ]},
  'pss-payments':{fields:[
    num('booking_id',true),txt('pnr'),num('amount',true),sel('currency',CURRENCIES,false,'USD'),
    sel('method',SELECT_OPTS.payment_method,false,'cash'),txt('reference'),txt('gateway_ref'),
    sel('status',['captured','paid','pending','failed','refunded'],false,'captured'),dat('paid_at',false,()=>today()),area('notes')
  ]},
  invoicing:{fields:[
    txt('invoice_no'),num('customer_id'),num('order_id'),dat('issue_date',false,()=>today()),dat('due_date'),
    sel('currency',CURRENCIES,false,'USD'),num('subtotal'),num('tax'),num('fees'),num('discount'),num('total'),
    txt('description'),area('notes')
  ]}
};
function inputFor(key, val, required){
  const t = FIELD_TYPES[key] || (typeof val==='number'?'number':(typeof val==='boolean'?'checkbox':'text'));
  const v = val===null||val===undefined?'':esc(val);
  const req = required?' required':'';
  const checked = (val===true||val===1||val==='1')?' checked':'';
  if(t==='hidden') return `<input type="hidden" name="${esc(key)}" value="${v}">`;
  if(t==='checkbox') return `<label class="checkbox-row"><input type="checkbox" name="${esc(key)}"${checked}> ${esc(key.replace(/[_]/g,' '))}</label>`;
  if(t==='select'){
    const opts = SELECT_OPTS[key]||[];
    const current = val===null||val===undefined?'':String(val);
    const extra = current && !opts.includes(current) ? `<option value="${esc(current)}" selected>${esc(current)}</option>` : '';
    const sel = opts.map(o=>`<option value="${esc(o)}"${o===current?' selected':''}>${esc(o)}</option>`).join('');
    return `<select name="${esc(key)}">${extra}${sel}</select>`;
  }
  if(t==='textarea') return `<textarea name="${esc(key)}" rows="3" placeholder="${esc(key)}">${v}</textarea>`;
  return `<input type="${t}" name="${esc(key)}" value="${v}"${req}>`;
}
function fieldLabel(key){ return esc(String(key).replace(/[_]/g,' ').replace(/\b\w/g,c=>c.toUpperCase())); }
function moneyCell(v){ return v===null||v===undefined||v===''?'-':money(Number(v||0)); }
function cellFor(key, val){
  if(key==='amount'||key==='total'||key==='balance'||key==='fare'||key==='margin'||key==='sell'||key==='cost'||key==='paid'||key==='revenue'||key==='expenses') return moneyCell(val);
  if(key==='status') return statusBadge(val);
  if(key==='is_active'||key==='active') return (val?`<span class="badge good">active</span>`:`<span class="badge bad">inactive</span>`);
  if(val===null||val===undefined||val==='') return `<span class="muted">-</span>`;
  return esc(val);
}
async function apiDelete(path,id){
  return api(path+'/'+encodeURIComponent(id),{method:'DELETE'});
}
async function apiPut(path,id,data){
  return api(path+'/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(data)});
}
function ctaBtn(label, action, id, extra='', cls='secondary', danger=false){
  return `<button class="mini-btn ${danger?'danger':''}${cls?` ${cls}`:''}" data-action="${esc(action)}" data-id="${esc(id)}"${extra?` data-extra="${esc(extra)}"`:''}>${esc(label)}</button>`;
}
async function renderList(name, el, pageArg){
  const meta = viewMeta[name] || [name,'','Workspace'];
  const cfg = cfgFor(name);
  const path = cfg.apiPath || VIEW_API[name] || `/api/${name}`;
  const createPath = cfg.createPath || path;
  let d = {};
  try{ d = await api(path); }catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  let rows = [];
  if(Array.isArray(d)) rows = d;
  else if(d && Array.isArray(d.results)) rows = d.results;
  else if(d && Array.isArray(d.data)) rows = d.data;
  else if(d && Array.isArray(d.pools)) rows = d.pools;
  else if(d && Array.isArray(d.bookings)) rows = d.bookings;
  else if(d && Array.isArray(d.invoices)) rows = d.invoices;
  else if(d && Array.isArray(d.customers)) rows = d.customers;
  else if(d && Array.isArray(d.travelers)) rows = d.travelers;
  else if(d && Array.isArray(d.flights)) rows = d.flights;
  else if(d && Array.isArray(d.allocations)) rows = d.allocations;
  else if(d && Array.isArray(d.tasks)) rows = d.tasks;
  else if(d && Array.isArray(d.notifications)) rows = d.notifications;
  else if(d && Array.isArray(d.documents)) rows = d.documents;
  else if(d && Array.isArray(d.payments)) rows = d.payments;
  else if(d && typeof d==='object' && !Array.isArray(d)){
    for(const k of Object.keys(d)){
      if(Array.isArray(d[k]) && d[k].length){ rows = d[k]; break; }
    }
  }
  const allRows = rows;
  const totalPages = Math.max(1, Math.ceil(allRows.length / PAGE_SIZE));
  const page = Math.max(1, Math.min(Number(pageArg || LIST_PAGES[name] || 1), totalPages));
  LIST_PAGES[name] = page;
  rows = allRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const kpis = d && d.kpis ? d.kpis : null;
  const sample = rows.length?rows[0]:{};
  const cols = rows.length?Object.keys(sample):[];
  const idKey = cols.find(c=>c==='id') || cols[0] || 'id';
  const headCols = cols.filter(c=>!FIELD_TYPES[c]?.startsWith('hidden') && c!==idKey);
  const actions = rows.length?`<th style="width:170px">Actions</th>`:'';
  const head = `<tr>${headCols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}${actions}</tr>`;
  const defs = actionsFor(name);
  const modern = MODERN_RECORD_VIEWS.has(name);
  const mode = modern ? recordMode(name) : 'list';
  const body = rows.map(r=>{
    const rid = esc(r[idKey]||'');
    const ref = r.invoice_no||r.payment_no||r.pnr||r.sale_no||r.offer_no||r.order_no||r.name||r.title||r[idKey]||'';
    if(modern) return mode==='card' ? modernRecordCard(name, r, rid, ref, defs) : modernRecordRow(name, r, rid, ref, defs);
    const cells = headCols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('');
    const menuItems = defs.map(a=>`<button type="button" class="${a.danger?'danger':''}" data-action="${esc(a.action)}" data-id="${rid}" data-view="${esc(name)}" data-ref="${esc(ref)}"${a.pdf?` data-pdf="${esc(a.pdf)}"`:''}>${esc(a.label)}</button>`).join('');
    const act = `<div class="action-cell"><button type="button" class="icon-btn" data-action="menu" title="Actions" aria-label="Actions">⋮</button><div class="action-menu hidden">${menuItems}</div></div>`;
    return `<tr data-id="${rid}">${cells}<td>${act}</td></tr>`;
  }).join('');
  const kpiHtml = kpis?`<div class="grid kpi-row" style="margin-bottom:16px">${Object.entries(kpis).map(([k,v])=>dashboardKpi(k,v)).join('')}</div>`:'';
  const emptyMsg = rows.length?`${rows.length} record${rows.length>1?'s':''}`:'No records';
  const pagHtml = allRows.length > PAGE_SIZE ? pager(name, page, totalPages, allRows.length) : '';
  const createFields = cfg.fields || [];
  const createLabel = CREATE_LABELS[name] || `Create ${fieldLabel(name)}`;
  const editLabel = EDIT_LABELS[name] || `Update ${fieldLabel(name)}`;
  const createForm = createFields.length?`<form id="f-create-${esc(name)}" class="form-grid"><div class="form-cols">${schemaFields(createFields)}</div><div class="cta-row"><button type="submit" class="primary">Save ${esc(createLabel.toLowerCase())}</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-create-${esc(name)}-msg" class="small"></div></form>`:'';
  const createBtn = createFields.length?`<button class="mini-btn primary" data-action="create-${esc(name)}">+ ${esc(createLabel)}</button>`:'';
  const modeToggle = modern?`<div class="segmented record-mode" role="group" aria-label="Record display">
    <button type="button" class="${mode==='list'?'active':''}" data-action="record-mode" data-mode="list" aria-pressed="${mode==='list'}">List</button>
    <button type="button" class="${mode==='card'?'active':''}" data-action="record-mode" data-mode="card" aria-pressed="${mode==='card'}">Cards</button>
  </div>`:'';
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), createBtn)}
    ${kpiHtml}
    <div class="panel${modern?' panel-records':''}"><div class="panel-head"><div><h3>${esc(meta[0])} records</h3><div class="muted tiny">${emptyMsg}</div></div>${modeToggle}</div><div class="panel-body">
      ${rows.length?(modern?`<div class="${mode==='card'?'record-card-grid':'record-list'}">${body}</div>`:`<div class="table-wrap"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`):`<div class="table-wrap"><div class="empty">No records available for ${esc(meta[0])}.</div></div>`}
    </div></div>
    ${pagHtml}`;
  el.querySelectorAll('[data-action="record-mode"]').forEach(btn=>{
    btn.onclick = ()=>{
      try{ localStorage.setItem('pss-record-mode-'+name, btn.getAttribute('data-mode')); }catch(e){}
      renderList(name, el, page);
    };
  });
  el.querySelectorAll('[data-action="edit"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      try{
        const row = await fetchRowDetail(path, id, rows.find(x=>String(x[idKey])===String(id)) || {}, idKey, d);
        const ucols = Object.keys(row).filter(c=>!FIELD_TYPES[c]?.startsWith('hidden') && c!==idKey && c!=='created_at' && c!=='updated_at');
        const passwordField = name==='setup-users' ? `<div><label>New password</label>${inputFor('password','',false)}<div class="muted tiny">Leave blank to keep the current password. Must be at least 8 characters with upper, lower and digit.</div></div>` : '';
        const uhtml = `<form id="f-edit-${id}" class="form-grid"><div class="form-cols">${ucols.map(k=>`<div><label>${fieldLabel(k)}</label>${inputFor(k,row[k],false)}</div>`).join('')}${passwordField}</div><div class="cta-row"><button type="submit" class="primary">${esc(editLabel)}</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-edit-${id}-msg" class="small"></div></form>`;
        showModal(`${editLabel} #${id}`, uhtml);
        const ef = $('#f-edit-'+id);
        if(ef) ef.onsubmit = async (e)=>{
          e.preventDefault();
          const em = $(`#f-edit-${id}-msg`); if(em) em.textContent='';
          const udata = Object.fromEntries(new FormData(ef).entries());
          try{ await apiPut(path,id,udata); hideModal(); toast('Saved'); renderList(name, el); }
          catch(err){ if(em){ em.textContent = err.message; em.className='small error'; } else toast(err.message,'error'); }
        };
      }catch(err){ toast(err.message,'error'); }
    };
  });
  el.querySelectorAll('[data-action="delete"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      if(!id || !confirm('Delete this record?')) return;
      try{ await apiDelete(path,id); toast('Deleted'); renderList(name, el); }
      catch(err){ toast(err.message,'error'); }
    };
  });
  el.querySelectorAll('[data-action="menu"]').forEach(btn=>{
    btn.onclick = (e)=>{
      e.stopPropagation();
      const menu = btn.parentElement.querySelector('.action-menu');
      if(!menu) return;
      $$('.action-menu').forEach(m=>{ if(m!==menu) m.classList.add('hidden'); });
      menu.classList.toggle('hidden');
    };
  });
  el.querySelectorAll('[data-action="share"]').forEach(btn=>{
    btn.onclick = ()=>{
      const view = btn.getAttribute('data-view') || name;
      const id = btn.getAttribute('data-id');
      const ref = btn.getAttribute('data-ref') || id;
      $$('.action-menu').forEach(m=>m.classList.add('hidden'));
      openShareDialog(view, id, ref);
    };
  });
  el.querySelectorAll('[data-action="page"]').forEach(btn=>{
    btn.onclick = ()=> renderList(name, el, Number(btn.getAttribute('data-id')));
  });
  el.querySelectorAll('[data-action="docs"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      const travelKinds = ['booking-confirmation','itinerary','journey-document'];
      const kinds = travelKinds.map(k=>`<button class="mini-btn secondary" type="button" onclick="pickDoc('${esc(k)}','${esc(id)}')">${esc(DOC_KINDS[k][0])}</button>`).join('');
      const pdfs = travelKinds.map(k=>`<a class="mini-btn secondary" target="_blank" href="/api/documents/pdf/${encodeURIComponent(k)}/${encodeURIComponent(id)}">${esc(DOC_KINDS[k][0])} PDF</a>`).join('');
      showModal('Travel documents', `<div style="padding:10px;display:flex;gap:8px;flex-wrap:wrap">${kinds}${pdfs}</div>`);
    };
  });
  el.querySelectorAll('[data-action="download"]').forEach(btn=>{
    btn.onclick = ()=> window.open(`/api/documents/${encodeURIComponent(btn.getAttribute('data-id'))}/download`,'_blank');
  });
  el.querySelectorAll('[data-action="open"]').forEach(btn=>{
    btn.onclick = ()=>{
      const row = rows.find(x=>String(x[idKey])===String(btn.getAttribute('data-id'))) || {};
      window.open(row.url || `/share/${row.token||''}`,'_blank');
    };
  });
  el.querySelectorAll('[data-action="copy"]').forEach(btn=>{
    btn.onclick = ()=>{
      const row = rows.find(x=>String(x[idKey])===String(btn.getAttribute('data-id'))) || {};
      copyShare(row.url || `${location.origin}/share/${row.token||''}`);
    };
  });
  el.querySelectorAll('[data-action="view"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      try{
        const listRow = rows.find(x=>String(x[idKey])===String(id)) || {};
        const detailRow = await fetchRowDetail(path, id, listRow, idKey, d);
        const row = {...listRow, ...detailRow};
        const fields = Object.keys(row).filter(k=>!FIELD_TYPES[k]?.startsWith('hidden'));
        if(name==='crm'){
          let linked = [];
          try{
            const td = await api('/api/travelers');
            const all = Array.isArray(td) ? td : (td.travelers||td.data||[]);
            linked = (all||[]).filter(t=>String(t.customer_id||'')===String(id) || (row.email && t.email===row.email)).slice(0,6);
          }catch(e){}
          const primary = linked[0] || {};
          const address = [primary.address,primary.city,primary.state,primary.postal_code,primary.country].filter(Boolean).join(', ');
          const info = [
            ['Customer ID',id],['Type',row.type],['Company',row.company],['Email',row.email],['Phone',row.phone],
            ['Loyalty ID',row.loyalty_id],['Source',row.source],['Bookings',row.bookings],
            ['Lifetime spend',row.spend!==undefined&&row.spend!==null&&row.spend!==''?money(Number(row.spend||0)):''],
            ['Nationality',primary.nationality],['ID / passport',primary.doc_no],['Date of birth',primary.dob],
            ['Address',address],['Created',row.created_at]
          ].filter(x=>x[1]!==undefined&&x[1]!==null&&x[1]!=='');
          const wa = phoneDigits(row.phone);
          const quick = [
            row.email?`<a class="mini-btn primary" href="mailto:${esc(row.email)}">Email</a>`:'',
            row.phone?`<a class="mini-btn" href="tel:${esc(phoneDigits(row.phone)||row.phone)}">Call</a>`:'',
            wa?`<a class="mini-btn" href="https://wa.me/${wa}" target="_blank" rel="noopener">WhatsApp</a>`:'',
            `<button type="button" class="mini-btn" id="copy-customer-${esc(id)}">Copy details</button>`,
            `<button type="button" class="mini-btn" data-share-customer="${esc(id)}">Share profile</button>`
          ].filter(Boolean).join('');
          const linkedHtml = linked.length?`<div class="profile-linked"><h4>Linked travelers</h4>${linked.map(t=>`<div class="profile-contact"><b>${esc(t.full_name||t.display_name||[t.first_name,t.last_name].filter(Boolean).join(' ')||'Traveler')}</b><div class="muted tiny">${[t.email,t.phone||t.mobile,t.nationality,t.doc_no].filter(Boolean).map(v=>esc(v)).join(' A� ')||'No contact details'}</div></div>`).join('')}</div>`:'';
          showModal(`Customer profile A� ${row.name||row.company||btn.getAttribute('data-ref')||id}`,
            `<div class="profile-hero"><div class="profile-avatar">${esc(recordInitials(row.name||row.company||id))}</div>
              <div class="profile-identity"><h3>${esc(row.name||row.company||'Customer')}</h3><div class="muted">${[row.company,row.type,row.loyalty_id].filter(Boolean).map(v=>esc(v)).join(' A� ')||'Customer profile'}</div></div>
              ${row.status||row.type?statusBadge(row.status||row.type):''}</div>
             <div class="profile-actions cta-row">${quick}</div>
             <div class="profile-grid">${info.map(([k,v])=>`<div><label>${fieldLabel(k)}</label><div>${k==='Email'&&v?`<a href="mailto:${esc(v)}">${esc(v)}</a>`:(k==='Phone'&&v?`<a href="tel:${esc(phoneDigits(v)||v)}">${esc(v)}</a>`:esc(v))}</div></div>`).join('')}</div>
             ${row.notes?`<div class="profile-notes"><label>Notes</label><div>${esc(row.notes)}</div></div>`:''}
             ${linkedHtml}
             <details class="profile-raw"><summary>All customer fields</summary><div class="form-cols" style="margin-top:12px">${fields.map(k=>`<div><label>${fieldLabel(k)}</label><div>${cellFor(k,row[k])}</div></div>`).join('')}</div></details>
             <div class="cta-row"><button type="button" class="secondary" onclick="hideModal()">Close</button></div>`);
          const cb = $('#copy-customer-'+id);
          if(cb) cb.onclick = ()=>{
            const text = info.map(([k,v])=>`${k}: ${v}`).join('\n');
            if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(()=>toast('Customer details copied')).catch(()=>toast('Copy failed','error'));
            else toast('Clipboard unavailable','error');
          };
          const sh = $('[data-share-customer="'+id+'"]');
          if(sh) sh.onclick = ()=>openShareDialog('crm', String(id), String(row.name||row.company||id));
          return;
        }
        if(name==='pss-bookings'){
          let detail = {};
          try{ detail = await api(`/api/pss/bookings/${encodeURIComponent(id)}`); }catch(e){ detail = {booking: row}; }
          const b = detail.booking || row || {};
          const payments = detail.payments || [];
          const checkins = detail.checkins || [];
          const events = detail.events || [];
          const payRows = payments.length ? `<div class="table-wrap"><table><thead><tr><th>Payment</th><th>Date</th><th>Method</th><th>Amount</th><th>Status</th></tr></thead><tbody>${payments.map(p=>`<tr><td>${esc(p.payment_no||'-')}</td><td>${esc((p.paid_at||p.created_at||'').slice(0,10))}</td><td>${esc((p.method||'').replace(/_/g,' '))}</td><td>${money(Number(p.amount||0),p.currency||b.currency||defaultCurrency())}</td><td>${statusBadge(p.status||'')}</td></tr>`).join('')}</tbody></table></div>` : '<div class="muted tiny">No payments recorded.</div>';
          const checkinRows = checkins.length ? `<div class="table-wrap"><table><thead><tr><th>Check-in</th><th>Seat</th><th>Bags</th><th>Status</th><th>Time</th></tr></thead><tbody>${checkins.map(c=>`<tr><td>${esc(c.checkin_no||'-')}</td><td>${esc(c.seat_no||'-')}</td><td>${esc(c.bag_count??0)}</td><td>${statusBadge(c.status||'')}</td><td>${esc((c.checked_in_at||c.created_at||'').slice(0,16).replace('T',' '))}</td></tr>`).join('')}</tbody></table></div>` : '<div class="muted tiny">No check-ins.</div>';
          showModal(`Booking A� ${esc(b.pnr||btn.getAttribute('data-ref')||id)}`,
            `<div class="form-cols">${fields.map(k=>`<div><label>${fieldLabel(k)}</label><div>${cellFor(k,b[k]!==undefined?b[k]:row[k])}</div></div>`).join('')}</div>
             <div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center;gap:8px"><h4 style="margin:0">Payments</h4><button type="button" class="mini-btn primary" id="booking-record-payment-${esc(id)}">Record payment</button></div>
             <div id="booking-payments-${esc(id)}">${payRows}</div>
             <div style="margin-top:14px"><h4>Check-ins</h4>${checkinRows}</div>
             ${events.length?`<div style="margin-top:14px"><h4>Ticket events</h4><div class="table-wrap"><table><thead><tr><th>Status</th><th>Reason</th><th>Actor</th><th>Date</th></tr></thead><tbody>${events.map(ev=>`<tr><td>${statusBadge(ev.ticket_status||ev.previous_status||'')}</td><td>${esc(ev.reason||'-')}</td><td>${esc(ev.actor||'-')}</td><td>${esc((ev.created_at||'').slice(0,16).replace('T',' '))}</td></tr>`).join('')}</tbody></table></div></div>`:''}
             <div class="cta-row"><button type="button" class="secondary" onclick="hideModal()">Close</button></div>`);
          const payBtn = document.getElementById(`booking-record-payment-${id}`);
          if(payBtn) payBtn.onclick = ()=>{
            const cur = b.currency || defaultCurrency();
            showModal('Record payment', `<form id="f-booking-pay-${esc(id)}" class="form-grid"><div class="form-cols">
              <div><label>Booking ID</label><input name="booking_id" value="${esc(b.id||id)}" readonly></div>
              <div><label>PNR</label><input name="pnr" value="${esc(b.pnr||'')}" readonly></div>
              <div><label>Amount</label><input name="amount" type="number" step="0.01" min="0" value="${esc(b.total_amount||0)}" required></div>
              <div><label>Currency</label><input name="currency" value="${esc(cur)}"></div>
              <div><label>Method</label><select name="method"><option value="cash">Cash</option><option value="card">Card</option><option value="mobile_money">Mobile Money</option><option value="bank">Bank</option><option value="transfer">Transfer</option><option value="flutterwave">Flutterwave</option></select></div>
              <div><label>Status</label><select name="status"><option value="captured">Captured</option><option value="paid">Paid</option><option value="pending">Pending</option></select></div>
              <div><label>Reference</label><input name="reference"></div>
              <div><label>Notes</label><input name="notes"></div>
            </div><div class="cta-row"><button type="submit" class="primary">Save payment</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-booking-pay-${esc(id)}-msg" class="small"></div></form>`);
            const f = document.getElementById(`f-booking-pay-${id}`);
            if(f) f.onsubmit = async (e)=>{
              e.preventDefault();
              const msg = document.getElementById(`f-booking-pay-${id}-msg`);
              if(msg){ msg.textContent=''; msg.className='small'; }
              const data = Object.fromEntries(new FormData(f).entries());
              data.booking_id = Number(data.booking_id||id);
              data.amount = Number(data.amount||0);
              try{
                await api('/api/pss/payments',{method:'POST',body:JSON.stringify(data)});
                hideModal(); toast('Payment recorded'); renderList(name, el);
              }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
            };
          };
          return;
        }
        showModal(`${btn.textContent} · ${btn.getAttribute('data-ref')||id}`,
          `<div class="form-cols">${fields.map(k=>`<div><label>${fieldLabel(k)}</label><div>${cellFor(k,row[k])}</div></div>`).join('')}</div>
           <div class="cta-row"><button type="button" class="secondary" onclick="hideModal()">Close</button></div>`);
      }catch(err){ toast(err.message,'error'); }
    };
  });
  el.querySelectorAll('[data-action="duplicate"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      try{
        const row = await fetchRowDetail(path, id, rows.find(x=>String(x[idKey])===String(id)) || {}, idKey, d);
        const clone = {...row};
        ['id','tenant_id','created_at','updated_at','created_by','trace_id'].forEach(k=>delete clone[k]);
        Object.keys(clone).forEach(k=>{
          if((k.endsWith('_no')||k==='pnr'||k==='reference') && clone[k]) clone[k]=`${clone[k]}-C${Date.now().toString().slice(-4)}`;
          if(k==='status') clone[k]='draft';
        });
        await api(createPath,{method:'POST',body:JSON.stringify(clone)});
        toast('Duplicated'); renderList(name, el);
      }catch(err){ toast(err.message,'error'); }
    };
  });
  ['pdf','receipt','statement'].forEach(action=>{
    el.querySelectorAll(`[data-action="${action}"]`).forEach(btn=>{
      btn.onclick = ()=>{
        const id = btn.getAttribute('data-id');
        const row = rows.find(x=>String(x[idKey])===String(id)) || {};
        const target = pdfTarget(name, row, id, btn.getAttribute('data-pdf') || action);
        if(!target.id){ toast('No linked document available','error'); return; }
        window.open(`/api/documents/pdf/${encodeURIComponent(target.kind)}/${encodeURIComponent(target.id)}`,'_blank');
      };
    });
  });
  el.querySelectorAll('[data-action="ticket"]').forEach(btn=>{
    btn.onclick = async ()=>{
      try{ await api(`/api/pss/bookings/${encodeURIComponent(btn.getAttribute('data-id'))}/ticket`,{method:'POST',body:'{}'}); toast('Ticketed'); renderList(name, el); }
      catch(err){ toast(err.message,'error'); }
    };
  });
  Object.keys(ACTION_STATUS).forEach(action=>{
    el.querySelectorAll(`[data-action="${action}"]`).forEach(btn=>{
      btn.onclick = async ()=>{
        const id = btn.getAttribute('data-id');
        const status = ACTION_STATUS[action];
        if(!confirm(`${esc(btn.textContent)} this record?`)) return;
        try{
          if(name==='pss-bookings' && action==='cancel') await api(`/api/pss/bookings/${encodeURIComponent(id)}/status`,{method:'POST',body:JSON.stringify({status})});
          else {
            const field = name==='bank-transactions' ? 'reconciliation_status' : 'status';
            await apiPut(path,id,{[field]:status});
          }
          toast(`${btn.textContent} complete`); renderList(name, el);
        }catch(err){ toast(err.message,'error'); }
      };
    });
  });
  el.querySelectorAll(`[data-action="create-${esc(name)}"]`).forEach(btn=>{
    btn.onclick = ()=>{
      showModal(createLabel, createForm);
      const form = $(`#f-create-${esc(name)}`);
      if(!form) return;
      form.onsubmit = async (e)=>{
        e.preventDefault();
        const msg = $(`#f-create-${esc(name)}-msg`); if(msg){ msg.textContent=''; msg.className='small'; }
          const data = schemaPayload(form,createFields);
          try{
            await api(createPath,{method:'POST',body:JSON.stringify(data)});
          hideModal(); toast('Created'); renderList(name, el);
        }catch(err){ if(msg){ msg.textContent = err.message; msg.className='small error'; } else toast(err.message,'error'); }
      };
    };
  });
}

/* ========================= CUSTOMER ENGAGEMENT / DOCUMENTS ========================= */
const DOC_KINDS = {
  'booking-confirmation':['Booking Confirmation','Confirmed reservation and e-ticket details'],
  'itinerary':['Itinerary','Journey schedule, segments and travel plan'],
  'invoice':['Invoice','Amounts, taxes and payment summary'],
  'receipt':['Payment Receipt','Payment confirmation and allocation summary'],
  'credit-note':['Credit Note','Refund or adjustment credit'],
  'quotation':['Quotation','Price quote and validity'],
  'statement':['Statement','Account activity and balance'],
  'journey-document':['Journey Document','Complete travel wallet: itinerary, tickets and services'],
  'boardingpass':['Boarding Pass','IATA boarding pass with BCBP and verification barcode'],
  'baggagetag':['Baggage Tag','IATA baggage tag with routing and tracking barcode'],
  'upliftcoupon':['Uplift Coupon','Excess baggage uplift charge and payment coupon']
};
function pdfKindForEntity(entityType){
  return ({invoice:'invoice',payment:'receipt',pss_payment:'receipt',credit_note:'credit-note',estimate:'quotation',customer:'statement',traveler:'statement',pss_booking:'itinerary',booking:'booking-confirmation',pss_checkin:'boardingpass'})[entityType] || 'invoice';
}
function pdfTarget(view, row, id, kind){
  if(view==='documents') return {kind:pdfKindForEntity(row.entity_type), id:row.entity_id || id};
  if(view==='pss-payments') return {kind:'receipt', id:row.erp_payment_id || ''};
  if(view==='orders') return row.invoice_id ? {kind:'invoice', id:row.invoice_id} : {kind:'order', id:id};
  return {kind:kind || 'invoice', id:row.entity_id || id};
}
async function docBranding(){
  try{ const b = await api('/api/branding'); return b.branding||{}; }catch(e){ return {}; }
}
function docPalette(b){
  const primary = b.primary_color||'#0b2a4a';
  const accent = b.accent_color||'#f59e0b';
  return {primary,accent,light:b.light_color||'#eaf1f8',logo:b.logo_data||'',name:b.company_name||b.tenant_name||'Airline',tagline:b.tagline||'Your travel partner',footer:b.footer_note||''};
}
function docHeader(kind, no, date, p){
  return `<div class="doc-head" style="background:${p.primary};color:#fff;padding:24px;border-radius:14px 14px 0 0;position:relative;overflow:hidden;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
      <div>
        ${p.logo?`<img src="${esc(p.logo)}" class="doc-logo" alt="logo">`:`<div class="doc-mark" style="background:rgba(255,255,255,.15)">${(p.name||'A').slice(0,2).toUpperCase()}</div>`}
        <div style="margin-top:6px"><div style="font-size:9px;letter-spacing:1.5px;text-transform:uppercase;opacity:.85">${esc(p.name)}</div><div style="font-size:20px;font-weight:800;letter-spacing:.2px">${esc(DOC_KINDS[kind][0])}</div><div style="font-size:11px;opacity:.9;margin-top:2px">${esc(p.tagline||'')}</div></div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;opacity:.85">Document</div>
        <div style="font-size:15px;font-weight:700">${esc(no||'-')}</div>
        <div style="font-size:11px;opacity:.9">${esc(date||today())}</div>
      </div>
    </div>
  </div>`;
}
function docBodyStart(){ return `<div class="doc-body">`; }
function docBodyEnd(){ return `</div>`; }
function docFooter(p){
  return `<div class="doc-foot">${esc(p.footer||'Thank you for travelling with us. This document is generated from your booking and may be shared with the client.')}</div>`;
}
function moneyDoc(v,c){ return v===null||v===undefined||v===''?'-':money(Number(v||0),c||defaultCurrency()); }
function kv(label,val){ return `<div class="doc-kv"><div class="doc-kv-label">${esc(label)}</div><div class="doc-kv-value">${esc(val||'-')}</div></div>`; }
function docTable(head,rows){
  const h=`<thead><tr>${head.map(h=>`<th class="doc-table-head">${esc(h)}</th>`).join('')}</tr></thead>`;
  const b=rows.map(r=>`<tr>${r.map(c=>`<td class="doc-table-cell">${esc(c)}</td>`).join('')}</tr>`).join('');
  return `<table class="doc-table">${h}<tbody>${b}</tbody></table>`;
}
async function fetchDocData(kind,id){
  const d = await api('/api/session'); const tid = d.session.tenant_id;
  if(kind==='booking-confirmation'||kind==='itinerary'||kind==='journey-document'){
    const b = await api(`/api/pss/bookings/${encodeURIComponent(id)}`);
    const cust = await api(`/api/customers/${encodeURIComponent(b.booking.customer_id||'')}`).catch(()=>({customer:{}}));
    return {booking:b.booking||b, customer:cust.customer||cust, payments:b.payments||[], checkins:b.checkins||[], events:b.events||[], tax_lines:b.tax_lines||[], tenant_id:tid};
  }
  if(kind==='invoice'){
    const rows = (await api('/api/invoices')).invoices||[];
    const inv = rows.find(x=>String(x.id)===String(id)||String(x.invoice_no)===String(id))||{};
    return {invoice:inv, tenant_id:tid};
  }
  if(kind==='receipt'){
    const rows = (await api('/api/payments')).payments||[];
    const payment = rows.find(x=>String(x.id)===String(id)||String(x.payment_no)===String(id))||{};
    return {payment, tenant_id:tid};
  }
  if(kind==='credit-note'){
    const rows = (await api('/api/credit-notes')).credit_notes||[];
    const creditNote = rows.find(x=>String(x.id)===String(id)||String(x.credit_note_no)===String(id))||{};
    return {credit_note:creditNote, tenant_id:tid};
  }
  if(kind==='quotation'){
    const rows = (await api('/api/estimates')).estimates||[];
    const q = rows.find(x=>String(x.id)===String(id)||String(x.estimate_no)===String(id))||{};
    return {estimate:q, tenant_id:tid};
  }
  if(kind==='statement'){
    const [inv,pay] = await Promise.all([api('/api/invoices'),api('/api/payments')]);
    return {invoices:inv.invoices||[], payments:pay.payments||[], tenant_id:tid};
  }
  return {};
}
function docEntityType(kind){
  if(['booking-confirmation','itinerary','journey-document','eticket'].includes(kind)) return 'pss_booking';
  if(kind==='boardingpass'||kind==='baggagetag') return 'pss_checkin';
  if(kind==='upliftcoupon') return 'upliftcoupon';
  if(kind==='invoice') return 'invoice';
  if(kind==='receipt'||kind==='credit-note') return 'payment';
  if(kind==='quotation') return 'estimate';
  if(kind==='statement') return 'customer';
  return 'booking';
}
function shareEntityType(view){
  if(['pss-bookings','pss-eticket-tracker','pss-ticket-status','pss-direct','unused-tickets','orders','bookings','seat-hold'].includes(view)) return 'pss_booking';
  if(view==='invoicing') return 'invoice';
  if(view==='estimates') return 'estimate';
  if(view==='travelers') return 'traveler';
  if(view==='crm'||view==='bank-accounts'||view==='bank-transactions'||view==='reconciliations') return 'customer';
  if(['payments','refunds','pss-payments','credit-notes','coupons','vouchers'].includes(view)) return 'payment';
  if(view==='documents') return 'booking';
  if(view==='pss-checkins') return 'pss_checkin';
  return 'booking';
}
async function createShareLink(entityType, entityId, recipient=''){
  try{
    const r = await api('/api/shares',{method:'POST',body:JSON.stringify({entity_type:entityType,entity_id:entityId,recipient,permission:'view'})});
    return location.origin + (r.url||`/share/${r.token}`);
  }catch(e){ return null; }
}
function shareButtons(url,title,recipient=''){
  const wa = `https://wa.me/?text=${encodeURIComponent(title+' '+url)}`;
  const mail = `mailto:${encodeURIComponent(recipient||'')}?subject=${encodeURIComponent(title)}&body=${encodeURIComponent('Please find your travel document here: '+url)}`;
  return `<div class="doc-share" style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
    <a href="${wa}" target="_blank" class="doc-share-btn" style="background:#25d366;color:#fff">WhatsApp</a>
    <a href="${mail}" class="doc-share-btn" style="background:${DOC_PRIMARY||'#0b2a4a'};color:#fff">Email</a>
    <button type="button" class="doc-share-btn secondary" onclick="copyShare('${esc(url)}')">Copy link</button>
  </div>`;
}
function openPdfViewer(url, title){
  let m = $('#admin-pdf-viewer');
  if(!m){
    m = document.createElement('div');
    m.id = 'admin-pdf-viewer';
    m.className = 'modal pdf-viewer-modal hidden';
    m.innerHTML = `<div class="modal-card"><div class="modal-head"><div><h3 id="admin-pdf-title">Document preview</h3><div class="muted tiny" id="admin-pdf-sub"></div></div>
      <button type="button" class="mini-btn secondary" onclick="closePdfViewer()">Close</button></div>
      <iframe id="admin-pdf-frame" class="pdf-frame" title="Document preview"></iframe>
      <div class="pdf-viewer-actions"><button type="button" class="mini-btn primary" id="admin-pdf-print">Print</button>
        <button type="button" class="mini-btn secondary" id="admin-pdf-share">Share</button>
        <button type="button" class="mini-btn secondary" id="admin-pdf-download">Download</button></div></div>`;
    document.body.appendChild(m);
    m.addEventListener('click', e=>{ if(e.target===m) closePdfViewer(); });
  }
  const frame = $('#admin-pdf-frame', m);
  $('#admin-pdf-title', m).textContent = title || 'Document preview';
  $('#admin-pdf-sub', m).textContent = url.split('?')[0];
  frame.src = url;
  m.classList.remove('hidden');
  $('#admin-pdf-print', m).onclick = ()=>{ try{ frame.contentWindow.focus(); frame.contentWindow.print(); }catch(e){ toast('Use the browser print dialog','error'); } };
  $('#admin-pdf-share', m).onclick = async ()=>{
    const full = location.origin + url;
    try{ if(navigator.share){ await navigator.share({title:title||'Document', url:full}); return; } }catch(e){}
    try{ await navigator.clipboard.writeText(full); toast('Document link copied'); }catch(e){ window.prompt('Copy document link', full); }
  };
  $('#admin-pdf-download', m).onclick = ()=>{
    const a = document.createElement('a');
    a.href = url + (url.includes('?') ? '&' : '?') + 'download=1';
    a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
  };
}
function closePdfViewer(){
  const m = $('#admin-pdf-viewer');
  if(!m) return;
  m.classList.add('hidden');
  const frame = $('#admin-pdf-frame', m);
  if(frame) frame.src = 'about:blank';
}
window.closePdfViewer = closePdfViewer;
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closePdfViewer(); });
async function openShareDialog(view,id,ref){
  const entityType = shareEntityType(view);
  showModal('Share record', `<form id="f-share-record" class="form-grid">
    <div class="form-cols"><div><label>Record</label><input value="${esc(ref||id)}" disabled></div><div><label>Recipient email</label><input name="recipient" type="email" placeholder="client@example.com"></div></div>
    <div><label>WhatsApp number (optional)</label><input name="phone" placeholder="256700000000"></div>
    <div class="cta-row"><button type="submit" class="primary">Create share &amp; email</button><button type="button" class="secondary mini-btn" data-action="share-wa">Share via WhatsApp</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div>
    <div id="f-share-record-msg" class="small"></div>
  </form>`);
  const form = $('#f-share-record');
  if(!form) return;
  const build = async ()=>{
    const data = Object.fromEntries(new FormData(form).entries());
    const url = await createShareLink(entityType, id, data.recipient||'');
    if(!url) throw new Error('Could not create share link');
    return {url, recipient:data.recipient||'', phone:(data.phone||'').replace(/[^0-9]/g,''), title:`Fayaka ${fieldLabel(view)} ${ref||id}`};
  };
  form.onsubmit = async (e)=>{
    e.preventDefault();
    const msg = $('#f-share-record-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
    try{
      const s = await build();
      window.location.href = `mailto:${encodeURIComponent(s.recipient)}?subject=${encodeURIComponent(s.title)}&body=${encodeURIComponent('Please find your document here: '+s.url)}`;
      hideModal(); toast('Email draft opened');
    }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
  };
  const waBtn = $('[data-action="share-wa"]', $('#modal-body'));
  if(waBtn) waBtn.onclick = async ()=>{
    const msg = $('#f-share-record-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
    try{
      const s = await build();
      const wa = s.phone ? `https://wa.me/${s.phone}` : 'https://wa.me/';
      window.open(`${wa}?text=${encodeURIComponent(s.title+' '+s.url)}`,'_blank');
      hideModal(); toast('WhatsApp opened');
    }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
  };
}
let DOC_PRIMARY='#0b2a4a';
async function renderDocument(kind,id,el){
  const p = await docBranding(); DOC_PRIMARY = p.primary_color||'#0b2a4a';
  const meta = DOC_KINDS[kind] || [kind,'Document',''];
  let data = {};
  try{ data = await fetchDocData(kind,id); }catch(e){
    showModal(esc(meta[0]), `<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`);
    return;
  }
  const no = data.booking?.pnr || data.booking?.trip_pnr || data.invoice?.invoice_no || data.estimate?.estimate_no || id;
  const date = data.booking?.created_at || data.invoice?.issue_date || data.estimate?.created_at || today();
  const shareUrl = await createShareLink(docEntityType(kind), id);
  const html = `<div class="doc-wrap">
    ${docHeader(kind,no,date,p)}
    ${docShareBar(kind,id,p,no,meta,shareUrl)}
    ${docBodyStart()}
    ${docContent(kind,data,p)}
    ${docBodyEnd()}
    ${docFooter(p)}
  </div>`;
  showModal(esc(meta[0])+' #'+esc(no), `<div style="max-height:78vh;overflow:auto">${html}</div>`);
}
function docShareBar(kind,id,p,no,meta,shareUrl){
  const title = `${p.name||'Fayaka'} ${meta?.[0]||'Document'} #${no||id}`;
  const url = shareUrl || `${location.origin}/api/documents/pdf/${kind}/${encodeURIComponent(id)}`;
  return `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#64748b">Share with client</div>
    ${shareButtons(url,title)}
  </div>`;
}
function docContent(kind,data,p){
  const b = data.booking||{};
  const cust = data.customer||{};
  switch(kind){
    case 'booking-confirmation': return `
      <div class="doc-cols">
        <div style="flex:1">
          <h4 class="doc-section-title">Passenger</h4>
          ${kv('Name',b.passenger_name||cust.name||'-')}${kv('Document',b.doc_no||'-')}${kv('Type',b.passenger_type||'ADT')}
        </div>
        <div style="flex:1">
          <h4 class="doc-section-title">Booking reference</h4>
          ${kv('PNR',b.pnr||'-')}${kv('Ticket',b.ticket_number||'Not issued')}${kv('Status',b.booking_status||'confirmed')}
        </div>
      </div>
      <h4 class="doc-section-title">Journey segments</h4>
      ${docTable(['Flight','Route','Date','Depart','Arrive','Status','Seat'],[[b.flight_no||'-',b.origin+' → '+b.destination,b.flight_date||'-',b.dep_time||'-',b.arr_time||'-',b.ticket_status||'unissued',b.seat_no||'-']])}
      ${data.tax_lines.length?`<h4 class="doc-section-title">Taxes &amp; fees</h4>${docTable(['Code','Description','Amount'],data.tax_lines.map(t=>[t.tax_code,t.description,moneyDoc(t.amount,t.currency||data.booking?.currency||data.pss_booking?.currency||defaultCurrency())]))}`:''}
      <div class="doc-note"><b>Trace ID:</b> ${esc(b.trace_id||'-')}&nbsp;&nbsp;<b>Channel:</b> ${esc(b.channel_name||'-')}</div>
    `;
    case 'itinerary': return `
      <div class="doc-cols">
        <div style="flex:1">${kv('PNR',b.pnr||'-')}${kv('Customer',cust.name||'-')}${kv('Travel date',b.flight_date||'-')}</div>
        <div style="flex:1">${kv('From',b.origin||'-')}${kv('To',b.destination||'-')}${kv('Cabin',b.fare_class||'-')}</div>
      </div>
      <h4 class="doc-section-title">Segment detail</h4>
      ${docTable(['#','Flight','Departure','Arrival','Aircraft','Status'],[[1,b.flight_no||'-',b.origin+' '+b.dep_time,b.destination+' '+b.arr_time,b.aircraft||'-',b.status||'scheduled']])}
      <div class="doc-note"><b>Check-in:</b> ${data.checkins.length?data.checkins[0].status:'Not checked in'} &nbsp;|&nbsp; <b>Seat:</b> ${b.seat_no||'Not assigned'}</div>
    `;
    case 'invoice': return `<div class="doc-cols">
      <div style="flex:1">${kv('Invoice no',data.invoice.invoice_no||'-')}${kv('Customer',data.invoice.customer_name||'-')}${kv('Issue date',data.invoice.issue_date||'-')}</div>
      <div style="flex:1">${kv('Due date',data.invoice.due_date||'-')}${kv('Status',data.invoice.status||'issued')}${kv('Currency',data.invoice.currency||defaultCurrency())}</div>
    </div>
    <h4 class="doc-section-title">Invoice lines</h4>
    ${docTable(['Description','Qty','Unit price','Tax','Line total'],[['Travel services','1',moneyDoc(data.invoice.subtotal),moneyDoc(data.invoice.tax),moneyDoc(data.invoice.total)]])}
    <div style="display:flex;justify-content:flex-end;margin-top:14px">
      <div style="text-align:right"><div class="doc-amount-label">Grand total</div><div class="doc-amount-value">${moneyDoc(data.invoice.total,data.invoice.currency)}</div></div>
    </div>`;
    case 'receipt': return `<div class="doc-cols">
      <div style="flex:1">${kv('Receipt no',data.payment.payment_no||'-')}${kv('Customer',data.payment.customer_name||'-')}${kv('Invoice',data.payment.invoice_no||'-')}</div>
      <div style="flex:1">${kv('Paid on',data.payment.paid_at||'-')}${kv('Method',data.payment.method||'-')}${kv('Reference',data.payment.reference||'-')}</div>
    </div>
    <div class="doc-amount"><div class="doc-amount-label">Amount received</div><div class="doc-amount-value">${moneyDoc(data.payment.amount,data.payment.currency)}</div></div>`;
    case 'credit-note': return `<div class="doc-cols">
      <div style="flex:1">${kv('Credit note',data.credit_note.credit_note_no||'-')}${kv('Customer',data.credit_note.customer_name||'-')}${kv('Invoice',data.credit_note.invoice_no||'-')}</div>
      <div style="flex:1">${kv('Issue date',data.credit_note.issue_date||'-')}${kv('Reason',data.credit_note.reason||'-')}${kv('Status',data.credit_note.status||'issued')}</div>
    </div>
    <div class="doc-amount"><div class="doc-amount-label">Credit amount</div><div class="doc-amount-value">${moneyDoc(data.credit_note.amount,data.credit_note.currency)}</div></div>`;
    case 'quotation': return `<div class="doc-cols">
      <div style="flex:1">${kv('Estimate no',data.estimate.estimate_no||'-')}${kv('Customer',data.estimate.customer_name||'-')}${kv('Title',data.estimate.title||'-')}</div>
      <div style="flex:1">${kv('Valid until',data.estimate.valid_until||'-')}${kv('Status',data.estimate.status||'draft')}${kv('Currency',data.estimate.currency||'USD')}</div>
    </div>
    <h4 class="doc-section-title">Quote lines</h4>
    ${docTable(['Description','Subtotal','Tax','Total'],[[data.estimate.description||'Travel services',moneyDoc(data.estimate.subtotal),moneyDoc(data.estimate.tax),moneyDoc(data.estimate.total)]])}
    <div class="doc-note">This quotation is valid until ${esc(data.estimate.valid_until||'')}. Acceptance converts to a booking.</div>`;
    case 'statement': return `
      <h4 class="doc-section-title">Account statement</h4>
      ${docTable(['Date','Reference','Type','Amount','Balance'],(data.invoices||[]).slice(0,20).map(i=>[i.issue_date,i.invoice_no,'Invoice',moneyDoc(i.total,i.currency),'-']))}
      <div style="margin-top:14px;padding:14px;background:#f9fbfd;border:1px solid #dfe7f1;border-radius:10px"><b>Open balance:</b> ${moneyDoc(0)}</div>`;
    case 'journey-document': return `
      <div class="doc-cols">
        <div style="flex:1">${kv('Travel wallet','Journey document')}${kv('PNR',b.pnr||'-')}${kv('Passenger',b.passenger_name||cust.name||'-')}</div>
        <div style="flex:1">${kv('From',b.origin||'-')}${kv('To',b.destination||'-')}${kv('Date',b.flight_date||'-')}</div>
      </div>
      ${docTable(['Item','Detail','Status'],[['Flight',b.flight_no||'-',b.status||'scheduled'],['Seat',b.seat_no||'-',b.ticket_status||'unissued'],['Check-in',data.checkins.length?data.checkins[0].status:'pending','-']])}
      <div class="doc-note"><b>Trace ID:</b> ${esc(b.trace_id||'-')}&nbsp;&nbsp;<b>Keep this document for airport check-in.</b></div>`;
  }
  return `<div class="empty">Document content not configured for ${esc(kind)}.</div>`;
}
async function captureCustomerProfile(booking){
  const cust = {
    name: booking.passenger_name||'',
    email: booking.contact_email||'',
    phone: booking.contact_phone||'',
    company: booking.agency_name||'',
    doc_no: booking.doc_no||''
  };
  try{
    const r = await api('/api/customers',{method:'POST',body:JSON.stringify(cust)});
    toast('Customer profile saved to CRM');
    return r;
  }catch(e){
    toast('Profile capture skipped','ok');
  }
}
function copyShare(url){
  navigator.clipboard?.writeText(url).then(()=>toast('Link copied')).catch(()=>{});
}
function pickDoc(kind,id){
  hideModal();
  renderDocument(kind, id, document.createElement('div'));
}
function printDocument(){
  window.print();
}

/* expose for portal use */
window.renderDocument = renderDocument;
window.captureCustomerProfile = captureCustomerProfile;
window.copyShare = copyShare;
window.printDocument = printDocument;

/* ========================= END DOCUMENTS ========================= */




function roleGroup(role){
  const r = String(role||'').toLowerCase();
  if(['owner','admin'].includes(r)) return 'airline_admin';
  if(r==='accountant') return 'airline_accountant';
  if(['reservations','reservation','agent'].includes(r)) return 'airline_reservations';
  if(['operations','operation','checkin','station'].includes(r)) return 'airline_operations';
  if(r==='agency_admin') return 'agency_admin';
  if(['agency','agency_agent'].includes(r)) return 'agency_agent';
  return 'viewer';
}

const ROLE_VIEWS = {
  airline_admin: '*',
  airline_accountant: ['dashboard','finance','invoicing','credit-notes','payments','revenue','refunds','coupons','vouchers','bank-accounts','bank-transactions','reconciliations','bills','expenses','journals','unused-tickets','fare-optimization','analytics','documents','shares','loyalty','tasks','notifications'],
  airline_reservations: ['dashboard','pss-overview','pss-flights','flight-inventory','pss-bookings','pss-checkins','pss-stock','agency-wallet','pss-eticket-tracker','pss-ticket-status','pss-agencies','pss-direct','pss-charters','pss-charter-flights','unused-tickets','fare-optimization','dispatch','crm','travelers','documents','shares','tasks','notifications','analytics','revenue'],
  airline_operations: ['dashboard','pss-overview','pss-flights','flight-inventory','pss-bookings','seat-hold','pss-checkins','pss-weather','pss-airport-docs','disruptions','dispatch','pss-ticket-status','pss-trace','documents','shares','tasks','notifications','intelligence','analytics','revenue'],
  agency_admin: ['pss-workspace','pss-bookings','crm','finance','invoicing','credit-notes','payments','estimates','documents','shares','tasks','notifications'],
  agency_agent: ['pss-workspace','pss-bookings','crm','finance','invoicing','credit-notes','payments','estimates','documents','shares','tasks','notifications'],
  viewer: ['dashboard','analytics']
};
const ROLE_HOME = {
  airline_admin:'dashboard', airline_accountant:'dashboard', airline_reservations:'pss-overview',
  airline_operations:'pss-overview', agency_admin:'pss-workspace', agency_agent:'pss-workspace', viewer:'dashboard'
};

function currentGroup(){ return roleGroup(state.session?.role); }
function allowedViews(){ return ROLE_VIEWS[currentGroup()] || ROLE_VIEWS.viewer; }
function canView(name){ const a = allowedViews(); return a === '*' || a.includes(name); }

function applyRoleNav(){
  const allowed = allowedViews();
  $$('#nav a').forEach(a=>{ a.style.display = (allowed === '*' || allowed.includes(a.dataset.view)) ? '' : 'none'; });
  $$('#nav .nav-group').forEach(g=>{
    const any = Array.from(g.querySelectorAll('a')).some(a=>a.style.display !== 'none');
    g.style.display = any ? '' : 'none';
  });
  const home = ROLE_HOME[currentGroup()] || 'dashboard';
  if(!canView(home)) return home;
  return home;
}

function navIsMobile(){ return window.matchMedia('(max-width: 900px)').matches; }

const NAV_GROUPS_KEY = 'fayaka.navGroups';
function navGroupState(){
  try{ return JSON.parse(localStorage.getItem(NAV_GROUPS_KEY) || '{}') || {}; }catch(e){ return {}; }
}
function saveNavGroups(){
  const out = {};
  $$('#nav .nav-group').forEach(g=>{ if(g.dataset.navKey) out[g.dataset.navKey] = g.classList.contains('collapsed'); });
  try{ localStorage.setItem(NAV_GROUPS_KEY, JSON.stringify(out)); }catch(e){}
}
function initNavGroups(){
  const saved = navGroupState();
  $$('#nav .nav-group').forEach((g,idx)=>{
    const label = g.querySelector('.nav-label');
    if(!label) return;
    const key = (label.textContent || '').trim().toLowerCase().replace(/[^a-z0-9]+/g,'-') || `group-${idx}`;
    g.dataset.navKey = key;
    label.setAttribute('role','button');
    label.tabIndex = 0;
    const collapsed = saved[key] === undefined ? true : !!saved[key];
    g.classList.toggle('collapsed', collapsed);
    label.setAttribute('aria-expanded', String(!collapsed));
    label.onclick = ()=>toggleNavGroup(g);
    label.onkeydown = (e)=>{ if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggleNavGroup(g); } };
  });
}
function toggleNavGroup(g){
  const collapsed = g.classList.toggle('collapsed');
  g.querySelector('.nav-label')?.setAttribute('aria-expanded', String(!collapsed));
  saveNavGroups();
}
function openNavGroup(g){
  if(!g || !g.classList.contains('collapsed')) return;
  g.classList.remove('collapsed');
  g.querySelector('.nav-label')?.setAttribute('aria-expanded','true');
  saveNavGroups();
}

const THEME_KEY = 'fayaka.theme';
function applyTheme(theme){
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  try{ localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light'); }catch(e){}
  const btn = $('#theme-toggle');
  if(btn){
    btn.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
    btn.setAttribute('aria-label', btn.title);
    const use = btn.querySelector('use');
    if(use) use.setAttribute('href', dark ? '#i-sun' : '#i-moon');
  }
}
function initTheme(){
  let saved = '';
  try{ saved = localStorage.getItem(THEME_KEY) || ''; }catch(e){}
  if(!saved) saved = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(saved);
  const btn = $('#theme-toggle');
  if(btn) btn.onclick = ()=> applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
}

function notificationTime(value){
  if(!value) return '';
  return String(value).replace('T',' ').slice(0,16);
}
async function loadNotifications(){
  const list = $('#notification-list');
  if(!list) return;
  try{
    const d = await api('/api/notifications');
    const items = d.notifications || [];
    const unread = items.filter(n=>!Number(n.is_read)).length;
    const badge = $('#notification-badge');
    if(badge){
      badge.textContent = unread > 9 ? '9+' : String(unread);
      badge.classList.toggle('hidden', unread === 0);
    }
    if(!items.length){ list.innerHTML = `<div class="empty">No notifications yet.</div>`; return; }
    list.innerHTML = items.slice(0,8).map(n=>`<button class="notification-item ${Number(n.is_read)?'read':'unread'}" data-notification-id="${esc(n.id)}">
      <span class="notification-dot"></span>
      <span><b>${esc(n.title||'Notification')}</b><small>${esc(n.message||'')}</small><span class="tiny muted">${esc(notificationTime(n.created_at))}</span></span>
    </button>`).join('');
    list.querySelectorAll('[data-notification-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          await api('/api/notifications/read',{method:'POST',body:JSON.stringify({id:Number(btn.getAttribute('data-notification-id'))})});
          $$('.menu-pop').forEach(p=>p.classList.remove('open'));
          await loadNotifications();
        }catch(err){ toast(err.message,'error'); }
      };
    });
  }catch(err){
    list.innerHTML = `<div class="error" style="padding:12px">${esc(err.message)}</div>`;
  }
}

function syncNavAria(){
  const main = $('#main-view'); const sidebar = $('#sidebar');
  if(!navIsMobile()) sidebar?.classList.remove('open');
  const open = !!sidebar?.classList.contains('open');
  const expanded = navIsMobile() ? open : !main?.classList.contains('nav-collapsed');
  [$('#menu-toggle'), $('#content-menu-toggle')].forEach(btn=>btn?.setAttribute('aria-expanded', String(expanded)));
  if(sidebar){
    const hidden = navIsMobile() && !open;
    sidebar.setAttribute('aria-hidden', String(hidden));
    sidebar.toggleAttribute('inert', hidden);
  }
}

function applyNavCollapsed(collapsed){
  $('#main-view')?.classList.toggle('nav-collapsed', !!collapsed);
  try{ localStorage.setItem('fayaka.navCollapsed', collapsed ? '1' : '0'); }catch(e){}
  syncNavAria();
}

function toggleNav(){
  if(navIsMobile()) $('#sidebar')?.classList.toggle('open');
  else applyNavCollapsed(!$('#main-view')?.classList.contains('nav-collapsed'));
  syncNavAria();
}

function setView(name){
  if(name !== 'flight-inventory') stopInventoryPoll();
  if(!canView(name)) name = ROLE_HOME[currentGroup()] || 'dashboard';
  if(!$(`#view-${name}`)) name = ROLE_HOME[currentGroup()] || 'dashboard';
  const viewEl = $(`#view-${name}`);
  if(!viewEl) return;
  $$('#nav a').forEach(a=>a.classList.toggle('active', a.dataset.view===name));
  const activeLink = $$('#nav a').find(a=>a.dataset.view===name);
  if(activeLink) openNavGroup(activeLink.closest('.nav-group'));
  $$('.view').forEach(v=>v.classList.add('hidden'));
  viewEl.classList.remove('hidden');
  const meta = viewMeta[name] || [name,'', 'Workspace'];
  $('#view-title').textContent = meta[0];
  $('#view-subtitle').textContent = meta[1];
  $('#crumb-section').textContent = meta[2];
  $('#sidebar').classList.remove('open');
  syncNavAria();
  renderView(name).catch(e=>{ $(`#view-${name}`).innerHTML = `<div class="panel"><div class="panel-body error">${esc(e.message)}</div></div>`; });
}


function applyBrandingUI(){
  const b = state.branding || {};
  const name = b.company_name || b.tenant_name || 'Fayaka Airline';
  const initials = (b.logo_text || name.replace(/[^A-Za-z0-9]/g,'').slice(0,2) || 'FA').toUpperCase().slice(0,3);
  const hasLogo = !!b.logo_data;
  const logoHtml = hasLogo
    ? `<img src="${esc(b.logo_data)}" alt="${esc(name)} logo" class="brand-logo-img">`
    : `<span class="brand-mark-text">${esc(initials)}</span>`;
  $$('.brand-mark').forEach(el=>{ el.innerHTML = logoHtml; el.classList.toggle('has-logo', hasLogo); });
  const sb = document.querySelector('.sidebar .brand b'); if(sb) sb.textContent = name;
  const st = document.querySelector('.sidebar .brand small'); if(st && b.location) st.textContent = b.location;
  const lb = document.querySelector('.login-brand h1'); if(lb) lb.textContent = name;
}

async function loadSession(){
  const d = await api('/api/session');
  state.session = d.session; state.plan = d.plan; state.usage = d.usage;
  state.branding = d.branding || state.branding || null;
  if(!state.branding){
    try { const b = await api('/api/branding'); state.branding = b.branding || null; } catch(e) {}
  }
  const tname = (state.branding && (state.branding.company_name || state.branding.tenant_name)) || d.session.tenant_name;
  $('#tenant-box').innerHTML = `<b>${esc(tname)}</b><small>${esc(d.plan?.name||'-')} plan · ${esc(d.session.role)} access</small>`;
  const userBox = $('#user-box');
  if(userBox){
    const ctx = [d.session.station_code, d.session.role].filter(Boolean).join(' · ');
    userBox.innerHTML = `<span class="status-dot good"></span>${esc(d.session.full_name||d.session.username)}${ctx?`<small style="margin-left:6px;color:#64748b">${esc(ctx)}</small>`:''}`;
  }
  applyBrandingUI();
}

async function openGlobalSearch(q){
  if(!q || q.length<2){ toast('Enter at least 2 characters','error'); return; }
  const d = await api(`/api/global-search?q=${encodeURIComponent(q)}`);
  const rows = (d.results||[]).map(r=>`<tr><td>${esc(r.type)}</td><td><a class="link" data-action="${esc(r.action)}" data-id="${r.id}">${esc(r.label)}</a></td><td>${esc(r.detail)}</td></tr>`).join('');
  showModal(`Search results for "${esc(q)}"`, `<div class="table-wrap"><table><thead><tr><th>Type</th><th>Record</th><th>Details</th></tr></thead><tbody>${rows||'<tr><td colspan="3" class="empty">No matches found</td></tr>'}</tbody></table></div>`);
}

const DASH_KPI_STYLE = {
  revenue:'finance', sell:'finance', sales:'finance', collected:'finance', amount:'finance',
  margin:'chart', profit:'chart', growth:'chart',
  order:'booking', booking:'booking', reservation:'booking',
  pax:'users', traveler:'users', passenger:'users', customer:'users', member:'users', user:'users',
  flight:'grid', inventory:'grid', seat:'grid',
  ticket:'file', issued:'file', document:'file', e_ticket:'file',
  pending:'warn', due:'warn', unpaid:'warn', overdue:'warn', disruption:'warn', hold:'warn',
  refund:'rose', cancelled:'rose', failed:'rose', void:'rose',
  checkin:'cyan', payment:'cyan', paid:'cyan', reconciliation:'cyan',
  coupon:'purple', voucher:'purple', loyalty:'purple',
  api:'setting', gateway:'setting', webhook:'setting', channel:'setting', source:'setting'
};
function dashboardKpi(key, value){
  const k = String(key).toLowerCase();
  const match = Object.keys(DASH_KPI_STYLE).find(p=>k.includes(p));
  const tone = match ? DASH_KPI_STYLE[match] : 'blue';
  const icons = {finance:'finance',booking:'booking',users:'users',grid:'grid',file:'file',warn:'task',rose:'task',cyan:'chart',purple:'file',setting:'setting',chart:'chart',blue:'chart'};
  return kpi(fieldLabel(key), typeof value==='number'?fmt(value):String(value), icons[tone]||'chart', '', false, tone);
}

const PSS_OVERVIEW_KPI_STYLE = {
  flights:['grid','blue'], seats:['grid','cyan'], bookings:['booking','purple'], unissued:['file','warn'],
  checked_in:['users','cyan'], pending_payment:['finance','warn'], captured:['finance','finance'], stock_remaining:['grid','purple']
};
async function render_pss_overview(el){
  const meta = viewMeta['pss-overview'] || ['PSS Overview','Airline reservations, inventory, stock and operations command center','Airline PSS'];
  let d = {};
  try{ d = await api('/api/pss/overview'); }
  catch(err){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(err.message||String(err))}</div></div>`;
    return;
  }
  const kpis = d.kpis || {};
  const kpiHtml = Object.entries(kpis).map(([k,v])=>{
    const style = PSS_OVERVIEW_KPI_STYLE[k] || ['chart','blue'];
    return kpi(fieldLabel(k), typeof v==='number'?fmt(v):String(v), style[0], '', false, style[1]);
  }).join('');
  const departures = Array.isArray(d.departures)?d.departures:[];
  const lowStock = Array.isArray(d.low_stock)?d.low_stock:[];
  const channels = Array.isArray(d.channels)?d.channels:[];
  const depHtml = table(departures, ['flight_no','origin','destination','flight_date','dep_time','available_seats','status'], (r,c)=>{
    if(c==='status') return statusBadge(r[c]);
    if(c==='origin'||c==='destination') return esc(r[c]||'-');
    return cellFor(c,r[c]);
  });
  const stockHtml = table(lowStock, ['pool_no','airline_code','prefix','next_serial','end_serial','remaining','status'], (r,c)=>c==='status'?statusBadge(r[c]):esc(r[c]??'-'));
  const channelHtml = table(channels, ['name','bookings','revenue'], (r,c)=>c==='revenue'?moneyCell(r[c]):esc(r[c]??'-'));
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn secondary" data-goto="pss-flights">Flights</button> <button class="mini-btn secondary" data-goto="pss-bookings">Bookings</button> <button class="mini-btn primary" data-goto="pss-stock">Ticket stock</button>`)}
    <div class="grid kpi-row" style="margin-bottom:18px">${kpiHtml}</div>
    <div class="grid two" style="gap:16px">
      ${panel('Upcoming departures', depHtml, '', `${departures.length} scheduled flight${departures.length===1?'':'s'}`)}
      ${panel('Low ticket stock', stockHtml, '', `${lowStock.length} pool${lowStock.length===1?'':'s'} near exhaustion`)}
    </div>
    <div style="margin-top:16px">${panel('Sales by channel', channelHtml, '', 'Bookings and revenue by distribution channel')}</div>`;
  el.querySelectorAll('[data-goto]').forEach(btn=>{ btn.onclick = ()=> setView(btn.getAttribute('data-goto')); });
}

async function render_dashboard(el){
  const meta = viewMeta.dashboard || ['Dashboard','Live command center for your travel business','Overview'];
  let d = {};
  try{ d = await api('/api/dashboard'); }catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const kpis = d && d.kpis ? d.kpis : {};
  const kpiHtml = Object.entries(kpis).map(([k,v])=>dashboardKpi(k,v)).join('');
  const recent = Array.isArray(d.recent_orders)?d.recent_orders:[];
  const deps = Array.isArray(d.departures_soon)?d.departures_soon:[];
  const unpaid = Array.isArray(d.unpaid)?d.unpaid:[];
  const tktl = Array.isArray(d.tktl_due)?d.tktl_due:[];
  const bySource = Array.isArray(d.by_source)?d.by_source:[];
  // --- disruptions for enterprise charter/low-cost carrier visibility ---
  let disruptionsHtml = '';
  try{
    const tenantCode = (state.session && (state.session.tenant_code || state.session.tenant_name)) || '';
    const disc = await api('/api/portal/disruptions?tenant='+encodeURIComponent(tenantCode));
    const items = disc.disruptions||[];
    if(items.length){
      const severityColors = { high:'#dc2626', medium:'#f59e0b', low:'#10b981', info:'#3b82f6' };
      const typeIcons = { cancel:'✗', delay:'⏱', gate:'🚪', aircraft:'✈', weather:'☁', staff:'👤', technical:'🔧', security:'🔒' };
      disruptionsHtml = `<div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Service Disruptions</h3></div><div class="panel-body" style="padding:8px 12px">
        <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Active disruptions</div>
        ${items.map(x=>`<div style="margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid ${severityColors[x.severity]||'#e5e7eb'}'"><span style="color:${severityColors[x.severity]||'#6b7280'}">${esc(typeIcons[x.disruption_type]||'•')} ${esc(x.disruption_type)}</span> ${esc(x.severity||'')} ${esc(x.description||'')}<br><small>${esc(x.affected_pnr||'')} ${esc(x.traveler_name||'')}</small></div>`).join('')}
      </div></div>`;
    }
  }catch(e){ /* disruptions non-fatal */ }
  const listHtml = recent.length?`<table><thead><tr><th>PNR</th><th>Route</th><th>Date</th><th>Status</th></tr></thead><tbody>${recent.map(x=>`<tr><td><b>${esc(x.pnr||'-')}</b><small class="cell-sub">${esc(x.order_number||x.source_type||'')}</small></td><td>${esc(x.origin||'')} → ${esc(x.destination||'')}</td><td>${esc(x.travel_start||'')}</td><td>${statusBadge(x.ticket_status||x.status)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No recent orders.</div>`;
  const depHtml = deps.length?`<table><thead><tr><th>PNR</th><th>Route</th><th>Date</th><th>Status</th></tr></thead><tbody>${deps.map(x=>`<tr><td><b>${esc(x.pnr||'-')}</b><small class="cell-sub">${esc(x.source_type||'')}</small></td><td>${esc(x.origin)} → ${esc(x.destination)}</td><td>${esc(x.travel_start)}</td><td>${statusBadge(x.ticket_status)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No departures soon.</div>`;
  const unpaidHtml = unpaid.length?`<table><thead><tr><th>Invoice</th><th>Customer</th><th>Balance</th></tr></thead><tbody>${unpaid.map(x=>`<tr><td>${esc(x.invoice_no||x.id)}</td><td>${esc(x.customer||'-')}</td><td>${moneyCell(x.balance)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No unpaid invoices.</div>`;
  const tktlHtml = tktl.length?`<table><thead><tr><th>PNR</th><th>TKTL</th><th>Status</th></tr></thead><tbody>${tktl.map(x=>`<tr><td>${esc(x.pnr)}</td><td>${esc(x.tktl)}</td><td>${statusBadge(x.ticket_status)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No tickets due.</div>`;
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn primary" data-action="create-dashboard">+ New Order</button>`)}
    <div class="grid kpi-row" style="margin-bottom:20px">${kpiHtml}</div>
    ${disruptionsHtml}
    <div class="grid two dashboard-pair" style="gap:16px">
      <div class="panel"><div class="panel-head"><h3>Recent Orders</h3><span class="muted">${recent.length} latest</span></div><div class="panel-body">${listHtml}</div></div>
      <div class="panel"><div class="panel-head"><h3>Departures Soon</h3><span class="muted">${deps.length} upcoming</span></div><div class="panel-body">${depHtml}</div></div>
    </div>
    <div class="grid two dashboard-pair" style="gap:16px">
      <div class="panel"><div class="panel-head"><h3>Unpaid Invoices</h3></div><div class="panel-body">${unpaidHtml}</div></div>
      <div class="panel"><div class="panel-head"><h3>Tickets Due (TKTL)</h3></div><div class="panel-body">${tktlHtml}</div></div>
    </div>
    <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Orders by Source</h3></div><div class="panel-body">
      ${bySource.length?`<table><thead><tr><th>Source</th><th>Orders</th><th>Sell</th><th>Margin</th></tr></thead><tbody>${bySource.map(x=>`<tr><td>${esc(x.source_type||x.k)}</td><td>${esc(x.orders||x.c)}</td><td>${moneyCell(x.sell||x.v)}</td><td>${moneyCell(x.margin||0)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No source data.</div>`}
    </div></div>`;
  el.querySelectorAll(`[data-action="create-dashboard"]`).forEach(btn=>{
    btn.onclick = ()=>{ toast('Use Travel Items → Create to add an order','ok'); };
  });
}

async function render_dispatch(el){
  const meta = viewMeta.dispatch || ['Dispatch','Airport check-in control, charter flight coordination, schedule changes and recovery','Control Center'];
  const load = async (path, keys) => {
    try{
      const d = await api(path);
      for(const k of keys){ if(Array.isArray(d[k])) return d[k]; }
      return [];
    }catch(e){ return []; }
  };
  const [checkins, charters, disruptions] = await Promise.all([
    load('/api/pss/checkins', ['checkins']),
    load('/api/pss/charter-flights', ['charter_flights','flights']),
    load('/api/setup/disruptions', ['disruptions'])
  ]);
  const table = (rows, cols, empty) => rows.length
    ? `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">${esc(empty)}</div>`;
  const openDisruptions = disruptions.filter(x=>!['resolved','closed'].includes(String(x.status||'').toLowerCase())).length;
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn secondary" data-goto="pss-checkins">Check-in desk</button> <button class="mini-btn secondary" data-goto="pss-charter-flights">Charter flights</button> <button class="mini-btn primary" data-goto="disruptions">Manage disruptions</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Check-ins',checkins.length,'task')}${kpi('Charter flights',charters.length,'booking')}${kpi('Open disruptions',openDisruptions,'chart')}</div>
    <div class="panel"><div class="panel-head"><h3>Airport check-in</h3><span class="badge info">${checkins.length}</span></div><div class="panel-body">
      ${table(checkins, ['checkin_no','pnr','flight_no','origin','destination','seat_no','status'], 'No airport check-ins yet.')}
    </div></div>
    <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Charter flight coordination</h3><span class="badge info">${charters.length}</span></div><div class="panel-body">
      ${table(charters, ['flight_no','client_name','origin','destination','flight_date','status'], 'No charter flights scheduled.')}
    </div></div>
    <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Schedule changes &amp; recovery</h3><span class="badge info">${openDisruptions}</span></div><div class="panel-body">
      ${table(disruptions, ['disruption_type','severity','affected_pnr','traveler_name','description','status'], 'No active disruptions.')}
    </div></div>`;
  el.querySelectorAll('[data-goto]').forEach(btn=>{
    btn.onclick = ()=> setView(btn.getAttribute('data-goto'));
  });
}

async function render_pss_checkins(el){
  const meta = viewMeta['pss-checkins'] || ['Check-in','Counter, kiosk and mobile check-in with boarding passes','Airline PSS'];
  let loadError = '';
  const [cd, bd] = await Promise.all([
    api('/api/pss/checkins').catch(e=>{ loadError = loadError || e.message; return {checkins:[]}; }),
    api('/api/pss/bookings').catch(e=>{ loadError = loadError || e.message; return {bookings:[]}; })
  ]);
  if(loadError){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(loadError)}</div></div>`;
    return;
  }
  const checkins = cd.checkins || [];
  const bookings = bd.bookings || [];
  const totalBags = checkins.reduce((n,r)=>n+Number(r.bag_count||r.bag_items||0),0);
  const boarded = checkins.filter(r=>['boarded','flown'].includes(String(r.boarding_status||'').toLowerCase())).length;
  const tracked = checkins.filter(r=>Number(r.bag_items||0)>0).length;
  let selectedId = state.checkinSelectedId && checkins.some(r=>String(r.id)===String(state.checkinSelectedId))
    ? state.checkinSelectedId : (checkins[0] && checkins[0].id);
  const initials = (name)=>String(name||'?').trim().split(/\s+/).filter(Boolean).slice(0,2).map(x=>x[0].toUpperCase()).join('') || '?';
  const rowsHtml = checkins.length ? checkins.map(r=>{
    const active = String(selectedId)===String(r.id);
    const bags = Number(r.bag_count||r.bag_items||0);
    return `<div class="checkin-passenger${active?' selected':''}" data-select-checkin="${esc(r.id)}" tabindex="0" role="button" aria-selected="${active}">
      <div class="checkin-avatar">${esc(initials(r.passenger_name))}</div>
      <div class="checkin-main">
        <div class="checkin-line"><b>${esc(r.passenger_name||'Passenger')}</b>${statusBadge(r.status)}</div>
        <div class="checkin-meta">${esc(r.pnr||'-')} · ${esc(r.checkin_no||'')} · ${esc(r.flight_no||'')}</div>
        <div class="checkin-route">${esc(r.origin||'')} → ${esc(r.destination||'')}</div>
        <div class="checkin-chips"><span>Seat ${esc(r.seat_no||'-')}</span><span>Gate ${esc(r.gate||'-')}</span><span>${bags} bag${bags===1?'':'s'}</span></div>
      </div>
      <div class="action-cell checkin-actions">
        <button type="button" class="icon-btn action-btn" data-checkin-menu="${esc(r.id)}" aria-label="Actions" title="Actions">⋮</button>
        <div class="action-menu hidden">
          <button type="button" data-checkin-action="baggage" data-id="${esc(r.id)}">Open passenger</button>
          <button type="button" data-checkin-action="boardingpass" data-id="${esc(r.id)}">Boarding pass</button>
          ${bags>0?`<button type="button" data-checkin-action="bagtag" data-id="${esc(r.id)}" data-extra="0">Print bag tag</button>
          <button type="button" data-checkin-action="upliftcoupon" data-id="${esc(r.id)}" data-extra="0">Uplift coupon</button>
          <button type="button" data-checkin-action="shareuplift" data-id="${esc(r.id)}" data-extra="0">Share uplift coupon</button>`:''}
          <button type="button" data-checkin-action="addbag" data-id="${esc(r.id)}">Add bag</button>
          <button type="button" data-checkin-action="share" data-id="${esc(r.id)}">Share boarding pass</button>
          <button type="button" data-checkin-action="status" data-id="${esc(r.id)}" data-extra="boarded">Mark boarded</button>
          <button type="button" data-checkin-action="status" data-id="${esc(r.id)}" data-extra="gate_closed">Close gate</button>
        </div>
      </div>
    </div>`;
  }).join('') : '';
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn primary" data-checkin-global="new">+ Check in passenger</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">
      ${kpi('Check-ins',fmt(checkins.length),'users')}
      ${kpi('Checked bags',fmt(totalBags),'file')}
      ${kpi('Boarded',fmt(boarded),'task')}
      ${kpi('Bag tracking',fmt(tracked),'chart')}
    </div>
    <div class="split checkin-layout">
      <div class="panel"><div class="panel-head"><div><h3>Check-in desk</h3><div class="muted tiny">Select a passenger to manage baggage, tags and boarding</div></div><span class="badge info">${checkins.length}</span></div><div class="panel-body">
        ${checkins.length ? `<div class="checkin-list">${rowsHtml}</div>` : `<div class="empty">No check-ins yet. Issue a ticket, then check in a passenger.</div>`}
      </div></div>
      <div class="panel" id="checkin-detail"><div class="panel-body"><div class="empty">Select a passenger to view details, baggage and boarding actions.</div></div></div>
    </div>
    <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Baggage tracking lookup</h3><span class="muted tiny">Search by PNR or bag tag</span></div><div class="panel-body">
      <form id="f-bag-track" class="grid-form"><div class="form-cols">
        <div><label>PNR</label><input name="pnr" placeholder="ABC123"></div>
        <div><label>Bag tag</label><input name="bag_tag" placeholder="BG-XXXXXX"></div>
      </div><div class="cta-row"><button type="submit" class="primary mini-btn">Track baggage</button></div></form>
      <div id="bag-track-result" style="margin-top:12px"></div>
    </div></div>`;

  bindDetailActions();
  bindCheckinMenus();
  const loadDetail = async (id)=>{
    if(!id) return;
    selectedId = id; state.checkinSelectedId = id;
    el.querySelectorAll('[data-select-checkin]').forEach(row=>{
      const active = String(row.getAttribute('data-select-checkin')) === String(id);
      row.classList.toggle('selected', active);
      row.setAttribute('aria-selected', String(active));
    });
    const detailHost = $('#checkin-detail', el);
    if(detailHost) detailHost.innerHTML = `<div class="panel-body"><div class="muted">Loading check-in detail...</div></div>`;
    let d = {};
    try{ d = await api(`/api/pss/checkins/${encodeURIComponent(id)}/baggage`); }
    catch(err){ if(detailHost) detailHost.innerHTML = `<div class="panel-body error">${esc(err.message)}</div>`; return; }
    const ci = d.checkin || {};
    const bags = d.baggage || [];
    const events = d.events || [];
    const facts = [
      ['Passenger',ci.passenger_name],['PNR',ci.pnr],['Check-in',ci.checkin_no],['Flight',ci.flight_no],
      ['Route',`${ci.origin||'-'} → ${ci.destination||'-'}`],['Seat',ci.seat_no],['Gate',ci.gate],['Terminal',ci.terminal],
      ['Status',ci.status],['Boarding',ci.boarding_status],['Checked in',ci.checked_in_at],['Method',ci.checkin_method]
    ];
    const factHtml = facts.map(([k,v])=>`<div class="share-fact"><span>${esc(k)}</span><b>${esc(v||'-')}</b></div>`).join('');
    const bagHtml = bags.length ? `<div class="bag-grid">${bags.map((b,i)=>`<div class="bag-card">
      <div class="bag-card-top"><span class="badge ${b.status==='claimed'?'good':'info'}">${esc(b.status||'tagged')}</span><span class="muted tiny">#${esc(b.sequence_no||i+1)}</span></div>
      <div class="bag-tag-no">${esc(b.bag_tag)}</div>
      <div class="muted tiny">${esc(b.weight_kg||0)} kg · ${esc(b.location||'check-in')}</div>
      <div class="cta-row" style="margin-top:10px">
        <button class="mini-btn secondary" data-checkin-action="bagtag" data-id="${esc(ci.id)}" data-extra="${i}">Print tag</button>
        <button class="mini-btn secondary" data-checkin-action="upliftcoupon" data-id="${esc(ci.id)}" data-extra="${i}">Uplift coupon</button>
        <button class="mini-btn secondary" data-checkin-action="shareuplift" data-id="${esc(ci.id)}" data-extra="${i}">Share coupon</button>
        <button class="mini-btn secondary" data-checkin-action="scan" data-id="${esc(b.id)}">Scan</button>
      </div>
    </div>`).join('')}</div>` : `<div class="empty">No bags recorded for this check-in.</div>`;
    const timeline = events.length ? `<div class="tracking-timeline">${events.map(e=>`<div class="track-step">
      <span class="track-dot ${String(e.event_type).includes('arriv')||String(e.event_type).includes('claim')?'done':''}"></span>
      <div><div class="tiny"><b>${esc(fieldLabel(String(e.event_type).replace(/_/g,' ')))}</b> · ${esc(e.created_at||'')}</div>
      <div class="muted tiny">${esc(e.location||'-')} · ${esc(e.actor||'system')}${e.note?` · ${esc(e.note)}`:''}</div></div>
    </div>`).join('')}</div>` : `<div class="empty">No baggage events yet.</div>`;
    if(detailHost) detailHost.innerHTML = `<div class="panel-head"><div><h3>${esc(ci.passenger_name||'Passenger')}</h3><div class="muted tiny">${esc(ci.checkin_no||'')} · ${esc(ci.pnr||'')}</div></div><div>${statusBadge(ci.status)}</div></div>
      <div class="panel-body">
        <div class="checkin-facts">${factHtml}</div>
        <div class="cta-row">
          <button class="mini-btn primary" data-checkin-action="boardingpass" data-id="${esc(ci.id)}">Boarding pass PDF</button>
          <button class="mini-btn secondary" data-checkin-action="addbag" data-id="${esc(ci.id)}">+ Add bag</button>
          <button class="mini-btn secondary" data-checkin-action="status" data-id="${esc(ci.id)}" data-extra="boarded">Board</button>
          <button class="mini-btn secondary" data-checkin-action="status" data-id="${esc(ci.id)}" data-extra="gate_closed">Close gate</button>
          <button class="mini-btn secondary" data-checkin-action="status" data-id="${esc(ci.id)}" data-extra="arrived">Arrived</button>
        </div>
        <div class="panel-subhead"><h4>Checked baggage</h4><span class="badge info">${bags.length}</span></div>
        ${bagHtml}
        <div class="panel-subhead"><h4>Tracking timeline</h4><span class="badge info">${events.length}</span></div>
        ${timeline}
      </div>`;
    bindDetailActions();
  };

  function bindDetailActions(){
    el.querySelectorAll('[data-checkin-action]').forEach(btn=>{
      btn.onclick = async ()=>{
        const action = btn.getAttribute('data-checkin-action');
        const id = btn.getAttribute('data-id');
        const extra = btn.getAttribute('data-extra') || '';
        $$('.action-menu', el).forEach(m=>m.classList.add('hidden'));
        try{
          if(action==='boardingpass'){ openPdfViewer(`/api/documents/pdf/boardingpass/${encodeURIComponent(id)}`, 'Boarding pass'); return; }
          if(action==='bagtag'){ openPdfViewer(`/api/documents/pdf/baggagetag/${encodeURIComponent(id)}?bag=${encodeURIComponent(extra)}`, 'Bag tag'); return; }
          if(action==='upliftcoupon'){ openPdfViewer(`/api/documents/pdf/upliftcoupon/${encodeURIComponent(id)}?bag=${encodeURIComponent(extra)}`, 'Uplift coupon'); return; }
          if(action==='shareuplift'){
            const baseUrl = await createShareLink('upliftcoupon', id, '');
            if(!baseUrl) throw new Error('Could not create share link');
            const url = `${baseUrl}?bag=${encodeURIComponent(extra)}`;
            showModal('Share uplift coupon', `<div class="muted">Secure link ready.</div><div style="margin:12px 0;word-break:break-all">${esc(url)}</div>${shareButtons(url,'Uplift coupon')}`);
            return;
          }
          if(action==='share'){
            const url = await createShareLink('pss_checkin', id, '');
            if(!url) throw new Error('Could not create share link');
            showModal('Share boarding pass', `<div class="muted">Share link ready.</div><div style="margin:12px 0;word-break:break-all">${esc(url)}</div>${shareButtons(url,'Boarding pass')}`);
            return;
          }
          if(action==='baggage'){ selectedId = id; await loadDetail(id); return; }
          if(action==='addbag'){
            showModal('Add baggage', `<form id="f-add-bag" class="form-grid"><div class="form-cols">
              <div><label>Bag tag (optional)</label><input name="bag_tag" placeholder="BG-XXXXXX"></div>
              <div><label>Weight (kg)</label><input name="weight_kg" type="number" step="0.1" value="23"></div>
              <div><label>Pieces</label><input name="pieces" type="number" min="1" value="1"></div>
              <div><label>Sale reference</label><input name="sale_no" placeholder="BAG-XXXXXX"></div>
            </div><div class="cta-row"><button type="submit" class="primary">Save baggage</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-add-bag-msg" class="small"></div></form>`);
            const form = $('#f-add-bag');
            if(form) form.onsubmit = async (e)=>{
              e.preventDefault();
              const payload = Object.fromEntries(new FormData(form).entries());
              try{ await api(`/api/pss/checkins/${encodeURIComponent(id)}/baggage`,{method:'POST',body:JSON.stringify(payload)}); hideModal(); toast('Baggage saved'); await loadDetail(id); }
              catch(err){ const m=$('#f-add-bag-msg'); if(m){ m.textContent=err.message; m.className='small error'; } else toast(err.message,'error'); }
            };
            return;
          }
          if(action==='scan'){
            showModal('Scan baggage', `<form id="f-bag-scan" class="form-grid"><div class="form-cols">
              <div><label>Event</label><select name="event_type">${['bag_tagged','bag_screened','bag_loaded','bag_unloaded','bag_arrived','bag_claimed','bag_missing'].map(x=>`<option value="${x}">${fieldLabel(x.replace(/_/g,' '))}</option>`).join('')}</select></div>
              <div><label>Location</label><input name="location" placeholder="security, aircraft, baggage hall"></div>
              <div><label>Carousel</label><input name="carousel" placeholder="A"></div>
              <div><label>Note</label><input name="note"></div>
            </div><div class="cta-row"><button type="submit" class="primary">Record scan</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-bag-scan-msg" class="small"></div></form>`);
            const form = $('#f-bag-scan');
            if(form) form.onsubmit = async (e)=>{
              e.preventDefault();
              const payload = Object.fromEntries(new FormData(form).entries());
              try{ await api(`/api/pss/baggage/${encodeURIComponent(id)}/scan`,{method:'POST',body:JSON.stringify(payload)}); hideModal(); toast('Bag scan recorded'); await loadDetail(selectedId); }
              catch(err){ const m=$('#f-bag-scan-msg'); if(m){ m.textContent=err.message; m.className='small error'; } else toast(err.message,'error'); }
            };
            return;
          }
          if(action==='status'){
            if(!confirm(`${fieldLabel(extra)} this check-in?`)) return;
            await api(`/api/pss/checkins/${encodeURIComponent(id)}/status`,{method:'POST',body:JSON.stringify({status:extra})});
            toast(`Check-in ${fieldLabel(extra).toLowerCase()}`);
            await loadDetail(id);
          }
        }catch(err){ toast(err.message,'error'); }
      };
    });
  }

  function bindCheckinMenus(){
    el.querySelectorAll('[data-checkin-menu]').forEach(btn=>{
      btn.onclick = (e)=>{
        e.stopPropagation();
        const menu = btn.parentElement.querySelector('.action-menu');
        if(!menu) return;
        const opening = menu.classList.contains('hidden');
        $$('.action-menu', el).forEach(m=>m.classList.add('hidden'));
        menu.classList.toggle('hidden', !opening);
      };
    });
  }

  el.querySelectorAll('[data-select-checkin]').forEach(row=>{
    const select = ()=> loadDetail(row.getAttribute('data-select-checkin'));
    row.onclick = (e)=>{
      if(e.target.closest('[data-checkin-action], [data-checkin-menu], .action-menu')) return;
      select();
    };
    row.onkeydown = (e)=>{
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); select(); }
    };
  });
  const newBtn = el.querySelector('[data-checkin-global="new"]');
  if(newBtn) newBtn.onclick = ()=>{
    const eligible = bookings.filter(b=>b.ticket_status==='issued' && !checkins.some(c=>String(c.booking_id)===String(b.id)));
    if(!eligible.length){ toast('No ticketed bookings available for check-in','error'); return; }
    showModal('Check in passenger', `<form id="f-new-checkin" class="form-grid"><div class="form-cols">
      <div><label>Booking / PNR</label><select name="booking_id">${eligible.map(b=>`<option value="${esc(b.id)}">${esc(b.pnr||'#'+b.id)} · ${esc(b.passenger_name||'')}</option>`).join('')}</select></div>
      <div><label>Seat</label><input name="seat_no" placeholder="12A"></div>
      <div><label>Checked bags</label><input name="bag_count" type="number" min="0" value="0"></div>
      <div><label>Method</label><select name="checkin_method"><option value="counter">counter</option><option value="kiosk">kiosk</option><option value="mobile">mobile</option><option value="agency">agency</option></select></div>
      <div><label>Counter</label><input name="counter" placeholder="KLA-1"></div>
    </div><div class="cta-row"><button type="submit" class="primary">Check in</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-new-checkin-msg" class="small"></div></form>`);
    const form = $('#f-new-checkin');
    if(form) form.onsubmit = async (e)=>{
      e.preventDefault();
      const payload = Object.fromEntries(new FormData(form).entries());
      const bid = payload.booking_id; delete payload.booking_id;
      try{
        const r = await api(`/api/pss/bookings/${encodeURIComponent(bid)}/checkin`,{method:'POST',body:JSON.stringify(payload)});
        hideModal(); toast(`Checked in ${r.checkin_no||''}`);
        state.checkinSelectedId = r.checkin_id || null;
        await render_pss_checkins(el);
      }catch(err){ const m=$('#f-new-checkin-msg'); if(m){ m.textContent=err.message; m.className='small error'; } else toast(err.message,'error'); }
    };
  };
  const trackForm = $('#f-bag-track', el);
  if(trackForm) trackForm.onsubmit = async (e)=>{
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(trackForm).entries());
    const qs = new URLSearchParams(Object.entries(payload).filter(([,v])=>v));
    const host = $('#bag-track-result', el);
    try{
      const d = await api(`/api/pss/baggage/tracking?${qs.toString()}`);
      const bags = d.bags||[];
      host.innerHTML = bags.length ? table(bags,['bag_tag','pnr','passenger_name','flight_no','route','status','location','checkin_no'],(r,c)=>{
        if(c==='route') return esc(`${r.origin||'-'} → ${r.destination||'-'}`);
        return cellFor(c,r[c]);
      }) : `<div class="empty">No bags found.</div>`;
    }catch(err){ host.innerHTML = `<div class="error">${esc(err.message)}</div>`; }
  };
  if(selectedId) await loadDetail(selectedId);
}

async function render_admin(el){
  const meta = viewMeta.admin || ['Admin & Subscription','Users, branches, plans, gateway and platform settings','Control Center'];
  let d = {};
  try{ d = await api('/api/admin/overview'); }
  catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const table = (rows, cols, empty) => (rows||[]).length
    ? `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">${esc(empty)}</div>`;
  const plan = d.plan || {};
  const sub = d.subscription || {};
  const gw = d.billing_gateway || {};
  const fact = (k,v)=>`<div style="min-width:150px"><div class="muted tiny">${esc(k)}</div><b>${esc(v||'-')}</b></div>`;
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Plan',plan.name||'-','setting')}${kpi('Users',(d.users||[]).length,'users')}${kpi('Branches',(d.branches||[]).length,'file')}${kpi('Gateway',gw.name||'Not set','finance')}</div>
    ${panel('Plan & subscription', `<div style="display:flex;gap:20px;flex-wrap:wrap">${fact('Plan',plan.name)}${fact('Plan code',plan.code||plan.plan_code)}${fact('Status',sub.status)}${fact('Renews',sub.current_period_end||sub.renewal_date)}${fact('Gateway',gw.provider||gw.name)}${fact('Gateway mode',gw.mode)}${fact('Public key',gw.public_key_masked)}${fact('Secret set',gw.secret_key_set?'yes':'no')}</div>`)}
    ${panel('Users', table(d.users,['id','username','full_name','email','role','status'],'No users.'), '', '')}
    ${panel('Branches', table(d.branches,['id','name','code','city','country','status'],'No branches.'), '', '')}
    ${panel('Plans', table(d.plans,['id','name','code','price_monthly','status'],'No plans.'), '', '')}
    ${panel('Subscription invoices', table(d.subscription_invoices,['id','invoice_no','amount','currency','status','created_at'],'No subscription invoices.'), '', '')}
    ${panel('Audit trail', table(d.audits,['id','username','action','created_at'],'No audit events.'), '', '')}`;
}

async function render_pss_channels(el){
  const meta = viewMeta['pss-channels'] || ['Channels','Distribution channels, commissions and airport access','Airline PSS'];
  const cols = ['code','name','channel_type','commission_rate','allowed_airports','currency','settlement_days','status'];
  let rows = [];
  try{
    const d = await api('/api/setup/pss-channels');
    rows = Array.isArray(d.channels) ? d.channels : [];
  }catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const formHtml = (r={}) => {
    const id = r.id || 'create';
    return `<form id="f-channel-${id}" class="form-grid"><div class="form-cols">${cols.map(k=>`<div><label>${fieldLabel(k)}</label>${inputFor(k,r[k]??'',false)}</div>`).join('')}</div><div class="cta-row"><button type="submit" class="primary">${r.id?'Save':'Create'} channel</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-channel-${id}-msg" class="small"></div></form>`;
  };
  const wireForm = (form, id) => {
    if(!form) return;
    form.onsubmit = async (e)=>{
      e.preventDefault();
      const msg = $(`#f-channel-${id}-msg`); if(msg){ msg.textContent=''; msg.className='small'; }
      const data = Object.fromEntries(new FormData(form).entries());
      try{
        if(id === 'create') await api('/api/setup/pss-channels',{method:'POST',body:JSON.stringify(data)});
        else await apiPut('/api/setup/pss-channels',id,data);
        hideModal(); toast(id === 'create' ? 'Created' : 'Saved'); render_pss_channels(el);
      }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
    };
  };
  const body = rows.map(r=>`<tr><td>${esc(r.code||'-')}</td><td>${esc(r.name||'-')}</td><td>${esc(r.channel_type||'-')}</td><td>${esc(r.commission_rate||'-')}</td><td>${esc(r.allowed_airports||'-')}</td><td>${esc(r.currency||'-')}</td><td>${esc(r.settlement_days||'-')}</td><td>${statusBadge(r.status||'active')}</td><td>${ctaBtn('Edit','channel-edit',r.id,'','secondary')}${ctaBtn('Delete','channel-del',r.id,'','danger')}</td></tr>`).join('') || `<tr><td colspan="9" class="empty">No channels configured.</td></tr>`;
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn primary" data-action="channel-create">+ New channel</button>`)}
    <div class="panel"><div class="panel-head"><h3>Distribution channels</h3><span class="badge info">${rows.length}</span></div><div class="panel-body"><div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}<th>Actions</th></tr></thead><tbody>${body}</tbody></table></div></div></div>`;
  const createBtn = $('[data-action="channel-create"]', el);
  if(createBtn) createBtn.onclick = ()=>{ showModal('New channel', formHtml()); wireForm($('#f-channel-create'), 'create'); };
  el.querySelectorAll('[data-action="channel-edit"]').forEach(btn=>{
    btn.onclick = ()=>{
      const id = btn.getAttribute('data-id');
      const row = rows.find(x=>String(x.id)===String(id)) || {};
      showModal('Edit channel', formHtml(row)); wireForm($(`#f-channel-${id}`), id);
    };
  });
  el.querySelectorAll('[data-action="channel-del"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      if(!id || !confirm('Delete this channel?')) return;
      try{ await apiDelete('/api/setup/pss-channels', id); toast('Deleted'); render_pss_channels(el); }
      catch(err){ toast(err.message,'error'); }
    };
  });
}

async function render_pss_workspace(el){
  const meta = viewMeta['pss-workspace'] || ['Partner Workspace','Agency workspace','Airline PSS'];
  let ws={}, stock={};
  try{ [ws, stock] = await Promise.all([api('/api/pss/partner-workspace'), api('/api/pss/stock').catch(()=>({}))]); }
  catch(e){ el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`; return; }
  const ctx = ws.context||{}, agency = ws.agency||{}, bookings = ws.bookings||[], checkins = ws.checkins||[];
  const wallets = Array.isArray(stock.wallets)?stock.wallets:[], allocs = Array.isArray(stock.allocations)?stock.allocations:[];
  const currency = wallets[0]?.currency || agency.currency || 'USD';
  const balance = wallets.reduce((s,w)=>s+Number(w.balance||0),0);
  const remaining = allocs.reduce((s,a)=>s+Math.max(0,Number(a.end_serial||0)-Number(a.next_serial||a.start_serial||0)+1),0);
  const canIssue = ['agency_admin','agency_agent','admin','super_admin'].includes(ctx.role);
  const table = (rows, cols, empty) => rows.length
    ? `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">${esc(empty)}</div>`;
  const bRows = bookings.map(b=>({pnr:b.pnr, passenger:b.passenger_name||'-', ticket:b.ticket_number||'-', ticket_status:b.ticket_status||'unissued', payment:b.payment_status||'-', booking:b.booking_status||'-', amount:money(b.total_amount||0,b.currency||currency),
    action: (canIssue && b.ticket_status!=='issued' && b.booking_status!=='cancelled') ? `<button class="mini-btn primary" data-issue-booking="${esc(b.id)}">Issue ticket</button>` : (b.ticket_status==='issued'?'<span class="badge ok">Issued</span>':'-')}));
  const aRows = allocs.map(a=>({allocation_no:a.allocation_no, range:`${a.start_serial} ? ${a.end_serial}`, remaining:Math.max(0,Number(a.end_serial||0)-Number(a.next_serial||a.start_serial||0)+1), amount:money(a.total_amount||0,a.currency||currency), status:a.status||'active'}));
  el.innerHTML = `${pageHead(esc(meta[0]), esc((agency.name||agency.code||meta[1]||'') + (ctx.station_code?` · ${ctx.station_code}`:'')), `<button class="mini-btn secondary" data-goto="agency-wallet">Wallet</button> <button class="mini-btn primary" data-goto="pss-bookings">Bookings</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Wallet balance',money(balance,currency),'finance')}${kpi('Stock remaining',remaining,'file')}${kpi('Bookings',bookings.length,'plane')}${kpi('Check-ins',checkins.length,'users')}</div>
    ${panel('My bookings', table(bRows,['pnr','passenger','ticket','ticket_status','payment','booking','amount','action'],'No bookings yet.'), '', 'Issue tickets from your allocated stock. Agency bookings consume only your agency allocation.')}
    ${panel('Allocated stock', table(aRows,['allocation_no','range','remaining','amount','status'],'No stock allocated yet.'), '', 'Ask admin to allocate stock; wallet is debited at allocation time.')}
    ${panel('Recent check-ins', table(checkins.map(c=>({checkin_no:c.checkin_no,pnr:c.pnr,passenger:c.passenger_name||'-',seat:c.seat_no||'-',status:c.status||'-',checked_in_at:c.checked_in_at||'-'})),['checkin_no','pnr','passenger','seat','status','checked_in_at'],'No check-ins yet.'), '', '')}`;
  el.querySelectorAll('[data-goto]').forEach(btn=>{ btn.onclick = ()=> setView(btn.getAttribute('data-goto')); });
  el.querySelectorAll('[data-issue-booking]').forEach(btn=>{ btn.onclick = async ()=>{
    const id = btn.getAttribute('data-issue-booking'); btn.disabled = true;
    try{ const r = await api(`/api/pss/bookings/${id}/issue-ticket`, {method:'POST'}); toast(`Ticket issued: ${r.ticket_number||'ok'}`); render_pss_workspace(el); }
    catch(err){ toast(err.message||'Issue failed','error'); btn.disabled = false; }
  }; });
}

async function render_agency_wallet(el){
  const meta = viewMeta['agency-wallet'] || ['Agency Wallet','Agency wallet credit, Flutterwave top-ups and stock allocations','Airline PSS'];
  let d = {}, ag = {agencies:[]};
  try{ [d, ag] = await Promise.all([api('/api/pss/stock'), api('/api/pss/agencies').catch(()=>({agencies:[]}))]); }
  catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const wallets = Array.isArray(d.wallets) ? d.wallets : [];
  const allocs = Array.isArray(d.allocations) ? d.allocations : [];
  const txs = Array.isArray(d.wallet_transactions) ? d.wallet_transactions : [];
  const agencies = Array.isArray(ag.agencies) ? ag.agencies : [];
  const totalBalance = wallets.reduce((s,w)=>s+Number(w.balance||0),0);
  const currency = wallets[0]?.currency || d.pools?.[0]?.currency || 'USD';
  const table = (rows, cols, empty) => rows.length
    ? `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">${esc(empty)}</div>`;
  const walletRows = wallets.map(w=>({agency:w.agency_name||w.agency_code||w.agency_id, balance:money(w.balance||0,w.currency||currency), allocations:w.allocations||0, allocated_amount:money(w.allocated_amount||0,w.currency||currency), status:w.status||'active'}));
  const allocView = allocs.map(a=>({allocation_no:a.allocation_no, agency:a.agency_name||a.allocated_to||'-', range:`${a.start_serial} ? ${a.end_serial}`, amount:money(a.total_amount||0,a.currency||currency), remaining:Math.max(0,Number(a.end_serial||0)-Number(a.next_serial||a.start_serial||0)+1), status:a.status||'active'}));
  const txView = txs.map(t=>({tx_no:t.tx_no, agency:t.agency_name||t.agency_id, type:t.tx_type, amount:money(t.direction==='debit'?-Math.abs(t.amount||0):t.amount||0,t.currency||currency), status:t.status, reference:t.reference||t.gateway_ref||'-', created_at:t.created_at}));
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn secondary" data-action="topup-wallet">Top up wallet</button> <button class="mini-btn primary" data-goto="pss-stock">Allocate stock</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Agencies',wallets.length,'users')}${kpi('Wallet balance',money(totalBalance,currency),'finance')}${kpi('Allocations',allocs.length,'file')}${kpi('Top-ups/Txns',txs.length,'chart')}</div>
    ${panel('Agency wallets', table(walletRows,['agency','balance','allocations','allocated_amount','status'],'No agency wallets yet.'), '', 'Wallet credit is debited when stock is allocated at a unit price.')}
    ${panel('Wallet transactions', table(txView,['tx_no','agency','type','amount','status','reference','created_at'],'No wallet transactions yet.'), '', 'Flutterwave top-ups and allocation debits.')}
    ${panel('Allocation ledger', table(allocView,['allocation_no','agency','range','amount','remaining','status'],'No allocations yet.'), '', '')}`;
  el.querySelectorAll('[data-goto]').forEach(btn=>{ btn.onclick = ()=> setView(btn.getAttribute('data-goto')); });
  const pendingRef = sessionStorage.getItem('walletTopupRef');
  if(pendingRef){
    sessionStorage.removeItem('walletTopupRef');
    api('/api/pss/wallets/topup/verify?tx_ref='+encodeURIComponent(pendingRef)).then(r=>{ if(r.verified){ toast('Wallet top-up verified'); render_agency_wallet(el); } }).catch(()=>{});
  }
  el.querySelector('[data-action="topup-wallet"]')?.addEventListener('click', ()=>{
    const agencyOpts = agencies.map(a=>`<option value="${esc(a.id)}">${esc(a.name||a.code||('Agency '+a.id))}</option>`).join('') || '<option value="">No agencies found</option>';
    showModal('Top up agency wallet', `<form id="f-wallet-topup" class="form-grid">
      <div class="form-cols"><div><label>Agency *</label><select name="agency_id" required>${agencyOpts}</select></div><div><label>Amount *</label><input name="amount" type="number" min="1" step="0.01" required></div></div>
      <div class="form-cols"><div><label>Currency</label><input name="currency" value="${esc(currency)}"></div><div><label>Phone (optional)</label><input name="phone" placeholder="Mobile Money number"></div></div>
      <div><label>Email (optional)</label><input name="email" type="email" placeholder="agency@example.com"></div>
      <div class="muted tiny">Uses configured Flutterwave gateway. In demo mode (no secret key), verification is immediate.</div>
      <div class="cta-row"><button type="submit" class="primary">Continue to Flutterwave</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div>
      <div id="f-wallet-topup-msg" class="small"></div></form>`);
    const form = $('#f-wallet-topup');
    if(form) form.onsubmit = async (e)=>{
      e.preventDefault();
      const msg = $('#f-wallet-topup-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
      const data = Object.fromEntries(new FormData(form).entries());
      data.redirect_url = location.origin + location.pathname + '#agency-wallet';
      try{
        const r = await api('/api/pss/wallets/topup', {method:'POST', body: JSON.stringify(data)});
        if(r.tx_ref) sessionStorage.setItem('walletTopupRef', r.tx_ref);
        if(r.url){ toast('Redirecting to Flutterwave'); hideModal(); if(r.demo){ await api('/api/pss/wallets/topup/verify?tx_ref='+encodeURIComponent(r.tx_ref)); render_agency_wallet(el); } else location.href = r.url; }
      }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
    };
  });
}

function invTable(headers, rows, emptyMsg='No records'){
  if(!rows || !rows.length) return `<div class="empty">${esc(emptyMsg)}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function invField(f, value=''){
  const v = value===null||value===undefined?'':value;
  if(f.type==='select') return `<div><label>${esc(f.label)}</label><select name="${esc(f.name)}">${(f.options||[]).map(o=>`<option value="${esc(o.value)}" ${String(v)===String(o.value)?'selected':''}>${esc(o.label)}</option>`).join('')}</select></div>`;
  if(f.type==='checkbox') return `<div><label>${esc(f.label)}</label><input type="checkbox" name="${esc(f.name)}" ${v?'checked':''}></div>`;
  if(f.type==='textarea') return `<div><label>${esc(f.label)}</label><textarea name="${esc(f.name)}" rows="2">${esc(v)}</textarea></div>`;
  return `<div><label>${esc(f.label)}</label><input name="${esc(f.name)}" type="${f.type||'text'}" value="${esc(v)}" ${f.placeholder?`placeholder="${esc(f.placeholder)}"`:''}></div>`;
}
function invForm(fields, submitLabel='Save'){
  return `<form data-inv-form class="form-grid"><div class="form-cols">${fields.map(f=>invField(f)).join('')}</div><div class="cta-row"><button type="submit" class="mini-btn primary">${esc(submitLabel)}</button><button type="button" class="mini-btn secondary" onclick="hideModal()">Cancel</button></div><div data-inv-msg class="small"></div></form>`;
}
async function openInvEditor(title, resource, fields, row, el){
  const path = `/api/pss/inventory/${resource}`;
  showModal(title, invForm(fields, row?.id?'Save changes':'Create record'));
  const form = $('#modal-body [data-inv-form]');
  if(!form) return;
  fields.forEach(f=>{
    const input = form.querySelector(`[name="${f.name}"]`);
    if(!input || !row) return;
    if(f.type==='checkbox') input.checked = !!row[f.name];
    else input.value = row[f.name]??'';
  });
  form.onsubmit = async (e)=>{
    e.preventDefault();
    const msg = $('[data-inv-msg]', $('#modal-body'));
    if(msg){ msg.textContent=''; msg.className='small'; }
    const data = {};
    fields.forEach(f=>{
      const input = form.querySelector(`[name="${f.name}"]`);
      data[f.name] = f.type==='checkbox' ? !!input?.checked : (input?.value||'');
    });
    try{
      if(row?.id) await apiPut(path, row.id, data);
      else await api(path,{method:'POST',body:JSON.stringify(data)});
      hideModal(); toast('Inventory updated'); await render_flight_inventory(el);
    }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
  };
}
async function invDelete(resource, id, el){
  if(!id || !confirm('Delete this inventory record?')) return;
  try{ await apiDelete(`/api/pss/inventory/${resource}`, id); toast('Deleted'); await render_flight_inventory(el); }
  catch(err){ toast(err.message,'error'); }
}
function wireInvEditors(el, resource, fields, cacheKey){
  const cache = INVENTORY_UI.cache[cacheKey] || [];
  $$('[data-action="menu"]', el).forEach(btn=>{
    btn.onclick = (e)=>{
      e.stopPropagation();
      const menu = btn.parentElement.querySelector('.action-menu');
      if(!menu) return;
      $$('.action-menu').forEach(m=>{ if(m!==menu) m.classList.add('hidden'); });
      menu.classList.toggle('hidden');
    };
  });
  $$(`[data-inv-edit="${resource}"]`, el).forEach(btn=>{
    btn.onclick = ()=>{ const row = cache.find(x=>String(x.id)===String(btn.getAttribute('data-id'))); if(row) openInvEditor(`Edit ${resource}`, resource, fields, row, el); };
  });
  $$(`[data-inv-delete="${resource}"]`, el).forEach(btn=>{
    btn.onclick = ()=> invDelete(resource, btn.getAttribute('data-id'), el);
  });
}
function invAct(id, kind){
  return `<div class="action-cell"><button type="button" class="icon-btn" data-action="menu" aria-label="Actions">⋮</button><div class="action-menu hidden"><button type="button" data-inv-edit="${kind}" data-id="${esc(id)}">Edit</button><button type="button" class="danger" data-inv-delete="${kind}" data-id="${esc(id)}">Delete</button></div></div>`;
}
async function render_flight_inventory(el){
  stopInventoryPoll();
  const [dates, over, uncommitted] = await Promise.all([
    api('/api/pss/inventory/dates').catch(()=>({dates:[],flights:[]})),
    api('/api/pss/inventory/overbooking').catch(()=>({overbookings:[]})),
    api('/api/pss/inventory/uncommitted').catch(()=>({holds:[]}))
  ]);
  INVENTORY_UI.cache.dates = dates;
  INVENTORY_UI.cache.overbooking = over;
  INVENTORY_UI.cache.uncommitted = uncommitted;
  if(!INVENTORY_UI.date || !(dates.dates||[]).includes(INVENTORY_UI.date)) INVENTORY_UI.date = dates.flight_date || (dates.dates||[])[0] || '';
  const tabs = [['dates','Flight Dates'],['legs','Legs'],['segments','Segments'],['availability','Availability'],['rules','Rules & TIA'],['overbooking','Overbooking'],['history','History'],['queue','Queue'],['valuation','Revenue Valuation'],['uncommitted','Uncommitted'],['market','Market Data'],['help','Help']];
  const available = (dates.flights||[]).reduce((s,f)=>s+Number(f.available_seats||0),0);
  el.innerHTML = `${pageHead('Flight Inventory','Flight-date inventory, availability, rules, valuation and uncommitted bookings',`<button class="mini-btn secondary" data-inv-tab="help">Help &amp; self-service</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Flight dates',(dates.dates||[]).length,'booking')}${kpi('Scheduled flights',(dates.flights||[]).length,'chart')}${kpi('Available seats',available,'chart')}${kpi('Overbookings',(over.overbookings||[]).length,'task')}${kpi('Uncommitted',(uncommitted.holds||[]).length,'file')}</div>
    <div class="segmented" style="margin-bottom:12px;flex-wrap:wrap">${tabs.map(([k,l])=>`<button type="button" data-inv-tab="${k}" class="${INVENTORY_UI.tab===k?'active':''}">${esc(l)}</button>`).join('')}</div>
    <div id="inv-feedback" class="small"></div><div id="inv-panel"></div>`;
  $$('[data-inv-tab]', el).forEach(btn=>{
    btn.onclick = ()=>{ INVENTORY_UI.tab = btn.getAttribute('data-inv-tab'); render_flight_inventory(el); };
  });
  await renderInventoryPanel(el, dates, over, uncommitted);
}
async function renderInventoryPanel(el, dates, over, uncommitted){
  const panel = $('#inv-panel', el);
  if(!panel) return;
  panel.innerHTML = `<div class="panel"><div class="panel-body muted">Loading inventory module...</div></div>`;
  const tab = INVENTORY_UI.tab;
  if(tab==='dates'){
    const flights = (dates.flights||[]).filter(f=>!INVENTORY_UI.date || f.flight_date===INVENTORY_UI.date);
    const dateBtns = (dates.dates||[]).map(d=>`<button type="button" class="mini-btn ${d===INVENTORY_UI.date?'primary':'secondary'}" data-inv-date="${esc(d)}">${esc(d)}</button>`).join('');
    const rows = flights.map(f=>[esc(f.flight_no),esc(`${f.origin||''} → ${f.destination||''}`),esc(f.flight_date||''),esc(f.total_seats||0),esc(f.capacity||0),esc(f.reservation_committed||0),esc(f.reservation_holds||0),esc(f.available_seats||0),f.overbooked?'<span class="badge bad">overbooked</span>':'<span class="badge good">ok</span>']);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Flight date inventory</h3><div class="row tight">${dateBtns||'<span class="muted tiny">No flight dates</span>'}</div></div><div class="panel-body">
      ${invTable(['Flight','Route','Date','Total seats','Segment capacity','Committed','Holds','Available','Impact'],rows,'No flights on this date')}
      <form data-mass-form class="form-grid" style="margin-top:12px"><div class="form-cols">
        <div><label>Flight IDs (blank = all)</label><input name="flight_ids" placeholder="1,2,3"></div>
        <div><label>Available seats</label><input name="available_seats" type="number"></div>
        <div><label>Status</label><select name="status"><option value="">Leave unchanged</option><option value="scheduled">scheduled</option><option value="delayed">delayed</option><option value="cancelled">cancelled</option></select></div>
        <div><label>Gate</label><input name="gate" placeholder="A12"></div>
        <div><label>Base fare</label><input name="base_fare" type="number" step="0.01"></div>
      </div><div class="cta-row"><button type="button" class="mini-btn secondary" data-mass="preview">Preview impact</button><button type="submit" class="mini-btn primary">Apply mass update</button></div><div data-mass-msg class="small"></div></form>
      <pre id="inv-mass-result" class="small" style="white-space:pre-wrap"></pre>
    </div></div>`;
    $$('[data-inv-date]', panel).forEach(btn=>{ btn.onclick=()=>{ INVENTORY_UI.date=btn.getAttribute('data-inv-date'); render_flight_inventory(el); }; });
    const form = $('[data-mass-form]', panel);
    const submit = async (preview)=>{
      const data = Object.fromEntries(new FormData(form).entries());
      if(preview) data.preview = true;
      for(const k of Object.keys(data)) if(data[k]==='') delete data[k];
      try{
        const r = await api('/api/pss/inventory/mass-update',{method:'POST',body:JSON.stringify(data)});
        $('#inv-mass-result', panel).textContent = JSON.stringify(r.preview||r,null,2);
        $('[data-mass-msg]', panel).textContent = preview?'Reservation impact preview ready':`Updated ${r.changed||0} flight(s)`;
        if(!preview) render_flight_inventory(el);
      }catch(err){ $('[data-mass-msg]', panel).textContent=err.message; }
    };
    $('[data-mass="preview"]', panel).onclick = ()=>submit(true);
    form.onsubmit = (e)=>{ e.preventDefault(); submit(false); };
    return;
  }
  if(tab==='legs'){
    const legs = (dates.flights||[]).flatMap(f=>(f.legs||[]).map(l=>({...l,flight_no:f.flight_no})));
    INVENTORY_UI.cache.legs = legs;
    const flightOpts = (dates.flights||[]).map(f=>({value:f.id,label:`${f.flight_no} · ${f.flight_date}`}));
    const fields = [
      {name:'flight_id',label:'Flight',type:'select',options:flightOpts},
      {name:'leg_no',label:'Leg no.',type:'number'},
      {name:'origin',label:'Origin'},
      {name:'destination',label:'Destination'},
      {name:'flight_date',label:'Flight date',type:'date'},
      {name:'dep_time',label:'Departure',type:'time'},
      {name:'arr_time',label:'Arrival',type:'time'},
      {name:'aircraft',label:'Aircraft'},
      {name:'status',label:'Status',type:'select',options:[{value:'active',label:'active'},{value:'inactive',label:'inactive'}]}
    ];
    const rows = legs.map(l=>[esc(l.id),esc(l.flight_no||''),esc(l.leg_no||''),esc(`${l.origin||''} → ${l.destination||''}`),esc(l.flight_date||''),esc(l.dep_time||''),esc(l.arr_time||''),esc(l.aircraft||''),invAct(l.id,'legs')]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Leg controls <button class="mini-btn primary" data-inv-new="legs">+ Add leg</button></h3></div><div class="panel-body">${invTable(['ID','Flight','Leg','Route','Date','Dep','Arr','Aircraft',''],rows,'No legs found')}</div></div>`;
    $('[data-inv-new="legs"]', panel).onclick = ()=>openInvEditor('Create leg','legs',fields,null,el);
    wireInvEditors(el,'legs',fields,'legs');
    return;
  }
  if(tab==='segments'){
    const segs = (dates.flights||[]).flatMap(f=>(f.segments||[]).map(s=>({...s,flight_no:f.flight_no})));
    INVENTORY_UI.cache.segments = segs;
    const legOpts = (dates.flights||[]).flatMap(f=>f.legs||[]).map(l=>({value:l.id,label:`Leg ${l.id} · ${l.origin}-${l.destination}`}));
    const fields = [
      {name:'leg_id',label:'Leg',type:'select',options:legOpts},
      {name:'segment_no',label:'Segment no.',type:'number'},
      {name:'origin',label:'Origin'},
      {name:'destination',label:'Destination'},
      {name:'dep_time',label:'Departure',type:'time'},
      {name:'arr_time',label:'Arrival',type:'time'},
      {name:'cabin',label:'Cabin',type:'select',options:[{value:'economy',label:'economy'},{value:'premium',label:'premium'},{value:'business',label:'business'},{value:'first',label:'first'}]},
      {name:'subclass',label:'Subclass'},
      {name:'seats',label:'Seats',type:'number'},
      {name:'status',label:'Status',type:'select',options:[{value:'active',label:'active'},{value:'inactive',label:'inactive'}]},
      {name:'supplementary_info',label:'Supplementary info',type:'textarea'}
    ];
    const rows = segs.map(s=>[esc(s.id),esc(s.flight_no||''),esc(s.leg_no||''),esc(s.segment_no||''),esc(`${s.origin||''} → ${s.destination||''}`),esc(s.cabin||''),esc(s.subclass||''),esc(s.seats||0),esc(s.supplementary_info||''),invAct(s.id,'segments')]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Segment controls <button class="mini-btn primary" data-inv-new="segments">+ Add segment</button></h3></div><div class="panel-body">${invTable(['ID','Flight','Leg','Seg','Route','Cabin','Class','Seats','Supplementary',''],rows,'No segments found')}</div></div>`;
    $('[data-inv-new="segments"]', panel).onclick = ()=>openInvEditor('Create segment','segments',fields,null,el);
    wireInvEditors(el,'segments',fields,'segments');
    return;
  }
  if(tab==='availability'){
    const url = `/api/pss/inventory/availability?pos=${encodeURIComponent(INVENTORY_UI.pos)}&subclass=${encodeURIComponent(INVENTORY_UI.subclass)}`;
    const data = await api(url).catch(()=>({availability:[],poll_key:''}));
    const posOpts = ['ALL','WEB','OTA','GDS','COUNTER','CALL','MOBILE'].map(p=>({value:p,label:p}));
    const classOpts = ['ALL','Y','J','F'].map(c=>({value:c,label:c}));
    const paint = (rows)=>{
      const body = $('#inv-availability-body', panel);
      if(!body) return;
      body.innerHTML = invTable(['Flight','Route','Date','POS','Subclass','Capacity','Committed','Available','Published'],rows.map(a=>[esc(a.flight_no||''),esc(`${a.origin||''} → ${a.destination||''}`),esc(a.flight_date||''),esc(a.pos||''),esc(a.pos_subclass||''),esc(a.capacity||0),esc(a.committed||0),`<b>${esc(a.available||0)}</b>`,esc(a.published_at||'')]),'No published availability');
    };
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Availability by POS <span class="badge info" id="inv-poll-status">live · 15s</span></h3><div class="row tight">
      <select id="inv-pos">${posOpts.map(o=>`<option value="${o.value}" ${o.value===INVENTORY_UI.pos?'selected':''}>${o.label}</option>`).join('')}</select>
      <select id="inv-subclass">${classOpts.map(o=>`<option value="${o.value}" ${o.value===INVENTORY_UI.subclass?'selected':''}>${o.label}</option>`).join('')}</select>
      <button class="mini-btn primary" data-inv-publish>Publish availability</button></div></div><div class="panel-body"><div id="inv-availability-body"></div><div class="muted tiny" style="margin-top:8px">Availability is calculated from segment capacity minus committed reservations, then adjusted by active POS rules and TIA.</div></div></div>`;
    paint(data.availability||[]);
    INVENTORY_UI.pollKey = data.poll_key||'';
    $('#inv-pos', panel).onchange = (e)=>{ INVENTORY_UI.pos=e.target.value; render_flight_inventory(el); };
    $('#inv-subclass', panel).onchange = (e)=>{ INVENTORY_UI.subclass=e.target.value; render_flight_inventory(el); };
    $('[data-inv-publish]', panel).onclick = async ()=>{
      try{ const r=await api('/api/pss/inventory/availability/publish',{method:'POST',body:'{}'}); toast(`Published ${r.published||0} availability rows`); render_flight_inventory(el); }
      catch(err){ toast(err.message,'error'); }
    };
    stopInventoryPoll();
    INVENTORY_UI.poll = setInterval(async ()=>{
      try{
        const fresh = await api(url);
        const status = $('#inv-poll-status', panel);
        if(status) status.textContent = fresh.poll_key!==INVENTORY_UI.pollKey ? 'new data · refreshing' : 'live · 15s';
        if(fresh.poll_key!==INVENTORY_UI.pollKey){ INVENTORY_UI.pollKey=fresh.poll_key; paint(fresh.availability||[]); }
      }catch(err){ const status=$('#inv-poll-status',panel); if(status) status.textContent='poll paused'; }
    },15000);
    return;
  }
  if(tab==='rules'){
    const [data, inv] = await Promise.all([
      api('/api/pss/inventory/rules').catch(()=>({rules:[]})),
      api('/api/pss/inventory/dates').catch(()=>({flights:[]}))
    ]);
    INVENTORY_UI.cache.rules = data.rules||[];
    const flights = inv.flights||[];
    const segOpts = [{value:'',label:'All segments'}];
    flights.forEach(f=> (f.segments||[]).forEach(s=> segOpts.push({value:String(s.id), label:`${f.flight_no||f.id} seg ${s.segment_no||s.id} ${s.origin||''}-${s.destination||''}`})));
    const flightOpts = [{value:'',label:'All flights'}].concat(flights.map(f=>({value:String(f.id), label:`${f.flight_no||f.id} ${f.flight_date||''} ${f.origin||''}-${f.destination||''}`})));
    const fields = [
      {name:'rule_code',label:'Code',placeholder:'SEG_LIMIT_Y'},
      {name:'rule_name',label:'Name'},
      {name:'rule_type',label:'Type',type:'select',options:[{value:'limit',label:'Segment limit'},{value:'gate',label:'Gating'},{value:'pos_subclass',label:'POS subclass logic'},{value:'hold_expiry',label:'Hold expiry'}]},
      {name:'flight_id',label:'Flight scope',type:'select',options:flightOpts},
      {name:'flight_date',label:'Flight date (optional)',type:'date'},
      {name:'segment_id',label:'Segment scope',type:'select',options:segOpts},
      {name:'nest_type',label:'Nesting',type:'select',options:[{value:'none',label:'none'},{value:'serial',label:'serial'},{value:'parallel',label:'parallel'},{value:'mixed',label:'mixed'}]},
      {name:'nest_bucket',label:'Nest bucket',placeholder:'Y/J/F'},
      {name:'priority',label:'Priority',type:'number',placeholder:'100'},
      {name:'pos',label:'POS',placeholder:'ALL'},
      {name:'subclass',label:'Subclass',placeholder:'ALL'},
      {name:'value',label:'Value',type:'number',placeholder:'20'},
      {name:'tia_enabled',label:'TIA enabled',type:'checkbox'},
      {name:'tia_percent',label:'TIA percent',type:'number'},
      {name:'status',label:'Status',type:'select',options:[{value:'active',label:'active'},{value:'inactive',label:'inactive'}]},
      {name:'effective_from',label:'Effective from',type:'date'},
      {name:'effective_to',label:'Effective to',type:'date'}
    ];
    const rows = (data.rules||[]).map(r=>[esc(r.rule_code||''),esc(r.rule_name||''),esc(r.rule_type||''),esc(r.nest_type||'none'),esc(r.pos||''),esc(r.subclass||''),esc(r.value||0),r.tia_enabled?'<span class="badge good">yes</span>':'-',esc(r.tia_percent||0),esc(r.status||''),invAct(r.id,'rules')]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Inventory business rules &amp; TIA <button class="mini-btn primary" data-inv-new="rules">+ Add rule</button></h3></div><div class="panel-body">${invTable(['Code','Name','Type','Nest','POS','Class','Value','TIA','TIA %','Status',''],rows,'No inventory rules')}<div class="muted tiny" style="margin-top:8px">Types: Segment limit, Gating, POS subclass logic, Hold expiry. Nesting: serial/parallel/mixed. TIA applies temporary adjustment %.</div></div></div>`;
    $('[data-inv-new="rules"]', panel).onclick = ()=>openInvEditor('Create inventory rule','rules',fields,null,el);
    wireInvEditors(el,'rules',fields,'rules');
    return;
  }
  if(tab==='overbooking'){
    const data = await api('/api/pss/inventory/overbooking').catch(()=>({overbookings:[]}));
    const rows = (data.overbookings||[]).map(r=>[esc(r.flight_no||''),esc(r.flight_date||''),esc(r.capacity||0),esc(r.committed||0),esc(r.available_seats||0),`<span class="badge bad">+${esc(r.excess||0)}</span>`,`<select class="form-select" style="min-width:160px" data-over-action="${esc(r.flight_id)}"><option value="raise_capacity">Raise capacity</option><option value="deny_sale">Deny sale (close)</option><option value="reaccommodate">Reaccommodate hold</option></select> <button class="mini-btn primary" data-inv-resolve-over="${esc(r.flight_id)}">Apply</button>`]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Overbooking detection</h3></div><div class="panel-body">${invTable(['Flight','Date','Capacity','Committed','Available','Excess','Action'],rows,'No overbookings detected')}</div></div>`;
    $$('[data-inv-resolve-over]', panel).forEach(btn=>{ btn.onclick=async()=>{ const fid=btn.getAttribute('data-inv-resolve-over'); const action=$(`[data-over-action="${fid}"]`,panel)?.value||'raise_capacity'; try{ await api('/api/pss/inventory/overbooking/resolve',{method:'POST',body:JSON.stringify({flight_id:fid,action})}); toast('Overbooking action applied'); render_flight_inventory(el); }catch(e){ toast(e.message,'error'); } }; });
    return;
  }
  if(tab==='history'){
    const data = await api('/api/pss/inventory/history').catch(()=>({history:[]}));
    const rows = (data.history||[]).map(h=>[esc(h.created_at||''),esc(h.action||''),esc(h.entity_type||''),esc(h.entity_id||''),esc(h.actor||''),`<details><summary>View changes</summary><pre class="small" style="white-space:pre-wrap">${esc(h.before_json||'')}\n→ ${esc(h.after_json||'')}</pre></details>`]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Inventory history</h3></div><div class="panel-body">${invTable(['When','Action','Entity','ID','Actor','Changes'],rows,'No inventory history')}</div></div>`;
    return;
  }
  if(tab==='queue'){
    const data = await api('/api/pss/inventory/queue').catch(()=>({queue:[]}));
    const rows = (data.queue||[]).map(q=>[esc(q.created_at||''),esc(q.issue_type||''),`<select class="form-select" style="min-width:90px" data-q-sev="${esc(q.id)}"><option ${q.severity==='warning'?'selected':''}>warning</option><option ${q.severity==='critical'?'selected':''}>critical</option><option ${q.severity==='info'?'selected':''}>info</option></select>`,esc(q.flight_no||''),esc(q.message||''),`<input class="form-input" style="min-width:120px" data-q-assignee="${esc(q.id)}" value="${esc(q.assignee||'')}" placeholder="assignee">`,q.status==='open'?`<button class="mini-btn" data-inv-save-queue="${esc(q.id)}">Save</button> <button class="mini-btn primary" data-inv-resolve-queue="${esc(q.id)}">Resolve</button>`:`<span class="badge good">${esc(q.status)}</span>`]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Inventory queue monitoring</h3></div><div class="panel-body">${invTable(['When','Issue','Severity','Flight','Message','Assignee',''],rows,'Queue is clear')}</div></div>`;
    $$('[data-inv-save-queue]', panel).forEach(btn=>{ btn.onclick=async()=>{ const id=btn.getAttribute('data-inv-save-queue'); try{ await api('/api/pss/inventory/queue/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify({severity:$(`[data-q-sev="${id}"]`,panel)?.value,assignee:$(`[data-q-assignee="${id}"]`,panel)?.value})}); toast('Queue updated'); }catch(e){ toast(e.message,'error'); } }; });
    $$('[data-inv-resolve-queue]', panel).forEach(btn=>{ btn.onclick=async()=>{ try{ await api('/api/pss/inventory/queue/'+encodeURIComponent(btn.getAttribute('data-inv-resolve-queue'))+'/resolve',{method:'POST',body:'{}'}); toast('Queue item resolved'); render_flight_inventory(el); }catch(e){ toast(e.message,'error'); } }; });
    return;
  }
  if(tab==='valuation'){
    const data = await api('/api/pss/inventory/valuation').catch(()=>({valuations:[],computed:[]}));
    INVENTORY_UI.cache.valuations = data.valuations||[];
    const fields = [
      {name:'flight_id',label:'Flight ID',type:'number'},
      {name:'segment_id',label:'Segment ID',type:'number'},
      {name:'pos',label:'POS',placeholder:'ALL'},
      {name:'subclass',label:'Subclass',placeholder:'ALL'},
      {name:'valuation',label:'Active valuation',type:'number'},
      {name:'active',label:'Active',type:'checkbox'}
    ];
    const computedRows = (data.computed||[]).map(v=>[esc(v.flight_no||''),esc(v.flight_date||''),esc(v.committed||0),esc(v.load_factor||0),money(v.valuation||0)]);
    const storedRows = (data.valuations||[]).map(v=>[esc(v.flight_no||''),esc(v.pos||''),esc(v.subclass||''),esc(v.valuation||0),v.active?'<span class="badge good">active</span>':'<span class="badge info">inactive</span>',invAct(v.id,'valuation')]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Revenue availability <button class="mini-btn primary" data-inv-new="valuation">+ Set valuation</button></h3></div><div class="panel-body"><h4>Active valuation model</h4>${invTable(['Flight','Date','Committed','Load factor','Valuation'],computedRows,'No computed valuation')}<h4 style="margin-top:14px">Manual active valuations</h4>${invTable(['Flight','POS','Class','Valuation','Status',''],storedRows,'No manual valuations')}</div></div>`;
    $('[data-inv-new="valuation"]', panel).onclick = ()=>openInvEditor('Set active valuation','valuation',fields,null,el);
    wireInvEditors(el,'valuation',fields,'valuations');
    return;
  }
  if(tab==='uncommitted'){
    const data = uncommitted || await api('/api/pss/inventory/uncommitted').catch(()=>({holds:[],hold_minutes:20}));
    const rows = (data.holds||[]).map(h=>[esc(h.pnr||''),esc(h.passenger_name||''),esc(h.flight_no||''),esc(`${h.origin||''} → ${h.destination||''}`),esc(h.flight_date||''),esc(h.seat_no||''),esc(h.hold_expires_at||''),`<div class="cta-row"><button class="mini-btn primary" data-inv-confirm="${esc(h.id)}">Confirm</button><button class="mini-btn danger" data-inv-release="${esc(h.id)}">Release</button></div>`]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Uncommitted bookings <span class="badge info">${esc(data.hold_minutes||20)} min holds</span></h3></div><div class="panel-body">${invTable(['PNR','Passenger','Flight','Route','Date','Seat','Expires',''],rows,'No uncommitted bookings')}</div></div>`;
    const act = async (btn, action)=>{ try{ await api(`/api/pss/inventory/uncommitted/${encodeURIComponent(btn.getAttribute('data-inv-'+action))}/${action}`,{method:'POST',body:'{}'}); toast(action==='confirm'?'Hold confirmed':'Hold released'); render_flight_inventory(el); }catch(err){ toast(err.message,'error'); } };
    $$('[data-inv-confirm]', panel).forEach(btn=>{ btn.onclick=()=>act(btn,'confirm'); });
    $$('[data-inv-release]', panel).forEach(btn=>{ btn.onclick=()=>act(btn,'release'); });
    return;
  }
  if(tab==='market'){
    const data = await api('/api/pss/inventory/market').catch(()=>({live:[],market:[]}));
    const liveRows = (data.live||[]).map(r=>[esc(`${r.origin||''} → ${r.destination||''}`),esc(r.flight_date||''),esc(r.flights||0),esc(r.bookings||0),money(r.revenue||0)]);
    const storedRows = (data.market||[]).map(r=>[esc(r.market||''),esc(r.flight_date||''),esc(r.demand||0),esc(r.bookings||0),money(r.revenue||0),esc(r.updated_at||'')]);
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Market data reports <button class="mini-btn primary" data-inv-market-refresh>Refresh market data</button></h3></div><div class="panel-body"><h4>Live market activity</h4>${invTable(['Market','Date','Flights','Bookings','Revenue'],liveRows,'No market activity')}<h4 style="margin-top:14px">Stored market snapshot</h4>${invTable(['Market','Date','Demand','Bookings','Revenue','Updated'],storedRows,'No market snapshots')}</div></div>`;
    $('[data-inv-market-refresh]', panel).onclick = async ()=>{ try{ const r=await api('/api/pss/inventory/market/refresh',{method:'POST',body:'{}'}); toast(`Refreshed ${r.refreshed||0} market rows`); render_flight_inventory(el); }catch(err){ toast(err.message,'error'); } };
    return;
  }
  if(tab==='help'){
    const data = await api('/api/pss/inventory/help').catch(()=>({articles:[],docs:[]}));
    panel.innerHTML = `<div class="panel"><div class="panel-head"><h3>Online help &amp; self-service support</h3><input class="form-input" id="inv-help-q" placeholder="Search help..." style="max-width:260px"></div><div class="panel-body"><div id="inv-help-docs" class="muted small" style="margin-bottom:10px"></div><div class="grid" id="inv-help-grid"></div><div class="panel" style="margin-top:12px"><div class="panel-head"><h4>Problem-solving checklist</h4></div><div class="panel-body"><ol><li>Recheck flight date, POS, subclass, segment.</li><li>Publish availability after rule/capacity changes.</li><li>Inspect Queue/History for latest action and assignee.</li><li>Use Market/Valuation before manual overrides.</li></ol></div></div></div></div>`;
    const renderHelp=()=>{ const q=($('#inv-help-q',panel)?.value||'').toLowerCase(); const arts=(data.articles||[]).filter(a=>!q||`${a.title} ${a.body}`.toLowerCase().includes(q)); $('#inv-help-grid',panel).innerHTML=arts.map(a=>`<div class="panel"><div class="panel-head"><h4>${esc(a.title)}</h4></div><div class="panel-body">${esc(a.body)}</div></div>`).join('')||'<div class="muted">No help articles match.</div>'; $('#inv-help-docs',panel).innerHTML=(data.docs||[]).map(d=>`<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a>`).join(' · ')||'Docs: README.md, DEPLOYMENT.md'; };
    $('#inv-help-q',panel).oninput=renderHelp; renderHelp();
    return;
  }
  panel.innerHTML = `<div class="panel"><div class="panel-body">Unknown inventory tab.</div></div>`;
}
async function render_seat_hold(el){
  const meta = viewMeta['seat-hold'] || ['Seat Hold','Temporary seat reservations with release and availability control','Airline PSS'];
  let d = {};
  try{ d = await api('/api/pss/seat-holds'); }
  catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const holds = Array.isArray(d.holds) ? d.holds : [];
  const flights = Array.isArray(d.flights) ? d.flights : [];
  const flightOpts = flights.map(f=>`<option value="${esc(f.id)}">${esc(f.flight_no||'Flight '+f.id)} · ${esc(f.origin)}→${esc(f.destination)} · ${esc(f.flight_date)} · ${esc(f.available_seats)} seats</option>`).join('') || '<option value="">No scheduled flights</option>';
  const holdForm = `<form id="f-seat-hold" class="form-grid">
    <div class="form-cols"><div><label>Flight *</label><select name="flight_id" required>${flightOpts}</select></div><div><label>Passenger *</label><input name="passenger_name" required></div></div>
    <div class="form-cols"><div><label>Seat</label><input name="seat_no" placeholder="e.g. 12A"></div><div><label>PNR (optional)</label><input name="pnr" placeholder="auto if blank"></div></div>
    <div class="form-cols"><div><label>Contact email</label><input name="contact_email" type="email"></div><div><label>Contact phone</label><input name="contact_phone"></div></div>
    <div class="cta-row"><button type="submit" class="primary">Hold seat</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div>
    <div id="f-seat-hold-msg" class="small"></div>
  </form>`;
  const holdRows = holds.map(h=>`<tr><td>${esc(h.pnr||'-')}</td><td>${esc(h.passenger_name||'-')}</td><td>${esc(h.flight_no||'-')}</td><td>${esc(h.origin||'')} → ${esc(h.destination||'')}</td><td>${esc(h.flight_date||'-')}</td><td>${esc(h.seat_no||'-')}</td><td>${esc(h.created_at||'-')}</td><td><button class="mini-btn danger" data-release="${esc(h.id)}">Release</button></td></tr>`).join('') || '<tr><td colspan="8" class="empty">No active seat holds.</td></tr>';
  const flightRows = flights.map(f=>`<tr><td>${esc(f.flight_no||'-')}</td><td>${esc(f.origin)} → ${esc(f.destination)}</td><td>${esc(f.flight_date||'-')}</td><td>${esc(f.dep_time||'-')}</td><td><b>${esc(f.available_seats)}</b></td></tr>`).join('') || '<tr><td colspan="5" class="empty">No scheduled flights.</td></tr>';
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn primary" data-action="seat-hold-create">+ Hold seat</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Active holds',holds.length,'task')}${kpi('Scheduled flights',flights.length,'booking')}${kpi('Seats available',flights.reduce((s,f)=>s+Number(f.available_seats||0),0),'chart')}</div>
    <div class="panel"><div class="panel-head"><h3>Active seat holds</h3><span class="badge info">${holds.length}</span></div><div class="panel-body"><div class="table-wrap"><table><thead><tr><th>PNR</th><th>Passenger</th><th>Flight</th><th>Route</th><th>Date</th><th>Seat</th><th>Held at</th><th></th></tr></thead><tbody>${holdRows}</tbody></table></div></div></div>
    <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Flight availability</h3></div><div class="panel-body"><div class="table-wrap"><table><thead><tr><th>Flight</th><th>Route</th><th>Date</th><th>Departure</th><th>Seats left</th></tr></thead><tbody>${flightRows}</tbody></table></div></div></div>`;
  const createBtn = $('[data-action="seat-hold-create"]', el);
  if(createBtn) createBtn.onclick = ()=>{
    showModal('Hold a seat', holdForm);
    const form = $('#f-seat-hold');
    if(form) form.onsubmit = async (e)=>{
      e.preventDefault();
      const msg = $('#f-seat-hold-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
      const data = Object.fromEntries(new FormData(form).entries());
      try{
        const r = await api('/api/pss/seat-holds',{method:'POST',body:JSON.stringify(data)});
        toast('Seat held'+(r.pnr?` (${r.pnr})`:'')); hideModal(); render_seat_hold(el);
      }catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
    };
  };
  el.querySelectorAll('[data-release]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-release');
      if(!id || !confirm('Release this seat hold?')) return;
      try{ await apiDelete('/api/pss/seat-holds', id); toast('Seat released'); render_seat_hold(el); }
      catch(err){ toast(err.message,'error'); }
    };
  });
}

async function render_unused_tickets(el){
  const meta = viewMeta['unused-tickets'] || ['Unused Tickets','Unused e-ticket tracking, refunds and expiry control','Travel'];
  let d = {};
  try{ d = await api('/api/setup/unused-tickets'); }
  catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const rows = Array.isArray(d.unused_tickets) ? d.unused_tickets : [];
  const auto = d.automation || {};
  const unused = rows.filter(r=>(r.ticket_status||'').toLowerCase()==='unused');
  const expired = rows.filter(r=>(r.ticket_status||'').toLowerCase()==='expired');
  const refunds = rows.filter(r=>(r.refund_status||'').toLowerCase()!=='pending');
  const value = rows.reduce((s,r)=>s+Number(r.value||0),0);
  const body = rows.map(r=>`<tr>
    <td><b>${esc(r.ticket_no||'-')}</b></td><td>${esc(r.pnr||'-')}</td><td>${esc(r.traveler_name||'-')}</td>
    <td>${esc(r.issue_date||'-')}</td><td>${esc(r.travel_date||'-')}</td><td>${esc(r.expiry_date||'-')}</td>
    <td>${statusBadge(r.ticket_status||'unused')}</td><td>${statusBadge(r.refund_status||'pending')}</td>
    <td>${money(r.value,r.currency)}</td>
    <td><button class="mini-btn secondary" data-action="ut-refund" data-id="${esc(r.id)}" data-ticket="${esc(r.ticket_no)}" data-value="${esc(r.value)}" data-currency="${esc(r.currency)}">Refund</button></td>
  </tr>`).join('') || '<tr><td colspan="10" class="empty">No unused tickets yet. Issued e-tickets are imported automatically.</td></tr>';
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn secondary" data-action="run-automation">Run automation</button> <button class="mini-btn primary" data-action="open-portal">Open booking portal</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Unused',unused.length,'file')}${kpi('Expired',expired.length,'task')}${kpi('Refund tracked',refunds.length,'finance')}${kpi('Ticket value',money(value),'chart')}</div>
    <div class="panel"><div class="panel-head"><div><h3>Automated e-ticket ledger</h3><div class="muted tiny">Imported ${auto.created||0} new · Expired ${auto.expired||0} · Refund records ${auto.refunds||0}</div></div></div><div class="panel-body"><div class="table-wrap"><table><thead><tr><th>Ticket</th><th>PNR</th><th>Traveler</th><th>Issued</th><th>Travel</th><th>Expiry</th><th>Status</th><th>Refund</th><th>Value</th><th></th></tr></thead><tbody>${body}</tbody></table></div></div></div>`;
  const runBtn = $('[data-action="run-automation"]', el);
  if(runBtn) runBtn.onclick = async ()=>{
    try{ const r = await api('/api/automation/run',{method:'POST',body:'{}'}); toast(`Automation complete: ${r.unused_tickets?.created||0} tickets, ${r.fare_optimization?.updated||0} fares`); render_unused_tickets(el); }
    catch(err){ toast(err.message,'error'); }
  };
  const portalBtn = $('[data-action="open-portal"]', el);
  if(portalBtn) portalBtn.onclick = ()=> window.open('/portal/fayaka','_blank');
  el.querySelectorAll('[data-action="ut-refund"]').forEach(btn=>{
    btn.onclick = ()=>{
      const id = btn.getAttribute('data-id');
      const ticket = btn.getAttribute('data-ticket');
      const val = btn.getAttribute('data-value') || '0';
      const cur = btn.getAttribute('data-currency') || 'USD';
      showModal('Request refund', `<form id="f-ut-refund" class="form-grid">
        <div class="form-cols"><div><label>Ticket</label><input value="${esc(ticket)}" disabled></div><div><label>Amount (${esc(cur)})</label><input name="amount" type="number" min="1" step="0.01" value="${esc(val)}" required></div></div>
        <div><label>Notes</label><input name="notes" placeholder="Optional"></div>
        <div class="cta-row"><button type="submit" class="primary">Request refund</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div>
        <div id="f-ut-refund-msg" class="small"></div>
      </form>`);
      const form = $('#f-ut-refund');
      if(form) form.onsubmit = async (e)=>{
        e.preventDefault();
        const msg = $('#f-ut-refund-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
        const data = Object.fromEntries(new FormData(form).entries()); data.id = id; data.ticket_no = ticket;
        try{ const r = await api('/api/setup/unused-tickets/refund',{method:'POST',body:JSON.stringify(data)}); toast(`Refund ${r.refund_no||'requested'}`); hideModal(); render_unused_tickets(el); }
        catch(err){ if(msg){ msg.textContent=err.message; msg.className='small error'; } else toast(err.message,'error'); }
      };
    };
  });
}

async function render_fare_optimization(el){
  const meta = viewMeta['fare-optimization'] || ['Fare Optimization','Automatic fare adjustments from load factor and departure pressure','Revenue'];
  let d = {};
  try{ d = await api('/api/setup/fare-optimization'); }
  catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const rows = Array.isArray(d.suggestions) ? d.suggestions : [];
  const body = rows.map(r=>`<tr><td>${esc(r.flight_no||'-')}</td><td>${esc(r.load_factor)}</td><td>${esc(r.days_to_departure)}</td><td>${money(r.current_fare)}</td><td><b>${money(r.recommended_fare)}</b></td><td>${esc(r.action||'-')}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">All fares are currently optimized.</td></tr>';
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn secondary" data-action="optimize-all">Run all automation</button> <button class="mini-btn primary" data-action="optimize-fares">Apply fare optimization</button>`)}
    <div class="grid kpi-row" style="margin-bottom:16px">${kpi('Recommendations',rows.length,'chart')}${kpi('Last applied',d.updated||0,'task')}${kpi('Rule','Load + departure','setting')}</div>
    <div class="panel"><div class="panel-head"><h3>Fare recommendations</h3><span class="badge info">${rows.length}</span></div><div class="panel-body"><div class="table-wrap"><table><thead><tr><th>Flight</th><th>Load factor</th><th>Days to departure</th><th>Current</th><th>Recommended</th><th>Action</th></tr></thead><tbody>${body}</tbody></table></div></div></div>`;
  const apply = $('[data-action="optimize-fares"]', el);
  if(apply) apply.onclick = async ()=>{
    try{ const r = await api('/api/setup/fare-optimization',{method:'POST',body:'{}'}); toast(`Updated ${r.updated||0} fares`); render_fare_optimization(el); }
    catch(err){ toast(err.message,'error'); }
  };
  const all = $('[data-action="optimize-all"]', el);
  if(all) all.onclick = async ()=>{
    try{ const r = await api('/api/automation/run',{method:'POST',body:'{}'}); toast(`Automation complete: ${r.fare_optimization?.updated||0} fares`); render_fare_optimization(el); }
    catch(err){ toast(err.message,'error'); }
  };
}

async function render_revenue(el){
  const range = state.cache.revenueRange || {from:new Date(Date.now()-29*86400000).toISOString().slice(0,10), to:today()};
  const params = new URLSearchParams({from:range.from, to:range.to});
  const d = await api(`/api/revenue/dashboard?${params.toString()}`);
  const k = d.kpis || {};
  const filter = `<form id="f-revenue-range" class="form-grid">
    <div class="form-cols"><div><label>From</label><input type="date" name="from" value="${esc(range.from)}" required></div><div><label>To</label><input type="date" name="to" value="${esc(range.to)}" required></div></div>
    <div class="cta-row"><button type="submit">Apply</button><button type="button" class="secondary" onclick="window.open('/docs','_blank')">API docs</button></div>
  </form>`;
  const bars = (rows, label, value) => {
    const max = Math.max(...rows.map(r=>Number(r[value]||0)), 1);
    return table(rows, [label,value], (r,c)=> c===value ? `${money(r[c])}<div style="height:4px;background:var(--line);border-radius:4px;margin-top:4px"><div style="height:4px;background:#0ea5e9;border-radius:4px;width:${Math.round(Number(r[value]||0)/max*100)}%"></div></div>` : esc(r[c]));
  };
  el.innerHTML = pageHead('Revenue dashboard', `Revenue performance from ${range.from} to ${range.to}`, '')
    + panel('Date range', filter, '', 'All booking, cash and ancillary figures use this period')
    + `<div class="kpi-grid">${kpi('Gross revenue',money(k.gross_revenue),'finance','booking value')}${kpi('Cash collected',money(k.cash_collected),'finance','received payments')}${kpi('Ancillary',money(k.ancillary_revenue),'finance','extra services')}${kpi('Refunds',money(k.refunds),'finance','refund outflow',true)}${kpi('Net revenue',money(k.net_revenue),'chart','gross + ancillary - refunds')}${kpi('Bookings',fmt(k.bookings),'booking',`avg ${money(k.average_booking_value)}`)}</div>`
    + panel('Daily revenue', bars(d.series||[], 'date', 'revenue'), '', 'Gross booking value by date')
    + `<div class="form-cols">${panel('Revenue by channel', bars(d.channels||[], 'name', 'revenue'))}${panel('Revenue by route', bars(d.routes||[], 'route', 'revenue'))}</div>`
    + panel('Recent bookings', table((d.recent_bookings||[]).map(b=>({...b,route:`${b.origin||''} ${b.destination||''}`.trim()})), ['pnr','passenger_name','route','total_amount','payment_status','booking_status'], (r,c)=> ['total_amount'].includes(c)?money(r[c]):['payment_status','booking_status'].includes(c)?statusBadge(r[c]):esc(r[c])));
  const form = $('#f-revenue-range', el);
  form.onsubmit = async (e)=>{ e.preventDefault(); state.cache.revenueRange = Object.fromEntries(new FormData(form).entries()); render_revenue(el); };
}

async function render_developer(el){
  const [keys, hooks, deliveries, docs] = await Promise.all([
    api('/api/developer/api-keys'), api('/api/developer/webhooks'), api('/api/developer/deliveries'), api('/api/public/v1/docs')
  ]);
  const keyRows = (keys.keys||[]).map(k=>({...k,statusBadge:statusBadge(k.status)}));
  const hookRows = (hooks.webhooks||[]).map(h=>({...h,statusBadge:statusBadge(h.status)}));
  el.innerHTML = pageHead('Developer Center', 'Issue partner keys, subscribe to events and monitor webhook delivery',
      `<button class="mini-btn" id="dev-new-key">Create API key</button><button class="mini-btn" id="dev-new-hook">Add webhook</button><a class="mini-btn secondary" href="/docs" target="_blank">Open API docs</a>`)
    + `<div class="kpi-grid">${kpi('Active keys',fmt((keys.keys||[]).filter(k=>k.status==='active').length),'setting','partner access')}${kpi('Webhooks',fmt((hooks.webhooks||[]).length),'source','event subscribers')}${kpi('Deliveries',fmt((deliveries.deliveries||[]).length),'task','last 200 attempts')}${kpi('Failed',fmt((deliveries.deliveries||[]).filter(d=>d.status==='failed').length),'task','retry available',true)}</div>`
    + panel('Partner API keys', table(keyRows,['name','token_prefix','scopes','statusBadge','last_used_at','expires_at'],(r,c)=>c==='statusBadge'?r[c]:esc(r[c])), '', 'Tokens are shown only once when created')
    + panel('Webhook subscriptions', table(hookRows,['name','url','events','statusBadge','secret_hint'],(r,c)=>c==='statusBadge'?r[c]:esc(r[c])), '', `Events: ${(hooks.events||[]).join(', ')}`)
    + panel('Delivery log', table(deliveries.deliveries||[],['event','object_id','webhook','status','attempts','response_code','created_at','id'],(r,c)=>{
        if(c==='status') return statusBadge(r[c]);
        if(c==='id') return `<button class="mini-btn secondary dev-retry" data-id="${esc(r.id)}">Retry</button>`;
        return esc(r[c]);
      }), '', 'HMAC signature: X-Fayaka-Signature: sha256=<hex>')
    + panel('Endpoint catalog', `<pre>${esc(JSON.stringify(docs.endpoints||[],null,2))}</pre>`, '', docs.auth);
  $('#dev-new-key',el).onclick = ()=>{
    showModal('Create partner API key', `<form id="f-api-key" class="form-grid">
      <div><label>Key name *</label><input name="name" placeholder="e.g. Sales partner" required></div>
      <div><label>Scopes</label><input name="scopes" value="offers:write,orders:write"></div>
      <div><label>Expires at</label><input name="expires_at" type="datetime-local"></div>
      <div class="cta-row"><button type="submit">Create key</button><button type="button" class="secondary" onclick="hideModal()">Cancel</button></div>
      <div id="f-api-key-msg" class="small error"></div></form>`);
    $('#f-api-key').onsubmit = async (e)=>{
      e.preventDefault(); const f=e.target; const data=Object.fromEntries(new FormData(f).entries());
      try{ const r=await api('/api/developer/api-keys',{method:'POST',body:JSON.stringify(data)});
        showModal('Copy API key', `<p>${esc(r.message)}</p><pre>${esc(r.token)}</pre><button class="mini-btn" onclick="hideModal()">Done</button>`); render_developer(el);
      }catch(err){ $('#f-api-key-msg').textContent=err.message; }
    };
  };
  $('#dev-new-hook',el).onclick = ()=>{
    showModal('Add webhook', `<form id="f-webhook" class="form-grid">
      <div><label>Name *</label><input name="name" required></div>
      <div><label>Endpoint URL *</label><input name="url" placeholder="https://partner.example.com/webhooks" required></div>
      <div><label>Events</label><input name="events" value="order.created,booking.created,payment.received,rebooking.confirmed"></div>
      <div class="cta-row"><button type="submit">Save webhook</button><button type="button" class="secondary" onclick="hideModal()">Cancel</button></div>
      <div id="f-webhook-msg" class="small error"></div></form>`);
    $('#f-webhook').onsubmit = async (e)=>{
      e.preventDefault(); const data=Object.fromEntries(new FormData(e.target).entries());
      try{ const r=await api('/api/developer/webhooks',{method:'POST',body:JSON.stringify(data)});
        showModal('Copy signing secret', `<p>${esc(r.message)}</p><pre>${esc(r.secret)}</pre><button class="mini-btn" onclick="hideModal()">Done</button>`); render_developer(el);
      }catch(err){ $('#f-webhook-msg').textContent=err.message; }
    };
  };
  $$('.dev-retry',el).forEach(b=>b.onclick=async()=>{ try{ await api(`/api/developer/webhooks/${b.dataset.id}/retry`,{method:'POST',body:'{}'}); toast('Webhook retried'); render_developer(el); }catch(err){ toast(err.message,'error'); } });
}

async function render_onboarding(el){
  const d = await api('/api/onboarding/status');
  const rawNotes = d.progress?.data_json;
  let notes = {};
  try{ notes = typeof rawNotes==='string' ? JSON.parse(rawNotes||'{}') : (rawNotes||{}); }catch(e){ notes = {}; }
  const rows = d.checklist.map(c=>`<tr><td>${esc(c.label)}</td><td>${c.done?'<span class="badge good">ready</span>':'<span class="badge warn">pending</span>'}</td><td><button class="mini-btn secondary onboarding-go" data-view="${esc(c.path)}">Open</button></td></tr>`).join('');
  const canProvision = currentGroup()==='airline_admin';
  el.innerHTML = pageHead('Airline onboarding', 'Launch a new airline tenant and track launch readiness', '')
    + `<div class="kpi-grid">${kpi('Readiness',`${d.checklist.filter(c=>c.done).length}/${d.checklist.length}`,'task',d.completed?'ready to launch':'setup incomplete')}${kpi('Users',fmt(d.counts.users),'users','team accounts')}${kpi('Flights',fmt(d.counts.flights),'booking','scheduled flights')}${kpi('API keys',fmt(d.counts.api_keys),'setting','partner access')}</div>`
    + panel('Launch checklist', `<table><thead><tr><th>Step</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table>`, '', `Current step: ${esc(d.progress?.step||'profile')}`)
    + (canProvision ? panel('Create another airline tenant', `<form id="f-new-tenant" class="form-grid">
        <div class="form-cols"><div><label>Airline name *</label><input name="airline_name" required></div><div><label>Tenant code *</label><input name="code" placeholder="e.g. SKY" maxlength="12" required></div></div>
        <div class="form-cols"><div><label>Admin username</label><input name="admin_username" value="admin"></div><div><label>Admin email</label><input name="admin_email" type="email"></div></div>
        <div><label>Admin password (leave blank to generate)</label><input name="admin_password" type="password"></div>
        <div class="cta-row"><button type="submit">Provision airline</button></div>
        <div id="f-new-tenant-msg" class="small error"></div></form>`, '', 'This creates an isolated tenant with seeded PSS defaults') : '')
    + panel('Progress notes', `<form id="f-onboarding-note" class="form-grid"><div><label>Internal note</label><textarea name="note" rows="3">${esc(notes.note||'')}</textarea></div><div class="cta-row"><button type="submit">Save note</button></div></form>`);
  $$('.onboarding-go',el).forEach(b=>b.onclick=()=>{ if(canView(b.dataset.view)) setView(b.dataset.view); else toast('Your role cannot open this setup view','error'); });
  const tenantForm = $('#f-new-tenant');
  if(tenantForm) tenantForm.onsubmit = async (e)=>{
    e.preventDefault(); const data=Object.fromEntries(new FormData(e.target).entries());
    try{ const r=await api('/api/onboarding/create-tenant',{method:'POST',body:JSON.stringify(data)});
      showModal('Airline created', `<p>Save these credentials now.</p><pre>${esc(JSON.stringify(r,null,2))}</pre><button class="mini-btn" onclick="hideModal()">Done</button>`); render_onboarding(el);
    }catch(err){ $('#f-new-tenant-msg').textContent=err.message; }
  };
  $('#f-onboarding-note').onsubmit = async (e)=>{
    e.preventDefault(); const data=Object.fromEntries(new FormData(e.target).entries());
    try{ await api('/api/onboarding/status',{method:'POST',body:JSON.stringify({step:'profile',data})}); toast('Onboarding note saved'); render_onboarding(el); }
    catch(err){ toast(err.message,'error'); }
  };
}

async function render_disruptions(el){
  const cached = state.cache.disruptionImpact;
  const data = cached ? await Promise.resolve(cached) : {affected:[],alternatives:[],history:[]};
  el.innerHTML = pageHead('Disruptions', 'Identify impacted PNRs, choose alternate flights and confirm rebooking', '')
    + panel('Find impact', `<form id="f-disruption-impact" class="form-grid">
        <div class="form-cols"><div><label>Flight ID</label><input name="flight_id" value="${esc(data.flight_id||'')}"></div><div><label>Or flight number</label><input name="flight_no" value="${esc(data.flight_no||'')}" placeholder="e.g. UG201"></div></div>
        <div class="cta-row"><button type="submit">Find affected bookings</button></div></form>`, '', 'Use a flight ID or flight number')
    + panel('Affected bookings', table(data.affected||[],['pnr','passenger_name','flight_no','flight_date','origin','destination','booking_status','id'],(r,c)=>{
        if(c==='booking_status') return statusBadge(r[c]);
        if(c==='id') return `<button class="mini-btn disruption-rebook" data-id="${esc(r.id)}" data-pnr="${esc(r.pnr)}">Rebook</button>`;
        return esc(r[c]);
      }))
    + panel('Alternate flights', table(data.alternatives||[],['flight_no','flight_date','dep_time','origin','destination','available_seats','base_fare','id'],(r,c)=>c==='id'?`<button class="mini-btn secondary disruption-pick" data-id="${esc(r.id)}">${esc(r.flight_no)}</button>`:esc(r[c])))
    + panel('Rebooking history', table(data.history||[],['created_at','booking_id','new_flight_id','reason','fare_difference','waiver','status','notify_sent'],(r,c)=>['waiver','notify_sent'].includes(c)?(r[c]?'<span class="badge good">yes</span>':'<span class="badge info">no</span>'):['status'].includes(c)?statusBadge(r[c]):esc(r[c])));
  $('#f-disruption-impact').onsubmit = async (e)=>{
    e.preventDefault(); const q = Object.fromEntries(new FormData(e.target).entries());
    try{ const r=await api(`/api/pss/disruptions/impact?${new URLSearchParams(q)}`); state.cache.disruptionImpact={...r,...q}; render_disruptions(el); }
    catch(err){ toast(err.message,'error'); }
  };
  let selectedFlight = '';
  $$('.disruption-pick',el).forEach(b=>b.onclick=()=>{ selectedFlight=b.dataset.id; toast(`Selected flight ${b.textContent}`); });
  $$('.disruption-rebook',el).forEach(b=>b.onclick=()=>{
    showModal(`Rebook ${b.dataset.pnr}`, `<form id="f-rebook" class="form-grid">
      <div><label>New flight *</label><select name="new_flight_id" required>${(data.alternatives||[]).map(f=>`<option value="${esc(f.id)}" ${selectedFlight===String(f.id)?'selected':''}>${esc(f.flight_no)} · ${esc(f.flight_date)} ${esc(f.dep_time||'')}</option>`).join('')||'<option value="">No alternates</option>'}</select></div>
      <div><label>Reason</label><input name="reason" value="Involuntary schedule change"></div>
      <div class="form-cols"><div><label>Fare difference</label><input name="fare_difference" type="number" step="0.01" value="0"></div><div><label>Waive fare difference</label><select name="waiver"><option value="1">Yes</option><option value="0">No</option></select></div></div>
      <input type="hidden" name="booking_id" value="${esc(b.dataset.id)}">
      <div class="cta-row"><button type="submit">Confirm rebooking</button><button type="button" class="secondary" onclick="hideModal()">Cancel</button></div>
      <div id="f-rebook-msg" class="small error"></div></form>`);
    $('#f-rebook').onsubmit = async (e)=>{
      e.preventDefault(); const payload=Object.fromEntries(new FormData(e.target).entries());
      payload.new_flight_id=Number(payload.new_flight_id); payload.booking_id=Number(payload.booking_id); payload.waiver=payload.waiver==='1';
      try{ const r=await api('/api/pss/disruptions/rebook',{method:'POST',body:JSON.stringify(payload)}); hideModal(); toast(`Rebooked ${r.pnr}`); state.cache.disruptionImpact=null; render_disruptions(el); }
      catch(err){ $('#f-rebook-msg').textContent=err.message; }
    };
  });
}

async function render_agency_profile(el){
  const d = await api('/api/setup/agency-profile');
  const row = {...(d.profile||{}), ...(d.branding||{})};
  const logo = row.logo_data || '';
  const logoHtml = `<div class="logo-panel">
    <div class="logo-preview-box">
      <img id="agency-logo-preview" class="logo-preview ${logo?'':'hidden'}" src="${esc(logo)}" alt="Agency logo" />
      <div id="agency-logo-empty" class="empty ${logo?'hidden':''}">No logo uploaded</div>
    </div>
    <div class="logo-actions">
      <label class="mini-btn secondary file-btn">Choose logo<input id="agency-logo-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/svg+xml" /></label>
      <button type="button" id="agency-logo-clear" class="mini-btn danger">Remove logo</button>
      <input id="agency-logo-data" type="hidden" name="logo_data" value="${esc(logo)}" />
      <div class="muted tiny">PNG, JPG, WEBP, GIF or SVG · maximum 2 MB. Stored securely as a data URL and used on documents and shares.</div>
    </div>
  </div>`;
  el.innerHTML = pageHead('Agency Profile','Company identity, branding and fiscal defaults','')
    + `<form id="f-agency-profile" class="form-grid">
        <div class="grid two" style="gap:16px">
          <div class="panel"><div class="panel-head"><h3>Company &amp; fiscal defaults</h3></div><div class="panel-body"><div class="form-cols">${schemaFields(AGENCY_PROFILE_FIELDS,row)}</div></div></div>
          <div class="panel"><div class="panel-head"><h3>Logo</h3><span class="muted tiny">Brand identity</span></div><div class="panel-body">${logoHtml}</div></div>
        </div>
        <div class="cta-row" style="margin-top:14px"><button type="submit" class="primary">Save profile</button></div>
        <div id="f-agency-profile-msg" class="small"></div>
      </form>`;
  const fileInput = $('#agency-logo-file');
  if(fileInput) fileInput.onchange = ()=>{
    const file = fileInput.files && fileInput.files[0];
    if(!file) return;
    if(!file.type.startsWith('image/')){ toast('Choose an image file','error'); fileInput.value=''; return; }
    if(file.size > 2 * 1024 * 1024){ toast('Logo must be smaller than 2 MB','error'); fileInput.value=''; return; }
    const reader = new FileReader();
    reader.onload = ()=>{
      const data = $('#agency-logo-data'); if(data) data.value = reader.result;
      const img = $('#agency-logo-preview');
      if(img){ img.src = reader.result; img.classList.remove('hidden'); }
      const empty = $('#agency-logo-empty'); if(empty) empty.classList.add('hidden');
    };
    reader.readAsDataURL(file);
  };
  const clearBtn = $('#agency-logo-clear');
  if(clearBtn) clearBtn.onclick = ()=>{
    const data = $('#agency-logo-data'); if(data) data.value = '';
    const img = $('#agency-logo-preview'); if(img){ img.src=''; img.classList.add('hidden'); }
    const empty = $('#agency-logo-empty'); if(empty) empty.classList.remove('hidden');
    if(fileInput) fileInput.value = '';
  };
  const form = $('#f-agency-profile');
  if(form) form.onsubmit = async e=>{
    e.preventDefault();
    const msg = $('#f-agency-profile-msg');
    const payload = schemaPayload(form, AGENCY_PROFILE_FIELDS);
    payload.logo_data = $('#agency-logo-data')?.value || '';
    try{
      await api('/api/setup/agency-profile',{method:'POST',body:JSON.stringify(payload)});
      toast('Agency profile saved'); render_agency_profile(el);
    }catch(err){ if(msg){ msg.textContent = err.message; msg.className = 'small error'; } else toast(err.message,'error'); }
  };
}
async function render_loyalty(el){
  const d = await api('/api/loyalty/report');
  const k = d.kpis||{};
  const cols = ['loyalty_id','customer','type','email','coupons','refunded','spend'];
  el.innerHTML = pageHead('Loyalty Reporting','Frequent traveler IDs, coupon activity and customer value','')
    + `<div class="kpi-grid">${kpi('Members',fmt(k.members||0),'users')}${kpi('Coupons',fmt(k.coupons||0),'file')}${kpi('Coupon value',money(k.coupon_value||0),'finance')}${kpi('Customer spend',money(k.spend||0),'chart')}</div>`
    + panel('Loyalty members', table(d.rows||[],cols,(r,c)=>['spend','coupon_value','refunded'].includes(c)?money(r[c]):esc(r[c])));
}
async function render_partner_reports(el){
  const d = await api('/api/pss/partner-reports');
  const k = d.kpis||{};
  el.innerHTML = pageHead('Partner Reports','Agency login, booking and settlement performance','')
    + `<div class="kpi-grid">${kpi('Agencies',fmt(k.agencies||0),'users')}${kpi('Partner users',fmt(k.users||0),'users')}${kpi('Bookings',fmt(k.bookings||0),'booking')}${kpi('Collected',money(k.collected||0),'finance')}</div>`
    + panel('Agency performance', table(d.rows||[],['agency','users','bookings','payments','collected'],(r,c)=>c==='collected'?money(r[c]):esc(r[c])));
}
async function renderView(name){
  const el = $(`#view-${name}`);
  if(!el) return;
  try {
    if(name === 'pss-stock') await renderPssStock(el);
    else if(typeof window['render_'+name.replace(/-/g,'_')] === 'function') await window['render_'+name.replace(/-/g,'_')](el);
    else await renderList(name, el);
    addReportButtons(name, el);
    watchReportButtons(name, el);
  } catch(e){
    el.innerHTML = `<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
  }
}

async function renderPssStock(el){
    const [d, ch, ag] = await Promise.all([
      api('/api/pss/stock'),
      api('/api/setup/pss-channels').catch(()=>({channels:[]})),
      api('/api/pss/agencies').catch(()=>({agencies:[]}))
    ]);
    const pools = d.pools || [];
    const allocs = d.allocations || [];
    const channels = ch.channels || [];
    const agencies = ag.agencies || [];
    const wallets = d.wallets || [];
  const poolRows = pools.map(p=>`<tr>
    <td>${esc(p.pool_no||'-')}</td>
    <td>${esc(p.airline_code||'-')} / ${esc(p.prefix||'-')}</td>
    <td>${esc(p.start_serial)} – ${esc(p.end_serial)}</td>
    <td>${esc(p.next_serial)}</td>
    <td><b>${esc(p.remaining)}</b></td>
    <td>${esc(p.location_type||'-')} ${esc(p.location_code||'')}</td>
    <td>${statusBadge(p.status||'active')}</td>
    <td><button class="mini-btn secondary" data-pss-pool-del="${esc(p.id)}">Delete</button></td>
  </tr>`).join('') || '<tr><td colspan="8" class="empty">No stock pools yet</td></tr>';
  const allocRows = allocs.map(a=>`<tr>
    <td>${esc(a.allocation_no||a.id)}</td><td>${esc(a.agency_name||a.allocated_to||a.airport_code||a.channel_id||'-')}</td><td>${esc(a.prefix||'-')}</td><td>${esc(a.start_serial)}�?"${esc(a.end_serial)}</td>
    <td>${esc(a.next_serial)}</td><td>${esc(a.remaining)}</td><td>${money(a.total_amount||0, a.currency||'USD')}</td><td>${statusBadge(a.status||'active')}</td>
  </tr>`).join('') || '<tr><td colspan="8" class="empty">No allocations</td></tr>';

  const poolForm = `<form id="f-pss-stock-pool" class="form-grid">
    <div class="form-cols"><div><label>Pool number</label><input name="pool_no" placeholder="e.g. POOL-001 (auto if blank)"></div><div><label>Status</label><select name="status"><option value="active">active</option><option value="inactive">inactive</option></select></div></div>
    <div class="form-cols"><div><label>Airline code</label><input name="airline_code" value="UG" required></div><div><label>Ticket prefix</label><input name="prefix" placeholder="e.g. UG"></div></div>
    <div class="form-cols"><div><label>Start serial *</label><input name="start_serial" type="number" min="1" required></div><div><label>End serial *</label><input name="end_serial" type="number" min="1" required></div></div>
    <div class="form-cols"><div><label>Location type</label><select name="location_type"><option value="airport">airport</option><option value="channel">channel</option><option value="office">office</option></select></div><div><label>Location code</label><input name="location_code" placeholder="e.g. KLA, GDS, WEB"></div></div>
    <div><label>Notes</label><input name="notes" placeholder="Optional"></div>
    <div class="cta-row"><button type="submit">Create stock pool</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div>
    <div id="f-pss-stock-pool-msg" class="small"></div>
  </form>`;
  const poolOpts = pools.map(p=>`<option value="${esc(p.id)}">${esc(p.pool_no||'Pool '+p.id)} ${esc(p.prefix||'')} · ${esc(p.remaining||0)} left</option>`).join('') || '<option value="">No pools available</option>';
  const channelOpts = channels.map(c=>`<option value="${esc(c.id)}">${esc(c.name||c.code||'Channel '+c.id)}</option>`).join('') || '<option value="">Direct / counter</option>';
  const agencyOpts = `<option value="">No agency (counter/channel)</option>` + agencies.map(a=>`<option value="${esc(a.id)}">${esc(a.name||a.code||('Agency '+a.id))}</option>`).join('');
  const allocForm = `<form id="f-pss-allocate" class="form-grid">
    <div class="form-cols"><div><label>Stock pool *</label><select name="pool_id" required>${poolOpts}</select></div><div><label>Quantity *</label><input name="quantity" type="number" min="1" value="10" required></div></div>
    <div class="form-cols"><div><label>Agency (wallet debit)</label><select name="agency_id">${agencyOpts}</select></div><div><label>Unit price</label><input name="unit_price" type="number" min="0" step="0.01" value="0"></div></div>
    <div class="form-cols"><div><label>Channel</label><select name="channel_id">${channelOpts}</select></div><div><label>Airport code</label><input name="airport_code" placeholder="e.g. KLA"></div></div>
    <div><label>Allocate to label</label><input name="allocated_to" placeholder="Optional display label"></div>
    <div class="muted tiny">If agency and unit price are set, allocation debits agency wallet credit. Top up wallets from Agency Wallet.</div>
    <div class="cta-row"><button type="submit" class="primary">Allocate stock</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div>
    <div id="f-pss-allocate-msg" class="small"></div>
  </form>`;
  const walletRows = wallets.map(w=>`<tr><td>${esc(w.agency_name||w.agency_id)}</td><td>${money(w.balance||0,w.currency||'USD')}</td><td>${esc(w.allocations||0)}</td><td>${money(w.allocated_amount||0,w.currency||'USD')}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">No agency wallets yet</td></tr>';
  el.innerHTML = `${pageHead('Ticket Stock','Create and manage ticket stock pools and allocations.', `<button class="mini-btn secondary" data-action="allocate-stock">Allocate stock</button> <button class="mini-btn primary" data-action="create-pool">+ New stock pool</button>`)}
  <div class="panel"><div class="panel-head"><h3>Stock pools</h3><span class="badge info">${pools.length}</span></div><div class="panel-body">
    <div class="table-wrap"><table><thead><tr><th>Pool</th><th>Airline/Prefix</th><th>Range</th><th>Next</th><th>Remaining</th><th>Location</th><th>Status</th><th></th></tr></thead><tbody>${poolRows}</tbody></table></div>
  </div></div>
  <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Agency wallets</h3><span class="badge info">${wallets.length}</span></div><div class="panel-body">
    <div class="table-wrap"><table><thead><tr><th>Agency</th><th>Balance</th><th>Allocations</th><th>Allocated amount</th></tr></thead><tbody>${walletRows}</tbody></table></div>
  </div></div>
  <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Allocations</h3><span class="badge info">${allocs.length}</span></div><div class="panel-body">
    <div class="table-wrap"><table><thead><tr><th>Allocation</th><th>Agency/Allocated to</th><th>Prefix</th><th>Range</th><th>Next</th><th>Remaining</th><th>Amount</th><th>Status</th></tr></thead><tbody>${allocRows}</tbody></table></div>
  </div></div>`;

  el.querySelectorAll('[data-action="allocate-stock"]').forEach(btn=>{
    btn.onclick = ()=>{
      showModal('Allocate ticket stock', allocForm);
      const form = $('#f-pss-allocate');
      if(form) form.onsubmit = async (e)=>{
        e.preventDefault();
        const msg = $('#f-pss-allocate-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
        const data = Object.fromEntries(new FormData(form).entries());
        try{
          const r = await api('/api/pss/stock/allocate', {method:'POST', body: JSON.stringify(data)});
          toast('Stock allocated'+(r.allocation_no?` (${r.allocation_no})`:''));
          hideModal(); renderPssStock(el);
        }catch(err){ if(msg){ msg.textContent = err.message; msg.className='small error'; } else toast(err.message,'error'); }
      };
    };
  });
  el.querySelectorAll('[data-action="create-pool"]').forEach(btn=>{
    btn.onclick = ()=>{
      showModal('New stock pool', poolForm);
      const form = $('#f-pss-stock-pool');
      if(form) form.onsubmit = async (e)=>{
        e.preventDefault();
        const msg = $('#f-pss-stock-pool-msg'); if(msg){ msg.textContent=''; msg.className='small'; }
        const data = Object.fromEntries(new FormData(form).entries());
        try{
          const r = await api('/api/pss/stock', {method:'POST', body: JSON.stringify(data)});
          toast('Stock pool created'+(r.pool_no?` (${r.pool_no})`:''));
          hideModal(); renderPssStock(el);
        }catch(err){ if(msg){ msg.textContent = err.message; msg.className='small error'; } else toast(err.message,'error'); }
      };
    };
  });
  el.querySelectorAll('[data-pss-pool-del]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-pss-pool-del');
      if(!id || !confirm('Delete this stock pool?')) return;
      try{ await api('/api/pss/stock/'+encodeURIComponent(id), {method:'DELETE'}); toast('Stock pool deleted'); renderPssStock(el); }
      catch(err){ toast(err.message,'error'); }
    };
  });
}

function showLogin(){
  const main = $('#main-view'); if(main) main.classList.add('hidden');
  const login = $('#login-view'); if(login) login.classList.remove('hidden');
}

async function showWorkspace(){
  const login = $('#login-view'); if(login) login.classList.add('hidden');
  const main = $('#main-view'); if(main) main.classList.remove('hidden');
  await loadSession();
  applyRoleNav();
  await loadNotifications();
  setView(ROLE_HOME[currentGroup()] || 'dashboard');
}

function bindLogin(){
  const form = $('#login-form');
  if(!form) return;
  form.onsubmit = async (e)=>{
    e.preventDefault();
    const err = $('#login-error'); if(err) err.textContent = '';
    const data = Object.fromEntries(new FormData(form).entries());
    try{
      await api('/api/login', {method:'POST', body:JSON.stringify(data)});
      await showWorkspace();
    }catch(ex){
      if(err) err.textContent = ex.message || 'Login failed';
      else toast(ex.message || 'Login failed','error');
    }
  };
}

function bindShell(){
  const logout = $('#logout-btn');
  if(logout) logout.onclick = async ()=>{
    $$('.menu-pop').forEach(p=>p.classList.remove('open'));
    try{ await api('/api/logout', {method:'POST', body:'{}'}); }catch(e){}
    location.reload();
  };

  const changePw = $('#change-pw-btn');
  if(changePw) changePw.onclick = ()=>{
    $$('.menu-pop').forEach(p=>p.classList.remove('open'));
    showModal('Change password', `<form id="change-password-form" class="form-grid">
      <div><label>Current password</label><input name="old_password" type="password" required /></div>
      <div><label>New password</label><input name="new_password" type="password" required /></div>
      <div><label>Confirm new password</label><input name="confirm_password" type="password" required /></div>
      <div id="change-password-msg" class="small error"></div>
      <div class="cta-row"><button type="submit">Change password</button></div>
    </form>`);
    const form = $('#change-password-form');
    form.onsubmit = async (e)=>{
      e.preventDefault();
      const msg = $('#change-password-msg'); if(msg) msg.textContent = '';
      const data = Object.fromEntries(new FormData(form).entries());
      try{
        await api('/api/auth/change-password', {method:'POST', body:JSON.stringify(data)});
        hideModal(); toast('Password changed');
      }catch(ex){ if(msg) msg.textContent = ex.message || 'Change failed'; }
    };
  };

  const modalClose = $('#modal-close'); if(modalClose) modalClose.onclick = hideModal;
  initTheme();
  initNavGroups();
  $$('[data-menu-toggle]').forEach(t=>t.setAttribute('aria-expanded','false'));
  $$('#nav a[data-view]').forEach(a=>{
    const label = a.querySelector('span')?.textContent?.trim();
    if(label){ a.title = label; a.setAttribute('aria-label', label); }
    a.onclick = (e)=>{ e.preventDefault(); setView(a.dataset.view); };
  });
  const readAll = $('#notifications-read-all');
  if(readAll) readAll.onclick = async ()=>{
    try{ await api('/api/notifications/read',{method:'POST',body:'{}'}); await loadNotifications(); toast('All notifications marked read'); }
    catch(err){ toast(err.message,'error'); }
  };
  $$('.notification-all').forEach(btn=>{
    btn.onclick = ()=>{ $$('.menu-pop').forEach(p=>p.classList.remove('open')); setView(btn.dataset.view); };
  });
  document.addEventListener('click', (e)=>{
    if(!e.target.closest('.action-cell')) $$('.action-menu').forEach(m=>m.classList.add('hidden'));
    const trigger = e.target.closest('[data-menu-toggle]');
    if(trigger){
      const pop = document.getElementById(trigger.getAttribute('data-menu-toggle'));
      if(pop){
        const open = pop.classList.toggle('open');
        $$('.menu-pop, .action-pop').forEach(p=>{ if(p!==pop) p.classList.remove('open'); });
        $$('[data-menu-toggle]').forEach(t=>t.setAttribute('aria-expanded','false'));
        trigger.setAttribute('aria-expanded', String(open));
        if(open && pop.id === 'notifications-menu') loadNotifications();
      }
      return;
    }
    if(!e.target.closest('.menu-pop') && !e.target.closest('.action-pop')){
      $$('.menu-pop, .action-pop').forEach(p=>p.classList.remove('open'));
      $$('[data-menu-toggle]').forEach(t=>t.setAttribute('aria-expanded','false'));
    }
    const sidebar = $('#sidebar');
    if(navIsMobile() && sidebar?.classList.contains('open') && !e.target.closest('.sidebar') && !e.target.closest('#menu-toggle') && !e.target.closest('#content-menu-toggle')){
      sidebar.classList.remove('open'); syncNavAria();
    }
  });
  document.addEventListener('keydown', (e)=>{
    if(e.key === 'Escape' && navIsMobile() && $('#sidebar')?.classList.contains('open')){
      $('#sidebar').classList.remove('open'); syncNavAria();
    }
    if(e.key === 'Escape'){
      $$('.menu-pop, .action-pop').forEach(p=>p.classList.remove('open'));
      $$('[data-menu-toggle]').forEach(t=>t.setAttribute('aria-expanded','false'));
    }
  });
  const menuToggle = $('#menu-toggle'); if(menuToggle) menuToggle.onclick = toggleNav;
  const contentMenuToggle = $('#content-menu-toggle'); if(contentMenuToggle) contentMenuToggle.onclick = toggleNav;
  try{ applyNavCollapsed(localStorage.getItem('fayaka.navCollapsed') === '1'); }catch(e){ syncNavAria(); }
  window.addEventListener('resize', syncNavAria);
  const search = $('#global-search'); if(search) search.onkeydown = (e)=>{
    if(e.key === 'Enter'){ e.preventDefault(); openGlobalSearch(search.value.trim()); }
  };
}

async function boot(){
  bindLogin();
  bindShell();
  try{
    await api('/api/session');
    await showWorkspace();
  }catch(e){
    showLogin();
  }
}

boot();
