"""Verify medical disclaimer links sit next to every medical keyword.

CI guard for spec ``commercial-readiness-v2.1.0`` Requirement 5.6 / 5.7 /
14.4 (Property 9 in design.md). The rule comes straight from design §3.5:

    For any paragraph in README.md, sleep_classifier/DOCS.md, or
    docs/*.md that matches the medical keyword regex, the surrounding
    paragraph window (the matched paragraph plus its immediate
    neighbours -- max one paragraph each side) must contain at least
    one *relative* markdown link to ``MEDICAL_DISCLAIMER.md``.

The check is a paragraph-window grep, not a generic markdown-link checker
(``markdown-link-check`` already covers dead-link detection). Splitting on
blank lines is intentional and matches how Markdown defines paragraphs.

Pure function
-------------
``check_paragraph_window(paragraphs, idx) -> bool`` is exposed for unit
tests (task 1.9). It returns ``True`` when the paragraph at ``idx`` is
**compliant**:

* the paragraph does not match the medical keyword pattern, **or**
* the window ``[idx - 1, idx, idx + 1]`` (clipped at file boundaries)
  contains at least one relative markdown link to ``MEDICAL_DISCLAIMER.md``.

Exit codes
----------
* ``0`` -- every scanned paragraph window is compliant
* ``1`` -- at least one window is missing the required disclaimer link

Missing target files (e.g., ``docs/`` not yet authored) are not fatal;
``MEDICAL_DISCLAIMER.md`` itself does not need to exist for the script to
run -- this guard is intentionally landable before task 2.3 / 2.9 / 2.10
populate the legal docs and README sections. When no scannable file is
found the script prints a notice to stderr but still exits ``0`` so the
CI step does not silently pass-through; the unit tests in task 1.9
validate the actual paragraph-window logic against constructed
fixtures, independent of repo state.

Usage
-----
::

    python scripts/check_medical_links.py
    python scripts/check_medical_links.py --repo-root /tmp/fixture-repo

The script has no runtime side effects -- pure read + diff -- so it is
safe to invoke from any working directory.

Acceptance criteria covered: Requirements 5.6, 5.7, 14.4.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple


_REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent

# Files always scanned by canonical name (skipped silently if absent).
_NAMED_TARGETS: tuple[Path, ...] = (
    Path("README.md"),
    Path("sleep_classifier") / "DOCS.md",
)

# Glob patterns expanded at scan time.
_GLOB_TARGETS: tuple[tuple[Path, str], ...] = (
    (Path("docs"), "*.md"),
)

# Window radius around the matched paragraph. ``1`` means the window
# spans ``[idx - 1, idx, idx + 1]``, clipped at the file boundaries.
WINDOW_RADIUS: int = 1

# Medical keyword regex -- mirrors design §3.5 / Property 9 verbatim.
# Case-insensitive so e.g. "Medical" / "MEDICAL" / "medical" all match.
MEDICAL_KEYWORD_PATTERN: re.Pattern[str] = re.compile(
    r"medical|医学|诊断|diagnose|sleep[\s-]apnea|呼吸暂停",
    re.IGNORECASE,
)

# Markdown link with a *relative* href whose path component contains
# ``MEDICAL_DISCLAIMER.md``. Absolute URLs (``http://``, ``https://``,
# protocol-relative ``//``, ``ftp://``) are explicitly excluded -- the
# requirement is for an in-repo relative link that resolves both on
# GitHub and inside the HA Add-on documentation renderer.
DISCLAIMER_LINK_PATTERN: re.Pattern[str] = re.compile(
    r"""
    \[                                  # link text opener
    [^\]]*                              # link text (no nested ])
    \]
    \(                                  # URL opener
    \s*                                 # tolerate leading whitespace
    (?!https?://|ftp://|//)             # NOT an absolute URL
    [^)\s]*                             # optional relative path prefix
    MEDICAL_DISCLAIMER\.md              # the file we require
    (?:[\#\?][^)\s]*)?                  # optional anchor or query
    [^)]*                               # tolerate optional title trailer
    \)                                  # URL closer
    """,
    re.IGNORECASE | re.VERBOSE,
)


def split_paragraphs(text: str) -> list[str]:
    """Split markdown ``text`` into paragraphs by blank lines.

    A blank line may contain only whitespace. Paragraph order is preserved
    so callers can rely on stable indices for the window check. Empty /
    whitespace-only paragraphs are dropped.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"\n[ \t]*\n+", normalized)
    return [chunk for chunk in (p.strip("\n") for p in parts) if chunk.strip()]


def paragraph_has_medical_keyword(paragraph: str) -> bool:
    """Return ``True`` iff ``paragraph`` triggers the medical keyword regex."""
    return MEDICAL_KEYWORD_PATTERN.search(paragraph) is not None


