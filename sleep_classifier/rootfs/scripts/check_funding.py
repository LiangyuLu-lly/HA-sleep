"""Verify the README sponsor badge owner matches ``.github/FUNDING.yml``.

CI guard for spec ``commercial-readiness-v2.1.0`` Requirement 10. The check
is deliberately narrow: every ``https://github.com/sponsors/<OWNER>`` URL
referenced from ``README.md`` must point at an owner declared under the
``github:`` key of ``.github/FUNDING.yml``. Any drift maps to a non-zero
exit code so the CI ``check_funding`` step blocks the merge.

The YAML ``github:`` field may be either a single string or a list of
strings (per the GitHub Sponsors funding file format), and both shapes are
accepted here.

Exit codes
----------
* ``0`` — every README sponsor URL owner is declared in FUNDING.yml
* ``1`` — one or more README owners do not match FUNDING.yml, or README
  references zero sponsor URLs while FUNDING.yml declares some
* ``2`` — required input file is missing, malformed, or has the wrong
  top-level shape (intentional behaviour while ``README.md`` /
  ``.github/FUNDING.yml`` are still being authored by tasks 2.8 and 2.9 —
  CI fails fast rather than silently passing)

Usage
-----
::

    python scripts/check_funding.py
    python scripts/check_funding.py --repo-root /tmp/fixture-repo

The script has no runtime side effects — pure read + diff — so it is safe
to invoke from any working directory.

Acceptance criteria covered: Requirements 10.1, 10.3.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml


_REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
_FUNDING_RELPATH = Path(".github") / "FUNDING.yml"
_README_RELPATH = Path("README.md")

# GitHub usernames: alphanumeric or hyphen, must start with alphanumeric,
# 1-39 chars (per GitHub's published constraints). The character class is
# kept conservative; the script's job is comparison, not validation, so we
# only need to delimit URL boundaries reliably.
_SPONSOR_URL_PATTERN: re.Pattern[str] = re.compile(
    r"https://github\.com/sponsors/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))",
)

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_MISSING_OR_MALFORMED = 2


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    """Load ``path`` as a YAML mapping.

    Raises :class:`FileNotFoundError` if ``path`` is missing and
    :class:`ValueError` for parse errors or non-mapping top-level shape.
    """
    if not path.is_file():
        raise FileNotFoundError(f"required file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"failed to parse YAML at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValueError(
            f"top-level YAML at {path} is not a mapping (found "
            f"{type(data).__name__})"
        )
    return data


def load_funding_github_owners(repo_root: Path) -> frozenset[str]:
    """Return the set of GitHub Sponsors owners declared in FUNDING.yml.

    Accepts either ``github: <string>`` or ``github: [<string>, ...]``.
    Raises :class:`ValueError` if the field is missing, empty, or has an
    unexpected shape.
    """
    funding = _load_yaml_mapping(repo_root / _FUNDING_RELPATH)
    github_field = funding.get("github")

    if github_field is None:
        raise ValueError(
            f"{_FUNDING_RELPATH}: missing 'github:' field "
            "(required for sponsor badge consistency)"
        )

    if isinstance(github_field, str):
        owners = [github_field.strip()]
    elif isinstance(github_field, list):
        owners = [str(item).strip() for item in github_field]
    else:
        raise ValueError(
            f"{_FUNDING_RELPATH}: 'github:' must be a string or list of "
            f"strings (found {type(github_field).__name__})"
        )

    owners = [o for o in owners if o]
    if not owners:
        raise ValueError(
            f"{_FUNDING_RELPATH}: 'github:' is empty after stripping; "
            "declare at least one sponsor account"
        )
    return frozenset(owners)


def extract_readme_sponsor_owners(repo_root: Path) -> frozenset[str]:
    """Return the set of owners referenced by README sponsor URLs.

    Raises :class:`FileNotFoundError` if README.md is missing.
    """
    readme = repo_root / _README_RELPATH
    if not readme.is_file():
        raise FileNotFoundError(f"required file not found: {readme}")
    text = readme.read_text(encoding="utf-8")
    return frozenset(match.group(1) for match in _SPONSOR_URL_PATTERN.finditer(text))


def diff_owners(
    funding_owners: frozenset[str],
    readme_owners: frozenset[str],
) -> dict[str, list[str]]:
    """Compute owner drift between README sponsor URLs and FUNDING.yml.

    The result reports two disjoint groups (omitted when empty):

    * ``readme_not_in_funding`` — owners referenced from README sponsor URLs
      that are *not* declared in ``.github/FUNDING.yml``. This is the
      primary failure mode (a stray badge URL pointing at the wrong owner).
    * ``readme_missing_badge`` — populated only when README contains zero
      sponsor URLs while FUNDING.yml declares one or more owners. In that
      case all funding owners are listed for the operator's reference.
    """
    report: dict[str, list[str]] = {}

    stray = sorted(readme_owners - funding_owners)
    if stray:
        report["readme_not_in_funding"] = stray

    if not readme_owners and funding_owners:
        report["readme_missing_badge"] = sorted(funding_owners)

    return report


def _format_drift_report(
    repo_root: Path,
    funding_owners: frozenset[str],
    readme_owners: frozenset[str],
    drift: Mapping[str, list[str]],
) -> str:
    """Render a human-readable owner drift summary."""
    lines: list[str] = [
        "Sponsor badge / FUNDING.yml owner mismatch.",
        f"FUNDING.yml: {sorted(funding_owners)} ({repo_root / _FUNDING_RELPATH})",
        f"README.md sponsor URLs: {sorted(readme_owners)} "
        f"({repo_root / _README_RELPATH})",
        "",
    ]
    if "readme_not_in_funding" in drift:
        lines.append(
            "  README sponsor URLs reference owners not listed in FUNDING.yml:"
        )
        for owner in drift["readme_not_in_funding"]:
            lines.append(f"    - {owner}")
        lines.append("")
    if "readme_missing_badge" in drift:
        lines.append(
            "  README contains no GitHub Sponsors badge URL, but "
            "FUNDING.yml declares:"
        )
        for owner in drift["readme_missing_badge"]:
            lines.append(f"    - {owner}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that every https://github.com/sponsors/<owner> URL in "
            "README.md points at an owner declared under 'github:' in "
            ".github/FUNDING.yml."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT_DEFAULT,
        help=(
            "Repository root containing README.md and .github/FUNDING.yml. "
            "Defaults to the checkout this script lives in."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns one of ``EXIT_*`` constants."""
    args = _build_parser().parse_args(argv)
    repo_root: Path = args.repo_root.resolve()

    try:
        funding_owners = load_funding_github_owners(repo_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MISSING_OR_MALFORMED

    try:
        readme_owners = extract_readme_sponsor_owners(repo_root)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MISSING_OR_MALFORMED

    drift = diff_owners(funding_owners, readme_owners)
    if drift:
        print(
            _format_drift_report(
                repo_root, funding_owners, readme_owners, drift
            ),
            file=sys.stderr,
        )
        return EXIT_MISMATCH

    print(
        "OK: README sponsor badge owner(s) "
        f"{sorted(readme_owners)} match FUNDING.yml github: "
        f"{sorted(funding_owners)}."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
