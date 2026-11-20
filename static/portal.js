const parts = location.pathname.split('/').filter(Boolean);
const tenantFromPath = parts[0] === 'portal' && parts[1] ? parts[1] : '';
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const money = (n,c='USD') => (n===null||n===undefined)?'-':`${c} ${Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`;
function getCsrf(){ const m=document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/); return m?m[1]:''; }
let tripType = 'oneway';
let searchData = null;
let selected = [];
let pax = {adults:1, children:0, infants:0};

async function api(path, opts={}) {
  const r = await fetch((window.API_BASE||'')+path, {headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrf()}, ...opts});
  const d = await r.json();
  if(!r.ok) throw new Error(d.error || 'Request failed');
  return d;
}
function tenant(){ return document.getElementById('f-tenant')?.value || document.getElementById('m-tenant')?.value || tenantFromPath; }

function showTab(name){
  ['book','manage','account'].forEach(n=>{
    const tab=document.getElementById('tab-'+n); if(tab) tab.classList.toggle('active', n===name);
    const pane=document.getElementById('pane-'+n); if(pane) pane.classList.toggle('hidden', n!==name);
  });
  if(name==='account') loadMyTrips();
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
  const opts = (d.tenants||[]).map(t=>`<option value="${esc(t.code)}" ${t.code===tenantFromPath?'selected':''}>${esc(t.name)} (${esc(t.code)})</option>`).join('');
  ['f-tenant','m-tenant','c-tenant'].forEach(id=>{ document.getElementById(id).innerHTML = opts; });
  await loadDisruptions();
}

async function loadDisruptions(){
  const box = document.getElementById('disruptions');
  try{
    const d = await api('/api/portal/disruptions?tenant='+encodeURIComponent(tenant()));
    const items = d.disruptions||[];
    if(!items.length){ box.classList.add('hidden'); return; }
    box.innerHTML = `<div class="portal-card" style="border-left:5px solid #f59e0b"><b style="color:#b45309">Service disruptions</b>` +
      items.map(x=>`<div class="trace-item"><b>${esc(x.disruption_type)} - ${esc(x.severity)}</b><small>${esc(x.affected_pnr||'')} ${esc(x.traveler_name||'')} ${esc(x.description||'')}</small></div>`).join('') + '</div>';
    box.classList.remove('hidden');
  }catch(e){ box.classList.add('hidden'); }
}

async function searchFlights(){
  const msg = document.getElementById('search-msg');
  const res = document.getElementById('results');
  msg.textContent = 'Searching...'; res.classList.add('hidden');
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
    res.innerHTML = segs.map((list,i)=>`<div class="seg-title">Segment ${i+1}${segs.length===2?(i===0?' - Outbound':' - Return'):''} · ${list.length} flight(s)</div>` +
      (list.length ? `<div class="flight-row head"><div>Flight</div><div>Route</div><div>Date / Time</div><div>Seats</div><div>Fare</div><div>Select</div></div>` +
        list.map((f,j)=>`<div class="flight-row"><div><b>${esc(f.flight_no)}</b><small>${esc(f.aircraft||'')}</small></div>
          <div><b>${esc(f.origin)} → ${esc(f.destination)}</b><small>${esc(f.gate||'')} ${esc(f.terminal||'')}</small></div>
          <div>${esc(f.flight_date)}<small>${esc(f.dep_time||'')} / ${esc(f.arr_time||'')}</small></div>
          <div>${esc(f.available_seats)}<small><span class="pill">${esc(f.status)}</span></small></div>
          <div><b>${money(f.fare, f.currency)}</b></div>
          <div><button class="portal-btn ${selected[i]===j?'dark':''}" onclick="selectFlight(${i},${j})">${selected[i]===j?'Selected':'Select'}</button></div></div>`).join('')
        : '<div class="muted">No flights for this segment.</div>')).join('');
    res.classList.remove('hidden');
  }catch(e){ msg.textContent = e.message; }
}

function selectFlight(seg, idx){
  selected[seg] = idx;
  const res = document.getElementById('results');
  const blocks = res.querySelectorAll('.seg-title');
  const start = blocks[seg] ? Array.from(res.children).indexOf(blocks[seg]) : -1;
  const end = seg+1 < blocks.length ? Array.from(res.children).indexOf(blocks[seg+1]) : res.children.length;
  for(let i=start+1;i<end;i++){
    const btn = res.children[i].querySelector?.('button'); if(!btn) continue;
    const isSel = Array.from(res.children[i].querySelectorAll('button')).indexOf(btn) === idx;
    btn.className = 'portal-btn ' + (isSel?'dark':''); btn.textContent = isSel?'Selected':'Select';
  }
  renderPassengerForm();
}

