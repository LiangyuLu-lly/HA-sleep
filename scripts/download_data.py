"""Download a small sample of the Sleep-EDF Telemetry dataset from PhysioNet.

PhysioNet kindly hosts the dataset over plain HTTPS so no account or auth
is required.  Each subject contributes two files:

  * ``ST7XXXJ0-PSG.edf``         – polysomnography signals (EEG, EOG, EMG,
    cardiorespiratory, etc.).  We use the heart-rate / movement-related
    channels.
  * ``ST7XXXJ0-Hypnogram.edf``   – sleep-stage annotations (W, 1, 2, 3, 4, R).

Usage
-----
.. code-block:: bash

    python scripts/download_data.py                     # default: 2 subjects
    python scripts/download_data.py --num-subjects 4    # 4 subjects, ~200 MB
    python scripts/download_data.py --subjects ST7011 ST7022

The files are saved under ``data/sleep-edf-telemetry/``.  Re-running the
script skips files that already exist on disk.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import List
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Add project root to PYTHONPATH so this script runs from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("download_data")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)

# -------------------------------------------------------------------------
# PhysioNet Sleep-EDF Telemetry index
# -------------------------------------------------------------------------
# Telemetry cohort uses the prefix "ST7" and a recording-night digit
# (0 = baseline, 1 = with temazepam).  We always pair both nights so the
# subjects directory ends up with hypnograms for both.
#
# Full subject list (22 subjects, 44 files):
#   ST7011, ST7012, ST7021, ST7022, ST7041, ST7042, ST7051, ST7052,
#   ST7061, ST7062, ST7071, ST7072, ST7081, ST7082, ST7091, ST7092,
#   ST7101, ST7102, ST7111, ST7112, ST7121, ST7122, ST7131, ST7132,
#   ST7141, ST7142, ST7151, ST7152, ST7161, ST7162, ST7171, ST7172,
#   ST7181, ST7182, ST7191, ST7192, ST7201, ST7202, ST7211, ST7212,
#   ST7221, ST7222, ST7241, ST7242

PHYSIONET_BASE = "https://physionet.org/files/sleep-edfx/1.0.0/sleep-telemetry"

# Default subject list — 2 subjects (= 4 files, ~100 MB) is enough for a
# quick demo run that produces meaningful metrics.
DEFAULT_SUBJECTS: List[str] = ["ST7011", "ST7022"]

# File index is fetched lazily from the PhysioNet directory listing on first
# use so that we don't need to hard-code Hypnogram suffixes (each scorer's
# initials differ subject-to-subject).
_FILE_INDEX_CACHE: dict | None = None


def _fetch_file_index() -> dict:
    """Fetch and parse the PhysioNet sleep-telemetry directory listing.

    Returns:
        ``{subject_id: (psg_filename, hypnogram_filename)}`` for every
        subject available on PhysioNet (44 files / 22 nights / 22 subjects).
    """
    global _FILE_INDEX_CACHE
    if _FILE_INDEX_CACHE is not None:
        return _FILE_INDEX_CACHE

    import re
    index_url = PHYSIONET_BASE + "/"
    logger.info("Fetching file index from %s", index_url)
    try:
        with urlopen(index_url, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(
            f"Could not retrieve PhysioNet directory listing: {exc}\n"
            f"Check your internet connection or visit {index_url} in a browser."
        ) from exc

    psgs = sorted(set(re.findall(r"ST7\d{3}J0-PSG\.edf", html)))
    hyps = sorted(set(re.findall(r"ST7\d{3}[A-Z]\w*-Hypnogram\.edf", html)))

    by_subject: dict = {}
    for psg in psgs:
        subject_id = psg[:6]  # e.g. "ST7011"
        # Find the matching hypnogram (same 6-char subject prefix)
        matching = [h for h in hyps if h.startswith(subject_id)]
        if not matching:
            logger.warning("No hypnogram for %s — skipping", subject_id)
            continue
        by_subject[subject_id] = (psg, matching[0])

    if not by_subject:
        raise RuntimeError(
            "PhysioNet directory listing returned no usable EDF files. "
            "The site layout may have changed."
        )
    _FILE_INDEX_CACHE = by_subject
    logger.info("Found %d subjects on PhysioNet", len(by_subject))
    return by_subject


def _human_readable(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024.0
    return f"{n_bytes:.1f} TB"


def download_file(url: str, dest: Path, retries: int = 3) -> bool:
    """Download a single file with progress logging.

    Returns True on success (or if the file already exists), False otherwise.
    """
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("✓ Already downloaded: %s (%s)", dest.name, _human_readable(dest.stat().st_size))
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            logger.info("↓ Downloading %s (attempt %d/%d)", dest.name, attempt, retries)
            t0 = time.time()
            with urlopen(url, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                last_log = t0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        # Log progress every 2 seconds
                        if time.time() - last_log > 2.0:
                            pct = 100.0 * downloaded / total if total else 0.0
                            logger.info(
                                "    %s / %s (%.1f%%)",
                                _human_readable(downloaded),
                                _human_readable(total) if total else "?",
                                pct,
                            )
                            last_log = time.time()
            tmp.rename(dest)
            elapsed = time.time() - t0
            logger.info(
                "✓ Saved %s (%s in %.1fs)",
                dest.name, _human_readable(dest.stat().st_size), elapsed,
            )
            return True
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.warning("  attempt %d failed: %s", attempt, exc)
            if tmp.exists():
                tmp.unlink()
            if attempt < retries:
                wait = 2 ** attempt
                logger.info("  retrying in %ds…", wait)
                time.sleep(wait)
        except Exception as exc:
            logger.error("  unexpected error: %s", exc)
            if tmp.exists():
                tmp.unlink()
            return False

    logger.error("✗ Failed to download %s after %d attempts", dest.name, retries)
    return False


def download_subject(subject_id: str, dest_dir: Path) -> bool:
    """Download both files (PSG + Hypnogram) for a single subject."""
    index = _fetch_file_index()
    if subject_id not in index:
        logger.error(
            "Unknown subject ID '%s'. Known IDs: %s",
            subject_id, ", ".join(sorted(index.keys())),
        )
        return False
    psg_name, hyp_name = index[subject_id]
    success = True
    for fname in (psg_name, hyp_name):
        url = f"{PHYSIONET_BASE}/{fname}"
        dest = dest_dir / fname
        if not download_file(url, dest):
            success = False
    return success


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subjects",
        nargs="+",
        default=None,
        help=f"Subject IDs to download (default: {' '.join(DEFAULT_SUBJECTS)}). "
             f"Use --list to see all available IDs on PhysioNet.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available subject IDs on PhysioNet and exit.",
    )
    parser.add_argument(
        "--num-subjects",
        type=int,
        default=None,
        help="Number of subjects to download (uses the first N from the default list).",
    )
    parser.add_argument(
        "--output-dir",
        default="data/sleep-edf-telemetry",
        help="Where to save the EDF files (default: data/sleep-edf-telemetry).",
    )
    args = parser.parse_args()

    # --list: show available subjects and exit
    if args.list:
        try:
            index = _fetch_file_index()
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        print("Available subjects on PhysioNet (Sleep-EDF Telemetry):")
        for sid in sorted(index.keys()):
            psg, hyp = index[sid]
            print(f"  {sid}  →  {psg}  +  {hyp}")
        return 0

    # Determine subject list
    if args.subjects:
        subjects = args.subjects
    elif args.num_subjects is not None:
        try:
            index = _fetch_file_index()
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        subjects = sorted(index.keys())[: args.num_subjects]
    else:
        subjects = DEFAULT_SUBJECTS

    dest_dir = (PROJECT_ROOT / args.output_dir).resolve()
    logger.info("Downloading %d subject(s) to %s", len(subjects), dest_dir)
    logger.info("Subjects: %s", ", ".join(subjects))

    failures = 0
    for subject_id in subjects:
        if not download_subject(subject_id, dest_dir):
            failures += 1

    if failures:
        logger.error("✗ %d subject(s) failed to download", failures)
        return 1

    logger.info("✓ All downloads complete. Files saved to: %s", dest_dir)

    # Print a quick summary of files on disk
    edf_files = sorted(dest_dir.glob("*.edf"))
    total_size = sum(f.stat().st_size for f in edf_files)
    logger.info(
        "  → %d EDF files, %s total",
        len(edf_files), _human_readable(total_size),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
