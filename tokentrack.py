#!/usr/bin/env python3
"""Track token usage across Claude Code and oh-my-pie (omp).

Both tools write per-message usage into session JSONL files. This reads them
directly, dedupes, prices them from pricing.json, and reports.

Cost is always recomputed from raw token counts. omp stores its own cost
figures but they are inconsistent (some rows are inflated 100x), so they are
ignored.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath so a symlinked entrypoint still finds pricing.json
CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
OMP_DIR = os.path.expanduser("~/.omp/agent/sessions")
EVENTS_PATH = os.path.join(HERE, "events.json")  # default dated labels for the charts; --events overrides

# ---------------------------------------------------------------- pricing

RATE_KEYS = ("in", "out", "cw5m", "cw1h", "cr", "long")  # the rest of a model entry is provenance (status, source_url, verified_on, note)


def load_pricing_doc(path=None) -> dict:
    """Read pricing.json. `path` is the file or the repo directory holding it; default is this repo.

    Returns {"models", "currency", "sources", "version", "path"}:
    - models: the file's model entries, unchanged, provenance keys included.
    - sources: the file's top-level _* keys except _comment (where the prices came from).
    - version: sha256 over models + sources, so it changes whenever a price or its provenance does.
    """
    p = os.path.abspath(os.path.expanduser(path or HERE))
    if os.path.isdir(p):
        p = os.path.join(p, "pricing.json")
    with open(p) as fh:
        raw = json.load(fh)
    models = raw["models"]
    sources = {k: v for k, v in raw.items() if k.startswith("_") and k != "_comment"}
    canon = json.dumps({"models": models, "sources": sources}, sort_keys=True, separators=(",", ":"))
    return {
        "models": models,
        "currency": raw.get("currency") or {"code": "USD", "symbol": "$", "per_usd": 1.0},
        "sources": sources,
        "version": "sha256:" + hashlib.sha256(canon.encode()).hexdigest()[:12],
        "path": p,
    }


def load_pricing(path=None):
    """(models, currency), for callers that need neither the version nor the sources."""
    doc = load_pricing_doc(path)
    return doc["models"], doc["currency"]


def pricing_version(path=None) -> str:
    return load_pricing_doc(path)["version"]


EFFORT_SUFFIXES = ("low", "medium", "high", "xhigh", "max")


def rate_key(model: str, rates: dict) -> str:
    """The rates key that prices model. The id itself when it has an entry; otherwise the id minus an
    effort suffix when that base id has one (claude-opus-5-5-low -> claude-opus-5-5, same rate). Any
    other id is returned unchanged and has no rate."""
    if model in rates:
        return model
    base, sep, effort = model.rpartition("-")
    if sep and effort in EFFORT_SUFFIXES and base in rates:
        return base
    return model


def model_status(model: str, rates: dict):
    """Price state of one model: True, "estimated", or False (no rate; cost is unknown, never $0).

    True when the model has a rate and no status, or status "verified". A configured rate with no
    status is priced but not externally verified; the models map in the payload shows which entries
    carry a status. Any other status value (the file uses "estimated") gives "estimated".
    Effort-suffixed ids take their base model's status (see rate_key).
    """
    r = rates.get(rate_key(model, rates))
    if not r:
        return False
    status = r.get("status")
    if status is None or status == "verified":
        return True
    return "estimated"


def normalize_model(model: str) -> str:
    """Strip date suffixes so claude-haiku-4-5-20251001 prices as claude-haiku-4-5."""
    if not model:
        return "unknown"
    model = model.rsplit("/", 1)[-1]  # OpenRouter: anthropic/claude-opus-5.5
    if model.startswith("claude-"):
        model = model.replace(".", "-")  # claude-opus-5.5 -> claude-opus-5-5
    parts = model.split("-")
    if parts[-1].isdigit() and len(parts[-1]) == 8:  # claude-haiku-4-5-20251001
        return "-".join(parts[:-1])
    if model.startswith("gemini-") and parts[-1] in ("low", "medium", "high", "xhigh"):
        return "-".join(parts[:-1])  # effort variants bill at the base model rate
    return model


def rate_for(model: str, prompt_tokens: int, rates: dict):
    """The rate dict that prices one request: the model's "long" tier once prompt tokens pass its
    threshold. None when the model has no rate (see rate_key for effort-suffixed ids)."""
    r = rates.get(rate_key(model, rates))
    if not r:
        return None
    long = r.get("long")  # tiered models (Haiku 5.5): whole request reprices when prompt > threshold
    if long and prompt_tokens > long["threshold"]:
        return long
    return r


def price(rec: dict, rates: dict) -> float:
    r = rate_for(rec["model"], rec["in"] + rec["cr"] + rec["cw5m"] + rec["cw1h"], rates)
    if r is None:
        return 0.0
    return (
        rec["in"] * r["in"]
        + rec["out"] * r["out"]
        + rec["cw5m"] * r["cw5m"]
        + rec["cw1h"] * r["cw1h"]
        + rec["cr"] * r["cr"]
    ) / 1_000_000


# ---------------------------------------------------------------- timezone


def resolve_tz(tz):
    """Zone that days, hours and periods are cut in. None, "local" or "system" mean the system zone
    (returned as None). Anything else is an IANA name (ZoneInfo) or a tzinfo already."""
    if tz is None or isinstance(tz, tzinfo):
        return tz
    name = str(tz).strip()
    if name.lower() in ("", "local", "system"):
        return None
    return ZoneInfo(name)  # raises ZoneInfoNotFoundError (a KeyError) or ValueError for a bad name


def to_local(ts: str, tz=None):
    """Convert an ISO-8601 timestamp (no offset means UTC) to tz; None = system zone. None if unparseable."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(resolve_tz(tz))


