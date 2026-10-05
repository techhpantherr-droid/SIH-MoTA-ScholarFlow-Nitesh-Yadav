from flask import Flask, request, jsonify, send_from_directory, send_file, abort
from werkzeug.utils import secure_filename
from datetime import datetime
import os, re, uuid, json
from io import BytesIO

import storage
from storage import DATA_DIR

BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, 'public')
UPLOADS = storage.UPLOAD_DIR

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = storage.max_upload_bytes()
app.config['MAX_FORM_MEMORY_SIZE'] = storage.max_upload_bytes()
app.json.sort_keys = False

OFFICIAL_SOURCES = {
    'mota': 'https://tribal.nic.in/Scholarship.aspx',
    'dbt': 'https://dbttribal.gov.in/AllScheme.aspx'
}

SCHEMES = [
    {'code':'BPVGK','name':'Pre-Matric Scholarship Scheme For ST Students','type':'Centrally Sponsored Scheme','portal':'https://dbttribal.gov.in/','source':OFFICIAL_SOURCES['dbt'],'notes':'Classes IX-X; implemented through States/UTs.','rules':{'min_class':9,'max_class':10}},
    {'code':'BVOBC','name':'Post-Matric Scholarship Scheme For ST Students','type':'Centrally Sponsored Scheme','portal':'https://dbttribal.gov.in/','source':OFFICIAL_SOURCES['dbt'],'notes':'Post-matric education; online application and verification through the applicable state/UT workflow.','rules':{'requires_st':True}},
    {'code':'A023B','name':'Top Class Education For ST Students','type':'Central Sector Scheme','portal':'https://scholarships.gov.in/','source':OFFICIAL_SOURCES['dbt'],'notes':'Higher education in identified premier institutes; MoTA publishes scheme and institute information.','rules':{'income_ceiling':600000,'requires_st':True}},
    {'code':'ARG45','name':'National Fellowship for ST Students','type':'Central Sector Scheme','portal':'https://fellowship.tribal.gov.in/','source':OFFICIAL_SOURCES['dbt'],'notes':'Research fellowship for ST students; institute verification/approval precedes release.','rules':{'requires_st':True}},
    {'code':'AZKMI','name':'National Overseas Scholarship Scheme','type':'Central Sector Scheme','portal':'https://overseas.tribal.gov.in/','source':OFFICIAL_SOURCES['dbt'],'notes':'Overseas higher studies; Ministry verification and committee screening/selection.','rules':{'income_ceiling':600000,'requires_st':True}}
]

RUPEE = '\u20b9'

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}
# Documents retrieved by the frontend from /api/applications/<id>.
DOC_COLUMNS = 'id,app_id,doc_type,filename,stored_path,mime_type,ocr_text,ai_status,ai_score,ai_findings,byte_size,created_at'


def db():
    return storage.connect()


def now():
    return datetime.now().isoformat(timespec='seconds')


def init_db():
    storage.init_db()


init_db()


def add_event(app_id, event, actor='System', details=''):
    c = db(); c.execute('INSERT INTO events(app_id,event,actor,details,created_at) VALUES(?,?,?,?,?)',
                        (app_id, event, actor, details, now())); c.commit(); c.close()


def notify(app_id, title, message):
    c = db(); c.execute('INSERT INTO notifications(app_id,title,message,created_at) VALUES(?,?,?,?)',
                        (app_id, title, message, now())); c.commit(); c.close()


def get_scheme(code):
    return next((s for s in SCHEMES if s['code'] == code), None)


