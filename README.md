# community.memex

A single command-line tool, **`memex`**, that turns personal exports and
archives into **SQLite**, ready to explore in [Datasette](https://datasette.io/).

The name is a nod to Vannevar Bush's *memex* — a personal "memory extender":
your mail, chats, and other records gathered into one queryable store. In spirit
it's a self-hosted cousin of [Dogsheep](https://dogsheep.github.io/).

Standard library only — no third-party runtime dependencies.

## Install

```bash
pipx install .            # puts the `memex` command on your PATH
# development (editable, with the test deps):
pip install -e ".[dev]"
```

## Usage

`memex` groups one subcommand per source:

```bash
memex gmail "All mail Including Spam and Trash.mbox" google_mail.db
memex --version
```

| Subcommand | Source → output | Status |
|------------|-----------------|--------|
| `memex gmail` | Google Takeout mbox → SQLite (full-text search, labels, dates) | Ready |
| `memex claude` | Claude `conversations.json` export → SQLite | Planned |

### `memex gmail`

Streams the (often multi-gigabyte) mbox one message at a time. The mbox path may
be given as the first argument or via the `GMAIL_MBOX` environment variable; the
DB defaults to `google_mail.db`.

| Option | Effect |
|--------|--------|
| `--force` | Rebuild from scratch (delete an existing DB first) |
| `--resume` | Continue a previously interrupted run |
| `--store-attachments` | Store attachment bytes as BLOBs (default: metadata only) |
| `--no-html` | Don't store the `text/html` body (smaller DB) |
| `--limit N` | Stop after N messages |
| `--max-body-bytes N` | Truncate stored bodies to N bytes (default 5 MB) |

The importer is resumable: interrupt with Ctrl-C, re-run with `--resume`.

## Explore in Datasette

```bash
datasette serve google_mail.db -i
```

Full-text search is enabled on `subject`, `sender`, `recipients`, `body_text`;
facet labels with `?_facet_array=labels_json`, or by year with `?_facet=year`.

## Layout

```
src/memex/
├── cli.py            # argparse dispatcher — wires each source up as a subcommand
└── sources/
    └── gmail.py      # NAME / HELP / configure(parser) / run(args)
tests/
└── test_gmail.py
```

Adding a source = drop a module in `src/memex/sources/` that exposes
`NAME`, `HELP`, `configure(parser)`, and `run(args)`, then list it in `cli.py`.

## Specs

Each source's behaviour is specified alongside its code (SDD):

| Spec | Status | Description |
|------|--------|-------------|
| [Gmail](specs/gmail.md) | Draft | Google Takeout mbox → SQLite — streaming parse, labels, dates, attachments, FTS; resumable |
| Claude | Planned | Claude `conversations.json` export → SQLite |

The cross-source **Source Manifest** design (a per-item index + checksum) lives in `community.selfhosted/specs/source-manifest.md`.

## Develop

```bash
pip install -e ".[dev]"
pytest
```
