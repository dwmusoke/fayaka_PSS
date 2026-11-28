import base64
import io
import json
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.graphics.barcode import qr
from reportlab.graphics.barcode import code128
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas


def _hex_color(value, fallback):
    value = (value or '').strip()
    if value.startswith('#') and len(value) in (4, 7):
        try:
            return colors.HexColor(value)
        except Exception:
            return fallback
    return fallback


def _lines(text, width=92):
    text = str(text or '')
    words = text.split()
    out, line = [], ''
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or ([''] if text == '' else [text[:width]])


def _money(value, currency='USD'):
    try:
        return f"{currency} {float(value or 0):,.2f}"
    except Exception:
        return f"{currency} {value or 0}"


def _iata_name(name, title=None):
    value = str(name or '').strip().upper()
    if '/' in value:
        last, _, first = value.partition('/')
        first = first.strip()
        if title and title.upper() not in first:
            first = (first + ' ' + title.upper()).strip()
        return f"{last.strip()}/{first}".strip('/')
    parts = value.split()
    if len(parts) >= 2:
        last, first = parts[-1], ' '.join(parts[:-1])
        if title and title.upper() not in first:
            first = (first + ' ' + title.upper()).strip()
        return f"{last}/{first}"
    return value + (('/' + title.upper()) if title else '')


def _bag_tag_no(bag):
    bag = bag or {}
    if not isinstance(bag, dict):
        return str(bag)
    for key in ('tag_no', 'bag_tag_no', 'bag_tag', 'tag', 'tag_number', 'bag_number', 'serial_no', 'id'):
        if bag.get(key):
            return str(bag[key])
    return ''


def _selected_bag(context):
    bags = context.get('baggage') or []
    idx = 0
    try:
        idx = int(context.get('bag_index') or 0)
    except Exception:
        idx = 0
    if bags:
        try:
            if 0 <= idx < len(bags):
                return bags[idx]
        except Exception:
            pass
        return bags[0]
    ci = context.get('checkin') or context.get('pss_checkin') or {}
    tags = ci.get('bag_tags') or ''
    if isinstance(tags, (list, tuple)):
        tags = tags[0] if tags else ''
    else:
        tags = str(tags).replace(';', ',').split(',')[0].strip()
    return {'tag_no': tags}


def _fit(text, font, size, max_w, c):
    text = str(text or '')
    if max_w <= 0:
        return ''
    while text and c.stringWidth(text, font, size) > max_w:
        text = text[:-1]
    return text


def _brand(branding):
    primary = _hex_color(branding.get('primary_color'), colors.HexColor('#206090'))
    accent = _hex_color(branding.get('accent_color'), colors.HexColor('#e02020'))
    tint = colors.Color(accent.red * 0.10 + 0.90, accent.green * 0.10 + 0.90, accent.blue * 0.10 + 0.90)
    ink = colors.HexColor('#132238')
    muted = colors.HexColor('#5b6b7f')
    return primary, accent, tint, ink, muted


def _qr(c, payload, x, y, size=70):
    widget = qr.QrCodeWidget(str(payload or 'PSS'))
    bounds = widget.getBounds()
    bw, bh = bounds[2] - bounds[0], bounds[3] - bounds[1]
    scale_x, scale_y = size / bw, size / bh
    drawing = Drawing(size, size, transform=[scale_x, 0, 0, scale_y,
                                             -bounds[0] * scale_x, -bounds[1] * scale_y])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x, y)


def _barcode128(c, value, x, y, width=300, height=34):
    text = str(value if value not in (None, '') else '')
    try:
        widget = code128.Code128(text or ' ', barHeight=height, barWidth=0.32, humanReadable=False)
        bw, bh = float(widget.width or 0), float(widget.height or 0)
        if bw <= 0 or bh <= 0:
            raise ValueError('zero size')
        # Code128 is a Flowable, not a Shape; draw it directly with its own drawOn
        widget.drawOn(c, x, y)
        return
    except Exception as e:
        import sys
        print('barcode128 failed:', repr(e), file=sys.stderr)
    try:
        c.setFillColor(colors.black)
    except Exception:
        pass
    try:
        c.setFont('Helvetica', 8)
    except Exception:
        pass
    try:
        c.drawString(x, y, _fit(text, 'Helvetica', 8, max(1, width)) or text[:200])
    except Exception:
        pass


def _bcbp(ci, bk):
    ci = ci or {}
    bk = bk or {}

    def fld(value, length, pad=' ', right=False):
        text = str(value if value not in (None, '') else '').strip()[:(length)]
        return text.rjust(length, pad) if right else text.ljust(length, pad)

    name = str(_iata_name(ci.get('passenger_name') or bk.get('passenger_name')) or '').strip()
    if not name:
        name = 'PAX/'
    flight = str(ci.get('flight_no') or bk.get('flight_no') or '').strip()
    letters = ''.join(ch for ch in flight if ch.isalpha()).upper()
    digits = ''.join(ch for ch in flight if ch.isdigit())
    carrier = fld(letters[:3] or (bk.get('carrier') or bk.get('operating_carrier') or ''), 3)
    number = fld(digits[:5], 5)
    raw_date = str(ci.get('flight_date') or bk.get('flight_date') or '').strip()
    doy = '001'
    if raw_date:
        parsed = None
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y', '%Y%m%d'):
            try:
                parsed = datetime.strptime(raw_date[:10], fmt)
                break
            except Exception:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw_date[:10])
            except Exception:
                pass
        if parsed is not None:
            doy = fld(parsed.timetuple().tm_yday, 3, '0', True)
    compartment = str(ci.get('cabin_class') or bk.get('fare_class') or bk.get('cabin_class') or 'Y').strip()[:1].upper() or 'Y'
    try:
        seq = int(ci.get('id') or ci.get('checkin_id') or 0)
    except Exception:
        seq = 0
    seq = max(0, seq)
    try:
        bags = int(ci.get('bag_count') or bk.get('bag_count') or 0)
    except Exception:
        bags = 0
    bags = max(0, bags)
    return ''.join([
        'M1',
        fld(name, 20),
        'E',
        fld(ci.get('pnr') or bk.get('pnr'), 7),
        fld(ci.get('origin') or bk.get('origin'), 3),
        fld(ci.get('destination') or bk.get('destination'), 3),
        carrier,
        number,
        doy,
        compartment,
        fld(ci.get('seat_no') or bk.get('seat_no'), 4),
        fld(seq, 5, '0', True),
        fld('1', 1),
        fld(0, 2, '0', True),
    ])