def eligibility_check(data, scheme):
    reasons = []
    warnings = []
    category = (data.get('category') or '').strip().upper()
    try:
        income = float(data.get('income') or 0)
    except (TypeError, ValueError):
        income = 0.0
    rules = scheme['rules']
    if rules.get('requires_st') and category != 'ST':
        reasons.append('Applicant category must be ST for this scheme.')
    if 'income_ceiling' in rules and income > rules['income_ceiling']:
        reasons.append(f"Reported family income {RUPEE}{income:,.0f} is above the configured ceiling of {RUPEE}{rules['income_ceiling']:,.0f}.")
    if not data.get('qualification'):
        warnings.append('Qualification is missing and should be completed during scrutiny.')
    if not data.get('institution'):
        warnings.append('Institution/university is missing and should be completed during scrutiny.')
    result = 'ELIGIBLE' if not reasons else 'NOT_ELIGIBLE'
    return {'result': result, 'reasons': reasons, 'warnings': warnings}


def agent_check(text, doc_type, application=None):
    t = (text or '').lower()
    findings = []
    # Base score keeps an un-readable document in REVIEW (a human must look at
    # the original) instead of FAIL. FAIL is reserved for explicit quality or
    # template concerns detected below.
    score = 50.0
    if len(t) >= 40:
        score += 15
        findings.append('OCR returned a readable text body.')
    elif len(t) > 0:
        score += 5
        findings.append('Only a small amount of text was extracted; manual review is recommended.')
    else:
        findings.append('No OCR text was available; original document must be checked by the officer.')

    dtype = doc_type.lower()
    if 'st' in dtype:
        if 'scheduled tribe' in t or re.search(r'\bst\b', t) or 'tribe certificate' in t:
            score += 30; findings.append('ST/category wording detected.')
        else:
            findings.append('ST/category wording was not confidently detected.')
    elif 'income' in dtype:
        if 'income' in t or 'annual' in t or 'family' in t:
            score += 20; findings.append('Income-certificate wording detected.')
        amounts = re.findall(r'(?:rs\.?' + RUPEE + r')\s*[0-9,]+', t)
        if amounts:
            findings.append('Income amount candidate detected: ' + amounts[0])
    elif 'marksheet' in dtype:
        if any(x in t for x in ['marks', 'semester', 'grade', 'percentage', 'university']):
            score += 25; findings.append('Academic marks/grade wording detected.')
    elif 'admission' in dtype or 'offer' in dtype:
        if any(x in t for x in ['admission', 'offer', 'university', 'institute']):
            score += 25; findings.append('Admission/institution wording detected.')
    elif 'domicile' in dtype:
        if any(x in t for x in ['domicile', 'residence', 'resident']):
            score += 25; findings.append('Residence/domicile wording detected.')

    if any(x in t for x in ['blur', 'illegible', 'sample', 'specimen', 'draft']):
        score -= 20; findings.append('Potential quality/template concern detected in OCR text.')
    if application and application.get('name') and application['name'].lower().split()[0] in t:
        score += 5; findings.append('Applicant-name token appears in extracted text.')

    score = max(0, min(99, score))
    status = 'PASS' if score >= 75 else ('REVIEW' if score >= 50 else 'FAIL')
    return {
        'status': status, 'score': round(score, 1), 'findings': findings,
        'agent': 'MoTA Document Intelligence Agent',
        'disclaimer': 'AI-assisted pre-screening only; an authorised officer remains the final verifier.'
    }