function renderPassengerForm(){
  if(selected.some(x=>x===null)) { document.getElementById('booking-form').classList.add('hidden'); return; }
  const totalPax = pax.adults + pax.children + pax.infants;
  const types = [...Array(pax.adults).fill('ADT'), ...Array(pax.children).fill('CHD'), ...Array(pax.infants).fill('INF')];
  const anc = searchData.ancillaries || [];
  const box = document.getElementById('booking-form');
  box.innerHTML = `<h3>Passenger details</h3><div class="muted" style="margin-bottom:10px">${totalPax} passenger(s) · ${selected.length} segment(s)</div>
    <div id="pax-rows">${types.map((t,i)=>`<div class="pax-row">
      <div class="portal-field"><label>Passenger ${i+1} full name</label><input class="pax-name" placeholder="As on travel document" /></div>
      <div class="portal-field"><label>Type</label><select class="pax-type"><option ${t==='ADT'?'selected':''}>ADT</option><option ${t==='CHD'?'selected':''}>CHD</option><option ${t==='INF'?'selected':''}>INF</option></select></div>
      <div class="portal-field"><label>Document no</label><input class="pax-doc" /></div></div>`).join('')}</div>
    <div class="portal-grid"><div class="portal-field"><label>Contact email</label><input id="bk-email" type="email" /></div>
      <div class="portal-field"><label>Contact phone</label><input id="bk-phone" /></div></div>
    <h3 style="margin-top:16px">Add ancillary services</h3><div id="anc-list" class="muted">Loading...</div>
    <div style="margin-top:14px"><button class="portal-btn" onclick="submitBooking()">Confirm booking</button>
      <button class="portal-btn secondary" onclick="document.getElementById('booking-form').classList.add('hidden')">Cancel</button></div>
    <div id="b-msg" class="error hidden" style="margin-top:10px"></div>`;
  box.classList.remove('hidden');
  api('/api/setup/pss-ancillaries').then(d=>{
    const list = document.getElementById('anc-list'); if(!list) return;
    list.className = 'anc-grid';
    list.innerHTML = (d.ancillaries||[]).filter(x=>x.status==='active').map(x=>`<label class="anc-item">
      <input type="checkbox" class="anc-check" value="${x.id}" /><span><b>${esc(x.name)}</b><br><small class="muted">${money(x.price,x.currency)} · ${esc(x.category)}</small></span></label>`).join('');
  }).catch(()=>{ const l=document.getElementById('anc-list'); if(l) l.textContent='Ancillaries unavailable'; });
  box.scrollIntoView({behavior:'smooth'});
}

