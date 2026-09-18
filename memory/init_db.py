"""Initialise (or re-verify) the memory database schema.

Usage:
    python -m memory.init_db
    python -m memory.init_db --path custom.db
"""
from __future__ import annotations

import argparse
import sys

from . import db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path", default=str(db.DEFAULT_DB_PATH),
        help="Where to create/verify the SQLite file.",
    )
    args = parser.parse_args(argv)

    conn = db.connect(args.path)
    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conn.close()
    print(f"Memory DB ready at {args.path} ({count} memories stored).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
