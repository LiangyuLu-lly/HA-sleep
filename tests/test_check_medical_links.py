"""Tests for ``scripts/check_medical_links.py`` — Property 9: 医疗免责链接可达性.

The script enforces design §3.5 / Requirements 5.6, 5.7, 14.4: any
paragraph in ``README.md``, ``sleep_classifier/DOCS.md``, or ``docs/*.md``
that triggers the medical keyword regex must sit within +/-1 paragraphs
of a *relative* markdown link to ``MEDICAL_DISCLAIMER.md``. Absolute
URLs (``https://...MEDICAL_DISCLAIMER.md``) do NOT satisfy the rule
because the disclaimer must resolve in the in-repo + Add-on doc renderer.

This suite exercises the pure paragraph-window function
``check_paragraph_window`` directly — the unit is the design contract
the CLI is built on top of, so testing it gives us full coverage of the
positive and negative cases without coupling to file I/O.

Validates: Requirements 5.6, 5.7
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``scripts/`` importable so we can call the public API directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_medical_links  # noqa: E402  (sys.path manipulation above)


# ---------------------------------------------------------------------------
# Convenience aliases — keeps each parametrise row self-explanatory.
# ---------------------------------------------------------------------------

# A relative markdown link that *does* satisfy the rule (the script
# accepts plain "MEDICAL_DISCLAIMER.md" or any non-absolute path that
# ends in that filename).
_REL_LINK = "See [医疗免责声明](MEDICAL_DISCLAIMER.md) for details."
_REL_LINK_NESTED = "Refer to [legal](../MEDICAL_DISCLAIMER.md) before use."
_REL_LINK_ANCHOR = "[disclaimer](MEDICAL_DISCLAIMER.md#scope) covers this."

# Absolute URLs that point to the same filename — these must NOT satisfy
# the rule because the requirement is for an in-repo relative link.
_ABS_LINK_HTTPS = (
    "Read [the disclaimer](https://example.com/MEDICAL_DISCLAIMER.md) please."
)
_ABS_LINK_PROTOCOL_RELATIVE = (
    "Read [the disclaimer](//example.com/MEDICAL_DISCLAIMER.md) please."
)

# Generic non-disclaimer paragraph (no medical keyword, no link).
_NEUTRAL = (
    "This paragraph is plainly about scheduling backups and "
    "does not mention anything sensitive."
)

# Paragraphs that DO trigger the medical keyword regex, in en + zh,
# covering each branch of the alternation in MEDICAL_KEYWORD_PATTERN.
_MED_EN_MEDICAL = (
    "Note: this is not medical advice and the maintainer is not a clinician."
)
_MED_EN_DIAGNOSE = (
    "Sleep Classifier does not diagnose any condition; it only summarises "
    "data your wearable already reports."
)
_MED_EN_APNEA_HYPHEN = (
    "The optional apnea_detector module is a research PoC and is "
    "not intended for sleep-apnea screening."
)
_MED_EN_APNEA_SPACE = (
    "The optional apnea_detector module is a research PoC and is "
    "not intended for sleep apnea screening."
)
_MED_ZH_MEDICAL = "本项目不提供任何医学建议，也不替代专业临床判断。"
_MED_ZH_DIAGNOSE = "Sleep Classifier 不做诊断，只对可穿戴上报的睡眠数据做统计。"
_MED_ZH_APNEA = "可选 apnea_detector 模块仅为研究 PoC，不能用于呼吸暂停筛查。"


# ---------------------------------------------------------------------------
# 1. Negative path: medical keyword + no nearby disclaimer link
#    ⇒ check_paragraph_window must return False.
# ---------------------------------------------------------------------------

_VIOLATION_CASES = [
    # Medical paragraph standing entirely alone, no link anywhere.
    pytest.param(
        [_MED_EN_MEDICAL],
        0,
        id="lone_medical_keyword_no_link",
    ),
    pytest.param(
        [_MED_ZH_MEDICAL],
        0,
        id="lone_medical_keyword_zh_no_link",
    ),
    # Medical paragraph in the middle, neighbours are neutral chatter.
    pytest.param(
        [_NEUTRAL, _MED_EN_DIAGNOSE, _NEUTRAL],
        1,
        id="diagnose_keyword_neutral_neighbours",
    ),
    pytest.param(
        [_NEUTRAL, _MED_ZH_DIAGNOSE, _NEUTRAL],
        1,
        id="zh_diagnose_keyword_neutral_neighbours",
    ),
    # Disclaimer link exists but lives outside the +/-1 window.
    pytest.param(
        [_REL_LINK, _NEUTRAL, _MED_EN_APNEA_HYPHEN, _NEUTRAL, _NEUTRAL],
        2,
        id="link_exists_but_two_paragraphs_away",
    ),
    pytest.param(
        [_NEUTRAL, _NEUTRAL, _MED_ZH_APNEA, _NEUTRAL, _REL_LINK],
        2,
        id="link_exists_but_two_paragraphs_after",
    ),
    # Absolute URLs do NOT satisfy the rule — even if right next to the
    # medical paragraph.
    pytest.param(
        [_MED_EN_MEDICAL, _ABS_LINK_HTTPS],
        0,
        id="absolute_https_link_does_not_satisfy",
    ),
    pytest.param(
        [_ABS_LINK_PROTOCOL_RELATIVE, _MED_EN_DIAGNOSE],
        1,
        id="protocol_relative_link_does_not_satisfy",
    ),
    # English + Chinese mix in the same window: keyword in zh, only abs
    # link nearby.
    pytest.param(
        [_NEUTRAL, _MED_ZH_DIAGNOSE, _ABS_LINK_HTTPS],
        1,
        id="zh_keyword_with_only_absolute_link",
    ),
    # Each apnea spelling variant must be detected as a violation when the
    # disclaimer link is missing.
    pytest.param(
        [_MED_EN_APNEA_SPACE],
        0,
        id="sleep_space_apnea_keyword",
    ),
    pytest.param(
        [_MED_EN_APNEA_HYPHEN],
        0,
        id="sleep_hyphen_apnea_keyword",
    ),
]


@pytest.mark.parametrize(("paragraphs", "idx"), _VIOLATION_CASES)
def test_violation_returns_false(
    paragraphs: list[str],
    idx: int,
) -> None:
    """Medical keyword + no nearby relative disclaimer link ⇒ False."""
    assert check_medical_links.check_paragraph_window(paragraphs, idx) is False


# ---------------------------------------------------------------------------
# 2. Positive path: medical keyword + relative link inside the +/-1 window
#    ⇒ check_paragraph_window must return True.
# ---------------------------------------------------------------------------

_COMPLIANT_CASES = [
    # Link right above.
    pytest.param(
        [_REL_LINK, _MED_EN_MEDICAL],
        1,
        id="link_directly_above_keyword",
    ),
    # Link right below.
    pytest.param(
        [_MED_EN_DIAGNOSE, _REL_LINK],
        0,
        id="link_directly_below_keyword",
    ),
    # Link in the same paragraph as the medical keyword.
    pytest.param(
        [
            _MED_EN_APNEA_HYPHEN
            + "  See [the disclaimer](MEDICAL_DISCLAIMER.md) for limits.",
        ],
        0,
        id="link_in_same_paragraph",
    ),
    # Window neighbour with a nested relative path link.
    pytest.param(
        [_NEUTRAL, _MED_ZH_APNEA, _REL_LINK_NESTED],
        1,
        id="zh_keyword_with_nested_relative_link",
    ),
    # Link with anchor fragment is still relative.
    pytest.param(
        [_REL_LINK_ANCHOR, _MED_ZH_DIAGNOSE],
        1,
        id="link_with_anchor_above",
    ),
    # English keyword, zh disclaimer link below — language mix is fine.
    pytest.param(
        [_NEUTRAL, _MED_EN_MEDICAL, _REL_LINK],
        1,
        id="en_keyword_zh_link_one_below",
    ),
]


@pytest.mark.parametrize(("paragraphs", "idx"), _COMPLIANT_CASES)
def test_compliant_returns_true(paragraphs: list[str], idx: int) -> None:
    """Medical keyword + relative disclaimer link in window ⇒ True."""
    assert check_medical_links.check_paragraph_window(paragraphs, idx) is True


# ---------------------------------------------------------------------------
# 3. Non-medical paragraphs short-circuit to True — no link required.
# ---------------------------------------------------------------------------

_NON_MEDICAL_CASES = [
    pytest.param([_NEUTRAL], 0, id="neutral_paragraph_alone"),
    pytest.param(
        [_NEUTRAL, _NEUTRAL, _NEUTRAL],
        1,
        id="neutral_paragraph_in_window_of_neutrals",
    ),
    pytest.param(
        [_REL_LINK, _NEUTRAL, _NEUTRAL],
        2,
        id="neutral_paragraph_with_unrelated_link",
    ),
    pytest.param(
        ["A paragraph about *aphorisms*, nothing remotely sensitive here."],
        0,
        id="paragraph_with_close_but_not_keyword_word",
    ),
]


@pytest.mark.parametrize(("paragraphs", "idx"), _NON_MEDICAL_CASES)
def test_non_medical_paragraphs_pass_without_link(
    paragraphs: list[str], idx: int
) -> None:
    """A paragraph not matching the medical regex never requires a link."""
    assert check_medical_links.check_paragraph_window(paragraphs, idx) is True


# ---------------------------------------------------------------------------
# 4. Window radius boundary: file edges must clip cleanly.
# ---------------------------------------------------------------------------


def test_window_radius_clips_at_start_of_file() -> None:
    """idx=0 must consider only paragraphs[0..1] (no negative slice)."""
    paragraphs = [_MED_EN_MEDICAL, _REL_LINK]
    assert check_medical_links.check_paragraph_window(paragraphs, 0) is True


def test_window_radius_clips_at_end_of_file() -> None:
    """idx=len-1 must consider only paragraphs[len-2..len-1]."""
    paragraphs = [_REL_LINK, _MED_EN_DIAGNOSE]
    assert check_medical_links.check_paragraph_window(paragraphs, 1) is True


def test_window_radius_does_not_reach_two_paragraphs_away_at_start() -> None:
    """idx=0 with link at idx=2 must still report a violation."""
    paragraphs = [_MED_EN_MEDICAL, _NEUTRAL, _REL_LINK]
    assert check_medical_links.check_paragraph_window(paragraphs, 0) is False


def test_window_radius_does_not_reach_two_paragraphs_away_at_end() -> None:
    """idx=N-1 with link at idx=N-3 must still report a violation."""
    paragraphs = [_REL_LINK, _NEUTRAL, _MED_ZH_APNEA]
    assert check_medical_links.check_paragraph_window(paragraphs, 2) is False


# ---------------------------------------------------------------------------
# 5. End-to-end CLI smoke: feed real paragraphs through main([repo_root]).
#    Anchors the unit-level guarantees to the public CLI surface so a
#    refactor that breaks the wiring can not silently slip past.
# ---------------------------------------------------------------------------


def _write_doc(repo_root: Path, relative: str, content: str) -> None:
    target = repo_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_main_passes_when_every_medical_paragraph_has_relative_link(
    tmp_path: Path,
) -> None:
    """README + DOCS + docs/*.md all compliant ⇒ exit 0."""
    compliant_md = (
        f"{_NEUTRAL}\n\n"
        f"{_MED_EN_MEDICAL}\n\n"
        f"{_REL_LINK}\n"
    )
    _write_doc(tmp_path, "README.md", compliant_md)
    _write_doc(
        tmp_path,
        "sleep_classifier/DOCS.md",
        f"{_REL_LINK}\n\n{_MED_ZH_APNEA}\n\n{_NEUTRAL}\n",
    )
    _write_doc(
        tmp_path,
        "docs/PRIVACY.md",
        f"{_NEUTRAL}\n\n{_MED_EN_DIAGNOSE} {_REL_LINK_ANCHOR}\n",
    )

    rc = check_medical_links.main(["--repo-root", str(tmp_path)])
    assert rc == 0


def test_main_fails_when_one_paragraph_lacks_relative_link(
    tmp_path: Path,
) -> None:
    """A single non-compliant window anywhere ⇒ exit non-zero."""
    _write_doc(
        tmp_path,
        "README.md",
        f"{_NEUTRAL}\n\n{_REL_LINK}\n\n{_MED_EN_MEDICAL}\n\n{_NEUTRAL}\n",
    )
    # Violation lives in DOCS.md: the disclaimer link sits 2 paragraphs
    # away (outside the +/-1 window) so the script must reject it.
    _write_doc(
        tmp_path,
        "sleep_classifier/DOCS.md",
        f"{_REL_LINK}\n\n{_NEUTRAL}\n\n{_MED_ZH_DIAGNOSE}\n",
    )

    rc = check_medical_links.main(["--repo-root", str(tmp_path)])
    assert rc != 0


def test_main_fails_when_only_absolute_url_present(tmp_path: Path) -> None:
    """An absolute URL to MEDICAL_DISCLAIMER.md must not satisfy the rule."""
    _write_doc(
        tmp_path,
        "README.md",
        f"{_MED_EN_MEDICAL}\n\n{_ABS_LINK_HTTPS}\n",
    )
    rc = check_medical_links.main(["--repo-root", str(tmp_path)])
    assert rc != 0
