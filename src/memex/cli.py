"""Command-line entry point: `memex <source> …`.

Each source module under `memex.sources` exposes a small, uniform interface so
adding a converter is just dropping in a module and listing it here:

    NAME              the subcommand name (e.g. "gmail")
    HELP              one-line help string
    configure(parser) add the subcommand's arguments to its argparse parser
    run(args)         do the work; return an exit code (or None for success)
"""

import argparse

from . import __version__
from .sources import gmail

# Registered sources, in the order they appear in `memex --help`.
SOURCES = [gmail]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memex",
        description="Turn personal exports and archives into SQLite for exploration in Datasette.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="source", metavar="<source>", required=True)
    for src in SOURCES:
        p = sub.add_parser(src.NAME, help=src.HELP, description=src.HELP)
        src.configure(p)
        p.set_defaults(_run=src.run)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args._run(args)
