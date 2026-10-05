# MoTA ScholarFlow — End-to-End Working Prototype

This prototype is designed for an SIH demonstration of a unified scholarship/fellowship workflow.

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\\Scripts\\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Open: `http://127.0.0.1:5000`

## What is actually connected

### 1. Application creation
The **Create Application** button sends the form to Flask and creates a persistent SQLite record. The generated MOTA application number is returned by the backend.

### 2. Backend database
`mota.db` is created automatically and stores:
- applications
- uploaded documents
- OCR text
- AI verification result and confidence
- officer decisions
- audit events
- applicant notifications

### 3. Real document upload
The browser uploads the actual JPG/PNG/WEBP/PDF to the Flask server. The file is saved under `uploads/` and can be opened from the Officer Review screen.

### 4. OCR + AI-assisted document agent
For image documents, Tesseract.js performs browser OCR. The extracted text is sent to the backend. The **MoTA Document Intelligence Agent** evaluates document-type indicators, readability, possible quality issues and applicant-name evidence and returns PASS / REVIEW / FAIL with confidence and findings.

This is an explainable prototype agent, not a claim of a production government AI verification service.

### 5. Eligibility pre-check
When an application is created, the backend runs configurable scheme rules. The rules are deliberately separated from the UI so additional MoTA schemes can be configured without rewriting the application screen.

### 6. Officer workflow
Officer can:
- open the application
- open the original uploaded document
- see AI findings
- approve / mark eligible
- request correction
- reject

The decision updates the same SQLite application row and creates an audit event and notification.

### 7. Live analytics
Dashboard counts are calculated from SQLite every time the API is called. There are no fake hard-coded application totals.

## Main API endpoints

- `GET /api/health`
- `GET /api/schemes`
- `GET /api/applications`
- `POST /api/applications`
- `GET /api/applications/<id>`
- `POST /api/upload`
- `GET /api/files/<stored_filename>`
- `POST /api/applications/<id>/review`
- `GET /api/notifications`
- `GET /api/analytics`

## Official scheme sources

- MoTA Scholarship: https://tribal.nic.in/Scholarship.aspx
- MoTA DBT All Schemes: https://dbttribal.gov.in/AllScheme.aspx

## Production integration note

The prototype does **not** pretend to have unauthorised access to Aadhaar, NSP, PFMS, DBT Bharat or internal MoTA systems. Those require official API access, credentials and security controls. The current prototype demonstrates the complete application-to-document-to-AI-to-officer workflow locally and leaves those external integrations as explicit adapters.
