"""Property 5: GitHub release version comparison semantics.

Parametrized exhaustive tests for UpgradeNotifier.is_newer() covering
PEP 440 strings (pre-release, post versions, malformed strings).

**Validates: Requirements 9.2**
"""
from __future__ import annotations

import itertools

import pytest

from src.upgrade_notifier import UpgradeNotifier

is_newer = UpgradeNotifier.is_newer

# ---------------------------------------------------------------------------
# Test data: valid PEP 440 version strings in ascending order
# ---------------------------------------------------------------------------

ORDERED_VERSIONS = [
    "1.0.0a1",
    "1.0.0a2",
    "1.0.0b1",
    "1.0.0rc1",
    "1.0.0",
    "1.0.0.post1",
    "1.0.1",
    "1.1.0",
    "2.0.0.dev1",
    "2.0.0a1",
    "2.0.0",
    "2.0.0.post1",
    "2.0.0.post2",
    "2.1.0",
    "2.1.1",
    "3.0.0rc1",
    "3.0.0",
    "10.0.0",
]

# Malformed strings that should always yield False
# Note: packaging library is quite permissive; only truly invalid strings here
MALFORMED = [
    "",
    "not-a-version",
    "abc",
    "latest",
    "$$garbage$$",
    "version-2.0",
    "hello world",
    "v",
]

# Version pairs where `latest` is strictly newer than `current`
NEWER_PAIRS = [
    ("1.0.0", "1.0.1"),
    ("1.0.0", "2.0.0"),
    ("2.0.0a1", "2.0.0"),
    ("2.0.0", "2.0.0.post1"),
    ("1.0.0rc1", "1.0.0"),
    ("1.0.0a1", "1.0.0b1"),
    ("2.1.0", "2.1.1"),
    ("2.1.0", "3.0.0"),
    ("1.0.0.post1", "1.0.1"),
    ("2.0.0.dev1", "2.0.0a1"),
]


# ---------------------------------------------------------------------------
# (a) Anti-symmetry: is_newer(a, b)=True => is_newer(b, a)=False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current,latest", NEWER_PAIRS)
def test_anti_symmetric_true_pairs(current: str, latest: str) -> None:
    """If is_newer(a, b) is True, then is_newer(b, a) must be False."""
    assert is_newer(current, latest) is True
    assert is_newer(latest, current) is False


@pytest.mark.parametrize(
    "a,b",
    [(a, b) for i, a in enumerate(ORDERED_VERSIONS) for b in ORDERED_VERSIONS[i + 1:]],
)
def test_anti_symmetric_ordered_versions(a: str, b: str) -> None:
    """For all ordered pairs from ORDERED_VERSIONS: is_newer(a,b)=True => is_newer(b,a)=False."""
    if is_newer(a, b):
        assert is_newer(b, a) is False


# ---------------------------------------------------------------------------
# (b) Consistency: current == latest => False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ORDERED_VERSIONS)
def test_same_version_returns_false(version: str) -> None:
    """is_newer(v, v) must always return False."""
    assert is_newer(version, version) is False


@pytest.mark.parametrize("version", MALFORMED)
def test_same_malformed_version_returns_false(version: str) -> None:
    """is_newer(v, v) for malformed versions must return False."""
    assert is_newer(version, version) is False


# ---------------------------------------------------------------------------
# (c) Transitivity: for <= 50 enumerated triples
#     is_newer(a, b) AND is_newer(b, c) => is_newer(a, c)
# ---------------------------------------------------------------------------


def _generate_transitivity_triples() -> list[tuple[str, str, str]]:
    """Generate up to 50 triples from ORDERED_VERSIONS to test transitivity."""
    triples = []
    for a, b, c in itertools.combinations(ORDERED_VERSIONS, 3):
        if is_newer(a, b) and is_newer(b, c):
            triples.append((a, b, c))
        if len(triples) >= 50:
            break
    return triples


@pytest.mark.parametrize("a,b,c", _generate_transitivity_triples())
def test_transitivity(a: str, b: str, c: str) -> None:
    """is_newer(a, b) AND is_newer(b, c) implies is_newer(a, c)."""
    assert is_newer(a, c) is True


# ---------------------------------------------------------------------------
# Malformed string pairs: always return False (conservative)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("malformed", MALFORMED)
def test_malformed_as_current(malformed: str) -> None:
    """Malformed current version always returns False."""
    assert is_newer(malformed, "2.1.0") is False


@pytest.mark.parametrize("malformed", MALFORMED)
def test_malformed_as_latest(malformed: str) -> None:
    """Malformed latest version always returns False."""
    assert is_newer("2.1.0", malformed) is False


@pytest.mark.parametrize(
    "a,b",
    list(itertools.combinations(MALFORMED, 2))[:20],
)
def test_both_malformed(a: str, b: str) -> None:
    """Two malformed versions always return False."""
    assert is_newer(a, b) is False
    assert is_newer(b, a) is False
