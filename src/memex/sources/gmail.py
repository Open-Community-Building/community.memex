"""Google Takeout mbox → SQLite (full-text search, Gmail labels, sortable dates).

Streams the (very large) "All mail Including Spam and Trash.mbox" one message at
a time — it never loads the whole file into memory — and writes a tidy schema
ready for Datasette.
"""

import email.policy
import email.utils
import json
import os
import sqlite3
import sys
import time
import re
from datetime import timezone
from email.parser import BytesParser
from html.parser import HTMLParser

NAME = "gmail"
HELP = "Convert a Google Takeout mbox into SQLite (full-text search, labels, dates)."

DEFAULT_DB = "google_mail.db"

# Google Takeout separates messages with an envelope line of the form
#   From 1863628584463804036@xxx Mon Apr 27 12:58:36 +0000 2026
# The "<thread-id>@xxx " signature is unique to these separators, so matching it
# is far more reliable than the classic "blank line, then From " heuristic —
# Takeout does NOT put a blank line before the separator, and that heuristic
# would merge the whole mailbox into one message.
SEP_RE = re.compile(rb"^From \d+@\S+ ")

# Column order for the messages table. parse_message() returns a dict keyed by
# these names; the writer fills in id + mbox_offset.
MESSAGE_COLUMNS = [
    "id", "mbox_offset", "message_id", "gmail_thread_id",
    "date", "date_raw", "year",
    "sender", "sender_email", "recipients", "cc", "bcc", "subject",
    "labels", "labels_json", "mail_references", "in_reply_to",
    "size_bytes", "has_attachments", "attachment_count",
    "body_text", "body_html", "parse_error",
]

# Flush a batch once it reaches this many messages OR this many bytes of payload,
# whichever comes first — the byte cap bounds memory against a run of huge mails.
FLUSH_BYTES = 96 * 1024 * 1024

PARSER = BytesParser(policy=email.policy.default)


# --------------------------------------------------------------------------- #
# mbox streaming
# --------------------------------------------------------------------------- #
def iter_mbox(fh, start_offset=0):
    """Yield (start_offset, raw_message_bytes, end_offset) for each message.

    The envelope "From " line is consumed as a separator and excluded from the
    bytes handed to the email parser. end_offset is the byte position of the next
    separator (or EOF) and is used for progress and resume bookkeeping.
    """
    offset = start_offset
    msg_start = None
    buf = []
    while True:
        line = fh.readline()
        if not line:
            break
        if SEP_RE.match(line):
            if msg_start is not None:
                yield msg_start, b"".join(buf), offset
            buf = []
            msg_start = offset
            offset += len(line)
            continue
        buf.append(line)
        offset += len(line)
    if msg_start is not None:
        yield msg_start, b"".join(buf), offset


# --------------------------------------------------------------------------- #
# HTML → text (stdlib only), for messages that carry no text/plain part
# --------------------------------------------------------------------------- #
class _HTMLText(HTMLParser):
    _BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    _SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def get_text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def html_to_text(html):
    try:
        p = _HTMLText()
        p.feed(html)
        return p.get_text() or None
    except Exception:
        stripped = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", stripped).strip() or None


# --------------------------------------------------------------------------- #
# message parsing
# --------------------------------------------------------------------------- #
def _clean(s):
    """Drop NUL bytes that SQLite would truncate display on."""
    if s is None:
        return None
    s = s.replace("\x00", "")
    return s or None


def hdr(msg, name):
    """Decoded, whitespace-normalised header value (or None)."""
    try:
        val = msg.get(name)
    except Exception:
        return None
    if val is None:
        return None
    try:
        s = str(val)
    except Exception:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def part_text(part):
    """Decode one text part, falling back through charsets on failure."""
    try:
        return part.get_content()
    except Exception:
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        except Exception:
            try:
                return (part.get_payload(decode=True) or b"").decode("latin-1", "replace")
            except Exception:
                return None


