"""One application and one send per job, and which employers have been sent to before.

`application_queue.url` is unique per spelling of a link; a job is not. These tables key
applications by `destinations.job_key`, so `:443`, a trailing slash, an extra query or a
board's second hostname cannot become a second application or a second send.

- `application_jobs`: every application's job key. A partial unique index allows one
  canonical application per key; rows that were already duplicates when the index was
  built stay, marked `canonical=0`, and can never send a second time for the job.
- `job_sends`: the one application per job key that was sent or is being sent.
- `sent_tenants`: employer boards that already received an application, or that the
  owner approved (the `first_send_hold: all` policy holds the first send to any other).
- `familiar_hosts`: hosts off the board table where the owner has let a form be filled
  once, by pasting the link, picking the job or replying `go`; a feed job whose form is
  on any other host off the table waits for him.

The first two are derived from `application_queue` and `live_submission_attempts` and
are rebuilt whenever `destinations.KEY_VERSION` changes; the other two persist. Every
function takes the caller's connection, so a check and its write share one transaction.
"""

import contextlib
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .destinations import KEY_VERSION, approved_ats, job_key, tenant_key

# States in which nothing has been sent; any other state means a send happened or is
# in flight, including states added later.
PRE_SEND = ("QUEUED", "PREPARING", "NEEDS_USER", "READY_FOR_REVIEW", "MANUAL_TAKEOVER", "DEFERRED")
LIVE_ATTEMPT = ("SUBMITTING", "APPLIED", "UNKNOWN_SUBMISSION")

SCHEMA = """
  CREATE TABLE IF NOT EXISTS application_jobs(
    application_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, canonical INTEGER NOT NULL);
  CREATE UNIQUE INDEX IF NOT EXISTS application_jobs_canonical
    ON application_jobs(job_key) WHERE canonical=1;
  CREATE INDEX IF NOT EXISTS application_jobs_key ON application_jobs(job_key);
  CREATE TABLE IF NOT EXISTS job_sends(
    job_key TEXT PRIMARY KEY, application_id TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS sent_tenants(
    tenant TEXT PRIMARY KEY, application_id TEXT NOT NULL, basis TEXT NOT NULL,
    created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS familiar_hosts(
    host TEXT PRIMARY KEY, application_id TEXT NOT NULL, basis TEXT NOT NULL,
    created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS job_index_meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def current(conn) -> bool:
    try:
        row = conn.execute("SELECT value FROM job_index_meta WHERE name='key_version'").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row) and row[0] == str(KEY_VERSION)


def ensure(conn):
    """Create or rebuild the index when it is missing or was built under older key rules.

    Existing applications keep their rows and ids. Where two of them are the same job,
    the one that was sent (else the oldest) becomes the job's application.
    """
    if current(conn):
        return
    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not current(conn):  # another process may have rebuilt it while this one waited
            rebuild(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def rebuild(conn):
    conn.execute("DELETE FROM application_jobs")
    conn.execute("DELETE FROM job_sends")
    rows = conn.execute(
        "SELECT q.id,q.url,q.status,q.created_at,a.status AS attempt FROM application_queue q "
        "LEFT JOIN live_submission_attempts a ON a.application_id=q.id"
    ).fetchall()

    def sent(row) -> bool:
        return row["status"] not in PRE_SEND or row["attempt"] in LIVE_ATTEMPT

    seen = set()
    for row in sorted(rows, key=lambda r: (not sent(r), r["created_at"], r["id"])):
        try:
            key = job_key(row["url"])
        except ValueError:
            # A stored link that no longer parses is its own job; it must not stop the rest.
            key = f'["unparsed","{row["id"]}"]'
        conn.execute(
            "INSERT INTO application_jobs VALUES(?,?,?)", (row["id"], key, int(key not in seen))
        )
        seen.add(key)
        if sent(row):
            conn.execute(
                "INSERT OR IGNORE INTO job_sends VALUES(?,?,?)", (key, row["id"], row["created_at"])
            )
            # An employer that already has an application learns nothing new from another.
            with contextlib.suppress(ValueError):
                approve_tenant(conn, row["url"], row["id"], "sent before", row["created_at"])
                familiarize(conn, row["url"], row["id"], "sent before", row["created_at"])
    conn.execute(
        "INSERT OR REPLACE INTO job_index_meta VALUES('key_version',?)", (str(KEY_VERSION),)
    )


def existing_application(conn, url: str) -> str | None:
    """The application this link belongs to: its exact URL first, else the job's own."""
    row = conn.execute("SELECT id FROM application_queue WHERE url=?", (url,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT q.id FROM application_jobs j JOIN application_queue q ON q.id=j.application_id "
        "WHERE j.job_key=? AND j.canonical=1",
        (job_key(url),),
    ).fetchone()
    return row[0] if row else None


def register(conn, application_id: str, url: str) -> str:
    """Record a new application's job key; the unique index refuses a second canonical row."""
    key = job_key(url)
    taken = conn.execute(
        "SELECT 1 FROM application_jobs WHERE job_key=? AND canonical=1", (key,)
    ).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO application_jobs VALUES(?,?,?)",
        (application_id, key, int(not taken)),
    )
    return key


