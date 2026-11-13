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
  ship view     the same snapshot drawn as one ship, cut away, that zooms and pans: agents
                as hands whose place on board is their phase (switch with "Ship" in the header)

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
    how = tr("the session lock")
    if chosen is None:
        how = tr("newest session in the home's folder") + (tr(" (the lock names a session not found here)") if sid else "")
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


RE_HOLD_SET = re.compile(r"^Captain hold set: (\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2})Z)?")


def parse_hold_set(body):
    """When a captain hold was set, from the `Captain hold set:` line that bin/fm-captain-hold.sh
    writes first in the held task's body: (epoch, exact), exact False for a date without a time
    (read as local midnight), or (None, False) without the line."""
    m = RE_HOLD_SET.match(body or "")
    if not m:
        return None, False
    try:
        if m.group(2):
            t = dt.datetime.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")
            return t.replace(tzinfo=dt.timezone.utc).timestamp(), True
        return time.mktime(dt.date.fromisoformat(m.group(1)).timetuple()), False
    except ValueError:
        return None, False


def hold_stamps(cfg):
    """{task id: (epoch, exact)} for the queued tasks held with a `Captain hold set:` stamp."""
    rc, out, _err = run([cfg.bin("fm-tasks-axi.sh"), "list", "--state", "queued", "--fields", "held,body"], env=cfg.env())
    if rc != 0:
        return {}
    stamps = {}
    for r in parse_toon_table(out, "tasks"):
        since, exact = parse_hold_set(r.get("body"))
        if r.get("held") == "yes" and since is not None:
            stamps[r["id"]] = (since, exact)
    return stamps


def queue_rows(cfg, backlog):
    if backlog is None:
        return []
    held = any(r.get("state") == "queued" and r.get("held") == "yes" for r in backlog)
    stamps = hold_stamps(cfg) if held else {}
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
                txt = tr("held until %s") % fmt_date_iso(r["hold_until"])
                reasons.append(("date", txt, txt))
            if r.get("hold_kind") == "captain" or not r.get("hold_until"):
                reasons.append(("decision", tr("captain's decision"), (r.get("hold_reason") or tr("captain's decision"))[:400]))
        deps = [d for d in re.split(r"[\s,]+", r.get("blocked_by") or "") if d and d != "none"]
        if deps:
            reasons.append(("task", tr("waits for %s") % ", ".join(deps), tr("waits for %s") % ", ".join(deps)))
        repo = r.get("repo") or ""
        if not reasons and repo in caps:
            h = sorted(holders.get(repo, []))
            if len(h) >= caps[repo]:
                reasons.append(("slot", tr("build slot %d/%d") % (len(h), caps[repo]),
                                tr("build slots for %s: %d of %d taken (%s)") % (repo, len(h), caps[repo], ", ".join(h))))
        if not reasons:
            reasons.append(("ready", tr("ready to dispatch"), tr("nothing holds it: firstmate can dispatch it")))
        since, exact = stamps.get(r["id"]) or parse_hold_set("Captain hold set: " + (r.get("created") or ""))
        out.append({"id": r["id"], "title": r.get("title", ""), "repo": repo, "kind": r.get("kind", ""),
                    "created": r.get("created", ""), "reasons": reasons,
                    "hold_reason": (r.get("hold_reason") or "")[:240],
                    # how long it has waited for the captain, when it does: since the hold was set,
                    # or since the day it was created when the hold carries no stamp
                    "waiting_since": since if any(x[0] == "decision" for x in reasons) else None,
                    "waiting_exact": exact})
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
                return {"ok": False, "error": tr("project is not in data/projects.md")}
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
                return {"ok": False, "error": tr("the backlog refused it: %s") % ((err or out).strip()[:300] or "exit %d" % rc)}
            if res.get("already"):
                return {"ok": False, "error": tr("a task with id %s already exists; change the title") % task["id"]}
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


# ---------------------------------------------------------------- language
#
# The interface is written in English; with --lang pt-BR (or SHIPMATE_LANG=pt-BR) every phrase the
# page shows goes through tr() and comes out in Brazilian Portuguese. Text read from the home's
# records (titles, status lines, hold reasons, notes) is shown as written.

LANGS = ("en", "pt-BR")
LANG = "en"
WEEKDAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "pt-BR": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]}
MONTHS = {"en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
          "pt-BR": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]}
PT_BR = {
    # formatting, sidebar, strip
    "unknown": "desconhecido", "today %s": "hoje %s", "under 1 min": "menos de 1 min",
    "at this pace runs out %s": "neste ritmo acaba %s", "at this pace lasts until reset": "neste ritmo dura até renovar",
    "projection unknown": "projeção desconhecida", "Quota unavailable: %s": "Cota indisponível: %s",
    "No Claude account measured.": "Nenhuma conta Claude medida.", "%s %s%% (resets %s)": "%s %s%% (renova %s)",
    "%d%% left": "%d%% restante", "%s resets %s": "%s renova %s", "limit": "limite", "new tokens today": "tokens novos hoje",
    "cache reads today": "leituras de cache hoje", "Firstmate context": "Contexto do firstmate", "%d%% used": "%d%% usado",
    "%s of %s · %d compactions": "%s de %s · %d compactações", "resets %s": "renova %s", "Today": "Hoje", "%s new": "%s novos",
    "%s cache reads": "%s leituras de cache", "context %d%%": "contexto %d%%", "%s %d%% left": "%s %d%% restante",
    "%s new today": "%s novos hoje",
    # board
    "exploring": "explorando", "implementing / writing": "implementando / escrevendo", "PR open": "PR aberto",
    "waiting for CI": "esperando o CI", "ready / delivered": "pronto / entregue", "for %s": "há %s",
    "phase start unknown": "início da fase desconhecido", "%s new · %s cache": "%s novos · %s cache",
    "tokens unknown": "tokens desconhecidos", "%s: details": "%s: detalhes", "Queue": "Fila", "phase unknown": "fase desconhecida",
    "Queue unavailable: %s": "Fila indisponível: %s", "Nothing waiting.": "Nada esperando.", "+ Add task": "+ Adicionar tarefa",
    "%s: empty": "%s: vazia", "%s: nothing here": "%s: nada aqui", "%d agent · %d in the queue": "%d agente · %d na fila",
    "%d agents · %d in the queue": "%d agentes · %d na fila",
    # crew states and phases
    "working": "trabalhando", "parked": "parado", "blocked": "bloqueado", "failed": "falhou", "done": "concluído",
    "paused": "pausado", "implementing": "implementando", "ready to merge": "pronto para o merge",
    "writing the report": "escrevendo o relatório", "report delivered": "relatório entregue",
    # queue reasons
    "held until %s": "segura até %s", "captain's decision": "decisão do capitão", "waits for %s": "espera %s",
    "build slot %d/%d": "vaga de build %d/%d", "build slots for %s: %d of %d taken (%s)": "vagas de build de %s: %d de %d ocupadas (%s)",
    "ready to dispatch": "pronta para despachar", "nothing holds it: firstmate can dispatch it": "nada a segura: o firstmate pode despachá-la",
    # drawer
    "not reached": "não alcançada", "time unknown": "hora desconhecida", "no report": "sem relatório", "no PR": "sem PR",
    "(checks unknown: %s)": "(checks desconhecidos: %s)", "%d passed": "%d passaram", "%d failed": "%d falharam",
    "%d running": "%d rodando", "%d skipped": "%d pulados", " draft": " rascunho", "failing: %s": "falhando: %s",
    "read from the forge %s ago, at most every %d s": "lido da forja há %s, no máximo a cada %d s",
    "no status lines": "sem linhas de status", "%s of %s (%s window), %d%%": "%s de %s (janela %s), %d%%",
    "last action %s ago": "última ação há %s", "Phases": "Fases", "current: %s%s · evidence: %s": "atual: %s%s · evidência: %s",
    "Pull request": "Pull request", "Report": "Relatório", "Captain's intent": "Intenção do capitão",
    "Latest status lines": "Últimas linhas de status", "Project": "Projeto", "Kind": "Tipo", "Model": "Modelo",
    "Effort": "Esforço", "Account": "Conta", "Harness": "Harness", "Started": "Início", "Idle": "Ocioso", "Tokens": "Tokens",
    "Context": "Contexto", "%s new · %s cache reads": "%s novos · %s leituras de cache", " for %s": " há %s",
    "no Captain's intent section in data/%s/brief.md": "sem seção Captain's intent em data/%s/brief.md",
    "queued": "na fila", "Why it waits": "Por que espera", "Notes": "Notas", "Since": "Desde", "Priority": "Prioridade",
    "Depends on": "Depende de", "not set": "não definido", "none": "nenhuma", "no notes on this item": "sem notas neste item",
    # firstmate session
    "Firstmate session unknown: no Claude transcript in the project folder of this home.":
        "Sessão do firstmate desconhecida: nenhuma transcrição Claude na pasta de projeto desta home.",
    "growth unknown (no call before the last %d h)": "crescimento desconhecido (nenhuma chamada antes das últimas %d h)",
    "%s%s since the compaction %s": "%s%s desde a compactação %s", "%s%s in the last %d h": "%s%s nas últimas %d h",
    "%d compaction": "%d compactação", "%d compactions": "%d compactações", ", last %s (%s, %s to %s)": ", última %s (%s, %s para %s)",
    "trigger unknown": "gatilho desconhecido",
    "Context is past %d%% of the window: a good moment to clear or compact the firstmate session.":
        "O contexto passou de %d%% da janela: bom momento para limpar ou compactar a sessão do firstmate.",
    "warning threshold": "limite de aviso", "%s of %s (%s window) · last call %s": "%s de %s (janela %s) · última chamada %s",
    "today: %s new · %s cache reads": "hoje: %s novos · %s leituras de cache", "session %s, found by %s": "sessão %s, encontrada por %s",
    "over %d%%": "acima %d%%", "inferred": "inferida", "configured": "configurada", "the session lock": "a trava da sessão",
    "newest session in the home's folder": "a sessão mais nova na pasta da home",
    " (the lock names a session not found here)": " (a trava aponta uma sessão que não está aqui)", "the demo": "a demo", "phases completed": "fases concluídas",
    # usage
    "Per day, every session of the accounts read": "Por dia, todas as sessões das contas lidas", "new": "novos",
    "%s cache": "%s cache", "no use": "sem uso", "workers": "workers", "firstmate": "firstmate", "validation": "validação",
    "other": "outros", "new: input + cache writes + output": "novos: entrada + escritas de cache + saída", "cache reads": "leituras de cache",
    "Per delivered task, session sheets of the last %d days": "Por tarefa entregue, fichas de sessão dos últimos %d dias",
    "No session sheets (data/fichas/fichas.jsonl is absent).": "Sem fichas de sessão (data/fichas/fichas.jsonl não existe).",
    "No session sheet in the last %d days.": "Nenhuma ficha de sessão nos últimos %d dias.", "task": "tarefa", "kind": "tipo",
    "model": "modelo", "wall time": "duração", "cache": "cache", "(+%d subagent)": "(+%d subagente)", "(+%d subagents)": "(+%d subagentes)",
    "(sheet total %s; transcript not found)": "(total da ficha %s; transcrição não encontrada)", " (partial)": " (parcial)",
    # the add form and its notices
    "Title": "Título", "Goes to the queue and notifies firstmate.": "Vai para a fila e avisa o firstmate.", "Queue it": "Enfileirar",
    "no projects": "sem projetos", "Task %s is queued and firstmate was notified.": "A tarefa %s está na fila e o firstmate foi avisado.",
    "Task %s is queued, but notifying firstmate failed; tell firstmate another way.":
        "A tarefa %s está na fila, mas avisar o firstmate falhou; avise o firstmate de outro jeito.",
    "Not queued: %s": "Não enfileirada: %s", "project is not in data/projects.md": "o projeto não está em data/projects.md",
    "the backlog refused it: %s": "o backlog recusou: %s", "a task with id %s already exists; change the title":
        "já existe uma tarefa com id %s; mude o título", "this is the demo: nothing was queued": "isto é a demo: nada foi enfileirado",
    "Demo: a made-up home, not real data.": "Demo: uma home inventada, não são dados reais.",
    # the ship
    "in the crow's nest with a spyglass": "na gávea com a luneta", "on deck with a hammer": "no convés com o martelo",
    "at the bow, signalling with flags": "na proa, sinalizando com bandeiras", "fishing at the rail": "pescando na amurada",
    "on the gangplank with a pack, waiting to go ashore": "na prancha com a mochila, esperando para desembarcar",
    "on deck, phase unknown": "no convés, fase desconhecida", "rowing out to sea": "remando mar adentro",
    "rowing back with the map": "voltando a remo com o mapa", "moored at the pier with the map": "atracado no cais com o mapa",
    "crew is %s": "estado da tripulação: %s", "%d hand aboard": "%d marujo a bordo", "%d hands aboard": "%d marujos a bordo",
    "%d red flag": "%d bandeira vermelha", "%d red flags": "%d bandeiras vermelhas", "%d off duty": "%d de folga",
    "%d boat moored": "%d bote atracado", "%d boats moored": "%d botes atracados", "today": "hoje",
    "no project": "sem projeto", "; off duty": "; de folga", "; needs the captain: ": "; precisa do capitão: ",
    "; number %d in line for the captain, waiting %s": "; número %d na fila do capitão, esperando há %s",
    "%s: queued (%s)%s%s": "%s: na fila (%s)%s%s",
    "the captain, off duty: nothing waits on a decision": "o capitão, de folga: nada espera uma decisão",
    "the captain: %d waiting, the longest %s for %s": "o capitão: %d esperando, o mais antigo %s há %s",
    "%d waiting for you": "%d esperando você", "longest: %s, %s": "mais antigo: %s, %s",
    "LOOKOUT · exploring": "GÁVEA · explorando", "HELM · firstmate": "LEME · firstmate", "CAPTAIN'S CABIN": "CABINE DO CAPITÃO",
    "DECK · implementing": "CONVÉS · implementando", "? · phase unknown": "? · fase desconhecida",
    "RAIL · waiting for CI": "AMURADA · esperando o CI", "BOW · PR open": "PROA · PR aberto",
    "HOLD · quota barrels, context water": "PORÃO · barris de cota, água de contexto",
    "GANGPLANK · ready to merge": "PRANCHA · pronto para o merge", "ASTERN · queued, waiting for you": "POPA · na fila, esperando você",
    "QUEUE": "FILA", "SCOUTS": "BATEDORES", "DELIVERED · %d days": "ENTREGUES · %d dias",
    "context %d%% of the window%s": "contexto em %d%% da janela%s", ", past the %d%% warning": ", acima do aviso de %d%%",
    "context unknown": "contexto desconhecido", "firstmate at the helm: %s": "o firstmate no leme: %s",
    "%s: %s%s": "%s: %s%s", ", runs out before it resets": ", acaba antes de renovar",
    "the hold: bilge water is firstmate's %s; the white line is the warning threshold":
        "o porão: a água é o %s do firstmate; a linha branca é o limite de aviso",
    "%d task delivered in the last %d days%s": "%d tarefa entregue nos últimos %d dias%s",
    "%d tasks delivered in the last %d days%s": "%d tarefas entregues nos últimos %d dias%s",
    "The ship: %s": "O navio: %s", "no project aboard": "nenhum projeto a bordo",
    "scouts": "batedores", "off duty": "de folga", "waiting for you": "esperando você",
    "in the crow's nest with a spyglass: reading the code and planning before it builds":
        "na gávea com a luneta: lendo o código e planejando antes de construir",
    "on deck with a hammer: making the change": "no convés com o martelo: fazendo a mudança",
    "at the bow with signal flags: the pull request is open": "na proa com as bandeiras de sinal: o pull request está aberto",
    "fishing at the rail: the checks are running": "pescando na amurada: os checks estão rodando",
    "on the gangplank with a pack: done, waiting to go ashore": "na prancha com a mochila: pronto, esperando para desembarcar",
    "under a question mark: no sign of its phase yet": "sob um ponto de interrogação: ainda sem sinal da fase",
    "in a skiff: rowing out to explore, back with the map, then moored at the pier with the report":
        "num bote: rema para explorar, volta com o mapa e atraca no cais com o relatório",
    "sitting with the dice: paused, or quiet for a while": "sentado com os dados: pausado, ou quieto há um tempo",
    "a red flag: needs your decision; hands wait in your cabin and queued tasks are tied astern, longest wait first, "
    "and the log below lists them all":
        "uma bandeira vermelha: precisa da sua decisão; os marujos esperam na sua cabine e as tarefas da fila ficam "
        "amarradas na popa, quem espera há mais tempo primeiro, e o diário abaixo lista todos",
    "%s: %s. %s": "%s: %s. %s", "Now: %s.": "Agora: %s.", "Nobody now.": "Ninguém agora.",
    "%d waits on my word. The longest, %s, has waited %s.": "%d espera a minha palavra. O mais antigo, %s, espera há %s.",
    "%d wait on my word. The longest, %s, has waited %s.": "%d esperam a minha palavra. O mais antigo, %s, espera há %s.",
    "queued, %s": "na fila, %s", "All clear. Nothing waits on my word.": "Tudo em ordem. Nada espera a minha palavra.",
    "Nobody is waiting for you.": "Ninguém está esperando você.", "Captain": "Capitão",
}