def get_bodies(msg):
    text = html = None
    try:
        part = msg.get_body(preferencelist=("plain",))
    except Exception:
        part = None
    if part is not None:
        text = part_text(part)
    try:
        part = msg.get_body(preferencelist=("html",))
    except Exception:
        part = None
    if part is not None:
        html = part_text(part)
    # Non-multipart message that get_body didn't resolve (rare/odd encodings).
    if text is None and html is None and not msg.is_multipart():
        body = part_text(msg)
        if msg.get_content_type() == "text/html":
            html = body
        else:
            text = body
    return text, html


def get_attachments(msg, store):
    out = []
    try:
        parts = list(msg.iter_attachments())
    except Exception:
        return out
    for part in parts:
        try:
            payload = part.get_payload(decode=True)
            size = len(payload) if payload else 0
            content = payload if (store and payload) else None
            out.append((part.get_filename(), part.get_content_type(), size, content))
        except Exception:
            continue
    return out


def parse_message(raw, store_attachments=True, keep_html=True, max_body=5_000_000):
    rec = {"size_bytes": len(raw), "_attachments": []}
    try:
        msg = PARSER.parsebytes(raw)
    except Exception as exc:
        rec["parse_error"] = f"parse: {exc!r}"
        return rec

    rec["message_id"] = hdr(msg, "message-id")
    rec["gmail_thread_id"] = hdr(msg, "x-gm-thrid")
    rec["subject"] = hdr(msg, "subject")
    rec["sender"] = hdr(msg, "from")
    rec["recipients"] = hdr(msg, "to")
    rec["cc"] = hdr(msg, "cc")
    rec["bcc"] = hdr(msg, "bcc")
    rec["mail_references"] = hdr(msg, "references")
    rec["in_reply_to"] = hdr(msg, "in-reply-to")

    # Sender address (lowercased addr-spec) for indexing/faceting.
    if rec["sender"]:
        try:
            addrs = email.utils.getaddresses([rec["sender"]])
            if addrs and addrs[0][1]:
                rec["sender_email"] = addrs[0][1].lower()
        except Exception:
            pass

    # Date → sortable ISO 8601 (UTC) plus a plain year column for faceting.
    date_raw = hdr(msg, "date")
    rec["date_raw"] = date_raw
    if date_raw:
        try:
            dt = email.utils.parsedate_to_datetime(date_raw)
            if dt is not None:
                # Aware → convert to UTC. A naive result is a "-0000" header
                # (RFC 5322: time is UTC, local unknown) — stamp UTC, don't shift.
                dt = (dt.astimezone(timezone.utc) if dt.tzinfo
                      else dt.replace(tzinfo=timezone.utc))
                rec["date"] = dt.isoformat()
                rec["year"] = dt.year
        except Exception:
            pass

    # Gmail labels → raw text + JSON array (for Datasette ?_facet_array=labels_json).
    labels_raw = hdr(msg, "x-gmail-labels")
    if labels_raw:
        rec["labels"] = labels_raw
        labels = [l.strip() for l in labels_raw.split(",") if l.strip()]
        if labels:
            rec["labels_json"] = json.dumps(labels, ensure_ascii=False)

    # Bodies. If there's no text/plain part, derive searchable text from the HTML.
    try:
        text, html = get_bodies(msg)
        if not text and html:
            text = html_to_text(html)
        rec["body_text"] = _clean(text[:max_body] if text else text)
        if keep_html and html:
            rec["body_html"] = _clean(html[:max_body])
    except Exception as exc:
        rec["parse_error"] = f"body: {exc!r}"

    atts = get_attachments(msg, store_attachments)
    rec["_attachments"] = atts
    rec["attachment_count"] = len(atts)
    rec["has_attachments"] = 1 if atts else 0
    return rec