def _verify_payload(kind, context, branding):
    refs = {
        'invoice': (context.get('invoice') or {}).get('invoice_no'),
        'booking': (context.get('order') or {}).get('pnr'),
        'bill': (context.get('bill') or {}).get('bill_no'),
        'traveler': (context.get('traveler') or {}).get('doc_no'),
        'receipt': (context.get('payment') or {}).get('payment_no'),
        'estimate': (context.get('estimate') or {}).get('estimate_no'),
        'credit_note': (context.get('credit_note') or context.get('note') or {}).get('credit_note_no'),
        'eticket': (context.get('pss_booking') or {}).get('ticket_number'),
        'boardingpass': (context.get('checkin') or context.get('pss_checkin') or {}).get('checkin_no'),
        'journeypass': (context.get('pss_booking') or {}).get('pnr'),
        'baggagetag': _bag_tag_no(_selected_bag(context)),
        'upliftcoupon': (context.get('uplift_sale') or {}).get('sale_no') or (context.get('checkin') or {}).get('checkin_no'),
    }
    ref = str(refs.get(kind) or '')
    extra = ''
    if kind == 'eticket':
        b = context.get('pss_booking') or {}
        extra = f"PNR {b.get('pnr') or ''} | {b.get('passenger_name') or ''} | {b.get('flight_no') or ''}"
    elif kind == 'boardingpass':
        b = context.get('pss_booking') or context.get('booking') or {}
        ci = context.get('checkin') or context.get('pss_checkin') or {}
        extra = f"PNR {b.get('pnr') or ''} | SEAT {ci.get('seat_no') or ''}"
    elif kind == 'baggagetag':
        b = context.get('pss_booking') or context.get('booking') or {}
        extra = f"PNR {b.get('pnr') or ''} | {_iata_name(b.get('passenger_name'))}"
    elif kind == 'upliftcoupon':
        b = context.get('pss_booking') or context.get('booking') or {}
        sale = context.get('uplift_sale') or {}
        extra = f"PNR {b.get('pnr') or ''} | {_bag_tag_no(_selected_bag(context))} | AMT {sale.get('amount') or 0}"
    elif kind == 'receipt':
        p = context.get('payment') or {}
        extra = f"AMT {p.get('amount') or ''} {p.get('currency') or ''}"
    elif kind == 'booking':
        extra = str((context.get('customer') or {}).get('name') or '')
    website = (branding.get('website') or '').strip().rstrip('/')
    prefix = f"{website}/verify" if website else 'PSS VERIFY'
    return f"{prefix} | {kind.upper()} | {ref} | {extra}".strip(' |')[:180]


def _qr_panel(c, y, branding, heading, payload, note='Scan with any QR reader to verify this document'):
    width, _ = A4
    primary, accent, tint, ink, muted = _brand(branding)
    box_h = 104
    if y < box_h + 66:
        c.showPage()
        y = A4[1] - 60
    x, w = 42, width - 84
    top = y
    c.setFillColor(tint)
    c.setStrokeColor(accent)
    c.setLineWidth(0.8)
    c.roundRect(x, top - box_h, w, box_h, 8, stroke=1, fill=1)
    c.setFillColor(primary)
    c.roundRect(x, top - 24, w, 24, 8, stroke=0, fill=1)
    c.rect(x, top - 28, w, 6, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(x + 12, top - 16, str(heading).upper())
    _qr(c, payload, x + 12, top - box_h + 14, 74)
    tx = x + 100
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(tx, top - 44, 'Verification code')
    c.setFont('Helvetica', 8)
    c.setFillColor(muted)
    c.drawString(tx, top - 58, str(note)[:95])
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 9)
    for i, chunk in enumerate(_lines(payload, 62)[:3]):
        c.drawString(tx, top - 74 - i * 11, chunk[:90])
    return top - box_h - 16


def _logo(branding, left, top, c):
    data = branding.get('logo_data') or ''
    if isinstance(data, str) and data.startswith('data:'):
        data = data.split(',', 1)[-1]
    primary = _hex_color(branding.get('primary_color'), colors.HexColor('#0f2742'))
    accent = _hex_color(branding.get('accent_color'), colors.HexColor('#0b6efd'))
    plate_w, plate_h = 38, 38
    pad = 5
    c.setFillColor(colors.white)
    c.setStrokeColor(colors.HexColor('#e3e8ef'))
    c.setLineWidth(0.6)
    c.roundRect(left, top - plate_h, plate_w, plate_h, 8, stroke=1, fill=1)
    if data:
        try:
            raw = base64.b64decode(data)
            img = ImageReader(io.BytesIO(raw))
            iw, ih = img.getSize()
            max_w, max_h = plate_w - pad * 2, plate_h - pad * 2
            scale = min(max_w / float(iw or 1), max_h / float(ih or 1))
            dw, dh = max(1, iw * scale), max(1, ih * scale)
            ix = left + (plate_w - dw) / 2.0
            iy = top - plate_h + (plate_h - dh) / 2.0
            c.drawImage(img, ix, iy, width=dw, height=dh, mask='auto', preserveAspectRatio=True, anchor='sw')
            return left + plate_w + 10
        except Exception:
            pass
    text = (branding.get('logo_text') or (branding.get('company_name') or 'FA')[:2] or 'FA').upper()
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 12)
    c.drawCentredString(left + plate_w / 2.0, top - plate_h / 2.0 - 4, str(text)[:3])
    c.setFillColor(accent)
    c.rect(left + 9, top - plate_h + 7, plate_w - 18, 1.2, stroke=0, fill=1)
    return left + plate_w + 10

def _footer(c, branding):
    width, _ = A4
    primary, accent, tint, ink, muted = _brand(branding)
    c.setFillColor(accent)
    c.rect(42, 46, width - 84, 2.5, stroke=0, fill=1)
    c.setFillColor(muted)
    c.setFont('Helvetica', 8)
    c.drawString(42, 30, str(branding.get('footer_note') or 'Fayaka Airline - Confidential')[:120])
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 8)
    c.drawRightString(width - 42, 30, f"Page {c.getPageNumber()}")


def _header(c, branding, title, subtitle, continuation=False):
    width, height = A4
    primary = _hex_color(branding.get('primary_color'), colors.HexColor('#0f2742'))
    accent = _hex_color(branding.get('accent_color'), colors.HexColor('#0b6efd'))
    doc_title = str(title or '')
    doc_sub = str(subtitle or '')
    if continuation:
        c.setFillColor(primary)
        c.rect(0, height - 30, width, 30, stroke=0, fill=1)
        c.setFillColor(accent)
        c.rect(0, height - 32, width, 2, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(42, height - 19, str(branding.get('company_name') or 'Fayaka Airline'))
        c.setFillColor(colors.HexColor('#d7e3f0'))
        c.setFont('Helvetica', 8.5)
        label = f"{doc_title}  ·  {doc_sub}" if doc_sub else doc_title
        c.drawRightString(width - 42, height - 19, _fit(label, 'Helvetica', 8.5, 260, c))
        return height - 48

    band_h = 108
    c.setFillColor(primary)
    c.rect(0, height - band_h, width, band_h, stroke=0, fill=1)
    c.setFillColor(accent)
    c.rect(0, height - band_h - 3, width, 3, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFillAlpha(0.05)
    c.circle(width - 28, height - 12, 64, stroke=0, fill=1)
    c.setFillAlpha(1)

    x = _logo(branding, 42, height - 34, c)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 13.5)
    c.drawRightString(width - 42, height - 46, doc_title)
    t_w = c.stringWidth(doc_title, 'Helvetica-Bold', 13.5)
    c.setStrokeColor(accent)
    c.setLineWidth(1)
    c.line(width - 42 - min(t_w, 150), height - 50, width - 42, height - 50)
    c.setFont('Helvetica', 8.5)
    c.setFillColor(colors.HexColor('#d7e3f0'))
    c.drawRightString(width - 42, height - 62, _fit(doc_sub, 'Helvetica', 8.5, 220, c))

    info_max = max(90, width - 42 - t_w - x - 18)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 15)
    c.drawString(x, height - 44, _fit(branding.get('company_name') or 'Fayaka Airline', 'Helvetica-Bold', 15, info_max, c))
    c.setFont('Helvetica', 8)
    c.setFillColor(colors.HexColor('#d7e3f0'))
    info_y = height - 58
    if branding.get('legal_name'):
        c.drawString(x, info_y, _fit(branding.get('legal_name'), 'Helvetica', 8, info_max, c))
        info_y -= 11
    parts = [p for p in [branding.get('address') or branding.get('location'), branding.get('phone'), branding.get('email'), branding.get('website')] if p]
    if parts:
        c.drawString(x, info_y, _fit(' · '.join(str(p) for p in parts), 'Helvetica', 8, info_max, c))
        info_y -= 11
    tax_bits = [b for b in [branding.get('tax_id') and f"Tax/Reg: {branding['tax_id']}", branding.get('location') if branding.get('address') else None] if b]
    if tax_bits:
        c.drawString(x, info_y, _fit(' · '.join(str(b) for b in tax_bits), 'Helvetica', 8, info_max, c))
    return height - band_h - 22