# the page template's own phrases ({{t:...}} and {{j:...}} in shipmate.html)
PT_BR.update({
    "View": "Vista", "Board": "Quadro", "Ship": "Navio", "refresh failed": "falha ao atualizar", "updated": "atualizado",
    "Work by phase": "Trabalho por fase", "The ship": "O navio", "Light a stage": "Acender uma etapa",
    "Click a stage to light its hands and take the view to them.":
        "Clique numa etapa para acender os marujos dela e levar a vista até eles.",
    "Zoom in": "Aproximar", "Zoom out": "Afastar", "Frame the crew": "Enquadrar a tripulação", "crew": "tripulação",
    "Fit everything": "Mostrar tudo", "fit all": "tudo", "Show the signs on the ship": "Mostrar as placas no navio",
    "signs": "placas", "Waiting for you": "Esperando você",
    "Wheel or pinch to zoom, drag to move, double click to zoom in there. Click a hand, a queued boat or a card in the log for its details.":
        "Roda ou pinça para o zoom, arraste para mover, clique duplo para aproximar ali. Clique num marujo, num bote da fila "
        "ou num cartão do diário para ver os detalhes.",
    "Projects aboard": "Projetos a bordo",
    "A shirt and bandana take the project's colour, and so do the stripes on the sails.":
        "A camisa e a bandana têm a cor do projeto, assim como as listras das velas.",
    "A red flag: the hand needs the captain (its crew is parked, blocked or failed, or a needs-decision or blocked line is still open), or a queued boat waits on the captain's decision.":
        "Uma bandeira vermelha: o marujo precisa do capitão (a tripulação está parada, bloqueada ou falhou, ou uma linha "
        "needs-decision ou blocked segue aberta), ou um bote da fila espera a decisão do capitão.",
    "Whoever needs the captain waits in the captain's cabin, and a queued task tied astern, numbered by how long it has waited; the captain attends them at the desk, or sits with a mug when nobody waits.":
        "Quem precisa do capitão espera na cabine do capitão, e a tarefa da fila fica amarrada na popa, numerados por quanto "
        "tempo esperam; o capitão atende à mesa, ou senta com uma caneca quando ninguém espera.",
    "Barrels in the hold: each account's quota, amber what is left, red when it runs out before it resets.":
        "Barris no porão: a cota de cada conta, em âmbar o que resta, em vermelho quando acaba antes de renovar.",
    "Water in the hold: firstmate's context, the white line its warning threshold.":
        "Água no porão: o contexto do firstmate, a linha branca é o limite de aviso.",
    "Scouts row out to the horizon, back with the map, and moor at the pier with it.":
        "Os batedores remam até o horizonte, voltam com o mapa e atracam no cais com ele.",
    "Boats at the pier: the queue. Crates: tasks delivered in the last 7 days.":
        "Botes no cais: a fila. Caixotes: tarefas entregues nos últimos 7 dias.",
    "The sky follows the local time, the sea the machine's load.": "O céu segue a hora local, o mar a carga da máquina.",
    "Usage": "Uso", "per day and per delivered task": "por dia e por tarefa entregue",
    "Firstmate session and accounts": "Sessão do firstmate e contas", "Firstmate session": "Sessão do firstmate",
    "Accounts and quota": "Contas e cota", "Details": "Detalhes", "Close": "Fechar",
    "%d lane out of view, scroll the board sideways": "%d raia fora de vista, role o quadro para o lado",
    "%d lanes out of view, scroll the board sideways": "%d raias fora de vista, role o quadro para o lado",
    "+%d more below": "+%d abaixo", "Loading…": "Carregando…", "Add a task": "Adicionar tarefa",
    "This item is no longer on the board.": "Este item não está mais no quadro.",
})
RE_PHRASE = re.compile(r"\{\{([tj]):(.+?)\}\}")


def translate_template(page):
    """The template's {{t:phrase}} as HTML text and {{j:phrase}} as a JavaScript string, in the
    interface language, and {{LANG}} as the page's language."""
    return RE_PHRASE.sub(lambda m: esc(tr(m.group(2))) if m.group(1) == "t" else json.dumps(tr(m.group(2))),
                         page.replace("{{LANG}}", LANG))


def tr(text):
    """text in the interface language: English is the key, and a phrase with no translation stays English."""
    return text if LANG == "en" else PT_BR.get(text, text)


# ---------------------------------------------------------------- formatting

esc = html.escape


def fmt_day(d):
    return "%s %02d %s" % (WEEKDAYS[LANG][d.weekday()], d.day, MONTHS[LANG][d.month - 1])


def fmt_tokens(n):
    if n is None:
        return tr("unknown")
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
        return tr("unknown")
    w = dt.datetime.fromtimestamp(ts)
    if w.date() == dt.date.fromtimestamp(now or time.time()):
        return tr("today %s") % w.strftime("%H:%M")
    return "%s %s" % (fmt_day(w.date()), w.strftime("%H:%M"))


def fmt_date_iso(s):
    try:
        return fmt_day(dt.date.fromisoformat(s))
    except (TypeError, ValueError):
        return s or "?"


def fmt_dur(sec):
    if sec is None:
        return tr("unknown")
    sec = max(0, int(sec))
    if sec < 60:
        return tr("under 1 min")
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
        return tr("at this pace runs out %s") % fmt_when(ln["exhausts"], now), ("danger" if early else "muted")
    if ln["runway"] == "through_reset":
        return tr("at this pace lasts until reset"), "muted"
    return tr("projection unknown"), "muted"


def lanes_by_urgency(q):
    """Accounts with the least left first; unmeasured ones last."""
    return sorted(q.get("lanes") or [], key=lambda ln: (ln["remaining"] is None, ln["remaining"] or 0))


# ---------------------------------------------------------------- blocks


def block_accounts(q, now):
    if q.get("error"):
        return '<div class="sm-empty">%s</div>' % esc(tr("Quota unavailable: %s") % q["error"])
    if not q["lanes"]:
        return '<div class="sm-empty">%s</div>' % esc(tr("No Claude account measured."))
    rows = []
    for ln in lanes_by_urgency(q):
        rem = ln["remaining"]
        tone = quota_tone(rem)
        proj, ptone = runway_text(ln, now)
        wins = " · ".join(tr("%s %s%% (resets %s)") % (w["label"] or "?", w["remaining"], fmt_when(w["resets"], now))
                          for w in ln["windows"])
        rows.append(
            '<div class="sm-acct" title="%s">'
            '<div class="sm-acct__top"><span class="sm-acct__name">%s</span>%s<span class="sm-acct__pct sm-%s">%s</span></div>'
            '<div class="sm-bar"><div class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></div></div>'
            '<div class="sm-acct__sub"><span>%s</span><span class="sm-%s">%s</span></div></div>'
            % (esc(wins), esc(ln["label"]), badge(ln["plan"]) if ln["plan"] else "",
               tone, (tr("%d%% left") % rem) if rem is not None else tr("unknown"),
               tone, max(0, min(100, rem or 0)),
               esc(tr("%s resets %s") % (ln["limit_label"] or tr("limit"), fmt_when(ln["resets"], now))), ptone, esc(proj)))
    return "".join(rows)


def block_today(daily):
    d = daily[0] if daily else {"new": 0, "cache": 0}
    return ('<div class="sm-tile"><b>%s</b><span>%s</span></div><div class="sm-tile"><b>%s</b><span>%s</span></div>'
            % (esc(fmt_tokens(d["new"])), esc(tr("new tokens today")), esc(fmt_tokens(d["cache"])), esc(tr("cache reads today"))))


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
        out.append(gauge(tr("Firstmate context"), pct, tr("%d%% used") % round(pct),
                         tr("%s of %s · %d compactions") % (fmt_tokens(fm["context"]), fmt_tokens(fm["limit"]), fm["compactions"]),
                         "danger" if pct >= fm["warn_at"] else "ok"))
    for ln in lanes_by_urgency(s["quota"]):
        rem = ln["remaining"]
        out.append(gauge(ln["label"], rem, (tr("%d%% left") % rem) if rem is not None else tr("unknown"),
                         runway_text(ln, now)[0], quota_tone(rem), tr("resets %s") % fmt_when(ln["resets"], now)))
    d = s["daily"][0] if s["daily"] else {"new": 0, "cache": 0}
    out.append('<div class="sm-gauge sm-gauge--today"><div class="sm-gauge__top"><b>%s</b><span>%s</span></div>'
               '<div class="sm-gauge__sub">%s</div></div>' % (esc(tr("Today")), esc(tr("%s new") % fmt_tokens(d["new"])),
                                                              esc(tr("%s cache reads") % fmt_tokens(d["cache"]))))
    return "".join(out)


def block_sumline(s):
    """Phones: one line that opens the full session and accounts blocks."""
    parts = []
    fm = s.get("firstmate")
    if fm and fm["percent"] is not None:
        over = fm["percent"] >= fm["warn_at"]
        parts.append('<span class="sm-%s">%s</span>' % ("danger" if over else "ok", tr("context %d%%") % round(fm["percent"])))
    lanes = lanes_by_urgency(s["quota"])
    if lanes and lanes[0]["remaining"] is not None:
        parts.append('<span class="sm-%s">%s</span>' % (quota_tone(lanes[0]["remaining"]),
                                                         esc(tr("%s %d%% left") % (lanes[0]["label"], lanes[0]["remaining"]))))
    d = s["daily"][0] if s["daily"] else {"new": 0}
    parts.append("<span>%s</span>" % esc(tr("%s new today") % fmt_tokens(d["new"])))
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
    since = (tr("for %s") % fmt_dur(now - a["phase_since"])) if a["phase_since"] else tr("phase start unknown")
    toks = tr("%s new · %s cache") % (fmt_tokens(tok["new"]), fmt_tokens(tok["cache"])) if tok else tr("tokens unknown")
    title = a["title"] or a["id"]
    return ('<article class="sm-card" data-id="%s" tabindex="0" role="button" aria-label="%s">'
            '<div class="sm-card__top"><span class="sm-id">%s</span>%s</div>'
            '<div class="sm-card__title" title="%s">%s</div>'
            '<div class="sm-card__line" title="%s">%s</div>'
            '<div class="sm-card__line">%s%s</div>'
            '<div class="sm-card__line sm-mono">%s</div></article>'
            % (esc(a["id"]), esc(tr("%s: details") % a["id"]), esc(a["id"]), badge(tr(a["crew"]), CREW_TONE.get(a["crew"], "neutral")),
               esc(title), esc(title), esc(meta), esc(meta), esc(since),
               (" · " + link(a["pr"], pr_text(a["pr"]))) if a["pr"] else "", esc(toks)))


def queue_card(q):
    reasons = " ".join(badge(short, REASON_TONE.get(k, "neutral"), full) for k, short, full in q["reasons"])
    return ('<article class="sm-card sm-card--queue" data-id="%s" tabindex="0" role="button" aria-label="%s">'
            '<div class="sm-card__top"><span class="sm-id">%s</span><span class="sm-faint sm-card__repo">%s</span></div>'
            '<div class="sm-card__title" title="%s">%s</div><div class="sm-reasons">%s</div></article>'
            % (esc(q["id"]), esc(tr("%s: details") % q["id"]), esc(q["id"]), esc(q["repo"]), esc(q["title"]), esc(q["title"]), reasons))


def block_board(agents, queue, err, now):
    """Queue lane, then one lane per phase. A phase with no card shrinks to a labelled strip so
    the lanes with work get the width, and keeps its place so the order never changes."""
    lanes = [[] for _ in LANE_NAMES]
    unknown = []
    for a in agents:
        i = lane_of(a)
        (unknown if i is None else lanes[i]).append(agent_card(a, now))
    spec = [(tr("Queue"), [queue_card(q) for q in queue], True)] + [(tr(n), c, False) for n, c in zip(LANE_NAMES, lanes)]
    if unknown:
        spec.append((tr("phase unknown"), unknown, False))
    cols, html_lanes = [], []
    for name, cards, is_queue in spec:
        if cards or is_queue:
            cols.append("minmax(%dpx, 1fr)" % LANE_MIN)
            if not cards:
                cards = ['<div class="sm-empty">%s</div>' % esc((tr("Queue unavailable: %s") % err) if err else tr("Nothing waiting."))]
            html_lanes.append(
                '<section class="sm-lane%s" aria-label="%s"><div class="sm-lane__head"><span>%s</span><span class="sm-count">%d</span></div>'
                '%s<div class="sm-lane__body">%s</div><div class="sm-lane__more" hidden></div></section>'
                % (" sm-lane--queue" if is_queue else "", esc(name), esc(name),
                   len(queue) if is_queue else len(cards),
                   '<button class="sm-addbtn" type="button" data-action="add">%s</button>' % esc(tr("+ Add task")) if is_queue else "",
                   "".join(cards)))
        else:
            cols.append("44px")
            html_lanes.append('<section class="sm-lane sm-lane--empty" aria-label="%s" title="%s">'
                              '<span class="sm-count">0</span><span class="sm-vert">%s</span></section>'
                              % (esc(tr("%s: empty") % name), esc(tr("%s: nothing here") % name), esc(name)))
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
            txt = tr("not reached")
        else:
            txt = tr("time unknown")
        items.append('<li class="sm-tl__%s"><span class="sm-tl__dot"></span><b>%s</b><span>%s</span></li>' % (state, esc(tr(name)), esc(txt)))
    return '<ol class="sm-tl">%s</ol>' % "".join(items)