def window_has_disclaimer_link(paragraphs: list[str], idx: int) -> bool:
    """Return ``True`` iff the paragraph window around ``idx`` contains a
    relative markdown link to ``MEDICAL_DISCLAIMER.md``.

    The window is
    ``paragraphs[max(0, idx - WINDOW_RADIUS) : idx + WINDOW_RADIUS + 1]``,
    i.e., the matched paragraph plus up to ``WINDOW_RADIUS`` neighbours on
    each side, clipped at file boundaries.
    """
    start = max(0, idx - WINDOW_RADIUS)
    stop = idx + WINDOW_RADIUS + 1
    return any(
        DISCLAIMER_LINK_PATTERN.search(p) is not None
        for p in paragraphs[start:stop]
    )


def check_paragraph_window(paragraphs: list[str], idx: int) -> bool:
    """Pure paragraph-window compliance check exposed for unit tests.

    Returns ``True`` when the paragraph at ``idx`` is **compliant**: either
    it does not contain a medical keyword (no link required), or its
    surrounding window contains at least one relative markdown link to
    ``MEDICAL_DISCLAIMER.md``.

    :param paragraphs: ordered list of paragraphs (typically the output
        of :func:`split_paragraphs`).
    :param idx: index of the paragraph being inspected. Out-of-range
        indices propagate the natural :class:`IndexError`.
    """
    if not paragraph_has_medical_keyword(paragraphs[idx]):
        return True
    return window_has_disclaimer_link(paragraphs, idx)


class _Violation(NamedTuple):
    """Internal record describing a non-compliant paragraph window."""

    file: Path
    paragraph_index: int
    snippet: str


def _iter_target_files(repo_root: Path) -> Iterator[Path]:
    """Yield existing files matching the configured named + glob targets."""
    for relative in _NAMED_TARGETS:
        candidate = repo_root / relative
        if candidate.is_file():
            yield candidate
    for directory, glob_pattern in _GLOB_TARGETS:
        target_dir = repo_root / directory
        if not target_dir.is_dir():
            continue
        for path in sorted(target_dir.glob(glob_pattern)):
            if path.is_file():
                yield path


def _scan_file(path: Path) -> list[_Violation]:
    """Return the list of paragraph windows in ``path`` missing the link."""
    text = path.read_text(encoding="utf-8")
    paragraphs = split_paragraphs(text)
    violations: list[_Violation] = []
    for idx, paragraph in enumerate(paragraphs):
        if not paragraph_has_medical_keyword(paragraph):
            continue
        if window_has_disclaimer_link(paragraphs, idx):
            continue
        first_line = paragraph.splitlines()[0] if paragraph else ""
        snippet = first_line.strip()[:120]
        violations.append(_Violation(path, idx, snippet))
    return violations


def _format_violations(
    repo_root: Path,
    violations: Iterable[_Violation],
) -> str:
    """Render a human-readable summary of non-compliant paragraph windows."""
    lines = [
        "Medical disclaimer link missing in the following paragraph windows:",
        "",
    ]
    for violation in violations:
        try:
            rel = violation.file.relative_to(repo_root).as_posix()
        except ValueError:
            rel = str(violation.file)
        lines.append(
            f"  {rel} [paragraph #{violation.paragraph_index}]: "
            f"{violation.snippet!r}"
        )
    lines.append("")
    lines.append(
        "Add a relative markdown link to MEDICAL_DISCLAIMER.md in the matched "
        "paragraph or one of its immediate neighbours (window radius "
        f"{WINDOW_RADIUS})."
    )
    return "\n".join(lines).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every paragraph mentioning a medical keyword "
            "(in README.md, sleep_classifier/DOCS.md, docs/*.md) sits "
            "within +/-1 paragraphs of a relative link to "
            "MEDICAL_DISCLAIMER.md."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT_DEFAULT,
        help=(
            "Repository root containing README.md, sleep_classifier/, "
            "docs/. Defaults to the checkout this script lives in."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns ``0`` on success or ``1`` on violation."""
    args = _build_parser().parse_args(argv)
    repo_root: Path = args.repo_root.resolve()

    all_violations: list[_Violation] = []
    scanned_files: list[Path] = []
    for path in _iter_target_files(repo_root):
        scanned_files.append(path)
        try:
            all_violations.extend(_scan_file(path))
        except OSError as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)
            continue

    if all_violations:
        print(
            _format_violations(repo_root, all_violations),
            file=sys.stderr,
        )
        return 1

    if not scanned_files:
        print(
            "check_medical_links: no scannable files found "
            "(README.md / sleep_classifier/DOCS.md / docs/*.md absent).",
            file=sys.stderr,
        )
        return 0

    rels = ", ".join(
        sorted(p.relative_to(repo_root).as_posix() for p in scanned_files)
    )
    print(
        f"OK: {len(scanned_files)} file(s) scanned, no medical-disclaimer "
        f"link gaps detected [{rels}]."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
