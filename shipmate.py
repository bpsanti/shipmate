#!/usr/bin/env python3
"""shipmate: a small local dashboard for a Firstmate home.

One page, read live from the home's own records on every load and on every 30 s
refresh:

  board         the queue as the first lane (with "+ Add task"), then one lane per
                phase of the live workers; empty phases shrink to thin strips
  sidebar       today's tokens, firstmate's own session (context against the window,
                growth, compactions, warning past a threshold) and each Claude
                account's quota (quota-axi, cached 60 s), tightest first
  detail drawer read-only facts behind any card: the captain's intent from the brief,
                phase timeline, latest status lines, PR checks (cached), tokens,
                context, idle time, report link
  usage         tokens per day and per delivered task, folded under the board
  ship view     the same snapshot drawn as a harbour: projects as ships, agents as hands
                whose place on board is their phase (switch with "Ship" in the header)

Read-only over the home, except the add-task form, which runs the same backlog
command firstmate uses (`bin/fm-tasks-axi.sh add ... --queue`) and then the inbox
note command (`bin/fm-inbox.sh note`). Standard library only; Python 3.9 or newer.

Usage:
  shipmate.py [--home DIR] [--fm-root DIR] [--claude-dir DIR]... [--port N] [--once] [--demo]

  --home        Firstmate home (default: $FM_HOME; one of the two is required)
  --fm-root     Firstmate code root holding bin/ (default: $FM_ROOT, else the home)
  --claude-dir  extra Claude config directory to read transcripts from; repeatable,
                also $SHIPMATE_CLAUDE_DIRS (path-separator list). The home's
                config/claude-accounts entries and ~/.claude are always read.
  --port        TCP port on 127.0.0.1 (default: $SHIPMATE_PORT, else 8737)
  --context-limit N   firstmate's context window in tokens ($SHIPMATE_CONTEXT_LIMIT;
                default: inferred, 1M when the session already went past 200k)
  --context-warn P    warn past P percent of that window ($SHIPMATE_CONTEXT_WARN; 60)
  --once        print the JSON snapshot and exit instead of serving
  --demo        serve a made-up home instead of a real one (no home needed)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import glob
import hmac
import html
import json
import os
import random
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8737
REFRESH_SECONDS = 30
QUOTA_TTL = 60
CREW_TTL = 20
SPAWN_SLACK = 120          # seconds: a session's first line lands a little after the task record is written
DAYS = 7
GROWTH_HOURS = 3
CONTEXT_WINDOWS = (200_000, 1_000_000)   # Claude Code context windows; the transcript does not record which one
DEFAULT_CONTEXT_WARN = 60                # percent of the window
CHECKS_TTL = 120                         # PR checks are read from the forge at most this often per PR
STATUS_TAIL = 8
RE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_BODY = 64 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- configuration


class Config:
    def __init__(self, home, fm_root=None, claude_dirs=(), port=DEFAULT_PORT, quota_cmd="quota-axi",
                 context_limit=None, context_warn=DEFAULT_CONTEXT_WARN, gh_cmd="gh"):
        self.home = os.path.abspath(home)
        self.fm_root = os.path.abspath(fm_root or self.home)
        self.port = port
        self.quota_cmd = quota_cmd
        self.context_limit = context_limit      # tokens, or None to infer
        self.context_warn = context_warn        # percent
        self.gh_cmd = gh_cmd                    # only the detail view's PR checks use it
        self.accounts = read_accounts(os.path.join(self.home, "config"))
        dirs = [os.path.expanduser("~/.claude")] + [d for _, d in self.accounts]
        dirs += [os.path.abspath(os.path.expanduser(d)) for d in claude_dirs if d]
        self.claude_dirs = list(dict.fromkeys(dirs))

    def bin(self, name):
        return os.path.join(self.fm_root, "bin", name)

    def env(self):
        env = dict(os.environ)
        env["FM_HOME"] = self.home
        return env


def read_accounts(config_dir):
    """config/claude-accounts: one entry per line, `ordinary` or an absolute config dir."""
    out = []
    try:
        with open(os.path.join(config_dir, "claude-accounts"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "ordinary":
                    out.append(("ordinary", os.path.expanduser("~/.claude")))
                elif os.path.isabs(line):
                    out.append((line, line))
    except OSError:
        pass
    return out


def account_label(path):
    """Short account name: `ordinary` for ~/.claude, else the config directory's name."""
    if not path:
        return "unknown"
    path = os.path.abspath(os.path.expanduser(path))
    if path == os.path.abspath(os.path.expanduser("~/.claude")):
        return "ordinary"
    return os.path.basename(path.rstrip("/")) or path


# ---------------------------------------------------------------- small readers


def read_kv(path):
    d = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    d.setdefault(k, v)
    except OSError:
        pass
    return d


RE_STATUS = re.compile(r"^([a-z][a-z-]*)((?:\s*\[[^\]]*\])*):\s?(.*)$")   # `state [k=v ..] [k=v ..]: text`
RE_PR = re.compile(r"https://[^\s)\"'<>]+?/(?:pull/\d+|-/merge_requests/\d+)")


def read_status(path):
    """state/<id>.status: `<state> [at=<epoch> key=..]: <text>` per line; the fields may also
    sit in separate brackets (`needs-decision [key=x] [at=5]: ...`)."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        m = RE_STATUS.match(line)
        if not m:
            continue
        ma = re.search(r"\bat=(\d+)\b", m.group(2))
        mk = re.search(r"\bkey=([^\s\]]+)", m.group(2))
        out.append({"state": m.group(1), "at": int(ma.group(1)) if ma else None, "key": mk.group(1) if mk else None,
                    "text": m.group(3)})
    return out


def spawn_epoch(meta):
    """`spawn_gen=s<epoch>.<pid>.<n>`, the same reading bin/fm-capacity.sh uses."""
    m = re.match(r"^s(\d+)\.", meta.get("spawn_gen") or "")
    return int(m.group(1)) if m else None


def run(cmd, env=None, timeout=30):
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, PermissionError) as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %d s" % timeout


# ---------------------------------------------------------------- TOON tables (tasks-axi list)


def split_toon_row(row):
    vals, cur, i, quoted = [], [], 0, False
    while i < len(row):
        c = row[i]
        if quoted:
            if c == "\\" and i + 1 < len(row):
                nxt = row[i + 1]
                cur.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                quoted = False
            else:
                cur.append(c)
        elif c == '"':
            quoted = True
        elif c == ",":
            vals.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    vals.append("".join(cur))
    return vals


def parse_toon_table(text, name):
    """Rows of the `name[N]{a,b,c}:` table in a TOON document, as dicts."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*%s\[\d+\]\{([^}]*)\}:\s*$" % re.escape(name), line)
        if not m:
            continue
        fields = m.group(1).split(",")
        out, indent = [], None
        for row in lines[i + 1:]:
            lead = len(row) - len(row.lstrip(" "))
            if not row.strip() or lead == 0 or (indent is not None and lead != indent):
                break
            indent = lead
            vals = split_toon_row(row.strip())
            if len(vals) == len(fields):
                out.append(dict(zip(fields, vals)))
        return out
    return []


def clean_title(t):
    # tasks-axi cuts long titles with "\n... (truncated, N chars total ...)"
    if "\n... (truncated" in t:
        return t.split("\n... (truncated", 1)[0].rstrip() + "…"
    return t


LIST_FIELDS = "blocked,blocked_by,created,deps,held,hold_kind,hold_reason,hold_until,priority,links"


def read_backlog(cfg):
    rc, out, err = run([cfg.bin("fm-tasks-axi.sh"), "list", "--fields", LIST_FIELDS], env=cfg.env())
    if rc != 0:
        return None, (err or out).strip()[:300] or "exit %d" % rc
    rows = parse_toon_table(out, "tasks")
    for r in rows:
        r["title"] = clean_title(r.get("title", ""))
        for k, v in list(r.items()):
            if v == "-":
                r[k] = ""
    return rows, None


