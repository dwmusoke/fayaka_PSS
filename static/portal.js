const parts = location.pathname.split('/').filter(Boolean);
const tenantFromPath = parts[0] === 'portal' && parts[1] ? parts[1] : '';
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const js = v => esc(JSON.stringify(String(v ?? '')));
const defaultCurrency = ()=> portalBranding?.currency || searchData?.airline?.currency || 'USD';
const money = (n,c) => { const cur = c || defaultCurrency(); return (n===null||n===undefined)?'-':`${cur} ${Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`; };
function getCsrf(){ const m=document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/); return m?m[1]:''; }
function toast(msg, type='ok'){
  let el = document.getElementById('portal-toast');
  if(!el){
    el = document.createElement('div');
    el.id = 'portal-toast';
    el.style.position = 'fixed';
    el.style.top = '14px';
    el.style.right = '14px';
    el.style.zIndex = '9999';
    el.style.maxWidth = '360px';
    document.body.appendChild(el);
  }
  el.className = 'global-alert';
  el.textContent = msg;
  if(type!=='error') setTimeout(()=>el.classList.add('hidden'), 3500);
}
let tripType = 'oneway';
let searchData = null;
let selected = [];
let selectedSeats = {};
let pax = {adults:1, children:0, infants:0};
let firstPassengerName = '';
let portalBranding = null;
const typeIcons = { cancel:'✗', delay:'⏱', gate:'🚪', aircraft:'✈', weather:'☁', staff:'👤', technical:'🔧', security:'🔒' };

async function api(path, opts={}) {
  const r = await fetch((window.API_BASE||'')+path, {headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrf()}, credentials:'same-origin', ...opts});
  const text = await r.text();
  let d = null;
  try{ d = JSON.parse(text); }
  catch(e){ throw new Error('Server returned an invalid response for '+path+' (HTTP '+r.status+')'); }
  if(!r.ok) throw new Error(d.error || ('Request failed (HTTP '+r.status+')'));
  return d;
}
function tenant(){ return document.getElementById('f-tenant')?.value || document.getElementById('m-tenant')?.value || tenantFromPath; }
function docLast(){ return (document.getElementById('m-last')?.value || '').trim() || firstPassengerName.trim().split(/\s+/).pop() || ''; }
function docTenant(){ return document.getElementById('m-tenant')?.value || tenantFromPath || tenant(); }
function docLabel(s){ return String(s||'').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()); }
function pdfUrl(kind, id, pnr, last, tcode){
  const qs = new URLSearchParams({tenant:tcode||docTenant(), pnr:pnr||'', last_name:last||docLast()});
  return `/api/documents/pdf/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?${qs.toString()}`;
}
function openPdf(kind, id, pnr, last, tcode, label){
  const url = pdfUrl(kind, id, pnr, last, tcode);
  openDocViewer(url, label, url + (url.includes('?') ? '&' : '?') + 'download=1');
}
function openDocViewer(url, label, downloadUrl){
  let m = document.getElementById('pdf-viewer-modal');
  if(!m){
    m = document.createElement('div');
    m.id = 'pdf-viewer-modal';
    m.className = 'modal hidden pdf-modal';
    m.innerHTML = `<div class="modal-card">
      <div class="modal-head"><div><h3 id="pdf-viewer-title">Document preview</h3><div class="muted small" id="pdf-viewer-sub"></div></div>
        <button class="portal-btn secondary" onclick="closePdfViewer()">Close</button></div>
      <iframe id="pdf-viewer-frame" class="pdf-frame" title="Document preview"></iframe>
      <div class="pdf-actions"><button class="portal-btn" id="pdf-print-btn">Print</button>
        <button class="portal-btn secondary" id="pdf-share-btn">Share</button>
        <button class="portal-btn secondary" id="pdf-download-btn">Download</button></div>
    </div>`;
    document.body.appendChild(m);
    m.addEventListener('click', e=>{ if(e.target===m) closePdfViewer(); });
  }
  const frame = m.querySelector('#pdf-viewer-frame');
  m.querySelector('#pdf-viewer-title').textContent = label || 'Document preview';
  m.querySelector('#pdf-viewer-sub').textContent = url.split('?')[0];
  frame.src = url;
  m.classList.remove('hidden');
  m.querySelector('#pdf-print-btn').onclick = ()=>{
    try{ frame.contentWindow.focus(); frame.contentWindow.print(); }
    catch(e){ toast('Use your browser print dialog on the document', 'error'); }
  };
  m.querySelector('#pdf-share-btn').onclick = async ()=>{
    const full = location.origin + url;
    try{ if(navigator.share){ await navigator.share({title:label||'Document', url:full}); return; } }catch(e){}
    try{ await navigator.clipboard.writeText(full); toast('Document link copied'); }
    catch(e){ window.prompt('Copy document link', full); }
  };
  m.querySelector('#pdf-download-btn').onclick = ()=>{
    const a = document.createElement('a');
    a.href = downloadUrl || url;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
}
function openStoredDoc(id, qs, label, mime){
  const url = '/api/portal/documents/' + encodeURIComponent(id) + '/download' + (qs ? '?' + qs : '');
  openDocViewer(url + (qs ? '&' : '?') + 'inline=1', label, url);
}
function closePdfViewer(){
  const m = document.getElementById('pdf-viewer-modal');
  if(!m) return;
  m.classList.add('hidden');
  const f = m.querySelector('iframe');
  if(f) f.src = 'about:blank';
}
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closePdfViewer(); });

function showTab(name){
  ['book','manage','account'].forEach(n=>{
    const tab=document.getElementById('tab-'+n); if(tab) tab.classList.toggle('active', n===name);
    const pane=document.getElementById('pane-'+n); if(pane) pane.classList.toggle('hidden', n!==name);
  });
  if(name==='account') loadMyTrips();
  if(name==='manage') refreshTicketStatus();
}

function setTrip(t){
  tripType = t;
  document.querySelectorAll('.trip-tab').forEach(b=>b.classList.toggle('active', b.dataset.trip===t));
  document.getElementById('search-box').classList.toggle('hidden', t==='charter');
  document.getElementById('charter-box').classList.toggle('hidden', t!=='charter');
  document.getElementById('return-field').style.display = t==='return' ? '' : 'none';
  const multi = document.getElementById('multi-segments');
  if(t==='multicity' && !multi.children.length) addSegment();
  multi.classList.toggle('hidden', t!=='multicity');
  document.getElementById('simple-search').classList.toggle('hidden', t==='multicity');
}

function changePax(kind, delta){
  pax[kind] = Math.max(kind==='adults'?1:0, Math.min(9, pax[kind]+delta));
  document.getElementById('n-'+kind).textContent = pax[kind];
}

function addSegment(){
  const box = document.getElementById('multi-segments');
  const i = box.children.length;
  const row = document.createElement('div');
  row.className = 'portal-grid seg-row';
  row.style.marginTop = '8px';
  row.innerHTML = `<div class="portal-field"><label>From</label><input class="seg-origin" placeholder="KLA" maxlength="3"></div>
    <div class="portal-field"><label>To</label><input class="seg-dest" placeholder="DXB" maxlength="3"></div>
    <div class="portal-field"><label>Date</label><input class="seg-date" type="date"></div>
    <div class="portal-field" style="display:flex;align-items:end"><button class="portal-btn secondary" onclick="removeSegment(this)">Remove</button></div>`;
  box.appendChild(row);
}
function removeSegment(btn){ btn.closest('.seg-row').remove(); }

