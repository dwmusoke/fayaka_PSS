const token = location.pathname.split('/').filter(Boolean).pop();
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const money = (n,c='USD') => (n===null||n===undefined)?'-':`${c} ${Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`;
function getCsrf(){ const m=document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/); return m?m[1]:''; }
let offer = null;
let airline = null;

async function api(path, opts={}) {
  const r = await fetch((window.API_BASE||'')+path, {headers:{'Content-Type':'application/json','X-CSRF-Token':getCsrf()}, ...opts});
  const d = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(d.error || 'Request failed');
  return d;
}

async function init(){
  const d = await api('/api/agency/offer/'+encodeURIComponent(token));
  offer = d.offer;
  airline = d.airline || airline || {};
  document.getElementById('ag-title').textContent = `${(airline && airline.name) || 'Airline'} - Direct E-ticket`;
  document.getElementById('ag-subtitle').textContent = `Issued to ${offer.agency_name} - offer ${offer.offer_no}`;
  const left = Number(offer.quantity||0) - Number(offer.used_count||0);
  document.getElementById('offer-card').innerHTML = `<div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">
    <div><div class="muted">Flight</div><b style="font-size:18px">${esc(offer.flight_no||'Open flight')}</b>
      <div class="muted">${esc(offer.origin||'')} - ${esc(offer.destination||'')} - ${esc(offer.flight_date||'')} ${esc(offer.dep_time||'')}</div></div>
    <div style="text-align:right"><div class="pill">${esc(offer.status)}</div><div class="muted" style="margin-top:6px">${left} of ${offer.quantity} e-tickets left</div></div></div>
    <hr style="border:0;border-top:1px solid #eef1f5;margin:14px 0">
    <div class="ag-grid">
      <div><label>Fare</label><b>${money(offer.fare_amount,offer.currency)}</b></div>
      <div><label>Taxes</label><b>${money(offer.taxes,offer.currency)}</b></div>
      <div><label>Valid until</label><b>${esc(offer.valid_until||'-')}</b></div>
      <div><label>Commission</label><b>${esc(offer.commission_rate||0)}%</b></div>
    </div>
    <button class="ag-btn" onclick="openBook()" ${left<=0?'disabled':''}>Book passenger against this e-ticket</button>`;
  await loadList();
}

function openBook(){
  const box = document.getElementById('book-card');
  box.innerHTML = `<h3>New booking - ${esc(offer.offer_no)}</h3>
    <div class="ag-grid">
      <div><label>Passenger full name</label><input id="bk-name" placeholder="As on passport" /></div>
      <div><label>Email</label><input id="bk-email" type="email" /></div>
      <div><label>Phone</label><input id="bk-phone" /></div>
      <div><label>Passenger type</label><select id="bk-type"><option>ADT</option><option>CHD</option><option>INF</option></select></div>
      <div><label>Travel document no.</label><input id="bk-doc" placeholder="Passport / ID" /></div>
    </div>
    <button class="ag-btn" onclick="submitBook()">Issue e-ticket</button>
    <button class="ag-btn secondary" onclick="document.getElementById('book-card').classList.add('hidden')">Cancel</button>
    <div id="bk-msg" class="error hidden" style="margin-top:10px"></div>`;
  box.classList.remove('hidden'); box.scrollIntoView({behavior:'smooth'});
}