def report_links(tid, reports):
    if not reports:
        return '<span class="sm-faint">%s</span>' % esc(tr("no report"))
    return " · ".join(link("/report?id=%s&f=%s" % (urllib.parse.quote(tid), ext), "report." + ext) for ext in reports)


def checks_html(pr, checks):
    if not pr:
        return '<span class="sm-faint">%s</span>' % esc(tr("no PR"))
    out = link(pr, pr_text(pr))
    if not checks:
        return out
    if checks.get("error"):
        return out + ' <span class="sm-faint">%s</span>' % esc(tr("(checks unknown: %s)") % checks["error"])
    c = checks["counts"]
    parts = [esc(tr("%d passed") % c["pass"])]
    if c["fail"]:
        parts.append('<span class="sm-danger">%s</span>' % esc(tr("%d failed") % c["fail"]))
    if c["pending"]:
        parts.append('<span class="sm-warn">%s</span>' % esc(tr("%d running") % c["pending"]))
    if c["skipped"]:
        parts.append(esc(tr("%d skipped") % c["skipped"]))
    state = (checks.get("state") or "?").lower() + (tr(" draft") if checks.get("draft") else "")
    out += " · %s · %s" % (esc(state), ", ".join(parts))
    if checks["failing"]:
        out += '<div class="sm-faint">%s</div>' % esc(tr("failing: %s") % ", ".join(checks["failing"]))
    return out + '<div class="sm-faint">%s</div>' % esc(tr("read from the forge %s ago, at most every %d s") % (
        fmt_dur(checks.get("age", 0)) if checks.get("age", 0) >= 60 else "%d s" % checks.get("age", 0), CHECKS_TTL))


