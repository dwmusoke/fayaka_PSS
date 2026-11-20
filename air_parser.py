import hashlib
import os
import re
from datetime import datetime


MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def clean_text(raw_bytes):
    if raw_bytes is None:
        return ""
    if isinstance(raw_bytes, str):
        t = raw_bytes
    else:
        # strip leading null padding seen in some files
        b = raw_bytes.lstrip(b"\x00")
        try:
            t = b.decode("utf-8")
        except Exception:
            t = b.decode("latin-1", errors="ignore")
    # normalize newlines
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return t


def _norm_year(yi):
    if yi is None:
        return None
    return yi if yi >= 100 else (2000 + yi)


def parse_date_token(tok, year_hint=None):
    # supports DDMMM, DDMMMYY, DDMMMYYYY, DDMMMHHMM, DDMMMYYHHMM
    if not tok:
        return None, None
    s = tok.strip().upper()
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})(\d{4})$', s)  # DDMMMYYHHMM
    if m:
        d, mon_s, yy, hhmm = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        mon = MONTHS.get(mon_s)
        if not mon: return None, None
        year = _norm_year(yy)
        try:
            date_iso = datetime(year, mon, d).date().isoformat()
        except Exception:
            return None, None
        return date_iso, f"{hhmm[:2]}:{hhmm[2:]}"
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{4})(\d{4})$', s)  # DDMMMYYYYHHMM (rare)
    if m:
        d, mon_s, yyyy, hhmm = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        mon = MONTHS.get(mon_s)
        if not mon: return None, None
        try:
            date_iso = datetime(yyyy, mon, d).date().isoformat()
        except Exception:
            return None, None
        return date_iso, f"{hhmm[:2]}:{hhmm[2:]}"
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{4})$', s)  # DDMMMYYYY or DDMMMHHMM
    if m:
        d, mon_s, suf = int(m.group(1)), m.group(2), int(m.group(3))
        mon = MONTHS.get(mon_s)
        if not mon: return None, None
        if suf > 2359:  # year
            year = suf
            try:
                return datetime(year, mon, d).date().isoformat(), None
            except Exception:
                return None, None
        else:  # HHMM
            hhmm = m.group(3)
            year = year_hint
            if year:
                try:
                    date_iso = datetime(year, mon, d).date().isoformat()
                except Exception:
                    date_iso = None
            else:
                date_iso = None
            return date_iso, f"{hhmm[:2]}:{hhmm[2:]}"
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})$', s)  # DDMMMYY
    if m:
        d, mon_s, yy = int(m.group(1)), m.group(2), int(m.group(3))
        mon = MONTHS.get(mon_s)
        if not mon: return None, None
        year = _norm_year(yy)
        try:
            return datetime(year, mon, d).date().isoformat(), None
        except Exception:
            return None, None
    m = re.match(r'^(\d{1,2})([A-Z]{3})$', s)  # DDMMM
    if m:
        d, mon_s = int(m.group(1)), m.group(2)
        mon = MONTHS.get(mon_s)
        if not mon: return None, None
        year = year_hint
        if year:
            try:
                return datetime(year, mon, d).date().isoformat(), None
            except Exception:
                return None, None
        return None, None
    return None, None


def parse_ddmon(s, year_hint=None):
    if not s:
        return None
    tok = s.strip().upper()
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{4})$', tok)
    if m:
        d, mon_s, y = int(m.group(1)), m.group(2), int(m.group(3))
        mon = MONTHS.get(mon_s)
        if not mon:
            return None
        if y <= 2359:
            # HHMM with no explicit year -> use hint
            if year_hint:
                try:
                    return datetime(year_hint, mon, d).date().isoformat()
                except Exception:
                    return None
            return None
        try:
            return datetime(y, mon, d).date().isoformat()
        except Exception:
            return None
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})$', tok)
    if m:
        d, mon_s, yy = int(m.group(1)), m.group(2), int(m.group(3))
        mon = MONTHS.get(mon_s)
        if not mon:
            return None
        year = 2000 + yy
        try:
            return datetime(year, mon, d).date().isoformat()
        except Exception:
            return None
    m = re.match(r'^(\d{1,2})([A-Z]{3})$', tok)
    if m and year_hint:
        d, mon_s = int(m.group(1)), m.group(2)
        mon = MONTHS.get(mon_s)
        if mon:
            try:
                return datetime(year_hint, mon, d).date().isoformat()
            except Exception:
                return None
    return None