def _new_page(c, branding, title, subtitle):
    _footer(c, branding)
    c.showPage()
    return _header(c, branding, title, subtitle, continuation=True)


def _facts(c, y, facts, branding, title, subtitle):
    width, _ = A4
    primary, accent, tint, ink, muted = _brand(branding)
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(42, y, 'Document details')
    c.setFillColor(accent)
    c.rect(42, y - 4, 92, 2.5, stroke=0, fill=1)
    y -= 20
    for idx, (key, value) in enumerate(facts):
        if y < 70:
            y = _new_page(c, branding, title, subtitle)
        if idx % 2 == 0:
            c.setFillColor(tint)
            c.rect(42, y - 5, A4[0] - 84, 19, stroke=0, fill=1)
        c.setFillColor(muted)
        c.setFont('Helvetica-Bold', 8.5)
        c.drawString(48, y, str(key))
        c.setFillColor(ink)
        c.setFont('Helvetica', 9)
        c.drawString(205, y, str(value if value not in (None, '') else '-')[:95])
        y -= 19
    return y - 10


def _section(c, y, heading, rows, columns, branding, title, subtitle):
    width, _ = A4
    usable = width - 84
    if y < 110:
        y = _new_page(c, branding, title, subtitle)
    primary, accent, tint, ink, muted = _brand(branding)
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 11)
    c.drawString(42, y, heading)
    c.setFillColor(accent)
    c.circle(38, y + 3, 2.6, stroke=0, fill=1)
    y -= 8
    c.setFillColor(primary)
    c.rect(42, y - 18, usable, 20, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 8)
    col_x = [42]
    for col in columns[:-1]:
        col_x.append(col_x[-1] + usable * col[1])
    for (label, _), x in zip(columns, col_x):
        c.drawString(x + 4, y - 12, str(label).upper())
    y -= 26
    c.setFont('Helvetica', 8)
    c.setFillColor(colors.HexColor('#132238'))
    if not rows:
        c.setFillColor(colors.HexColor('#6b7280'))
        c.drawString(46, y - 2, 'No records')
        return y - 18
    for row in rows:
        values = [str(row.get(col[0], '') or '') for col in columns]
        wrapped = [_lines(v, max(12, int(95 * col[1])))[:3] for v, col in zip(values, columns)]
        height = max(len(w) for w in wrapped) * 12 + 10
        if y - height < 70:
            y = _new_page(c, branding, title, subtitle)
            c.setFillColor(primary)
            c.rect(42, y - 18, usable, 20, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont('Helvetica-Bold', 8)
            for (label, _), x in zip(columns, col_x):
                c.drawString(x + 4, y - 12, str(label).upper())
            y -= 26
            c.setFont('Helvetica', 8)
            c.setFillColor(colors.HexColor('#132238'))
        c.setStrokeColor(colors.HexColor('#e4eaf2'))
        c.line(42, y + 4, width - 42, y + 4)
        for lines, x in zip(wrapped, col_x):
            for i, line in enumerate(lines):
                c.drawString(x + 4, y - i * 12, line[:120])
        y -= height
    c.setStrokeColor(colors.HexColor('#e4eaf2'))
    c.line(42, y + 4, width - 42, y + 4)
    return y - 12


def _totals_box(c, y, lines, branding, title, subtitle):
    width, _ = A4
    box_w, line_h = 260, 19
    needed = len(lines) * line_h + 38
    if y - needed < 70:
        y = _new_page(c, branding, title, subtitle)
    x = width - 42 - box_w
    top = y
    primary, accent, tint, ink, muted = _brand(branding)
    c.setFillColor(tint)
    c.setStrokeColor(accent)
    c.setLineWidth(0.8)
    c.roundRect(x, top - needed, box_w, needed, 6, stroke=1, fill=1)
    c.setFillColor(primary)
    c.roundRect(x, top - 20, box_w, 20, 6, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(x + 10, top - 14, 'AMOUNT SUMMARY')
    ty = top - 32
    for label, value, bold in lines:
        if bold:
            c.setFillColor(accent)
            c.roundRect(x + 6, ty - 4, box_w - 12, 15, 3, stroke=0, fill=1)
            c.setFillColor(colors.white)
        else:
            c.setFillColor(ink)
        c.setFont('Helvetica-Bold' if bold else 'Helvetica', 9 if bold else 8)
        c.drawString(x + 10, ty, str(label))
        c.drawRightString(x + box_w - 10, ty, str(value))
        ty -= line_h
    return top - needed - 14


def _boarding_card(c, y, branding, ci, bk, payload, bags=None, bcbp=None):
    width, _ = A4
    primary, accent, tint, ink, muted = _brand(branding)
    ci = ci or {}
    bk = bk or {}
    x, w = 42, width - 84
    h = 372
    top = y
    if top - h < 56:
        c.showPage()
        top = A4[1] - 60
    bottom = top - h
    code = str(bcbp or _bcbp(ci, bk))
    tags = []
    for bag in (bags or []):
        if isinstance(bag, dict):
            tag = (bag.get('tag_no') or bag.get('bag_tag_no') or bag.get('bag_tag')
                   or bag.get('tag') or bag.get('bag_number') or bag.get('tag_number')
                   or bag.get('id'))
        else:
            tag = bag
        if tag:
            tags.append(str(tag))
    if not tags:
        raw = ci.get('bag_tags') or bk.get('bag_tags') or ''
        if isinstance(raw, (list, tuple)):
            tags = [str(t) for t in raw if t]
        elif raw:
            tags = [p.strip() for p in str(raw).replace(';', ',').split(',') if p.strip()]
    try:
        seq = int(ci.get('id') or ci.get('checkin_id') or 0)
    except Exception:
        seq = 0
    seq = max(0, seq)
    try:
        bag_count = int(ci.get('bag_count') or bk.get('bag_count') or 0)
    except Exception:
        bag_count = 0
    bag_count = max(0, bag_count)
    fields = [
        ('PASSENGER', _iata_name(ci.get('passenger_name') or bk.get('passenger_name'))),
        ('PNR', bk.get('pnr') or ci.get('pnr')),
        ('FLIGHT', ci.get('flight_no') or bk.get('flight_no')),
        ('DATE', ci.get('flight_date') or bk.get('flight_date')),
        ('SEAT', ci.get('seat_no') or bk.get('seat_no')),
        ('GATE', ci.get('gate') or bk.get('gate')),
        ('TERMINAL', ci.get('terminal') or bk.get('terminal')),
        ('BOARDING', ci.get('dep_time') or bk.get('dep_time')),
        ('CLASS', bk.get('fare_class') or bk.get('cabin_class') or ci.get('cabin_class') or 'Y'),
        ('SEQ', f"{seq:05d}"[-5:]),
        ('BAGS', bag_count or len(tags) or 0),
        ('TICKET', bk.get('ticket_number')),
    ]
    c.setFillColor(colors.white)
    c.setStrokeColor(accent)
    c.setLineWidth(1.2)
    c.roundRect(x, bottom, w, h, 10, stroke=1, fill=1)
    c.setFillColor(primary)
    c.roundRect(x, top - 48, w, 48, 10, stroke=0, fill=1)
    c.rect(x, top - 54, w, 8, stroke=0, fill=1)
    c.setFillColor(accent)
    c.rect(x, top - 54, w, 4, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 17)
    c.drawString(x + 18, top - 24, 'BOARDING PASS')
    c.setFont('Helvetica', 10)
    c.drawRightString(x + w - 18, top - 24, f"FLIGHT {ci.get('flight_no') or bk.get('flight_no') or '-'}")
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 28)
    c.drawString(x + 18, top - 88, str(ci.get('origin') or bk.get('origin') or '-'))
    c.setFillColor(accent)
    c.drawString(x + 128, top - 88, '>')
    c.setFillColor(primary)
    c.drawString(x + 164, top - 88, str(ci.get('destination') or bk.get('destination') or '-'))
    c.setFillColor(muted)
    c.setFont('Helvetica', 9)
    c.drawString(x + 18, top - 106, f"{ci.get('flight_date') or bk.get('flight_date') or ''}   {ci.get('dep_time') or bk.get('dep_time') or ''}")
    col_w = (w - 150) / 2
    for idx, (label, value) in enumerate(fields):
        col, row = idx % 2, idx // 2
        fx = x + 18 + col * col_w
        fy = top - 140 - row * 26
        c.setFillColor(muted)
        c.setFont('Helvetica-Bold', 7.5)
        c.drawString(fx, fy, str(label))
        c.setFillColor(ink)
        c.setFont('Helvetica-Bold', 10.5)
        c.drawString(fx, fy - 12, _fit(str(value if value not in (None, '') else '-'), 'Helvetica-Bold', 10.5, col_w - 12, c))
    c.setStrokeColor(accent)
    c.setDash(4, 3)
    c.line(x + 12, top - 292, x + w - 12, top - 292)
    c.setDash()
    qr_size = 74
    qr_x = x + w - qr_size - 14
    _qr(c, code, qr_x, bottom + 8, qr_size)
    # Linear barcode (Code 128) drawn vertically on the right side to avoid
    # overlapping the BCBP/security text block on the left.
    c.saveState()
    c.translate(x + w - 30, bottom + 8)
    c.rotate(90)
    _barcode128(c, code, 0, 0, width=qr_size, height=22)
    c.restoreState()
    code_w = w - qr_size - 46
    c.setFillColor(muted)
    c.setFont('Helvetica-Bold', 7.5)
    c.drawString(x + 18, bottom + 70, 'BCBP / SECURITY CODE')
    c.setFillColor(ink)
    c.setFont('Helvetica', 6.5)
    c.drawString(x + 18, bottom + 60, _fit(code, 'Helvetica', 6.5, code_w, c))
    c.setFillColor(muted)
    c.setFont('Helvetica', 7)
    c.drawString(x + 18, bottom + 44, _fit(str(payload or ''), 'Helvetica', 7, code_w, c))
    c.setFont('Helvetica-Bold', 7.5)
    c.drawString(x + 18, bottom + 28, _fit('BAGS ' + (', '.join(tags[:8]) or str(bag_count)), 'Helvetica-Bold', 7.5, code_w, c))
    c.setFont('Helvetica', 7)
    c.drawString(x + 18, bottom + 14, 'Present the QR code at security and the boarding gate.')
    return bottom - 18


def _baggage_tag(c, y, branding, ci, bk, bag, payload):
    width, _ = A4
    primary, accent, tint, ink, muted = _brand(branding)
    ci = ci or {}
    bk = bk or {}
    bag = bag or {}
    x, w = 42, width - 84
    h = 340
    top = y
    if top - h < 56:
        c.showPage()
        top = A4[1] - 60
    bottom = top - h
    tag = _bag_tag_no(bag) or '-'
    origin = ci.get('origin') or bk.get('origin') or bag.get('origin') or '-'
    destination = ci.get('destination') or bk.get('destination') or bag.get('destination') or '-'
    weight = next((bag.get(k) for k in ('weight_kg', 'weight') if bag.get(k) not in (None, '')),
                  bk.get('baggage_allowance_kg') if bk.get('baggage_allowance_kg') not in (None, '') else 23)
    col_w = (w - 60) / 2
    c.setFillColor(colors.white)
    c.setStrokeColor(accent)
    c.setLineWidth(1.2)
    c.roundRect(x, bottom, w, h, 10, stroke=1, fill=1)
    c.setFillColor(primary)
    c.roundRect(x, top - 46, w, 46, 10, stroke=0, fill=1)
    c.rect(x, top - 50, w, 6, stroke=0, fill=1)
    c.setFillColor(accent)
    c.rect(x, top - 50, w, 3, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 16)
    c.drawString(x + 18, top - 22, 'BAGGAGE TAG')
    c.setFont('Helvetica-Bold', 9)
    c.drawRightString(x + w - 18, top - 22, _fit(branding.get('company_name') or 'Fayaka Airline', 'Helvetica-Bold', 9, 180, c))
    c.setFont('Helvetica', 7.5)
    c.drawRightString(x + w - 18, top - 36, 'IATA BAGGAGE IDENTIFICATION')
    c.setFillColor(muted)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(x + 18, top - 68, 'BAG TAG NUMBER')
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 28)
    c.drawString(x + 18, top - 102, _fit(tag, 'Helvetica-Bold', 28, w - 36, c))
    _barcode128(c, tag, x + 18, top - 146, w - 36, 36)
    c.setStrokeColor(accent)
    c.setDash(4, 3)
    c.line(x + 12, top - 158, x + w - 12, top - 158)
    c.setDash()
    fields = [
        ('PASSENGER', _iata_name(ci.get('passenger_name') or bk.get('passenger_name') or bag.get('passenger_name'))),
        ('PNR', bk.get('pnr') or ci.get('pnr') or bag.get('pnr')),
        ('FLIGHT', ci.get('flight_no') or bk.get('flight_no') or bag.get('flight_no')),
        ('ROUTE', f"{origin} - {destination}"),
        ('DATE', ci.get('flight_date') or bk.get('flight_date') or bag.get('flight_date')),
        ('SEAT', ci.get('seat_no') or bk.get('seat_no') or bag.get('seat_no')),
        ('CLASS', bk.get('fare_class') or bk.get('cabin_class') or bag.get('cabin_class') or 'Y'),
        ('WEIGHT', f"{weight} kg"),
    ]
    for idx, (label, value) in enumerate(fields):
        col, row = idx % 2, idx // 2
        fx = x + 18 + col * col_w
        fy = top - 178 - row * 24
        c.setFillColor(muted)
        c.setFont('Helvetica-Bold', 7.5)
        c.drawString(fx, fy, str(label))
        c.setFillColor(ink)
        c.setFont('Helvetica-Bold', 10.5)
        c.drawString(fx, fy - 11, _fit(str(value if value not in (None, '') else '-'), 'Helvetica-Bold', 10.5, col_w - 12, c))
    c.setFillColor(tint)
    c.setStrokeColor(accent)
    c.setLineWidth(0.8)
    c.roundRect(x + 18, bottom + 10, w - 110, 52, 6, stroke=1, fill=1)
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(x + 26, bottom + 50, 'HANDLING INSTRUCTIONS')
    c.setFillColor(ink)
    c.setFont('Helvetica', 8)
    note = str(bag.get('handling_instructions') or 'Handle with care. Do not expose to rain. Retain this tag until baggage claim.')
    for i, line in enumerate(_lines(note, 72)[:2]):
        c.drawString(x + 26, bottom + 38 - i * 11, line)
    c.setFillColor(muted)
    c.setFont('Helvetica-Bold', 7.5)
    c.drawString(x + 26, bottom + 14, f"DESTINATION {destination}")
    _qr(c, payload, x + w - 72, bottom + 10, 56)
    return bottom - 18


