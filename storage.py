"""Pluggable persistence for MoTA ScholarFlow.

Local development uses a SQLite file next to the app. Serverless hosts such as
Vercel have a read-only and ephemeral filesystem, so the same API transparently
uses a remote libSQL / Turso database when credentials are supplied, and falls
back to a SQLite file under /tmp otherwise (functional, but not durable between
serverless invocations).

Environment variables
---------------------
MOTA_DATABASE_URL / TURSO_DATABASE_URL   remote libSQL database URL
MOTA_DATABASE_TOKEN / TURSO_AUTH_TOKEN   bearer token for that database
MOTA_DATA_DIR                            override for the local data directory
MOTA_MAX_UPLOAD_MB                       upload size limit in megabytes
"""

from __future__ import annotations

import os
import sqlite3
import threading

BASE = os.path.dirname(os.path.abspath(__file__))


def _flag(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value) and value.strip().lower() not in ('0', 'false', 'no', '')


IS_SERVERLESS = _flag('VERCEL') or bool(os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))

DB_URL = (os.environ.get('MOTA_DATABASE_URL') or os.environ.get('TURSO_DATABASE_URL') or '').strip()
DB_TOKEN = (os.environ.get('MOTA_DATABASE_TOKEN') or os.environ.get('TURSO_AUTH_TOKEN') or '').strip()

DATA_DIR = os.environ.get('MOTA_DATA_DIR') or ('/tmp/mota' if IS_SERVERLESS else BASE)
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_FILE = os.path.join(DATA_DIR, 'mota.db')

# Uploads are kept in the database on serverless hosts so that the "view file"
# links keep working between invocations. On a normal filesystem they stay on
# disk, which is cheaper and keeps the original on-disk behaviour.
MAX_DB_BLOB_BYTES = 8 * 1024 * 1024

_remote_error = None
_client = None
_client_lock = threading.Lock()


def _turso_client():
    """Return a shared libSQL client, or None when the remote backend is unusable."""
    global _client, _remote_error
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from libsql_client import create_client_sync
            _client = create_client_sync(url=DB_URL, auth_token=DB_TOKEN or None)
        except Exception as exc:  # missing package, bad URL, ...
            _remote_error = f'{type(exc).__name__}: {exc}'
            return None
    return _client


def remote_available() -> bool:
    return bool(DB_URL) and _turso_client() is not None


def backend() -> str:
    if remote_available():
        return 'turso'
    if IS_SERVERLESS:
        return 'sqlite-ephemeral'
    return 'sqlite'


def durable() -> bool:
    """True when writes survive a container restart / a new serverless instance."""
    return remote_available() or not IS_SERVERLESS


def storage_warning() -> str:
    if durable():
        return ''
    if not DB_URL and IS_SERVERLESS:
        return ('No durable database is configured, so this deployment is using an '
                'ephemeral database in /tmp. Set MOTA_DATABASE_URL and '
                'MOTA_DATABASE_TOKEN to a libSQL/Turso database to persist data.')
    if DB_URL and not remote_available():
        return ('MOTA_DATABASE_URL is set but the libSQL client could not be used '
                f'({_remote_error}). Falling back to an ephemeral /tmp database.')
    return ''


def _ensure_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.write-probe')
        with open(probe, 'wb') as handle:
            handle.write(b'1')
        os.remove(probe)
        return True
    except OSError:
        return False


FILES_ON_DISK = _ensure_dir(UPLOAD_DIR)


