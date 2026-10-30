#!/usr/bin/env python3
"""Build one local CBA-KB v2.1.1 projection sample (T2-1, Issue #226).

LOCAL ONLY, synthetic only, by construction:
- The only input is the clean-room fixture (build_cleanroom_fixture).
  This script accepts no real data, no Drive path, no credentials,
  and no network address. It cannot publish to Drive, change an ACL,
  or move a current pointer.
- Output goes only to --output-dir (a local directory).
- The script exits non-zero unless verify_local_projection() passes
  on the directory it just wrote.

Usage:
  python3 scripts/build_local_projection.py --output-dir /path/to/dir
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from cba_kb.kc_cleanroom import build_cleanroom_fixture  # noqa: E402
from cba_kb.kc_local_producer import (  # noqa: E402
    build_local_projection,
    verify_local_projection,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a local-only synthetic projection sample.")
    parser.add_argument("--output-dir", required=True,
                        help="Local output directory (not Drive).")
    parser.add_argument("--target-domain", default="fixture_target",
                        help="Synthetic target domain (default: fixture_target).")
    args = parser.parse_args()
    fixture = build_cleanroom_fixture()
    summary = build_local_projection(
        fixture=fixture, output_dir=args.output_dir,
        target_domain=args.target_domain)
    result = verify_local_projection(args.output_dir)
    print(json.dumps({"summary": summary, "verification": result},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
