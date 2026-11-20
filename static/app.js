const state = { session: null, plan: null, usage: null, branding: null, cache: {}, menuSeq: 0, pages: {} };

const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const fmt = (n) => (n===null||n===undefined||n==='') ? '-' : (typeof n==='number' ? n.toLocaleString(undefined,{maximumFractionDigits:2}) : n);
const money = (n, c='USD') => (n===null||n===undefined) ? '-' : `${c} ${Number(n).toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2})}`;
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
  if (!res.ok) throw new Error(data.error || res.statusText || 'Request failed');
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
  const good = ['paid','received','active','ticketed','completed','successful','posted','closed'];
  const warn = ['pending','partial','pending_payment','in_progress','open','ticketing_pending','initiated'];
  const bad = ['failed','overdue','cancelled','void','unticketed','revoked'];
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
function kpi(label, value, ic, note='', warn=false){
  return `<div class="kpi-card"><div class="kpi-top"><span class="kpi-label">${esc(label)}</span><span class="kpi-icon">${icon(ic)}</span></div><div class="kpi-value">${esc(value)}</div><div class="trend ${warn?'warn':''}">${note}</div></div>`;
}
function pageHead(title, subtitle, actions=''){
  return `<div class="page-head"><div><h2>${title}</h2><p>${subtitle}</p></div><div class="page-actions">${actions}</div></div>`;
}

const viewMeta = {
  'pss-overview': ['PSS Overview','Airline reservations, inventory, stock and operations command center','Airline PSS'],
  'pss-flights': ['Flights & Inventory','Schedules, seats, fares and flight status','Airline PSS'],
  'pss-bookings': ['PNR / Bookings','Passenger bookings, ticketing and e-tickets','Airline PSS'],
  'pss-stock': ['Ticket Stock','Ticket stock pools, channel allocations and serial control','Airline PSS'],
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
  'pss-branches': ['Offices & Branches','Airport offices, sales branches and check-in desks','Airline PSS'],
  dashboard: ['Dashboard','Live command center for your travel business','Overview'],
  travelers: ['Travelers','Traveler profiles, documents and journey history','Travel'],
  orders: ['Travel Items','Unified flight, hotel, car and miscellaneous travel items','Travel'],
  'unused-tickets': ['Unused Tickets','Unused e-ticket tracking, refunds and expiry control','Travel'],
  crm: ['Customers','Corporate accounts, B2B clients and traveler relationships','Customer & Sales'],
  estimates: ['Estimates','Quotes and proposals before ticket issuance','Customer & Sales'],
  partners: ['Partners','Travel partner and agency management','Customer & Sales'],
  invoicing: ['Invoices','Auto-billing, AR, allocations and customer receipts','Finance'],
  'credit-notes': ['Credit Notes','Refunds, credits and invoice adjustments','Finance'],
  payments: ['Payments','Cash, bank, card and Mobile Money receipts','Finance'],
  bills: ['Bills','Supplier payables, reconciliation and payment control','Finance'],
  expenses: ['Expenses','Operating costs with receipts and GL postings','Finance'],
  journals: ['Manual Journals','Double-entry adjustments and transfers','Finance'],
  'agency-profile': ['Agency Profile','Company identity, address, currency and fiscal settings','Agency Setup'],
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
  sources: ['Sources & Import','GDS, LCC, NDC, portal feeds and raw document vault','Control Center'],
  backoffice: ['Operations Queue','Ticketing, refunds, SLA tasks and daily workflow','Control Center'],
  notifications: ['Notifications','Alerts, reminders and operational notices','Control Center'],
  disruptions: ['Disruptions','Schedule changes, cancellations and recovery handling','Control Center'],
  intelligence: ['Intelligence','Consolidated insights for revenue, partners, unused tickets and disruptions','Control Center'],
  analytics: ['MIS & Analytics','Profitability, productivity and channel intelligence','Control Center'],
  refunds: ['Refund Management','GDS/LCC refunds, policy checks, auto-void and refund tracking','Airline Finance'],
  coupons: ['Coupon Reporting','Coupon creation, cancellations and pre-billing control','Airline Finance'],
  vouchers: ['Vouchers','Hotel, car, transfer and service vouchers with supplier data','Airline Finance'],
  'bank-accounts': ['Cash & Bank','Cash, bank and petty cash accounts with live balances','Airline Finance'],
  'bank-transactions': ['Bank Transactions','Receipts, payments, transfers and bank matching','Airline Finance'],
  reconciliations: ['Reconciliations','BSP, bank, card, supplier and LCC reconciliation control','Airline Finance'],
  admin: ['Admin & Subscription','Users, branches, plans, gateway and platform settings','Control Center']
};


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
  airline_accountant: ['dashboard','finance','invoicing','credit-notes','payments','bills','expenses','journals','analytics','documents','tasks','notifications'],
  airline_reservations: ['dashboard','pss-overview','pss-flights','pss-bookings','pss-stock','pss-eticket-tracker','pss-ticket-status','pss-charters','pss-charter-flights','crm','travelers','documents','tasks','notifications','analytics'],
  airline_operations: ['dashboard','pss-overview','pss-flights','pss-bookings','pss-checkins','pss-weather','pss-airport-docs','pss-disruptions','pss-ticket-status','pss-trace','documents','tasks','notifications','intelligence','analytics'],
  agency_admin: ['pss-workspace','pss-bookings','crm','finance','invoicing','credit-notes','payments','estimates','documents','tasks','notifications'],
  agency_agent: ['pss-workspace','pss-bookings','crm','finance','invoicing','credit-notes','payments','estimates','documents','tasks','notifications'],
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