def status_lines(status):
    if not status:
        return '<div class="sm-faint">%s</div>' % esc(tr("no status lines"))
    rows = []
    for s in reversed(status):
        rows.append('<li><span class="sm-st sm-st--%s">%s</span><span class="sm-faint">%s</span><span>%s</span></li>'
                    % (esc(s["state"]), esc(s["state"]), esc(fmt_when(s["at"], time.time()) if s["at"] else tr("time unknown")),
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
        ctx = ('%s<span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></span></span>'
               % (esc(tr("%s of %s (%s window), %d%%") % (fmt_tokens(a["context"]), fmt_tokens(a["context_limit"]), tr(a["context_how"]), round(pct))),
                  "danger" if pct >= 80 else "warn" if pct >= 60 else "ok", min(100, round(pct))))
    else:
        ctx = esc(tr("unknown"))
    idle = (tr("last action %s ago") % fmt_dur(now - a["last_activity"])) if a["last_activity"] else tr("unknown")
    phase = tr(a["phases"][a["phase"]]) if a["phase"] is not None else tr("unknown")
    return (
        '<div class="sm-dhead"><span class="sm-id">%s</span>%s</div><h2 class="sm-dtitle">%s</h2>'
        '<div class="sm-facts">%s</div>'
        '<h3>%s</h3>%s<div class="sm-faint">%s</div>'
        '<h3>%s</h3><div>%s</div>'
        '<h3>%s</h3><div>%s</div>'
        '<h3>%s</h3>%s'
        '<h3>%s</h3>%s'
        % (esc(a["id"]), badge(tr(a["crew"]), CREW_TONE.get(a["crew"], "neutral")), esc(a["title"] or a["id"]),
           "".join([fact(tr("Project"), esc(a["project"] or "?")), fact(tr("Kind"), esc(a["kind"])), fact(tr("Model"), esc(model)),
                    fact(tr("Effort"), esc(a["effort"] or "?")), fact(tr("Account"), esc(a["account"])),
                    fact(tr("Harness"), esc(a["harness"])), fact(tr("Started"), esc(fmt_when(a["spawn"], now))),
                    fact(tr("Idle"), esc(idle)),
                    fact(tr("Tokens"), esc(tr("%s new · %s cache reads") % (fmt_tokens(tok["new"]), fmt_tokens(tok["cache"])) if tok else tr("unknown"))),
                    fact(tr("Context"), ctx)]),
           esc(tr("Phases")), timeline(a["phases"], a.get("milestones") or [], a["phase"], now),
           esc(tr("current: %s%s · evidence: %s") % (phase, (tr(" for %s") % fmt_dur(now - a["phase_since"])) if a["phase_since"] else "",
                                                     a["phase_why"])),
           esc(tr("Pull request")), checks_html(a["pr"], checks), esc(tr("Report")), report_links(a["id"], reports),
           esc(tr("Captain's intent")), pre(intent, tr("no Captain's intent section in data/%s/brief.md") % a["id"]),
           esc(tr("Latest status lines")), status_lines(status)))


def render_queue_detail(q, full, intent, reports, now):
    reasons = "".join('<li>%s%s</li>' % (badge(short, REASON_TONE.get(k, "neutral")),
                                         (" <span>%s</span>" % esc(long_)) if long_ != short else "")
                      for k, short, long_ in q["reasons"])
    return (
        '<div class="sm-dhead"><span class="sm-id">%s</span>%s</div><h2 class="sm-dtitle">%s</h2>'
        '<div class="sm-facts">%s</div><h3>%s</h3><ul class="sm-reasonlist">%s</ul>'
        '<h3>%s</h3>%s%s<h3>%s</h3><div>%s</div>'
        % (esc(q["id"]), badge(tr("queued"), "info"), esc(full.get("title") or q["title"]),
           "".join([fact(tr("Project"), esc(q["repo"] or "?")), fact(tr("Kind"), esc(q["kind"] or tr("not set"))),
                    fact(tr("Since"), esc(fmt_date_iso(q["created"]) if q["created"] else "?")),
                    fact(tr("Priority"), esc(full.get("priority") or "-")), fact(tr("Depends on"), esc(full.get("deps") or tr("none")))]),
           esc(tr("Why it waits")), reasons, esc(tr("Notes")),
           pre(full.get("body") if full.get("body") not in (None, "-") else None, tr("no notes on this item")),
           ("<h3>%s</h3>" % esc(tr("Captain's intent")) + pre(intent, "")) if intent else "", esc(tr("Report")), report_links(q["id"], reports)))


def block_firstmate(fm, now):
    if fm is None:
        return '<div class="sm-empty">%s</div>' % esc(tr("Firstmate session unknown: no Claude transcript in the project folder of this home."))
    pct = fm["percent"]
    over = pct is not None and pct >= fm["warn_at"]
    tone = "danger" if over else "ok"
    g = fm["growth"]
    if g is None:
        growth = tr("growth unknown (no call before the last %d h)") % GROWTH_HOURS
    elif fm["growth_since_compaction"]:
        growth = tr("%s%s since the compaction %s") % ("+" if g >= 0 else "-", fmt_tokens(abs(g)), fmt_when(fm["growth_since"], now))
    else:
        growth = tr("%s%s in the last %d h") % ("+" if g >= 0 else "-", fmt_tokens(abs(g)), GROWTH_HOURS)
    lc = fm["last_compaction"]
    comp = tr("%d compaction" if fm["compactions"] == 1 else "%d compactions") % fm["compactions"]
    if lc:
        comp += tr(", last %s (%s, %s to %s)") % (fmt_when(lc["at"], now), lc["trigger"] or tr("trigger unknown"),
                                                 fmt_tokens(lc["before"]), fmt_tokens(lc["after"]))
    warn = ('<div class="sm-warnline">%s</div>'
            % esc(tr("Context is past %d%% of the window: a good moment to clear or compact the firstmate session.") % fm["warn_at"])
            if over else "")
    return (
        '<div class="sm-acct">'
        '<div class="sm-acct__top"><span class="sm-acct__name">%s</span>%s<span class="sm-acct__pct sm-%s">%s</span></div>'
        '<div class="sm-bar"><div class="sm-bar__fill sm-bar__fill--%s" style="width:%d%%"></div>'
        '<div class="sm-bar__mark" style="left:%d%%" title="%s"></div></div>'
        '<div class="sm-acct__sub"><span>%s</span><span>%s</span></div>'
        '<div class="sm-acct__sub"><span>%s</span><span>%s</span></div>'
        '%s<div class="sm-acct__win">%s</div></div>'
        % (esc(tr("Context")), badge(tr("over %d%%") % fm["warn_at"], "danger") if over else "", tone,
           esc(tr("%d%% used") % round(pct)) if pct is not None else esc(tr("unknown")),
           tone, max(0, min(100, round(pct or 0))), max(0, min(100, fm["warn_at"])), esc(tr("warning threshold")),
           esc(tr("%s of %s (%s window) · last call %s") % (fmt_tokens(fm["context"]), fmt_tokens(fm["limit"]), tr(fm["limit_how"]),
                                                           fmt_when(fm["last_call"], now))), esc(growth),
           esc(tr("today: %s new · %s cache reads") % (fmt_tokens(fm["today_new"]), fmt_tokens(fm["today_cache"]))), esc(comp),
           warn, esc(tr("session %s, found by %s") % (fm["session"][:8], fm["found_by"]))))


def phase_bar(a):
    segs = []
    for i, name in enumerate(a["phases"]):
        cls = "done" if a["phase"] is not None and i < a["phase"] else "now" if a["phase"] == i else ""
        segs.append('<span class="sm-step %s" title="%s"></span>' % (cls, esc(tr(name))))
    return '<span class="sm-steps" role="img" aria-label="%s">%s</span>' % (esc(tr("phases completed")), "".join(segs))


def block_usage(daily, deliv):
    top_new = max([d["new"] for d in daily] + [1])
    top_cache = max([d["cache"] for d in daily] + [1])
    out = ['<div class="sm-sub">%s</div><div class="sm-days">' % esc(tr("Per day, every session of the accounts read"))]
    for d in daily:
        by = " · ".join("%s %s" % (tr(c), fmt_tokens(v[0])) for c, v in d["by"].items() if v[0])
        out.append('<div class="sm-day"><span class="sm-day__d">%s</span>'
                   '<span class="sm-day__bars"><span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--new" style="width:%.1f%%"></span></span>'
                   '<span class="sm-bar sm-bar--thin"><span class="sm-bar__fill sm-bar__fill--cache" style="width:%.1f%%"></span></span></span>'
                   '<span class="sm-day__n"><b>%s</b> %s<br><span class="sm-faint">%s</span></span>'
                   '<span class="sm-day__by sm-faint">%s</span></div>'
                   % (esc(fmt_date_iso(d["day"])), 100.0 * d["new"] / top_new, 100.0 * d["cache"] / top_cache,
                      esc(fmt_tokens(d["new"])), esc(tr("new")), esc(tr("%s cache") % fmt_tokens(d["cache"])), esc(by or tr("no use"))))
    out.append('</div><div class="sm-legend"><span class="sm-sw sm-sw--new"></span>%s<span class="sm-sw sm-sw--cache"></span>%s</div>'
               % (esc(tr("new: input + cache writes + output")), esc(tr("cache reads"))))
    out.append('<div class="sm-sub">%s</div>' % esc(tr("Per delivered task, session sheets of the last %d days") % DAYS))
    if deliv is None:
        out.append('<div class="sm-empty">%s</div>' % esc(tr("No session sheets (data/fichas/fichas.jsonl is absent).")))
        return "".join(out)
    if not deliv:
        out.append('<div class="sm-empty">%s</div>' % esc(tr("No session sheet in the last %d days.") % DAYS))
        return "".join(out)
    out.append('<div class="sm-table"><table><thead><tr>%s<th></th></tr></thead><tbody>'
               % "".join('<th%s>%s</th>' % (' class="r"' if h in ("new", "cache") else "", esc(tr(h)))
                         for h in ("task", "kind", "model", "wall time", "new", "cache")))
    for r in deliv:
        sp = r["split"]
        if sp:
            extra = (' <span class="sm-faint">%s</span>' % esc(tr("(+%d subagents)" if sp["subagents"] > 1 else "(+%d subagent)") % sp["subagents"])
                     if sp.get("subagents") else "")
        else:
            extra = ' <span class="sm-faint">%s</span>' % esc(tr("(sheet total %s; transcript not found)") % fmt_tokens(r["sheet_total"]))
        prl = link(r["pr"], pr_text(r["pr"])) if r["pr"] else (esc("PR #%s" % r["pr_number"]) if r["pr_number"] else "")
        out.append('<tr><td><span class="sm-id">%s</span>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                   '<td class="r">%s%s</td><td class="r">%s</td><td>%s</td></tr>'
                   % (esc(r["id"] or "?"), esc(tr(" (partial)")) if r["partial"] else "", esc(r["kind"] or "?"), esc(r["model"] or "?"),
                      esc(fmt_dur(r["minutes"] * 60) if isinstance(r["minutes"], (int, float)) else "?"),
                      esc(fmt_tokens(sp["new"]) if sp else tr("unknown")), extra,
                      esc(fmt_tokens(sp["cache"]) if sp else tr("unknown")), prl))
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
        "counts": tr("%d agent · %d in the queue" if len(s["agents"]) == 1 else "%d agents · %d in the queue") % (len(s["agents"]), len(s["queue"])),
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
            '<label>%s<input name="title" required maxlength="200" autocomplete="off"></label>'
            '<label>%s<select name="project" required>%s</select></label>'
            '<label>%s<textarea name="text" rows="4" maxlength="5000"></textarea></label>'
            '<div class="sm-form__foot"><span class="sm-faint">%s</span>'
            '<button class="fm-btn fm-btn--sm fm-btn--primary" type="submit"%s>%s</button></div></form>'
            % (esc(panel.token), esc(secrets.token_urlsafe(12)), esc(tr("Title")), esc(tr("Project")),
               opts or '<option value="">%s</option>' % esc(tr("no projects")), esc(tr("Notes")),
               esc(tr("Goes to the queue and notifies firstmate.")), "" if opts else " disabled", esc(tr("Queue it"))))
    ship = block_ship(snap)
    page = translate_template(asset("shipmate.html"))
    keys = ("board", "today", "firstmate", "accounts", "strip", "sumline", "usage", "counts")
    for k, v in [("{{CSS}}", asset("shipmate.css")), ("{{NONCE}}", nonce), ("{{TIME}}", esc(b["time"])),
                 ("{{FORM}}", form), ("{{NOTICE}}", notice), ("{{REFRESH_MS}}", str(REFRESH_SECONDS * 1000)),
                 ("{{SHIP}}", ship["ship"]), ("{{SHIPCOUNTS}}", esc(ship["counts"])),
                 ("{{SHIPPROJECTS}}", ship["projects"]), ("{{SHIPSTAGES}}", ship["stages"]), ("{{SHIPLOG}}", ship["log"])] + \
            [("{{%s}}" % k.upper(), b[k]) for k in keys]:
        page = page.replace(k, v)
    return page, nonce


def notice_html(q):
    if "added" in q:
        tid = q["added"][0]
        if q.get("notified", [""])[0] == "yes":
            return '<div class="sm-notice sm-notice--ok">%s</div>' % esc(tr("Task %s is queued and firstmate was notified.") % tid)
        return ('<div class="sm-notice sm-notice--warn">%s</div>'
                % esc(tr("Task %s is queued, but notifying firstmate failed; tell firstmate another way.") % tid))
    if "error" in q:
        return '<div class="sm-notice sm-notice--err">%s</div>' % esc(tr("Not queued: %s") % q["error"][0][:300])
    return ""


# ---------------------------------------------------------------- the ship (ship view)
#
# The same snapshot as the board, drawn as one ship seen from the side and cut away. Each live
# agent is a hand whose place on board is its phase; firstmate is at the helm; the quota is the
# barrels in the hold and firstmate's context the bilge water; the queue is boats moored at the
# pier and delivered work crates on it. ship_scene() derives what to show from the snapshot alone,
# ship_layout() places it, render_ship() draws it as pixel art in SVG.

PLACES = ["nest", "deck", "bow", "rail", "plank"]          # a ship agent's place, by ship phase index
SCOUT_PLACES = ["out", "back", "dock"]                     # a scout's boat, by scout phase index
PLACE_TEXT = {
    "nest": "in the crow's nest with a spyglass",
    "deck": "on deck with a hammer",
    "bow": "at the bow, signalling with flags",
    "rail": "fishing at the rail",
    "plank": "on the gangplank with a pack, waiting to go ashore",
    "hatch": "on deck, phase unknown",
    "out": "rowing out to sea",
    "back": "rowing back with the map",
    "dock": "moored at the pier with the map",
}
PLACE_POSE = {"nest": "spy", "deck": "hammer", "bow": "signal", "rail": "fish", "plank": "pack", "hatch": "stand"}
CAPTAIN_STATES = {"parked", "blocked", "failed"}   # fm-crew-state.sh reports a needs-decision crew as parked
SEA_LOAD = (0.6, 1.0)                              # load per CPU where the sea turns choppy, then rough
OFF_DUTY_AFTER = 20 * 60                           # seconds without activity before an idle hand rests
OFF_DUTY_PLACES = {"nest", "deck", "bow", "hatch"}  # waiting for CI or to go ashore is the job itself
DAY_HOURS = (6, 18)                                # local hours of daylight in the picture
MAX_QUEUE_BOATS = 8
MAX_CRATES = 12
NEST_SEATS = 6
# strong project colours, none of them the red that marks a red flag
PROJECT_COLORS = ["#16a4a4", "#9150dc", "#f2a114", "#2f78d8", "#3fa34d", "#e0569b", "#a0713a", "#5a6f88"]


def captain_reasons(a):
    """Why a hand raises the red flag: its crew state waits on the captain, or a decision is open."""
    why = []
    if a.get("crew") in CAPTAIN_STATES:
        why.append(tr("crew is %s") % tr(a["crew"]))
    for d in a.get("decisions") or []:
        why.append("%s%s: %s" % (d["state"], (" " + d["key"]) if d.get("key") else "", d["text"][:120]))
    return why


def wait_text(since, now, exact=True):
    """How long something has waited, short enough for a name tag: 50m, 3h 05m, 2d 4h; a wait
    known only to the day reads today, 1d, 2d."""
    if since is None:
        return "?"
    if not exact:
        days = (dt.date.fromtimestamp(now) - dt.date.fromtimestamp(since)).days
        return tr("today") if days <= 0 else "%dd" % days
    sec = max(0, int(now - since))
    if sec < 60:
        return "<1m"
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh %02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd %dh" % (sec // 86400, sec % 86400 // 3600)


def captain_line(hands, queue):
    """Everyone waiting on the captain, the longest wait first: each flagged hand or scout and each
    queued task held for the captain's decision. A wait not known goes last, and equal waits keep id
    order, so the line holds still across refreshes."""
    line = [x for x in hands if x["flag"]] + [q for q in queue if q["flag"]]
    line.sort(key=lambda x: (x["since"] is None, x["since"] or 0, x["id"]))
    return line


def runs_out_early(ln):
    return bool(ln.get("runway") == "projected_exhaustion" and ln.get("exhausts")
                and (not ln.get("resets") or ln["exhausts"] < ln["resets"]))


def sea_state(machine):
    if not machine or machine.get("load") is None:
        return "calm"
    per = machine["load"] / max(1, machine.get("cpus") or 1)
    return "calm" if per < SEA_LOAD[0] else "choppy" if per < SEA_LOAD[1] else "rough"


def is_night(now):
    h = dt.datetime.fromtimestamp(now).hour
    return not (DAY_HOURS[0] <= h < DAY_HOURS[1])


def off_duty(a, place, flagged, now):
    """An idle hand rests: paused, or quiet for a while without a busy session. A flagged hand
    never rests (it waits on the captain), nor one whose phase is itself a wait."""
    if flagged or place not in OFF_DUTY_PLACES:
        return False
    if a.get("crew") == "paused":
        return True
    last = a.get("last_activity")
    return bool(last and now - last > OFF_DUTY_AFTER and a.get("crew") != "working")


def project_colors(s):
    """A colour per project, in the order of data/projects.md so it stays put across refreshes."""
    names = list(s.get("projects") or [])
    for a in s.get("agents") or []:
        p = a.get("project") or ""
        if p and p not in names:
            names.append(p)
    return {p: PROJECT_COLORS[i % len(PROJECT_COLORS)] for i, p in enumerate(names)}


def ship_scene(s):
    """What the ship shows, derived from the snapshot alone; render_ship draws it."""
    now, colors = s["now"], project_colors(s)
    hands, scouts = [], []
    for a in s["agents"]:
        why = captain_reasons(a)
        phases = a.get("phases") or []
        ph = a.get("phase")
        scout = a.get("kind") == "scout"
        places = SCOUT_PLACES if scout else PLACES
        place = places[ph] if ph is not None and ph < len(places) else ("out" if scout else "hatch")
        h = {"id": a["id"], "title": a.get("title") or "", "crew": a.get("crew") or "unknown",
             "flag": bool(why), "why": why, "project": a.get("project") or "", "place": place,
             "color": colors.get(a.get("project") or "", "#8a8a8a"), "kind": "scout" if scout else "hand",
             "phase": tr(phases[ph] if ph is not None and ph < len(phases) else "unknown")}
        h["off"] = off_duty(a, place, h["flag"], now)
        # a flagged hand waits since its oldest open decision line; flagged by its crew state
        # alone, since it went quiet
        ats = [d["at"] for d in a.get("decisions") or [] if d.get("at")]
        h["since"] = (min(ats) if ats else a.get("last_activity")) if h["flag"] else None
        h["exact"] = True
        (scouts if scout else hands).append(h)
    order = {p: i for i, p in enumerate(PLACES + ["hatch"])}
    hands.sort(key=lambda x: (order[x["place"]], x["id"]))
    scouts.sort(key=lambda x: (SCOUT_PLACES.index(x["place"]), x["id"]))
    fm = s.get("firstmate") or {}
    pct, warn = fm.get("percent"), fm.get("warn_at", DEFAULT_CONTEXT_WARN)
    barrels = []
    for ln in lanes_by_urgency(s.get("quota") or {}):
        rem, early = ln.get("remaining"), runs_out_early(ln)
        barrels.append({"label": ln.get("label") or "account", "remaining": rem, "early": early,
                        "empty": early or (rem is not None and rem < 15), "resets": ln.get("resets"),
                        "exhausts": ln.get("exhausts")})
    queue = [{"id": q["id"], "title": q.get("title") or "", "repo": q.get("repo") or "", "kind": "queue",
              "flag": any(r[0] == "decision" for r in q.get("reasons") or []),
              "why": [r[1] for r in q.get("reasons") or []], "hold": q.get("hold_reason") or "",
              "since": q.get("waiting_since"), "exact": q.get("waiting_exact", True)} for q in s.get("queue") or []]
    for q in queue:
        if not q["flag"]:
            q["since"] = None
    crates = [{"id": r.get("id") or "?", "kind": r.get("kind") or "?"} for r in (s.get("delivered") or [])]
    used = {h["project"] for h in hands + scouts}
    for x in hands + scouts + queue:
        x["line"], x["wait"] = 0, None
    line = captain_line(hands + scouts, queue)
    for i, x in enumerate(line):
        x["line"], x["wait"] = i + 1, wait_text(x["since"], now, x["exact"])
        x["tag"] = "%d · %s · %s" % (x["line"], x["id"], x["wait"])
    return {
        "line": line, "captain": {"busy": bool(line), "waiting": len(line), "oldest": line[0] if line else None},
        "hands": hands, "scouts": scouts, "queue": queue, "crates": crates, "barrels": barrels,
        "firstmate": {"context": pct, "warn_at": warn, "over": pct is not None and pct >= warn,
                      "tokens": fm.get("context"), "limit": fm.get("limit"), "known": bool(fm)},
        "colors": {p: c for p, c in colors.items() if p in used},
        "sea": sea_state(s.get("machine")), "night": is_night(now),
        "flags": sum(h["flag"] for h in hands + scouts) + sum(q["flag"] for q in queue),
        "off": sum(h["off"] for h in hands),
    }


def ship_counts(sc):
    n = len(sc["hands"]) + len(sc["scouts"])
    parts = [tr("%d hand aboard" if n == 1 else "%d hands aboard") % n]
    if sc["flags"]:
        parts.append(tr("%d red flag" if sc["flags"] == 1 else "%d red flags") % sc["flags"])
    if sc["off"]:
        parts.append(tr("%d off duty") % sc["off"])
    parts.append(tr("%d boat moored" if len(sc["queue"]) == 1 else "%d boats moored") % len(sc["queue"]))
    return " · ".join(parts)


# -- layout, in art pixels: the ship grows with its crew so nobody stands on anybody

SHIP_H, HOR, FAR, NEAR, WL, PIER = 340, 112, 176, 196, 262, 300
CABIN_X = 70          # the captain's cabin starts this far into the stern castle, after its windows
CABIN_DESK = 44       # the desk and the captain take this much of the cabin before the line
CABIN_STEP = 17       # between hands in line in the cabin
CABIN_MIN = 2         # the cabin always has room for this many, so it does not jump at the first request
CABIN_DOOR = 22       # the open door onto the main deck
CABIN_TOP, CABIN_FLOOR = 156, 194
ASTERN_STEP = 28      # between queued boats tied astern
MAX_ASTERN = 6        # queued boats tied astern; the log below the scene lists every request


def ship_layout(sc):
    """Where every hand, boat and part of the ship goes. Hands get (x, feet, pose, upto) with x
    the sprite's left edge and feet its lowest painted row; nothing here depends on the clock."""
    cabin = [h for h in sc["line"] if h["kind"] != "queue"]
    astern = [q for q in sc["line"] if q["kind"] == "queue"][:MAX_ASTERN]
    by = {k: [h for h in sc["hands"] if h["place"] == k and not h["line"]] for k in PLACES + ["hatch"]}
    n = {k: len(v) for k, v in by.items()}
    castle = CABIN_X + CABIN_DESK + CABIN_STEP * max(CABIN_MIN, len(cabin)) + CABIN_DOOR
    deck_zone = max(110, 48 * ((n["deck"] + 1) // 2) + 30, 14 * len(sc["barrels"]) + 100)
    hatch_zone = 60 + 26 * max(0, n["hatch"] - 1)
    rail_zone = max(40, 26 * n["rail"] + 16)
    deck_len = max(260, deck_zone + hatch_zone + rail_zone)
    fc_len = max(104, 46 * n["bow"] + 18)
    x0 = 70 + 22 * max(0, n["plank"] - 3) + (ASTERN_STEP * len(astern) + 14 if astern else 0)
    length = castle + deck_len + fc_len
    deck_x, fc_x = x0 + castle, x0 + castle + deck_len
    hatch_x = deck_x + deck_zone + 4
    rail_x = deck_x + deck_zone + hatch_zone
    main_x = deck_x + deck_zone - 6
    nest_w = max(54, 22 * min(n["nest"], NEST_SEATS) + 10)
    out = {"x0": x0, "length": length, "castle": castle, "cabin": (x0 + CABIN_X, x0 + castle - 4), "deck_x": deck_x, "deck_len": deck_len, "fc_x": fc_x, "fc_len": fc_len,
           "hatch_x": hatch_x, "rail_x": rail_x, "main_x": main_x, "nest": (main_x + 2 - nest_w // 2, nest_w),
           "hold": (x0 + 98, deck_x + deck_zone + hatch_zone - 8), "hands": {}}
    put = out["hands"]
    ladder = ((main_x - 38, FAR), (main_x - 8, 46))
    for i, h in enumerate(by["nest"]):
        if i < NEST_SEATS:
            put[h["id"]] = (out["nest"][0] + 6 + 22 * i, 43, "spy", 9)
        elif i < NEST_SEATS + 3:                       # more lookouts than seats climb the ladder
            t = (0.3, 0.55, 0.8)[i - NEST_SEATS]
            put[h["id"]] = (int(ladder[1][0] + (ladder[0][0] - ladder[1][0]) * t) - 4,
                            int(ladder[1][1] + (ladder[0][1] - ladder[1][1]) * t), "spy", None)
        else:
            put[h["id"]] = (main_x + 14 + 22 * (i - NEST_SEATS - 3), FAR + 6, "spy", None)
    for i, h in enumerate(by["deck"]):
        put[h["id"]] = (deck_x + 12 + 48 * (i // 2) + 24 * (i % 2), 194 if i % 2 == 0 else 183, "hammer", None)
    for i, h in enumerate(by["hatch"]):
        put[h["id"]] = (hatch_x + 38 + 26 * i, 188, "stand", None)
    for i, h in enumerate(by["rail"]):
        put[h["id"]] = (rail_x + 4 + 26 * i, NEAR + 2, "fish", None)
    for i, h in enumerate(by["bow"]):
        put[h["id"]] = (fc_x + 8 + 46 * i, 166 + 3 * (i % 2), "signal", None)
    plank = ((x0 + 86, NEAR + 2), (x0 + 30, PIER + 4))
    out["plank"] = plank
    for i, h in enumerate(by["plank"]):
        if i < 3:
            t = (0.22, 0.5, 0.78)[i]
            put[h["id"]] = (int(plank[0][0] + (plank[1][0] - plank[0][0]) * t) - 5,
                            int(plank[0][1] + (plank[1][1] - plank[0][1]) * t), "pack", None)
        else:                                           # the rest wait on the pier at the foot of the plank
            put[h["id"]] = (plank[1][0] - 12 - 22 * (i - 3), PIER + 8, "pack", None)
    # the line in the captain's cabin, facing the desk, the longest wait nearest it
    for i, h in enumerate(cabin):
        pose = "scroll" if h["kind"] == "scout" else PLACE_POSE.get(h["place"], "stand")
        put[h["id"]] = (x0 + CABIN_X + CABIN_DESK + CABIN_STEP * i, CABIN_FLOOR - 2, pose + "-l", None)
    out["astern"] = {q["id"]: x0 - 26 - ASTERN_STEP * j for j, q in enumerate(astern)}
    for h in sc["hands"]:
        if h["off"] and h["id"] in put:
            x, feet, _pose, upto = put[h["id"]]
            put[h["id"]] = (x, feet, "rest", upto)
    right = x0 + length + 70
    sb = {k: [h for h in sc["scouts"] if h["place"] == k and not h["line"]] for k in SCOUT_PLACES}
    out["scouts"] = {}
    for i, h in enumerate(sb["out"]):
        out["scouts"][h["id"]] = (right + 30 * i, HOR + 14, "spy", 9)
    for i, h in enumerate(sb["back"]):                  # rowing back: out at sea, clear of the pier
        out["scouts"][h["id"]] = (right + 44 + 30 * i, WL + 16, "scroll", 10)
    for i, h in enumerate(sb["dock"]):
        out["scouts"][h["id"]] = (right + 30 * i, PIER - 2, "stand", 10)
    out["crates_x"] = right + 30 * max(1, len(sb["dock"])) + 10
    widest = max([1] + [len(v) for v in sb.values()] + [len(sb["back"]) + 2])
    out["width"] = max(640, right + 30 * widest + 9 * ((min(len(sc["crates"]), MAX_CRATES) + 2) // 3) + 70)
    out["queue_x"] = x0 + 160
    # the opening frame: the deck and every hand aboard, with a margin; the nest only when manned
    # the opening frame also takes in the captain's cabin and the boats tied astern
    xs = ([x0 + CABIN_X - 10, fc_x + fc_len] + [x for x, _f, _p, _u in put.values()] + [x + 18 for x, _f, _p, _u in put.values()]
          + list(out["astern"].values()))
    ys = ([FAR - 50, NEAR + 20] + [f - 26 for _x, f, _p, _u in put.values()] + [f + 4 for _x, f, _p, _u in put.values()]
          + ([WL + 8] if astern else []))
    out["crew"] = (min(xs) - 12, min(ys) - 8, max(xs) - min(xs) + 24, max(ys) - min(ys) + 16)
    return out


# -- pixel art: palette, sprites, and a canvas that writes one SVG path per colour

INK = {
    "e": "#22180f", "E": "#1d140c", "p": "#34405e", "b": "#2a1d14", "w": "#fbf6ea", "g": "#e8b83a", "T": "#8a6420",
    "G": "#6d7380", "o": "#7a4a24", "O": "#5a3418", "t": "#c9a46a", "r": "#d8342a", "R": "#a32218", "y": "#f6d040",
    "u": "#2f6fd8", "m": "#f2e2b4", "M": "#b89a5e", "n": "#22345e", "N": "#162447", "l": "#e9edf2", "x": "#c23b22",
    "P": "#8a5a30", "k": "#1a1a22",
}
SKINS = ["#f1c9a0", "#d9a173", "#b07a4f", "#8a5a3a", "#f5d6b8"]
HAIRS = ["#2a1d14", "#5a3a1e", "#a0662a", "#d9b45a", "#8d8d8d", "#1d1d2a", "#7a2e1a"]

# A hand is 9 pixels wide and faces right. k bandana and c shirt take the project's colour, w the
# shirt's stripes; the poses add the prop that makes each phase a different silhouette.
STAND = ["...kkk...", ".kkkkkk..", ".khssss..", "..hssEs..", "..hssss..", "...sss...",
         "..ccccc..", ".cwwwwwc.", ".ccccccc.", ".swwwwws.", "..ccccc..", "..pp.pp..", "..pp.pp..", ".bbb.bbb."]


def build_pose(w, h, ox, oy, sets=(), clear=()):
    grid = [["."] * w for _ in range(h)]
    for j, row in enumerate(STAND):
        for i, ch in enumerate(row):
            if ch != ".":
                grid[oy + j][ox + i] = ch
    for x, y in clear:
        grid[oy + y][ox + x] = "."
    for x, y, ch in sets:
        grid[y][x] = ch
    return ["".join(r) for r in grid]


def line_points(x0, y0, x1, y1):
    n = max(abs(x1 - x0), abs(y1 - y0))
    return [(round(x0 + (x1 - x0) * k / float(n)), round(y0 + (y1 - y0) * k / float(n))) for k in range(n + 1)]


ARMS_R = [(7, 7), (7, 8), (7, 9)]
LEGS = [(x, y) for y in (11, 12, 13) for x in range(1, 8)]
POSES = {
    "stand": STAND,
    "spy": build_pose(14, 14, 0, 0, [(7, 6, "c"), (8, 5, "c"), (8, 4, "s")] + [(x, 3, "g") for x in range(6, 12)]
                      + [(12, 3, "T"), (13, 3, "T"), (11, 2, "g"), (12, 2, "T")], clear=ARMS_R),
    "hammer": build_pose(12, 18, 0, 4, [(7, 9, "c"), (8, 8, "c"), (8, 7, "s")] + [(8, y, "o") for y in range(3, 7)]
                         + [(x, y, "G") for x in range(6, 12) for y in (0, 1, 2)], clear=ARMS_R),
    "signal": build_pose(17, 18, 4, 4, [(4, 9, "c"), (3, 8, "c"), (3, 7, "s"), (12, 9, "c"), (13, 8, "c"), (13, 7, "s")]
                         + [(3, y, "o") for y in range(7)] + [(13, y, "o") for y in range(7)]
                         + [(x, y, "y") for x in range(3) for y in range(4)]
                         + [(x, y, "u") for x in range(14, 17) for y in range(4)],
                         clear=[(1, 7), (1, 8), (1, 9)] + ARMS_R),
    "fish": build_pose(18, 14, 0, 0, [(7, 9, "s")] + [(x, y, "o") for x, y in line_points(8, 9, 17, 0)]
                       + [(x, 12, "p") for x in (2, 3, 5, 6)] + [(x, 13, "b") for x in (2, 3, 5, 6)],
                       clear=[(7, 7), (7, 8)] + LEGS),
    "pack": build_pose(12, 14, 3, 0, [(x, y, "P") for x in range(4) for y in range(6, 12)]
                       + [(x, 5, "w") for x in range(4)] + [(4, 7, "O"), (4, 8, "O"), (1, 8, "O"), (2, 8, "O")]
                       + [(5, 11, "p"), (4, 12, "p"), (5, 12, "p"), (9, 11, "p"), (10, 12, "p"), (9, 12, "p")]
                       + [(x, 13, "b") for x in (3, 4, 5, 9, 10, 11)], clear=LEGS),
    "scroll": build_pose(12, 14, 0, 0, [(x, y, "m") for x in range(6, 12) for y in (7, 8, 9)]
                         + [(6, 7, "M"), (11, 7, "M"), (6, 9, "M"), (11, 9, "M"), (8, 8, "x")]),
    # off duty: sitting on a crate, a pair of dice in front
    "rest": build_pose(15, 14, 0, 0, [(x, 11, "p") for x in range(2, 8)] + [(7, 12, "p"), (8, 12, "p"), (8, 13, "b"), (9, 13, "b")]
                       + [(x, y, "O") for x in range(1, 6) for y in (12, 13)]
                       + [(11, 12, "w"), (12, 12, "w"), (11, 13, "w"), (12, 13, "w"), (13, 13, "w"), (14, 13, "w"), (14, 12, "w")],
                       clear=LEGS),
    "helm": ["..NNNNN..", ".NNNNNNN.", "..ggggg..", "..hssss..", "..hssEs..", "..hssss..", "...sss...",
             "..nnnnn..", ".nngnnnn.", ".nnngnnn.", ".snnnnns.", "..nnnnn..", "..pp.pp..", "..pp.pp..", ".bbb.bbb."],
}
# the same poses facing left, for the line in the captain's cabin
POSES.update({k + "-l": [r[::-1] for r in v] for k, v in list(POSES.items())})


def paint(w, h, base, sets=(), clear=()):
    """A w by h sprite from base rows, with some pixels cleared and others set."""
    grid = [["."] * w for _ in range(h)]
    for j, row in enumerate(base):
        for i, ch in enumerate(row):
            if ch != ".":
                grid[j][i] = ch
    for x, y in clear:
        grid[y][x] = "."
    for x, y, ch in sets:
        grid[y][x] = ch
    return ["".join(r) for r in grid]


# The captain: a black tricorn with a gold badge, a grey beard, a red coat with gold buttons.
CAPTAIN_INK = {"k": "#1d1a24", "x": "#a8322a", "g": "#e8b83a", "w": "#fbf6ea", "h": "#8d8d8d", "s": "#d9a173",
               "p": "#2a2a3a", "b": "#2a1d14", "l": "#e9edf2", "m": "#f2e2b4", "M": "#b89a5e", "O": "#6b3a1c"}
CAPTAIN = ["...kkkkk...", "..kkkkkkk..", "kkkkkgkkkkk", "...hssss...", "...hssEs...", "...hssss...", "...hhhhh...",
           "..xxxwxxx..", ".xxxxwgxxx.", ".xxxxwxxxx.", ".sxxxwgxxs.", "..xxxxxxx..", "..xxx.xxx..",
           "...pp.pp...", "...pp.pp...", "..bbb.bbbb."]
# attending: a paper held up in the right hand
CAPTAIN_BUSY = paint(14, 16, CAPTAIN, [(9, 9, "x"), (10, 8, "s")] + [(x, y, "m") for x in (11, 12, 13) for y in (5, 6, 7, 8)]
                     + [(12, 6, "M"), (12, 7, "M")], clear=[(9, 10)])
# off duty: sitting on a stool, a mug in hand
CAPTAIN_OFF = paint(14, 15, CAPTAIN[:12],
                    [(8, 9, "s"), (9, 9, "s"), (10, 8, "l"), (11, 8, "l"), (10, 9, "l"), (11, 9, "l"), (12, 9, "l"),
                     (10, 10, "l"), (11, 10, "l")]
                    + [(x, 11, "p") for x in range(3, 10)] + [(8, 12, "p"), (9, 12, "p"), (8, 13, "p"), (9, 13, "p"),
                                                              (8, 14, "b"), (9, 14, "b"), (10, 14, "b")]
                    + [(x, 12, "O") for x in range(1, 7)] + [(2, 13, "O"), (5, 13, "O"), (2, 14, "O"), (5, 14, "O")],
                    clear=[(9, 10), (1, 10)])
QMARK = [".www.", "w...w", "...w.", "..w..", ".....", "..w.."]
ZZZ = ["www...", "..w...", ".w.www", "www..w", "....w.", "...www"]
WHEEL = ["..o.o..", ".ooooo.", "oo.O.oo", ".oOOOo.", "oo.O.oo", ".ooooo.", "..o.o.."]
BARREL = ["..OOOOO..", ".OoooooO.", "OGGGGGGGO", "OoooooooO", "OoooooooO", "OoooooooO", "OGGGGGGGO",
          "OoooooooO", "OoooooooO", "OGGGGGGGO", ".OoooooO.", "..OOOOO.."]
CRATE = ["OOOOOOOO", "OoMMMMoO", "OMoMMoMO", "OMMooMMO", "OMMooMMO", "OMoMMoMO", "OoMMMMoO", "OOOOOOOO"]
BOAT = ["O..............O", "OoooooooooooooOO", ".OooooooooooooO.", "..OOOOOOOOOOOO.."]
MAP = ["MmmmmmmmM", "Mmmxmmmm.", "Mmmmmxmm.", "MmxmmmmmM"]
CRATE_INK = {"O": "#5a3418", "o": "#a5743f", "M": "#c99a5c"}
BARREL_INK = {"O": "#3e220f", "o": "#8a5530", "G": "#5a5f6a"}


def pick(seq, key):
    """A stable choice from seq for key (str), so a hand keeps its looks across refreshes."""
    h = 0
    for ch in key:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return seq[h % len(seq)]


def mix(c, other, t):
    a = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def lit(c, t=0.22):
    return mix(c, "#fff4d6", t)


def dark(c, t=0.32):
    return mix(c, "#1a1030", t)


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
        for x, y in line_points(x0, y0, x1, y1) if (x0, y0) != (x1, y1) else [(x0, y0)]:
            self.px[(x, y)] = c

    def sprite(self, x, y, rows, pal):
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                c = pal.get(ch) or INK.get(ch)
                if ch != "." and c:
                    self.px[(x + i, y + j)] = c

    def shaded(self, x, y, rows, pal, outline="#24160c"):
        """A sprite with volume: each material gets a lit edge toward the light (upper left) and a
        shaded edge away from it, and the figure a dark outline."""
        grid = {(i, j): ch for j, row in enumerate(rows) for i, ch in enumerate(row) if ch != "."}
        for (i, j) in grid:
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (i + di, j + dj) not in grid:
                    self.px.setdefault((x + i + di, y + j + dj), outline)
        for (i, j), ch in grid.items():
            base = pal.get(ch) or INK.get(ch)
            if not base:
                continue
            if ch in "Ee":
                c = base
            elif grid.get((i - 1, j - 1)) != ch and grid.get((i, j - 1)) != ch:
                c = lit(base)
            elif grid.get((i + 1, j + 1)) != ch or grid.get((i + 1, j)) != ch:
                c = dark(base)
            else:
                c = base
            self.px[(x + i, y + j)] = c

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


def band(x, y0, w, y1, c, attrs=""):
    return '<rect x="%d" y="%d" width="%d" height="%d" fill="%s"%s/>' % (x, y0, w, y1 - y0, c, attrs)


def polygon(pts, c, attrs=""):
    return '<polygon points="%s" fill="%s"%s/>' % (" ".join("%g,%g" % p for p in pts), c, attrs)


def shadow(cx, cy, w=11, h=3, op=0.28):
    """A soft drop shadow on the deck, a little to the lower right (the light is upper left)."""
    p = Pix()
    for j in range(h):
        k = 1 if j in (0, h - 1) else 0
        p.rect(cx - w // 2 + k + 2, cy + j, w - 2 * k, 1, "#000000")
    return p.svg(' opacity="%.2f"' % op)


def hand_palette(h):
    c = h.get("color") or "#8a8a8a"
    return {"c": c, "k": dark(c, 0.38), "h": pick(HAIRS, h["id"] + "h"), "s": pick(SKINS, h["id"] + "s")}


def red_flag(p, x, y):
    """The red flag a hand raises when it needs the captain: a pole at x with the cloth to its left."""
    p.rect(x, y, 1, 13, INK["o"])
    p.rect(x - 6, y, 6, 4, INK["r"])
    p.rect(x - 6, y, 6, 1, lit(INK["r"]))
    p.rect(x - 6, y + 3, 6, 1, INK["R"])


class Tags:
    """Name tags placed so that none overlaps another: each goes above its hand, or higher with a
    leader line when that spot is taken."""

    def __init__(self):
        self.boxes, self.out = [], []

    def place(self, text, cx, top, owner=""):
        w, h = int(len(text) * 2.75) + 5, 7
        for dy in (0, -8, -16, 8, -24, 16, -32, -40):
            x0, y0 = int(cx - w / 2), top - h - 2 + dy
            box = (x0, y0, x0 + w, y0 + h)
            if all(box[2] <= b[0] or box[0] >= b[2] or box[3] <= b[1] or box[1] >= b[3] for b in self.boxes):
                break
        self.boxes.append(box)
        lead = ('<line class="sm-tag__lead" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (cx, y0 + h, cx, top - 1)) if dy < 0 else ""
        self.out.append('<g class="sm-tag" data-for="%s" data-ax="%d" data-ay="%d">%s<rect x="%d" y="%d" width="%d" height="%d" rx="1.5"/>'
                        '<text x="%d" y="%.1f">%s</text></g>' % (esc(owner), cx, top, lead, x0, y0, w, h, cx, y0 + 5.2, esc(text)))
        return box

    def svg(self):
        return "".join(self.out)


def hand_svg(h, spot, tags, extra=None, shadow_on=True):
    """One clickable hand at its spot, with its flag, its rest marks, and its name tag."""
    x, feet, pose, upto = spot
    rows = POSES[pose][:upto]
    base, left = pose.split("-")[0], pose.endswith("-l")
    top = feet - len(rows) + 1
    p = Pix()
    p.shaded(x, top, rows, hand_palette(h))
    if extra:
        extra(p, top)
    if h["place"] == "hatch":
        p.sprite(x + 2, top - 8, QMARK, {})
    if pose == "rest":
        p.sprite(x + 9, top - 7, ZZZ, {})
    flag = ""
    if h["flag"]:
        f = Pix()
        red_flag(f, x - 2, top - 10)
        flag = f.svg(' class="sm-redflag"')
    off = {"signal": 8, "pack": 7}.get(base, 4)
    cx = x + (len(rows[0]) - 1 - off if left else off)
    tags.place(h.get("tag") or h["id"], cx, top - (10 if h["flag"] else 1) + (4 if base in ("hammer", "signal") else 0), h["id"])
    state = tr("; off duty") if h["off"] else ""
    title = "%s · %s · %s (%s%s)%s" % (h["id"], h["project"] or tr("no project"), tr(PLACE_TEXT[h["place"]]), h["phase"], state,
                                       tr("; needs the captain: ") + "; ".join(h["why"]) if h["flag"] else "")
    if h.get("line"):
        title += tr("; number %d in line for the captain, waiting %s") % (h["line"], h["wait"])
    stage = "scouts" if h.get("kind") == "scout" else h["place"]
    return ('%s<g class="sm-hand pose-%s%s" data-id="%s" data-stage="%s"%s tabindex="0" role="button" aria-label="%s"><title>%s</title>%s%s</g>'
            % (shadow(cx, feet + 1) if shadow_on else "", base, " has-flag" if h["flag"] else "", esc(h["id"]), stage,
               ' data-off="1"' if h["off"] else "", esc(tr("%s: details") % title), esc(title), p.svg(), flag))


def boat_pix(x, wl, cargo=False, flag=False):
    p = Pix()
    p.shaded(x, wl - 3, BOAT, {})
    if cargo:
        p.shaded(x + 5, wl - 8, CRATE[:6], CRATE_INK)
    if flag:
        red_flag(p, x + 13, wl - 16)
    return p


def sail(x0, x1, y0, y1, colors, belly=4):
    """A sail with volume: lit on the luff, shaded toward the leech, seams, a belly, and one
    stripe per project aboard."""
    p = Pix()
    w = x1 - x0
    for y in range(y0, y1):
        t = (y - y0) / float(max(1, y1 - y0 - 1))
        b = int(round(belly * (1 - (2 * t - 1) ** 2)))
        for x in range(x0 - b, x1 + b):
            u = (x - x0 + b) / float(w + 2 * b)
            c = "#fbf3dc" if u < 0.25 else "#f1e5c4" if u < 0.7 else "#ddcda4"
            for i, col in enumerate(colors[:4]):
                if y - y0 in (8 + 5 * i, 9 + 5 * i):
                    c = lit(col, 0.2) if u < 0.25 else col if u < 0.7 else dark(col, 0.2)
            if (y - y0) % 8 == 7:
                c = dark(c, 0.08)
            p.dot(x, y, c)
    p.rect(x0 - 2, y0 - 1, w + 4, 1, "#4a2a12")
    p.rect(x0 - 2, y1, w + 4, 1, "#4a2a12")
    return p.svg()


def sky_and_sea(w, night, sea, rnd):
    far, mid = [], []
    sky = (["#0d1631", "#111c3b", "#152244", "#1a294d", "#203157", "#283a60"] if night else
           ["#6fb3e0", "#78b9e4", "#83c0e8", "#8fc8ec", "#9dd0ee", "#b3dcf2"])
    for i, c in enumerate(sky):
        far.append(band(-300, HOR * i // 6, w + 600, HOR * (i + 1) // 6, c))
    deco = Pix()
    if night:
        for _ in range(w // 6):
            deco.dot(rnd.randrange(-200, w + 200), rnd.randrange(2, HOR - 18), "#fff8d8" if rnd.random() < 0.3 else "#aab6d8")
        for y in range(-7, 8):
            for x in range(-7, 8):
                if x * x + y * y <= 49 and (x - 3) ** 2 + (y + 2) ** 2 > 30:
                    deco.dot(w - 90 + x, 24 + y, "#f6efd0")
    else:
        for y in range(-8, 9):
            for x in range(-8, 9):
                if x * x + y * y <= 64:
                    deco.dot(w - 90 + x, 22 + y, "#fff1b8" if x * x + y * y < 40 else "#ffe58a")
    clouds = Pix()
    for cx, cy in ((40, 14), (250, 30), (470, 10), (690, 26), (910, 16), (1130, 30)):
        if cx > w + 100:
            break
        for dx, dy, ww in ((6, 0, 14), (0, 4, 30), (3, 8, 26)):
            clouds.rect(cx + dx, cy + dy, ww, 4, "#3a4a72" if night else "#fbfdff")
        clouds.rect(cx + 3, cy + 12, 26, 1, "#2c3a5e" if night else "#dfeef7")
    hills = Pix()
    hc = ("#2a3b5c", "#304466", "#22314f") if night else ("#8fb7c9", "#9ec2d2", "#7aa6bb")
    for x in range(-300, w + 300):
        hgt = int(9 + 8 * abs(((x * 7) % 130) - 65) / 65.0 + 4 * ((x // 37) % 2))
        hills.rect(x, HOR - hgt, 1, hgt, hc[0] if x % 210 < 130 else hc[1])
        hgt = int(4 + 4 * abs(((x * 5 + 40) % 100) - 50) / 50.0)
        hills.rect(x, HOR - hgt, 1, hgt, hc[2])
    far += [deco.svg(), '<g class="sm-drift">%s</g>' % clouds.svg(), hills.svg()]
    near_c, far_c = ("#0c2440", "#1d3d64") if night else ("#2c6f9f", "#5aa6cf")
    for i in range(6):
        mid.append(band(-300, HOR + (SHIP_H - HOR) * i // 6, w + 600, HOR + (SHIP_H - HOR) * (i + 1) // 6,
                        mix(far_c, near_c, i / 5.0)))
    caps = Pix()
    count = {"calm": w // 5, "choppy": w // 3, "rough": w // 2}[sea]
    for _ in range(count):
        y = rnd.randrange(HOR + 2, SHIP_H)
        t = (y - HOR) / float(SHIP_H - HOR)
        x = rnd.randrange(-100, w + 100)
        size = 2 + int((4 if sea == "rough" else 3) * t)
        caps.rect(x, y, size, 1, mix("#cfe8f5", "#9bc9e4", t) if not night else mix("#5a7aa8", "#3a5a88", t))
        if t > 0.5 and sea != "calm":
            caps.rect(x + 1, y - 1, max(1, size - 2), 1, "#f6fbfd" if not night else "#8aa6cc")
    mid.append('<g class="sm-waves">%s</g>' % caps.svg())
    return far, mid


def sign(tags, text, cx, y, cls=""):
    """A sign painted on the ship, in the name-tag layer so it stays readable at any zoom; name
    tags placed after it keep clear of it."""
    w = int(len(text) * 2.75) + 8
    x0 = int(cx - w / 2)
    tags.boxes.append((x0, y, x0 + w, y + 8))
    tags.out.append('<g class="sm-tag sm-sign%s" data-ax="%d" data-ay="%d"><rect x="%d" y="%d" width="%d" height="8" rx="1"/>'
                    '<text x="%d" y="%.1f">%s</text></g>' % (cls, cx, y, x0, y, w, cx, y + 5.7, esc(text)))


def balloon(tags, cx, bottom, lines):
    """The captain's speech balloon, pointing down at bottom."""
    w = int(max(len(t) for t in lines) * 2.75) + 8
    h = 6 * len(lines) + 3
    x0, y0 = int(cx - w / 2), bottom - 4 - h
    tags.boxes.append((x0, y0, x0 + w, y0 + h + 4))
    texts = "".join('<text x="%d" y="%.1f">%s</text>' % (cx, y0 + 5.6 + 6 * i, esc(t)) for i, t in enumerate(lines))
    tags.out.append('<g class="sm-tag sm-balloon" data-ax="%d" data-ay="%d"><rect x="%d" y="%d" width="%d" height="%d" rx="2"/>'
                    '<polygon points="%d,%.1f %d,%.1f %d,%d"/>%s</g>'
                    % (cx, bottom, x0, y0, w, h, cx - 2, y0 + h - 0.5, cx + 2, y0 + h - 0.5, cx, bottom, texts))


def line_number(n, x, y):
    """A hand's or boat's place in the captain's line: a red disc with its number."""
    return ('<g class="sm-num"><rect x="%d" y="%d" width="7" height="7" rx="3.5"/><text x="%.1f" y="%.1f">%d</text></g>'
            % (x, y, x + 3.5, y + 5.3, n))


def captain_title(sc):
    cap = sc["captain"]
    if not cap["busy"]:
        return tr("the captain, off duty: nothing waits on a decision")
    o = cap["oldest"]
    return tr("the captain: %d waiting, the longest %s for %s") % (cap["waiting"], o["id"], o["wait"])


def draw_cabin(sc, lay, tags):
    """The captain's cabin, cut open in the stern castle: the desk and the captain, attending the
    line or off duty with a mug, and the line of hands that wait on a decision. Returns the cabin
    and, apart, the line's numbers, which go over the hull's edge at the hands' feet."""
    cx0, cx1 = lay["cabin"]
    top, floor = CABIN_TOP, CABIN_FLOOR
    p = Pix()
    p.rect(cx0 - 2, top - 2, cx1 - cx0 + 4, floor - top + 4, "#e0b552")
    for y in range(top, floor):
        p.rect(cx0, y, cx1 - cx0, 1, mix("#3a2414", "#5a361c", (y - top) / float(floor - top)))
    for x in range(cx0 + 12, cx1, 14):
        p.rect(x, top, 1, floor - top - 4, "#2a170b")
    p.rect(cx0, floor - 3, cx1 - cx0, 3, "#9a6233")
    p.rect(cx0, floor - 3, cx1 - cx0, 1, "#c08a55")
    p.rect(cx1 - 9, top + 10, 9, floor - top - 13, "#140b05")          # the door onto the main deck, open
    p.rect(cx1 - 10, top + 9, 1, floor - top - 12, "#e0b552")
    p.rect(cx0 + 20, top, 1, 4, "#2a170b")                              # a lantern
    p.rect(cx0 + 18, top + 4, 5, 5, "#f2c35a")
    p.rect(cx0 + 19, top + 5, 2, 2, "#fff0b0")
    p.shaded(cx0 + 4, top + 8, MAP, {})                                 # a chart on the wall
    p.rect(cx0 + 2, floor - 12, 22, 2, "#8a5530")                       # the desk, a paper and a quill
    p.rect(cx0 + 2, floor - 12, 22, 1, "#b07a45")
    p.rect(cx0 + 4, floor - 10, 2, 7, "#5a3418")
    p.rect(cx0 + 20, floor - 10, 2, 7, "#5a3418")
    p.rect(cx0 + 6, floor - 14, 7, 2, "#f2e2b4")
    p.rect(cx0 + 15, floor - 17, 1, 5, "#fbf6ea")
    out = [p.svg(), '<circle cx="%d" cy="%d" r="10" fill="#ffd77a" opacity="0.18"/>' % (cx0 + 20, top + 7)]
    busy = sc["captain"]["busy"]
    rows = CAPTAIN_BUSY if busy else CAPTAIN_OFF
    x, feet = cx0 + (24 if busy else 22), floor - 2
    ctop = feet - len(rows) + 1
    cp = Pix()
    cp.shaded(x, ctop, rows, CAPTAIN_INK)
    steam = ""
    if not busy:
        st = Pix()
        for dx, dy in ((11, 6), (10, 5), (11, 4)):
            st.dot(x + dx, ctop + dy, "#ffffff")
        steam = st.svg(' class="sm-steam" opacity="0.8"')
    out.append('%s<g class="sm-captain%s"><title>%s</title>%s%s</g>'
               % (shadow(x + 6, feet + 1, 14), " is-busy" if busy else " is-off", esc(captain_title(sc)), cp.svg(), steam))
    if busy:
        o = sc["captain"]["oldest"]
        n = sc["captain"]["waiting"]
        balloon(tags, x + 7, ctop - 1, [tr("%d waiting for you") % n, tr("longest: %s, %s") % (o["id"], o["wait"])])
    numbers = []
    for h in sc["line"]:
        if h["kind"] != "queue" and h["id"] in lay["hands"]:
            hx, hfeet, _pose, _upto = lay["hands"][h["id"]]
            out.append(hand_svg(h, lay["hands"][h["id"]], tags))
            numbers.append(line_number(h["line"], hx - 1, hfeet + 1))
    return "".join(out), "".join(numbers)


def queue_boat(q, x, wl, tags=None, tag_top=None):
    """A queued task's boat, red-flagged when it waits on the captain."""
    p = boat_pix(x, wl, cargo=True, flag=q["flag"])
    tip = tr("%s: queued (%s)%s%s") % (q["id"], ", ".join(q["why"]), (" · " + q["title"]) if q["title"] else "",
                                       tr("; number %d in line for the captain, waiting %s") % (q["line"], q["wait"]) if q["line"] else "")
    if tags is not None:
        tags.place(q.get("tag") or q["id"], x + 8, tag_top, q["id"])
    return ('<g class="sm-boat" data-id="%s"%s tabindex="0" role="button" aria-label="%s"><title>%s</title>%s</g>'
            % (esc(q["id"]), ' data-flag="1"' if q["flag"] else "", esc(tr("%s: details") % q["id"]), esc(tip), p.svg()))


def draw_astern(sc, lay, tags):
    """Queued tasks held for the captain's decision: boats tied astern under the cabin windows."""
    out = []
    by_id = {q["id"]: q for q in sc["queue"]}
    for j, (qid, bx) in enumerate(lay["astern"].items()):
        q = by_id[qid]
        rope = Pix()
        rope.line(bx + 15, WL - 4, lay["x0"] + 1, WL - 22 - 3 * j, "#e9dcb8")
        out += [rope.svg(), queue_boat(q, bx, WL, tags, WL - 17), line_number(q["line"], bx + 4, WL + 2)]
    return "".join(out)


def paint_signs(sc, lay, tags):
    """The legend painted on the ship: what each place means, where it is."""
    x0 = lay["x0"]
    nx, nw = lay["nest"]
    sign(tags, tr("LOOKOUT · exploring"), nx + nw // 2, 51)
    sign(tags, tr("HELM · firstmate"), x0 + 37, 174)
    cx0 = lay["cabin"][0]
    sign(tags, tr("CAPTAIN'S CABIN"), cx0 + 18, NEAR + 2, " sm-sign--red")       # under the desk, clear of the line
    sign(tags, tr("DECK · implementing"), lay["deck_x"] + 40, NEAR + 2)
    if any(h["place"] == "hatch" and not h["line"] for h in sc["hands"]):
        sign(tags, tr("? · phase unknown"), lay["hatch_x"] + 12, NEAR + 2)
    sign(tags, tr("RAIL · waiting for CI"), lay["rail_x"] + 30, NEAR + 2)
    sign(tags, tr("BOW · PR open"), lay["fc_x"] + lay["fc_len"] // 2, NEAR + 2)
    hx0, hx1 = lay["hold"]
    sign(tags, tr("HOLD · quota barrels, context water"), (hx0 + hx1) // 2, NEAR + 16)
    sign(tags, tr("GANGPLANK · ready to merge"), x0 + 48, PIER + 4)
    if lay["astern"]:
        xs = list(lay["astern"].values())
        sign(tags, tr("ASTERN · queued, waiting for you"), (min(xs) + max(xs) + 16) // 2, WL + 11, " sm-sign--red")
    pier = len(sc["queue"]) - len(lay["astern"])
    if pier:
        sign(tags, tr("QUEUE"), lay["queue_x"] + 13 * min(pier, MAX_QUEUE_BOATS), PIER + 10)
    if lay["scouts"]:
        sign(tags, tr("SCOUTS"), min(v[0] for v in lay["scouts"].values()) + 20, HOR - 4)
    sign(tags, tr("DELIVERED · %d days") % DAYS, lay["crates_x"] + 14, PIER + 10)


def render_ship(sc):
    """The ship as one SVG in art pixels; the page frames, zooms and pans it."""
    lay = ship_layout(sc)
    W, x0, L = lay["width"], lay["x0"], lay["length"]
    night = sc["night"]
    rnd = random.Random(21)                     # the same stars, clouds and waves on every refresh
    tags = Tags()
    paint_signs(sc, lay, tags)
    colors = list(sc["colors"].values())
    far, mid = sky_and_sea(W, night, sc["sea"], rnd)
    ship, ashore, near, lights = [], [], [], []
    spots = lay["hands"]
    by_id = {h["id"]: h for h in sc["hands"]}

    def hands_at(place):
        return [h for h in sc["hands"] if h["place"] == place and not h["line"]]

    # scouts out on the horizon, behind the ship
    for h in sc["scouts"]:
        if h["line"]:
            continue                                   # waiting in the captain's cabin
        x, wl, pose, upto = lay["scouts"][h["id"]]
        if h["place"] == "out":
            mid.append(hand_svg(h, (x + 3, wl - 3, pose, upto), tags, shadow_on=False) + boat_pix(x, wl).svg())
    mid.append(polygon([(x0 + 10, WL), (x0 + L - 60, WL), (x0 + L - 80, WL + 12), (x0 + 20, WL + 12)],
                       "#0a2238" if night else "#1d4f73", ' opacity="0.45"'))
    # masts, stays and sails
    deck_x, fc_x, main_x = lay["deck_x"], lay["fc_x"], lay["main_x"]
    masts = [(x0 + 60, 36, (x0 + 24, x0 + 100, 50, 124)), (main_x, 6, (main_x - 54, main_x + 56, 56, 150)),
             (fc_x + lay["fc_len"] // 2 - 6, 26, (fc_x + lay["fc_len"] // 2 - 54, fc_x + lay["fc_len"] // 2 + 44, 50, 136))]
    for mx, top, (sx0, sx1, sy0, sy1) in masts:
        stays = Pix()
        stays.line(mx + 2, top + 4, mx - 46, FAR - 2, "#2e1c0e")
        stays.line(mx + 2, top + 4, mx + 46, FAR - 2, "#2e1c0e")
        m = Pix()
        for x in range(4):
            m.rect(mx + x, top, 1, NEAR - top, ("#a06a32", "#7a4a24", "#5a3418", "#3e220f")[x])
        m.rect(mx - 4, top - 2, 10, 2, "#5a3418")
        pen = Pix()
        pen.rect(mx - 12, top - 1, 12, 4, "#c0452a")
        pen.rect(mx - 12, top + 3, 12, 1, dark("#c0452a"))
        ship += [stays.svg(), sail(sx0, sx1, sy0, sy1, colors), m.svg(), pen.svg(' class="sm-pennant"')]
    bs = Pix()
    bs.line(x0 + L - 4, 160, x0 + L + 56, 140, "#5a3418")
    bs.line(x0 + L - 4, 161, x0 + L + 56, 141, "#7a4a24")
    bs.line(masts[2][0] + 2, masts[2][1] + 4, x0 + L + 54, 140, "#2e1c0e")
    lad = Pix()
    for k in range(2):
        lad.line(main_x - 38 + 7 * k, FAR, main_x - 8 + 4 * k, 46, "#2e1c0e")
    for y in range(52, FAR - 2, 7):
        t = (y - 46) / float(FAR - 46)
        lad.rect(int(main_x - 8 + (main_x - 38 - main_x + 8) * t), y, 8, 1, "#2e1c0e")
    ship += [bs.svg(), lad.svg()]
    # the crow's nest and its lookouts
    nx, nw = lay["nest"]
    nest = Pix()
    for y in range(40, 50):
        nest.rect(nx, y, nw, 1, mix("#8a5530", "#4e2a12", (y - 40) / 9.0))
    nest.rect(nx, 40, nw, 1, "#e0b552")
    for h in hands_at("nest"):
        ship.append(hand_svg(h, spots[h["id"]], tags, shadow_on=False))
    ship.append(nest.svg())
    # the main deck in light perspective, then the castles
    dlen = lay["deck_len"]
    deck = Pix()
    for y in range(FAR, NEAR + 1):
        deck.rect(deck_x, y, dlen, 1, mix("#a87a48", "#d6a86e", (y - FAR) / float(NEAR - FAR)))
    for k in range(1, 6):
        deck.rect(deck_x, FAR + int((NEAR - FAR) * (k / 6.0) ** 1.4), dlen, 1, "#9a6c3c")
    vp = (deck_x + dlen // 2, 60)
    for xn in range(deck_x - 200, deck_x + dlen + 200, 24):
        for y in range(FAR, NEAR + 1):
            x = int(vp[0] + (xn - vp[0]) * (y - vp[1]) / float(NEAR - vp[1]))
            if deck_x <= x < deck_x + dlen and (y // 3 + xn // 24) % 3 == 0:
                deck.dot(x, y, "#8e6234")
    for y in range(FAR - 8, FAR):
        deck.rect(deck_x, y, dlen, 1, mix("#7a4524", "#9a6233", (y - FAR + 8) / 8.0))
    deck.rect(deck_x, FAR - 9, dlen, 1, "#e0b552")
    for x in range(deck_x + 6, deck_x + dlen, 20):
        deck.rect(x, FAR - 8, 2, 8, "#5a3418")
    ship.append(deck.svg())
    ship.append(polygon([(main_x + 3, FAR + 4), (main_x + 7, FAR + 4), (main_x + 46, NEAR), (main_x + 38, NEAR)],
                        "#000", ' opacity="0.2"'))
    hx = lay["hatch_x"]
    ship.append(polygon([(hx, 182), (hx + 28, 182), (hx + 31, 192), (hx - 3, 192)], "#6b3a1c"))
    for k in range(6):
        ship.append(polygon([(hx + 1 + 4.6 * k, 183), (hx + 3 + 4.6 * k, 183), (hx + 2 + 5.4 * k, 191),
                             (hx - 1 + 5.4 * k, 191)], "#3a2214"))

    def castle(cx0, cx1, top):
        s = polygon([(cx0, top), (cx1, top), (cx1 - 2, top + 10), (cx0, top + 10)], "#c79a62")
        s += band(cx0, top - 1, cx1 - cx0, top, "#e0b552")
        fc = Pix()
        for y in range(top + 10, NEAR):
            fc.rect(cx0, y, cx1 - cx0 - 2, 1, mix("#8a5530", "#6b3a1c", (y - top - 10) / float(max(1, NEAR - top - 10))))
        fc.rect(cx0, top + 10, cx1 - cx0 - 2, 1, "#e0b552")
        return s + fc.svg()
    ship.append(castle(x0, x0 + lay["castle"], 144))
    ship.append(castle(fc_x, x0 + L - 8, 158))
    win = Pix()
    for wx in (x0 + 8, x0 + 24, x0 + 40, x0 + 56):
        win.rect(wx, 162, 9, 9, "#3a2214")
        win.rect(wx + 1, 163, 7, 7, "#f2c35a")
        win.rect(wx + 1, 163, 3, 3, "#fff0b0")
        if night:
            lights.append('<circle cx="%d" cy="166" r="9" fill="#ffd77a" opacity="0.22"/>' % (wx + 4))
    ship.append(win.svg())
    cabin, numbers = draw_cabin(sc, lay, tags)
    ship.append(cabin)
    # people on deck, far to near; firstmate at the helm on the stern castle
    people = []
    fm = Pix()
    fm.shaded(x0 + 30, 152 - 14, POSES["helm"], {"h": "#2a1d14", "s": "#d9a173"})
    fm.shaded(x0 + 39, 152 - 10, WHEEL, {})
    f = sc["firstmate"]
    ctx = (tr("context %d%% of the window%s") % (round(f["context"]), tr(", past the %d%% warning") % f["warn_at"] if f["over"] else "")
           if f["context"] is not None else tr("context unknown"))
    people.append((152, shadow(x0 + 37, 153, 16) + '<g class="sm-fm"><title>%s</title>%s</g>'
                   % (esc(tr("firstmate at the helm: %s") % ctx), fm.svg())))
    for place in ("deck", "hatch", "bow"):
        for h in hands_at(place):
            people.append((spots[h["id"]][1], hand_svg(h, spots[h["id"]], tags)))
    for h in hands_at("nest"):
        if spots[h["id"]][3] is None:                  # lookouts without a seat in the nest
            people.append((spots[h["id"]][1], hand_svg(h, spots[h["id"]], tags, shadow_on=False)))
    ship += [s for _, s in sorted(people, key=lambda t: t[0])]
    # the hull, cut open: barrels (quota) and the chart table on the orlop, bilge water (context) below
    hull = Pix()
    for y in range(NEAR, WL):
        d = y - NEAR
        left = x0 + max(0, d - 34) // 2
        right = x0 + L - int(((max(0, d) / float(WL - NEAR)) ** 1.7) * 70)
        c = mix("#8a5530", "#3e200d", d / float(WL - NEAR))
        hull.rect(left, y, right - left, 1, dark(c, 0.25) if d % 7 == 6 else c)
    hull.rect(x0, NEAR, L, 1, "#e9c06a")
    hull.rect(x0, NEAR + 1, L, 3, "#9a6233")
    hull.rect(x0, NEAR + 6, L - 2, 3, "#22345e")
    ship.append(hull.svg())
    ship.append(numbers)
    hx0, hx1 = lay["hold"]
    hy0, hy1 = NEAR + 14, WL - 6
    hold = Pix()
    hold.rect(hx0 - 2, hy0 - 2, hx1 - hx0 + 4, hy1 - hy0 + 4, "#e0b552")
    for y in range(hy0, hy1):
        hold.rect(hx0, y, hx1 - hx0, 1, mix("#1c1009", "#3a2414", (y - hy0) / float(hy1 - hy0)))
    orlop = hy0 + 22
    room = hy1 - orlop - 2
    lvl = int(room * min(100.0, f["context"] or 0) / 100.0)
    thr = int(room * f["warn_at"] / 100.0)
    for y in range(hy1 - lvl, hy1):
        hold.rect(hx0, y, hx1 - hx0, 1, "#7fb6d8" if y == hy1 - lvl else mix("#3d82ad", "#1f5579", (y - hy1 + lvl) / float(max(1, lvl))))
    hold.rect(hx0, hy1 - thr, hx1 - hx0, 1, "#f6f1e3")
    hold.rect(hx0, orlop, hx1 - hx0, 3, "#6b3a1c")
    hold.rect(hx0, orlop, hx1 - hx0, 1, "#9a6233")
    for x in range(hx0 + 40, hx1, 46):
        hold.rect(x, hy0, 3, hy1 - hy0, "#2a170b")
    barrels = []
    for i, b in enumerate(sc["barrels"]):
        bx = hx0 + 8 + 14 * i
        bp = Pix()
        bp.shaded(bx, orlop - 13, BARREL, BARREL_INK)
        level = int(round(max(0, min(100, b["remaining"] or 0)) / 100.0 * 7))
        col = "#d8342a" if b["empty"] else "#e0a52e"
        if b["empty"]:
            level = max(1, level)
            for row in (2, 6, 9):
                bp.rect(bx + 1, orlop - 13 + row, 8, 1, "#d8342a")
        bp.rect(bx + 3, orlop - 3 - level, 4, level, col)
        bp.rect(bx + 3, orlop - 3 - level, 1, level, lit(col))
        tip = "%s: %s%s" % (b["label"], (tr("%d%% left") % b["remaining"]) if b["remaining"] is not None else tr("unknown"),
                            tr(", runs out before it resets") if b["early"] else "")
        barrels.append('<g class="sm-barrel%s"><title>%s</title>%s</g>' % (" is-empty" if b["empty"] else "", esc(tip), bp.svg()))
    hold.rect(hx1 - 70, orlop - 6, 46, 2, "#8a5530")
    hold.rect(hx1 - 66, orlop - 4, 2, 4, "#5a3418")
    hold.rect(hx1 - 30, orlop - 4, 2, 4, "#5a3418")
    hold.shaded(hx1 - 62, orlop - 12, MAP, {})
    ship.append('<g><title>%s</title>%s</g>'
                % (esc(tr("the hold: bilge water is firstmate's %s; the white line is the warning threshold") % ctx), hold.svg()))
    ship += barrels
    ship.append('<text class="sm-ship__name" x="%d" y="%d">firstmate</text>' % (x0 + L - 60, WL - 12))
    for h in hands_at("rail"):
        x = spots[h["id"]][0]

        def line(p, t, x=x):
            p.line(x + 17, t + 1, x + 17, WL + 10, "#f2f5f8")
            p.rect(x + 16, WL + 9, 3, 3, "#f6f1e3")
        ship.append(hand_svg(h, spots[h["id"]], tags, extra=line, shadow_on=False))
    # the pier: the queue moored behind it, scouts back with their maps, crates of delivered work
    ship.append(draw_astern(sc, lay, tags))
    moored = [q for q in sc["queue"] if q["id"] not in lay["astern"]]
    for i, q in enumerate(moored[:MAX_QUEUE_BOATS]):
        ashore.append(queue_boat(q, lay["queue_x"] + 26 * i, PIER - 2))
    if len(moored) > MAX_QUEUE_BOATS:
        ashore.append('<text class="sm-ship__more" x="%d" y="%d">+%d</text>'
                      % (lay["queue_x"] + 26 * MAX_QUEUE_BOATS + 2, PIER - 4, len(moored) - MAX_QUEUE_BOATS))
    stuff = Pix()
    for h in sc["scouts"]:
        if h["line"]:
            continue
        x, wl, pose, upto = lay["scouts"][h["id"]]
        if h["place"] == "back":
            mid.append(hand_svg(h, (x + 3, wl - 3, pose, upto), tags, shadow_on=False) + boat_pix(x, wl).svg())
        elif h["place"] == "dock":
            ashore.append(hand_svg(h, (x + 3, wl - 3, pose, upto), tags, shadow_on=False) + boat_pix(x, wl).svg())
            stuff.shaded(x + 4, PIER + 2, MAP, {})
    pier = Pix()
    for y in range(PIER, PIER + 12):
        pier.rect(0, y, W, 1, mix("#c08a55", "#a06c3a", (y - PIER) / 11.0) if (y - PIER) % 4 else "#8a5530")
    for x in range(0, W, 34):
        pier.rect(x, PIER, 1, 12, "#8a5530")
    pier.rect(0, PIER + 12, W, 5, "#6b3a1c")
    pier.rect(0, PIER + 12, W, 1, "#9a6233")
    for x in range(6, W, 30):
        pier.rect(x, PIER + 17, 5, SHIP_H - PIER - 17, "#4f2c14")
        pier.rect(x, PIER + 17, 1, SHIP_H - PIER - 17, "#6b3a1c")
    for x in range(40, W, 64):
        pier.shaded(x, PIER - 4, ["OOO", "OoO", "OoO"], {"O": "#2a1a10", "o": "#4a3020"})
    ashore.append(pier.svg())
    for i in range(min(len(sc["crates"]), MAX_CRATES)):
        stuff.shaded(lay["crates_x"] + 9 * (i // 3), PIER + 2 - 8 * (i % 3 + 1), CRATE, CRATE_INK)
    ashore.append('<g><title>%s</title>%s</g>'
                  % (esc(tr("%d task delivered in the last %d days%s" if len(sc["crates"]) == 1 else "%d tasks delivered in the last %d days%s")
                         % (len(sc["crates"]), DAYS, ": " + ", ".join(c["id"] for c in sc["crates"][:20]) if sc["crates"] else "")),
                     stuff.svg()))
    if len(sc["crates"]) > MAX_CRATES:
        ashore.append('<text class="sm-ship__more" x="%d" y="%d">+%d</text>'
                      % (lay["crates_x"] + 9 * (MAX_CRATES // 3) + 2, PIER - 4, len(sc["crates"]) - MAX_CRATES))
    (px0, py0), (px1, py1) = lay["plank"]
    if hands_at("plank"):
        plank = Pix()
        for k in range(5):
            plank.line(px0 + k, py0, px1 + k, py1, ("#e0aa72", "#d6a06a", "#b07a45", "#b07a45", "#6b3a1c")[k])
        for t in (0.2, 0.4, 0.6, 0.8):
            plank.rect(int(px0 + (px1 - px0) * t), int(py0 + (py1 - py0) * t), 5, 1, "#5a3418")
        ashore.append(shadow(px1 + 10, py1, 24, 3, 0.25) + plank.svg())
        for h in hands_at("plank"):
            ashore.append(hand_svg(h, spots[h["id"]], tags, shadow_on=False))
    # the near layer: out of focus, dark and chunky, at the edges so it hides nobody
    fg = Pix()
    fg.rect(4, 286, 18, 54, "#1c120a")
    fg.rect(4, 286, 18, 3, "#2a1a10")
    for k in range(4):
        fg.rect(0, 300 + 6 * k, 28, 3, "#3a2414")
    fg.rect(W - 18, 214, 6, 126, "#1c120a")
    fg.rect(W - 26, 204, 22, 4, "#1c120a")
    fg.rect(W - 24, 208, 18, 14, "#24170d")
    fg.rect(W - 22, 210, 14, 10, "#f2c35a")
    fg.rect(W - 20, 211, 6, 4, "#fff0b0")
    for _ in range(W // 16):
        x = rnd.randrange(-40, W + 40)
        fg.rect(x, SHIP_H - 3, 14, 3, "#0a1c33" if night else "#24577d")
        fg.rect(x + 2, SHIP_H - 5, 10, 2, "#0f2846" if night else "#2c6690")
    near.append(fg.svg())
    lights.append('<circle class="sm-glow" cx="%d" cy="215" r="%d" fill="#fff0b0" opacity="%s"/>'
                  % (W - 15, 30 if night else 20, "0.3" if night else "0.18"))
    shade = band(-300, -300, W + 600, SHIP_H + 300, "#081633", ' opacity="0.38"') if night else ""
    args = ((sc["sea"], " is-night" if night else "", W, SHIP_H, esc(tr("The ship: %s") % ship_counts(sc)), W, SHIP_H)
            + tuple(lay["crew"])
            + ("#0c2440" if night else "#2c6f9f", "#0d1631" if night else "#6fb3e0", "".join(far), "".join(mid),
               "".join(ship), "".join(ashore), shade, "".join(near), "".join(lights), tags.svg()))
    return ('<svg class="sm-ship__svg sea-%s%s" viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet" '
            'shape-rendering="crispEdges" role="group" aria-label="%s" data-w="%d" data-h="%d" data-crew="%d,%d,%d,%d">'
            '<rect x="-3000" y="-3000" width="7000" height="7000" fill="%s"/>'
            '<rect x="-3000" y="-3000" width="7000" height="3010" fill="%s"/>'
            '<g class="sm-far">%s</g><g class="sm-mid">%s<g class="sm-bob">%s</g>%s</g>%s'
            '<g class="sm-near">%s</g><g class="sm-lights">%s</g><g class="sm-tags">%s</g></svg>' % args)


def sprite_icon(rows, pal, scale=2):
    """A small inline picture of a sprite, for the stage chips and the log."""
    p = Pix()
    p.shaded(1, 1, rows, pal)
    w, h = max(len(r) for r in rows) + 2, len(rows) + 2
    return ('<svg class="sm-icon" viewBox="0 0 %d %d" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (w, h, w * scale, h * scale, p.svg()))


def pose_icon(pose, scale=2, color=None):
    return sprite_icon(POSES[pose], hand_palette({"id": "key", "color": color or PROJECT_COLORS[0]}), scale)


def flag_icon(scale=2):
    p = Pix()
    red_flag(p, 7, 1)
    return ('<svg class="sm-icon" viewBox="0 0 9 15" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (9 * scale, 15 * scale, p.svg()))


def boat_icon(scale=2):
    return ('<svg class="sm-icon" viewBox="-1 -1 18 16" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (18 * scale, 16 * scale, boat_pix(0, 13, cargo=True, flag=True).svg()))


# the stages a chip lights: key, pose, name, and where a hand stands and what that means
STAGES = [
    ("nest", "spy", "exploring", "in the crow's nest with a spyglass: reading the code and planning before it builds"),
    ("deck", "hammer", "implementing", "on deck with a hammer: making the change"),
    ("bow", "signal", "PR open", "at the bow with signal flags: the pull request is open"),
    ("rail", "fish", "waiting for CI", "fishing at the rail: the checks are running"),
    ("plank", "pack", "ready to merge", "on the gangplank with a pack: done, waiting to go ashore"),
    ("hatch", "stand", "phase unknown", "under a question mark: no sign of its phase yet"),
    ("scouts", "scroll", "scouts", "in a skiff: rowing out to explore, back with the map, then moored at the pier with the report"),
    ("off", "rest", "off duty", "sitting with the dice: paused, or quiet for a while"),
    ("flag", None, "waiting for you", "a red flag: needs your decision; hands wait in your cabin and queued tasks are tied astern, "
                                      "longest wait first, and the log below lists them all"),
]


def stage_members(sc, key):
    if key == "flag":
        return sc["line"]
    if key == "off":
        return [h for h in sc["hands"] if h["off"]]
    if key == "scouts":
        return sc["scouts"]
    return [h for h in sc["hands"] if h["place"] == key]


def stage_chips(sc):
    """One chip per stage, with how many are in it; a click lights them in the scene."""
    out = []
    for key, pose, name, where in STAGES:
        who = stage_members(sc, key)
        if not who and key in ("hatch", "scouts"):
            continue
        name, where = tr(name), tr(where)
        desc = "%s: %s. %s" % (name[0].upper() + name[1:], where,
                               (tr("Now: %s.") % ", ".join(x["id"] for x in who)) if who else tr("Nobody now."))
        out.append('<button type="button" class="sm-chip%s" data-stage="%s" data-desc="%s" aria-pressed="false"%s>%s<span>%s</span><b>%d</b></button>'
                   % (" sm-chip--red" if key == "flag" else "", key, esc(desc), "" if who else " disabled",
                      flag_icon(1) if pose is None else pose_icon(pose, 1), esc(name), len(who)))
    return "".join(out)


def captain_log(sc):
    """The strip below the scene: the captain's word and everyone in line, longest wait first."""
    cap = sc["captain"]
    if cap["busy"]:
        o = cap["oldest"]
        say = tr("%d waits on my word. The longest, %s, has waited %s." if cap["waiting"] == 1 else
                 "%d wait on my word. The longest, %s, has waited %s.") % (cap["waiting"], o["id"], o["wait"])
        cards = []
        for x in sc["line"]:
            if x["kind"] == "queue":
                icon, what, why = boat_icon(), tr("queued, %s") % (x["repo"] or tr("no project")), x["hold"] or ", ".join(x["why"])
            else:
                pose = "scroll" if x["kind"] == "scout" else PLACE_POSE.get(x["place"], "stand")
                said = x["why"][1:] if x["crew"] in CAPTAIN_STATES else x["why"]      # the open line says more than the crew state
                icon, what, why = pose_icon(pose, 2, x["color"]), "%s, %s" % (x["phase"], x["project"] or tr("no project")), (said or x["why"])[0]
            cards.append('<button type="button" class="sm-logcard" data-id="%s" title="%s"><span class="sm-logcard__n">%d</span>%s'
                         '<span class="sm-logcard__body"><b>%s</b><span>%s</span><em>%s</em></span><span class="sm-logcard__wait">%s</span></button>'
                         % (esc(x["id"]), esc("%s: %s" % (x["id"], why)), x["line"], icon, esc(x["id"]), esc(what), esc(why), esc(x["wait"])))
        body = "".join(cards)
    else:
        say, body = tr("All clear. Nothing waits on my word."), '<span class="sm-log__none">%s</span>' % esc(tr("Nobody is waiting for you."))
    return ('<div class="sm-log__capt">%s<p><b>%s</b><span>%s</span></p></div><div class="sm-log__cards">%s</div>'
            % (sprite_icon((CAPTAIN_BUSY if cap["busy"] else CAPTAIN_OFF)[:10], CAPTAIN_INK, 4), esc(tr("Captain")), esc(say), body))


def block_ship(s):
    sc = ship_scene(s)
    chips = "".join('<li><span class="sm-sw" style="background:%s"></span>%s</li>' % (c, esc(p))
                    for p, c in sc["colors"].items()) or '<li class="sm-faint">%s</li>' % esc(tr("no project aboard"))
    return {"ship": render_ship(sc), "counts": ship_counts(sc), "projects": chips, "stages": stage_chips(sc), "log": captain_log(sc)}


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
                "decisions": [{"state": st, "key": k, "at": int(now - age), "text": t} for st, k, t, age in decisions]}

    agents = [
        agent("DEMO-SEARCH", "Find where the search index drifts", "harbor-api", 0),
        agent("DEMO-AUTH", "Rotate session tokens on login", "harbor-api", 1),
        agent("DEMO-RATE", "Rate limit the public endpoints", "harbor-api", 1, crew="parked",
              decisions=[("needs-decision", "limits", "per user or per key?", 50 * 60)]),
        agent("DEMO-CACHE", "Cache the tide tables", "harbor-api", 2),
        agent("DEMO-LOGS", "Structured logs for the workers", "harbor-api", 3),
        agent("DEMO-I18N", "Translate the settings page", "harbor-api", 4, crew="done"),
        agent("DEMO-DARK", "Dark theme for the charts", "lighthouse-web", 0, crew="paused"),
        agent("DEMO-NAV", "Keyboard navigation in the menu", "tide-charts", 1),
        agent("DEMO-FORMS", "Validate the signup form", "lighthouse-web", 3),
        agent("DEMO-ICONS", "Redraw the toolbar icons", "lighthouse-web", 4, crew="done"),
        agent("DEMO-EXPORT", "Export charts as CSV", "tide-charts", 2, crew="blocked",
              decisions=[("blocked", "bucket", "the export bucket refuses writes from the worker", 3 * h + 5 * 60)]),
        agent("DEMO-AXIS", "Fix the time axis labels", "tide-charts", None, crew="unknown"),
        agent("DEMO-SCOUT-DB", "Compare two storage engines", "harbor-api", 0, kind="scout"),
        agent("DEMO-SCOUT-CDN", "Survey the image CDN costs", "lighthouse-web", 1, kind="scout"),
        agent("DEMO-SCOUT-UX", "Review the onboarding flow", "lighthouse-web", 2, crew="done", kind="scout"),
    ]
    day = dt.date.fromtimestamp(now)
    queue = [
        {"id": "DEMO-Q-PUSH", "title": "Push notifications", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("slot", tr("build slot %d/%d") % (2, 2), tr("build slots for %s: %d of %d taken (%s)") % ("harbor-api", 2, 2, "DEMO-AUTH, DEMO-RATE"))],
         "hold_reason": ""},
        {"id": "DEMO-Q-BILL", "title": "New billing plan", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("decision", tr("captain's decision"), "which plan ships first")], "hold_reason": "which plan ships first"},
        {"id": "DEMO-Q-MAPS", "title": "Offline maps", "repo": "lighthouse-web", "kind": "ship", "created": day.isoformat(),
         "reasons": [("date", tr("held until %s") % fmt_date_iso((day + dt.timedelta(days=3)).isoformat()), "held")],
         "hold_reason": ""},
        {"id": "DEMO-Q-AUDIT", "title": "Audit log", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("task", tr("waits for %s") % "DEMO-AUTH", tr("waits for %s") % "DEMO-AUTH")], "hold_reason": ""},
        {"id": "DEMO-Q-THEME", "title": "Theme picker", "repo": "lighthouse-web", "kind": "ship", "created": day.isoformat(),
         "reasons": [("ready", tr("ready to dispatch"), tr("nothing holds it: firstmate can dispatch it"))], "hold_reason": ""},
        {"id": "DEMO-Q-TILES", "title": "Faster chart tiles", "repo": "tide-charts", "kind": "scout", "created": day.isoformat(),
         "reasons": [("decision", tr("captain's decision"), "worth a scout?")], "hold_reason": "worth a scout?"},
        {"id": "DEMO-Q-DOCS", "title": "API reference pages", "repo": "harbor-api", "kind": "ship", "created": day.isoformat(),
         "reasons": [("slot", tr("build slot %d/%d") % (2, 2), tr("build slots for %s: %d of %d taken (%s)") % ("harbor-api", 2, 2, "DEMO-AUTH, DEMO-RATE"))],
         "hold_reason": ""},
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
    for q in queue:                                     # how long the held ones have waited for the captain
        q["waiting_since"] = {"DEMO-Q-BILL": now - 26 * h, "DEMO-Q-TILES": now - 25 * 60}.get(q["id"])
        q["waiting_exact"] = True
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
        "firstmate": {"session": "demo0000-session", "found_by": tr("the demo"), "model": "model-a", "context": 144000,
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
    @property
    def notice(self):
        return '<div class="sm-notice sm-notice--warn">%s</div>' % esc(tr("Demo: a made-up home, not real data."))

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
        return {"ok": False, "error": tr("this is the demo: nothing was queued")}


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
            if u.path == "/ship":
                return self.send(200, json.dumps(block_ship(panel.recent())), "application/json")
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
    ap.add_argument("--lang", choices=LANGS, default=os.environ.get("SHIPMATE_LANG") or "en",
                    help="the page's language: en (default) or pt-BR")
    a = ap.parse_args(argv)
    if a.lang not in LANGS:
        print("shipmate: SHIPMATE_LANG must be one of %s" % ", ".join(LANGS), file=sys.stderr)
        return 2
    global LANG
    LANG = a.lang
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