def _uplift_coupon(c, y, branding, ci, bk, bag, sale, payload):
    width, _ = A4
    primary, accent, tint, ink, muted = _brand(branding)
    ci = ci or {}
    bk = bk or {}
    bag = bag or {}
    sale = sale or {}
    x, w = 42, width - 84
    h = 352
    top = y
    if top - h < 56:
        c.showPage()
        top = A4[1] - 60
    bottom = top - h
    tag = _bag_tag_no(bag) or '-'
    sale_no = str(sale.get('sale_no') or ci.get('checkin_no') or 'UPLIFT')
    amount = sale.get('amount') if sale.get('amount') not in (None, '') else 0
    cur = sale.get('currency') or bk.get('currency') or 'USD'
    desc = str(sale.get('description') or '')
    excess = ''
    if 'excess ' in desc:
        excess = desc.split('excess ', 1)[-1].split('kg', 1)[0].strip() + ' kg'
    weight = next((bag.get(k) for k in ('weight_kg', 'weight') if bag.get(k) not in (None, '')),
                  bk.get('baggage_allowance_kg') if bk.get('baggage_allowance_kg') not in (None, '') else 23)
    origin = ci.get('origin') or bk.get('origin') or '-'
    destination = ci.get('destination') or bk.get('destination') or '-'
    c.setFillColor(colors.white)
    c.setStrokeColor(accent)
    c.setLineWidth(1.2)
    c.roundRect(x, bottom, w, h, 10, stroke=1, fill=1)
    c.setFillColor(primary)
    c.roundRect(x, top - 48, w, 48, 10, stroke=0, fill=1)
    c.rect(x, top - 54, w, 8, stroke=0, fill=1)
    c.setFillColor(accent)
    c.rect(x, top - 54, w, 4, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 17)
    c.drawString(x + 18, top - 22, 'UPLIFT COUPON')
    c.setFont('Helvetica', 10)
    c.drawRightString(x + w - 18, top - 22, f"EXCESS BAGGAGE · {sale_no}")
    c.setFillColor(tint)
    c.roundRect(x + 18, top - 100, w - 36, 42, 8, stroke=0, fill=1)
    c.setFillColor(muted)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(x + 30, top - 74, 'AMOUNT PAYABLE')
    c.setFillColor(primary)
    c.setFont('Helvetica-Bold', 22)
    c.drawRightString(x + w - 30, top - 80, _money(amount, cur))
    fields = [
        ('PASSENGER', _iata_name(ci.get('passenger_name') or bk.get('passenger_name'))),
        ('PNR', bk.get('pnr') or ci.get('pnr')),
        ('FLIGHT', ci.get('flight_no') or bk.get('flight_no')),
        ('ROUTE', f"{origin} - {destination}"),
        ('DATE', ci.get('flight_date') or bk.get('flight_date')),
        ('SEAT', ci.get('seat_no') or bk.get('seat_no')),
        ('BAG TAG', tag),
        ('WEIGHT', f"{weight} kg"),
        ('EXCESS', excess or '-'),
        ('PIECES', sale.get('quantity') or ci.get('bag_count') or 1),
        ('STATUS', str(sale.get('payment_status') or 'pending').upper()),
        ('CLASS', bk.get('fare_class') or bk.get('cabin_class') or 'Y'),
    ]
    col_w = (w - 60) / 2
    for idx, (label, value) in enumerate(fields):
        col, row = idx % 2, idx // 2
        fx = x + 18 + col * col_w
        fy = top - 112 - row * 26
        c.setFillColor(muted)
        c.setFont('Helvetica-Bold', 7.5)
        c.drawString(fx, fy, str(label))
        c.setFillColor(ink)
        c.setFont('Helvetica-Bold', 10.5)
        c.drawString(fx, fy - 13, _fit(str(value if value not in (None, '') else '-'), 'Helvetica-Bold', 10.5, col_w - 12, c))
    c.setStrokeColor(accent)
    c.setDash(4, 3)
    c.line(x + 12, top - 266, x + w - 12, top - 266)
    c.setDash()
    _barcode128(c, sale_no, x + 18, bottom + 42, w - 140, 28)
    _qr(c, payload, x + w - 74, bottom + 12, 56)
    c.setFillColor(muted)
    c.setFont('Helvetica', 7.5)
    c.drawString(x + 18, bottom + 30, _fit('Present this coupon at the baggage drop counter. Charges are final after payment.', 'Helvetica', 7.5, w - 140, c))
    c.drawString(x + 18, bottom + 14, _fit(f"Reference: {sale_no} · {desc[:72]}", 'Helvetica', 7.5, w - 140, c))
    return bottom - 18


