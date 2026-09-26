#!/usr/bin/env python3
"""Build one locally verifiable qsl.development_research_review.v1 message."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from service.development_research_review import (  # noqa: E402
    A_SUMMARY_PATH,
    DevelopmentResearchReviewError,
    build_message_from_file,
    canonical_json,
    validate_development_research_review,
)


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
    args.output.write_text(canonical_json(message) + "\n", encoding="utf-8")
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
