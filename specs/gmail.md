# Gmail (Google Takeout mbox → SQLite)

## Purpose

Convert a Google Takeout Gmail export (`.mbox`) into a tidy SQLite database ready
for exploration in [Datasette](https://datasette.io/) — with full-text search,
Gmail-label faceting, and sortable dates. Exposed as the `memex gmail` subcommand.

## Definitions

- **mbox**: the Takeout mail export file (typically `All mail Including Spam and
  Trash.mbox`) — a concatenation of RFC 822 messages, each preceded by a Takeout
  envelope separator line of the form `From <thread-id>@… <date>`.
- **Message**: one email in the mbox. Its **locator** is the byte offset of its
  envelope separator (`mbox_offset`), which enables resume and re-extraction.
- **Attachment**: a MIME attachment part of a message — stored as metadata, and
  optionally as bytes.
- **Label**: a Gmail label from the `X-Gmail-Labels` header (e.g. `Inbox`, `Unread`).

## Behavior

### Streaming

1. Read the mbox one message at a time; never load the whole file into memory.
2. Separate messages on the Takeout envelope line matching `From \d+@\S+ ` — the
   `<thread-id>@…` signature — which, unlike the classic "blank line then `From `"
   heuristic, reliably splits Takeout output (Takeout puts no blank line before it).
3. Record each message's byte offset as its `mbox_offset` (locator).

### Parsing (per message)

1. Extract headers, decoded and whitespace-normalised: message-id, Gmail thread-id
   (`X-GM-THRID`), subject, from, to, cc, bcc, references, in-reply-to.
2. Derive `sender_email`: the lowercased addr-spec of `From`.
3. Parse `Date` into a sortable ISO-8601 UTC timestamp plus a plain `year`.
4. Parse `X-Gmail-Labels` into raw text and a JSON array (for `?_facet_array`).
5. Extract bodies: prefer `text/plain`; if absent, derive searchable text from the
   HTML part. Optionally keep the HTML body. Truncate stored bodies to a cap.
6. Extract attachments: filename, content-type, size — storing bytes only on request.
7. On a parse failure, record the reason in `parse_error` and continue; one bad
   message never aborts the run.

### Persistence

1. Insert into `messages` and `attachments`, flushing in batches bounded by message
   count or pending payload bytes.
2. Build an FTS5 index `messages_fts` over subject, sender, recipients, body_text.

### Resume

1. `--resume` continues from the last recorded `mbox_offset`, reprocessing the final
   (possibly partially written) message cleanly. `--force` rebuilds from scratch.

## Inputs

- A Takeout `.mbox` path — the first positional argument, or the `GMAIL_MBOX`
  environment variable.
- An output database path (default `google_mail.db`).
- Options: `--force`, `--resume`, `--store-attachments`, `--no-html`,
  `--max-body-bytes`, `--limit`, `--batch`, `--progress-every`.

## Outputs

- An SQLite database containing:
  - `messages` — one row per email (headers, ISO date + `year`, labels raw + JSON
    array, bodies, sizes, `parse_error`), keyed by `id`, with `mbox_offset` unique.
  - `attachments` — one row per attachment (filename, content-type, size, optional
    BLOB), referencing `messages(id)`.
  - `messages_fts` — FTS5 over subject, sender, recipients, body_text.
- A console summary: message / attachment / parse-error counts, and Datasette hints
  (`?_facet_array=labels_json`, `?_facet=year`).

## Constraints

- Streaming and memory-bounded; never materialises the whole mbox.
- Read-only on the source; writes only the output database.
- Standard library only — no third-party dependencies.
- Resumable and idempotent under `--resume` / `--force`; `mbox_offset` is unique.
- NUL bytes are stripped from stored text (SQLite would otherwise truncate display).

## Open Questions

- Should `gmail` become the first **item iterator** for the cross-source *Source
  Manifest* (spec in community.selfhosted) — feeding `mbox_offset` as the locator and
  a per-message (and per-attachment) MD5 as the checksum?
- Thread reconstruction: expose a `threads` view keyed by `gmail_thread_id`?
- Should label parsing normalise Gmail's category labels (e.g. `Category Personal`)?
- Optionally store decoded attachment text (for FTS over attachment contents)?
