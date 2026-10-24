"""Operator entry points. Never prints secrets or resets existing accounts."""

import argparse
import json
import os

from .auth import bootstrap_admin
from .db import SessionLocal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["migrate", "bootstrap", "doctor", "seed", "preflight", "reindex"],
    )
    args = parser.parse_args()
    if args.command == "reindex":
        from .recovery import reindex_published

        with SessionLocal() as db:
            print(json.dumps(reindex_published(db)))
    elif args.command in {"migrate", "bootstrap", "seed"}:
        from .migrations import migrate, provision_roles

        migrate()
        if os.getenv("KORSIK_PROVISION_ROLES") == "true":
            provision_roles()
        with SessionLocal() as db:
            if args.command in {"bootstrap", "seed"}:
                bootstrap_admin(db)
                from .seed import seed_defaults

                seed_defaults(db)
                db.commit()
        print(json.dumps({"command": args.command, "status": "OK"}))
    else:
        from .settings import get_settings, read_secret

        s = get_settings()
        info = {
            "product_mode": s.product_mode,
            "generation_enabled": s.generation_enabled,
            "max_mode": s.max_mode,
            "database_configured": bool(s.database_url),
            "session_secret_present": bool(read_secret(s.secret_key_file, False)),
        }
        if s.product_mode == "FULL" and not s.generation_enabled:
            raise SystemExit("FULL requires local generation")
        print(json.dumps(info))


if __name__ == "__main__":
    main()