async function submitBook(){
  const msg = document.getElementById('bk-msg');
  try{
    const r = await api('/api/agency/offer/'+encodeURIComponent(token)+'/book',{method:'POST',body:JSON.stringify({
      passenger_name:document.getElementById('bk-name').value.trim(), email:document.getElementById('bk-email').value.trim(),
      phone:document.getElementById('bk-phone').value.trim(), passenger_type:document.getElementById('bk-type').value,
      doc_no:document.getElementById('bk-doc').value.trim()})});
    document.getElementById('book-card').classList.add('hidden');
    const et = r.booking_id ? `/api/documents/pdf/eticket/${r.booking_id}?token=${encodeURIComponent(token)}` : '';
    const it = r.booking_id ? `/api/documents/pdf/journeypass/${r.booking_id}?token=${encodeURIComponent(token)}` : '';
    document.getElementById('confirm-card').innerHTML = `<div class="ok"><b>Booking created${r.ticket_number?' and e-ticket issued':''}.</b></div>
      <div class="ag-grid" style="margin-top:12px">
        <div><label>PNR</label><b>${esc(r.pnr)}</b></div>
        <div><label>Ticket number</label><b>${esc(r.ticket_number||'Pending')}</b></div>
        <div><label>Payment status</label><b>${esc(r.payment_status||'pending')}</b></div>
        <div><label>Trace ID</label><b>${esc(r.trace_id)}</b></div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        ${et?`<a class="ag-btn" style="text-decoration:none;display:inline-block;margin-top:0" href="${et}" target="_blank">Open e-ticket</a>`:''}
        ${it?`<a class="ag-btn secondary" style="text-decoration:none;display:inline-block;margin-top:0" href="${it}" target="_blank">Open itinerary / travel doc</a>`:''}
      </div>
      <div class="muted" style="margin-top:10px">If payment is pending, use "Pay online" below (Flutterwave/mobile money/card when configured) or contact the airline.</div>`;
    document.getElementById('confirm-card').classList.remove('hidden');
    await loadList(); await init();
  }catch(e){ msg.textContent=e.message; msg.classList.remove('hidden'); }
}

async function payBooking(id, pnr, amount, currency){
  try{
    const tenant = (airline && airline.code) || (offer && offer.tenant_code) || (airline && airline.name) || '';
    if(!tenant){ alert('Airline/tenant code unavailable for payment. Contact the airline.'); return; }
    const r = await api('/api/pss/payments/flutterwave/init',{method:'POST',body:JSON.stringify({tenant, pnr, amount: Number(amount||0), currency: currency||'USD'})});
    if(r.url){ location.href = r.url.startsWith('http') ? r.url : ((window.API_BASE||'') + r.url); return; }
    alert(r.error || 'Online payment link unavailable. If no gateway is configured, contact the airline to record payment.');
  }catch(e){ alert(e.message); }
}

async function loadList(){
  try{
    const d = await api('/api/agency/offer/'+encodeURIComponent(token)+'/bookings');
    if(d.airline) airline = d.airline;
    const rows = (d.bookings||[]).map(b=>{
      const id = b.id;
      const cur = b.currency || (offer && offer.currency) || 'USD';
      const paid = String(b.payment_status||'').toLowerCase()==='paid';
      const payBtn = (!paid && id) ? `<button class="ag-btn secondary" style="margin-top:0;padding:6px 10px;font-size:12px" onclick="payBooking(${id},'${esc(b.pnr||'')}',${Number(b.total_amount||0)},'${esc(cur)}')">Pay online</button>` : (paid ? '<span class="muted">Paid</span>' : '');
      const docs = id ? `<a class="muted" href="/api/documents/pdf/eticket/${id}?token=${encodeURIComponent(token)}" target="_blank">E-ticket</a> | <a class="muted" href="/api/documents/pdf/journeypass/${id}?token=${encodeURIComponent(token)}" target="_blank">Itinerary</a>` : '-';
      return `<tr><td><b>${esc(b.pnr)}</b></td><td>${esc(b.passenger_name)}</td>
      <td>${esc(b.ticket_number||'-')}</td><td>${money(b.total_amount,cur)}</td><td>${esc(b.payment_status||'pending')}</td><td>${esc(b.booking_status||'-')}</td>
      <td>${docs}</td><td>${payBtn}</td></tr>`;
    }).join('');
    document.getElementById('list-card').innerHTML = `<h3>Bookings on this direct offer</h3>
      <div class="muted" style="margin-bottom:8px">Pending bookings: pay online via Flutterwave/mobile money/card when the airline gateway is configured, or ask the airline to record payment. Use E-ticket/Itinerary links to view and share travel documents (itinerary is available even if payment is pending).</div>
      <table><thead><tr><th>PNR</th><th>Passenger</th><th>Ticket</th><th>Total</th><th>Payment</th><th>Status</th><th>Documents</th><th></th></tr></thead>
      <tbody>${rows||'<tr><td colspan="8" class="muted">No bookings yet</td></tr>'}</tbody></table>`;
  }catch(e){ document.getElementById('list-card').innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

init().catch(e=>{ document.getElementById('offer-card').innerHTML = `<div class="error">${esc(e.message)}</div>`; });
