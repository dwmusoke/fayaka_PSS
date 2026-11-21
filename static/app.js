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
const VIEW_API = {
  'pss-overview':'/api/dashboard','pss-flights':'/api/pss/flights','pss-bookings':'/api/pss/bookings',
  'pss-checkins':'/api/pss/checkins','pss-payments':'/api/pss/payments','pss-agencies':'/api/pss/agencies',
  'pss-direct':'/api/pss/direct-etickets','pss-charters':'/api/pss/charters','pss-charter-flights':'/api/pss/charter-flights',
  'pss-trace':'/api/pss/trace','pss-eticket-tracker':'/api/pss/eticket-tracker','pss-aftersales':'/api/pss/service-cases',
  'pss-workspace':'/api/pss/partner-workspace','pss-partner-users':'/api/pss/partner-users','dashboard':'/api/dashboard',
  travelers:'/api/travelers',orders:'/api/orders',crm:'/api/customers',estimates:'/api/estimates',invoicing:'/api/invoices',
  'credit-notes':'/api/credit-notes',bills:'/api/bills',expenses:'/api/expenses',journals:'/api/journals',
  documents:'/api/documents',tasks:'/api/tasks',notifications:'/api/notifications',finance:'/api/finance',
  analytics:'/api/analytics','bank-accounts':'/api/bank-accounts','bank-transactions':'/api/bank-transactions',
  reconciliations:'/api/reconciliations',refunds:'/api/refunds',coupons:'/api/coupons',vouchers:'/api/vouchers',
  intelligence:'/api/intelligence',admin:'/api/admin',sources:'/api/sources',backoffice:'/api/tasks',
  disruptions:'/api/pss/disruptions',airports:'/api/airports',channels:'/api/channels','setup-users':'/api/setup/users',
  'setup-chart':'/api/setup/chart-of-accounts','setup-products':'/api/setup/products','setup-currencies':'/api/setup/currencies',
  'setup-contacts':'/api/setup/contacts','setup-rules':'/api/setup/rules','setup-preferences':'/api/setup/preferences',
  'setup-mail':'/api/setup/mail-templates','setup-integrations':'/api/setup/integrations','agency-profile':'/api/setup/agency-profile',
  'pss-ancillaries':'/api/pss/ancillary-sales','pss-ancillary-sales':'/api/pss/ancillary-sales','pss-baggage':'/api/pss/baggage',
  'pss-airport-docs':'/api/pss/travel-documents','pss-weather':'/api/pss/weather','pss-ticket-status':'/api/pss/ticket-status',
  'pss-branches':'/api/pss/branches','unused-tickets':'/api/orders','partners':'/api/partners'
};
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
  role:['owner','admin','accountant','reservations','operations','checkin','agency_admin','agency_agent','desk-kla','office-kla','office-dxb','office-nbo'],
  priority:['low','medium','high','urgent'],
  urgency:['low','medium','high','critical'],
  account_type:['asset','liability','equity','revenue','expense'],
  location_type:['airport','channel','office']
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
    const sel = opts.map(o=>`<option value="${esc(o)}"${o===String(val)?' selected':''}>${esc(o)}</option>`).join('');
    return `<select name="${esc(key)}">${sel}</select>`;
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
  return `<button class="mini-btn ${danger?'danger':''}${cls?` ${cls}`:''}" data-action="${esc(action)}" data-id="${esc(id)}"${extra?` data-extra="${esc(extra)}"`:''>${esc(label)}</button>`;
}
async function renderList(name, el){
  const meta = viewMeta[name] || [name,'','Workspace'];
  const path = VIEW_API[name] || `/api/${name}`;
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
  const kpis = d && d.kpis ? d.kpis : null;
  const sample = rows.length?rows[0]:{};
  const cols = rows.length?Object.keys(sample):[];
  const idKey = cols.find(c=>c==='id') || cols[0] || 'id';
  const headCols = cols.filter(c=>!FIELD_TYPES[c]?.startsWith('hidden') && c!==idKey);
  const actions = rows.length?`<th style="width:170px">Actions</th>`:'';
  const head = `<tr>${headCols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}${actions}</tr>`;
  const body = rows.map(r=>{
    const rid = esc(r[idKey]||'');
    const cells = headCols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('');
    const act = `<div class="action-cell">
      ${ctaBtn('Edit','edit',rid,'','secondary')}
      ${ctaBtn('Delete','delete',rid,'','danger')}
      ${ctaBtn('Docs','docs',rid,'','secondary')}
    </div>`;
    return `<tr data-id="${rid}">${cells}<td>${act}</td></tr>`;
  }).join('');
  const kpiHtml = kpis?`<div class="grid kpi-row" style="margin-bottom:16px">${Object.entries(kpis).map(([k,v])=>kpi(k,v,'chart',typeof v==='number'?fmt(v):String(v))).join('')}</div>`:'';
  const emptyMsg = rows.length?`${rows.length} record${rows.length>1?'s':''}`:'No records';
  // --- pagination support ---
  const page = d && d.page ? Math.max(1, d.page) : 1;
  const total = d && d.total ? Math.max(1, d.total) : rows.length + (d.extra||0);
  const count = rows.length;
  const pagHtml = pager(name, page, total, count);
  // -------------------------
  const createFields = Object.keys(sample).filter(c=>!FIELD_TYPES[c]?.startsWith('hidden') && c!==idKey && c!=='created_at' && c!=='updated_at');
  const createForm = createFields.length?`<form id="f-create-${esc(name)}" class="form-grid"><div class="form-cols">${createFields.map(k=>`<div><label>${fieldLabel(k)}</label>${inputFor(k,sample[k],false)}</div>`).join('')}</div><div class="cta-row"><button type="submit" class="primary">Create ${fieldLabel(name)}</button><button type="button" class="secondary mini-btn" onclick="this.closest('form').reset()">Reset</button></div><div id="f-create-${esc(name)}-msg" class="small"></div></form>`:`<div class="empty">No creatable fields detected for ${esc(meta[0])}.</div>`;
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn primary" data-action="create-${esc(name)}">+ Create ${fieldLabel(name)}</button>`)}
    ${kpiHtml}
    <div class="panel"><div class="panel-head"><div><h3>${esc(meta[0])} records</h3><div class="muted tiny">${emptyMsg}</div></div><div></div></div><div class="panel-body">
      ${createForm}
      <div class="table-wrap" style="margin-top:16px">${rows.length?`<table><thead>${head}</thead><tbody>${body}</tbody></table>`:`<div class="empty">No records available for ${esc(meta[0])}.</div>`}</div>
    </div></div>`;
  const form = $(`#f-create-${esc(name)}`, el);
  if(form){
    form.onsubmit = async (e)=>{
      e.preventDefault();
      const msg = $(`#f-create-${esc(name)}-msg`, el); if(msg) msg.textContent='';
      const data = Object.fromEntries(new FormData(form).entries());
      try{
        const r = await api(path,{method:'POST',body:JSON.stringify(data)});
        toast('Created'); form.reset(); renderList(name, el);
      }catch(err){ if(msg){ msg.textContent = err.message; msg.className='small error'; } else toast(err.message,'error'); }
    };
  }
  el.querySelectorAll('[data-action="edit"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      try{
        const rd = await api(path+'/'+encodeURIComponent(id));
        const listKey = d && typeof d==='object' && !Array.isArray(d)?Object.keys(d).find(k=>Array.isArray(d[k])):null;
        const row = rd && typeof rd==='object' && !Array.isArray(rd) && rd[idKey]!==undefined?rd:(listKey && d[listKey]?d[listKey].find(x=>String(x[idKey])===String(id)):null) || rows.find(x=>String(x[idKey])===String(id)) || {};
        const ucols = Object.keys(row).filter(c=>!FIELD_TYPES[c]?.startsWith('hidden') && c!==idKey && c!=='created_at' && c!=='updated_at');
        const uhtml = `<form id="f-edit-${id}" class="form-grid"><div class="form-cols">${ucols.map(k=>`<div><label>${fieldLabel(k)}</label>${inputFor(k,row[k],false)}</div>`).join('')}</div><div class="cta-row"><button type="submit" class="primary">Save</button><button type="button" class="secondary mini-btn" onclick="hideModal()">Cancel</button></div><div id="f-edit-${id}-msg" class="small"></div></form>`;
        showModal(`Edit ${fieldLabel(name)} #${id}`, uhtml);
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
  el.querySelectorAll('[data-action="docs"]').forEach(btn=>{
    btn.onclick = async ()=>{
      const id = btn.getAttribute('data-id');
      const kinds = Object.entries(DOC_KINDS).map(([k,[t,d]])=>`<button class="mini-btn secondary" onclick="pickDoc('${esc(k)}','${esc(id)}')">${esc(t)}</button>`).join('');
      showModal('Choose document', `<div style="padding:10px">${kinds.join('')}</div>`);
    };
  });
  el.querySelectorAll(`[data-action="create-${esc(name)}"]`).forEach(btn=>{
    btn.onclick = ()=>{ const f=$(`#f-create-${esc(name)}`, el); if(f) f.scrollIntoView({behavior:'smooth',block:'center'}); };
  });
}

/* ========================= CUSTOMER ENGAGEMENT / DOCUMENTS ========================= */
const DOC_KINDS = {
  'booking-confirmation':['Booking Confirmation','Confirmed reservation and e-ticket details'],
  'itinerary':['Itinerary','Journey schedule, segments and travel plan'],
  'invoice':['Invoice','Amounts, taxes and payment summary'],
  'quotation':['Quotation','Price quote and validity'],
  'statement':['Statement','Account activity and balance'],
  'journey-document':['Journey Document','Complete travel wallet: itinerary, tickets and services']
};
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
function docBodyStart(){ return `<div class="doc-body" style="padding:22px">`; }
function docBodyEnd(){ return `</div>`; }
function docFooter(p){
  return `<div class="doc-foot" style="background:#f4f7fb;border-top:1px solid #dfe7f1;padding:14px 22px;border-radius:0 0 14px 14px;font-size:10px;color:#64748b;text-align:center">${esc(p.footer||'Thank you for travelling with us. This document is generated from your booking and may be shared with the client.')}</div>`;
}
function moneyDoc(v,c){ return v===null||v===undefined||v===''?'-':money(Number(v||0),c||'USD'); }
function kv(label,val){ return `<div class="doc-kv"><div style="font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#64748b">${esc(label)}</div><div style="font-size:13px;font-weight:700">${esc(val||'-')}</div></div>`; }
function docTable(head,rows){
  const h=`<thead><tr>${head.map(h=>`<th style="font-size:9px;text-transform:uppercase;letter-spacing:.6px;color:#64748b;text-align:left;padding:8px 10px;border-bottom:1px solid #e2e8f0">${esc(h)}</th>`).join('')}</tr></thead>`;
  const b=rows.map(r=>`<tr>${r.map(c=>`<td style="font-size:12px;padding:8px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top">${esc(c)}</td>`).join('')}</tr>`).join('');
  return `<table style="width:100%;border-collapse:collapse">${h}<tbody>${b}</tbody></table>`;
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
function shareLinks(url,title,recipient){
  const wa = `https://wa.me/?text=${encodeURIComponent(title+' '+url)}`;
  const mail = `mailto:${recipient||''}?subject=${encodeURIComponent(title)}&body=${encodeURIComponent('Please find attached your travel document: '+url)}`;
  return `<div class="doc-share" style="display:flex;gap:8px;margin-top:10px">
    <a href="${wa}" target="_blank" class="doc-share-btn" style="background:#25d366;color:#fff"><span style="font-size:12px">💬</span> WhatsApp</a>
    <a href="${mail}" class="doc-share-btn" style="background:${DOC_PRIMARY||'#0b2a4a'};color:#fff"><span style="font-size:12px">✉️</span> Email</a>
    <button class="doc-share-btn secondary" onclick="copyShare('${esc(url)}')">Copy link</button>
  </div>`;
}
let DOC_PRIMARY='#0b2a4a';
async function renderDocument(kind,id,el){
  const p = await docBranding(); DOC_PRIMARY = p.primary_color||'#0b2a4a';
  const meta = DOC_KINDS[kind] || [kind,'Document',''];
  let data = {};
  try{ data = await fetchDocData(kind,id); }catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]),esc(meta[1]),'')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const no = data.booking?.pnr || data.booking?.trip_pnr || data.invoice?.invoice_no || data.estimate?.estimate_no || id;
  const date = data.booking?.created_at || data.invoice?.issue_date || data.estimate?.created_at || today();
  const html = `<div class="doc-wrap">
    ${docHeader(kind,no,date,p)}
    ${docShareBar(kind,id,p)}
    ${docContent(kind,data,p)}
    ${docBodyEnd()}
    ${docFooter(p)}
  </div>`;
  el.innerHTML = `${pageHead(esc(meta[0]),esc(meta[1]),`<button class="mini-btn primary" onclick="printDocument()">Print / PDF</button><button class="mini-btn secondary" onclick="hideModal()">Close</button>`)}${html}`;
  showModal(esc(meta[0])+' #'+esc(no), `<div style="max-height:78vh;overflow:auto">${html}</div>`);
}
function docShareBar(kind,id,p){
  const url = `${location.origin}/share/doc?kind=${kind}&id=${encodeURIComponent(id)}`;
  return `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#64748b">Share with client</div>
    <div class="doc-share" style="display:flex;gap:8px">
      <a href="https://wa.me/?text=${encodeURIComponent('Your travel document '+esc(p.name)+' #'+esc(no)+' '+url)}" target="_blank" class="doc-share-btn" style="background:#25d366;color:#fff"><span style="font-size:13px">💬</span> WhatsApp</a>
      <a href="mailto:?subject=${encodeURIComponent(esc(p.name)+' '+esc(meta[0]))}&body=${encodeURIComponent('Dear client, please find your '+esc(meta[0])+' below: '+url)}" class="doc-share-btn" style="background:${p.primary_color||'#0b2a4a'};color:#fff"><span style="font-size:13px">✉️</span> Email</a>
      <button class="doc-share-btn secondary" onclick="copyShare('${esc(url)}')">Copy link</button>
    </div>
  </div>`;
}
function docContent(kind,data,p){
  const b = data.booking||{};
  const cust = data.customer||{};
  switch(kind){
    case 'booking-confirmation': return `
      <div style="display:flex;gap:16px">
        <div style="flex:1">
          <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin-bottom:8px">Passenger</h4>
          ${kv('Name',b.passenger_name||cust.name||'-')}${kv('Document',b.doc_no||'-')}${kv('Type',b.passenger_type||'ADT')}
        </div>
        <div style="flex:1">
          <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin-bottom:8px">Booking reference</h4>
          ${kv('PNR',b.pnr||'-')}${kv('Ticket',b.ticket_number||'Not issued')}${kv('Status',b.booking_status||'confirmed')}
        </div>
      </div>
      <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin:16px 0 8px">Journey segments</h4>
      ${docTable(['Flight','Route','Date','Depart','Arrive','Status','Seat'],[[b.flight_no||'-',b.origin+' → '+b.destination,b.flight_date||'-',b.dep_time||'-',b.arr_time||'-',b.ticket_status||'unissued',b.seat_no||'-']])}
      ${data.tax_lines.length?`<h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin:16px 0 8px">Taxes &amp; fees</h4>${docTable(['Code','Description','Amount'],data.tax_lines.map(t=>[t.tax_code,t.description,moneyDoc(t.amount,t.currency)]))}`:''}
      <div style="margin-top:16px;padding:14px;background:#f9fbfd;border:1px solid #dfe7f1;border-radius:10px"><b>Trace ID:</b> ${esc(b.trace_id||'-')}&nbsp;&nbsp;<b>Channel:</b> ${esc(b.channel_name||'-')}</div>
    `;
    case 'itinerary': return `
      <div style="display:flex;gap:16px">
        <div style="flex:1">${kv('PNR',b.pnr||'-')}${kv('Customer',cust.name||'-')}${kv('Travel date',b.flight_date||'-')}</div>
        <div style="flex:1">${kv('From',b.origin||'-')}${kv('To',b.destination||'-')}${kv('Cabin',b.fare_class||'-')}</div>
      </div>
      <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin:16px 0 8px">Segment detail</h4>
      ${docTable(['#','Flight','Departure','Arrival','Aircraft','Status'],[[1,b.flight_no||'-',b.origin+' '+b.dep_time,b.destination+' '+b.arr_time,b.aircraft||'-',b.status||'scheduled']])}
      <div style="margin-top:16px;padding:14px;background:#f9fbfd;border:1px solid #dfe7f1;border-radius:10px"><b>Check-in:</b> ${data.checkins.length?data.checkins[0].status:'Not checked in'} &nbsp;|&nbsp; <b>Seat:</b> ${b.seat_no||'Not assigned'}</div>
    `;
    case 'invoice': return `<div style="display:flex;gap:16px">
      <div style="flex:1">${kv('Invoice no',data.invoice.invoice_no||'-')}${kv('Customer',data.invoice.customer_name||'-')}${kv('Issue date',data.invoice.issue_date||'-')}</div>
      <div style="flex:1">${kv('Due date',data.invoice.due_date||'-')}${kv('Status',data.invoice.status||'issued')}${kv('Currency',data.invoice.currency||'USD')}</div>
    </div>
    <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin:16px 0 8px">Invoice lines</h4>
    ${docTable(['Description','Qty','Unit price','Tax','Line total'],[['Travel services','1',moneyDoc(data.invoice.subtotal),moneyDoc(data.invoice.tax),moneyDoc(data.invoice.total)]])}
    <div style="display:flex;justify-content:flex-end;margin-top:14px">
      <div style="text-align:right"><div style="font-size:10px;color:#64748b">Grand total</div><div style="font-size:18px;font-weight:800">${moneyDoc(data.invoice.total,data.invoice.currency)}</div></div>
    </div>`;
    case 'quotation': return `<div style="display:flex;gap:16px">
      <div style="flex:1">${kv('Estimate no',data.estimate.estimate_no||'-')}${kv('Customer',data.estimate.customer_name||'-')}${kv('Title',data.estimate.title||'-')}</div>
      <div style="flex:1">${kv('Valid until',data.estimate.valid_until||'-')}${kv('Status',data.estimate.status||'draft')}${kv('Currency',data.estimate.currency||'USD')}</div>
    </div>
    <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin:16px 0 8px">Quote lines</h4>
    ${docTable(['Description','Subtotal','Tax','Total'],[[data.estimate.description||'Travel services',moneyDoc(data.estimate.subtotal),moneyDoc(data.estimate.tax),moneyDoc(data.estimate.total)]])}
    <div style="margin-top:16px;padding:14px;background:#f9fbfd;border:1px solid #dfe7f1;border-radius:10px">This quotation is valid until ${esc(data.estimate.valid_until||'')}. Acceptance converts to a booking.</div>`;
    case 'statement': return `
      <h4 style="font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:${p.primary_color||'#0b2a4a'};margin-bottom:8px">Account statement</h4>
      ${docTable(['Date','Reference','Type','Amount','Balance'],(data.invoices||[]).slice(0,20).map(i=>[i.issue_date,i.invoice_no,'Invoice',moneyDoc(i.total,i.currency),'-']))}
      <div style="margin-top:14px;padding:14px;background:#f9fbfd;border:1px solid #dfe7f1;border-radius:10px"><b>Open balance:</b> ${moneyDoc(0)}</div>`;
    case 'journey-document': return `
      <div style="display:flex;gap:16px;margin-bottom:14px">
        <div style="flex:1">${kv('Travel wallet','Journey document')}${kv('PNR',b.pnr||'-')}${kv('Passenger',b.passenger_name||cust.name||'-')}</div>
        <div style="flex:1">${kv('From',b.origin||'-')}${kv('To',b.destination||'-')}${kv('Date',b.flight_date||'-')}</div>
      </div>
      ${docTable(['Item','Detail','Status'],[['Flight',b.flight_no||'-',b.status||'scheduled'],['Seat',b.seat_no||'-',b.ticket_status||'unissued'],['Check-in',data.checkins.length?data.checkins[0].status:'pending','-']]]}
      <div style="margin-top:16px;padding:14px;background:#f9fbfd;border:1px solid #dfe7f1;border-radius:10px"><b>Trace ID:</b> ${esc(b.trace_id||'-')}&nbsp;&nbsp;<b>Keep this document for airport check-in.</b></div>`;
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
  const el = document.createElement('div');
  el.id = 'doc-view-' + kind;
  document.body.appendChild(el);
  renderDocument(kind, id, el);
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


async function render_dashboard(el){
  const meta = viewMeta.dashboard || ['Dashboard','Live command center for your travel business','Overview'];
  let d = {};
  try{ d = await api('/api/dashboard'); }catch(e){
    el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), '')}<div class="panel"><div class="panel-body error">${esc(e.message||String(e))}</div></div>`;
    return;
  }
  const kpis = d && d.kpis ? d.kpis : {};
  const kpiHtml = Object.entries(kpis).map(([k,v])=>kpi(k,v,'chart',typeof v==='number'?fmt(v):String(v))).join('');
  const recent = Array.isArray(d.recent_orders)?d.recent_orders:[];
  const deps = Array.isArray(d.departures_soon)?d.departures_soon:[];
  const unpaid = Array.isArray(d.unpaid)?d.unpaid:[];
  const tktl = Array.isArray(d.tktl_due)?d.tktl_due:[];
  const bySource = Array.isArray(d.by_source)?d.by_source:[];
  // --- disruptions for enterprise charter/low-cost carrier visibility ---
  let disruptionsHtml = '';
  try{
    const disc = await api('/api/portal/disruptions?tenant='+encodeURIComponent(tenant||''));
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
  const rows = recent.length?recent:[];
  const idKey = rows.length?Object.keys(rows[0]).find(c=>c==='id')||Object.keys(rows[0])[0]||'id':'id';
  const headCols = rows.length?Object.keys(rows[0]).filter(c=>c!==idKey&&c!=='created_at'&&c!=='updated_at'&&c!=='tenant_id'):[];
  const head = `<tr>${headCols.map(c=>`<th>${fieldLabel(c)}</th>`).join('')}</tr>`;
  const body = rows.map(r=>`<tr>${headCols.map(c=>`<td>${cellFor(c,r[c])}</td>`).join('')}</tr>`).join('');
  const listHtml = rows.length?`<table><thead>${head}</thead><tbody>${body}</tbody></table>`:`<div class="empty">No recent orders.</div>`;
  const depHtml = deps.length?`<table><thead><tr><th>PNR</th><th>Route</th><th>Date</th><th>Status</th></tr></thead><tbody>${deps.map(x=>`<tr><td>${esc(x.pnr)}</td><td>${esc(x.origin)} → ${esc(x.destination)}</td><td>${esc(x.travel_start)}</td><td>${statusBadge(x.ticket_status)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No departures soon.</div>`;
  const unpaidHtml = unpaid.length?`<table><thead><tr><th>Invoice</th><th>Customer</th><th>Balance</th></tr></thead><tbody>${unpaid.map(x=>`<tr><td>${esc(x.invoice_no||x.id)}</td><td>${esc(x.customer||'-')}</td><td>${moneyCell(x.balance)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No unpaid invoices.</div>`;
  const tktlHtml = tktl.length?`<table><thead><tr><th>PNR</th><th>TKTL</th><th>Status</th></tr></thead><tbody>${tktl.map(x=>`<tr><td>${esc(x.pnr)}</td><td>${esc(x.tktl)}</td><td>${statusBadge(x.ticket_status)}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">No tickets due.</div>`;
  el.innerHTML = `${pageHead(esc(meta[0]), esc(meta[1]||''), `<button class="mini-btn primary" data-action="create-dashboard">+ New Order</button>`)}
    <div class="grid kpi-row" style="margin-bottom:20px">${kpiHtml}</div>
    ${disruptionsHtml}
    <div class="grid two" style="gap:16px">
      <div class="panel"><div class="panel-head"><h3>Recent Orders</h3></div><div class="panel-body">${listHtml}</div></div>
      <div class="panel"><div class="panel-head"><h3>Departures Soon</h3></div><div class="panel-body">${depHtml}</div></div>
    </div>
    <div class="grid two" style="gap:16px">
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

async function renderView(name){
  const el = $(`#view-${name}`);
  if(!el) return;
  try {
    if(name === 'pss-stock') return renderPssStock(el);
    if(typeof window['render_'+name.replace(/-/g,'_')] === 'function') return window['render_'+name.replace(/-/g,'_')](el);
    // generic fallback: load list from API so every screen shows data instead of a missing-module error
    await renderList(name, el);
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

function showLogin(){
  const main = $('#main-view'); if(main) main.classList.add('hidden');
  const login = $('#login-view'); if(login) login.classList.remove('hidden');
}

async function showWorkspace(){
  const login = $('#login-view'); if(login) login.classList.add('hidden');
  const main = $('#main-view'); if(main) main.classList.remove('hidden');
  await loadSession();
  applyRoleNav();
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
    try{ await api('/api/logout', {method:'POST', body:'{}'}); }catch(e){}
    location.reload();
  };

  const changePw = $('#change-pw-btn');
  if(changePw) changePw.onclick = ()=>{
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
  $$('#nav a[data-view]').forEach(a=>{
    a.onclick = (e)=>{ e.preventDefault(); setView(a.dataset.view); };
  });
  const menuToggle = $('#menu-toggle'); if(menuToggle) menuToggle.onclick = ()=> $('#sidebar')?.classList.toggle('open');
  const contentMenuToggle = $('#content-menu-toggle'); if(contentMenuToggle) contentMenuToggle.onclick = ()=> $('#sidebar')?.classList.toggle('open');
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