def key_of(conn, application_id: str, url: str) -> str:
    row = conn.execute(
        "SELECT job_key FROM application_jobs WHERE application_id=?", (application_id,)
    ).fetchone()
    return row[0] if row else register(conn, application_id, url)


def sent_elsewhere(conn, application_id: str, key: str) -> str | None:
    """Another application for the same job that was sent or is being sent."""
    marks = ",".join("?" * len(PRE_SEND))
    live = ",".join("?" * len(LIVE_ATTEMPT))
    row = conn.execute(
        "SELECT q.id FROM application_jobs j JOIN application_queue q ON q.id=j.application_id "
        "LEFT JOIN live_submission_attempts a ON a.application_id=q.id "
        f"WHERE j.job_key=? AND q.id!=? AND (q.status NOT IN ({marks}) OR a.status IN ({live})) "
        "LIMIT 1",
        (key, application_id, *PRE_SEND, *LIVE_ATTEMPT),
    ).fetchone()
    return row[0] if row else None


def claim_send(conn, application_id: str, key: str):
    """Take the job's one send. Call `sent_elsewhere` first: a row left by an application
    that turned out not to have sent is replaced, a live one is never."""
    if sent_elsewhere(conn, application_id, key):
        raise PermissionError("Another application for this job was already sent")
    conn.execute("DELETE FROM job_sends WHERE job_key=?", (key,))
    conn.execute("INSERT INTO job_sends VALUES(?,?,?)", (key, application_id, now()))


def keep_send(conn, application_id: str, url: str):
    """An application that was sent without a claim (by hand, or settled by the
    employer's mail) holds the job's send too, unless another one already does."""
    conn.execute(
        "INSERT OR IGNORE INTO job_sends VALUES(?,?,?)",
        (key_of(conn, application_id, url), application_id, now()),
    )


def release_send(conn, application_id: str):
    """Nothing was sent after all: the job may be sent once by a later attempt."""
    conn.execute("DELETE FROM job_sends WHERE application_id=?", (application_id,))


def tenant_seen(conn, url: str) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM sent_tenants WHERE tenant=?", (tenant_key(url),)).fetchone()
    )


def approve_tenant(conn, url: str, application_id: str, basis: str, when: str | None = None):
    conn.execute(
        "INSERT OR IGNORE INTO sent_tenants VALUES(?,?,?,?)",
        (tenant_key(url), application_id, basis, when or now()),
    )


def host_of(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def familiar(conn, url: str) -> bool:
    """A host a form may be filled on: a board in the table, or a host off the table the
    owner has already let a form be filled on once."""
    if approved_ats(url):
        return True
    host = host_of(url)
    return bool(
        host and conn.execute("SELECT 1 FROM familiar_hosts WHERE host=?", (host,)).fetchone()
    )


def familiarize(conn, url: str, application_id: str, basis: str, when: str | None = None):
    """Remember a host off the table the owner has let a form be filled on."""
    host = host_of(url)
    if not host or approved_ats(url):
        return
    conn.execute(
        "INSERT OR IGNORE INTO familiar_hosts VALUES(?,?,?,?)",
        (host, application_id, basis, when or now()),
    )