def parse_ddmontime(s, year_hint=None):
    # compatibility wrapper
    d, t = parse_date_token(s, year_hint=year_hint)
    if d and t:
        return d, f"{d}T{t}"
    return d, t


def parse_segment(line, year_hint=None):
    # U-001X;002OEBB;ENTEBBE          ;NBO;NAIROBI KENYATTA ;KQ    0411 V V 18NOV0935 1050 18NOV;OK01;HK01;M ;0;73H;;;;;;ET;0115 ;N;;324;;1E
    parts = [p.strip() for p in line.split(';')]
    seg = {
        'seg_ref': parts[0] if parts else None,
        'carrier': None, 'flight_no': None, 'class_code': None,
        'origin': None, 'origin_name': None, 'destination': None, 'destination_name': None,
        'depart_at': None, 'arrive_at': None, 'depart_date': None, 'arrive_date': None,
        'status_code': None, 'status_pax': None, 'equipment': None, 'eticket': None,
        'is_void': 1 if 'VOID' in line.upper() else 0,
        'raw': line,
    }
    if len(parts) > 1:
        m = re.search(r'([A-Z]{3})$', parts[1])
        if m: seg['origin'] = m.group(1)
    if len(parts) > 2:
        seg['origin_name'] = parts[2] or None
    if len(parts) > 3:
        m = re.search(r'([A-Z]{3})$', parts[3])
        if m: seg['destination'] = m.group(1)
    if len(parts) > 4:
        seg['destination_name'] = parts[4] or None
    if len(parts) > 5:
        f = parts[5]
        if 'VOID' in f.upper():
            seg['is_void'] = 1
        m = re.match(r'([A-Z0-9]{2})\s*(\d+)?\s*([A-Z])?', f)
        if m:
            seg['carrier'] = m.group(1)
            seg['flight_no'] = m.group(2)
            seg['class_code'] = m.group(3)
        # date/time parsing: tokens like 18NOV0935, 18NOV26, 24OCT1520, 18NOV
        dtoks = re.findall(r'\b\d{1,2}[A-Z]{3}(?:\d{2}|\d{4})?(?:\d{4})?\b', f)
        local_year_hint = year_hint
        for t in dtoks:
            # find explicit year in segment
            m2 = re.match(r'^\d{1,2}[A-Z]{3}(\d{2})(?:\d{4})?$', t)
            if m2:
                local_year_hint = _norm_year(int(m2.group(1))); break
            m2 = re.match(r'^\d{1,2}[A-Z]{3}(\d{4})(?:\d{4})?$', t)
            if m2 and int(m2.group(1)) > 2359:
                local_year_hint = int(m2.group(1)); break
        # standalone 4-digit time tokens (not attached to date)
        all_times = re.findall(r'\b(\d{4})\b', f)
        date_entries = []
        for t in dtoks:
            d, tm = parse_date_token(t, year_hint=local_year_hint)
            date_entries.append({'tok': t, 'date': d, 'time': tm})
        # assign depart/arrive
        if date_entries:
            dep = date_entries[0]
            arr = date_entries[-1] if len(date_entries) > 1 else None
            seg['depart_date'] = dep['date']
            seg['arrive_date'] = (arr['date'] if arr and arr['date'] else dep['date'])
            dep_time = dep['time']
            arr_time = arr['time'] if arr else None
            # if dep had embedded time, separate arrival time token may exist
            if dep_time and not arr_time:
                # look for a standalone time token (e.g. 1050) different from embedded
                embedded = None
                m_emb = re.search(r'\d{1,2}[A-Z]{3}(?:\d{2})?(\d{4})$', dep['tok'])
                if m_emb and int(m_emb.group(1)) <= 2359:
                    embedded = m_emb.group(1)
                for tt in all_times:
                    if 0 <= int(tt[:2]) <= 23 and 0 <= int(tt[2:]) <= 59 and tt != embedded:
                        arr_time = f"{tt[:2]}:{tt[2:]}"
                        break
            if not dep_time and not arr_time and all_times:
                times = [tt for tt in all_times if 0 <= int(tt[:2]) <= 23 and 0 <= int(tt[2:]) <= 59]
                if times:
                    dep_time = f"{times[0][:2]}:{times[0][2:]}"
                    if len(times) > 1:
                        arr_time = f"{times[1][:2]}:{times[1][2:]}"
            if seg['depart_date'] and dep_time:
                seg['depart_at'] = f"{seg['depart_date']}T{dep_time}"
            elif dep_time:
                seg['depart_at'] = dep_time
            ad = seg.get('arrive_date') or seg.get('depart_date')
            if ad and arr_time:
                seg['arrive_at'] = f"{ad}T{arr_time}"
            elif arr_time:
                seg['arrive_at'] = arr_time
    if len(parts) > 6: seg['status_code'] = parts[6] or None
    if len(parts) > 7: seg['status_pax'] = parts[7] or None
    for p in parts[8:]:
        if p in ('ET','E'):
            seg['eticket'] = p
        if re.match(r'^[0-9A-Z]{2,3}$', p) and p not in ('OK','HK','RQ','UC','UN','NO','HX','WK','ET','SS','TK','SA') and not p.isdigit():
            if not seg['equipment'] and any(c.isdigit() for c in p):
                seg['equipment'] = p
    return seg