def build_pdf(kind, context, branding):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    kind_title = {'invoice': 'Invoice', 'booking': 'Booking Confirmation', 'bill': 'Supplier Bill', 'traveler': 'Traveler Statement', 'receipt': 'Payment Receipt', 'estimate': 'Estimate', 'credit_note': 'Credit Note', 'eticket': 'Electronic Ticket', 'boardingpass': 'Boarding Pass', 'journeypass': 'Travel Itinerary', 'baggagetag': 'Baggage Tag', 'upliftcoupon': 'Uplift Coupon'}.get(kind, 'Customer Statement')
    company = str((branding or {}).get('company_name') or 'Fayaka Airline')
    c.setTitle(f"{kind_title} - {company}")
    c.setAuthor(company)
    branding = branding or {}
    subtitle = ''
    qr_payload = _verify_payload(kind, context, branding)

    if kind == 'invoice':
        inv = context.get('invoice') or {}
        subtitle = str(inv.get('invoice_no') or '')
        cur = inv.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Invoice', subtitle)
        y = _facts(c, y, [
            ('Invoice No', inv.get('invoice_no')),
            ('Issue Date', inv.get('issue_date')),
            ('Due Date', inv.get('due_date')),
            ('Customer', (context.get('customer') or {}).get('name')),
            ('Booking', (context.get('order') or {}).get('pnr') or inv.get('pnr')),
            ('Status', str(inv.get('status') or '').replace('_', ' ').title()),
        ], branding, 'Invoice', subtitle)
        y = _section(c, y, 'Invoice Lines', context.get('lines') or [], [('description', 0.52), ('qty', 0.10), ('unit_price', 0.16), ('line_total', 0.22)], branding, 'Invoice', subtitle)
        y = _totals_box(c, y, [
            ('Subtotal', _money(inv.get('subtotal') or inv.get('total'), cur), False),
            ('Tax', _money(inv.get('tax'), cur), False),
            ('Total', _money(inv.get('total'), cur), True),
            ('Paid', _money(inv.get('amount_paid'), cur), False),
            ('Balance Due', _money(inv.get('balance'), cur), True),
        ], branding, 'Invoice', subtitle)

    elif kind == 'booking':
        order = context.get('order') or {}
        subtitle = str(order.get('pnr') or order.get('order_number') or '')
        cur = order.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Booking Confirmation', subtitle)
        y = _facts(c, y, [
            ('PNR / Order', order.get('pnr') or order.get('order_number')),
            ('Customer', (context.get('customer') or {}).get('name')),
            ('Route', f"{order.get('origin') or '-'} - {order.get('destination') or '-'}"),
            ('Travel Dates', f"{order.get('travel_start') or '-'} to {order.get('travel_end') or '-'}"),
            ('Ticket Status', str(order.get('ticket_status') or '').replace('_', ' ').title()),
            ('Ticketing Deadline', order.get('tktl')),
        ], branding, 'Booking Confirmation', subtitle)
        y = _section(c, y, 'Itinerary', context.get('segments') or [], [('carrier', 0.14), ('flight_no', 0.14), ('origin', 0.14), ('destination', 0.14), ('depart_at', 0.22), ('arrive_at', 0.22)], branding, 'Booking Confirmation', subtitle)
        y = _section(c, y, 'Passengers', context.get('passengers') or [], [('full_name', 0.45), ('traveler_type', 0.15), ('ticket_numbers', 0.40)], branding, 'Booking Confirmation', subtitle)
        y = _totals_box(c, y, [
            ('Total Sell', _money(order.get('total_sell'), cur), True),
            ('Total Cost', _money(order.get('total_cost'), cur), False),
            ('Balance Due', _money(order.get('balance_due'), cur), True),
        ], branding, 'Booking Confirmation', subtitle)

    elif kind == 'bill':
        bill = context.get('bill') or {}
        subtitle = str(bill.get('bill_no') or '')
        cur = bill.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Supplier Bill', subtitle)
        y = _facts(c, y, [
            ('Bill No', bill.get('bill_no')),
            ('Supplier', bill.get('supplier')),
            ('Category', bill.get('category')),
            ('Bill Date', bill.get('bill_date')),
            ('Due Date', bill.get('due_date')),
            ('Status', str(bill.get('status') or '').replace('_', ' ').title()),
        ], branding, 'Supplier Bill', subtitle)
        if bill.get('description'):
            c.setFillColor(colors.HexColor('#0f2742'))
            c.setFont('Helvetica-Bold', 10)
            c.drawString(42, y, 'Description')
            y -= 14
            c.setFont('Helvetica', 9)
            c.setFillColor(colors.HexColor('#132238'))
            for line in _lines(bill.get('description') or '', 105)[:12]:
                if y < 70:
                    y = _new_page(c, branding, 'Supplier Bill', subtitle)
                c.drawString(42, y, line)
                y -= 13
            y -= 6
        y = _totals_box(c, y, [
            ('Total', _money(bill.get('total'), cur), True),
            ('Paid', _money(bill.get('amount_paid'), cur), False),
            ('Balance Due', _money(bill.get('balance'), cur), True),
        ], branding, 'Supplier Bill', subtitle)

    elif kind == 'traveler':
        traveler = context.get('traveler') or {}
        summary = context.get('summary') or {}
        name = traveler.get('display_name') or traveler.get('full_name') or ''
        subtitle = name
        cur = branding.get('currency') or 'USD'
        y = _header(c, branding, 'Traveler Statement', subtitle)
        y = _facts(c, y, [
            ('Traveler', name),
            ('Document', f"{traveler.get('doc_type') or 'Passport'} {traveler.get('doc_no') or ''}".strip()),
            ('Nationality', traveler.get('nationality')),
            ('Email', traveler.get('email')),
            ('Phone', traveler.get('phone') or traveler.get('mobile')),
            ('Period', f"{context.get('date_from') or 'All'} to {context.get('date_to') or 'Today'}"),
            ('Travel Status', str(traveler.get('travel_status') or '').replace('_', ' ').title()),
        ], branding, 'Traveler Statement', subtitle)
        y = _section(c, y, 'Bookings', context.get('orders') or [], [('product_type', 0.14), ('pnr', 0.14), ('origin', 0.10), ('destination', 0.10), ('travel_start', 0.17), ('total_sell', 0.17), ('ticket_status', 0.18)], branding, 'Traveler Statement', subtitle)
        y = _section(c, y, 'Invoices', context.get('invoices') or [], [('invoice_no', 0.25), ('issue_date', 0.20), ('total', 0.18), ('amount_paid', 0.18), ('balance', 0.19)], branding, 'Traveler Statement', subtitle)
        y = _totals_box(c, y, [
            ('Bookings', str(summary.get('bookings', 0)), False),
            ('Sales', _money(summary.get('sell'), cur), True),
            ('Margin', _money(summary.get('margin'), cur), False),
            ('Invoiced', _money(summary.get('invoiced'), cur), False),
            ('Paid', _money(summary.get('paid'), cur), True),
        ], branding, 'Traveler Statement', subtitle)

    elif kind == 'receipt':
        pay = context.get('payment') or {}
        inv = context.get('invoice') or {}
        customer = context.get('customer') or {}
        subtitle = str(pay.get('payment_no') or '')
        cur = pay.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Payment Receipt', subtitle)
        y = _facts(c, y, [
            ('Receipt No', pay.get('payment_no')),
            ('Date', pay.get('paid_at')),
            ('Customer', customer.get('name')),
            ('Invoice', inv.get('invoice_no')),
            ('Booking', (context.get('order') or {}).get('pnr')),
            ('Method', str(pay.get('method') or '').replace('_', ' ').title()),
            ('Reference', pay.get('reference')),
            ('Status', str(pay.get('status') or '').replace('_', ' ').title()),
        ], branding, 'Payment Receipt', subtitle)
        y = _totals_box(c, y, [
            ('Amount Received', _money(pay.get('amount'), cur), True),
            ('Invoice Total', _money(inv.get('total'), cur), False),
            ('Invoice Balance', _money(inv.get('balance'), cur), True),
        ], branding, 'Payment Receipt', subtitle)

    elif kind == 'estimate':
        est = context.get('estimate') or {}
        customer = context.get('customer') or {}
        subtitle = str(est.get('estimate_no') or '')
        cur = est.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Estimate', subtitle)
        y = _facts(c, y, [
            ('Estimate No', est.get('estimate_no')),
            ('Title', est.get('title')),
            ('Customer', customer.get('name')),
            ('Issue Date', (est.get('created_at') or '')[:10]),
            ('Valid Until', est.get('valid_until')),
            ('Status', str(est.get('status') or '').replace('_', ' ').title()),
        ], branding, 'Estimate', subtitle)
        if est.get('description'):
            c.setFillColor(colors.HexColor('#0f2742'))
            c.setFont('Helvetica-Bold', 10)
            c.drawString(42, y, 'Description')
            y -= 14
            c.setFont('Helvetica', 9)
            c.setFillColor(colors.HexColor('#132238'))
            for line in _lines(est.get('description') or '', 105)[:14]:
                if y < 70:
                    y = _new_page(c, branding, 'Estimate', subtitle)
                c.drawString(42, y, line)
                y -= 13
            y -= 6
        y = _section(c, y, 'Quoted Items', context.get('lines') or [{'description': est.get('title') or 'Travel services', 'qty': 1, 'unit_price': est.get('subtotal') or est.get('total') or 0, 'line_total': est.get('total') or 0}], [('description', 0.52), ('qty', 0.10), ('unit_price', 0.16), ('line_total', 0.22)], branding, 'Estimate', subtitle)
        y = _totals_box(c, y, [
            ('Subtotal', _money(est.get('subtotal'), cur), False),
            ('Tax', _money(est.get('tax'), cur), False),
            ('Total', _money(est.get('total'), cur), True),
        ], branding, 'Estimate', subtitle)

    elif kind == 'credit_note':
        note = context.get('credit_note') or context.get('note') or {}
        customer = context.get('customer') or {}
        inv = context.get('invoice') or {}
        subtitle = str(note.get('credit_note_no') or '')
        cur = note.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Credit Note', subtitle)
        y = _facts(c, y, [
            ('Credit Note No', note.get('credit_note_no')),
            ('Issue Date', note.get('issue_date')),
            ('Customer', customer.get('name')),
            ('Invoice', inv.get('invoice_no')),
            ('Reason', str(note.get('reason') or '').replace('_', ' ').title()),
            ('Status', str(note.get('status') or '').replace('_', ' ').title()),
        ], branding, 'Credit Note', subtitle)
        if note.get('description'):
            c.setFillColor(colors.HexColor('#0f2742'))
            c.setFont('Helvetica-Bold', 10)
            c.drawString(42, y, 'Description')
            y -= 14
            c.setFont('Helvetica', 9)
            c.setFillColor(colors.HexColor('#132238'))
            for line in _lines(note.get('description') or '', 105)[:14]:
                if y < 70:
                    y = _new_page(c, branding, 'Credit Note', subtitle)
                c.drawString(42, y, line)
                y -= 13
            y -= 6
        y = _totals_box(c, y, [
            ('Credit Amount', _money(note.get('amount'), cur), True),
        ], branding, 'Credit Note', subtitle)

    elif kind in ('eticket', 'boardingpass'):
        ci = context.get('checkin') or context.get('pss_checkin') or {}
        bk = context.get('pss_booking') or context.get('booking') or {}
        if kind == 'boardingpass':
            subtitle = f"{ci.get('checkin_no') or bk.get('pnr') or ''}  |  {str(bk.get('ticket_status') or '').upper()}"
            y = _header(c, branding, 'Boarding Pass', subtitle)
            bags = context.get('baggage') or []
            y = _boarding_card(c, y, branding, ci, bk, qr_payload, bags=bags, bcbp=_bcbp(ci, bk))
        else:
            subtitle = f"{bk.get('pnr') or bk.get('ticket_number') or ''}  |  {str(bk.get('ticket_status') or '').upper()}"
            y = _header(c, branding, 'E-Ticket', subtitle)
            cur = bk.get('currency') or branding.get('currency') or 'USD'
            y = _facts(c, y, [
                ('Passenger (IATA)', _iata_name(bk.get('passenger_name'))),
                ('PNR / locator', bk.get('pnr')),
                ('Ticket number', bk.get('ticket_number')),
                ('Flight', bk.get('flight_no')),
                ('Route', f"{bk.get('origin') or '-'} - {bk.get('destination') or '-'}"),
                ('Flight date', bk.get('flight_date')),
                ('Departure', f"{bk.get('dep_time') or '-'} / {bk.get('arr_time') or '-'}"),
                ('Class / fare basis', f"{bk.get('fare_class') or 'Y'} / {bk.get('fare_basis') or 'Y'}"),
                ('Baggage allowance', f"{bk.get('baggage_allowance_kg') or 23} kg"),
                ('Issuing office', bk.get('issued_by') or branding.get('company_name')),
                ('Issue date', bk.get('issue_date') or ''),
                ('Seat', bk.get('seat_no')),
                ('Channel', bk.get('channel_name')),
                ('Airport', bk.get('airport_code')),
                ('Ticket status', str(bk.get('ticket_status') or '').replace('_', ' ').upper()),
            ], branding, 'E-Ticket', subtitle)
            tax_lines = context.get('tax_lines') or []
            if tax_lines:
                tax_rows = [{'code': t.get('tax_code'), 'description': t.get('description'),
                             'amount': _money(t.get('amount'), t.get('currency') or cur)} for t in tax_lines]
                y = _section(c, y, 'Taxes & surcharges (IATA codes)', tax_rows,
                             [('code', 0.18), ('description', 0.52), ('amount', 0.30)], branding, 'E-Ticket', subtitle)
            y = _totals_box(c, y, [('Fare', _money(bk.get('fare_amount'), cur), False),
                                   ('Taxes / surcharges', _money(bk.get('taxes'), cur), False),
                                   ('Service fee', _money(bk.get('service_fee'), cur), False),
                                   ('Commission', _money(bk.get('commission'), cur), False),
                                   ('Total', _money(bk.get('total_amount'), cur), True),
                                   ('Payment status', str(bk.get('payment_status') or '').title(), False)],
                            branding, 'E-Ticket', subtitle)
            payments = context.get('payments') or []
            if payments:
                pay_rows = [{'payment_no': p.get('payment_no'), 'paid_at': (p.get('paid_at') or p.get('created_at') or '')[:10],
                             'method': str(p.get('method') or '').replace('_', ' ').title(),
                             'amount': _money(p.get('amount'), p.get('currency') or cur),
                             'status': str(p.get('status') or '').title()} for p in payments]
                y = _section(c, y, 'Payments', pay_rows,
                             [('payment_no', 0.22), ('paid_at', 0.18), ('method', 0.20), ('amount', 0.22), ('status', 0.18)],
                             branding, 'E-Ticket', subtitle)

            y = _qr_panel(c, y, branding, 'E-ticket verification', qr_payload,
                          'Present this code at check-in or to your travel agency')

    elif kind == 'journeypass':
        bk = context.get('pss_booking') or {}
        subtitle = f"{bk.get('pnr') or bk.get('ticket_number') or ''}  |  {str(bk.get('ticket_status') or '').upper()}"
        cur = bk.get('currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Travel Itinerary', subtitle)
        y = _facts(c, y, [
            ('Passenger', bk.get('passenger_name')),
            ('PNR', bk.get('pnr')),
            ('E-ticket', bk.get('ticket_number')),
            ('Flight', bk.get('flight_no')),
            ('Route', f"{bk.get('origin') or '-'} �+' {bk.get('destination') or '-'}"),
            ('Date', bk.get('flight_date')),
            ('Departure / arrival', f"{bk.get('dep_time') or '-'} / {bk.get('arr_time') or '-'}"),
            ('Seat / class', f"{bk.get('seat_no') or '-'} / {bk.get('fare_class') or '-'}"),
            ('Payment status', str(bk.get('payment_status') or '').title()),
        ], branding, 'Travel Itinerary', subtitle)
        payments = context.get('payments') or []
        if payments:
            cur = bk.get('currency') or branding.get('currency') or 'USD'
            pay_rows = [{'payment_no': p.get('payment_no'), 'paid_at': (p.get('paid_at') or p.get('created_at') or '')[:10],
                         'method': str(p.get('method') or '').replace('_', ' ').title(),
                         'amount': _money(p.get('amount'), p.get('currency') or cur),
                         'status': str(p.get('status') or '').title()} for p in payments]
            y = _section(c, y, 'Payments', pay_rows,
                         [('payment_no', 0.22), ('paid_at', 0.18), ('method', 0.20), ('amount', 0.22), ('status', 0.18)],
                         branding, 'Travel Itinerary', subtitle)
        y = _section(c, y, 'Ancillary services', context.get('ancillaries') or [],
                     [('sale_no', 0.24), ('description', 0.42), ('quantity', 0.10), ('amount', 0.24)],
                     branding, 'Travel Itinerary', subtitle)
        y = _section(c, y, 'Baggage allowance', context.get('baggage') or [],
                     [('cabin_class', 0.20), ('free_kg', 0.22), ('excess_rate_per_kg', 0.28), ('max_kg', 0.30)],
                     branding, 'Travel Itinerary', subtitle)
        y = _section(c, y, 'Airport documents & requirements', context.get('airport_docs') or [],
                     [('airport_code', 0.16), ('doc_type', 0.20), ('title', 0.28), ('requirement', 0.36)],
                     branding, 'Travel Itinerary', subtitle)
        y = _section(c, y, 'Weather advisory', context.get('weather') or [],
                     [('airport_code', 0.18), ('condition', 0.24), ('temperature_c', 0.20), ('advisory', 0.38)],
                     branding, 'Travel Itinerary', subtitle)
        y = _totals_box(c, y, [('Fare', _money(bk.get('fare_amount'), cur), False),
                               ('Taxes', _money(bk.get('taxes'), cur), False),
                               ('Total', _money(bk.get('total_amount'), cur), True)],
                        branding, 'Travel Itinerary', subtitle)
        y = _qr_panel(c, y, branding, 'Travel itinerary verification', qr_payload,
                      'Scan to verify booking, ticket and travel details')

    elif kind == 'baggagetag':
        ci = context.get('checkin') or context.get('pss_checkin') or {}
        bk = context.get('pss_booking') or context.get('booking') or {}
        bags = context.get('baggage') or []
        idx = 0
        try:
            idx = int(context.get('bag_index') or 0)
        except Exception:
            idx = 0
        bag = None
        if bags:
            try:
                bag = bags[idx] if 0 <= idx < len(bags) else bags[0]
            except Exception:
                bag = bags[0]
        if not bag:
            tags = ci.get('bag_tags') or ''
            if isinstance(tags, (list, tuple)):
                tags = tags[0] if tags else ''
            else:
                tags = str(tags).replace(';', ',').split(',')[0].strip()
            bag = {'tag_no': tags}
        subtitle = f"{_bag_tag_no(bag) or ''}  |  {bk.get('pnr') or ''}".strip(' |')
        y = _header(c, branding, 'Baggage Tag', subtitle)
        y = _baggage_tag(c, y, branding, ci, bk, bag, qr_payload)

    elif kind == 'upliftcoupon':
        ci = context.get('checkin') or context.get('pss_checkin') or {}
        bk = context.get('pss_booking') or context.get('booking') or {}
        sale = context.get('uplift_sale') or {}
        bag = _selected_bag(context) or {}
        subtitle = f"{sale.get('sale_no') or ci.get('checkin_no') or bk.get('pnr') or ''}  |  {bk.get('pnr') or ''}".strip(' |')
        y = _header(c, branding, 'Uplift Coupon', subtitle)
        y = _uplift_coupon(c, y, branding, ci, bk, bag, sale, qr_payload)

    else:
        customer = context.get('customer') or {}
        subtitle = str(customer.get('name') or '')
        cur = context.get('branding_currency') or branding.get('currency') or 'USD'
        y = _header(c, branding, 'Customer Statement', subtitle)
        y = _facts(c, y, [
            ('Customer', customer.get('name')),
            ('Type', str(customer.get('type') or '').title()),
            ('Company', customer.get('company')),
            ('Email', customer.get('email')),
            ('Phone', customer.get('phone')),
            ('Bookings', len(context.get('orders') or [])),
            ('Lifetime Spend', _money(customer.get('spend'), cur)),
        ], branding, 'Customer Statement', subtitle)
        y = _section(c, y, 'Bookings', context.get('orders') or [], [('pnr', 0.22), ('origin', 0.12), ('destination', 0.12), ('travel_start', 0.18), ('total_sell', 0.18), ('ticket_status', 0.18)], branding, 'Customer Statement', subtitle)
        y = _section(c, y, 'Invoices', context.get('invoices') or [], [('invoice_no', 0.28), ('issue_date', 0.18), ('total', 0.18), ('amount_paid', 0.18), ('balance', 0.18)], branding, 'Customer Statement', subtitle)
        y = _section(c, y, 'Documents', context.get('documents') or [], [('title', 0.42), ('category', 0.22), ('file_name', 0.36)], branding, 'Customer Statement', subtitle)

    if kind not in ('boardingpass', 'eticket', 'journeypass', 'baggagetag', 'upliftcoupon'):
        _qr_panel(c, y, branding, 'Document verification', qr_payload,
                  'Scan to verify the reference and authenticity of this document')
    _footer(c, branding)
    c.save()
    return buf.getvalue()


