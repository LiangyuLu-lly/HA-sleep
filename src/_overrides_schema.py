"""Forward-compatible defaults for ``/data/web_ui_overrides.json``.

Why this module exists
======================
v2.0.3 persisted only slot bindings (``sleep_stage_source``,
``light_targets``, ...) in ``/data/web_ui_overrides.json``.  v2.1.0 grows
that file with three opt-in / behavior flags:

* ``onboarding_skipped``            — wizard 已完成,首次启动不再弹。
* ``telemetry_enabled``             — opt-in 匿名遥测,默认关闭。
* ``upgrade_notifications_enabled`` — Web UI / HA notification 升级提示,默认开启。

Per design §4.2 + PR3.2, every new field MUST be optional and missing
values MUST fall back to the **privacy-safest** default (wizard re-pops,
telemetry off, upgrade-notifier on).  Multiple components need to agree on
those defaults: ``sleep_classifier/web_ui.py`` reads them at startup,
``src/telemetry_reporter.py`` & ``src/upgrade_notifier.py`` decide whether
to register a task at all, and ``scripts/run_ha_smart_service.py`` wires
the result into the main asyncio loop.

To keep that agreement single-sourced, we centralize the constants and
``apply_v2_1_0_defaults()`` here.  The function is **pure** (no I/O, no
in-place mutation of its argument) so it can be exercised by hypothesis-
style parameter sweeps without filesystem fixtures.
"""
from __future__ import annotations

from typing import Any, Mapping, MutableMapping

# ---------------------------------------------------------------------------
# Defaults — single source of truth.
# ---------------------------------------------------------------------------

#: Onboarding wizard not yet acknowledged → re-pop on next Web UI open.
DEFAULT_ONBOARDING_SKIPPED: bool = False

#: Anonymous telemetry is opt-in (Requirement 6.1, P6.1).
DEFAULT_TELEMETRY_ENABLED: bool = False

#: Upgrade banner is on by default (Requirement 9, opt-out via config).
DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED: bool = True

#: Mapping of v2.1.0 field name → privacy-safe default.  Iteration order is
#: stable (Python 3.7+ dict ordering) so equality checks in tests are
#: deterministic.
V2_1_0_DEFAULTS: Mapping[str, Any] = {
    "onboarding_skipped": DEFAULT_ONBOARDING_SKIPPED,
    "telemetry_enabled": DEFAULT_TELEMETRY_ENABLED,
    "upgrade_notifications_enabled": DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED,
}


def apply_v2_1_0_defaults(
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a NEW dict with v2.1.0 fields backfilled to safe defaults.

    :param data: Existing overrides dict (typically the JSON parsed from
        ``/data/web_ui_overrides.json``) or ``None`` when the file does not
        exist yet.  Treated as read-only — never mutated in place (PR3.1).
    :returns: A fresh ``dict`` whose contents are::

            data ∪ {
                "onboarding_skipped":            data.get(..., False),
                "telemetry_enabled":             data.get(..., False),
                "upgrade_notifications_enabled": data.get(..., True),
            }

        Every v2.0.3 field present in *data* is preserved untouched, so
        callers reading legacy slot bindings keep working without changes.

    The implementation is intentionally trivial (``setdefault`` per key) so
    it is unambiguous what "缺失字段一律 ``.get(key, default)``" means: each
    key is filled iff it is absent from *data*; existing values — including
    explicit ``False`` / empty list / empty string — are never overwritten.
    """
    out: MutableMapping[str, Any] = dict(data) if data is not None else {}
    for key, default in V2_1_0_DEFAULTS.items():
        out.setdefault(key, default)
    return dict(out)