def parse_passenger(line):
    # I-001;01KAGGWA/DANIEL MR;;APM +256772508884;;
    parts = [p.strip() for p in line.split(';')]
    pax = {'pax_ref': parts[0] if parts else None, 'seq': None, 'full_name': None, 'phone': None, 'email': None, 'type': 'ADT', 'raw': line}
    name = None
    if len(parts) > 1:
        raw1 = parts[1]
        mseq = re.match(r'^(\d{2})(.*)$', raw1)
        if mseq:
            pax['seq'] = mseq.group(1)
            name = mseq.group(2).strip()
        else:
            pax['seq'] = raw1
            name = raw1
    if (not name) and len(parts) > 2 and parts[2].strip():
        name = parts[2].strip()
    if len(parts) > 2 and parts[2].strip() and (not name or name.isdigit()):
        name = parts[2].strip()
    pax['full_name'] = name if name else None
    if pax['full_name']:
        u = pax['full_name'].upper()
        if re.search(r'\b(INF|INFANT)\b', u): pax['type'] = 'INF'
        elif re.search(r'\b(CHD|CHILD|MISS|MSTR)\b', u): pax['type'] = 'CHD'
        else: pax['type'] = 'ADT'
    contact_blob = ' '.join(parts[3:]) if len(parts) > 3 else ''
    m = re.search(r'(\+?\d[\d\s\-]{6,}\d)', contact_blob)
    if not m:
        m = re.search(r'(\+?\d[\d\s\-]{6,}\d)', line)
    if m:
        pax['phone'] = re.sub(r'[\s\-]', '', m.group(1))
    m = re.search(r'[\w\.-]+@[\w\.-]+\.[A-Za-z]{2,}', ' '.join(parts))
    if m:
        pax['email'] = m.group(0)
    return pax