def _system_zone_name():
    """IANA name of the system zone: $TZ if it names one, else the /etc/localtime link; None if neither does."""
    names = [os.environ.get("TZ", "").lstrip(":")]
    real = os.path.realpath("/etc/localtime")
    if "/zoneinfo/" in real:
        names.append(real.split("/zoneinfo/", 1)[1])
    for name in names:
        if name.startswith("posix/"):
            name = name[len("posix/"):]
        if not name:
            continue
        try:
            ZoneInfo(name)
        except (KeyError, ValueError):
            continue
        return name
    return None


def timezone_name(tz=None) -> str:
    """IANA name of tz (None = system zone), or "local" when no IANA name is known."""
    tz = resolve_tz(tz)
    if tz is None:
        return _system_zone_name() or "local"
    return getattr(tz, "key", None) or str(tz)


# ---------------------------------------------------------------- parse cache

CACHE_DIR = os.environ.get("TOKENTRACK_CACHE") or os.path.expanduser("~/.cache/token-tracker/parse-v5")  # bump when parsed fields change
CACHE_CAP_BYTES = 64 * 1024 * 1024

# Set to a dict by the harness to collect per-file parse timings. None = off.
PROFILE = None


class ParseCache:
    """Memoizes the parsed records of one session file.

    Session files are append-only and mostly cold: yesterday's transcripts never
    change again, so re-reading and re-parsing every one of them on each run is
    pure waste. The key includes mtime and size, so an active session that grew
    since the last run misses and is re-read.
    """

    def __init__(self, enabled=True, cap=CACHE_CAP_BYTES, directory=CACHE_DIR):
        self.enabled = enabled
        self.cap = cap
        self.dir = directory
        self.hits = self.misses = self.evictions = 0
        self.hit_ns = []      # per-lookup latency, so the harness can report percentiles
        self.miss_ns = []
        self.bytes_in = self.bytes_out = 0
        if enabled:
            os.makedirs(self.dir, exist_ok=True)

    def _path(self, fp):
        try:
            st = os.stat(fp)
        except OSError:
            return None
        sig = f"{os.path.realpath(fp)}:{st.st_mtime_ns}:{st.st_size}"
        return os.path.join(self.dir, hashlib.sha1(sig.encode()).hexdigest() + ".json")

    def get(self, fp):
        if not self.enabled:
            return None
        t0 = time.perf_counter_ns()
        path = self._path(fp)
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
            recs = json.loads(blob)
        except (OSError, TypeError, json.JSONDecodeError):
            self.misses += 1
            self.miss_ns.append(time.perf_counter_ns() - t0)
            return None
        os.utime(path, None)  # touch, so eviction can drop the least recently used
        self.bytes_in += len(blob)
        self.hits += 1
        self.hit_ns.append(time.perf_counter_ns() - t0)
        return recs

    def put(self, fp, recs):
        if not self.enabled:
            return
        path = self._path(fp)
        if not path:
            return
        try:
            blob = json.dumps(recs).encode()
            tmp = path + f".{os.getpid()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, path)  # atomic, so a concurrent run never reads a half file
            self.bytes_out += len(blob)
        except OSError:
            pass

    def usage(self):
        """(bytes, entries) currently held."""
        total = n = 0
        try:
            for name in os.listdir(self.dir):
                try:
                    total += os.stat(os.path.join(self.dir, name)).st_size
                    n += 1
                except OSError:
                    continue
        except OSError:
            pass
        return total, n

    def evict(self):
        """Drop least-recently-used entries until the directory fits the cap."""
        if not self.enabled:
            return
        try:
            entries = []
            for name in os.listdir(self.dir):
                p = os.path.join(self.dir, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((st.st_atime, st.st_size, p))
        except OSError:
            return
        total = sum(e[1] for e in entries)
        if total <= self.cap:
            return
        for _, size, p in sorted(entries):
            try:
                os.remove(p)
            except OSError:
                continue
            total -= size
            self.evictions += 1
            if total <= self.cap:
                break


CACHE = ParseCache(enabled=False)


def _cached_parse(fp, parser):
    """Parse one session file, through the cache, timing it for the harness."""
    t0 = time.perf_counter_ns()
    recs = CACHE.get(fp)
    cached = recs is not None
    if not cached:
        recs = parser(fp)
        CACHE.put(fp, recs)
    if PROFILE is not None:
        try:
            size = os.path.getsize(fp)
        except OSError:
            size = 0
        PROFILE["files"].append(
            {"ns": time.perf_counter_ns() - t0, "cached": cached, "rows": len(recs["rows"]), "bytes": size}
        )
    return recs


# ---------------------------------------------------------------- parsing


def _iso(ts) -> str | None:
    """Normalize a timestamp (ISO string or epoch ms) to ISO-8601 UTC."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
    return str(ts)


# Failures seen while reading. Only covers files actually parsed this run:
# a cache hit never re-reads the file, so these counts fall as the cache warms.
STATS = {"bad_lines": 0, "unreadable_files": 0, "lines": 0, "bytes": 0, "bad_timestamps": 0}


def _read_json_lines(fp: str):
    try:
        with open(fp, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                STATS["lines"] += 1
                STATS["bytes"] += len(line)
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    STATS["bad_lines"] += 1
                    continue
    except OSError:
        STATS["unreadable_files"] += 1
        return


def _iter_json_lines(pattern: str):
    for fp in glob.iglob(pattern, recursive=True):
        for d in _read_json_lines(fp):
            yield fp, d


EDIT_TOOLS = {"edit", "write", "Edit", "Write", "MultiEdit", "NotebookEdit"}  # recognised edit-tool names; counts invocations, not diffs


def session_id(source: str, rel: str) -> str:
    """Opaque, stable ID for one session file: a hash of its source and its path under the source root.
    The path is never stored, so IDs are safe to put in the payload."""
    return "s_" + hashlib.sha256(f"{source}\0{rel}".encode()).hexdigest()[:16]


def _rel_under(root: str, path: str):
    """Path of `path` relative to `root`, or None when it lies outside root."""
    rel = os.path.relpath(os.path.abspath(os.path.expanduser(path)), root)
    return None if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def _claude_parent_rel(rel: str):
    """A Claude Code subagent transcript, <project>/<session>/subagents/<agent>.jsonl, belongs to
    <project>/<session>.jsonl. None for every other file."""
    parts = rel.split(os.sep)
    if "subagents" not in parts[1:-1]:
        return None
    return os.sep.join(parts[: parts.index("subagents", 1)]) + ".jsonl"


def _blocks(msg: dict) -> list:
    """Dict content blocks of a message; plain-string content has none."""
    content = msg.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


SPEED_MAX_SECS = 600  # a longer gap is idle time between requests, not generation; such requests get no speed


def _speed_secs(secs, out):
    """A request's generation seconds when usable: positive, at most SPEED_MAX_SECS, and out > 0. Else None."""
    return secs if secs and out and 0 < secs <= SPEED_MAX_SECS else None


def _parse_claude_file(fp: str):
    """Parse one Claude Code transcript into {sid, parent, subagent, source, rows, tools}.

    rows: one per request with usage (deduped globally, after caching). tools: [ts, tool id, name] for
    every tool_use block in an assistant row, whether or not that row carries usage.
    """
    rel = _rel_under(CLAUDE_DIR, fp) or os.path.basename(fp)
    sid = session_id("claude-code", rel)
    parent_rel = _claude_parent_rel(rel)
    sidechain = False
    rows, tools = [], []
    for d in _read_json_lines(fp):
        sidechain = sidechain or bool(d.get("isSidechain"))
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        ts = _iso(d.get("timestamp"))
        if ts:
            for b in _blocks(msg):
                if b.get("type") == "tool_use":
                    tools.append([ts, b.get("id") or f"{sid}#{len(tools)}", b.get("name") or ""])
        usage = msg.get("usage")
        if not usage:
            continue

        model = normalize_model(msg.get("model") or "unknown")
        if model in ("<synthetic>", "unknown"):
            continue
        cc = usage.get("cache_creation") or {}
        cw5m = cc.get("ephemeral_5m_input_tokens")
        cw1h = cc.get("ephemeral_1h_input_tokens")
        if cw5m is None and cw1h is None:
            # Older rows only have the flat total; assume the 5m rate.
            cw5m, cw1h = usage.get("cache_creation_input_tokens", 0) or 0, 0

        cwd = d.get("cwd") or ""
        rows.append(
            {
                "_key": [d.get("requestId"), msg.get("id")],
                "ts": ts,
                "source": "claude-code",
                "provider": "anthropic",
                "project": os.path.basename(cwd) if cwd else os.path.basename(os.path.dirname(fp)),
                "model": model,
                "in": usage.get("input_tokens", 0) or 0,
                "out": usage.get("output_tokens", 0) or 0,
                "cr": usage.get("cache_read_input_tokens", 0) or 0,
                "cw5m": cw5m or 0,
                "cw1h": cw1h or 0,
                "secs": None,
            }
        )
    return {
        "sid": sid,
        "parent": session_id("claude-code", parent_rel) if parent_rel else None,
        "subagent": parent_rel is not None or sidechain,
        "source": "claude-code",
        "rows": rows,
        "tools": tools,
    }


def _parse_omp_file(fp: str):
    """Parse one omp session file into {sid, parent, subagent, source, rows, tools}. Dedupe happens globally,
    after caching. The session header's parentSession is set only on subagent sessions and names their parent."""
    rel = _rel_under(OMP_DIR, fp) or os.path.basename(fp)
    sid = session_id("omp", rel)
    cwd = parent_path = None
    rows, tools = [], []
    for d in _read_json_lines(fp):
        if cwd is None and d.get("cwd"):
            cwd = d["cwd"]  # omp records the session cwd in its own entry
        if d.get("type") == "session":
            parent_path = d.get("parentSession") or None
        if d.get("type") != "message":
            continue
        msg = d.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        ts = _iso(d.get("timestamp") or msg.get("timestamp"))
        if ts:
            for b in _blocks(msg):
                if b.get("type") == "toolCall":
                    tools.append([ts, b.get("id") or f"{sid}#{len(tools)}", b.get("name") or ""])
        usage = msg.get("usage")
        if not usage:
            continue

        cttl = usage.get("cttl") or {}
        cw5m = cttl.get("ephemeral5m")
        cw1h = cttl.get("ephemeral1h")
        if cw5m is None and cw1h is None:
            cw5m, cw1h = usage.get("cacheWrite", 0) or 0, 0

        # duration and ttft are ms; generation is the time after the first token.
        out = usage.get("output", 0) or 0
        dur = msg.get("duration")
        secs = (dur - (msg.get("ttft") or 0)) / 1000 if dur else None
        rows.append(
            {
                "_key": msg.get("responseId") or d.get("id"),
                "ts": ts,
                "source": "omp",
                "provider": msg.get("provider") or "unknown",
                "project": None,  # set below, once the whole file has been read
                "model": normalize_model(msg.get("model") or "unknown"),
                "in": usage.get("input", 0) or 0,
                "out": out,
                "cr": usage.get("cacheRead", 0) or 0,
                "cw5m": cw5m or 0,
                "cw1h": cw1h or 0,
                "secs": _speed_secs(secs, out),
            }
        )
    if cwd:
        project = os.path.basename(cwd.rstrip("/")) or cwd
    else:
        # Fall back to the dir name: "-Code-learning-backend" -> "learning-backend"
        project = rel.split(os.sep)[0].lstrip("-").split("-", 1)[-1] or "unknown"
    for r in rows:
        r["project"] = project
    parent_rel = _rel_under(OMP_DIR, parent_path) if parent_path else None
    return {
        "sid": sid,
        "parent": session_id("omp", parent_rel) if parent_rel else None,
        "subagent": parent_path is not None,
        "source": "omp",
        "rows": rows,
        "tools": tools,
    }


def _parse_files(sources) -> list:
    """Parsed session files of the requested sources, through the parse cache."""
    files = []
    if "claude-code" in sources:
        files += [_cached_parse(fp, _parse_claude_file)
                  for fp in glob.iglob(os.path.join(CLAUDE_DIR, "**", "*.jsonl"), recursive=True)]
    if "omp" in sources:
        files += [_cached_parse(fp, _parse_omp_file)
                  for fp in glob.iglob(os.path.join(OMP_DIR, "**", "*.jsonl"), recursive=True)]
    return files


# Folders that are really parts of one project; merged into one row in reports.
PROJECT_ALIASES = {
    n: "Agrawal_Samajh_App"
    for n in (
        "Agrawal_App", "Agrawal_Frontend", "Agrawal_Backend", "Agrawal_privacy_Policy", "agrason_samaj",
        "-Code-projects-agrason_samaj", "-home-tankaizokuo-Code-projects-Agrawal-Samajh-App",
    )
}


# Aliased folders that show up as named sub repos when a parent project is opened in the dashboard.
# Everything else folded into the parent (the root folder, old names) is bucketed as ROOT_SUB.
PARENTS = set(PROJECT_ALIASES.values())
SUB_REPOS = {"Agrawal_App", "Agrawal_Frontend", "Agrawal_Backend", "Agrawal_privacy_Policy"}
ROOT_SUB = "(root / other)"


class Records(list):
    """collect()'s result: the priced records, with the session facts on .facts."""

    facts = None


def collect(rates, since_days=None, sources=("claude-code", "omp"), tz=None) -> Records:
    """Priced records for the selected sources, with day and hour cut in tz.

    tz: None for the system zone, else an IANA name or a tzinfo. since_days means that many whole
    calendar days in tz, ending today, so the cutoff always lines up with record["day"]. Records with
    no parseable timestamp are dropped and counted in STATS["bad_timestamps"]. record["priced"] is
    model_status(): True, "estimated" or False. Each record also has sid (its session file) and hod
    (local hour of day).

    .facts, for build_payload's session fields:
      files:    {sid: {"subagent": bool, "parent": sid or None}}
      tools:    {sid: [tool invocations, edit-tool invocations]}, windowed like the records
      children: {sid: linked subagent files that have requests}, counted over every file parsed
    """
    tz = resolve_tz(tz)
    first_day = None
    if since_days:
        first_day = datetime.now(timezone.utc).astimezone(tz).date() - timedelta(days=since_days - 1)

    facts = {"files": {}, "tools": {}, "children": {}}
    seen_rows, seen_tools = set(), set()
    out = Records()
    for f in _parse_files(sources):
        sid, source = f["sid"], f["source"]
        facts["files"][sid] = {"subagent": f["subagent"], "parent": f["parent"]}
        if f["parent"] and f["rows"]:
            facts["children"][f["parent"]] = facts["children"].get(f["parent"], 0) + 1

        for ts, tool_id, name in f["tools"]:
            if (source, tool_id) in seen_tools:
                continue
            seen_tools.add((source, tool_id))
            if first_day:
                local = to_local(ts, tz)
                if local is None or local.date() < first_day:
                    continue
            counts = facts["tools"].setdefault(sid, [0, 0])
            counts[0] += 1
            if name in EDIT_TOOLS:
                counts[1] += 1

        for r in f["rows"]:
            key = (source, tuple(r["_key"]) if isinstance(r["_key"], list) else r["_key"])
            if key in seen_rows:
                continue
            seen_rows.add(key)
            if not r["ts"]:
                continue
            local = to_local(r["ts"], tz)
            if local is None:
                STATS["bad_timestamps"] += 1
                continue
            if first_day and local.date() < first_day:
                continue
            r["sid"] = sid
            r["total"] = r["in"] + r["out"] + r["cr"] + r["cw5m"] + r["cw1h"]
            r["cost"] = price(r, rates)
            r["day"] = local.date().isoformat()
            r["hour"] = local.strftime("%m-%d %H:00")  # sortable, unlike weekday names
            r["hod"] = local.hour
            orig = r["project"]
            r["project"] = PROJECT_ALIASES.get(orig, orig)
            r["sub"] = orig if orig in SUB_REPOS else ROOT_SUB
            r["priced"] = model_status(r["model"], rates)
            out.append(r)
    out.sort(key=lambda r: r["ts"])
    out.facts = facts
    return out


def model_entry(model: str, rates: dict) -> dict:
    """models-map entry: priced flag plus the model's provenance keys (status, source_url, verified_on, note) verbatim."""
    entry = {k: v for k, v in (rates.get(rate_key(model, rates)) or {}).items() if k not in RATE_KEYS}
    entry["priced"] = model_status(model, rates)
    return entry


def coverage(recs) -> dict:
    """Requests and USD by price state. Unpriced requests are counted but carry no cost, never $0."""
    cov = {
        "priced": {"requests": 0, "cost_usd": 0.0},
        "estimated": {"requests": 0, "cost_usd": 0.0},
        "unpriced": {"requests": 0},
    }
    for r in recs:
        if r["priced"] is False:
            cov["unpriced"]["requests"] += 1
            continue
        b = cov["estimated" if r["priced"] == "estimated" else "priced"]
        b["requests"] += 1
        b["cost_usd"] += r["cost"]
    for k in ("priced", "estimated"):
        cov[k]["cost_usd"] = round(cov[k]["cost_usd"], 8)
    return cov


SESSION_DETAIL_BYTES = 8 * 1024 * 1024  # JSON budget for DATA.sessions; keeps the page far under the 16 MB limit
SESSIONS_BASIS = "start/end are first and last observed request timestamps (UTC); session lifetime is not recorded."


def _pricing_status(priced: int, estimated: int, unpriced: int) -> str:
    """Session price state from its request counts: priced, estimated, partial or unpriced."""
    if unpriced:
        return "partial" if priced or estimated else "unpriced"
    return "estimated" if estimated else "priced"


def build_sessions(recs, facts=None) -> list:
    """One entry per session with requests in recs, newest first (shape: local://token-session-contract.md).
    facts (collect().facts) fills the tool, subagent and parent fields; without it they are null, not guessed."""
    tot = _agg(recs, lambda r: r["sid"])
    seg = _agg(recs, lambda r: (r["sid"], r["day"], r["model"]))
    model_state = {r["model"]: r["priced"] for r in recs}
    states = Counter((r["sid"], r["priced"]) for r in recs)
    models = defaultdict(Counter)
    first, last, project, source = {}, {}, {}, {}
    for r in recs:  # recs are in timestamp order: setdefault keeps the first record, assignment the last
        sid = r["sid"]
        first.setdefault(sid, r["ts"])
        last[sid] = r["ts"]
        project.setdefault(sid, r["project"])
        source.setdefault(sid, r["source"])
        models[sid][r["model"]] += 1
    segments = defaultdict(list)
    for (sid, day, model), v in seg.items():
        cost = None if model_state[model] is False else round(v["cost"], 8)
        segments[sid].append([day, model, v["n"], v["in"], v["out"], v["cr"], v["cw"], cost])

    out = []
    for sid, t in tot.items():
        priced, estimated, unpriced = states[(sid, True)], states[(sid, "estimated")], states[(sid, False)]
        info = facts["files"].get(sid) if facts else None
        calls = facts["tools"].get(sid, [0, 0]) if facts else None
        out.append(
            {
                "id": sid,
                "source": source[sid],
                "project": project[sid],
                "models": [m for m, _ in models[sid].most_common()],
                "parent": info["parent"] if info else None,
                "is_subagent": info["subagent"] if info else None,
                "subagent_count": facts["children"].get(sid, 0) if facts else None,
                "start": first[sid],
                "end": last[sid],
                "requests": t["n"],
                "tokens": {"in": t["in"], "out": t["out"], "cr": t["cr"], "cw": t["cw"]},
                "cost_usd": round(t["cost"], 8) if priced or estimated else None,
                "pricing": _pricing_status(priced, estimated, unpriced),
                "unpriced_requests": unpriced,
                "tool_calls": calls[0] if calls else None,
                "edit_tool_calls": calls[1] if calls else None,
                "edits_made": calls[1] > 0 if calls else None,
                "segments": sorted(segments[sid], key=lambda s: (s[0], s[1])),
            }
        )
    out.sort(key=lambda s: (s["end"], s["id"]), reverse=True)
    return out


def cap_sessions(rows: list, budget: int = SESSION_DETAIL_BYTES):
    """Keep the newest sessions (rows are newest first) while their JSON fits in budget bytes.
    Returns (kept, sessions_coverage)."""
    kept, used = [], 0
    for s in rows:
        size = len(json.dumps(s)) + 2  # + ", " between list items
        if used + size > budget:
            break
        kept.append(s)
        used += size
    omitted = len(rows) - len(kept)
    return kept, {"complete": omitted == 0, "included": len(kept), "omitted": omitted,
                  "archived": None, "cap_bytes": budget}


def build_hours(recs) -> list:
    """Hourly usage in DATA.timezone, bucketed by each request's own timestamp. Rows:
    [day, hour_of_day, source, model, requests, in, out, cr, cw, cost_usd, project];
    cost_usd is None for an unpriced model. Grouped by project too, so project filters work."""
    unpriced = {r["model"] for r in recs if r["priced"] is False}
    buckets = _agg(recs, lambda r: (r["day"], r["hod"], r["source"], r["model"], r["project"]))
    return [
        [k[0], k[1], k[2], k[3], v["n"], v["in"], v["out"], v["cr"], v["cw"],
         None if k[3] in unpriced else round(v["cost"], 8), k[4]]
        for k, v in sorted(buckets.items())
    ]


def build_speed(recs) -> list:
    """Generation speed in DATA.timezone: [day, source, model, n, out, secs] per (day, source, model) with a timed
    omp request. Claude Code is not timed (its transcript timestamps are too noisy). n and out count only timed
    requests; secs is their summed generation time (3 dp). tok/s = out / secs."""
    buckets = defaultdict(lambda: [0, 0, 0.0])
    for r in recs:
        if r["source"] != "omp" or r["secs"] is None:
            continue
        b = buckets[(r["day"], r["source"], r["model"])]
        b[0] += 1
        b[1] += r["out"]
        b[2] += r["secs"]
    return [[k[0], k[1], k[2], v[0], v[1], round(v[2], 3)] for k, v in sorted(buckets.items())]


class EventsError(ValueError):
    """An events file that cannot be used. The message names the file, the entry and the problem."""


def _valid_day(s) -> bool:
    """True for a real calendar date written YYYY-MM-DD."""
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def load_events(path=None) -> list:
    """Dated labels for the daily charts, from a JSON array of {"date": "YYYY-MM-DD", "label": "..."}.

    path=None reads events.json beside this file, where a missing file means no events. An explicit path
    must exist. Entries are strict (exactly those two keys) so a typo fails instead of silently vanishing.
    Returns the entries sorted by date, then label.
    """
    explicit = path is not None
    p = os.path.abspath(os.path.expanduser(path if explicit else EVENTS_PATH))
    if not os.path.exists(p):
        if explicit:
            raise EventsError(f"events file not found: {p}")
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as e:
        raise EventsError(f"{p}: not valid JSON ({e.msg} at line {e.lineno}, column {e.colno})") from None
    except (OSError, UnicodeDecodeError) as e:
        raise EventsError(f"{p}: cannot read: {e}") from None
    if not isinstance(raw, list):
        raise EventsError(f'{p}: expected a JSON array of {{"date", "label"}} objects, got {type(raw).__name__}')
    out = []
    for i, item in enumerate(raw, 1):
        where = f"{p}: entry {i}"
        if not isinstance(item, dict):
            raise EventsError(f'{where}: expected an object with "date" and "label", got {type(item).__name__}')
        extra = sorted(set(item) - {"date", "label"})
        if extra:
            raise EventsError(f"{where}: unknown key(s) {', '.join(map(repr, extra))}; allowed: date, label")
        if not _valid_day(item.get("date")):
            raise EventsError(f'{where}: "date" must be YYYY-MM-DD, got {item.get("date")!r}')
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise EventsError(f'{where}: "label" must be a non-empty string')
        out.append({"date": item["date"], "label": label.strip()})
    return sorted(out, key=lambda e: (e["date"], e["label"]))


def build_payload(recs, pricing, tz=None, currency=None, events=None, facts=None) -> dict:
    """Payload for recs from collect(..., tz=tz). pricing is load_pricing_doc()'s result; currency is the
    display currency (default: the pricing file's). events: load_events() entries. facts: collect().facts
    (default: recs.facts); without facts the session tool, subagent and parent fields are null.

    cells: [day, source, model, project, n, in, out, cr, cw, cost_usd, archived]
    sub_cells: the same, with sub_repo before archived.
    archived is always False here. merge_data.py sets it on rows restored from an older artifact.
    hours: [day, hour_of_day, source, model, requests, in, out, cr, cw, cost_usd, project], complete.
    speed: build_speed() rows, [day, source, model, n, out, secs] over timed omp requests only.
    sessions: build_sessions() rows, newest first, capped; sessions_coverage says what was kept.
    """
    rates = pricing["models"]
    if facts is None:
        facts = getattr(recs, "facts", None)
    sessions, sessions_coverage = cap_sessions(build_sessions(recs, facts))

    def dump(buckets):
        return [dict(key=k, **v) for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"])]

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "currency": currency or pricing["currency"],
        "timezone": timezone_name(tz),
        "pricing": {
            "models": rates,
            "currency": pricing["currency"],
            "version": pricing["version"],
            "sources": pricing["sources"],
        },
        "messages": len(recs),
        "span": [recs[0]["day"], recs[-1]["day"]] if recs else None,
        "totals": _agg(recs, lambda r: "all")["all"] if recs else {},
        "coverage": coverage(recs),
        "models": {m: model_entry(m, rates) for m in sorted({r["model"] for r in recs})},
        "by_day": dump(_agg(recs, lambda r: r["day"])),
        "by_model": dump(_agg(recs, lambda r: r["model"])),
        "by_source": dump(_agg(recs, lambda r: r["source"])),
        "by_project": dump(_agg(recs, lambda r: r["project"])),
        "by_day_source": [
            dict(key=list(k), **v) for k, v in _agg(recs, lambda r: (r["day"], r["source"])).items()
        ],
        "hours": build_hours(recs),
        "speed": build_speed(recs),
        "sessions": sessions,
        "sessions_coverage": sessions_coverage,
        "sessions_basis": SESSIONS_BASIS,
        "events": list(events or []),
        # Fact table: one row per (day, tool, model, project). The dashboard
        # re-aggregates from this so its week/month/all toggle needs no reload.
        "cells": [
            [k[0], k[1], k[2], k[3], v["n"], v["in"], v["out"], v["cr"], v["cw"], round(v["cost"], 8), False]
            for k, v in _agg(recs, lambda r: (r["day"], r["source"], r["model"], r["project"])).items()
        ],
        # Same row plus the sub-repo name (index 10) and archived (index 11), only for projects that
        # merge several folders; the dashboard's project drawer reads it.
        "sub_cells": [
            [k[0], k[1], k[2], k[3], v["n"], v["in"], v["out"], v["cr"], v["cw"], round(v["cost"], 8), k[4], False]
            for k, v in _agg(
                [r for r in recs if r["project"] in PARENTS],
                lambda r: (r["day"], r["source"], r["model"], r["project"], r["sub"]),
            ).items()
        ],
    }


# ---------------------------------------------------------------- reporting

def _agg(recs, keyfn):
    buckets = defaultdict(lambda: {"n": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "total": 0, "cost": 0.0})
    for r in recs:
        b = buckets[keyfn(r)]
        b["n"] += 1
        for k in ("in", "out", "total"):
            b[k] += r[k]
        b["cr"] += r["cr"]
        b["cw"] += r["cw5m"] + r["cw1h"]
        b["cost"] += r["cost"]
    return buckets


CUR = {"code": "USD", "symbol": "$", "per_usd": 1.0}


def money(usd: float) -> str:
    """Format a USD amount in the configured display currency."""
    v = usd * CUR["per_usd"]
    return f"{CUR['symbol']}{v:,.2f}"


def human(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return str(int(n))


def table(title, buckets, limit=None, sort_by="cost"):
    if sort_by == "key":
        rows = sorted(buckets.items())
    else:
        rows = sorted(buckets.items(), key=lambda kv: -kv[1][sort_by])
    if limit:
        rows = rows[:limit]
    w = max([len(str(k)) for k, _ in rows] + [len(title)]) if rows else len(title)
    print(f"\n\033[1m{title}\033[0m")
    print(f"  {'':<{w}}  {'msgs':>6} {'input':>8} {'output':>8} {'cache r':>9} {'cache w':>8} {'cost ('+CUR['code']+')':>13}")
    for k, b in rows:
        print(
            f"  {str(k):<{w}}  {b['n']:>6} {human(b['in']):>8} {human(b['out']):>8} "
            f"{human(b['cr']):>9} {human(b['cw']):>8} {money(b['cost']):>13}"
        )


def report(recs, args, tz):
    if not recs:
        print("No usage found. Checked:\n  " + CLAUDE_DIR + "\n  " + OMP_DIR)
        return

    zone = timezone_name(tz)
    tot = _agg(recs, lambda r: "all")["all"]
    days = len({r["day"] for r in recs})
    span = f"{recs[0]['day']} -> {recs[-1]['day']}"

    if args.days == 1:
        span, days_note = "today", ""
    else:
        days_note = f"{days} active days, "
    print(f"\n\033[1mToken usage\033[0m  {span}  ({days_note}{len(recs)} messages, {zone})")
    print(f"  billable in   {human(tot['in']):>13}")
    print(f"  output        {human(tot['out']):>13}")
    print(f"  cache read    {human(tot['cr']):>13}")
    print(f"  cache write   {human(tot['cw']):>13}")
    print(f"  \033[1mtotal        {human(tot['total']):>13}\033[0m")
    per_day = "" if args.days == 1 else f"   (~{money(tot['cost']/max(days,1))}/active day)"
    print(f"  \033[1mcost        {money(tot['cost']):>13}\033[0m{per_day}")

    unpriced = sorted({r["model"] for r in recs if r["priced"] is False})
    if unpriced:
        print(f"  \033[33mno rate for: {', '.join(unpriced)} - counted as $0\033[0m")
    estimated = sorted({r["model"] for r in recs if r["priced"] == "estimated"})
    if estimated:
        print(f"  \033[33munconfirmed rate for: {', '.join(estimated)} - priced, not verified\033[0m")

    table("By tool", _agg(recs, lambda r: r["source"]))
    table("By model", _agg(recs, lambda r: r["model"]))
    table(f"By project (top {args.top})", _agg(recs, lambda r: r["project"]), limit=args.top)
    if args.daily:
        table("By day", _agg(recs, lambda r: r["day"]), limit=args.daily, sort_by="cost")
    if args.days == 1:
        table(f"By hour ({zone})", _agg(recs, lambda r: r["hour"]), sort_by="key")
    print()


# ---------------------------------------------------------------- cli

COMMANDS = ("summary", "daily", "dashboard", "json", "raw", "clear-cache")
PERIODS = {"today": 1, "week": 7, "month": 30, "all": None}
DASHBOARD_PATH = os.path.expanduser("~/.cache/token-tracker/dashboard.html")

USAGE = """\
tokens [command] [period] [options]

commands:
  summary        tables by tool, model and project (default)
  daily          summary plus a per-day table
  dashboard      build the HTML dashboard and open it in the browser
  json | raw     aggregates / every priced message, as JSON
  clear-cache    delete the parse cache

period:
  today | week | month | all (default) | a number of days (whole days in --tz, today included)

examples:
  tokens                 tokens week
  tokens dashboard       tokens dashboard month
  tokens today           tokens omp week
  tokens daily 14
"""


def _parse_words(p, words):
    """Read the free-form positionals: a command, a period, a tool, in any order."""
    cmd = days = None
    period_given = False
    sources = []
    for w in words:
        wl = w.lower()
        if wl in COMMANDS or wl in ("dash", "html"):
            if cmd:
                p.error(f"more than one command: {cmd}, {w}")
            cmd = "dashboard" if wl in ("dash", "html") else wl
        elif wl in PERIODS:
            days, period_given = PERIODS[wl], True
        elif w.isdigit() and int(w) > 0:
            days, period_given = int(w), True
        elif wl in ("claude", "claude-code", "omp"):
            sources.append("omp" if wl == "omp" else "claude-code")
        else:
            p.error(f"unknown word {w!r}; try: tokens --help")
    return cmd or "summary", days, period_given, sources


def _open_in_browser(path):
    """Detach the browser so a slow xdg-open never holds the terminal."""
    import subprocess
    import webbrowser
    opener = shutil.which("xdg-open") if sys.platform.startswith("linux") else None
    if opener:
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif not webbrowser.open("file://" + path):
        print(f"open it manually: file://{path}")


def main():
    p = argparse.ArgumentParser(prog="tokens", usage=USAGE, add_help=True,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("--usd", action="store_true", help="costs in USD instead of the configured currency")
    p.add_argument("--harness", action="store_true", help="also report cache, timing and resource metrics")
    p.add_argument("--top", type=int, default=10, help="projects to show (default 10)")
    p.add_argument("--no-open", action="store_true", help="dashboard: write the file but don't open it")
    p.add_argument("--tz", default="local", metavar="ZONE",
                   help="cut days, hours and periods in this IANA zone (default: system local, e.g. Asia/Kolkata)")
    p.add_argument("--events", metavar="PATH",
                   help='dated labels for the charts: a JSON array of {"date": "YYYY-MM-DD", "label": "..."}; default: events.json beside tokentrack.py')
    # Old flag spellings. Still accepted, kept out of --help.
    hide = argparse.SUPPRESS
    p.add_argument("--days", type=int, help=hide)
    p.add_argument("--period", choices=list(PERIODS), help=hide)
    p.add_argument("--source", choices=["claude-code", "omp"], action="append", help=hide)
    p.add_argument("--daily", type=int, nargs="?", const=14, help=hide)
    p.add_argument("--json", action="store_true", help=hide)
    p.add_argument("--raw", action="store_true", help=hide)
    p.add_argument("--html", nargs="?", const=DASHBOARD_PATH, metavar="PATH", help=hide)
    p.add_argument("--pricing", help=hide)
    p.add_argument("--rate", type=float, help=hide)
    p.add_argument("--no-cache", action="store_true", help=hide)
    p.add_argument("--clear-cache", action="store_true", help=hide)
    args = p.parse_args()

    cmd, days, period_given, sources = _parse_words(p, args.words)
    if period_given:
        args.days = days
    elif args.period:
        args.days = PERIODS[args.period]
    args.source = (args.source or []) + sources
    if cmd == "daily" and args.daily is None:
        args.daily = days if period_given and days else 14
    args.json = args.json or cmd == "json"
    args.raw = args.raw or cmd == "raw"
    args.clear_cache = args.clear_cache or cmd == "clear-cache"
    # The dashboard toggles week/month/all itself, so it gets every day of data;
    # the period only picks which range it opens on. An odd day count still filters.
    initial_days = 0
    if cmd == "dashboard":
        args.html = args.html or DASHBOARD_PATH
        if args.days in (7, 30):
            initial_days, args.days = args.days, None
    open_after = cmd == "dashboard" and not args.no_open

    if args.clear_cache:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print(f"cleared {CACHE_DIR}")
        return

    pricing = load_pricing_doc(args.pricing)
    rates = pricing["models"]
    cur = pricing["currency"]
    if args.usd:
        cur = {"code": "USD", "symbol": "$", "per_usd": 1.0}
    if args.rate:
        cur = dict(cur, per_usd=args.rate)
    CUR.update(cur)
    try:
        tz = resolve_tz(args.tz)
    except (KeyError, ValueError):
        p.error(f"unknown timezone {args.tz!r}; use an IANA name such as Asia/Kolkata, or 'local'")

    events = []
    if args.json or args.html:
        try:
            events = load_events(args.events)
        except EventsError as e:
            p.error(str(e))

    sources = tuple(args.source or ["claude-code", "omp"])
    harness = None
    if args.harness:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)  # so --harness works through a symlinked entrypoint
        import harness as harness_mod
        harness, recs = harness_mod.run(rates, args.days, sources, use_cache=not args.no_cache)
    else:
        global CACHE
        CACHE = ParseCache(enabled=not args.no_cache)
        recs = collect(rates, args.days, sources, tz=tz)
        CACHE.evict()

    if args.raw:
        json.dump(recs, sys.stdout, indent=1)
        return
    if args.json or args.html:
        payload = build_payload(recs, pricing, tz, CUR, events=events)
        if harness:
            payload["harness"] = harness
        if args.html:
            payload["initial_days"] = initial_days
            tpl = os.path.join(HERE, "dashboard_template.html")
            with open(tpl) as fh:
                # "</" would close the <script> element early if any provenance string contained it.
                html = fh.read().replace("/*__DATA__*/", json.dumps(payload).replace("</", "<\\/"))
            out_path = os.path.abspath(os.path.expanduser(args.html))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w") as fh:
                fh.write(html)
            span = f"{payload['span'][0]} -> {payload['span'][1]}" if recs else "no data"
            print(f"wrote {out_path}  ({len(recs)} messages, {span})")
            if open_after:
                _open_in_browser(out_path)
            return
        json.dump(payload, sys.stdout, indent=1)
        return

    report(recs, args, tz)
    if harness:
        harness_mod.render(harness)


if __name__ == "__main__":
    main()