def resolve_application_id(value):
    """Accept a numeric database id or a MOTA-YYYY-XXXXXXX application number."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if not re.fullmatch(r'MOTA-\d{4}-[0-9A-Za-z]+', raw, flags=re.IGNORECASE):
        return None
    c = db()
    try:
        row = c.execute('SELECT id FROM applications WHERE app_no=? COLLATE NOCASE', (raw.upper(),)).fetchone()
    finally:
        c.close()
    return row['id'] if row else None


def persist_file(original, blob):
    """Store an upload on disk when possible, otherwise in the database.

    Serverless filesystems are ephemeral, so on Vercel the bytes go into the
    database to keep the "view document" links alive between invocations.
    Returns (stored_path, blob_or_None).
    """
    stored = f'{uuid.uuid4().hex}_{original}'
    if storage.FILES_ON_DISK:
        with open(os.path.join(UPLOADS, stored), 'wb') as handle:
            handle.write(blob)
        return stored, None
    if len(blob) > storage.MAX_DB_BLOB_BYTES:
        raise ValueError(f'File exceeds the {storage.MAX_DB_BLOB_BYTES // (1024 * 1024)} MB limit for this deployment.')
    return stored, blob


def load_file(stored_path):
    """Return (bytes, mime) for a stored upload, from disk or the database."""
    if storage.FILES_ON_DISK:
        path = os.path.join(UPLOADS, stored_path)
        if os.path.isfile(path):
            with open(path, 'rb') as handle:
                return handle.read(), None
    c = db()
    try:
        row = c.execute('SELECT mime_type,content FROM documents WHERE stored_path=?', (stored_path,)).fetchone()
    finally:
        c.close()
    if row and row['content'] is not None:
        blob = row['content']
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        return bytes(blob), row['mime_type'] or None
    return None, None


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.get('/')
def home():
    return send_from_directory(WEB_DIR, 'index.html')


@app.get('/favicon.ico')
def favicon():
    return '', 204


@app.get('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get('/api/health')
def health():
    count = 0
    database_error = ''
    try:
        c = db(); count = c.execute('SELECT COUNT(*) n FROM applications').fetchone()['n']; c.close()
    except Exception as exc:
        database_error = f'{type(exc).__name__}: {exc}'
    return jsonify({
        'ok': not database_error,
        'database': storage.backend(),
        'durable': storage.durable(),
        'application_count': count,
        'data_dir': DATA_DIR,
        'files_on_disk': storage.FILES_ON_DISK,
        'max_upload_mb': app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024),
        'warning': storage.storage_warning(),
        'error': database_error,
        'time': now()
    })


@app.get('/api/schemes')
def schemes():
    return jsonify(SCHEMES)


@app.route('/api/applications', methods=['GET', 'POST'])
def applications():
    if request.method == 'GET':
        c = db()
        rows = c.execute('SELECT * FROM applications ORDER BY id DESC').fetchall()
        c.close()
        return jsonify(rows)

    data = request.get_json(silent=True) or {}
    required = ['scheme_code', 'name', 'email', 'state']
    missing = [x for x in required if not str(data.get(x, '')).strip()]
    if missing: return jsonify({'ok': False, 'error': 'Missing: ' + ', '.join(missing)}), 400
    scheme = get_scheme(data['scheme_code'])
    if not scheme: return jsonify({'ok': False, 'error': 'Invalid scheme'}), 400
    try: income = float(data.get('income') or 0)
    except (TypeError, ValueError): return jsonify({'ok': False, 'error': 'Income must be numeric'}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', data['email']):
        return jsonify({'ok': False, 'error': 'Enter a valid email address'}), 400

    appno = f"MOTA-{datetime.now().year}-{uuid.uuid4().hex[:7].upper()}"
    ts = now(); c = db()
    try:
        cur = c.execute('''INSERT INTO applications(app_no,scheme_code,name,email,mobile,state,district,category,income,qualification,institution,status,stage,ai_score,ai_result,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (appno, scheme['code'], data['name'].strip(), data['email'].strip(), data.get('mobile',''), data['state'].strip(),
                         data.get('district',''), data.get('category','ST'), income, data.get('qualification',''), data.get('institution',''),
                         'Submitted', 'Application Submitted', 0, 'Pending', ts, ts))
        aid = cur.lastrowid
        c.commit()
    finally:
        c.close()
    eligibility = eligibility_check(data, scheme)
    add_event(aid, 'Application submitted', 'Applicant', f"Application {appno} created for {scheme['name']}")
    notify(aid, 'Application received', f'Your application {appno} has been saved and is now in Document Verification.')
    return jsonify({'ok': True, 'id': aid, 'app_no': appno, 'eligibility_precheck': eligibility}), 201


@app.get('/api/applications/<int:aid>')
def application(aid):
    c = db()
    try:
        a = c.execute('SELECT * FROM applications WHERE id=?', (aid,)).fetchone()
        if not a: return jsonify({'ok': False, 'error': 'Application not found'}), 404
        docs = c.execute(f'SELECT {DOC_COLUMNS} FROM documents WHERE app_id=? ORDER BY id', (aid,)).fetchall()
        ev = c.execute('SELECT * FROM events WHERE app_id=? ORDER BY id', (aid,)).fetchall()
        notes = c.execute('SELECT * FROM notifications WHERE app_id=? ORDER BY id DESC', (aid,)).fetchall()
    finally:
        c.close()
    return jsonify({'ok': True, 'application': a, 'documents': docs, 'events': ev, 'notifications': notes})


@app.get('/api/applications/by-number/<app_no>')
def application_by_number(app_no):
    aid = resolve_application_id(app_no)
    if aid is None: return jsonify({'ok': False, 'error': 'Invalid application number'}), 400
    return application(aid)


@app.post('/api/upload')
def upload():
    f = request.files.get('file'); aid_raw = request.form.get('app_id') or request.form.get('app_no')
    dtype = request.form.get('doc_type', 'Other')
    if not f or not aid_raw:
        return jsonify({'ok': False, 'error': 'Application ID and file are required'}), 400
    aid = resolve_application_id(aid_raw)
    if aid is None:
        return jsonify({'ok': False, 'error': f'"{str(aid_raw)[:40]}" is not a valid application ID or MOTA application number.'}), 400
    c = db()
    try:
        a = c.execute('SELECT * FROM applications WHERE id=?', (aid,)).fetchone()
    finally:
        c.close()
    if not a: return jsonify({'ok': False, 'error': 'Application not found'}), 404
    if not f.filename: return jsonify({'ok': False, 'error': 'Select a file'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'ok': False, 'error': 'Allowed files: JPG, JPEG, PNG, WEBP, PDF'}), 400

    original = secure_filename(f.filename) or f'document{ext}'
    blob = f.read()
    if not blob: return jsonify({'ok': False, 'error': 'The selected file is empty'}), 400
    try:
        stored, db_blob = persist_file(original, blob)
    except (ValueError, OSError) as exc:
        return jsonify({'ok': False, 'error': f'Could not store the upload: {exc}'}), 500

    text = request.form.get('ocr_text', '')
    result = agent_check(text, dtype, a)
    ts = now(); c = db()
    try:
        cur = c.execute('''INSERT INTO documents(app_id,doc_type,filename,stored_path,mime_type,ocr_text,ai_status,ai_score,ai_findings,byte_size,content,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (aid,dtype,original,stored,f.mimetype or '',text,result['status'],result['score'],
                         json.dumps(result['findings']),len(blob),db_blob,ts))
        did = cur.lastrowid
        avg = c.execute('SELECT AVG(ai_score) v FROM documents WHERE app_id=?', (aid,)).fetchone()['v'] or 0
        c.execute("UPDATE applications SET ai_score=?, ai_result=?, stage=?, updated_at=? WHERE id=?",
                  (round(avg,1), 'AI pre-screen completed', 'Document Verification', ts, aid))
        c.commit()
    finally:
        c.close()
    add_event(aid, 'Document AI check completed', 'MoTA AI Agent', f'{dtype}: {result["status"]} ({result["score"]}%)')
    notify(aid, 'Document verification updated', f'{dtype} received. AI pre-screen result: {result["status"]} ({result["score"]}%).')
    return jsonify({'ok': True, 'document_id': did, 'result': result, 'file': original, 'application_id': aid,
                    'stored_on_disk': db_blob is None})


@app.get('/api/files/<path:filename>')
def files(filename):
    name = os.path.basename(filename)
    path = os.path.join(UPLOADS, name)
    if storage.FILES_ON_DISK and os.path.isfile(path):
        return send_from_directory(UPLOADS, name, as_attachment=False)
    blob, mime = load_file(name)
    if blob is None:
        return jsonify({'ok': False, 'error': 'File not found'}), 404
    return send_file(BytesIO(blob), mimetype=mime or 'application/octet-stream', download_name=name)


@app.post('/api/applications/<int:aid>/review')
def review(aid):
    data = request.get_json(silent=True) or {}; action = data.get('action'); note = data.get('note','')
    mapping = {
        'approve': ('Eligible', 'Officer Scrutiny Completed'),
        'correction': ('Deficiency', 'Correction Requested'),
        'reject': ('Rejected', 'Officer Decision - Rejected')
    }
    if action not in mapping: return jsonify({'ok': False, 'error': 'Invalid action'}), 400
    c = db()
    try:
        a = c.execute('SELECT * FROM applications WHERE id=?', (aid,)).fetchone()
        if not a: return jsonify({'ok': False, 'error': 'Application not found'}), 404
        status, stage = mapping[action]; ts = now()
        c.execute('UPDATE applications SET status=?, stage=?, updated_at=? WHERE id=?', (status,stage,ts,aid))
        c.commit()
    finally:
        c.close()
    add_event(aid, stage, 'Authorised Officer', note or 'Decision recorded in review console.')
    notify(aid, 'Application status updated', f'Application {a["app_no"]} is now {status}.')
    return jsonify({'ok': True, 'status': status, 'stage': stage})


@app.get('/api/notifications')
def notifications():
    aid = request.args.get('app_id')
    resolved = resolve_application_id(aid) if aid else None
    if aid and resolved is None:
        return jsonify({'ok': False, 'error': 'Invalid application id'}), 400
    c = db()
    try:
        if resolved is not None:
            rows = c.execute('SELECT * FROM notifications WHERE app_id=? ORDER BY id DESC', (resolved,)).fetchall()
        else:
            rows = c.execute('SELECT * FROM notifications ORDER BY id DESC LIMIT 100').fetchall()
    finally:
        c.close()
    return jsonify(rows)


@app.get('/api/analytics')
def analytics():
    c = db()
    try:
        total = c.execute('SELECT COUNT(*) n FROM applications').fetchone()['n']
        statuses = c.execute('SELECT status,COUNT(*) n FROM applications GROUP BY status').fetchall()
        stages = c.execute('SELECT stage,COUNT(*) n FROM applications GROUP BY stage').fetchall()
        docs = c.execute('SELECT ai_status,COUNT(*) n FROM documents GROUP BY ai_status').fetchall()
        schemes = c.execute('SELECT scheme_code,COUNT(*) n FROM applications GROUP BY scheme_code').fetchall()
    finally:
        c.close()
    return jsonify({'total':total,'statuses':statuses,'stages':stages,'documents':docs,'schemes':schemes,
                    'official_sources':list(OFFICIAL_SOURCES.values()),
                    'database': storage.backend(), 'durable': storage.durable()})


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Endpoint not found'}), 404
    return send_from_directory(WEB_DIR, 'index.html')


@app.errorhandler(405)
def not_allowed(_e):
    return jsonify({'ok': False, 'error': 'Method not allowed'}), 405


@app.errorhandler(413)
def too_large(_e):
    limit = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify({'ok': False, 'error': f'File is too large. Maximum upload size on this deployment is {limit} MB.'}), 413


@app.errorhandler(500)
def server_error(_e):
    return jsonify({'ok': False, 'error': 'Internal server error'}), 500


if __name__ == '__main__':
    storage.reset_schema_cache()
    init_db()
    port = int(os.environ.get('PORT') or 5000)
    app.run(host=os.environ.get('HOST', '127.0.0.1'), port=port, debug=bool(os.environ.get('FLASK_DEBUG')))