def parse_air_text(text, filename=None):
    t = clean_text(text)
    lines = [ln for ln in t.split('\n')]
    res = {
        'filename': filename,
        'pnr': None,
        'office_id': None,
        'account_code': None,
        'booking_date': None,
        'tktl': None,
        'ticketed': False,
        'ticket_numbers': [],
        'airline_locators': [],
        'segments': [],
        'passengers': [],
        'services': [],
        'remarks': [],
        'currency': 'USD',
        'status': 'booked',
        'parse_ok': False,
        'raw_text': t,
    }
    if not t.strip():
        return res
    # header tokens
    year_hint = None
    for ln in lines[:10]:
        if 'MUC1A' in ln:
            m = re.search(r'MUC1A\s+([A-Z0-9]{6})', ln)
            if m: res['pnr'] = m.group(1)
            else:
                m = re.search(r'MUC1A\s+([A-Z0-9]{5,8})', ln)
                if m: res['pnr'] = m.group(1)[:6]
            tail = ln.split(';')[-1] if ';' in ln else ln
            for am in re.finditer(r'\b([A-Z0-9]{2})\s+([A-Z0-9]{5,7})\b', tail):
                val = am.group(1)+' '+am.group(2)
                if val not in res['airline_locators']:
                    res['airline_locators'].append(val)
        if ln.startswith('AIR-BLK') or ln.startswith('AMDR') or ln.startswith('AMD'):
            parts = [p.strip() for p in ln.split(';')]
            for p in parts:
                if re.match(r'^[A-Z0-9]{6,}$', p) and p not in ('O','1') and not p.startswith('0000'):
                    if not res['office_id']:
                        res['office_id'] = p
        if ln.startswith('D-'):
            parts = [p.strip() for p in ln.split(';')]
            for p in parts[1:]:
                tok = p.strip()
                m = re.match(r'^(\d{2})(\d{2})(\d{2})$', tok)  # YYMMDD
                if m:
                    yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if 1 <= mm <= 12 and 1 <= dd <= 31:
                        year_hint = 2000 + yy
                        try:
                            res['booking_date'] = datetime(year_hint, mm, dd).date().isoformat()
                            break
                        except Exception:
                            pass
                d = parse_ddmon(tok, year_hint=year_hint)
                if d and len(d)==10:
                    res['booking_date'] = d
                    break
    # derive year hint
    if not year_hint and res.get('booking_date') and len(res['booking_date']) >= 4:
        try: year_hint = int(res['booking_date'][:4])
        except: pass
    if not year_hint:
        for ln in lines:
            for y in re.findall(r'\b(20\d{2})\b', ln):
                try: year_hint = int(y); break
                except: pass
            if year_hint: break
            for t in re.findall(r'\b\d{1,2}[A-Z]{3}(2[6-9])\b', ln):
                try: year_hint = 2000 + int(t); break
                except: pass
            if year_hint: break
    if not year_hint:
        year_hint = 2026
    res['_year_hint'] = year_hint
    # scan all lines
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('U-'):
            res['segments'].append(parse_segment(s, year_hint=year_hint))
        elif s.startswith('I-'):
            res['passengers'].append(parse_passenger(s))
        elif s.startswith('TKTL'):
            m = re.search(r'TKTL(\d{1,2}[A-Z]{3}\d{0,4})', s)
            if m:
                tok = m.group(1)
                d = parse_ddmon(tok, year_hint=year_hint)
                res['tktl'] = d or tok
            res['services'].append({'kind':'TKT','code':'TKTL','text':s})
        elif s.startswith('TKOK') or 'TKOK' in s:
            res['ticketed'] = True
            res['services'].append({'kind':'TKT','code':'TKOK','text':s})
        elif s.startswith('SSR'):
            res['services'].append({'kind':'SSR','code':'SSR','text':s})
        elif s.startswith('RM'):
            res['remarks'].append(s)
            res['services'].append({'kind':'RM','code':'RM','text':s})
        elif s.startswith('FH') or s.startswith('FHE') or s.startswith('FA '):
            res['ticketed'] = True
            # extract ticket numbers
            for tn in re.findall(r'\b(\d{3}-?\d{8,10})\b', s):
                res['ticket_numbers'].append(tn)
            res['services'].append({'kind':'DOC','code':'TKT','text':s})
        elif s.startswith('FP') or s.startswith('FV') or s.startswith('FE') or s.startswith('FM'):
            res['services'].append({'kind':'FARE','code':s[:2],'text':s})
        elif s.startswith('RC') or s.startswith('B-') or s.startswith('C-'):
            res['services'].append({'kind':'OTH','code':s[:2],'text':s})
        # account/office hints
        macc = re.search(r'KLA\d+', s)
        if macc and not res['account_code']:
            res['account_code'] = macc.group(0)
    # derive travel dates and route
    dep_dates = [seg.get('depart_date') for seg in res['segments'] if seg.get('depart_date') and not str(seg['depart_date']).startswith('--') and not seg.get('is_void')]
    if dep_dates:
        res['travel_start'] = min(dep_dates)
        res['travel_end'] = max([seg.get('arrive_date') or seg.get('depart_date') for seg in res['segments'] if (seg.get('arrive_date') or seg.get('depart_date')) and not str(seg.get('arrive_date') or seg.get('depart_date') or '').startswith('--') and not seg.get('is_void')] or dep_dates)
    valid_segs = [s for s in res['segments'] if not s.get('is_void') and s.get('origin') and s.get('destination')]
    if valid_segs:
        res['origin'] = valid_segs[0]['origin']
        res['destination'] = valid_segs[-1]['destination']
    # status
    if res['ticketed']:
        res['status'] = 'ticketed'
    elif res['tktl']:
        res['status'] = 'ticketing_pending'
    res['parse_ok'] = bool(res['pnr'] or res['segments'] or res['passengers'] or (t.strip() and 'AIR' in t[:50]))
    return res


def parse_air_file(path):
    with open(path, 'rb') as f:
        b = f.read()
    h = hashlib.sha256(b).hexdigest()
    parsed = parse_air_text(b, filename=os.path.basename(path))
    parsed['content_hash'] = h
    parsed['size'] = len(b)
    return parsed