def max_upload_bytes() -> int:
    configured = os.environ.get('MOTA_MAX_UPLOAD_MB')
    if configured:
        try:
            return max(1, int(configured)) * 1024 * 1024
        except ValueError:
            pass
    return (4 if IS_SERVERLESS else 10) * 1024 * 1024


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_no TEXT UNIQUE NOT NULL,
        scheme_code TEXT NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        mobile TEXT DEFAULT '',
        state TEXT NOT NULL,
        district TEXT DEFAULT '',
        category TEXT DEFAULT 'ST',
        income REAL DEFAULT 0,
        qualification TEXT DEFAULT '',
        institution TEXT DEFAULT '',
        status TEXT DEFAULT 'Submitted',
        stage TEXT DEFAULT 'Application Submitted',
        ai_score REAL DEFAULT 0,
        ai_result TEXT DEFAULT 'Pending',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id INTEGER NOT NULL,
        doc_type TEXT NOT NULL,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        mime_type TEXT DEFAULT '',
        ocr_text TEXT DEFAULT '',
        ai_status TEXT DEFAULT 'REVIEW',
        ai_score REAL DEFAULT 0,
        ai_findings TEXT DEFAULT '[]',
        byte_size INTEGER DEFAULT 0,
        content BLOB,
        created_at TEXT NOT NULL,
        FOREIGN KEY(app_id) REFERENCES applications(id)
    )''',
    '''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        details TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(app_id) REFERENCES applications(id)
    )''',
    '''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read_flag INTEGER DEFAULT 0,
        FOREIGN KEY(app_id) REFERENCES applications(id)
    )''',
    'CREATE INDEX IF NOT EXISTS idx_apps_scheme ON applications(scheme_code)',
    'CREATE INDEX IF NOT EXISTS idx_docs_app ON documents(app_id)',
    'CREATE INDEX IF NOT EXISTS idx_events_app ON events(app_id)',
    'CREATE INDEX IF NOT EXISTS idx_notif_app ON notifications(app_id)',
]

# Added after the first release; applied idempotently so existing local
# databases keep working.
MIGRATIONS = [
    "ALTER TABLE documents ADD COLUMN byte_size INTEGER DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN content BLOB",
]


# --------------------------------------------------------------------------- #
# Backend implementations
# --------------------------------------------------------------------------- #

class _SqliteCursor:
    """Smallest common subset of the sqlite3 cursor API used by the app."""

    __slots__ = ('_source',)

    def __init__(self, source):
        self._source = source

    @property
    def lastrowid(self):
        return self._source.lastrowid

    def fetchone(self):
        row = self._source.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self._source.fetchall()]


class _TursoCursor:

    __slots__ = ('_source',)

    def __init__(self, source):
        self._source = source

    @property
    def lastrowid(self):
        return self._source.last_insert_rowid

    def fetchone(self):
        rows = self._source.rows
        return rows[0].asdict() if rows else None

    def fetchall(self):
        return [row.asdict() for row in self._source.rows]


class _SqliteConnection:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA busy_timeout=30000')
        if path != ':memory:':
            try:
                self._conn.execute('PRAGMA journal_mode=WAL')
            except sqlite3.Error:
                pass

    def execute(self, sql, params=()):
        return _SqliteCursor(self._conn.execute(sql, tuple(params) if params else ()))

    def executescript(self, statements):
        for statement in statements:
            self._conn.execute(statement)
        self.commit()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class _TursoConnection:
    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        args = list(params) if params else None
        return _TursoCursor(self._client.execute(sql, args))

    def executescript(self, statements):
        self._client.batch(list(statements))

    def commit(self):
        return None

    def close(self):
        return None


def connect():
    """Open a connection to whichever backend is configured."""
    client = _turso_client() if DB_URL else None
    if client is not None:
        return _TursoConnection(client)
    return _SqliteConnection(DB_FILE)


_init_lock = threading.Lock()
_initialised = False


def init_db() -> None:
    """Create the schema. Safe to call on every cold start."""
    global _initialised
    with _init_lock:
        if _initialised:
            return
        if DB_URL and not remote_available():
            _initialised = True
            return
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            existing = {row['name'] for row in conn.execute('PRAGMA table_info(documents)').fetchall()}
            if existing:
                for statement in MIGRATIONS:
                    column = statement.split('ADD COLUMN')[1].split()[0]
                    if column not in existing:
                        conn.execute(statement)
                conn.commit()
            _initialised = True
        finally:
            conn.close()


def reset_schema_cache() -> None:
    """Test helper: allow init_db() to run again."""
    global _initialised
    with _init_lock:
        _initialised = False