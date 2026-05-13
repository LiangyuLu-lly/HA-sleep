"""Export a sanitised diagnostic JSON to stdout.

Reads persisted state files from the Add-on's ``/data`` volume (or the
project root ``data/`` for local dev) and emits a JSON object containing:

- ``n_sessions``: number of recorded sleep sessions
- ``last_session_at``: ISO timestamp of the most recent session
- ``learner_status``: current learner status string
- ``apnea_baseline``: apnea baseline summary (if present)
- ``config_summary``: area / sleep_stage_source / dry_run / wind_down_minutes
- ``version``: "2.0.0"

No tokens, passwords, or personally-identifiable information are included.

Usage (inside the Add-on container)::

    python scripts/diagnostic_export.py

Usage (local dev)::

    python scripts/diagnostic_export.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


VERSION = "2.0.0"

# Persistence root: /data inside the Add-on container, otherwise project root.
_DATA_DIR = Path("/data") if Path("/data").is_dir() else Path(__file__).resolve().parent.parent / "data"


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load a JSON file, returning None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _extract_preferences(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract session count and last session timestamp from preferences."""
    if data is None:
        return {"n_sessions": 0, "last_session_at": None, "learner_status": "no_data"}

    sessions = data.get("sessions", [])
    n_sessions = len(sessions)
    last_session_at: Optional[str] = None
    if sessions:
        last = sessions[-1]
        ended_at = last.get("ended_at")
        if ended_at is not None:
            try:
                last_session_at = datetime.fromtimestamp(
                    float(ended_at), tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OSError):
                last_session_at = str(ended_at)

    # Derive learner status from session count.
    if n_sessions == 0:
        learner_status = "no_data"
    elif n_sessions < 3:
        learner_status = "collecting"
    elif n_sessions < 14:
        learner_status = "learning"
    else:
        learner_status = "personalised"

    return {
        "n_sessions": n_sessions,
        "last_session_at": last_session_at,
        "learner_status": learner_status,
    }


def _extract_apnea(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extract apnea baseline summary (no raw samples)."""
    if data is None:
        return None
    return {
        "calibration_nights": data.get("calibration_nights"),
        "status": data.get("status"),
    }


def _extract_config(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract only safe config fields."""
    if data is None:
        return {}
    ha_cfg = data.get("home_assistant", data)
    api_cfg = ha_cfg.get("api", ha_cfg) if isinstance(ha_cfg, dict) else {}
    smart_ctrl = ha_cfg.get("smart_control", {}) if isinstance(ha_cfg, dict) else {}
    return {
        "area": api_cfg.get("area", ""),
        "sleep_stage_source": api_cfg.get("sleep_stage_source", ""),
        "dry_run": smart_ctrl.get("dry_run", True),
        "wind_down_minutes": smart_ctrl.get("wind_down_minutes", 30),
    }


def main() -> int:
    """Build and print the diagnostic JSON."""
    prefs_path = _DATA_DIR / "user_preferences.json"
    apnea_path = _DATA_DIR / "apnea_baseline.json"
    config_path = _DATA_DIR / "effective_config.json"

    prefs_data = _load_json(prefs_path)
    apnea_data = _load_json(apnea_path)
    config_data = _load_json(config_path)

    pref_info = _extract_preferences(prefs_data)

    diagnostic: Dict[str, Any] = {
        "version": VERSION,
        "n_sessions": pref_info["n_sessions"],
        "last_session_at": pref_info["last_session_at"],
        "learner_status": pref_info["learner_status"],
        "apnea_baseline": _extract_apnea(apnea_data),
        "config_summary": _extract_config(config_data),
    }

    json.dump(diagnostic, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
