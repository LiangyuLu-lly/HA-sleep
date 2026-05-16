#!/usr/bin/env python3
"""ltm.py — Local Long-Term Memory CLI for Kiro.

Single-file, stdlib-only recall and maintenance tool.
Reads/writes JSONL ledgers under ltm/store/ and runtime artifacts under ltm/runtime/.
"""
import argparse, datetime, fnmatch, hashlib, json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path

VERSION = "1.0.1"
ROOT = Path("ltm")
STORE = ROOT / "store"
RUNTIME = ROOT / "runtime"
REPORTS = ROOT / "reports"
SNAPSHOTS = ROOT / "snapshots"
BIN = ROOT / "bin"
CONFIG_PATH = ROOT / "config.json"
MANIFEST_PATH = ROOT / "manifest.json"
EVENTS = STORE / "events.jsonl"
CHECKPOINTS = STORE / "checkpoints.jsonl"
SESSIONS = STORE / "sessions.jsonl"
THREADS = STORE / "open_threads.jsonl"
ACTIVE_CTX = RUNTIME / "active-context.json"
LAST_RECALL = RUNTIME / "last-recall.md"
CUR_SESSION = RUNTIME / "current-session.json"
HEALTH_PATH = RUNTIME / "health.json"
GIT_TIMEOUT = 2
MAX_FILES_SAMPLE = 20
SEARCH_LIMIT_DEFAULT = 5
SEARCH_LIMIT_MAX = 20
SEARCH_SNIPPET = 160
SEARCH_MAX_BYTES = 4096
SHOW_EVENT_DEFAULT = 10
SHOW_EVENT_MAX = 50
COMPACT_THRESHOLD = 500_000  # bytes
COMPACT_HARD_LINES = 50_000
COMPACT_HARD_BYTES = 5_000_000

# ── exit codes ───────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_INVALID = 3
EXIT_IO_ERROR = 4
EXIT_USAGE = 64

SECRET_PATTERNS = [
    re.compile(r'sk_live_\S+'), re.compile(r'sk_test_\S+'), re.compile(r'AKIA\S{16,}'),
    re.compile(r'ghp_\S+'), re.compile(r'gho_\S+'), re.compile(r'-----BEGIN\s'),
    re.compile(r'Bearer\s+\S{20,}'), re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'),
]
SECRET_KEYS = {'password', 'secret', 'token', 'api_key', 'private_key', 'access_key'}

# ── helpers ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}

def _read_jsonl(path, skip_bad=True):
    records = []
    if not path.exists():
        return records
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if not skip_bad:
                raise
    return records

def _append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")

def _write_jsonl(path, records):
    """Atomic rewrite: write to temp file, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)

def _atomic_write_text(path, text):
    """Atomic text write: write to temp file, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def _event_fingerprint(files_sample, git_status, session_id):
    """Content-based fingerprint for deduplication."""
    data = json.dumps({"f": sorted(files_sample), "g": git_status, "s": session_id}, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()[:16]

def _next_id(path, prefix):
    records = _read_jsonl(path)
    max_n = 0
    for r in records:
        rid = r.get("id", "") or r.get("thread_id", "") or r.get("session_id", "")
        if rid.startswith(prefix):
            try:
                max_n = max(max_n, int(rid.split("_")[-1]))
            except ValueError:
                pass
    return f"{prefix}{max_n + 1:06d}"

def _git(*args):
    try:
        r = subprocess.run(["git"] + list(args), capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode == 0:
            return r.stdout.strip().splitlines()
        return None
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "timeout"

def _git_status():
    """Returns (files_list, git_status_str)."""
    diff = _git("diff", "--name-only")
    if diff == "timeout":
        return [], "timeout"
    if diff is None:
        # check if not a repo vs git missing
        check = _git("rev-parse", "--is-inside-work-tree")
        if check is None:
            return [], "unavailable"
        if check == "timeout":
            return [], "timeout"
        return [], "not_repo"
    cached = _git("diff", "--cached", "--name-only") or []
    if cached == "timeout":
        cached = []
    untracked = _git("ls-files", "--others", "--exclude-standard") or []
    if untracked == "timeout":
        untracked = []
    all_files = list(dict.fromkeys(diff + (cached if isinstance(cached, list) else []) + (untracked if isinstance(untracked, list) else [])))
    if not all_files:
        return [], "clean"
    return all_files, "ok"

def _filter_paths(paths, config):
    exclude = config.get("exclude_paths", [])
    sensitive = config.get("sensitive_path_patterns", [])
    result, redacted = [], False
    for p in paths:
        if any(fnmatch.fnmatch(p, pat) for pat in exclude):
            continue
        if any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat) for pat in sensitive):
            redacted = True
            continue
        result.append(p)
    return result[:MAX_FILES_SAMPLE], redacted

def _redact_text(text):
    if not isinstance(text, str):
        return text, False
    redacted = False
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            text = pat.sub("[REDACTED]", text)
            redacted = True
    return text, redacted

def _days_filter(records, days, ts_field="ts"):
    if days is None:
        return records
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r for r in records if r.get(ts_field, r.get("started_at", "")) >= cutoff_str]

def _out(data):
    sys.stdout.write(json.dumps(data, indent=None, separators=(",", ":")) + "\n")

def _err(msg):
    sys.stderr.write(f"ltm: {msg}\n")