def branding_from_row(row):
    row = dict(row or {})
    settings = {}
    if row.get('settings_json'):
        try:
            settings = json.loads(row['settings_json'])
        except Exception:
            settings = {}
    company = row.get('company_name') or row.get('a_company_name') or settings.get('branding') or row.get('tenant_name') or 'Fayaka Airline'
    addr_parts = [p for p in [row.get('address') or row.get('a_address'), row.get('city') or row.get('a_city'), row.get('state') or row.get('a_state'), row.get('postal_code') or row.get('a_postal'), row.get('country') or row.get('a_country')] if p]
    address = ', '.join(str(p) for p in addr_parts) if addr_parts else (row.get('address') or row.get('a_address') or '')
    location = row.get('location') or address or (row.get('city') or row.get('a_city') or '')
    return {
        'company_name': company,
        'legal_name': row.get('legal_name') or row.get('a_legal_name') or '',
        'location': location,
        'address': address,
        'phone': row.get('phone') or row.get('a_phone') or '',
        'email': row.get('email') or row.get('a_email') or '',
        'website': row.get('website') or row.get('a_website') or '',
        'tax_id': row.get('tax_id') or row.get('a_tax_id') or '',
        'logo_text': row.get('logo_text') or (company[:3].upper() if company else 'TE'),
        'logo_data': row.get('logo_data') or '',
        'primary_color': row.get('primary_color') or '#0f2742',
        'accent_color': row.get('accent_color') or '#0b6efd',
        'footer_note': row.get('footer_note') or 'Fayaka Airline - Confidential',
        'currency': row.get('a_currency') or settings.get('default_currency') or 'USD',
    }
