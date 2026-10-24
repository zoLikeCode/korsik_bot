#!/usr/bin/env python3
"""Connected operator only: shared pinned installer, never runtime download.

Default bundle uses Qwen3.5-4B. Optional 0.8B: --role generator. Its measured
semantic failure forbids activation without a new independent quality pass.
"""
import argparse
import importlib.util
import json
from pathlib import Path


def main():
    source = Path(__file__).resolve().parents[1] / "services/model_server/fetch_models.py"
    spec = importlib.util.spec_from_file_location("korsik_pinned_model_installer", source)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default="/opt/korsik/models")
    parser.add_argument("--role", action="append", choices=list(installer.CATALOG))
    args = parser.parse_args()
    for role in args.role or ["embedder", "reranker", "generator-4b"]:
        print(json.dumps(installer.install(role, args.destination)), flush=True)


if __name__ == "__main__":
    main()