async function init(){
  const d = await api('/api/portal/tenants');
  const tenants = d.tenants||[];
  const selectedTenant = tenants.some(t=>t.code===tenantFromPath) ? tenantFromPath : (tenants[0]?.code || tenantFromPath);
  const opts = tenants.map((t,i)=>`<option value="${esc(t.code)}" ${t.code===selectedTenant?'selected':''}>${esc(t.name)} (${esc(t.code)})</option>`).join('');
  ['f-tenant','m-tenant','c-tenant'].forEach(id=>{ document.getElementById(id).innerHTML = opts; });
  await loadPortalBranding();
  const modal = document.getElementById('flight-detail-modal');
  if(modal) modal.addEventListener('click', e=>{ if(e.target === modal) closeFlightDetails(); });
  document.addEventListener('keydown', e=>{ if(e.key === 'Escape') closeFlightDetails(); });
  await loadDisruptions();
  try{ const me = await api('/api/portal/me'); if(me && me.customer) portalCustomer = me.customer; }catch(e){}
}

async function loadPortalBranding(){
  try{
    const d = await api('/api/portal/branding?tenant='+encodeURIComponent(tenant()));
    portalBranding = d.airline || {};
    const name = portalBranding.name || 'Fayaka Airline';
    const tagline = portalBranding.tagline || 'Fly with confidence';
    document.title = name + ' - Online Booking';
    const title = document.getElementById('airline-name'); if(title) title.textContent = name;
    const brandStrong = document.querySelector('.brand-text strong'); if(brandStrong) brandStrong.textContent = name;
    const brandSpan = document.querySelector('.brand-text span'); if(brandSpan) brandSpan.textContent = tagline;
    const setLogo = (selector, fallback)=>{
      const host = document.querySelector(selector);
      if(!host) return;
      host.textContent = '';
      host.classList.toggle('has-logo', !!portalBranding.logo_data);
      if(portalBranding.logo_data){
        const img = document.createElement('img');
        img.src = portalBranding.logo_data;
        img.alt = name + ' logo';
        host.appendChild(img);
      } else {
        host.textContent = fallback || (portalBranding.logo_text || name.slice(0,2)).toUpperCase().slice(0,3);
      }
    };
    setLogo('.logo-mark', (portalBranding.logo_text || name.slice(0,2)).toUpperCase().slice(0,3));
    setLogo('.hero-logo', (portalBranding.logo_text || name.slice(0,2)).toUpperCase().slice(0,3));
  }catch(e){}
}

async function loadDisruptions(){
  const box = document.getElementById('disruptions');
  try{
    const d = await api('/api/portal/disruptions?tenant='+encodeURIComponent(tenant()));
    const items = d.disruptions||[];
    if(!items.length){ box.classList.add('hidden'); return; }
    const severityColors = { high:'#dc2626', medium:'#f59e0b', low:'#10b981', info:'#3b82f6' };
    box.innerHTML = `<div class="portal-card" style="border-left:5px solid #dc2626; background:#fffbff"><b style="color:#dc2626">Service disruptions</b>` +
      items.map(x=>`<div class="trace-item" style="border-left:3px solid ${severityColors[x.severity]||'#6b7280'}"><span style="color:${severityColors[x.severity]||'#6b7280'}">${esc(typeIcons[x.disruption_type]||'•')} ${esc(x.disruption_type)}</span> ${esc(x.severity||'')} ${esc(x.description||'')}<br><small>${esc(x.affected_pnr||'')} ${esc(x.traveler_name||'')}</small></div>`).join('') + '</div>';
    box.classList.remove('hidden');
  }catch(e){ box.classList.add('hidden'); }
}

