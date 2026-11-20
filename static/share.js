const root = document.getElementById('share-root');
const token = location.pathname.split('/').filter(Boolean).pop();
const esc = (s) => String(s??'').replace(/[&<>"']/g, m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
const money = (n, c='USD') => (n===null||n===undefined) ? '-' : `${c} ${Number(n).toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2})}`;

function fact(label, value){ return `<div class="share-fact"><small>${esc(label)}</small><b>${esc(value ?? '-')}</b></div>`; }

function brandMark(b){
  const name = b.company_name || 'Travel ERP';
  const initials = (b.logo_text || name.slice(0,3) || 'TE').toUpperCase().slice(0,3);
  const img = b.logo_data ? `<img src="${esc(b.logo_data)}" alt="${esc(name)} logo" style="max-height:46px;max-width:120px;object-fit:contain;display:block" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">` : '';
  const mono = `<span style="display:${img?'none':'flex'};align-items:center;justify-content:center;width:44px;height:44px;border-radius:10px;background:rgba(255,255,255,.18);color:#fff;font-weight:700;font-size:15px">${esc(initials)}</span>`;
  return `<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">${img}${mono}<div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:.85">${esc(b.location || 'Client document')}</div></div>`;
}

function summary(data){
  const c = data.context || {};
  if(c.payment) return [
    fact('Receipt', c.payment.payment_no),
    fact('Customer', c.customer?.name),
    fact('Invoice', c.invoice?.invoice_no),
    fact('Booking', c.order?.pnr),
    fact('Method', c.payment.method),
    fact('Reference', c.payment.reference),
    fact('Status', c.payment.status),
    fact('Amount', money(c.payment.amount, c.payment.currency))
  ].join('');
  if(c.invoice) return [
    fact('Invoice', c.invoice.invoice_no),
    fact('Customer', c.customer?.name),
    fact('Issue date', c.invoice.issue_date),
    fact('Due date', c.invoice.due_date),
    fact('Total', money(c.invoice.total, c.invoice.currency)),
    fact('Balance', money(c.invoice.balance, c.invoice.currency))
  ].join('');
  if(c.estimate) return [
    fact('Estimate', c.estimate.estimate_no),
    fact('Title', c.estimate.title),
    fact('Customer', c.customer?.name),
    fact('Issue date', (c.estimate.created_at||'').slice(0,10)),
    fact('Valid until', c.estimate.valid_until),
    fact('Total', money(c.estimate.total, c.estimate.currency)),
    fact('Status', c.estimate.status)
  ].join('');
  if(c.pss_booking) return [
    fact('PNR', c.pss_booking.pnr),
    fact('Passenger', c.pss_booking.passenger_name),
    fact('Flight', c.pss_booking.flight_no),
    fact('Route', `${c.pss_booking.origin||'-'} ? ${c.pss_booking.destination||'-'}`),
    fact('Ticket', c.pss_booking.ticket_number||'Not issued'),
    fact('Payment', c.pss_booking.payment_status),
    fact('Total', money(c.pss_booking.total_amount, c.pss_booking.currency))
  ].join('');
  if(c.checkin) return [
    fact('Check-in', c.checkin.checkin_no),
    fact('Passenger', c.checkin.passenger_name),
    fact('Flight', c.checkin.flight_no),
    fact('Seat', c.checkin.seat_no),
    fact('Bags', c.checkin.bag_count),
    fact('Method', c.checkin.checkin_method),
    fact('Status', c.checkin.status)
  ].join('');
  if(c.credit_note) return [
    fact('Credit note', c.credit_note.credit_note_no),
    fact('Customer', c.customer?.name),
    fact('Invoice', c.invoice?.invoice_no),
    fact('Reason', c.credit_note.reason),
    fact('Issue date', c.credit_note.issue_date),
    fact('Amount', money(c.credit_note.amount, c.credit_note.currency)),
    fact('Status', c.credit_note.status)
  ].join('');
  if(c.bill) return [
    fact('Bill', c.bill.bill_no),
    fact('Supplier', c.bill.supplier),
    fact('Due date', c.bill.due_date),
    fact('Status', c.bill.status),
    fact('Total', money(c.bill.total, c.bill.currency)),
    fact('Balance', money(c.bill.balance, c.bill.currency))
  ].join('');
  if(c.order) return [
    fact('PNR / Booking', c.order.pnr || c.order.order_number),
    fact('Route', `${c.order.origin||'-'} → ${c.order.destination||'-'}`),
    fact('Travel', `${c.order.travel_start||'-'} / ${c.order.travel_end||'-'}`),
    fact('Ticket status', c.order.ticket_status),
    fact('Total sell', money(c.order.total_sell, c.order.currency)),
    fact('Balance', money(c.order.balance_due, c.order.currency))
  ].join('');
  if(c.customer && (c.orders||c.invoices)) return [
    fact('Customer', c.customer.name),
    fact('Company', c.customer.company),
    fact('Bookings', (c.orders||[]).length),
    fact('Lifetime spend', money(c.customer.spend)),
    fact('Email', c.customer.email),
    fact('Phone', c.customer.phone)
  ].join('');
  if(c.traveler) return [
    fact('Traveler', c.traveler.display_name || c.traveler.full_name),
    fact('Document', `${c.traveler.doc_type || 'Passport'} ${c.traveler.doc_no || ''}`.trim()),
    fact('Travel status', c.traveler.travel_status),
    fact('Bookings', (c.orders||c.bookings||[]).length),
    fact('Collection', money((c.summary||{}).sell ?? c.traveler.collection)),
    fact('Next travel', c.traveler.next_travel)
  ].join('');
  return '';
}

function timeline(activity){
  if(!activity || !activity.length) return '<div class="muted small">No activity yet.</div>';
  return activity.map(a=>`<div class="activity-item"><div class="activity-dot">${esc(String(a.action||'-').slice(0,2).toUpperCase())}</div><div><b>${esc(a.action?.replace(/_/g,' ')||'-')}</b><small>${esc(a.actor||'system')} · ${esc(a.created_at||'')}</small>${a.detail?`<div class="tiny muted">${esc(a.detail)}</div>`:''}</div></div>`).join('');
}

function render(data){
  const b = data.branding || {};
  const titleMap = {booking:'Booking confirmation', invoice:'Invoice', customer:'Customer statement', traveler:'Traveler statement', payment:'Payment receipt', receipt:'Payment receipt', bill:'Supplier bill', order:'Booking confirmation', estimate:'Estimate', credit_note:'Credit note', eticket:'Electronic ticket', boardingpass:'Boarding pass', journeypass:'Travel Itinerary'};
  const title = titleMap[data.entity_type] || 'Client document';
  const pdfKind = data.entity_type === 'order' ? 'booking' : data.entity_type;
  root.innerHTML = `<div class="share-top"><div><h2 style="margin:0">${esc(title)}</h2><div class="muted small">Secure client view · prepared by ${esc(b.company_name||'Travel ERP')}</div></div><div><span class="live-pill"><span class="live-dot"></span>Live · ${data.share?.views||0} views</span> <a class="btn" style="background:${esc(b.accent_color||'#2563eb')};color:#fff;padding:9px 13px;border-radius:9px;text-decoration:none;font-weight:600;display:inline-block" href="/api/documents/pdf/${esc(pdfKind)}/${data.entity_id}?token=${encodeURIComponent(token)}" target="_blank">Open PDF</a></div></div>
  <div class="share-hero" style="background:linear-gradient(135deg,${esc(b.primary_color||'#0f2742')},${esc(b.accent_color||'#2563eb')})">${brandMark(b)}<h1 style="margin:8px 0;color:#fff;font-size:24px">${esc(b.company_name||'Travel ERP')}</h1><div style="opacity:.85;font-size:12px">${esc(b.address||'')} ${esc(b.phone||'')} ${esc(b.email||'')}</div></div>
  <div class="share-body"><div class="share-grid">${summary(data)}</div><hr><div class="workspace-grid grid two"><div><h3>Live activity</h3><div class="activity-list">${timeline(data.activity)}</div></div><div><h3>Document note</h3><p class="muted small">${esc(b.footer_note||'This document is shared for client review. Values update as the booking, invoice or payment status changes.')}</p><p class="tiny muted">Recipient: ${esc(data.share?.recipient||'Client')} · Permission: ${esc(data.share?.permission||'view')} · Created ${esc(data.share?.created_at||'')}</p></div></div></div>`;
}

async function load(){
  try{
    const res = await fetch(`${window.API_BASE||''}/api/public/share/${encodeURIComponent(token)}`, {cache:'no-store'});
    const data = await res.json();
    if(!res.ok) throw new Error(data.error || 'Unable to load document');
    render(data);
  }catch(e){ root.innerHTML = `<div class="loading error">${esc(e.message)}</div>`; }
}

load();
setInterval(load, 15000);
