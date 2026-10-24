"""Container-local progress probes; no credentials, model calls or public data."""

import argparse
import os
from pathlib import Path
import time

PATH = Path("/tmp/korsik-heartbeat")


def heartbeat(path=PATH):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(str(time.time()))
    os.replace(temporary, path)


def healthy(max_age, path=PATH, timestamp=None):
    try:
        elapsed = (time.time() if timestamp is None else timestamp) - float(path.read_text())
        return 0 <= elapsed <= max_age
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age", type=int, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if healthy(args.max_age) else 1)
