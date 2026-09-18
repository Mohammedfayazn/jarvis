"""Create (or verify) the project database.

    python -m projects.init_db
    python -m projects.init_db --path somewhere.db
"""
from __future__ import annotations

import argparse
import sys

from . import db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(db.DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    conn = db.connect(args.path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("projects", "tasks", "entries", "work_sessions")}
    conn.close()
    print(f"Project DB ready at {args.path}: "
          + ", ".join(f"{n} {t}" for t, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
