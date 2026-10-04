#!/usr/bin/env python3
"""Build one locally verifiable qsl.development_research_review.v1 message."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path


def _producer():
    """Import the installed producer, or the single checkout that contains this script."""
    try:
        return importlib.import_module("service.development_research_review")
    except ModuleNotFoundError:
        repository_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repository_root))
        return importlib.import_module("service.development_research_review")


_producer_module = _producer()
A_SUMMARY_PATH = _producer_module.A_SUMMARY_PATH
DevelopmentResearchReviewError = _producer_module.DevelopmentResearchReviewError
build_message_from_file = _producer_module.build_message_from_file
canonical_json = _producer_module.canonical_json
validate_development_research_review = _producer_module.validate_development_research_review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=A_SUMMARY_PATH,
                        help="path to the fixed A summary bytes")
    parser.add_argument("--output", required=True, type=Path,
                        help="destination for the new review message")
    args = parser.parse_args()
    try:
        message = validate_development_research_review(build_message_from_file(args.summary))
    except DevelopmentResearchReviewError as exc:
        print(f"development review rejected: {exc.code}", file=sys.stderr)
        return 2
    fd, temporary = tempfile.mkstemp(prefix=f".{args.output.name}.", dir=args.output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(message) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({
        "status": "built",
        "schema": message["schema"],
        "message_sha256": message["message_sha256"],
        "duplicate_key": message["duplicate_key"],
        "result_digest": message["result_digest"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