function setView(name){
  if(!canView(name)) name = ROLE_HOME[currentGroup()] || 'dashboard';
  $$('#nav a').forEach(a=>a.classList.toggle('active', a.dataset.view===name));
  $$('.view').forEach(v=>v.classList.add('hidden'));
  $(`#view-${name}`).classList.remove('hidden');
  const meta = viewMeta[name] || [name,'', 'Workspace'];
  $('#view-title').textContent = meta[0];
  $('#view-subtitle').textContent = meta[1];
  $('#crumb-section').textContent = meta[2];
  $('#sidebar').classList.remove('open');
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
  $('#tenant-box').innerHTML = `<b>${esc(tname)}</b><small>${esc(d.plan?.name||'-')} plan ? ${esc(d.session.role)} access</small>`;
  const userBox = $('#user-box');
  if(userBox){
    const ctx = [d.session.station_code, d.session.role].filter(Boolean).join(' ? ');
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

boot();


async function renderView(name){
  const el = $(`#view-${name}`);
  if(!el) return;
  try {
    if(name === 'pss-stock') return renderPssStock(el);
    if(typeof window['render_'+name.replace(/-/g,'_')] === 'function') return window['render_'+name.replace(/-/g,'_')](el);
    // minimal fallback: keep existing static content if any, else show notice
    if(!el.innerHTML || !el.innerHTML.trim()){
      const meta = viewMeta[name] || [name,'View module not loaded in this build.','Workspace'];
      el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}
        <div class="panel"><div class="panel-body"><div class="empty">This screen’s UI module is not available in the current front-end build. Ticket Stock can now be managed via API; other screens may require restoring the full app.js.</div></div></div>`;
    }
  } catch(e){
    el.innerHTML = `<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
  }
}

async function renderPssStock(el){
  const d = await api('/api/pss/stock');
  const pools = d.pools || [];
  const allocs = d.allocations || [];
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
    <td>${esc(a.allocation_no||a.id)}</td><td>${esc(a.prefix||'-')}</td><td>${esc(a.start_serial)}–${esc(a.end_serial)}</td>
    <td>${esc(a.next_serial)}</td><td>${esc(a.remaining)}</td><td>${esc(a.allocated_to||a.airport_code||a.channel_id||'-')}</td><td>${statusBadge(a.status||'active')}</td>
  </tr>`).join('') || '<tr><td colspan="7" class="empty">No allocations</td></tr>';

  el.innerHTML = `${pageHead('Ticket Stock','Create and manage ticket stock pools and allocations.', '')}
  <div class="grid two" style="align-items:start;gap:16px">
    <div class="panel"><div class="panel-head"><h3>New stock pool</h3></div><div class="panel-body">
      <form id="f-pss-stock-pool" class="form-grid">
        <div class="form-cols"><div><label>Pool number</label><input name="pool_no" placeholder="e.g. POOL-001 (auto if blank)"></div><div><label>Status</label><select name="status"><option value="active">active</option><option value="inactive">inactive</option></select></div></div>
        <div class="form-cols"><div><label>Airline code</label><input name="airline_code" value="UG" required></div><div><label>Ticket prefix</label><input name="prefix" placeholder="e.g. UG"></div></div>
        <div class="form-cols"><div><label>Start serial *</label><input name="start_serial" type="number" min="1" required></div><div><label>End serial *</label><input name="end_serial" type="number" min="1" required></div></div>
        <div class="form-cols"><div><label>Location type</label><select name="location_type"><option value="airport">airport</option><option value="channel">channel</option><option value="office">office</option></select></div><div><label>Location code</label><input name="location_code" placeholder="e.g. KLA, GDS, WEB"></div></div>
        <div><label>Notes</label><input name="notes" placeholder="Optional"></div>
        <div class="cta-row"><button type="submit">Create stock pool</button></div>
        <div id="f-pss-stock-pool-msg" class="small"></div>
      </form>
    </div></div>
    <div class="panel"><div class="panel-head"><h3>Stock pools</h3><span class="badge info">${pools.length}</span></div><div class="panel-body">
      <div class="table-wrap"><table><thead><tr><th>Pool</th><th>Airline/Prefix</th><th>Range</th><th>Next</th><th>Remaining</th><th>Location</th><th>Status</th><th></th></tr></thead><tbody>${poolRows}</tbody></table></div>
    </div></div>
  </div>
  <div class="panel" style="margin-top:16px"><div class="panel-head"><h3>Allocations</h3><span class="badge info">${allocs.length}</span></div><div class="panel-body">
    <div class="table-wrap"><table><thead><tr><th>Allocation</th><th>Prefix</th><th>Range</th><th>Next</th><th>Remaining</th><th>Allocated to</th><th>Status</th></tr></thead><tbody>${allocRows}</tbody></table></div>
  </div></div>`;

  const form = $('#f-pss-stock-pool', el);
  if(form) form.onsubmit = async (e)=>{
    e.preventDefault();
    const msg = $('#f-pss-stock-pool-msg', el); if(msg) msg.textContent='';
    const data = Object.fromEntries(new FormData(form).entries());
    try{
      const r = await api('/api/pss/stock', {method:'POST', body: JSON.stringify(data)});
      toast('Stock pool created'+(r.pool_no?` (${r.pool_no})`:'')); 
      renderPssStock(el);
    }catch(err){ if(msg){ msg.textContent = err.message; msg.className='small error'; } else toast(err.message,'error'); }
  };
  el.querySelectorAll('[data-pss-pool-del]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-pss-pool-del');
      if(!id || !confirm('Delete this stock pool?')) return;
      try{ await api('/api/pss/stock/'+encodeURIComponent(id), {method:'DELETE'}); toast('Stock pool deleted'); renderPssStock(el); }
      catch(err){ toast(err.message,'error'); }
    };
  });
}
