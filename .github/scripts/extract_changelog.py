#!/usr/bin/env python3
"""Print the CHANGELOG.md release-notes body for a given version.

Used by the Release workflow to populate GitHub release notes. Prints the content of the
`## [<version>]` section (without the heading and without the trailing `---` separator).
Falls back to a one-line note if the section is missing, so a release can still publish.

Usage: extract_changelog.py <version> [path-to-changelog]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract(changelog: str, version: str) -> str:
    version = version.lstrip("v")
    # Match the heading line "## [<version>] ..." then capture everything up to the next
    # "## [" heading (or end of file).
    pattern = re.compile(
        r"^##\s*\[" + re.escape(version) + r"\][^\n]*\n(.*?)(?=^##\s*\[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if not match:
        return f"Release {version}."
    body = match.group(1).strip()
    # Drop a trailing horizontal-rule separator between sections, if captured.
    body = re.sub(r"\n-{3,}\s*$", "", body).strip()
    return body or f"Release {version}."


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: extract_changelog.py <version> [changelog_path]", file=sys.stderr)
        return 2
    version = sys.argv[1]
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("CHANGELOG.md")
    print(extract(path.read_text(encoding="utf-8"), version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