async function refreshUnusedTickets(){
  const box = document.getElementById('unused-tickets-list');
  box.textContent = 'Loading unused tickets...';
  try{
    const d = await api('/api/portal/unused-tickets?tenant='+encodeURIComponent(tenant()));
    const items = d.unused_tickets||[];
    if(!items.length){
      box.innerHTML = '<div class="muted">No unused tickets found.</div>';
      return;
    }
    const statusColors = { unused:'#6b7280', used:'#059669', voided:'#dc2626', refunded:'#f59e0b' };
    box.innerHTML = `<div style="max-height:400px;overflow-y:auto;">
      ${items.map(x=>`<div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><span style="color:${statusColors[x.ticket_status]||'#6b7280'}">${esc(x.ticket_no||'—')}</span><span style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px">${esc(x.ticket_status||'unused')}</span></div><div style="font-size:11px;color:#374151"><b>Traveler:</b> ${esc(x.traveler_name||'-')} <b>Airline:</b> ${esc(x.airline||'-')} <b>Issue:</b> ${esc(x.issue_date||'-')} <b>Travel date:</b> ${esc(x.travel_date||'-')} <b>Expiry:</b> ${esc(x.expiry_date||'—')}</div><div style="font-size:10px;color:#9ca3a9;margin-top:2px">Value: ${money(x.value, x.currency)} · ${x.refund_status||'pending'}</div></div>`).join('')}
    </div>`;
  }catch(e){ box.innerHTML = '<div class="muted error">Error loading unused tickets.</div>'; }
}

async function refreshTicketStatus(){
  const box = document.getElementById('ticket-status-list');
  box.textContent = 'Loading ticket statuses...';
  try{
    const d = await api('/api/portal/ticket-status?tenant='+encodeURIComponent(tenant()));
    const items = d.ticket_status||[];
    if(!items.length){
      box.innerHTML = '<div class="muted">No ticket statuses found.</div>';
      return;
    }
    const statuses = { issued:'✓ issued', voided:'✗ voided', refunded:' refunded', partially_refunded:' part-refunded', no_show:' no-show', unissued:' unissued', ticketed:' ticketed' };
    box.innerHTML = `<div style="max-height:400px;overflow-y:auto;">
      ${items.map(x=>`<div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><span style="color:#059669">${esc(typeIcons[x.ticket_status]||'•')} ${esc(x.ticket_no||'—')}</span><span style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px">${esc(statuses[x.ticket_status]||x.ticket_status)}</span></div><div style="font-size:11px;color:#374151"><b>Passenger:</b> ${esc(x.passenger_name||'-')} <b>PNR:</b> ${esc(x.pnr||'-')} <b>Flight:</b> ${esc(x.flight_no||'-')} <b>Status:</b> ${esc(x.ticket_status||'-')} <b>Issue:</b> ${esc(x.issue_date||'-')}</div></div>`).join('')}
    </div>`;
  }catch(e){ box.innerHTML = '<div class="muted error">Error loading ticket statuses.</div>'; }
}

const tag = (text, kind='') => text ? `<span class="avail-badge ${kind}">${esc(text)}</span>` : '';
const airportLabel = (f, code) => {
  const key = code === f.origin ? 'origin' : (code === f.destination ? 'destination' : code);
  const a = f.airports?.[key];
  return a ? `${code} · ${a.city || a.name || ''}` : code;
};

async function searchFlights(){
  const msg = document.getElementById('search-msg');
  const res = document.getElementById('results');
  msg.innerHTML = '<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:14px;border:2px solid #0e7490;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite;display:inline-block"></span> Searching flights...</span>';
  res.classList.remove('hidden');
  res.innerHTML = `<div style="display:grid;gap:12px;margin-top:12px">${Array(3).fill(0).map(()=>`<div style="height:140px;border-radius:18px;background:linear-gradient(90deg,#f1f5f9 25%,#e2e8f0 50%,#f1f5f9 75%);background-size:200% 100%;animation:shimmer 1.5s infinite"></div>`).join('')}</div>`;
  res.classList.remove('hidden');
  const q = new URLSearchParams({tenant:tenant(), trip_type:tripType, cabin:document.getElementById('f-cabin').value,
    adults:pax.adults, children:pax.children, infants:pax.infants});
  if(tripType==='multicity'){
    const segs = Array.from(document.querySelectorAll('.seg-row')).map(r=>({
      origin:r.querySelector('.seg-origin').value.trim().toUpperCase(),
      destination:r.querySelector('.seg-dest').value.trim().toUpperCase(),
      date:r.querySelector('.seg-date').value}));
    q.set('segments', JSON.stringify(segs));
  } else {
    q.set('origin', document.getElementById('f-origin').value.trim().toUpperCase());
    q.set('destination', document.getElementById('f-destination').value.trim().toUpperCase());
    q.set('date', document.getElementById('f-date').value);
    if(tripType==='return') q.set('return_date', document.getElementById('f-return').value);
  }
  try{
    searchData = await api('/api/portal/search?'+q.toString());
    selected = (searchData.segments||[]).map(()=>null);
    const segs = searchData.segments||[];
    msg.textContent = `${segs.reduce((a,b)=>a+b.length,0)} flight(s) found`;
    renderResults();
  }catch(e){ msg.textContent = e.message; res.classList.add('hidden'); res.innerHTML=''; }
}
function renderResults(){
  if(!searchData) return;
  const res = document.getElementById('results');
  const segs = searchData.segments||[];
  const caps = searchData.capabilities||{};
  const airline = portalBranding?.name || searchData.airline?.name || 'Fayaka Airline';
  const logoHtml = portalBranding?.logo_data
    ? `<img src="${esc(portalBranding.logo_data)}" alt="" />`
    : esc((portalBranding?.code || searchData.airline?.code || 'FA').slice(0,2).toUpperCase());
  const totalPax = searchData.passengers?.total || 1;
  const cabin = searchData.cabin || 'Y';
  const tripType = searchData.trip_type || 'oneway';
  res.innerHTML = `<div class="avail-summary">
      <div>
        <b>${esc(airline)}</b>
        <small>${esc(cabin)} cabin · ${esc(tripType)} · ${esc(totalPax)} passenger(s) · ${segs.reduce((a,b)=>a+b.length,0)} flight(s) found</small>
      </div>
      <div class="avail-badges">
        ${tag('Online booking', 'good')}${tag(caps.ndc ? 'NDC offers' : '', 'info')}${tag(caps.seat_map ? 'Seat maps' : '', 'info')}${tag('Live availability', 'good')}
      </div>
    </div>` + segs.map((list,i)=>`<div class="seg-title">${segs.length===2?(i===0?'✈ Outbound':'✈ Return'):'✈ Segment '+(i+1)} · ${list.length} option${list.length===1?'':'s'}</div>` +
      (list.length ? list.map((f,j)=>{
        const seats = f.seats_available ?? f.available_seats;
        const fare = parseFloat(f.fare||0)||0;
        const tax = Math.round(fare*0.10*100)/100;
        const total = Math.round((fare+tax)*100)/100;
        const isSel = selected[i]===j;
        const seatColor = Number(seats) <= 3 ? 'warn' : (Number(seats) <= 9 ? 'info' : 'good');
        const ff = f.fare_family||{};
        return `<div class="avail-card ${isSel?'selected':''}" style="animation:fadeSlideIn .3s ease ${j*0.05}s both">
          <div class="avail-card-top">
            <div class="avail-airline">
              <div class="avail-logo ${portalBranding?.logo_data?'has-logo':''}">${logoHtml}</div>
              <div>
                <b>${esc(f.flight_no)}</b>
                <small>${esc(ff.name||'Economy Saver')} · ${esc(ff.code||'Y')} class</small>
              </div>
            </div>
            <div class="avail-badges">
              ${tag(ff.fare_basis||'', 'info')}
              ${tag(f.is_best_fare ? '★ Best fare' : '', 'good')}
              ${tag(f.refundable ? 'Refundable' : 'Non-refundable', f.refundable ? 'good' : 'warn')}
              ${tag(f.ndc?.enabled ? 'NDC' : '', 'info')}
              ${tag(ff.priority_boarding ? 'Priority' : '', 'info')}
              ${Number(seats) <= 9 ? `<span class="avail-badge ${seatColor}">${Number(seats) <= 3 ? '🔥' : ''} ${seats} left</span>` : ''}
            </div>
          </div>
          <div class="avail-card-main">
            <div class="avail-route">
              <div class="avail-point"><b>${esc(f.dep_time||'')}</b><span>${esc(f.origin)}</span><small>${esc(airportLabel(f,f.origin))}</small></div>
              <div class="avail-line"><small>${esc(f.duration||'')}</small><i></i><small>${esc(f.stops||'Nonstop')}</small></div>
              <div class="avail-point right"><b>${esc(f.arr_time||'')}</b><span>${esc(f.destination)}</span><small>${esc(airportLabel(f,f.destination))}</small></div>
            </div>
            <div class="avail-meta">
              <div><small>Aircraft</small><b>${esc(f.equipment?.aircraft||f.aircraft||'-')}</b></div>
              <div><small>Layout</small><b>${esc(f.equipment?.layout||'-')}</b></div>
              <div><small>Baggage</small><b>${esc(ff.checked_baggage||'-')}</b></div>
              <div><small>Terminal / Gate</small><b>${esc([f.terminal,f.gate].filter(Boolean).join(' / ')||'-')}</b></div>
            </div>
            <div class="avail-price">
              <div class="price-row"><small>Base fare</small><b>${money(fare, f.currency)}</b></div>
              <div class="price-row"><small>Taxes &amp; fees (10%)</small><b>${money(tax, f.currency)}</b></div>
              <div class="price-row total"><small>Total per adult</small><b>${money(total, f.currency)}</b></div>
              <small style="text-align:center">${esc(f.flight_date||'')}</small>
              <button class="portal-btn ${isSel?'dark':''}" onclick="selectFlight(${i},${j})">${isSel?'✓ Selected':'Select flight'}</button>
              <button class="portal-btn ghost" onclick="showFlightDetails(${i},${j})">Details</button>
            </div>
          </div>
          <div class="avail-card-foot">
            ${tag(f.status||'scheduled','good')}${tag(`${seats ?? 0} seats left`, seatColor)}${tag(ff.change_policy||'')}${tag('Terms apply')}
          </div>
        </div>`;
      }).join('') : '<div class="muted" style="padding:20px;text-align:center">No flights found for this segment.</div>')).join('');
  res.classList.remove('hidden');
  // scroll to results
  res.scrollIntoView({behavior:'smooth', block:'start'});
}

function selectFlight(seg, idx){
  selected[seg] = idx;
  renderResults();
  renderPassengerForm();
}

function showFlightDetails(seg, idx){
  const f = searchData?.segments?.[seg]?.[idx];
  if(!f) return;
  const title = document.getElementById('flight-detail-title');
  const body = document.getElementById('flight-detail-body');
  const modal = document.getElementById('flight-detail-modal');
  if(!title || !body || !modal) return;
  title.textContent = `${f.flight_no} · ${f.origin} → ${f.destination} · ${f.flight_date}`;
  const terms = (f.terms||[]).map(t=>`<li>${esc(t)}</li>`).join('');
  const anc = (f.ancillaries||[]).map(a=>`<div class="detail-row"><span>${esc(a.name)} <small class="muted">${esc(a.category||'')}</small></span><b>${money(a.price,a.currency)}</b></div>`).join('') || '<div class="muted">No optional services configured.</div>';
  const rows = f.equipment?.seat_rows||[];
  const aisleAfter = f.equipment?.layout === '2-3-2' ? [1,4] : [2];
  const picked = selectedSeats[String(f.id)] || '';
  const seatMap = rows.map(r=>`<div class="seat-row"><span class="seat-row-no">${esc(r.row)}</span>${r.seats.map((s,k)=>`<span class="seat ${esc(s.status)} ${aisleAfter.includes(k)?'aisle':''} ${picked===esc(s.label)?'selected':''}" title="${esc(s.label)}" onclick="${esc(s.status)==='available'?'selectSeat('+f.id+',\''+esc(s.label)+'\')':''}">${esc(s.label)}</span>`).join('')}</div>`).join('');
  const airportBlock = key => {
    const a = f.airports?.[key] || {};
    const w = f.weather?.[key] || {};
    const docs = f.documents?.[key] || [];
    return `<div class="detail-section"><h4>${esc(key==='origin'?'Departure':'Arrival')} · ${esc(f[key])}</h4>
      <div class="detail-row"><span>Airport</span><b>${esc(a.name||'Not published')}</b></div>
      <div class="detail-row"><span>City / country</span><b>${esc([a.city,a.country].filter(Boolean).join(', ')||'-')}</b></div>
      <div class="detail-row"><span>Terminal / gate</span><b>${esc([a.terminal||f.terminal,f.gate].filter(Boolean).join(' / ')||'-')}</b></div>
      <div class="detail-row"><span>Weather</span><b>${esc([w.condition,(w.temperature_c!==null && w.temperature_c!==undefined)?`${w.temperature_c}°C`:'',w.wind_kph?`${w.wind_kph} kph`:''].filter(Boolean).join(' · ')||'Not available')}</b></div>
      ${docs.length?`<div class="muted" style="margin-top:6px">Travel documents: ${Array.from(new Set(docs.map(d=>esc(d.title||d.doc_type)))).join(' · ')}</div>`:''}</div>`;
  };
  body.innerHTML = `<div class="detail-grid">
    <div class="detail-section wide"><h4>Fare conditions &amp; policies</h4>
      <div class="detail-kv">
        <div><small>Fare brand</small><b>${esc(f.fare_family?.name||'Economy Saver')}</b></div>
        <div><small>Refundable</small><b>${f.refundable?'Yes':'No'}</b></div>
        <div><small>Changes</small><b>${esc(f.fare_family?.change_policy||'-')}</b></div>
        <div><small>Carry-on</small><b>${esc(f.fare_family?.carry_on||'-')}</b></div>
        <div><small>Checked baggage</small><b>${esc(f.fare_family?.checked_baggage||'-')}</b></div>
        <div><small>Seat selection</small><b>${esc(f.fare_family?.seat_selection||'-')}</b></div>
        ${f.fare_family?.fare_basis ? `<div><small>Fare basis</small><b>${esc(f.fare_family.fare_basis)}</b></div>` : ''}
        ${f.is_best_fare ? `<div style="margin-top:4px"><small>Best fare</small><b style="color:#059669">✓ Best available price</b></div>` : ''}
      </div><ul class="detail-terms">${terms}</ul></div>
    <div class="detail-section"><h4>Equipment</h4>
      <div class="detail-row"><span>Aircraft</span><b>${esc(f.equipment?.aircraft||f.aircraft||'-')}</b></div>
      <div class="detail-row"><span>Layout</span><b>${esc(f.equipment?.layout||'-')}</b></div>
      <div class="detail-row"><span>Seat pitch</span><b>${esc(f.equipment?.seat_pitch||'-')}</b></div>
      <div class="detail-row"><span>Wi-Fi</span><b>${esc(f.equipment?.wifi||'-')}</b></div>
      <div class="detail-row"><span>Duration</span><b>${esc(f.duration||'-')}</b></div></div>
    <div class="detail-section"><h4>NDC &amp; ancillaries</h4>
      <div class="detail-row"><span>NDC offer</span><b>${f.ndc?.enabled?esc(f.ndc.offer||'Enabled'):'Not enabled'}</b></div>
      <div class="detail-row"><span>Seat map</span><b>${f.equipment?.seat_rows?.length?'Available':'Not published'}</b></div>
      <div style="margin-top:8px">${anc}</div></div>
    ${airportBlock('origin')}${airportBlock('destination')}
    <div class="detail-section wide"><h4>Seat map</h4>
      <div class="seat-legend">${tag('Available','good')}${tag('Occupied','warn')}${picked?`<span style="margin-left:auto"><b style="color:#0e7490">Your seat:</b> <b>${esc(picked)}</b></span>`:''}</div>
      <div class="seat-map">${seatMap||'<div class="muted">Seat map not published for this flight.</div>'}</div></div>
  </div>`;
  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeFlightDetails(){
  const modal = document.getElementById('flight-detail-modal');
  if(modal) modal.classList.add('hidden');
  document.body.style.overflow = '';
}
function selectSeat(flightId, label){
  selectedSeats[String(flightId)] = label;
  const modal = document.getElementById('flight-detail-modal');
  if(!modal.classList.contains('hidden')) showFlightDetailsFromCache(flightId);
  toast('Seat '+esc(label)+' selected','ok');
}
function showFlightDetailsFromCache(flightId){
  for(let si=0; si<(searchData?.segments||[]).length; si++){
    const list = searchData.segments[si];
    for(let fi=0; fi<list.length; fi++){
      if(String(list[fi].id)===String(flightId)){ showFlightDetails(si, fi); return; }
    }
  }
}

function renderPassengerForm(){
  if(selected.some(x=>x===null)) { document.getElementById('booking-form').classList.add('hidden'); return; }
  const totalPax = pax.adults + pax.children + pax.infants;
  const types = [...Array(pax.adults).fill('ADT'), ...Array(pax.children).fill('CHD'), ...Array(pax.infants).fill('INF')];
  const anc = (searchData.ancillaries || []).filter(x => x.status === 'active');
  const chosenFlightIds = new Set(selected.map((idx,i)=>Number(searchData.segments[i]?.[idx]?.id)));
  const ancOptions = anc.filter(x => x.id !== null && x.id !== undefined && x.id !== '' && Number.isFinite(Number(x.id)))
    .filter(x => !x.flight_id || chosenFlightIds.has(Number(x.flight_id)));
  const taxPax = pax.adults + pax.children;
  const segFares = selected.map((idx,i)=>{
    const f = searchData.segments[i][idx];
    const fare = parseFloat(f.fare||0)||0;
    return {f, fare, tax: Math.round(fare*0.10*100)/100};
  });
  const baseSubtotal = segFares.reduce((a,{fare})=>a + fare*totalPax, 0);
  const taxTotal = segFares.reduce((a,{tax})=>a + tax*taxPax, 0);
  const ancTotal = 0; // ancillaries are optional; selected ones added on submit
  const grandTotal = baseSubtotal + taxTotal + ancTotal;
  const box = document.getElementById('booking-form');
  box.innerHTML = `<h3>Passenger details</h3><div class="muted" style="margin-bottom:10px">${totalPax} passenger(s) · ${selected.length} segment(s)</div>
    <div id="pax-rows">${types.map((t,i)=>`<div class="pax-row">
      <div class="portal-field"><label>Passenger ${i+1} full name</label><input class="pax-name" placeholder="As on travel document" /></div>
      <div class="portal-field"><label>Type</label><select class="pax-type"><option ${t==='ADT'?'selected':''}>ADT</option><option ${t==='CHD'?'selected':''}>CHD</option><option ${t==='INF'?'selected':''}>INF</option></select></div>
      <div class="portal-field"><label>Document no</label><input class="pax-doc" /></div></div>`).join('')}</div>
<div class="portal-grid"><div class="portal-field"><label>Contact email</label><input id="bk-email" type="email" /></div>
      <div class="portal-field"><label>Contact phone</label><input id="bk-phone" /></div></div>
    <div class="portal-card" style="margin-top:14px">
      <h3 style="margin-top:0">Price summary</h3>
      <div class="portal-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:8px">
        ${segFares.map(({f,fare})=>`<div class="portal-field" style="border:1px solid #eef1f5;border-radius:10px;padding:10px;background:#f9fbfd"><div style="font-size:10px;color:#64748b;text-transform:uppercase">${esc(f.flight_no)} · ${esc(f.origin)}→${esc(f.destination)}</div><div style="font-size:12px;color:#64748b">${esc(f.flight_date||'')}</div><div><b>${money(fare*totalPax, f.currency)}</b> <small style="color:#64748b">${totalPax} × ${money(fare, f.currency)}</small></div></div>`).join('')}
        <div class="portal-field" style="border:1px solid #eef1f5;border-radius:10px;padding:10px;background:#f9fbfd"><div style="font-size:10px;color:#64748b;text-transform:uppercase">Base fare subtotal</div><div><b>${money(baseSubtotal)}</b></div></div>
        <div class="portal-field" style="border:1px solid #eef1f5;border-radius:10px;padding:10px;background:#f9fbfd"><div style="font-size:10px;color:#64748b;text-transform:uppercase">Taxes & fees (10%)</div><div><b>${money(taxTotal)}</b></div></div>
        <div class="portal-field" style="border:1px solid #eef1f5;border-radius:10px;padding:10px;background:#f9fbfd"><div style="font-size:10px;color:#64748b;text-transform:uppercase">Ancillary services (optional)</div><div><b style="color:#0e7490">+${money(ancTotal)}</b> <small style="color:#64748b">select below</small></div></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:8px;padding-top:8px;border-top:1px dashed #cfd8e3">
        <span><b>Estimated total</b></span><b style="color:#0b2a4a;font-size:15px">${money(grandTotal)}</b>
      </div>
    </div>
    <h3 style="margin-top:16px">Add ancillary services</h3><div id="anc-list" class="muted">Loading...</div>
    <div style="margin-top:14px"><button class="portal-btn" onclick="submitBooking()">Confirm booking</button>
      <button class="portal-btn secondary" onclick="document.getElementById('booking-form').classList.add('hidden')">Cancel</button></div>
    <div id="b-msg" class="error hidden" style="margin-top:10px"></div>`;
  const list = document.getElementById('anc-list');
  list.className = 'anc-grid';
  list.innerHTML = ancOptions.map(x=>`<label class="anc-item">
    <input type="checkbox" class="anc-check" value="${esc(x.id)}" /><span><b>${esc(x.name)}</b><br><small class="muted">${money(x.price,x.currency)} · ${esc(x.category)}</small></span></label>`).join('') || '<div class="muted">No optional services configured.</div>';
  box.classList.remove('hidden');
  const bkEmail = document.getElementById('bk-email');
  const bkPhone = document.getElementById('bk-phone');
  if(portalCustomer){
    if(bkEmail && !bkEmail.value) bkEmail.value = portalCustomer.email || '';
    if(bkPhone && !bkPhone.value) bkPhone.value = portalCustomer.phone || '';
  }
  box.scrollIntoView({behavior:'smooth'});
}

async function submitBooking(){
  const msg = document.getElementById('b-msg');
  const flightIds = selected.map((idx,i)=>searchData.segments[i][idx].id);
  const passengers = Array.from(document.querySelectorAll('.pax-row')).map(r=>({
    full_name:r.querySelector('.pax-name').value.trim(), type:r.querySelector('.pax-type').value,
    doc_no:r.querySelector('.pax-doc').value.trim()}));
  if(passengers.some(p=>!p.full_name)){ msg.textContent='Enter a name for every passenger'; msg.classList.remove('hidden'); return; }
  if(passengers.length) firstPassengerName = passengers[0].full_name;
  const ancillaries = Array.from(document.querySelectorAll('.anc-check:checked')).map(x=>({ancillary_id:Number(x.value), quantity:1}));
  const seatSelections = Object.entries(selectedSeats).map(([fid,label])=>({flight_id:Number(fid), seat:label}));
  try{
    const cur = searchData.segments[0]?.[selected[0]]?.currency || 'USD';
    const r = await api('/api/portal/book',{method:'POST',body:JSON.stringify({
      tenant:tenant(), trip_type:tripType, cabin:document.getElementById('f-cabin').value,
      flights:flightIds, passengers, contact:{email:document.getElementById('bk-email').value.trim(), phone:document.getElementById('bk-phone').value.trim()}, ancillaries, seat_selections: seatSelections, currency: cur
    })
  });
    document.getElementById('booking-form').classList.add('hidden');
    const totalPax = pax.adults + pax.children + pax.infants;
    const taxPax = pax.adults + pax.children;
    const segFares = selected.map((idx,i)=>{
      const f = searchData.segments[i][idx];
      const fare = parseFloat(f.fare||0)||0;
      return {f, fare, tax: Math.round(fare*0.10*100)/100};
    });
    const baseSubtotal = segFares.reduce((a,{fare})=>a + fare*totalPax, 0);
    const taxTotal = segFares.reduce((a,{tax})=>a + tax*taxPax, 0);
    const ancTotal = ancillaries.reduce((a,x)=>{
      const anc = (searchData.ancillaries||[]).find(a=>a.id===x.ancillary_id);
      return a + (anc?parseFloat(anc.price||0)*x.quantity:0);
    }, 0);
    const grandTotal = Number(r.total) || Math.round((baseSubtotal + taxTotal + ancTotal)*100)/100;
    const segList = selected.map((idx,i)=>{
      const f = searchData.segments[i][idx];
      return `<tr><td>${esc(f.flight_no)}</td><td>${esc(f.origin)} → ${esc(f.destination)}</td><td>${esc(f.flight_date||'')}</td><td>${esc(f.dep_time||'')}-${esc(f.arr_time||'')}</td><td>${money(f.fare||0, f.currency)}</td><td>${esc(selectedSeats[String(f.id)]||'—')}</td></tr>`;
    }).join('');
    document.getElementById('confirmation').innerHTML = `<div class="success"><b>Booking confirmed.</b> Your PNR is <b>${esc(r.pnr)}</b>.
        <div class="muted" style="margin-top:6px;color:#065f46">Booking ref <b>${esc(r.pnr)}</b> · ${esc(r.passengers)} passenger(s) · ${esc(r.segments)} segment(s)${r.invoice_no?` · Invoice <b>${esc(r.invoice_no)}</b>`:''}</div></div>
      <div class="portal-grid">
        <div class="portal-field"><label>PNR / booking reference</label><input value="${esc(r.pnr)}" readonly /></div>
        <div class="portal-field"><label>Trace reference</label><input value="${esc(r.trace_id)}" readonly /></div>
        <div class="portal-field"><label>Itinerary ID</label><input value="${esc(r.itinerary_id)}" readonly /></div>
        <div class="portal-field"><label>Invoice number</label><input value="${esc(r.invoice_no||'Issuing…')}" readonly /></div>
        <div class="portal-field"><label>Total due</label><input value="${money(r.total, cur)}" readonly /></div>
      </div>
      ${r.sync_error?`<div class="error" style="margin-bottom:12px">Your booking is saved, but invoicing is still catching up. Refresh Manage booking in a moment to see the invoice.</div>`:''}
      <div class="portal-card" style="margin-top:12px">
        <h3 style="margin-top:0">Itemized receipt</h3>
        <table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="text-align:left">Segment</th><th style="text-align:left">Route</th><th style="text-align:left">Date</th><th style="text-align:left">Times</th><th style="text-align:left">Fare</th><th style="text-align:left">Seat</th></tr></thead><tbody>${segList}</tbody></table>
        <div style="display:flex;justify-content:space-between;margin-top:8px;padding-top:8px;border-top:1px dashed #cfd8e3"><span>Base fare subtotal</span><b>${money(baseSubtotal, cur)}</b></div>
        <div style="display:flex;justify-content:space-between"><span>Taxes &amp; fees (10%)</span><b>${money(taxTotal, cur)}</b></div>
        <div style="display:flex;justify-content:space-between"><span>Ancillary services</span><b>${money(ancTotal, cur)}</b></div>
        <div style="display:flex;justify-content:space-between;margin-top:6px"><span><b>Total due</b></span><b style="color:#0b2a4a">${money(grandTotal, cur)}</b></div>
      </div>
      <div class="portal-card" style="margin-top:12px"><h3 style="margin-top:0">Your documents</h3>
        <div class="doc-actions">
          <button class="portal-btn secondary" onclick="openPdf(${js('booking-confirmation')},${js((r.booking_ids||[])[0])},${js(r.pnr)},${js(docLast())},${js(tenant())},${js('Booking confirmation')})">Booking confirmation PDF</button>
          <button class="portal-btn secondary" onclick="openPdf(${js('itinerary')},${js((r.booking_ids||[])[0])},${js(r.pnr)},${js(docLast())},${js(tenant())},${js('Itinerary')})">Itinerary PDF</button>
          ${r.invoice_id?`<button class="portal-btn secondary" onclick="openPdf(${js('invoice')},${js(r.invoice_id)},${js(r.pnr)},${js(docLast())},${js(tenant())},${js('Invoice')})">Invoice PDF</button>`:''}
        </div></div>
      <div style="margin-top:14px"><button class="portal-btn" onclick="prefillManage('${esc(r.pnr)}','${esc(tenant())}')">Manage / pay now</button>
        <button class="portal-btn secondary" onclick="location.href='/portal/${tenant()}'">New booking</button></div>`;
    document.getElementById('confirmation').classList.remove('hidden');
    document.getElementById('confirmation').scrollIntoView({behavior:'smooth'});
    refreshTicketStatus();
  }catch(e){ msg.textContent=e.message; msg.classList.remove('hidden'); }
}

async function submitCharter(){
  const msg = document.getElementById('charter-msg');
  try{
    const r = await api('/api/portal/charter',{method:'POST',body:JSON.stringify({
      tenant:document.getElementById('c-tenant').value, client_name:document.getElementById('c-client').value.trim(),
      email:document.getElementById('c-email').value.trim(), phone:document.getElementById('c-phone').value.trim(),
      origin:document.getElementById('c-origin').value.trim().toUpperCase(), destination:document.getElementById('c-dest').value.trim().toUpperCase(),
      flight_date:document.getElementById('c-date').value, pax_count:document.getElementById('c-pax').value,
      aircraft_type:document.getElementById('c-aircraft').value.trim(), requirements:document.getElementById('c-notes').value.trim()})});
    msg.innerHTML = `<div class="success">Charter request <b>${esc(r.request_no)}</b> received. Our charter team will quote shortly. Trace: ${esc(r.trace_id)}</div>`;
  }catch(e){ msg.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

function prefillManage(pnr, tenantCode, passengerName){
  showTab('manage');
  document.getElementById('m-pnr').value=pnr;
  if(tenantCode) document.getElementById('m-tenant').value=tenantCode;
  const source = passengerName || firstPassengerName || '';
  document.getElementById('m-last').value = source.trim().split(/\s+/).pop() || '';
  loadBooking();
  document.getElementById('m-last').focus();
}

async function loadBooking(){
  const out = document.getElementById('booking-detail');
  const q = new URLSearchParams({tenant:document.getElementById('m-tenant').value, pnr:document.getElementById('m-pnr').value.trim().toUpperCase(),
    last_name:document.getElementById('m-last').value.trim()});
  try{
    const d = await api('/api/portal/booking?'+q.toString());
    const pnr = document.getElementById('m-pnr').value.trim().toUpperCase();
    const last = document.getElementById('m-last').value.trim();
    const docQs = new URLSearchParams({tenant:docTenant(), pnr:pnr, last_name:last}).toString();
    const rows = d.bookings||[];
    const segs = rows.map(b=>`<tr><td>${esc(b.segment_index+1)}</td><td>${esc(b.flight_no||'')}</td><td>${esc(b.origin||'')} → ${esc(b.destination||'')}</td>
      <td>${esc(b.flight_date||'')}</td><td>${esc(b.passenger_name)}</td><td>${esc(b.ticket_number||'Not issued')}</td>
      <td>${esc(b.payment_status)}</td><td>${b.ticket_number||String(b.ticket_status||'').toLowerCase()==='ticketed'
        ? `<button class="pdf-link" onclick="openPdf(${js('eticket')},${js(b.id)},${js(pnr)},${js(last)},${js(docTenant())},${js('E-ticket')})">E-ticket</button>`
        : '<span class="muted">Not issued</span>'}</td></tr>`).join('');
    const pays = (d.payments||[]).map(p=>`<div class="trace-item"><b>${esc(p.payment_no)}</b> · ${money(p.amount,p.currency)} · ${esc(p.status)}
        <small>${esc(p.method)} · ${esc(p.paid_at||'')}</small>
        <div style="margin-top:6px">${p.erp_payment_id
          ? `<button class="pdf-link muted" onclick="openPdf(${js('receipt')},${js(p.erp_payment_id)},${js(pnr)},${js(last)},${js(docTenant())},${js('Payment receipt')})">Receipt PDF</button>`
          : '<span class="muted">Receipt issuing…</span>'}</div></div>`).join('')||'<div class="muted">No payments recorded</div>';
    const cis = (d.checkins||[]).map(c=>`<div class="trace-item"><b>${esc(c.checkin_no)}</b> · seat ${esc(c.seat_no)} · ${esc(c.status)}</div>`).join('')||'<div class="muted">Not checked in yet</div>';
    const anc = (d.ancillaries||[]).map(a=>`<div class="trace-item"><b>${esc(a.description)}</b> · ${money(a.amount,a.currency)}</div>`).join('')||'<div class="muted">No extras</div>';
    const tr = (d.trace||[]).map(t=>`<div class="trace-item"><b>${esc(t.action)}</b> · ${esc(t.source)}<small>${esc(t.actor)} · ${esc(t.created_at)} · ${esc(t.detail||'')}</small></div>`).join('')||'<div class="muted">No trace events</div>';
    const total = Math.round((rows.reduce((a,b)=>a+Number(b.total_amount||0),0) +
      (d.ancillaries||[]).reduce((a,x)=>a+Number(x.amount||0),0))*100)/100;
    const invTotal = (d.invoices||[]).reduce((a,v)=>a+Number(v.total||0),0);
    const invPaid = (d.invoices||[]).reduce((a,v)=>a+Number(v.amount_paid||0),0);
    const invoices = (d.invoices||[]).map(v=>`<div class="trace-item"><b>${esc(v.invoice_no)}</b> · ${money(v.total,v.currency)} · ${esc(v.status)}
        <small>Issued ${esc(v.issue_date||'')} · Paid ${money(v.amount_paid,v.currency)} · Balance ${money(v.balance,v.currency)}</small>
        <div class="doc-actions" style="margin-top:6px"><button class="pdf-link muted" onclick="openPdf(${js('invoice')},${js(v.id)},${js(pnr)},${js(last)},${js(docTenant())},${js('Invoice')})">Invoice PDF</button></div></div>`).join('')
      || '<div class="muted">No invoice issued yet.</div>';
    out.innerHTML = `<div class="portal-card"><div class="success"><b>PNR ${esc(rows[0].pnr)}</b> · ${esc(d.airline.name)} · ${esc(rows[0].trip_type||'')}</div>
      <table><thead><tr><th>Seg</th><th>Flight</th><th>Route</th><th>Date</th><th>Passenger</th><th>Ticket</th><th>Payment</th><th>E-ticket</th></tr></thead><tbody>${segs}</tbody></table>
      <div class="doc-actions" style="margin-top:10px">
        <button class="pdf-link muted" onclick="openPdf(${js('booking-confirmation')},${js(rows[0].id)},${js(pnr)},${js(last)},${js(docTenant())},${js('Booking confirmation')})">Booking confirmation PDF</button>
        <button class="pdf-link muted" onclick="openPdf(${js('itinerary')},${js(rows[0].id)},${js(pnr)},${js(last)},${js(docTenant())},${js('Itinerary')})">Itinerary PDF</button>
      </div></div>
      <div class="portal-card"><h3>Invoice</h3><div class="muted">Trip total ${money(invTotal)} · Paid ${money(invPaid)} · Balance ${money(Math.max(0, invTotal-invPaid))}</div>${invoices}</div>
      <div class="portal-card"><h3>Payment</h3>${pays}
        <div class="portal-grid" style="margin-top:12px">
          <div class="portal-field"><label>Amount (booking total ${money(total)})</label><input id="p-amount" type="number" step="0.01" value="${Math.max(0, total-invPaid).toFixed(2)}" /></div>
          <div class="portal-field"><label>Method</label><select id="p-method"><option value="flutterwave">Pay online (Flutterwave / MoMo / Card)</option><option value="mobilemoney">Mobile money (recorded)</option><option value="card">Card (recorded)</option><option value="bank">Bank (recorded)</option><option value="cash">Cash (recorded)</option></select></div>
          <div class="portal-field"><label>Reference</label><input id="p-ref" /></div>
        </div><div style="margin-top:12px"><button class="portal-btn" onclick="pay('${esc(pnr)}')">Pay now</button></div>
        <div id="p-msg" class="error hidden" style="margin-top:10px"></div></div>
      <div class="portal-card"><h3>Traveller documents</h3><div id="trip-docs" class="muted">Loading…</div>
        <div class="portal-grid" style="margin-top:10px">
          <div class="portal-field"><label>Document type</label><select id="mt-type"><option>passport</option><option>visa</option><option>id</option><option>insurance</option><option>other</option></select></div>
          <div class="portal-field"><label>Document number</label><input id="mt-no" /></div>
          <div class="portal-field"><label>File</label><input id="mt-file" type="file" /></div>
        </div><div style="margin-top:10px"><button class="portal-btn secondary" onclick="uploadTravelDoc()">Upload document</button></div>
        <div id="mt-msg" class="muted" style="margin-top:8px"></div></div>
      <div class="portal-card"><h3>Ancillary services</h3>${anc}</div>
       <div class="portal-card"><h3>Check-in</h3>${cis}</div>
       <div class="portal-card"><h3>Traceability</h3>${tr}</div>`;
    api('/api/portal/documents?'+docQs).then(r=>{
      const el = document.getElementById('trip-docs'); if(!el) return;
      const docs = r.documents||[];
      el.className = docs.length ? '' : 'muted';
      el.innerHTML = docs.length ? docs.map(x=>`<div class="trace-item"><b>${esc(docLabel(x.doc_type))} ${esc(x.doc_no||'')}</b>
        <small>${esc(x.file_name||'')} · ${esc(x.created_at||'')}</small>
        <div style="margin-top:6px">${x.has_file?`<button class="pdf-link muted" onclick="openStoredDoc(${js(x.id)},${js(docQs)},${js(docLabel(x.doc_type)+' '+(x.doc_no||''))},${js(x.mime_type)})">View document</button>`:'<span class="muted">No file attached</span>'}</div></div>`).join('')
        : 'No traveller documents uploaded for this booking.';
    }).catch(()=>{ const el=document.getElementById('trip-docs'); if(el) el.textContent='Traveller documents unavailable.'; });
    refreshTicketStatus();
    out.classList.remove('hidden');
  }catch(e){ out.innerHTML=`<div class="portal-card error">${esc(e.message)}</div>`; out.classList.remove('hidden'); }
}

async function pay(pnr){
  const msg = document.getElementById('p-msg');
  const method=document.getElementById('p-method').value;
  try{
    if(method==='flutterwave'){
      const r=await api('/api/pss/payments/flutterwave/init',{method:'POST',body:JSON.stringify({tenant:document.getElementById('m-tenant').value,pnr,
        amount:document.getElementById('p-amount').value,email:document.getElementById('bk-email')?.value||'',phone:document.getElementById('bk-phone')?.value||'',last_name:document.getElementById('m-last')?.value.trim()||''})});
      if(r.url){ location.href=r.url.startsWith('http')?r.url:((window.API_BASE||'')+r.url); return; }
      msg.textContent='Payment gateway unavailable'; msg.classList.remove('hidden'); return;
    }
    await api('/api/portal/pay',{method:'POST',body:JSON.stringify({tenant:document.getElementById('m-tenant').value, pnr,
      last_name:document.getElementById('m-last').value.trim(), amount:document.getElementById('p-amount').value,
      method, reference:document.getElementById('p-ref').value})});
    loadBooking();
    refreshTicketStatus();
  }catch(e){ msg.textContent=e.message; msg.classList.remove('hidden'); }
}


let portalCustomer = null;
function setAuth(mode){
  document.getElementById('auth-tab-login').classList.toggle('active', mode==='login');
  document.getElementById('auth-tab-register').classList.toggle('active', mode==='register');
  document.getElementById('login-box').classList.toggle('hidden', mode!=='login');
  document.getElementById('register-box').classList.toggle('hidden', mode!=='register');
}
async function portalRegister(){
  const msg=document.getElementById('auth-msg');
  try{
    await api('/api/portal/register',{method:'POST',body:JSON.stringify({tenant:tenant(),full_name:document.getElementById('r-name').value.trim(),
      email:document.getElementById('r-email').value.trim(),phone:document.getElementById('r-phone').value.trim(),
      doc_no:document.getElementById('r-doc').value.trim(),password:document.getElementById('r-password').value})});
    msg.textContent='Account created successfully. Please sign in with your email and password.'; msg.className='success'; setAuth('login');
  }catch(e){ msg.textContent=e.message; }
}
async function portalLogin(){
  const msg=document.getElementById('auth-msg');
  try{
    const r=await api('/api/portal/login',{method:'POST',body:JSON.stringify({tenant:tenant(),email:document.getElementById('l-email').value.trim(),password:document.getElementById('l-password').value})});
    if(!r.customer) throw new Error('Login failed: no customer data returned');
    portalCustomer=r.customer; msg.textContent='Signed in as '+esc(r.customer.full_name);
    await loadMyTrips();
  }catch(e){ msg.textContent=e.message; }
}
async function portalLogout(){
  try{ await api('/api/portal/logout',{method:'POST',body:'{}'}); }catch(e){}
  portalCustomer=null; document.getElementById('auth-box').classList.remove('hidden');
  document.getElementById('my-trips').classList.add('hidden');
}
async function loadMyTrips(){
  const box=document.getElementById('my-trips');
  try{
    const d=await api('/api/portal/me');
    portalCustomer=d.customer;
    document.getElementById('auth-box').classList.add('hidden');
    box.classList.remove('hidden');
    const rows=(d.bookings||[]).map(b=>`<tr><td><b>${esc(b.pnr)}</b></td><td>${esc(b.flight_no||'')}</td><td>${esc(b.origin||'')} → ${esc(b.destination||'')}</td>
      <td>${esc(b.flight_date||'')}</td><td>${esc(b.passenger_name)}</td><td>${esc(b.ticket_number||'Not issued')}</td>
      <td>${esc(b.payment_status)}</td><td>${b.ticket_number||String(b.ticket_status||'').toLowerCase()==='ticketed'
        ? `<button class="pdf-link" onclick="openPdf(${js('eticket')},${js(b.id)},${js(b.pnr)},${js(b.passenger_name.trim().split(/\s+/).pop())},${js(b.tenant_code)},${js('E-ticket')})">E-ticket</button>`
        : '<span class="muted">Not issued</span>'}</td>
      <td><button class="portal-btn secondary" onclick="prefillManage('${esc(b.pnr)}','${esc(b.tenant_code||tenant())}')">Manage</button></td></tr>`).join('');
    box.innerHTML=`<div class="portal-card"><div style="display:flex;justify-content:space-between"><h3>My trips · ${esc(d.customer.full_name)}</h3><button class="portal-btn secondary" onclick="portalLogout()">Sign out</button></div>
      <table><thead><tr><th>PNR</th><th>Flight</th><th>Route</th><th>Date</th><th>Passenger</th><th>Ticket</th><th>Payment</th><th>E-ticket</th><th>Actions</th></tr></thead><tbody>${rows||'<tr><td colspan="9" class="muted">No trips yet - book a flight to see it here.</td></tr>'}</tbody></table></div>
      <div class="portal-card"><h3>My travel documents</h3><div id="my-docs" class="muted">Loading...</div>
        <div class="portal-grid" style="margin-top:10px"><div class="portal-field"><label>Document type</label><select id="td-type"><option>passport</option><option>visa</option><option>id</option><option>insurance</option><option>other</option></select></div>
        <div class="portal-field"><label>Document number</label><input id="td-no" /></div>
        <div class="portal-field"><label>PNR</label><input id="td-pnr" /></div>
        <div class="portal-field"><label>File</label><input id="td-file" type="file" /></div></div>
        <div style="margin-top:10px"><button class="portal-btn" onclick="uploadAccountDoc()">Upload document</button></div>
        <div id="td-msg" class="muted" style="margin-top:8px"></div></div>`;
    api('/api/portal/documents?tenant='+encodeURIComponent(tenant())).then(r=>{ const el=document.getElementById('my-docs'); if(!el)return;
      const docs=r.documents||[];
      el.className=docs.length?'':'muted';
      el.innerHTML=docs.map(x=>`<div class="trace-item"><b>${esc(docLabel(x.doc_type))} ${esc(x.doc_no||'')}</b>
        <small>${esc(x.file_name||'')} · ${esc(x.created_at||'')}</small>
        <div style="margin-top:6px">${x.has_file?`<button class="pdf-link muted" onclick="openStoredDoc(${js(x.id)},'',${js(docLabel(x.doc_type)+' '+(x.doc_no||''))},${js(x.mime_type)})">View document</button>`:'<span class="muted">No file attached</span>'}</div></div>`).join('')
        || 'No traveller documents uploaded';
    }).catch(()=>{});
  }catch(e){ console.warn('loadMyTrips:', e.message); }
}
async function uploadDoc(prefix, pnr, reload){
  const msg=document.getElementById(prefix+'-msg');
  const fileEl=document.getElementById(prefix+'-file');
  const file=fileEl?fileEl.files[0]:null;
  if(!msg||!fileEl) return;
  if(!pnr){ msg.textContent='Find a booking first'; return; }
  if(!file){ msg.textContent='Choose a file first'; return; }
  const dataUrl=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(file);});
  try{
    await api('/api/portal/documents',{method:'POST',body:JSON.stringify({tenant:docTenant(), pnr:pnr,
      last_name:docLast(), doc_type:document.getElementById(prefix+'-type').value,
      doc_no:document.getElementById(prefix+'-no').value.trim(),
      file_name:file.name, mime_type:file.type, file_base64:dataUrl})});
    msg.textContent='Document uploaded and linked to your booking';
    if(reload==='account') loadMyTrips(); else loadBooking();
  }catch(e){ msg.textContent=e.message; }
}
async function uploadTravelDoc(){
  await uploadDoc('mt', (document.getElementById('m-pnr')?.value||'').trim().toUpperCase(), 'booking');
}
async function uploadAccountDoc(){
  await uploadDoc('td', ((document.getElementById('td-pnr')?.value)||(document.getElementById('m-pnr')?.value)||'').trim().toUpperCase(), 'account');
}

async function refreshSuppliers(){
  const box = document.getElementById('suppliers-list');
  box.textContent = 'Loading suppliers...';
  try{
    const d = await api('/api/portal/suppliers?tenant='+encodeURIComponent(tenant()));
    const items = d.suppliers||[];
    if(!items.length){
      box.innerHTML = '<div class="muted">No suppliers found.</div>';
      return;
    }
    box.innerHTML = `<div style="max-height:400px;overflow-y:auto;">
      ${items.map(x=>`<div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><span style="color:#374151">${esc(x.supplier_name||'—')}</span><span style="font-size:10px;color:#6b728b;text-transform:uppercase;letter-spacing:.5px">${esc(x.category||'General')}</span></div><div style="font-size:11px;color:#374151"><b>Contact:</b> ${esc(x.contact_person||'-')} <b>Email:</b> ${esc(x.email||'-')} <b>Phone:</b> ${esc(x.phone||'-')}</div><div style="font-size:10px;color:#9ca3a9;margin-top:2px">Balance: ${money(x.balance, x.currency)} · Status: ${x.status||'Active'}</div></div>`).join('')}
    </div>`;
  }catch(e){ box.innerHTML = '<div class="muted error">Error loading suppliers.</div>'; }
}

init().catch(e=>{
  const t = document.getElementById('airline-name');
  if(t) t.textContent = 'Portal unavailable';
  if(window.__portalFatal) window.__portalFatal('Portal failed to initialise: ' + ((e && e.message) || e));
});