async function submitBooking(){
  const msg = document.getElementById('b-msg');
  const flightIds = selected.map((idx,i)=>searchData.segments[i][idx].id);
  const passengers = Array.from(document.querySelectorAll('.pax-row')).map(r=>({
    full_name:r.querySelector('.pax-name').value.trim(), type:r.querySelector('.pax-type').value,
    doc_no:r.querySelector('.pax-doc').value.trim()}));
  if(passengers.some(p=>!p.full_name)){ msg.textContent='Enter a name for every passenger'; msg.classList.remove('hidden'); return; }
  const ancillaries = Array.from(document.querySelectorAll('.anc-check:checked')).map(x=>({ancillary_id:Number(x.value), quantity:1}));
  try{
    const r = await api('/api/portal/book',{method:'POST',body:JSON.stringify({
      tenant:tenant(), trip_type:tripType, cabin:document.getElementById('f-cabin').value,
      flights:flightIds, passengers, contact:{email:document.getElementById('bk-email').value.trim(), phone:document.getElementById('bk-phone').value.trim()}, ancillaries})});
    document.getElementById('booking-form').classList.add('hidden');
    document.getElementById('confirmation').innerHTML = `<div class="success"><b>Booking confirmed.</b> Your PNR is <b>${esc(r.pnr)}</b>.</div>
      <div class="portal-grid">
        <div class="portal-field"><label>PNR</label><input value="${esc(r.pnr)}" readonly /></div>
        <div class="portal-field"><label>Trace reference</label><input value="${esc(r.trace_id)}" readonly /></div>
        <div class="portal-field"><label>Total</label><input value="${money(r.total)}" readonly /></div>
        <div class="portal-field"><label>Segments / passengers</label><input value="${r.segments} / ${r.passengers}" readonly /></div>
      </div><div style="margin-top:14px"><button class="portal-btn" onclick="prefillManage('${esc(r.pnr)}')">Manage / pay now</button></div>`;
    document.getElementById('confirmation').classList.remove('hidden');
    document.getElementById('confirmation').scrollIntoView({behavior:'smooth'});
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

function prefillManage(pnr){ showTab('manage'); document.getElementById('m-pnr').value=pnr; document.getElementById('m-last').focus(); }

async function loadBooking(){
  const out = document.getElementById('booking-detail');
  const q = new URLSearchParams({tenant:document.getElementById('m-tenant').value, pnr:document.getElementById('m-pnr').value.trim().toUpperCase(),
    last_name:document.getElementById('m-last').value.trim()});
  try{
    const d = await api('/api/portal/booking?'+q.toString());
    const rows = d.bookings||[];
    const segs = rows.map(b=>`<tr><td>${esc(b.segment_index+1)}</td><td>${esc(b.flight_no||'')}</td><td>${esc(b.origin||'')} → ${esc(b.destination||'')}</td>
      <td>${esc(b.flight_date||'')}</td><td>${esc(b.passenger_name)}</td><td>${esc(b.ticket_number||'Not issued')}</td>
      <td>${esc(b.payment_status)}</td><td><a href="/api/documents/pdf/eticket/${b.id}" target="_blank">E-ticket</a></td></tr>`).join('');
    const pays = (d.payments||[]).map(p=>`<div class="trace-item"><b>${esc(p.payment_no)}</b> · ${money(p.amount,p.currency)} · ${esc(p.status)}<small>${esc(p.method)} · ${esc(p.paid_at||'')}</small></div>`).join('')||'<div class="muted">No payments recorded</div>';
    const cis = (d.checkins||[]).map(c=>`<div class="trace-item"><b>${esc(c.checkin_no)}</b> · seat ${esc(c.seat_no)} · ${esc(c.status)}</div>`).join('')||'<div class="muted">Not checked in yet</div>';
    const anc = (d.ancillaries||[]).map(a=>`<div class="trace-item"><b>${esc(a.description)}</b> · ${money(a.amount,a.currency)}</div>`).join('')||'<div class="muted">No extras</div>';
    const tr = (d.trace||[]).map(t=>`<div class="trace-item"><b>${esc(t.action)}</b> · ${esc(t.source)}<small>${esc(t.actor)} · ${esc(t.created_at)} · ${esc(t.detail||'')}</small></div>`).join('')||'<div class="muted">No trace events</div>';
    const total = rows.reduce((a,b)=>a+Number(b.total_amount||0),0);
    out.innerHTML = `<div class="portal-card"><div class="success"><b>PNR ${esc(rows[0].pnr)}</b> · ${esc(d.airline.name)} · ${esc(rows[0].trip_type||'')}</div>
      <table><thead><tr><th>Seg</th><th>Flight</th><th>Route</th><th>Date</th><th>Passenger</th><th>Ticket</th><th>Payment</th><th></th></tr></thead><tbody>${segs}</tbody></table></div>
      <div class="portal-card"><h3>Payment</h3>${pays}
        <div class="portal-grid" style="margin-top:12px">
          <div class="portal-field"><label>Amount (total ${money(total)})</label><input id="p-amount" type="number" step="0.01" value="${total.toFixed(2)}" /></div>
          <div class="portal-field"><label>Method</label><select id="p-method"><option value="flutterwave">Pay online (Flutterwave / MoMo / Card)</option><option value="mobilemoney">Mobile money (recorded)</option><option value="card">Card (recorded)</option><option value="bank">Bank (recorded)</option><option value="cash">Cash (recorded)</option></select></div>
          <div class="portal-field"><label>Reference</label><input id="p-ref" /></div>
        </div><div style="margin-top:12px"><button class="portal-btn" onclick="pay('${esc(rows[0].pnr)}')">Pay now</button></div>
        <div id="p-msg" class="error hidden" style="margin-top:10px"></div></div>
      <div class="portal-card"><h3>Ancillary services</h3>${anc}</div>
      <div class="portal-card"><h3>Check-in</h3>${cis}</div>
      <div class="portal-card"><h3>Traceability</h3>${tr}</div>`;
    out.classList.remove('hidden');
  }catch(e){ out.innerHTML=`<div class="portal-card error">${esc(e.message)}</div>`; out.classList.remove('hidden'); }
}

async function pay(pnr){
  const msg = document.getElementById('p-msg');
  const method=document.getElementById('p-method').value;
  try{
    if(method==='flutterwave'){
      const r=await api('/api/pss/payments/flutterwave/init',{method:'POST',body:JSON.stringify({tenant:document.getElementById('m-tenant').value,pnr,
        amount:document.getElementById('p-amount').value,email:document.getElementById('bk-email')?.value||'',phone:document.getElementById('bk-phone')?.value||''})});
      if(r.url){ location.href=r.url.startsWith('http')?r.url:((window.API_BASE||'')+r.url); return; }
      msg.textContent='Payment gateway unavailable'; msg.classList.remove('hidden'); return;
    }
    await api('/api/portal/pay',{method:'POST',body:JSON.stringify({tenant:document.getElementById('m-tenant').value, pnr,
      last_name:document.getElementById('m-last').value.trim(), amount:document.getElementById('p-amount').value,
      method, reference:document.getElementById('p-ref').value})});
    loadBooking();
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
    portalCustomer=r.customer; msg.textContent='Signed in as '+esc(r.customer.full_name);
    await loadMyTrips();
  }catch(e){ msg.textContent=e.message; }
}
async function portalLogout(){
  await api('/api/portal/logout',{method:'POST',body:'{}'});
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
      <td>${esc(b.payment_status)}</td><td><a href="/api/documents/pdf/eticket/${b.id}" target="_blank">E-ticket</a></td>
      <td><button class="portal-btn secondary" onclick="prefillManage('${esc(b.pnr)}')">Manage</button></td></tr>`).join('');
    box.innerHTML=`<div class="portal-card"><div style="display:flex;justify-content:space-between"><h3>My trips · ${esc(d.customer.full_name)}</h3><button class="portal-btn secondary" onclick="portalLogout()">Sign out</button></div>
      <table><thead><tr><th>PNR</th><th>Flight</th><th>Route</th><th>Date</th><th>Passenger</th><th>Ticket</th><th>Payment</th><th></th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="9" class="muted">No trips yet - book a flight to see it here.</td></tr>'}</tbody></table></div>
      <div class="portal-card"><h3>My travel documents</h3><div id="my-docs" class="muted">Loading...</div>
        <div class="portal-grid" style="margin-top:10px"><div class="portal-field"><label>Document type</label><select id="td-type"><option>passport</option><option>visa</option><option>id</option><option>insurance</option><option>other</option></select></div>
        <div class="portal-field"><label>Document number</label><input id="td-no" /></div>
        <div class="portal-field"><label>PNR</label><input id="td-pnr" /></div>
        <div class="portal-field"><label>File</label><input id="td-file" type="file" /></div></div>
        <div style="margin-top:10px"><button class="portal-btn" onclick="uploadTravelDoc()">Upload document</button></div>
        <div id="td-msg" class="muted" style="margin-top:8px"></div></div>`;
    api('/api/portal/documents').then(r=>{ const el=document.getElementById('my-docs'); if(!el)return;
      el.className=''; el.innerHTML=(r.documents||[]).map(x=>`<div class="trace-item"><b>${esc(x.doc_type)} ${esc(x.doc_no||'')}</b><small>${esc(x.title||'')} · ${esc(x.file_name||'')} · ${esc(x.created_at||'')}</small></div>`).join('')||'<div class="muted">No documents uploaded</div>';
    }).catch(()=>{});
  }catch(e){ /* not signed in */ }
}
async function uploadTravelDoc(){
  const msg=document.getElementById('td-msg');
  const file=document.getElementById('td-file').files[0];
  if(!file){ msg.textContent='Choose a file first'; return; }
  const dataUrl=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(file);});
  try{
    await api('/api/portal/documents',{method:'POST',body:JSON.stringify({pnr:document.getElementById('td-pnr').value.trim(),
      doc_type:document.getElementById('td-type').value,doc_no:document.getElementById('td-no').value.trim(),
      title:document.getElementById('td-type').value,file_name:file.name,mime_type:file.type,file_base64:dataUrl})});
    msg.textContent='Document uploaded and linked to your booking'; loadMyTrips();
  }catch(e){ msg.textContent=e.message; }
}

init().catch(e=>{ document.getElementById('airline-name').textContent='Portal unavailable'; });