# --------------------------------------------------------------------------- #
# database
# --------------------------------------------------------------------------- #
def create_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id               INTEGER PRIMARY KEY,
            mbox_offset      INTEGER UNIQUE,   -- byte offset of the "From " separator
            message_id       TEXT,
            gmail_thread_id  TEXT,
            date             TEXT,             -- ISO 8601, UTC — sortable
            date_raw         TEXT,
            year             INTEGER,
            sender           TEXT,
            sender_email     TEXT,
            recipients       TEXT,
            cc               TEXT,
            bcc              TEXT,
            subject          TEXT,
            labels           TEXT,             -- raw "Inbox,Unread,…"
            labels_json      TEXT,             -- JSON array → ?_facet_array=labels_json
            mail_references  TEXT,
            in_reply_to      TEXT,
            size_bytes       INTEGER,
            has_attachments  INTEGER,
            attachment_count INTEGER,
            body_text        TEXT,
            body_html        TEXT,
            parse_error      TEXT
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id           INTEGER PRIMARY KEY,
            message_id   INTEGER REFERENCES messages(id),
            filename     TEXT,
            content_type TEXT,
            size_bytes   INTEGER,
            content      BLOB
        );

        CREATE INDEX IF NOT EXISTS idx_messages_date         ON messages(date);
        CREATE INDEX IF NOT EXISTS idx_messages_year         ON messages(year);
        CREATE INDEX IF NOT EXISTS idx_messages_thread       ON messages(gmail_thread_id);
        CREATE INDEX IF NOT EXISTS idx_messages_sender_email ON messages(sender_email);
        CREATE INDEX IF NOT EXISTS idx_messages_message_id   ON messages(message_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_message   ON attachments(message_id);
    """)


def build_fts(db):
    """(Re)build the external-content FTS5 index over messages."""
    db.executescript("""
        DROP TABLE IF EXISTS messages_fts;
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            subject, sender, recipients, body_text,
            content='messages', content_rowid='id'
        );
        INSERT INTO messages_fts(rowid, subject, sender, recipients, body_text)
            SELECT id, subject, sender, recipients, body_text FROM messages;
        INSERT INTO messages_fts(messages_fts) VALUES('optimize');
    """)


def tune(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-262144")   # ~256 MB page cache


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f}{unit}"
        n /= 1024
    return f"{n:,.1f}PB"


def hms(secs):
    secs = int(max(secs, 0))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# CLI: `memex gmail …`
# --------------------------------------------------------------------------- #
def configure(parser):
    parser.add_argument("mbox", nargs="?", default=os.environ.get("GMAIL_MBOX"),
                        help="path to the .mbox file (or set the GMAIL_MBOX env var)")
    parser.add_argument("db", nargs="?", default=DEFAULT_DB, help="output SQLite database")
    parser.add_argument("--force", action="store_true", help="delete an existing DB and start over")
    parser.add_argument("--resume", action="store_true", help="continue a previously interrupted run")
    parser.add_argument("--store-attachments", action="store_true",
                        help="store attachment bytes as BLOBs (default: metadata only)")
    parser.add_argument("--no-html", action="store_true", help="don't store text/html bodies")
    parser.add_argument("--max-body-bytes", type=int, default=5_000_000,
                        help="truncate stored bodies to this length (default 5 MB)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N messages (testing)")
    parser.add_argument("--batch", type=int, default=2000, help="max messages per transaction")
    parser.add_argument("--progress-every", type=int, default=2000,
                        help="refresh the progress line every N messages")


def run(args):
    if not args.mbox:
        sys.exit("memex gmail: no mbox given — pass MBOX or set the GMAIL_MBOX environment variable")
    if not os.path.exists(args.mbox):
        sys.exit(f"memex gmail: mbox not found: {args.mbox}")

    if args.force:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(args.db + suffix)
            except FileNotFoundError:
                pass

    fresh = not os.path.exists(args.db)
    db = sqlite3.connect(args.db)
    if fresh:
        db.execute("PRAGMA page_size=8192")
    tune(db)
    create_schema(db)

    # Decide where to start (fresh vs. resume).
    existing, max_off, _ = db.execute(
        "SELECT COUNT(*), MAX(mbox_offset), MAX(id) FROM messages").fetchone()
    start_offset, next_id = 0, 1
    if existing:
        if not args.resume:
            db.close()
            sys.exit(f"{args.db} already has {existing:,} messages. "
                     f"Use --resume to continue, or --force to rebuild.")
        # Reprocess the last (possibly partially written) message cleanly.
        ids = [r[0] for r in db.execute(
            "SELECT id FROM messages WHERE mbox_offset >= ?", (max_off,))]
        db.executemany("DELETE FROM attachments WHERE message_id=?", [(i,) for i in ids])
        db.execute("DELETE FROM messages WHERE mbox_offset >= ?", (max_off,))
        db.commit()
        start_offset = max_off
        next_id = (db.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0) + 1
        print(f"Resuming from byte {start_offset:,}.")

    existing = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total = os.path.getsize(args.mbox)
    batch = 200 if args.store_attachments else args.batch
    keep_html = not args.no_html

    msg_sql = (f"INSERT INTO messages ({','.join(MESSAGE_COLUMNS)}) "
               f"VALUES ({','.join('?' * len(MESSAGE_COLUMNS))})")
    att_sql = ("INSERT INTO attachments (message_id, filename, content_type, size_bytes, content) "
               "VALUES (?,?,?,?,?)")

    msg_rows, att_rows = [], []
    pending_bytes = 0
    count = 0
    cur_end = start_offset
    started = time.time()

    def flush():
        nonlocal pending_bytes
        if msg_rows:
            db.executemany(msg_sql, msg_rows)
            msg_rows.clear()
        if att_rows:
            db.executemany(att_sql, att_rows)
            att_rows.clear()
        db.commit()
        pending_bytes = 0

    def progress():
        done = cur_end - start_offset
        rate_b = done / max(time.time() - started, 1e-9)
        pct = (cur_end / total * 100) if total else 0
        eta = (total - cur_end) / rate_b if rate_b > 0 else 0
        sys.stderr.write(
            f"\r[{pct:5.1f}%] {existing + count:>9,} msgs  "
            f"{human(rate_b)}/s  elapsed {hms(time.time() - started)}  ETA {hms(eta)}   ")
        sys.stderr.flush()

    print(f"Reading {args.mbox}  ({human(total)})")
    try:
        with open(args.mbox, "rb", buffering=1024 * 1024) as fh:
            fh.seek(start_offset)
            for offset, raw, end in iter_mbox(fh, start_offset):
                cur_end = end
                rec = parse_message(raw, args.store_attachments, keep_html, args.max_body_bytes)
                mid = next_id
                next_id += 1
                rec["id"] = mid
                rec["mbox_offset"] = offset
                msg_rows.append(tuple(rec.get(c) for c in MESSAGE_COLUMNS))
                for att in rec["_attachments"]:
                    att_rows.append((mid,) + att)
                pending_bytes += rec["size_bytes"]
                count += 1
                if len(msg_rows) >= batch or pending_bytes >= FLUSH_BYTES:
                    flush()
                if count % args.progress_every == 0:
                    progress()
                if args.limit and count >= args.limit:
                    break
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted — flushing and exiting (use --resume to continue).\n")
    finally:
        flush()
        progress()
        sys.stderr.write("\n")

    print("Building full-text search index …")
    build_fts(db)
    db.commit()

    n_msgs = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_att = db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    n_err = db.execute("SELECT COUNT(*) FROM messages WHERE parse_error IS NOT NULL").fetchone()[0]
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()

    print(f"\nDone → {args.db}")
    print(f"  {n_msgs:>9,}  messages")
    print(f"  {n_att:>9,}  attachments")
    if n_err:
        print(f"  {n_err:>9,}  messages with parse_error (see the parse_error column)")
    print()
    print(f"Open with:  datasette serve {args.db} -i")
    print("  • full-text search is enabled on messages (subject, sender, recipients, body_text)")
    print("  • facet labels with  ?_facet_array=labels_json   — also try  ?_facet=year")
