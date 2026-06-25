"""End-to-end test for `memex gmail`: a tiny Takeout-style mbox → SQLite."""

import sqlite3

from memex.cli import build_parser

# Two messages in Google Takeout format. Each begins with the envelope line
# `From <digits>@xxx …` that SEP_RE keys on (note: no blank line before it).
MBOX = b"""From 100@xxx Mon Jan 01 00:00:00 +0000 2024
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Hello
Date: Mon, 01 Jan 2024 00:00:00 +0000
X-Gmail-Labels: Inbox,Important

Hi Bob, this is the body.
From 200@xxx Tue Jan 02 00:00:00 +0000 2024
From: Carol <carol@example.com>
To: Bob <bob@example.com>
Subject: Second
Date: Tue, 02 Jan 2024 00:00:00 +0000
X-Gmail-Labels: Inbox

Another message body.
"""


def _run(argv):
    args = build_parser().parse_args(argv)
    return args._run(args)


def test_gmail_import(tmp_path):
    mbox = tmp_path / "test.mbox"
    mbox.write_bytes(MBOX)
    db = tmp_path / "out.db"

    _run(["gmail", str(mbox), str(db)])

    con = sqlite3.connect(db)
    try:
        # Both messages landed, with the right subjects.
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        subjects = {r[0] for r in con.execute("SELECT subject FROM messages")}
        assert subjects == {"Hello", "Second"}

        # Sender address is extracted and lowercased.
        sender = con.execute(
            "SELECT sender_email FROM messages WHERE subject='Hello'").fetchone()[0]
        assert sender == "alice@example.com"

        # Gmail labels become a JSON array.
        labels = con.execute(
            "SELECT labels_json FROM messages WHERE subject='Hello'").fetchone()[0]
        assert "Important" in labels

        # Full-text search index is built and queryable.
        hits = con.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'body'").fetchone()[0]
        assert hits == 2
    finally:
        con.close()


def test_missing_mbox_exits(tmp_path):
    db = tmp_path / "out.db"
    try:
        _run(["gmail", str(tmp_path / "nope.mbox"), str(db)])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("expected SystemExit for a missing mbox")