def read_projects(home):
    """data/projects.md: `- <name> [mode] - description` per registered project."""
    out = []
    try:
        with open(os.path.join(home, "data", "projects.md"), encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^- (\S+)", line)
                if m:
                    out.append(m.group(1))
    except OSError:
        pass
    return out


def read_capacity(home):
    """config/project-capacity: `<project> <n>` per line (bin/fm-project-capacity-lib.sh owns the format)."""
    caps = {}
    try:
        with open(os.path.join(home, "config", "project-capacity"), encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s or s == "#" or re.match(r"^#\s", s):
                    continue
                parts = s.rsplit(None, 1)
                if len(parts) == 2 and parts[1].isdigit():
                    caps[parts[0]] = int(parts[1])
    except OSError:
        pass
    return caps


# ---------------------------------------------------------------- transcripts


def encode_dir(path):
    """Claude Code's project folder name: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


RE_CITE = re.compile(r"state/([A-Za-z0-9._-]+)\.inbox")
RE_SEG = re.compile(r"&&|\|\||;|\n|\|")
RE_PREFIX = re.compile(r"^(?:\(|\{|!|time\s+|command\s+|env\s+|nohup\s+|timeout\s+\S+\s+|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
# a CI read in command position: segment start, a $( ) substitution, or the body of a loop or if
# (`for ..; do out=$(gh pr checks ..)`); a quoted mention such as grep 'gh pr checks' does not count
RE_CI = re.compile(r"(?:^|\$\(|`|\b(?:do|then|else|until|while)\s+)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\$\(\s*)?"
                   r"gh(?:-axi)?\s+(?:pr\s+checks|run\s+(?:watch|view|list)|checks\b)")
RE_COMMIT = re.compile(r"^git\s+(?:-C\s+\S+\s+)?commit\b")
RE_PR_CREATE = re.compile(r"^gh(?:-axi)?\s+pr\s+create\b")
LAUNCH_MARK = "FIRSTMATE_OP: v1 launch-brief"


def segments(cmd):
    out = []
    for s in RE_SEG.split(cmd or ""):
        s = RE_PREFIX.sub("", s.strip())
        if s:
            out.append(s)
    return out


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class FileData:
    """What one transcript file says: messages with usage, milestone events, task citations."""
    __slots__ = ("msgs", "events", "cites", "launch", "first", "last", "compactions")

    def __init__(self):
        self.msgs = {}      # message id -> (ts, model, input, cache_write, cache_read, output)
        self.events = []    # (ts, kind, arg) with kind edit / commit / ci / pr
        self.compactions = []   # (ts, trigger, tokens before, tokens after) per compact_boundary record
        self.cites = set()
        self.launch = False
        self.first = self.last = None


def parse_file(path):
    fd = FileData()
    pr_calls = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return fd
    with fh:
        for line in fh:
            if "state/" in line:
                fd.cites.update(RE_CITE.findall(line))
            if not fd.launch and LAUNCH_MARK in line:
                fd.launch = True
            if '"assistant"' not in line and '"tool_result"' not in line and '"compact_boundary"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = parse_ts(r.get("timestamp"))
            if ts is not None:
                fd.first = ts if fd.first is None else min(fd.first, ts)
                fd.last = ts if fd.last is None else max(fd.last, ts)
            msg = r.get("message") or {}
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
                # Claude Code writes one of these at every compaction, manual or automatic
                cm = r.get("compactMetadata") or {}
                fd.compactions.append((ts, cm.get("trigger"), cm.get("preTokens"), cm.get("postTokens")))
                continue
            if r.get("type") == "assistant":
                mid = msg.get("id")
                if not mid:
                    continue
                u = msg.get("usage") or {}
                rec = (ts, msg.get("model"), u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                       u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0)
                old = fd.msgs.get(mid)
                # one message is written as several lines (one per content block): keep the fullest usage
                # and the earliest time
                if old is None or rec[5] >= old[5]:
                    times = [x for x in (rec[0], old[0] if old else None) if x is not None]
                    fd.msgs[mid] = (min(times) if times else None,) + rec[1:]
                for b in msg.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = b.get("name"), b.get("input") or {}
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        fd.events.append((ts, "edit", inp.get("file_path") or inp.get("notebook_path") or ""))
                    elif name == "Bash":
                        for sg in segments(inp.get("command") or ""):
                            if RE_CI.search(sg):
                                fd.events.append((ts, "ci", ""))
                            elif RE_COMMIT.search(sg):
                                fd.events.append((ts, "commit", ""))
                            elif RE_PR_CREATE.search(sg):
                                pr_calls[b.get("id")] = ts
                    elif name == "Monitor" and re.search(r"\bgh(?:-axi)?\s+(?:pr\s+checks|run)\b", json.dumps(inp)):
                        fd.events.append((ts, "ci", ""))
            elif r.get("type") == "user" and pr_calls and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pr_calls:
                        c = b.get("content")
                        m = RE_PR.search(c if isinstance(c, str) else json.dumps(c))
                        if m:
                            fd.events.append((ts, "pr", m.group(0)))
    fd.events.sort(key=lambda ev: ev[0] or 0)
    return fd


class TranscriptIndex:
    """Parsed transcripts, re-read only when a file's size or mtime changes."""

    def __init__(self, claude_dirs):
        self.claude_dirs = claude_dirs
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_size, st.st_mtime_ns)
        with self.lock:
            hit = self.cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        fd = parse_file(path)
        with self.lock:
            self.cache[path] = (key, fd)
        return fd

    def project_dirs(self):
        out = []
        for d in self.claude_dirs:
            out += glob.glob(os.path.join(glob.escape(d), "projects", "*"))
        return out

    def session_files(self, folder_names):
        """(main session file, its subagent files) under the given project folder names."""
        out = []
        for d in self.claude_dirs:
            for name in folder_names:
                base = os.path.join(d, "projects", name)
                for f in glob.glob(os.path.join(glob.escape(base), "*.jsonl")):
                    out.append((f, glob.glob(os.path.join(glob.escape(f[:-6]), "subagents", "*.jsonl"))))
        return out

    def find_session(self, prefix):
        if not re.match(r"^[A-Za-z0-9-]{4,}$", prefix or ""):
            return None, []
        for d in self.claude_dirs:
            for f in glob.glob(os.path.join(glob.escape(d), "projects", "*", prefix + "*.jsonl")):
                return f, glob.glob(os.path.join(glob.escape(f[:-6]), "subagents", "*.jsonl"))
        return None, []


def sum_tokens(fds, since=None, seen=None):
    """New (input + cache write + output) and cache-read tokens, counted once per message id."""
    seen = set() if seen is None else seen
    new = cache = n = 0
    models = {}
    for fd in fds:
        for mid, (ts, model, inp, cw, cr, out) in fd.msgs.items():
            if mid in seen or (since is not None and (ts is None or ts < since)):
                continue
            seen.add(mid)
            new += inp + cw + out
            cache += cr
            n += 1
            if model and not model.startswith("<"):
                models[model] = models.get(model, 0) + 1
    return {"new": new, "cache": cache, "calls": n, "model": max(models, key=models.get) if models else None}


# ---------------------------------------------------------------- firstmate's own session


def read_lock_session(home):
    """state/.lock-session: the Claude session id that holds this home's firstmate lock
    (written only by bin/fm-lock.sh)."""
    try:
        with open(os.path.join(home, "state", ".lock-session"), encoding="utf-8") as fh:
            sid = fh.readline().strip()
    except OSError:
        return None
    return sid if re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{3,}$", sid) else None


def firstmate_session(cfg, index, now):
    """Context and spend of the supervisor's own Claude session: the session named by the
    home's lock in the Claude project folder of FM_HOME, else that folder's newest session."""
    files = index.session_files({encode_dir(cfg.home)})
    if not files:
        return None
    sid = read_lock_session(cfg.home)
    chosen = next((f for f in files if os.path.basename(f[0])[:-6] == sid), None) if sid else None
    how = "the session lock"
    if chosen is None:
        how = "newest session in the home's folder" + (" (the lock names a session not found here)" if sid else "")
        try:
            chosen = max(files, key=lambda f: os.stat(f[0]).st_mtime)
        except OSError:
            return None
    fd = index.get(chosen[0])
    if fd is None:
        return None
    subs = [x for x in (index.get(s) for s in chosen[1]) if x is not None]
    calls = sorted((ts, inp + cw + cr, model) for ts, model, inp, cw, cr, _o in fd.msgs.values()
                   if ts is not None and model and not model.startswith("<"))
    comps = sorted((c for c in fd.compactions if c[0] is not None), key=lambda c: c[0])
    ctx = calls[-1][1] if calls else None
    model = calls[-1][2] if calls else None
    # window: configured, else inferred from what the session has already held
    seen = max([c[1] for c in calls] + [c[2] or 0 for c in comps] + [0])
    limit, limit_how = infer_window(model, seen, cfg.context_limit)
    # growth: against the last call before the window start, or the last compaction inside it
    start = now - GROWTH_HOURS * 3600
    base = next((c for c in reversed(calls) if c[0] <= start), None)
    base_t, base_ctx, since_compaction = (base[0], base[1], False) if base else (None, None, False)
    last_comp = comps[-1] if comps else None
    if last_comp and (base_t is None or last_comp[0] > base_t) and last_comp[3] is not None:
        base_t, base_ctx, since_compaction = last_comp[0], last_comp[3], True
    midnight = time.mktime(dt.date.fromtimestamp(now).timetuple())
    today = sum_tokens([fd] + subs, since=midnight)
    return {
        "session": os.path.basename(chosen[0])[:-6], "found_by": how, "model": model,
        "context": ctx, "last_call": calls[-1][0] if calls else None,
        "limit": limit, "limit_how": limit_how,
        "percent": (100.0 * ctx / limit) if (ctx is not None and limit) else None,
        "warn_at": cfg.context_warn,
        "growth": (ctx - base_ctx) if (ctx is not None and base_ctx is not None) else None,
        "growth_since": base_t, "growth_since_compaction": since_compaction,
        "today_new": today["new"], "today_cache": today["cache"],
        "compactions": len(comps),
        "last_compaction": {"at": last_comp[0], "trigger": last_comp[1], "before": last_comp[2], "after": last_comp[3]}
        if last_comp else None,
    }


# ---------------------------------------------------------------- live agents


SHIP_PHASES = ["exploring", "implementing", "PR open", "waiting for CI", "ready to merge"]
SCOUT_PHASES = ["exploring", "writing the report", "report delivered"]
CREW_TONE = {"working": "online", "paused": "info", "blocked": "danger", "failed": "danger", "done": "online"}


def parse_crew_state(text):
    """bin/fm-crew-state.sh prints `state: <s> · source: <src> · <detail>`."""
    first = (text or "").strip().splitlines()
    m = re.match(r"^state:\s*(\S+)\s*·\s*source:\s*(\S+)\s*(?:·\s*(.*))?$", first[0] if first else "")
    if not m:
        return {"state": "unknown", "source": "none", "detail": first[0] if first else ""}
    return {"state": m.group(1), "source": m.group(2), "detail": m.group(3) or ""}


def task_sessions(index, meta, tid):
    """The task's own sessions: files in its worktree's project folder with activity after the
    spawn. A pooled worktree's folder holds the sessions of every task that used that slot,
    so the spawn time, not the folder, decides what belongs to this task.
    Returns (all files with subagents, cites the task, spawn, main session files only)."""
    wt = meta.get("worktree")
    spawn = spawn_epoch(meta)
    if not wt or spawn is None:
        return [], False, spawn, []
    since = spawn - SPAWN_SLACK
    picked, mains, cites = [], [], False
    for main, subs in index.session_files({encode_dir(wt), re.sub(r"[/.]", "-", wt)}):
        fd = index.get(main)
        if fd is None or fd.last is None or fd.last < since:
            continue
        picked.append(fd)
        mains.append(fd)
        cites = cites or tid in fd.cites
        picked += [x for x in (index.get(s) for s in subs) if x is not None]
    return picked, cites, spawn, mains


def infer_window(model, seen, configured=None):
    """(window, how). Claude Code transcripts do not record the context window, so it is the
    configured one, else 1M when the model id says [1m] or the session already held more than
    200k, else 200k."""
    if configured:
        return configured, "configured"
    big = "[1m]" in (model or "") or (seen or 0) > CONTEXT_WINDOWS[0]
    return (CONTEXT_WINDOWS[1] if big else CONTEXT_WINDOWS[0]), "inferred"


def last_context(fds, since=None):
    """(context of the latest model call, its model, largest context seen) over the given files."""
    calls = sorted((ts, inp + cw + cr, model) for fd in fds for ts, model, inp, cw, cr, _o in fd.msgs.values()
                   if ts is not None and model and not model.startswith("<") and (since is None or ts >= since))
    if not calls:
        return None, None, 0
    return calls[-1][1], calls[-1][2], max(c[1] for c in calls)


def phase_milestones(kind, meta, status, crew, fds, home, tid, spawn):
    """When each phase was reached and which one is current, from recorded milestones only.
    Returns {"times": [epoch or None per phase], "current": index or None (cannot tell),
    "since": start of the current phase, "why": the evidence used, "pr": the PR URL or None}."""
    events = sorted((ev for fd in fds for ev in fd.events if ev[0] is not None), key=lambda ev: ev[0])
    st = crew.get("state")
    done_at = None
    for s in status:
        if s["state"] == "done" and s["at"]:
            done_at = s["at"]
    if kind == "scout":
        report = os.path.join(home, "data", tid, "report.md")
        rep_t = None
        has_report = os.path.exists(report)
        if has_report:
            rep_t = next((ev[0] for ev in events if ev[1] == "edit" and ev[2] == report), None)
            if rep_t is None:
                try:
                    stt = os.stat(report)
                    rep_t = getattr(stt, "st_birthtime", stt.st_mtime)
                except OSError:
                    rep_t = None
        times = [spawn, rep_t, done_at if st == "done" else None]
        if st == "done":
            cur, since, why = 2, done_at, "the worker reported done"
        elif has_report:
            cur, since, why = 1, rep_t, "the report file exists"
        elif fds:
            cur, since, why = 0, spawn, "no report written yet"
        else:
            cur, since, why = None, None, "no session transcript and no report"
        return {"times": times, "current": cur, "since": since, "why": why, "pr": None}

    pr, pr_t = meta.get("pr") or None, None
    for s in status:
        m = RE_PR.search(s["text"])
        if m:
            pr = pr or m.group(0)
            if s["at"] and (pr_t is None or s["at"] < pr_t):
                pr_t = s["at"]
    for ev in events:
        if ev[1] == "pr":
            pr = pr or ev[2]
            pr_t = ev[0] if pr_t is None else min(pr_t, ev[0])
            break
    wt = (meta.get("worktree") or "").rstrip("/") + "/"
    impl_t = next((ev[0] for ev in events if (ev[1] == "edit" and ev[2].startswith(wt)) or ev[1] == "commit"), None)
    after = [ev for ev in events if pr_t is None or ev[0] >= pr_t] if pr else []
    first_ci = next((ev[0] for ev in after if ev[1] == "ci"), None)
    times = [spawn, impl_t, pr_t, first_ci, done_at if (pr and st == "done") else None]
    if pr and st == "done":
        cur, since, why = 4, done_at, "PR open and the worker reported done"
    elif pr:
        last_work = next((ev for ev in reversed(after) if ev[1] in ("edit", "commit", "pr")), None)
        ci = [ev for ev in after if ev[1] == "ci" and (last_work is None or ev[0] > last_work[0])]
        if ci and st in ("working", "paused"):
            cur, since, why = 3, ci[0][0], "latest activity is watching CI"
        else:
            cur, since, why = 2, pr_t, "PR open"
    elif impl_t is not None:
        cur, since, why = 1, impl_t, "first file edit or commit in the worktree"
    elif fds:
        cur, since, why = 0, spawn, "no edit in the worktree yet"
    else:
        cur, since, why = None, None, "no session transcript and no PR"
    return {"times": times, "current": cur, "since": since, "why": why, "pr": pr}


def derive_phase(kind, meta, status, crew, fds, home, tid, spawn):
    """(phase index, phase start, evidence); index None when the evidence cannot tell."""
    m = phase_milestones(kind, meta, status, crew, fds, home, tid, spawn)
    return m["current"], m["since"], m["why"]


class CrewCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def get(self, cfg, ids):
        now = time.time()
        with self.lock:
            todo = [i for i in ids if i not in self.data or now - self.data[i][0] > CREW_TTL]
        if todo:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
                res = dict(zip(todo, ex.map(lambda i: run([cfg.bin("fm-crew-state.sh"), i], env=cfg.env(), timeout=25), todo)))
            with self.lock:
                for i, (rc, out, err) in res.items():
                    self.data[i] = (now, parse_crew_state(out) if rc == 0 else
                                    {"state": "unknown", "source": "none", "detail": (err or "")[:200]})
        with self.lock:
            return {i: self.data[i][1] for i in ids}


def live_agents(cfg, index, crew_cache, backlog_by_id):
    metas = []
    for p in sorted(glob.glob(os.path.join(glob.escape(cfg.home), "state", "*.meta"))):
        meta = read_kv(p)
        if meta.get("kind") != "secondmate":
            metas.append((os.path.basename(p)[:-5], meta))
    crews = crew_cache.get(cfg, [t for t, _ in metas]) if metas else {}
    out = []
    for tid, meta in metas:
        status = read_status(os.path.join(cfg.home, "state", tid + ".status"))
        crew = crews.get(tid, {"state": "unknown"})
        fds, cites, spawn, mains = task_sessions(index, meta, tid)
        kind = meta.get("kind") or "ship"
        ms = phase_milestones(kind, meta, status, crew, fds, cfg.home, tid, spawn)
        since = spawn - SPAWN_SLACK if spawn else None
        tok = sum_tokens(fds, since=since) if fds else None
        ctx, ctx_model, seen = last_context(mains, since)
        window, window_how = infer_window(ctx_model, seen)
        pr = ms["pr"]
        for s in reversed(status):
            m = RE_PR.search(s["text"])
            if not pr and m:
                pr = m.group(0)
        b = backlog_by_id.get(tid, {})
        harness = meta.get("harness") or "claude"
        out.append({
            "id": tid, "title": b.get("title") or "", "kind": kind,
            "project": os.path.basename((meta.get("project") or "").rstrip("/")) or b.get("repo") or "",
            "model": meta.get("model") or "", "model_seen": tok["model"] if tok else None,
            "effort": meta.get("effort") or "", "harness": harness,
            "account": account_label(meta.get("account")) if harness == "claude" else harness,
            "phases": SCOUT_PHASES if kind == "scout" else SHIP_PHASES,
            "phase": ms["current"], "phase_since": ms["since"], "phase_why": ms["why"], "milestones": ms["times"],
            "crew": crew.get("state", "unknown"), "spawn": spawn, "pr": pr,
            "tokens": tok, "cites": cites, "sessions": len(fds),
            "context": ctx, "context_limit": window if ctx is not None else None, "context_how": window_how,
            "last_activity": max((fd.last for fd in fds if fd.last), default=None),
            "decisions": open_decisions(status),
        })
    return out


def open_decisions(status):
    """needs-decision and blocked lines that no `resolved` line has closed yet. A keyed one closes
    only with a resolved line carrying the same key, an unkeyed one with a later unkeyed resolved
    line; a later working or done line closes neither."""
    open_ = []
    for s in status:
        if s["state"] in ("needs-decision", "blocked"):
            open_ = [o for o in open_ if not (s.get("key") and o.get("key") == s.get("key"))]
            open_.append({"state": s["state"], "key": s.get("key"), "at": s["at"], "text": s["text"][:240]})
        elif s["state"] == "resolved":
            open_ = [o for o in open_ if o.get("key") != s.get("key")]
    return open_


# ---------------------------------------------------------------- queue


def queue_rows(cfg, backlog):
    if backlog is None:
        return []
    caps = read_capacity(cfg.home)
    holders = {}
    # what holds a place, as bin/fm-project-capacity-lib.sh counts it in this home: a non-secondmate
    # task record with no recorded PR handoff
    for p in glob.glob(os.path.join(glob.escape(cfg.home), "state", "*.meta")):
        meta = read_kv(p)
        if meta.get("kind") == "secondmate" or meta.get("pr"):
            continue
        holders.setdefault(os.path.basename((meta.get("project") or "").rstrip("/")), []).append(os.path.basename(p)[:-5])
    today = dt.date.today().isoformat()
    out = []
    for r in backlog:
        if r.get("state") != "queued":
            continue
        reasons = []
        if r.get("held") == "yes":
            if r.get("hold_until") and r["hold_until"] > today:
                txt = "held until " + fmt_date_iso(r["hold_until"])
                reasons.append(("date", txt, txt))
            if r.get("hold_kind") == "captain" or not r.get("hold_until"):
                reasons.append(("decision", "captain's decision", (r.get("hold_reason") or "captain's decision")[:400]))
        deps = [d for d in re.split(r"[\s,]+", r.get("blocked_by") or "") if d and d != "none"]
        if deps:
            reasons.append(("task", "waits for " + ", ".join(deps), "waits for " + ", ".join(deps)))
        repo = r.get("repo") or ""
        if not reasons and repo in caps:
            h = sorted(holders.get(repo, []))
            if len(h) >= caps[repo]:
                reasons.append(("slot", "build slot %d/%d" % (len(h), caps[repo]),
                                "build slots for %s: %d of %d taken (%s)" % (repo, len(h), caps[repo], ", ".join(h))))
        if not reasons:
            reasons.append(("ready", "ready to dispatch", "nothing holds it: firstmate can dispatch it"))
        out.append({"id": r["id"], "title": r.get("title", ""), "repo": repo, "kind": r.get("kind", ""),
                    "created": r.get("created", ""), "reasons": reasons,
                    "hold_reason": (r.get("hold_reason") or "")[:240]})
    return out


def machine_load():
    """One-minute load average against the CPU count; None where the platform has none."""
    try:
        return {"load": round(os.getloadavg()[0], 2), "cpus": os.cpu_count() or 1}
    except (OSError, AttributeError):
        return None


# ---------------------------------------------------------------- quota


class QuotaCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.at = 0
        self.value = None

    def get(self, cfg):
        with self.lock:
            if self.value is not None and time.time() - self.at < QUOTA_TTL:
                return self.value
            env = dict(os.environ)
            listed = [d for name, d in cfg.accounts if name != "ordinary"]
            if listed:
                env["QUOTA_AXI_CLAUDE_PROFILES"] = os.pathsep.join(listed)
            # --no-credential-refresh: reading quota must never renew or rewrite a credential
            rc, out, err = run([cfg.quota_cmd, "--provider", "claude", "--full", "--json", "--no-credential-refresh"],
                               env=env, timeout=45)
            try:
                doc = json.loads(out)
                self.value = {"lanes": [quota_lane(p) for p in doc.get("providers", []) if p.get("provider") == "claude"],
                              "at": doc.get("generatedAt"), "error": None}
            except ValueError:
                self.value = {"lanes": [], "at": None, "error": (err or out or "no answer from quota-axi").strip()[:300]}
            self.at = time.time()
            return self.value


def quota_lane(p):
    loc = (p.get("accountLocator") or {}).get("path")
    eff = next((x for x in (p.get("quotaSemantics") or {}).get("effectiveAvailability", [])
                if x.get("scope") in ("all_models", "all_products")), None) or {}
    windows = {w.get("id"): w for w in p.get("windows", [])}
    lim = windows.get((eff.get("limitingWindowIds") or [None])[0]) or {}
    runway = eff.get("runway") or {}
    status = (p.get("state") or {}).get("status")
    return {
        # the account's own config directory names it; quota-axi's identity fields are not shown
        "label": account_label(loc) if loc else "account",
        "plan": p.get("plan"),
        "status": status,
        "remaining": eff.get("effectivePercentRemaining"),
        "limit_label": lim.get("label"),
        "resets": parse_ts(lim.get("resetsAt")),
        "runway": runway.get("status"),
        "exhausts": parse_ts(runway.get("projectedExhaustedAt")),
        "windows": [{"label": w.get("label"), "remaining": w.get("percentRemaining"), "resets": parse_ts(w.get("resetsAt"))}
                    for w in p.get("windows", []) if w.get("percentRemaining") is not None],
    }


# ---------------------------------------------------------------- usage


CLASSES = ["workers", "firstmate", "validation", "other"]


def session_class(cfg, folder, fd):
    if folder == encode_dir(cfg.home):
        return "firstmate"
    if fd.launch:
        return "workers"
    if "no-mistakes" in folder:
        return "validation"
    return "other"


def daily_usage(cfg, index, now):
    """Tokens per local day over the last DAYS days, across every transcript, once per message id."""
    start_day = dt.date.fromtimestamp(now) - dt.timedelta(days=DAYS - 1)
    since = time.mktime(start_day.timetuple())
    days = {(start_day + dt.timedelta(days=i)).isoformat(): {c: [0, 0] for c in CLASSES} for i in range(DAYS)}
    seen = set()
    for proj in index.project_dirs():
        folder = os.path.basename(proj)
        for main in glob.glob(os.path.join(glob.escape(proj), "*.jsonl")):
            subs = glob.glob(os.path.join(glob.escape(main[:-6]), "subagents", "*.jsonl"))
            try:
                if max(os.stat(f).st_mtime for f in [main] + subs) < since:
                    continue
            except OSError:
                continue
            fd = index.get(main)
            if fd is None:
                continue
            cls = session_class(cfg, folder, fd)
            for f in [fd] + [index.get(s) for s in subs]:
                if f is None:
                    continue
                for mid, (ts, _m, inp, cw, cr, out) in f.msgs.items():
                    if ts is None or ts < since or mid in seen:
                        continue
                    seen.add(mid)
                    day = days.get(dt.date.fromtimestamp(ts).isoformat())
                    if day is not None:
                        day[cls][0] += inp + cw + out
                        day[cls][1] += cr
    return [{"day": d, "by": v, "new": sum(x[0] for x in v.values()), "cache": sum(x[1] for x in v.values())}
            for d, v in sorted(days.items(), reverse=True)]


def delivered(cfg, index, backlog_by_id, now):
    """Delivered tasks from the session sheets (data/fichas/fichas.jsonl), with the new/cache
    split recomputed from each sheet's own session transcript and its subagents."""
    try:
        with open(os.path.join(cfg.home, "data", "fichas", "fichas.jsonl"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    cutoff = (dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%MZ")
    rows = []
    for line in lines:
        try:
            f = json.loads(line)
        except ValueError:
            continue
        if not isinstance(f, dict) or (f.get("inicio") or "") < cutoff:
            continue
        main, subs = index.find_session(f.get("sessao") or "")
        split = None
        if main:
            split = sum_tokens([x for x in [index.get(main)] + [index.get(s) for s in subs] if x is not None])
            split["subagents"] = len(subs)
        links = backlog_by_id.get(f.get("id"), {}).get("links") or ""
        m = RE_PR.search(links)
        rows.append({"id": f.get("id"), "kind": {"entrega": "ship", "investigacao": "scout"}.get(f.get("tipo"), f.get("tipo")),
                     "model": f.get("modelo"), "start": f.get("inicio"),
                     "minutes": (f.get("minutos") or {}).get("parede"),
                     "sheet_total": (f.get("tokens") or {}).get("total"),
                     "split": split, "pr": m.group(0) if m else None, "pr_number": f.get("pr"),
                     "partial": f.get("parcial")})
    rows.sort(key=lambda r: r["start"] or "", reverse=True)
    return rows


# ---------------------------------------------------------------- detail readers (read only)


def valid_id(tid):
    return bool(tid and RE_ID.match(tid) and ".." not in tid)


def captain_intent(home, tid):
    """The `Captain's intent` section of data/<id>/brief.md, up to the next level 1 or 2 heading."""
    try:
        with open(os.path.join(home, "data", tid, "brief.md"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    out, inside = [], False
    for line in lines:
        if re.match(r"^#{1,3}\s*Captain.s intent\s*$", line.strip(), re.I):
            inside = True
            continue
        if inside and re.match(r"^#{1,2}\s", line):
            break
        if inside:
            out.append(line)
    text = "\n".join(out).strip()
    return text[:6000] if text else None


def report_files(home, tid):
    return [ext for ext in ("md", "html") if os.path.isfile(os.path.join(home, "data", tid, "report." + ext))]


def parse_show(text):
    """`tasks-axi show <id> --full`: `  key: value` lines, values possibly quoted with escapes."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s+([a-z_]+):\s?(.*)$", line)
        if m:
            v = m.group(2)
            out[m.group(1)] = split_toon_row(v)[0] if v.startswith('"') else v
    return out


def summarize_checks(doc):
    counts = {"pass": 0, "fail": 0, "pending": 0, "skipped": 0}
    failing = []
    for c in doc.get("statusCheckRollup") or []:
        if c.get("__typename") == "StatusContext":
            st = (c.get("state") or "").upper()
            bucket = "pass" if st == "SUCCESS" else "pending" if st in ("PENDING", "EXPECTED") else "fail"
            name = c.get("context") or "?"
        else:
            name = c.get("name") or "?"
            if (c.get("status") or "").upper() != "COMPLETED":
                bucket = "pending"
            else:
                con = (c.get("conclusion") or "").upper()
                bucket = ("pass" if con in ("SUCCESS", "NEUTRAL") else "skipped" if con == "SKIPPED" else "fail")
        counts[bucket] += 1
        if bucket == "fail":
            failing.append(name)
    return {"state": doc.get("state"), "draft": doc.get("isDraft"), "counts": counts, "failing": failing[:12],
            "error": None}


class ChecksCache:
    """PR state and checks, read from the forge only when a detail is opened, at most every CHECKS_TTL s."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def get(self, cfg, url):
        if not re.match(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+$", url or ""):
            return {"error": "checks are only read for GitHub pull requests"}
        now = time.time()
        with self.lock:
            hit = self.data.get(url)
            if hit and now - hit[0] < CHECKS_TTL:
                return dict(hit[1], age=int(now - hit[0]))
        rc, out, err = run([cfg.gh_cmd, "pr", "view", url, "--json", "state,isDraft,statusCheckRollup"], timeout=20)
        try:
            val = summarize_checks(json.loads(out)) if rc == 0 else {"error": (err or out).strip()[:200] or "exit %d" % rc}
        except (ValueError, AttributeError):
            val = {"error": "unreadable answer from gh"}
        with self.lock:
            self.data[url] = (now, val)
        return dict(val, age=0)


# ---------------------------------------------------------------- snapshot and the add form


class Panel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.index = TranscriptIndex(cfg.claude_dirs)
        self.crew = CrewCache()
        self.quota = QuotaCache()
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)   # per server run: a page from elsewhere cannot know it
        self.requests = {}                       # form request id -> result: a resubmitted form never queues twice
        self.checks = ChecksCache()
        self.last = None                         # latest snapshot, reused by the detail view while fresh

    def snapshot(self):
        with self.lock, concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            now = time.time()
            quota = ex.submit(self.quota.get, self.cfg)   # the slowest read runs beside the others
            backlog, berr = read_backlog(self.cfg)
            by_id = {r["id"]: r for r in (backlog or [])}
            snap = {
                "now": now,
                "firstmate": firstmate_session(self.cfg, self.index, now),
                "agents": live_agents(self.cfg, self.index, self.crew, by_id),
                "queue": queue_rows(self.cfg, backlog),
                "backlog_error": berr,
                "projects": read_projects(self.cfg.home),
                "daily": daily_usage(self.cfg, self.index, now),
                "delivered": delivered(self.cfg, self.index, by_id, now),
                "machine": machine_load(),
            }
            snap["quota"] = quota.result()
            self.last = snap
            return snap

    def recent(self):
        snap = self.last
        if snap is None or time.time() - snap["now"] > REFRESH_SECONDS:
            snap = self.snapshot()
        return snap

    def detail(self, tid):
        """HTML for one card's detail drawer, or None when the id is on no card."""
        if not valid_id(tid):
            return None
        snap = self.recent()
        now = time.time()
        agent = next((a for a in snap["agents"] if a["id"] == tid), None)
        if agent is not None:
            status = read_status(os.path.join(self.cfg.home, "state", tid + ".status"))[-STATUS_TAIL:]
            checks = self.checks.get(self.cfg, agent["pr"]) if agent["pr"] else None
            return render_agent_detail(agent, captain_intent(self.cfg.home, tid), status, checks,
                                       report_files(self.cfg.home, tid), now)
        item = next((q for q in snap["queue"] if q["id"] == tid), None)
        if item is not None:
            rc, out, _err = run([self.cfg.bin("fm-tasks-axi.sh"), "show", tid, "--full"], env=self.cfg.env())
            full = parse_show(out) if rc == 0 else {}
            return render_queue_detail(item, full, captain_intent(self.cfg.home, tid),
                                       report_files(self.cfg.home, tid), now)
        return None

    def add_task(self, title, project, text, request_id):
        with self.lock:
            if request_id in self.requests:
                return self.requests[request_id]
            if project not in read_projects(self.cfg.home):
                return {"ok": False, "error": "project is not in data/projects.md"}
            cmd = [self.cfg.bin("fm-tasks-axi.sh"), "add", title, "--mint", "--repo", project, "--queue", "--json"]
            if text:
                cmd += ["--body", text]
            rc, out, err = run(cmd, env=self.cfg.env(), timeout=60)
            try:
                res = json.loads(out)
            except ValueError:
                res = {}
            if not isinstance(res, dict):
                res = {}
            task = res.get("task") or {}
            if rc != 0 or not res.get("ok") or not task.get("id"):
                return {"ok": False, "error": "the backlog refused it: " + ((err or out).strip()[:300] or "exit %d" % rc)}
            if res.get("already"):
                return {"ok": False, "error": "a task with id %s already exists; change the title" % task["id"]}
            tid = task["id"]
            note = ("New task queued from shipmate: %s (project %s): %s. Details: bin/fm-tasks-axi.sh show %s --full"
                    % (tid, project, title, tid))
            nrc, nout, nerr = run([self.cfg.bin("fm-inbox.sh"), "note", "--request-id", "shipmate-" + request_id,
                                   "--json", "--", note], env=self.cfg.env(), timeout=60)
            try:
                announced = bool(json.loads(nout).get("announced"))
            except (ValueError, AttributeError):
                announced = False
            result = {"ok": True, "id": tid, "notified": nrc == 0 and announced,
                      "notify_error": None if nrc == 0 and announced else ((nerr or nout).strip()[:300] or "exit %d" % nrc)}
            self.requests[request_id] = result
            return result


# ---------------------------------------------------------------- formatting

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
esc = html.escape


def fmt_tokens(n):
    if n is None:
        return "unknown"
    if n >= 1e9:
        return "%.2fB" % (n / 1e9)
    if n >= 1e7:
        return "%.1fM" % (n / 1e6)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%dk" % round(n / 1e3)
    return str(int(n))


def fmt_when(ts, now=None):
    if ts is None:
        return "unknown"
    t = dt.datetime.fromtimestamp(ts)
    if t.date() == dt.date.fromtimestamp(now or time.time()):
        return "today " + t.strftime("%H:%M")
    return "%s %s" % (WEEKDAYS[t.weekday()], t.strftime("%d %b %H:%M"))


def fmt_date_iso(s):
    try:
        d = dt.date.fromisoformat(s)
        return "%s %s" % (WEEKDAYS[d.weekday()], d.strftime("%d %b"))
    except (TypeError, ValueError):
        return s or "?"


def fmt_dur(sec):
    if sec is None:
        return "unknown"
    sec = max(0, int(sec))
    if sec < 60:
        return "under 1 min"
    if sec < 3600:
        return "%d min" % (sec // 60)
    if sec < 86400:
        return "%d h %02d min" % (sec // 3600, (sec % 3600) // 60)
    return "%d d %d h" % (sec // 86400, (sec % 86400) // 3600)


def badge(text, tone="neutral", title=None):
    t = ' title="%s"' % esc(title) if title and title != text else ""
    return '<span class="fm-badge fm-badge--%s"%s>%s</span>' % (tone, t, esc(text))


def link(url, text=None):
    if not url or not url.startswith(("https://", "/")):
        return esc(text or url or "")
    return '<a class="sm-link" href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (esc(url), esc(text or url))


def pr_text(url):
    m = re.search(r"/(?:pull|merge_requests)/(\d+)", url or "")
    return "PR #%s" % m.group(1) if m else "PR"


def quota_tone(rem):
    return "danger" if rem is not None and rem < 15 else "warn" if rem is not None and rem < 35 else "ok"


def runway_text(ln, now):
    if ln["runway"] == "projected_exhaustion" and ln["exhausts"]:
        early = not ln["resets"] or ln["exhausts"] < ln["resets"]
        return "at this pace runs out " + fmt_when(ln["exhausts"], now), ("danger" if early else "muted")
    if ln["runway"] == "through_reset":
        return "at this pace lasts until reset", "muted"
    return "projection unknown", "muted"


def lanes_by_urgency(q):
    """Accounts with the least left first; unmeasured ones last."""
    return sorted(q.get("lanes") or [], key=lambda ln: (ln["remaining"] is None, ln["remaining"] or 0))


# ---------------------------------------------------------------- blocks


def block_accounts(q, now):
    if q.get("error"):
        return '<div class="sm-empty">Quota unavailable: %s</div>' % esc(q["error"])
    if not q["lanes"]:
        return '<div class="sm-empty">No Claude account measured.</div>'
    rows = []
    for ln in lanes_by_urgency(q):
        rem = ln["remaining"]
        tone = quota_tone(rem)
        proj, ptone = runway_text(ln, now)
        wins = " · ".join("%s %s%% (resets %s)" % (w["label"] or "?", w["remaining"], fmt_when(w["resets"], now))
                          for w in ln["windows"])
        rows.append(
            '<div class="sm-acct" title="%s">'
            '<div class="sm-acct__top"><span class="sm-acct__name">%s</span>%s<span class="sm-acct__pct sm-%s">%s</span></div>'
            '<div class="sm-bar"><div class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></div></div>'
            '<div class="sm-acct__sub"><span>%s resets %s</span><span class="sm-%s">%s</span></div></div>'
            % (esc(wins), esc(ln["label"]), badge(ln["plan"]) if ln["plan"] else "",
               tone, ("%d%% left" % rem) if rem is not None else "unknown",
               tone, max(0, min(100, rem or 0)),
               esc(ln["limit_label"] or "limit"), esc(fmt_when(ln["resets"], now)), ptone, esc(proj)))
    return "".join(rows)


def block_today(daily):
    d = daily[0] if daily else {"new": 0, "cache": 0}
    return ('<div class="sm-tile"><b>%s</b><span>new tokens today</span></div>'
            '<div class="sm-tile"><b>%s</b><span>cache reads today</span></div>' % (esc(fmt_tokens(d["new"])), esc(fmt_tokens(d["cache"]))))


def gauge(label, pct, value, sub, tone, title=""):
    return ('<div class="sm-gauge" title="%s"><div class="sm-gauge__top"><b>%s</b><span class="sm-%s">%s</span></div>'
            '<span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></span></span>'
            '<div class="sm-gauge__sub">%s</div></div>'
            % (esc(title), esc(label), tone, esc(value), tone, max(0, min(100, round(pct or 0))), esc(sub)))


def block_strip(s):
    """Narrow screens: the sidebar's numbers as one row of small gauges above the board."""
    now, fm, out = s["now"], s.get("firstmate"), []
    if fm:
        pct = fm["percent"] or 0
        out.append(gauge("Firstmate context", pct, "%d%% used" % round(pct),
                         "%s of %s · %d compactions" % (fmt_tokens(fm["context"]), fmt_tokens(fm["limit"]), fm["compactions"]),
                         "danger" if pct >= fm["warn_at"] else "ok"))
    for ln in lanes_by_urgency(s["quota"]):
        rem = ln["remaining"]
        out.append(gauge(ln["label"], rem, ("%d%% left" % rem) if rem is not None else "unknown",
                         runway_text(ln, now)[0], quota_tone(rem), "resets " + fmt_when(ln["resets"], now)))
    d = s["daily"][0] if s["daily"] else {"new": 0, "cache": 0}
    out.append('<div class="sm-gauge sm-gauge--today"><div class="sm-gauge__top"><b>Today</b><span>%s new</span></div>'
               '<div class="sm-gauge__sub">%s cache reads</div></div>' % (esc(fmt_tokens(d["new"])), esc(fmt_tokens(d["cache"]))))
    return "".join(out)


def block_sumline(s):
    """Phones: one line that opens the full session and accounts blocks."""
    parts = []
    fm = s.get("firstmate")
    if fm and fm["percent"] is not None:
        over = fm["percent"] >= fm["warn_at"]
        parts.append('<span class="sm-%s">context %d%%</span>' % ("danger" if over else "ok", round(fm["percent"])))
    lanes = lanes_by_urgency(s["quota"])
    if lanes and lanes[0]["remaining"] is not None:
        parts.append('<span class="sm-%s">%s %d%% left</span>' % (quota_tone(lanes[0]["remaining"]), esc(lanes[0]["label"]),
                                                                  lanes[0]["remaining"]))
    d = s["daily"][0] if s["daily"] else {"new": 0}
    parts.append("<span>%s new today</span>" % esc(fmt_tokens(d["new"])))
    return " · ".join(parts) + '<span class="sm-sumline__open" aria-hidden="true">▾</span>'


LANE_NAMES = ["exploring", "implementing / writing", "PR open", "waiting for CI", "ready / delivered"]
LANE_MIN = 165       # six full lanes still fit beside the sidebar at 1400 px
CREW_TONE = {"working": "online", "paused": "info", "blocked": "danger", "failed": "danger", "done": "online"}
REASON_TONE = {"slot": "warn", "date": "info", "task": "info", "decision": "danger", "ready": "online"}


def lane_of(a):
    """Board column of an agent; a scout's three phases sit in exploring, writing and delivered."""
    if a["phase"] is None:
        return None
    return a["phase"] if a["kind"] != "scout" else {0: 0, 1: 1, 2: 4}[a["phase"]]


def agent_card(a, now):
    tok = a["tokens"]
    meta = " · ".join(x for x in (a["project"], a["model"], a["account"]) if x)
    since = ("for " + fmt_dur(now - a["phase_since"])) if a["phase_since"] else "phase start unknown"
    toks = "%s new · %s cache" % (fmt_tokens(tok["new"]), fmt_tokens(tok["cache"])) if tok else "tokens unknown"
    title = a["title"] or a["id"]
    return ('<article class="sm-card" data-id="%s" tabindex="0" role="button" aria-label="%s: details">'
            '<div class="sm-card__top"><span class="sm-id">%s</span>%s</div>'
            '<div class="sm-card__title" title="%s">%s</div>'
            '<div class="sm-card__line" title="%s">%s</div>'
            '<div class="sm-card__line">%s%s</div>'
            '<div class="sm-card__line sm-mono">%s</div></article>'
            % (esc(a["id"]), esc(a["id"]), esc(a["id"]), badge(a["crew"], CREW_TONE.get(a["crew"], "neutral")),
               esc(title), esc(title), esc(meta), esc(meta), esc(since),
               (" · " + link(a["pr"], pr_text(a["pr"]))) if a["pr"] else "", esc(toks)))


def queue_card(q):
    reasons = " ".join(badge(short, REASON_TONE.get(k, "neutral"), full) for k, short, full in q["reasons"])
    return ('<article class="sm-card sm-card--queue" data-id="%s" tabindex="0" role="button" aria-label="%s: details">'
            '<div class="sm-card__top"><span class="sm-id">%s</span><span class="sm-faint sm-card__repo">%s</span></div>'
            '<div class="sm-card__title" title="%s">%s</div><div class="sm-reasons">%s</div></article>'
            % (esc(q["id"]), esc(q["id"]), esc(q["id"]), esc(q["repo"]), esc(q["title"]), esc(q["title"]), reasons))


def block_board(agents, queue, err, now):
    """Queue lane, then one lane per phase. A phase with no card shrinks to a labelled strip so
    the lanes with work get the width, and keeps its place so the order never changes."""
    lanes = [[] for _ in LANE_NAMES]
    unknown = []
    for a in agents:
        i = lane_of(a)
        (unknown if i is None else lanes[i]).append(agent_card(a, now))
    spec = [("Queue", [queue_card(q) for q in queue], True)] + [(n, c, False) for n, c in zip(LANE_NAMES, lanes)]
    if unknown:
        spec.append(("phase unknown", unknown, False))
    cols, html_lanes = [], []
    for name, cards, is_queue in spec:
        if cards or is_queue:
            cols.append("minmax(%dpx, 1fr)" % LANE_MIN)
            if not cards:
                cards = ['<div class="sm-empty">%s</div>' % (("Queue unavailable: " + esc(err)) if err else "Nothing waiting.")]
            html_lanes.append(
                '<section class="sm-lane%s" aria-label="%s"><div class="sm-lane__head"><span>%s</span><span class="sm-count">%d</span></div>'
                '%s<div class="sm-lane__body">%s</div><div class="sm-lane__more" hidden></div></section>'
                % (" sm-lane--queue" if is_queue else "", esc(name), esc(name),
                   len(queue) if is_queue else len(cards),
                   '<button class="sm-addbtn" type="button" data-action="add">+ Add task</button>' if is_queue else "",
                   "".join(cards)))
        else:
            cols.append("44px")
            html_lanes.append('<section class="sm-lane sm-lane--empty" aria-label="%s: empty" title="%s: nothing here">'
                              '<span class="sm-count">0</span><span class="sm-vert">%s</span></section>'
                              % (esc(name), esc(name), esc(name)))
    return '<div class="sm-board" style="grid-template-columns:%s">%s</div>' % (" ".join(cols), "".join(html_lanes))


# ---------------------------------------------------------------- detail drawer


def fact(label, value_html):
    return '<div class="sm-fact"><span>%s</span><b>%s</b></div>' % (esc(label), value_html)


def timeline(phases, times, current, now):
    items = []
    for i, name in enumerate(phases):
        t = times[i] if i < len(times) else None
        state = "done" if current is not None and i < current else "now" if current == i else "todo"
        if t:
            txt = fmt_when(t, now)
        elif current is not None and i > current:
            txt = "not reached"
        else:
            txt = "time unknown"
        items.append('<li class="sm-tl__%s"><span class="sm-tl__dot"></span><b>%s</b><span>%s</span></li>' % (state, esc(name), esc(txt)))
    return '<ol class="sm-tl">%s</ol>' % "".join(items)


def report_links(tid, reports):
    if not reports:
        return '<span class="sm-faint">no report</span>'
    return " · ".join(link("/report?id=%s&f=%s" % (urllib.parse.quote(tid), ext), "report." + ext) for ext in reports)


def checks_html(pr, checks):
    if not pr:
        return '<span class="sm-faint">no PR</span>'
    out = link(pr, pr_text(pr))
    if not checks:
        return out
    if checks.get("error"):
        return out + ' <span class="sm-faint">(checks unknown: %s)</span>' % esc(checks["error"])
    c = checks["counts"]
    parts = ["%d passed" % c["pass"]]
    if c["fail"]:
        parts.append('<span class="sm-danger">%d failed</span>' % c["fail"])
    if c["pending"]:
        parts.append('<span class="sm-warn">%d running</span>' % c["pending"])
    if c["skipped"]:
        parts.append("%d skipped" % c["skipped"])
    state = (checks.get("state") or "?").lower() + (" draft" if checks.get("draft") else "")
    out += " · %s · %s" % (esc(state), ", ".join(parts))
    if checks["failing"]:
        out += '<div class="sm-faint">failing: %s</div>' % esc(", ".join(checks["failing"]))
    return out + '<div class="sm-faint">read from the forge %s ago, at most every %d s</div>' % (
        esc(fmt_dur(checks.get("age", 0)) if checks.get("age", 0) >= 60 else "%d s" % checks.get("age", 0)), CHECKS_TTL)


def status_lines(status):
    if not status:
        return '<div class="sm-faint">no status lines</div>'
    rows = []
    for s in reversed(status):
        rows.append('<li><span class="sm-st sm-st--%s">%s</span><span class="sm-faint">%s</span><span>%s</span></li>'
                    % (esc(s["state"]), esc(s["state"]), esc(fmt_when(s["at"], time.time()) if s["at"] else "time unknown"),
                       esc(s["text"][:600])))
    return '<ol class="sm-status">%s</ol>' % "".join(rows)


def pre(text, empty):
    return ('<div class="sm-pre">%s</div>' % esc(text)) if text else '<div class="sm-faint">%s</div>' % esc(empty)


def render_agent_detail(a, intent, status, checks, reports, now):
    tok = a["tokens"]
    model = a["model"] or "?"
    if a["model_seen"]:
        model += " (%s)" % a["model_seen"].replace("claude-", "")
    if a["context"] is not None and a["context_limit"]:
        pct = 100.0 * a["context"] / a["context_limit"]
        ctx = ('%s of %s (%s window), %d%%<span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></span></span>'
               % (esc(fmt_tokens(a["context"])), esc(fmt_tokens(a["context_limit"])), esc(a["context_how"]), round(pct),
                  "danger" if pct >= 80 else "warn" if pct >= 60 else "ok", min(100, round(pct))))
    else:
        ctx = "unknown"
    idle = ("last action %s ago" % fmt_dur(now - a["last_activity"])) if a["last_activity"] else "unknown"
    phase = a["phases"][a["phase"]] if a["phase"] is not None else "unknown"
    return (
        '<div class="sm-dhead"><span class="sm-id">%s</span>%s</div><h2 class="sm-dtitle">%s</h2>'
        '<div class="sm-facts">%s</div>'
        '<h3>Phases</h3>%s<div class="sm-faint">current: %s%s · evidence: %s</div>'
        '<h3>Pull request</h3><div>%s</div>'
        '<h3>Report</h3><div>%s</div>'
        '<h3>Captain\'s intent</h3>%s'
        '<h3>Latest status lines</h3>%s'
        % (esc(a["id"]), badge(a["crew"], CREW_TONE.get(a["crew"], "neutral")), esc(a["title"] or a["id"]),
           "".join([fact("Project", esc(a["project"] or "?")), fact("Kind", esc(a["kind"])), fact("Model", esc(model)),
                    fact("Effort", esc(a["effort"] or "?")), fact("Account", esc(a["account"])),
                    fact("Harness", esc(a["harness"])), fact("Started", esc(fmt_when(a["spawn"], now))),
                    fact("Idle", esc(idle)),
                    fact("Tokens", esc("%s new · %s cache reads" % (fmt_tokens(tok["new"]), fmt_tokens(tok["cache"]))) if tok else "unknown"),
                    fact("Context", ctx)]),
           timeline(a["phases"], a.get("milestones") or [], a["phase"], now), esc(phase),
           (" for " + esc(fmt_dur(now - a["phase_since"]))) if a["phase_since"] else "", esc(a["phase_why"]),
           checks_html(a["pr"], checks), report_links(a["id"], reports),
           pre(intent, "no Captain's intent section in data/%s/brief.md" % a["id"]), status_lines(status)))


def render_queue_detail(q, full, intent, reports, now):
    reasons = "".join('<li>%s%s</li>' % (badge(short, REASON_TONE.get(k, "neutral")),
                                         (" <span>%s</span>" % esc(long_)) if long_ != short else "")
                      for k, short, long_ in q["reasons"])
    return (
        '<div class="sm-dhead"><span class="sm-id">%s</span>%s</div><h2 class="sm-dtitle">%s</h2>'
        '<div class="sm-facts">%s</div><h3>Why it waits</h3><ul class="sm-reasonlist">%s</ul>'
        '<h3>Notes</h3>%s%s<h3>Report</h3><div>%s</div>'
        % (esc(q["id"]), badge("queued", "info"), esc(full.get("title") or q["title"]),
           "".join([fact("Project", esc(q["repo"] or "?")), fact("Kind", esc(q["kind"] or "not set")),
                    fact("Since", esc(fmt_date_iso(q["created"]) if q["created"] else "?")),
                    fact("Priority", esc(full.get("priority") or "-")), fact("Depends on", esc(full.get("deps") or "none"))]),
           reasons, pre(full.get("body") if full.get("body") not in (None, "-") else None, "no notes on this item"),
           ("<h3>Captain's intent</h3>" + pre(intent, "")) if intent else "", report_links(q["id"], reports)))


def block_firstmate(fm, now):
    if fm is None:
        return ('<div class="sm-empty">Firstmate session unknown: no Claude transcript in the project folder '
                'of this home.</div>')
    pct = fm["percent"]
    over = pct is not None and pct >= fm["warn_at"]
    tone = "danger" if over else "ok"
    g = fm["growth"]
    if g is None:
        growth = "growth unknown (no call before the last %d h)" % GROWTH_HOURS
    elif fm["growth_since_compaction"]:
        growth = "%s%s since the compaction %s" % ("+" if g >= 0 else "-", fmt_tokens(abs(g)), fmt_when(fm["growth_since"], now))
    else:
        growth = "%s%s in the last %d h" % ("+" if g >= 0 else "-", fmt_tokens(abs(g)), GROWTH_HOURS)
    lc = fm["last_compaction"]
    comp = "%d compaction%s" % (fm["compactions"], "" if fm["compactions"] == 1 else "s")
    if lc:
        comp += ", last %s (%s, %s to %s)" % (fmt_when(lc["at"], now), lc["trigger"] or "trigger unknown",
                                              fmt_tokens(lc["before"]), fmt_tokens(lc["after"]))
    warn = ('<div class="sm-warnline">Context is past %d%% of the window: a good moment to clear or compact '
            'the firstmate session.</div>' % fm["warn_at"]) if over else ""
    return (
        '<div class="sm-acct">'
        '<div class="sm-acct__top"><span class="sm-acct__name">Context</span>%s<span class="sm-acct__pct sm-%s">%s</span></div>'
        '<div class="sm-bar"><div class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></div>'
        '<div class="sm-bar__mark" style="left:%d%%" title="warning threshold"></div></div>'
        '<div class="sm-acct__sub"><span>%s of %s (%s window) · last call %s</span><span>%s</span></div>'
        '<div class="sm-acct__sub"><span>today: %s new · %s cache reads</span><span>%s</span></div>'
        '%s<div class="sm-acct__win">session %s, found by %s</div></div>'
        % (badge("over %d%%" % fm["warn_at"], "danger") if over else "", tone,
           ("%d%% used" % round(pct)) if pct is not None else "unknown",
           tone, max(0, min(100, round(pct or 0))), max(0, min(100, fm["warn_at"])),
           esc(fmt_tokens(fm["context"])), esc(fmt_tokens(fm["limit"])), esc(fm["limit_how"]),
           esc(fmt_when(fm["last_call"], now)), esc(growth),
           esc(fmt_tokens(fm["today_new"])), esc(fmt_tokens(fm["today_cache"])), esc(comp),
           warn, esc(fm["session"][:8]), esc(fm["found_by"])))


def phase_bar(a):
    segs = []
    for i, name in enumerate(a["phases"]):
        cls = "done" if a["phase"] is not None and i < a["phase"] else "now" if a["phase"] == i else ""
        segs.append('<span class="sm-step %s" title="%s"></span>' % (cls, esc(name)))
    return '<span class="sm-steps" role="img" aria-label="phases completed">%s</span>' % "".join(segs)


def block_usage(daily, deliv):
    top_new = max([d["new"] for d in daily] + [1])
    top_cache = max([d["cache"] for d in daily] + [1])
    out = ['<div class="sm-sub">Per day, every session of the accounts read</div><div class="sm-days">']
    for d in daily:
        by = " · ".join("%s %s" % (c, fmt_tokens(v[0])) for c, v in d["by"].items() if v[0])
        out.append('<div class="sm-day"><span class="sm-day__d">%s</span>'
                   '<span class="sm-day__bars"><span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--new" style="width:%.1f%%"></span></span>'
                   '<span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--cache" style="width:%.1f%%"></span></span></span>'
                   '<span class="sm-day__n"><b>%s</b> new<br><span class="sm-faint">%s cache</span></span>'
                   '<span class="sm-day__by sm-faint">%s</span></div>'
                   % (esc(fmt_date_iso(d["day"])), 100.0 * d["new"] / top_new, 100.0 * d["cache"] / top_cache,
                      esc(fmt_tokens(d["new"])), esc(fmt_tokens(d["cache"])), esc(by or "no use")))
    out.append('</div><div class="sm-legend"><span class="sm-sw sm-sw--new"></span>new: input + cache writes + output'
               '<span class="sm-sw sm-sw--cache"></span>cache reads</div>')
    out.append('<div class="sm-sub">Per delivered task, session sheets of the last %d days</div>' % DAYS)
    if deliv is None:
        out.append('<div class="sm-empty">No session sheets (data/fichas/fichas.jsonl is absent).</div>')
        return "".join(out)
    if not deliv:
        out.append('<div class="sm-empty">No session sheet in the last %d days.</div>' % DAYS)
        return "".join(out)
    out.append('<div class="sm-table"><table><thead><tr><th>task</th><th>kind</th><th>model</th><th>wall time</th>'
               '<th class="r">new</th><th class="r">cache</th><th></th></tr></thead><tbody>')
    for r in deliv:
        sp = r["split"]
        if sp:
            extra = (' <span class="sm-faint">(+%d subagent%s)</span>' % (sp["subagents"], "s" if sp["subagents"] > 1 else "")
                     if sp.get("subagents") else "")
        else:
            extra = ' <span class="sm-faint">(sheet total %s; transcript not found)</span>' % esc(fmt_tokens(r["sheet_total"]))
        prl = link(r["pr"], pr_text(r["pr"])) if r["pr"] else (esc("PR #%s" % r["pr_number"]) if r["pr_number"] else "")
        out.append('<tr><td><span class="sm-id">%s</span>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                   '<td class="r">%s%s</td><td class="r">%s</td><td>%s</td></tr>'
                   % (esc(r["id"] or "?"), " (partial)" if r["partial"] else "", esc(r["kind"] or "?"), esc(r["model"] or "?"),
                      esc(fmt_dur(r["minutes"] * 60) if isinstance(r["minutes"], (int, float)) else "?"),
                      esc(fmt_tokens(sp["new"]) if sp else "unknown"), extra,
                      esc(fmt_tokens(sp["cache"]) if sp else "unknown"), prl))
    out.append("</tbody></table></div>")
    return "".join(out)


def render_blocks(s):
    return {
        "board": block_board(s["agents"], s["queue"], s["backlog_error"], s["now"]),
        "today": block_today(s["daily"]),
        "firstmate": block_firstmate(s.get("firstmate"), s["now"]),
        "accounts": block_accounts(s["quota"], s["now"]),
        "strip": block_strip(s),
        "sumline": block_sumline(s),
        "usage": block_usage(s["daily"], s["delivered"]),
        "counts": "%d agent%s · %d in the queue" % (len(s["agents"]), "" if len(s["agents"]) == 1 else "s", len(s["queue"])),
        "time": dt.datetime.fromtimestamp(s["now"]).strftime("%H:%M:%S"),
    }


def asset(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def render_page(panel, snap, notice=""):
    b = render_blocks(snap)
    nonce = secrets.token_urlsafe(16)
    opts = "".join('<option value="%s">%s</option>' % (esc(p), esc(p)) for p in snap["projects"])
    form = ('<form class="sm-form" method="post" action="/task">'
            '<input type="hidden" name="token" value="%s"><input type="hidden" name="request" value="%s">'
            '<label>Title<input name="title" required maxlength="200" autocomplete="off"></label>'
            '<label>Project<select name="project" required>%s</select></label>'
            '<label>Notes<textarea name="text" rows="4" maxlength="5000"></textarea></label>'
            '<div class="sm-form__foot"><span class="sm-faint">Goes to the queue and notifies firstmate.</span>'
            '<button class="fm-btn fm-btn--sm fm-btn--primary" type="submit"%s>Queue it</button></div></form>'
            % (esc(panel.token), esc(secrets.token_urlsafe(12)), opts or '<option value="">no projects</option>',
               "" if opts else " disabled"))
    bay = block_bay(snap)
    page = asset("shipmate.html")
    keys = ("board", "today", "firstmate", "accounts", "strip", "sumline", "usage", "counts")
    for k, v in [("{{CSS}}", asset("shipmate.css")), ("{{NONCE}}", nonce), ("{{TIME}}", esc(b["time"])),
                 ("{{FORM}}", form), ("{{NOTICE}}", notice), ("{{REFRESH_MS}}", str(REFRESH_SECONDS * 1000)),
                 ("{{BAY}}", bay["bay"]), ("{{BAYCOUNTS}}", esc(bay["counts"]))] + \
            [("{{%s}}" % k.upper(), b[k]) for k in keys]:
        page = page.replace(k, v)
    return page, nonce


def notice_html(q):
    if "added" in q:
        tid = q["added"][0]
        if q.get("notified", [""])[0] == "yes":
            return '<div class="sm-notice sm-notice--ok">Task %s is queued and firstmate was notified.</div>' % esc(tid)
        return ('<div class="sm-notice sm-notice--warn">Task %s is queued, but notifying firstmate failed; '
                'tell firstmate another way.</div>' % esc(tid))
    if "error" in q:
        return '<div class="sm-notice sm-notice--err">Not queued: %s</div>' % esc(q["error"][0][:300])
    return ""


# ---------------------------------------------------------------- the bay (ship view)
#
# The same snapshot as the board, drawn as a harbour: each project is a ship, each live agent a
# hand whose place on board is its phase, firstmate at the helm of the flagship with the quota as
# barrels in its hold and its context as ballast, the queue as boats moored at the pier. The scene
# is a pure function of the snapshot (bay_scene), drawn as pixel art in SVG (render_bay).

PLACES = ["nest", "deck", "bow", "rail", "plank"]          # a ship agent's place, by ship phase index
SCOUT_PLACES = ["out", "back", "dock"]                     # a scout's boat, by scout phase index
PLACE_TEXT = {
    "nest": "in the crow's nest with a spyglass",
    "deck": "on deck, hauling rope or hammering",
    "bow": "at the bow, signalling with flags",
    "rail": "fishing at the rail",
    "plank": "on the gangplank, waiting to go ashore",
    "hatch": "on deck, phase unknown",
    "out": "rowing out to sea",
    "back": "rowing back with the map",
    "dock": "moored at the pier with the map",
}
CAPTAIN_STATES = {"parked", "blocked", "failed"}   # fm-crew-state.sh reports a needs-decision crew as parked
SEA_LOAD = (0.6, 1.0)                              # load per CPU where the sea turns choppy, then rough
MAX_QUEUE_BOATS = 6
MAX_CRATES = 12


def captain_reasons(a):
    """Why a hand raises the red flag: its crew state waits on the captain, or a decision is open."""
    why = []
    if a.get("crew") in CAPTAIN_STATES:
        why.append("crew is " + a["crew"])
    for d in a.get("decisions") or []:
        why.append("%s%s: %s" % (d["state"], (" " + d["key"]) if d.get("key") else "", d["text"][:120]))
    return why


def runs_out_early(ln):
    return bool(ln.get("runway") == "projected_exhaustion" and ln.get("exhausts")
                and (not ln.get("resets") or ln["exhausts"] < ln["resets"]))


def sea_state(machine):
    if not machine or machine.get("load") is None:
        return "calm"
    per = machine["load"] / max(1, machine.get("cpus") or 1)
    return "calm" if per < SEA_LOAD[0] else "choppy" if per < SEA_LOAD[1] else "rough"


def bay_scene(s):
    """What the bay shows, derived from the snapshot alone; render_bay draws it."""
    ships, scouts = {}, []
    for a in s["agents"]:
        why = captain_reasons(a)
        phases = a.get("phases") or []
        hand = {"id": a["id"], "title": a.get("title") or "", "crew": a.get("crew") or "unknown",
                "flag": bool(why), "why": why, "project": a.get("project") or "",
                "phase": phases[a["phase"]] if a.get("phase") is not None and a["phase"] < len(phases) else "unknown"}
        if a.get("kind") == "scout":
            ph = a.get("phase")
            hand["place"] = SCOUT_PLACES[ph] if ph is not None and ph < len(SCOUT_PLACES) else "out"
            scouts.append(hand)
        else:
            ph = a.get("phase")
            hand["place"] = PLACES[ph] if ph is not None and ph < len(PLACES) else "hatch"
            ships.setdefault(a.get("project") or "no project", []).append(hand)
    order = {p: i for i, p in enumerate(PLACES + ["hatch"])}
    ship_list = [{"project": p, "hands": sorted(h, key=lambda x: (order[x["place"]], x["id"]))}
                 for p, h in sorted(ships.items())]
    fm = s.get("firstmate") or {}
    pct, warn = fm.get("percent"), fm.get("warn_at", DEFAULT_CONTEXT_WARN)
    barrels = []
    for ln in lanes_by_urgency(s.get("quota") or {}):
        rem, early = ln.get("remaining"), runs_out_early(ln)
        barrels.append({"label": ln.get("label") or "account", "remaining": rem, "early": early,
                        "empty": early or (rem is not None and rem < 15), "resets": ln.get("resets"),
                        "exhausts": ln.get("exhausts")})
    queue = [{"id": q["id"], "title": q.get("title") or "", "repo": q.get("repo") or "",
              "flag": any(r[0] == "decision" for r in q.get("reasons") or []),
              "why": [r[1] for r in q.get("reasons") or []]} for q in s.get("queue") or []]
    crates = [{"id": r.get("id") or "?", "kind": r.get("kind") or "?"} for r in (s.get("delivered") or [])]
    hands = sum(len(x["hands"]) for x in ship_list) + len(scouts)
    flags = (sum(h["flag"] for x in ship_list for h in x["hands"]) + sum(h["flag"] for h in scouts)
             + sum(q["flag"] for q in queue))
    return {
        "ships": ship_list, "scouts": scouts, "queue": queue, "crates": crates, "barrels": barrels,
        "flagship": {"context": pct, "warn_at": warn, "over": pct is not None and pct >= warn,
                     "tokens": fm.get("context"), "limit": fm.get("limit"), "known": bool(fm)},
        "sea": sea_state(s.get("machine")), "load": s.get("machine"),
        "hands": hands, "flags": flags,
    }


def bay_counts(sc):
    n = len(sc["ships"])
    parts = ["%d ship%s" % (n, "" if n == 1 else "s"), "%d hand%s" % (sc["hands"], "" if sc["hands"] == 1 else "s")]
    if sc["flags"]:
        parts.append("%d red flag%s" % (sc["flags"], "" if sc["flags"] == 1 else "s"))
    parts.append("%d boat%s moored" % (len(sc["queue"]), "" if len(sc["queue"]) == 1 else "s"))
    return " · ".join(parts)


# -- pixel art: a palette, a few sprites, and a canvas that writes one SVG path per colour

INK = {
    "e": "#22180f", "p": "#34405e", "b": "#2a1d14", "w": "#f6f1e3", "g": "#d4a83c", "G": "#8a6a2a",
    "o": "#7a4a24", "O": "#5a3418", "t": "#c9a46a", "r": "#d8342a", "R": "#a32218", "y": "#f2c53d",
    "u": "#2f6fd0", "m": "#efdcae", "M": "#b89a5e", "n": "#22345e", "N": "#162447", "l": "#e9edf2",
    "k": "#1a1a22", "x": "#c23b22",
}
SHIRTS = ["#2f6fb0", "#3c8f5a", "#c98a2b", "#7a4fa0", "#2b8c8c", "#b0563a", "#56657a", "#d9d6c8"]
SKINS = ["#f1c9a0", "#d9a173", "#b07a4f", "#8a5a3a", "#f5d6b8"]
HAIRS = ["#2a1d14", "#5a3a1e", "#a0662a", "#d9b45a", "#8d8d8d", "#1d1d2a", "#7a2e1a"]
PROJECT_HUES = ["#c0452a", "#2f6688", "#2f6b4f", "#b5791c", "#6a4a9c", "#2b8c8c", "#8f2f17"]

# sprites face right; the last row is where the feet are. h hair, s skin, E eye, c shirt, C shirt
# stripe, p trousers, b boots; the rest are INK colours.
HEAD = ["..hhh..", ".hhhhh.", ".hssEs.", ".hssss.", "...ss.."]
LEGS = ["..ppp..", "..p.p..", ".bb.bb."]
POSES = {
    "stand": HEAD + [".ccccc.", "ccCcCcc", "s.ccc.s"] + LEGS,
    "spy": ["..hhh.....", ".hhhhh....", ".hssEgggGG", ".hssssss..", "...ss.c...", ".cccccc...",
            "ccCcC.....", "s.ccc.....", "..ppp.....", "..p.p.....", ".bb.bb...."],
    "pull": [".........t", "........t.", "..hhh..t..", ".hhhhhs...", ".hssEsc...", ".hssssc...",
             "...ss.c...", ".cccccc...", "ccCcC.....", "s.ccc.....", "..ppp.....", "..p.p.....", ".bb..bb..."],
    "hammer": [".......GGG", ".......GGG", "..hhh...o.", ".hhhhh..s.", ".hssEs..c.", ".hssss..c.",
               "...ss..c..", ".cccccc...", "ccCcC.....", "s.ccc.....", "..ppp.....", "..p.p.....", ".bb.bb...."],
    "signal": ["yyy.......uuu", "yyy.......uuu", "..o.......o..", "..s..hhh..s..", "..c.hhhhh.c..",
               "..c.hssEs.c..", "...chssssc...", "....cs.sc....", "....ccccc....", "....cCcCc....",
               "....ccccc....", "....ppppp....", "....pp.pp....", "...bbb.bbb..."],
    "fish": ["..........t", ".........t.", "..hhh...t..", ".hhhhh.t...", ".hssEst....", ".hssss.....",
             "...ss.....", ".ccccc....", "cCcCcs....", ".ppppp....", "...p.p....", "...b.b...."],
    "bag": ["......www", "..hhh.www", ".hhhhhwww", ".hssEsw..", ".hssss...", "...ss....", ".ccccc...",
            "ccCcCc...", "s.ccc....", "..ppp....", "..p.p....", ".bb.bb..."],
    "scroll": HEAD + [".ccccc..", "ccCcsmmm", "s.ccmmmM"] + LEGS,
    "row": ["..hhh....", ".hhhhh...", ".hssEs...", ".hssss...", "...ss....", ".cccccs..", "ccCcCc.o.",
            "..........o"],
    "helm": ["..NNN..", ".NNNNN.", "ggggggg", ".hssEs.", ".hssss.", "...ss..", ".nnnnn.", "nngnnnn",
             "s.nnn.s", "..ppp..", "..p.p..", ".bb.bb."],
}
QMARK = [".www.", "w...w", "...w.", "..w..", ".....", "..w.."]
WHEEL = ["..o.o..", ".ooooo.", "oo.O.oo", ".oOOOo.", "oo.O.oo", ".ooooo.", "..o.o.."]
BARREL = [".OoooO.", "OGGGGGO", "OoooooO", "OoooooO", "OGGGGGO", "OoooooO", "OoooooO", ".OGGGO."]
CRATE = ["OOOOOO", "OoMMoO", "OMoOMO", "OMOoMO", "OoMMoO", "OOOOOO"]
BOAT = ["O..............O", "OoooooooooooooOO", ".OooooooooooooO.", "..OOOOOOOOOOOO.."]
SKIFF = ["O..........O", "OoooooooooOO", ".OOOOOOOOOO."]
MAP = ["MmmmmmmmM", "Mmmxmmmm.", "Mmmmmxmm.", "MmxmmmmmM"]


def pick(seq, key):
    """A stable choice from seq for key (str), so a hand keeps its looks across refreshes."""
    h = 0
    for ch in key:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return seq[h % len(seq)]


class Pix:
    """A small pixel canvas: later paint covers earlier paint, and svg() writes one path per colour."""

    def __init__(self):
        self.px = {}

    def rect(self, x, y, w, h, c):
        for j in range(h):
            for i in range(w):
                self.px[(x + i, y + j)] = c

    def dot(self, x, y, c):
        self.px[(x, y)] = c

    def line(self, x0, y0, x1, y1, c):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.px[(x0, y0)] = c
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def sprite(self, x, y, rows, pal, flip=False, upto=None):
        """Paint rows at (x, y) as the top-left corner; upto keeps only the first rows."""
        for j, row in enumerate(rows[:upto]):
            if flip:
                row = row[::-1]
            for i, ch in enumerate(row):
                c = pal.get(ch) or INK.get(ch)
                if ch != "." and c:
                    self.px[(x + i, y + j)] = c

    def clip_below(self, y):
        self.px = {k: v for k, v in self.px.items() if k[1] < y}

    def svg(self, attrs=""):
        runs = {}
        for (x, y), c in sorted(self.px.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            last = runs.setdefault(c, [])
            if last and last[-1][1] == y and last[-1][0] + last[-1][2] == x:
                last[-1][2] += 1
            else:
                last.append([x, y, 1])
        paths = "".join('<path fill="%s" d="%s"/>' % (c, "".join("M%d %dh%dv1h-%dz" % (x, y, w, w) for x, y, w in r))
                        for c, r in runs.items())
        return "<g%s>%s</g>" % (attrs, paths)


def hand_palette(hid):
    shirt = pick(SHIRTS, hid)
    return {"h": pick(HAIRS, hid + "h"), "s": pick(SKINS, hid + "s"), "E": INK["e"], "c": shirt,
            "C": "#f6f1e3" if shirt != "#d9d6c8" else "#2f6fb0", "p": INK["p"], "b": INK["b"]}


def red_flag(p, x, y):
    """The red flag a hand raises when it needs the captain: a pole at x with the cloth to its left."""
    p.rect(x, y, 1, 13, INK["o"])
    p.rect(x - 6, y, 6, 4, INK["r"])
    p.rect(x - 6, y + 3, 6, 1, INK["R"])


def hand_svg(h, x, feet, pose, flip=False, upto=None, extra=None):
    """One clickable hand: the sprite with its lowest painted row (its feet, or its waist when
    `upto` cuts it, as in a boat or the nest) on row `feet`, its flag, and a name tag."""
    rows = POSES[pose]
    top = feet - len(rows[:upto]) + 1
    p = Pix()
    p.sprite(x, top, rows, hand_palette(h["id"]), flip=flip, upto=upto)
    if extra:
        extra(p, top)
    if h["place"] == "hatch":
        p.sprite(x + 1, top - 7, QMARK, {})
    if h["flag"]:
        red_flag(p, x - 1, top - 9)
    title = "%s: %s (%s%s)" % (h["id"], PLACE_TEXT[h["place"]], h["phase"],
                               "; needs the captain: " + "; ".join(h["why"]) if h["flag"] else "")
    label = h["id"] if len(h["id"]) <= 18 else h["id"][:17] + "…"
    return ('<g class="sm-hand%s" data-id="%s" tabindex="0" role="button" aria-label="%s"><title>%s</title>%s'
            '<text class="sm-hand__tag" x="%d" y="%d">%s</text></g>'
            % (" has-flag" if h["flag"] else "", esc(h["id"]), esc(title + ": details"), esc(title),
               p.svg()[3:-4], x + 3, top - (11 if h["flag"] else 3), esc(label)))


# -- layout constants, in art pixels

BAY_H = 178
HORIZON = 62
WL = 124            # ships' waterline
PIER = 150          # top of the pier's deck
PIER_FRONT = 158


def ship_size(hands, name=""):
    """Hull length: room for every hand on deck, at the rail and at the bow, and for the name."""
    n = {k: sum(1 for h in hands if h["place"] == k) for k in PLACES + ["hatch"]}
    deck = max(20, 9 * (n["deck"] + n["hatch"] + max(0, n["nest"] - 4)))
    length = 18 + deck + 10 * n["rail"] + 11 * n["bow"] + 16
    return max(70, length, 26 + int(3.7 * len(name[:28])))


def draw_hull(front, x0, length, deck_y, hue, castle=16):
    """Hull from the bulwark down to the waterline, a raised stern castle and the bowsprit."""
    for y in range(deck_y - 3, WL):
        depth = y - deck_y
        left = x0 + (max(0, depth - 6) // 3)
        right = x0 + length - max(0, (depth + 3) // 2)
        c = "#d4a83c" if y == deck_y - 3 else "#8a5530" if y < deck_y else "#57301a" if depth % 4 == 3 else "#6b3a1c"
        if deck_y + 1 <= y <= deck_y + 2:
            c = hue
        front.rect(left, y, right - left, 1, c)
    front.rect(x0, deck_y - 11, castle, 8, "#7a4524")
    front.rect(x0, deck_y - 12, castle, 1, "#d4a83c")
    front.rect(x0 + 3, deck_y - 9, 3, 3, "#f0d070")
    front.rect(x0 + 8, deck_y - 9, 3, 3, "#f0d070")
    front.line(x0 + length - 1, deck_y - 4, x0 + length + 13, deck_y - 13, "#5a3418")
    front.line(x0 + length - 1, deck_y - 3, x0 + length + 13, deck_y - 12, "#7a4a24")


def draw_mast(back, front, mx, deck_y, top, hue, sail_rows=(18, 50)):
    back.rect(mx, top, 2, deck_y - top, "#5a3418")
    back.rect(mx + 2, top - 1, 1, 6, "#5a3418")
    back.rect(mx - 9, top, 9, 3, hue)                        # pennant, blowing astern
    back.rect(mx - 6, top + 3, 6, 1, hue)
    s0, s1 = top + sail_rows[0], top + sail_rows[1]
    back.rect(mx - 19, s0 - 1, 40, 1, "#5a3418")
    for y in range(s0, s1):
        bulge = 1 if s0 + 4 < y < s1 - 4 else 0
        back.rect(mx - 17 - bulge, y, 36 + 2 * bulge, 1, "#e2d3ad" if (y - s0) % 7 == 6 else "#f4ead0")
        back.dot(mx + 18 + bulge, y, "#cdbb92")
    back.rect(mx - 17, (s0 + s1) // 2 - 1, 36, 3, hue)
    back.rect(mx - 15, s1, 32, 1, "#5a3418")
    front.rect(mx - 8, top + 8, 18, 5, "#6b3a1c")             # crow's nest, drawn over its lookouts
    front.rect(mx - 8, top + 8, 18, 1, "#d4a83c")
    return top + 12                                            # the nest floor


def ship_svg(ship, x0):
    hands, length = ship["hands"], ship_size(ship["hands"], ship["project"])
    deck_y, hue = WL - 14, pick(PROJECT_HUES, ship["project"])
    back, front, out_hands = Pix(), Pix(), []
    mx = x0 + length // 2 - 1
    top = deck_y - 70
    for side in (-1, 1):
        back.line(mx + 1, top + 2, mx + side * (length // 2 - 4), deck_y - 3, "#3a2a1a")
    back.line(mx + 1, top + 4, x0 + length + 12, deck_y - 12, "#3a2a1a")
    nest_floor = draw_mast(back, front, mx, deck_y, top, hue)
    by_place = {k: [h for h in hands if h["place"] == k] for k in PLACES + ["hatch"]}
    deck_x = x0 + 19
    for i, h in enumerate(by_place["nest"]):
        if i < 2:
            out_hands.append(hand_svg(h, mx - 7 + 8 * i, nest_floor - 2, "spy", upto=8))
        elif i < 4:                                            # more lookouts than the nest holds climb the shrouds
            t = (0.42, 0.7)[i - 2]
            sx, sy = mx - int((length // 2 - 4) * t), int(top + 2 + (deck_y - 3 - top - 2) * t)
            out_hands.append(hand_svg(h, sx - 3, sy + 4, "spy"))
        else:
            out_hands.append(hand_svg(h, deck_x, deck_y - 1, "spy"))
            deck_x += 9
    for i, h in enumerate(by_place["deck"]):
        out_hands.append(hand_svg(h, deck_x, deck_y - 1, "pull" if i % 2 == 0 else "hammer"))
        deck_x += 9
    for h in by_place["hatch"]:
        out_hands.append(hand_svg(h, deck_x, deck_y - 1, "stand"))
        deck_x += 9
    rail_x = max(deck_x + 2, x0 + 38)
    bow_x = x0 + length - 13
    for h in by_place["bow"]:
        out_hands.append(hand_svg(h, bow_x - 3, deck_y - 1, "signal"))
        bow_x -= 11
    draw_hull(front, x0, length, deck_y, hue)
    rails = []
    for i, h in enumerate(by_place["rail"]):
        x = rail_x + 10 * i

        def line_down(p, top_, x=x):
            p.line(x + 10, top_ + 1, x + 10, WL - 1, "#e9edf2")
        rails.append(hand_svg(h, x, deck_y - 2, "fish", extra=line_down))
    planks = []
    if by_place["plank"]:
        px0, py0, px1, py1 = x0 + 6, deck_y - 2, x0 - 26, PIER + 2
        plank = Pix()
        for k in range(3):
            plank.line(px0, py0 + k, px1, py1 + k, "#b07a45" if k < 2 else "#6b3a1c")
        for t in (0.2, 0.4, 0.6, 0.8):
            plank.dot(int(px0 + (px1 - px0) * t), int(py0 + (py1 - py0) * t) - 1, "#5a3418")
        planks.append(plank.svg())
        for i, h in enumerate(by_place["plank"]):
            if i < 3:
                t = (0.3, 0.58, 0.86)[i]
                fx, fy = int(px0 + (px1 - px0) * t), int(py0 + (py1 - py0) * t)
            else:
                fx, fy = px1 - 6 - 9 * (i - 3), PIER + 3
            planks.append(hand_svg(h, fx - 3, fy - 1, "bag"))
    name = ('<text class="sm-bay__name" x="%d" y="%d">%s</text>'
            % (x0 + length - 8, WL - 3, esc(ship["project"][:28])))
    return ('<g class="sm-ship" aria-label="%s"><title>%s: %d hand%s aboard</title>%s%s%s%s%s</g>'
            % (esc(ship["project"]), esc(ship["project"]), len(hands), "" if len(hands) == 1 else "s",
               back.svg(), "".join(out_hands), front.svg(), "".join(rails), name), planks, length)


def flagship_svg(sc, x0, length=112):
    """Firstmate's own ship: firstmate at the helm, one barrel per account in the hold, and the
    context as ballast: the fuller the context, the lower she sits; past the warning threshold the
    load line goes under."""
    f = sc["flagship"]
    pct = max(0.0, min(100.0, f["context"] or 0.0))
    sink = int(round(pct * 6 / 100.0)) + (2 if f["over"] else 0)
    mark = int(round(f["warn_at"] * 6 / 100.0))
    deck_y = WL - 22 + sink
    hue = "#22345e"
    back, front = Pix(), Pix()
    for mx, top in ((x0 + 46, deck_y - 74), (x0 + 82, deck_y - 62)):
        back.line(mx + 1, top + 2, mx - 22, deck_y - 3, "#3a2a1a")
        back.line(mx + 1, top + 2, mx + 22, deck_y - 3, "#3a2a1a")
        draw_mast(back, front, mx, deck_y, top, "#c0452a")
    back.line(x0 + 83, deck_y - 60, x0 + length + 12, deck_y - 12, "#3a2a1a")
    draw_hull(front, x0, length, deck_y, hue, castle=24)
    # the hold, cut away to show the barrels
    hold_w = max(34, 10 * len(sc["barrels"]) + 4)
    hx, hy = x0 + 30, deck_y + 4
    front.rect(hx - 1, hy - 1, hold_w + 2, 12, "#d4a83c")
    front.rect(hx, hy, hold_w, 10, "#2a1a10")
    barrels = []
    for i, b in enumerate(sc["barrels"]):
        bx = hx + 3 + 10 * i
        p = Pix()
        p.sprite(bx, hy + 1, BARREL, {})
        level = 0 if b["remaining"] is None else int(round(max(0, min(100, b["remaining"])) / 100.0 * 6))
        fill = "#d8342a" if b["empty"] else "#e0a52e"
        if b["empty"]:
            level = max(1, level)
            for row in (1, 4, 7):                               # red hoops on a barrel that runs dry
                p.rect(bx + 1, hy + 1 + row, 5, 1, "#d8342a")
        if level:
            p.rect(bx + 1, hy + 8 - level, 5, level, fill)
        tip = "%s: %s%s" % (b["label"], ("%d%% left" % b["remaining"]) if b["remaining"] is not None else "unknown",
                            ", runs out before it resets" if b["early"] else "")
        barrels.append('<g class="sm-barrel%s"><title>%s</title>%s</g>' % (" is-empty" if b["empty"] else "", esc(tip), p.svg()))
    # load line on the hull, `mark` rows above the light waterline: she sinks onto it as the
    # context nears the threshold, and past the threshold it is under water
    if not f["over"]:
        ly = WL - 3 - mark + sink
        lx = x0 + length - 30
        front.rect(lx, ly, 7, 1, "#f6f1e3")
        front.rect(lx + 3, ly - 2, 1, 5, "#f6f1e3")
    # the helm on the stern castle, with firstmate behind it
    helm = Pix()
    helm.sprite(x0 + 6, deck_y - 12 - 12, POSES["helm"], {"h": "#2a1d14", "s": "#d9a173", "E": INK["e"],
                                                         "p": INK["p"], "b": INK["b"]})
    helm.sprite(x0 + 14, deck_y - 12 - 9, WHEEL, {})
    front.clip_below(WL)
    if f["context"] is None:
        state = "context unknown"
    else:
        state = "context %d%% of the window%s" % (round(f["context"]), ", past the %d%% warning" % f["warn_at"] if f["over"] else "")
    return ('<g class="sm-ship sm-ship--flag"><title>firstmate: %s</title>%s%s<g class="sm-helm"><title>firstmate at the helm: %s</title>%s</g>'
            '%s<text class="sm-bay__name" x="%d" y="%d">firstmate</text></g>'
            % (esc(state), back.svg(), front.svg(), esc(state), helm.svg(), "".join(barrels),
               x0 + length - 10, min(WL - 3, deck_y + 12)))


def boat_svg(item, x, wl, cls, sailor=None, flag=False, cargo=True):
    p = Pix()
    p.sprite(x, wl - 3, BOAT, {})
    if cargo and sailor is None:
        p.sprite(x + 5, wl - 8, CRATE, {})
    if flag:
        red_flag(p, x + 12, wl - 15)
    if sailor is not None:                     # the hand aboard is what opens the details
        return '<g class="%s">%s%s</g>' % (cls, sailor, p.svg())
    return '<g class="%s" data-id="%s" tabindex="0" role="button" aria-label="%s"><title>%s</title>%s</g>' % (
        cls, esc(item["id"]), esc(item["id"] + ": details"), esc(item["title_text"]), p.svg())


def sky_and_sea(w, sea, rnd):
    out = []
    for y0, y1, c in ((0, 18, "#79bde6"), (18, 34, "#8ccaee"), (34, 48, "#a2d6f2"), (48, HORIZON, "#bfe4f6")):
        out.append('<rect x="0" y="%d" width="%d" height="%d" fill="%s"/>' % (y0, w, y1 - y0, c))
    sun = Pix()
    cx, cy = w - 42, 18
    for y in range(-7, 8):
        for x in range(-7, 8):
            if x * x + y * y <= 49:
                sun.dot(cx + x, cy + y, "#ffe58a" if x * x + y * y > 30 else "#fff1b8")
    out.append(sun.svg())
    clouds = Pix()
    for _ in range(max(3, w // 140)):
        cx, cy = rnd.randrange(10, w - 40), rnd.randrange(6, 40)
        for (dx, dy, ww) in ((4, 0, 10), (0, 3, 22), (2, 6, 18)):
            clouds.rect(cx + dx, cy + dy, ww, 3, "#fbfdff")
        clouds.rect(cx + 2, cy + 8, 18, 1, "#dfeef7")
    out.append(clouds.svg())
    for y0, y1, c in ((HORIZON, HORIZON + 14, "#4d9fcc"), (HORIZON + 14, WL - 20, "#3f8fbf"),
                      (WL - 20, WL + 8, "#3480b2"), (WL + 8, BAY_H, "#2c70a0")):
        out.append('<rect x="0" y="%d" width="%d" height="%d" fill="%s"/>' % (y0, w, y1 - y0, c))
    out.append('<rect x="0" y="%d" width="%d" height="1" fill="#d7eef8"/>' % (HORIZON, w))
    caps = Pix()
    n, size = {"calm": (w // 40, 2), "choppy": (w // 16, 3), "rough": (w // 7, 4)}[sea]
    for _ in range(n):
        x, y = rnd.randrange(0, w), rnd.randrange(HORIZON + 3, BAY_H - 2)
        far = y < HORIZON + 20
        caps.rect(x, y, max(1, size - (1 if far else 0)), 1, "#d4ecf7")
        if sea != "calm" and not far:
            caps.dot(x + 1, y - 1, "#f6fbfd")
            if sea == "rough":
                caps.rect(x - 1, y + 1, size + 2, 1, "#8fc3e0")
    out.append(caps.svg(' class="sm-waves"'))
    return out


def pier_svg(w):
    p = Pix()
    for y in range(PIER, PIER_FRONT):
        p.rect(0, y, w, 1, "#b07a45" if (y - PIER) % 3 else "#8a5530")
    for x in range(0, w, 13):
        p.rect(x, PIER, 1, PIER_FRONT - PIER, "#8a5530")
    p.rect(0, PIER_FRONT, w, 4, "#6b3a1c")
    for x in range(4, w, 26):
        p.rect(x, PIER_FRONT + 4, 4, BAY_H - PIER_FRONT - 4, "#4f2c14")
        p.rect(x + 1, PIER_FRONT + 4, 1, BAY_H - PIER_FRONT - 4, "#6b3a1c")
    return p


def render_bay(sc):
    """The bay as one SVG, sized in art pixels; the page scales it by a whole number."""
    rnd = random.Random(7)                  # the same clouds and waves on every refresh
    flag_w = max(130, 22 * min(len(sc["queue"]), MAX_QUEUE_BOATS) + 34)
    sizes = [ship_size(s["hands"], s["project"]) for s in sc["ships"]]
    n_out = sum(1 for h in sc["scouts"] if h["place"] == "out")
    n_back = sum(1 for h in sc["scouts"] if h["place"] == "back")
    n_dock = sum(1 for h in sc["scouts"] if h["place"] == "dock")
    n_crates = min(len(sc["crates"]), MAX_CRATES)
    open_w = max(80, 24 * n_out + 16, 26 * n_back + 16, 30 * n_dock + 8 * ((n_crates + 2) // 3) + 30)
    w = 10 + flag_w + sum(l + 44 for l in sizes) + open_w
    parts = sky_and_sea(w, sc["sea"], rnd)
    far, near, lower = [], [], []
    ox = 10 + flag_w + sum(l + 44 for l in sizes)
    # scouts: far out on the horizon, rowing back, or moored at the pier with the map
    for i, h in enumerate([h for h in sc["scouts"] if h["place"] == "out"]):
        x = ox + 8 + 24 * i
        p = Pix()
        p.sprite(x, HORIZON + 8, SKIFF, {})
        far.append(hand_svg(h, x + 2, HORIZON + 8, "spy", upto=7) + p.svg())
    for i, h in enumerate([h for h in sc["scouts"] if h["place"] == "back"]):
        x = ox + 8 + 26 * i
        sailor = hand_svg(h, x + 4, WL + 8, "scroll", upto=8)
        near.append(boat_svg(h, x, WL + 10, "sm-boat", sailor=sailor))
    dock_x = ox + 6
    for h in [h for h in sc["scouts"] if h["place"] == "dock"]:
        sailor = hand_svg(h, dock_x + 4, PIER - 4, "stand", upto=9)
        lower.append(boat_svg(h, dock_x, PIER - 1, "sm-boat", sailor=sailor))
        dock_x += 30
    # the flagship and the project ships
    ships, planks = [], []
    ships.append(flagship_svg(sc, 14))
    x = 10 + flag_w
    for s, l in zip(sc["ships"], sizes):
        svg, plank, _ = ship_svg(s, x + 6)
        ships.append(svg)
        planks.extend(plank)
        x += l + 44
    # queue boats moored along the pier under the flagship, red flag when they wait on a decision
    for i, q in enumerate(sc["queue"][:MAX_QUEUE_BOATS]):
        item = {"id": q["id"], "title_text": "%s: queued (%s)%s" % (q["id"], ", ".join(q["why"]), (" · " + q["title"]) if q["title"] else "")}
        lower.append(boat_svg(item, 14 + 22 * i, PIER - 1, "sm-boat sm-boat--queue", flag=q["flag"]))
    more = ""
    if len(sc["queue"]) > MAX_QUEUE_BOATS:
        more = '<text class="sm-bay__more" x="%d" y="%d">+%d</text>' % (16 + 22 * MAX_QUEUE_BOATS, PIER - 3,
                                                                       len(sc["queue"]) - MAX_QUEUE_BOATS)
    pier = pier_svg(w)
    stuff = Pix()
    for i in range(0, w, 52):
        stuff.rect(i + 30, PIER - 3, 3, 4, "#3a2a1a")         # bollards
    cx = dock_x + 4
    crates = []
    for i, c in enumerate(sc["crates"][:MAX_CRATES]):
        p = Pix()
        p.sprite(cx + 7 * (i // 3), PIER + 1 - 6 * (i % 3 + 1), CRATE, {})
        crates.append('<g class="sm-crate"><title>%s delivered (%s)</title>%s</g>' % (esc(c["id"]), esc(c["kind"]), p.svg()))
    if len(sc["crates"]) > MAX_CRATES:
        crates.append('<text class="sm-bay__more" x="%d" y="%d">+%d</text>' % (cx + 7 * (MAX_CRATES // 3) + 2, PIER - 3,
                                                                             len(sc["crates"]) - MAX_CRATES))
    for h in [h for h in sc["scouts"] if h["place"] == "dock"]:
        stuff.sprite(cx - 26, PIER + 3, MAP, {})
    label = "The bay: %s" % bay_counts(sc)
    return ('<svg class="sm-bay__svg" viewBox="0 0 %d %d" style="--w:%dpx;--h:%dpx" role="group" aria-label="%s" '
            'shape-rendering="crispEdges">%s%s%s%s%s%s%s%s%s</svg>'
            % (w, BAY_H, w, BAY_H, esc(label), "".join(parts), "".join(far), "".join(ships), "".join(near),
               "".join(lower), more, pier.svg(), stuff.svg() + "".join(crates), "".join(planks)))


def block_bay(s):
    sc = bay_scene(s)
    return {"bay": render_bay(sc), "counts": bay_counts(sc)}


# ---------------------------------------------------------------- demo (--demo)


def demo_snapshot(now=None):
    """A made-up home with every phase, flag and gauge in use, for `--demo` and the tests.
    Every name in it is invented; nothing is read from disk."""
    now = time.time() if now is None else now
    h = 3600

    def agent(tid, title, project, phase, crew="working", kind="ship", decisions=(), pr=None, ago=2 * h):
        phases = SCOUT_PHASES if kind == "scout" else SHIP_PHASES
        spawn = now - ago
        times = [spawn + i * ago / 5.0 if phase is not None and i <= phase else None for i in range(len(phases))]
        return {"id": tid, "title": title, "kind": kind, "project": project, "model": "model-a", "model_seen": None,
                "effort": "high", "harness": "claude", "account": "account-%s" % "ab"[len(tid) % 2],
                "phases": phases, "phase": phase, "phase_since": times[phase] if phase is not None else None,
                "phase_why": "demo", "milestones": times, "crew": crew, "spawn": spawn,
                "pr": pr or ("https://forge.example/demo/%s/pull/%d" % (project, len(tid) * 7)
                             if kind == "ship" and phase is not None and phase >= 2 else None),
                "tokens": {"new": 40000 + len(tid) * 3100, "cache": 900000 + len(title) * 9000, "calls": 40, "model": None},
                "cites": True, "sessions": 1, "context": 60000 + len(title) * 1500, "context_limit": 200000,
                "context_how": "inferred", "last_activity": now - 300,
                "decisions": [{"state": "needs-decision", "key": k, "at": int(now - 900), "text": t} for k, t in decisions]}

    agents = [
        agent("DEMO-SEARCH", "Find where the search index drifts", "harbor-api", 0),
        agent("DEMO-AUTH", "Rotate session tokens on login", "harbor-api", 1),
        agent("DEMO-RATE", "Rate limit the public endpoints", "harbor-api", 1, crew="parked",
              decisions=[("limits", "per user or per key?")]),
        agent("DEMO-CACHE", "Cache the tide tables", "harbor-api", 2),
        agent("DEMO-LOGS", "Structured logs for the workers", "harbor-api", 3),
        agent("DEMO-I18N", "Translate the settings page", "harbor-api", 4, crew="done"),
        agent("DEMO-DARK", "Dark theme for the charts", "lighthouse-web", 0),
        agent("DEMO-FORMS", "Validate the signup form", "lighthouse-web", 3),
        agent("DEMO-ICONS", "Redraw the toolbar icons", "lighthouse-web", 4, crew="done"),
        agent("DEMO-EXPORT", "Export charts as CSV", "tide-charts", 2, crew="blocked"),
        agent("DEMO-AXIS", "Fix the time axis labels", "tide-charts", None, crew="unknown"),
        agent("DEMO-SCOUT-DB", "Compare two storage engines", "harbor-api", 0, kind="scout"),
        agent("DEMO-SCOUT-CDN", "Survey the image CDN costs", "lighthouse-web", 1, kind="scout"),
        agent("DEMO-SCOUT-UX", "Review the onboarding flow", "lighthouse-web", 2, crew="done", kind="scout"),
    ]
    day = dt.date.fromtimestamp(now)
    queue = [
        {"id": "DEMO-Q-PUSH", "title": "Push notifications", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("slot", "build slot 2/2", "build slots for harbor-api: 2 of 2 taken")], "hold_reason": ""},
        {"id": "DEMO-Q-BILL", "title": "New billing plan", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("decision", "captain's decision", "which plan ships first")], "hold_reason": "which plan ships first"},
        {"id": "DEMO-Q-MAPS", "title": "Offline maps", "repo": "lighthouse-web", "kind": "ship", "created": day.isoformat(),
         "reasons": [("date", "held until " + fmt_date_iso((day + dt.timedelta(days=3)).isoformat()), "held")],
         "hold_reason": ""},
        {"id": "DEMO-Q-AUDIT", "title": "Audit log", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("task", "waits for DEMO-AUTH", "waits for DEMO-AUTH")], "hold_reason": ""},
        {"id": "DEMO-Q-THEME", "title": "Theme picker", "repo": "lighthouse-web", "kind": "ship", "created": day.isoformat(),
         "reasons": [("ready", "ready to dispatch", "nothing holds it")], "hold_reason": ""},
        {"id": "DEMO-Q-TILES", "title": "Faster chart tiles", "repo": "tide-charts", "kind": "scout", "created": day.isoformat(),
         "reasons": [("decision", "captain's decision", "worth a scout?")], "hold_reason": "worth a scout?"},
        {"id": "DEMO-Q-DOCS", "title": "API reference pages", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("slot", "build slot 2/2", "build slots for harbor-api: 2 of 2 taken")], "hold_reason": ""},
    ]

    def lane(label, rem, reset_h, runway, exhaust_h=None):
        return {"label": label, "plan": "max", "status": "ok", "remaining": rem, "limit_label": "weekly",
                "resets": now + reset_h * h, "runway": runway, "exhausts": (now + exhaust_h * h) if exhaust_h else None,
                "windows": [{"label": "weekly", "remaining": rem, "resets": now + reset_h * h}]}

    daily = []
    for i in range(DAYS):
        new = 4000000 - i * 350000
        by = {"workers": [new * 6 // 10, new * 40], "firstmate": [new * 3 // 10, new * 20],
              "validation": [new // 10, new * 5], "other": [0, 0]}
        daily.append({"day": (day - dt.timedelta(days=i)).isoformat(), "by": by,
                      "new": sum(v[0] for v in by.values()), "cache": sum(v[1] for v in by.values())})
    delivered_rows = []
    for i, (tid, kind) in enumerate([("DEMO-D-LOGIN", "ship"), ("DEMO-D-PAGER", "ship"), ("DEMO-D-SCOUT", "scout"),
                                     ("DEMO-D-UPLOAD", "ship"), ("DEMO-D-ROUTES", "ship"), ("DEMO-D-ALERTS", "ship"),
                                     ("DEMO-D-SPEED", "scout"), ("DEMO-D-EMAIL", "ship")]):
        start = dt.datetime.fromtimestamp(now - (i + 1) * 9 * h, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        delivered_rows.append({"id": tid, "kind": kind, "model": "model-a", "start": start, "minutes": 40 + 7 * i,
                               "sheet_total": 3000000, "split": {"new": 300000 + i * 20000, "cache": 2600000, "calls": 80,
                                                                 "model": None, "subagents": i % 2},
                               "pr": None, "pr_number": 100 + i if kind == "ship" else None, "partial": False})
    return {
        "now": now,
        "firstmate": {"session": "demo0000-session", "found_by": "the demo", "model": "model-a", "context": 144000,
                      "last_call": now - 60, "limit": 200000, "limit_how": "inferred", "percent": 72.0,
                      "warn_at": DEFAULT_CONTEXT_WARN, "growth": 38000, "growth_since": now - GROWTH_HOURS * h,
                      "growth_since_compaction": False, "today_new": 1200000, "today_cache": 30000000,
                      "compactions": 1, "last_compaction": {"at": now - 7 * h, "trigger": "auto",
                                                            "before": 180000, "after": 40000}},
        "agents": agents, "queue": queue, "backlog_error": None,
        "projects": ["harbor-api", "lighthouse-web", "tide-charts"],
        "daily": daily, "delivered": delivered_rows,
        "quota": {"lanes": [lane("account-a", 8, 30, "projected_exhaustion", 5),
                            lane("account-b", 41, 50, "through_reset"),
                            lane("ordinary", 86, 90, "through_reset")], "at": None, "error": None},
        "machine": {"load": 6.0, "cpus": 8},
    }


class DemoPanel:
    """`--demo`: serves demo_snapshot() in place of a home; the add form queues nothing."""
    notice = '<div class="sm-notice sm-notice--warn">Demo: a made-up home, not real data.</div>'

    def __init__(self, port=DEFAULT_PORT):
        self.cfg = argparse.Namespace(port=port, home=os.devnull)   # no report file can exist under it
        self.token = secrets.token_urlsafe(32)
        self.last = None

    def snapshot(self):
        self.last = demo_snapshot()
        return self.last

    def recent(self):
        return self.last if self.last and time.time() - self.last["now"] < REFRESH_SECONDS else self.snapshot()

    def detail(self, tid):
        snap, now = self.recent(), time.time()
        a = next((x for x in snap["agents"] if x["id"] == tid), None)
        if a is not None:
            status = [{"state": d["state"], "at": d["at"], "key": d["key"], "text": d["text"]} for d in a["decisions"]]
            return render_agent_detail(a, "A made-up task for the demo.", status, None, [], now)
        q = next((x for x in snap["queue"] if x["id"] == tid), None)
        return render_queue_detail(q, {}, None, [], now) if q is not None else None

    def add_task(self, title, project, text, request_id):
        return {"ok": False, "error": "this is the demo: nothing was queued"}


# ---------------------------------------------------------------- HTTP


def make_handler(panel):
    allowed_hosts = {"127.0.0.1:%d" % panel.cfg.port, "localhost:%d" % panel.cfg.port}
    allowed_origins = {"http://" + h for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "shipmate"
        sys_version = ""

        def log_message(self, fmt, *args):  # no per-request log on the terminal
            pass

        def local_only(self):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return False
            # another site can make a browser talk to 127.0.0.1, but cannot set this Host (DNS rebinding)
            return self.headers.get("Host", "") in allowed_hosts

        def send(self, code, body, ctype="text/plain; charset=utf-8", nonce=None, extra=None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if nonce:
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'nonce-%s'; style-src-attr 'unsafe-inline'; "
                                 "script-src 'nonce-%s'; connect-src 'self'; form-action 'self'; img-src 'self' data:; "
                                 "base-uri 'none'; frame-ancestors 'none'" % (nonce, nonce))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.local_only():
                return self.send(403, "forbidden")
            u = urllib.parse.urlsplit(self.path)
            if u.path == "/":
                page, nonce = render_page(panel, panel.snapshot(),
                                          getattr(panel, "notice", "") + notice_html(urllib.parse.parse_qs(u.query)))
                return self.send(200, page, "text/html; charset=utf-8", nonce=nonce)
            if u.path == "/blocks":
                return self.send(200, json.dumps(render_blocks(panel.snapshot())), "application/json")
            if u.path == "/bay":
                return self.send(200, json.dumps(block_bay(panel.recent())), "application/json")
            if u.path == "/detail":
                tid = (urllib.parse.parse_qs(u.query).get("id") or [""])[0]
                body = panel.detail(tid)
                if body is None:
                    return self.send(404, "not on the board")
                return self.send(200, body, "text/html; charset=utf-8")
            if u.path == "/report":
                qs = urllib.parse.parse_qs(u.query)
                tid, ext = (qs.get("id") or [""])[0], (qs.get("f") or [""])[0]
                if not valid_id(tid) or ext not in report_files(panel.cfg.home, tid):
                    return self.send(404, "no such report")
                with open(os.path.join(panel.cfg.home, "data", tid, "report." + ext), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                # a report is shown as a document of its own: no access to this page or the network
                return self.send(200, text, "text/html; charset=utf-8" if ext == "html" else "text/plain; charset=utf-8",
                                 extra={"Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; "
                                        "script-src 'unsafe-inline'; style-src 'unsafe-inline' data:; img-src data:; font-src data:"})
            if u.path == "/state.json":
                return self.send(200, json.dumps(panel.snapshot(), default=str, ensure_ascii=False), "application/json")
            return self.send(404, "not found")

        def do_POST(self):
            if not self.local_only():
                return self.send(403, "forbidden")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                return self.send(403, "forbidden")
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                return self.send(403, "forbidden")
            if urllib.parse.urlsplit(self.path).path != "/task":
                return self.send(404, "not found")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self.send(413, "too large")
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
            get = lambda k: (form.get(k) or [""])[0]
            if not hmac.compare_digest(get("token").encode(), panel.token.encode()):
                return self.send(403, "invalid token: reload the page")
            title = re.sub(r"\s+", " ", get("title")).strip()[:200]
            project = get("project").strip()
            text = get("text").replace("\r\n", "\n").strip()[:5000]
            req = re.sub(r"[^A-Za-z0-9_-]", "", get("request"))[:40] or secrets.token_urlsafe(12)
            res = panel.add_task(title, project, text, req) if title else {"ok": False, "error": "the title is empty"}
            q = ({"added": res["id"], "notified": "yes" if res["notified"] else "no"} if res["ok"]
                 else {"error": res["error"]})
            return self.send(303, "", extra={"Location": "/?" + urllib.parse.urlencode(q)})

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(prog="shipmate.py", description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=os.environ.get("FM_HOME"))
    ap.add_argument("--fm-root", default=os.environ.get("FM_ROOT"))
    ap.add_argument("--claude-dir", action="append", default=[])
    ap.add_argument("--port", type=int, default=int(os.environ.get("SHIPMATE_PORT") or DEFAULT_PORT))
    ap.add_argument("--context-limit", type=int, default=int(os.environ.get("SHIPMATE_CONTEXT_LIMIT") or 0) or None,
                    help="firstmate's context window in tokens (default: inferred)")
    ap.add_argument("--context-warn", type=int, default=int(os.environ.get("SHIPMATE_CONTEXT_WARN") or DEFAULT_CONTEXT_WARN),
                    help="warn when the firstmate context passes this percent of the window (default 60)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--demo", action="store_true", help="serve a made-up home instead of a real one")
    a = ap.parse_args(argv)
    if a.demo:
        panel = DemoPanel(a.port)
        if a.once:
            print(json.dumps(panel.snapshot(), default=str, ensure_ascii=False, indent=1))
            return 0
        return serve(panel, "demo, a made-up home")
    if not a.home or not os.path.isdir(a.home):
        print("shipmate: name the Firstmate home with --home or FM_HOME", file=sys.stderr)
        return 2
    extra = a.claude_dir + [d for d in (os.environ.get("SHIPMATE_CLAUDE_DIRS") or "").split(os.pathsep) if d]
    cfg = Config(a.home, a.fm_root, extra, a.port, os.environ.get("SHIPMATE_QUOTA_CMD") or "quota-axi",
                 a.context_limit, a.context_warn, os.environ.get("SHIPMATE_GH_CMD") or "gh")
    if not os.path.exists(cfg.bin("fm-tasks-axi.sh")):
        print("shipmate: %s has no bin/fm-tasks-axi.sh; name the code root with --fm-root or FM_ROOT" % cfg.fm_root,
              file=sys.stderr)
        return 2
    panel = Panel(cfg)
    if a.once:
        print(json.dumps(panel.snapshot(), default=str, ensure_ascii=False, indent=1))
        return 0
    return serve(panel, "home " + cfg.home)


def serve(panel, what):
    srv = ThreadingHTTPServer(("127.0.0.1", panel.cfg.port), make_handler(panel))
    print("shipmate: http://127.0.0.1:%d/  (%s; Ctrl-C stops it)" % (panel.cfg.port, what), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
