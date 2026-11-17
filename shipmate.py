#!/usr/bin/env python3
"""shipmate: a small local dashboard for a Firstmate home.

One page, read live from the home's own records on every load and on every 30 s
refresh:

  board         the queue as the first lane (with "+ Add task"), then one lane per
                phase of the live workers; empty phases shrink to thin strips
  sidebar       today's tokens, firstmate's own session (context against the window,
                growth, compactions, warning past a threshold) and each Claude
                account's quota (quota-axi, cached 60 s), tightest first
  detail drawer facts behind any card: the captain's intent from the brief, phase
                timeline, latest status lines, PR checks (cached), tokens, context,
                idle time, report link; for an item waiting on the captain, a form to
                answer the decision and the answers sent with firstmate's replies
  usage         tokens per day and per delivered task, folded under the board
  ship view     the same snapshot drawn as one ship, cut away, that zooms and pans: agents
                as hands whose place on board is their phase (switch with "Ship" in the header)

Read-only over the home, except the add-task form, which runs the same backlog
command firstmate uses (`bin/fm-tasks-axi.sh add ... --queue`) and then the inbox
note command (`bin/fm-inbox.sh note`), and the answer form, which sends the
captain's answer with `bin/fm-inbox.sh note --request-id` (and `announce` to
repair a failed wake), and "Clear context", which asks firstmate for /stow with an
inbox note and, only after its ready reply, types /clear into firstmate's own herdr
pane with `bin/fm-send.sh`. Standard library only; Python 3.9 or newer.

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
  --clear-timeout N   seconds firstmate has to stow and reply to a clear request
                ($SHIPMATE_CLEAR_TIMEOUT; 900)
  --herdr-session NAME  the herdr session that runs firstmate ($SHIPMATE_HERDR_SESSION; default)
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
import math
import os
import random
import re
import secrets
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8737
REFRESH_SECONDS = 30
DEFAULT_CACHE_SECONDS = 5      # how long one reading of the fleet answers every request
QUOTA_TTL = 60
DOCTOR_TIMEOUT = 90                      # seconds `claude doctor` may take before it is stopped
CREW_TTL = 20
SPAWN_SLACK = 120          # seconds: a session's first line lands a little after the task record is written
DAYS = 7
GROWTH_HOURS = 3
CONTEXT_WINDOWS = (200_000, 1_000_000)   # Claude Code context windows; the transcript does not record which one
DEFAULT_CONTEXT_WARN = 60                # percent of the window
CHECKS_TTL = 120                         # PR checks are read from the forge at most this often per PR
STATUS_TAIL = 8
DECISION_TEXT = 1000                     # characters of an open decision line kept in the snapshot
RE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_BODY = 64 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- configuration


class Config:
    def __init__(self, home, fm_root=None, claude_dirs=(), port=DEFAULT_PORT, quota_cmd="quota-axi",
                 context_limit=None, context_warn=DEFAULT_CONTEXT_WARN, gh_cmd="gh",
                 cache_seconds=DEFAULT_CACHE_SECONDS, claude_cmd="claude",
                 herdr_cmd="herdr", herdr_session="default", clear_timeout=None):
        self.home = os.path.abspath(home)
        self.fm_root = os.path.abspath(fm_root or self.home)
        self.port = port
        self.quota_cmd = quota_cmd
        self.context_limit = context_limit      # tokens, or None to infer
        self.context_warn = context_warn        # percent
        self.gh_cmd = gh_cmd                    # only the detail view's PR checks use it
        self.claude_cmd = claude_cmd            # only the accounts' "claude doctor" button runs it
        self.cache_seconds = cache_seconds      # seconds one snapshot is reused; 0 reads the fleet on every request
        self.herdr_cmd = herdr_cmd              # only "Clear context" reads herdr, to find firstmate's pane
        self.herdr_session = herdr_session      # the herdr session that runs firstmate
        self.clear_timeout = clear_timeout or DEFAULT_CLEAR_TIMEOUT   # seconds firstmate has to stow and reply
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


LIST_FIELDS = "blocked,blocked_by,closed,created,deps,held,hold_kind,hold_reason,hold_until,priority,links"


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


def closed_rows(backlog, now):
    """The tasks firstmate closed as done in the last DAYS days, oldest first: what the crates on the
    pier count. firstmate closes a task when it delivers it, so a crate appears with the delivery;
    the session sheets come later and in batches, and only feed the usage block."""
    since = (dt.date.fromtimestamp(now) - dt.timedelta(days=DAYS - 1)).isoformat()
    rows = [{"id": r["id"], "kind": r.get("kind") or "", "repo": r.get("repo") or "", "closed": r["closed"][:10]}
            for r in backlog or [] if r.get("state") == "done" and (r.get("closed") or "")[:10] >= since]
    rows.sort(key=lambda r: (r["closed"], r["id"]))
    return rows


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


SHIP_PHASES = ["exploring", "implementing", "PR open", "waiting for CI", "waiting for merge"]
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
            # kept whole enough that the ready options a line lists survive for the answer form
            open_.append({"state": s["state"], "key": s.get("key"), "at": s["at"], "text": s["text"][:DECISION_TEXT]})
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


LANES_TTL = 20


def parse_capacity_line(text):
    """The first line of `bin/fm-capacity.sh`: `capacity: free_lanes=.. reason=.. lanes=.. target=..`.
    Returns {"free", "reason", "lanes", "target"} when the home sets a numeric lane target, else None."""
    line = (text or "").splitlines()[0] if text else ""
    if not line.startswith("capacity:"):
        return None
    kv = dict(x.split("=", 1) for x in line[len("capacity:"):].split() if "=" in x)
    if not kv.get("target", "").isdigit() or not kv.get("lanes", "").isdigit():
        return None
    free = kv.get("free_lanes", "")
    return {"free": int(free) if free.isdigit() else None, "reason": kv.get("reason") or "", "lanes": int(kv["lanes"]),
            "target": int(kv["target"])}


class LanesCache:
    """The home's own lane capacity, from `bin/fm-capacity.sh`, read only when `config/lane-capacity`
    exists (without it the script sets no target) and at most every LANES_TTL seconds."""

    def __init__(self):
        self.at, self.val = 0, None

    def get(self, cfg):
        if not os.path.exists(os.path.join(cfg.home, "config", "lane-capacity")) or not os.path.exists(cfg.bin("fm-capacity.sh")):
            return None
        if time.time() - self.at > LANES_TTL:
            rc, out, _err = run([cfg.bin("fm-capacity.sh")], env=cfg.env(), timeout=20)
            self.val, self.at = parse_capacity_line(out) if rc == 0 else None, time.time()
        return self.val


def queue_rows(cfg, backlog, lanes=None):
    """The queued tasks with why each waits, most telling reason first. `lanes` is the home's lane
    capacity (LanesCache), which holds back a task nothing else holds when no lane is free."""
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
                # a dated hold is scheduled, not a live decision, until its date arrives
                txt = tr("held until %s") % fmt_date_iso(r["hold_until"])
                reasons.append(("date", txt, txt))
            elif r.get("hold_kind") == "captain" or not r.get("hold_until"):
                reasons.append(("decision", tr("captain's decision"), (r.get("hold_reason") or tr("captain's decision"))[:DECISION_TEXT]))
        deps = [d for d in re.split(r"[\s,]+", r.get("blocked_by") or "") if d and d != "none"]
        if deps:
            reasons.append(("task", tr("waits for %s") % ", ".join(deps), tr("waits for %s") % ", ".join(deps)))
        repo = r.get("repo") or ""
        slot = None
        if not reasons and repo in caps:
            h = sorted(holders.get(repo, []))
            if len(h) >= caps[repo]:
                reasons.append(("slot", tr("build slot %d/%d") % (len(h), caps[repo]),
                                tr("build slots for %s: %d of %d taken (%s)") % (repo, len(h), caps[repo], ", ".join(h))))
                slot = {"group": repo, "cap": caps[repo], "taken": len(h), "holders": h}
        if not reasons and lanes and lanes["free"] == 0:
            reasons.append(("lane", tr("home lanes %d/%d") % (lanes["lanes"], lanes["target"]),
                            tr("no free lane in this home (%s): %d of %d in use") % (lanes["reason"], lanes["lanes"], lanes["target"])))
            slot = {"group": "", "cap": lanes["target"], "taken": lanes["lanes"], "holders": []}
        if not reasons:
            reasons.append(("ready", tr("ready to dispatch"), tr("nothing holds it: firstmate can dispatch it")))
        since, exact = stamps.get(r["id"]) or parse_hold_set("Captain hold set: " + (r.get("created") or ""))
        out.append({"id": r["id"], "title": r.get("title", ""), "repo": repo, "kind": r.get("kind", ""),
                    "created": r.get("created", ""), "reasons": reasons,
                    "hold_reason": (r.get("hold_reason") or "")[:DECISION_TEXT], "deps": deps, "slot": slot,
                    "hold_until": r["hold_until"] if any(x[0] == "date" for x in reasons) else "",
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

    def forget(self):
        """The next read asks quota-axi again, e.g. right after an account's token was renewed."""
        with self.lock:
            self.value = None


def quota_lane(p):
    loc = (p.get("accountLocator") or {}).get("path")
    eff = next((x for x in (p.get("quotaSemantics") or {}).get("effectiveAvailability", [])
                if x.get("scope") in ("all_models", "all_products")), None) or {}
    windows = {w.get("id"): w for w in p.get("windows", [])}
    lim = windows.get((eff.get("limitingWindowIds") or [None])[0]) or {}
    runway = eff.get("runway") or {}
    state = p.get("state") or {}
    status = state.get("status")
    return {
        # the account's own config directory names it; quota-axi's identity fields are not shown
        "label": account_label(loc) if loc else "account",
        "plan": p.get("plan"),
        "status": status,
        "reason": state.get("reason"),          # credentials_expired: the short-lived token ran out
        "auth": state.get("authStatus"),        # expired_refreshable: claude doctor can renew it
        "remaining": eff.get("effectivePercentRemaining"),
        "limit_label": lim.get("label"),
        "resets": parse_ts(lim.get("resetsAt")),
        "runway": runway.get("status"),
        "exhausts": parse_ts(runway.get("projectedExhaustedAt")),
        "windows": [{"label": w.get("label"), "remaining": w.get("percentRemaining"), "resets": parse_ts(w.get("resetsAt"))}
                    for w in p.get("windows", []) if w.get("percentRemaining") is not None],
    }


# ---------------------------------------------------------------- claude doctor


def doctor_accounts(cfg):
    """The accounts `claude doctor` may run for, by the label the page shows: only the home's own
    config/claude-accounts, never a path from a request. A label two directories share names neither."""
    seen = {}
    for name, path in cfg.accounts:
        seen.setdefault(account_label(path), []).append((name, path))
    return {label: rows[0] for label, rows in seen.items() if len(rows) == 1}


def needs_doctor(ln):
    """A reading that a renewed token may bring back: stale, unknown, or expired by quota-axi's word."""
    return (ln.get("status") in ("stale", "unavailable", "error") or ln.get("remaining") is None
            or ln.get("reason") == "credentials_expired" or ln.get("auth") == "expired_refreshable")


def doctor_command(cfg, name, path):
    """`claude doctor` for one account, as an argument list (never a shell line), with its environment.
    The ordinary account is Claude Code's default, which a CLAUDE_CONFIG_DIR would turn into another one."""
    env = dict(os.environ)
    if name == "ordinary":
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = path
    return [cfg.claude_cmd, "doctor"], env


def run_doctor(cfg, label, timeout=DOCTOR_TIMEOUT):
    """Runs `claude doctor` for one listed account, with no terminal and a time limit. Its output is
    thrown away unread: it can name the account, and nothing of it is shown or kept."""
    target = doctor_accounts(cfg).get(label)
    if target is None:
        return {"ok": False, "error": tr("no account of this home is called %s") % label[:80]}
    argv, env = doctor_command(cfg, *target)
    # it reads the settings of the directory it runs in: the account's own, never the server's
    cwd = next((d for d in (target[1], os.path.expanduser("~")) if os.path.isdir(d)), os.sep)
    try:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "error": tr("could not start %s: %s") % (argv[0], exc.strerror or exc)}
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)             # it and whatever it started
        except OSError:
            proc.kill()
        proc.wait()
        return {"ok": False, "error": tr("claude doctor did not finish in %d s and was stopped") % timeout}
    if rc != 0:
        return {"ok": False, "error": tr("claude doctor exited with status %d") % rc}
    return {"ok": True}


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


# ---------------------------------------------------------------- the captain's answers
#
# A decision that waits on the captain is answered from its drawer. shipmate sends the answer as a
# note in firstmate's inbox with `bin/fm-inbox.sh note --request-id`, which wakes firstmate and
# never keeps the same request twice, and reads back with `bin/fm-inbox.sh receipts` whether
# firstmate took the note and what it replied with `bin/fm-inbox.sh reply`. shipmate never writes
# the inbox files itself.

ANSWER_PREFIX = "shipmate-answer:"      # an answer's request id: shipmate-answer:<form id>:<task id>
RE_FORM_ID = re.compile(r"^[A-Za-z0-9_-]{1,10}$")   # short enough that the request id fits fm-inbox's 128
RECEIPTS_TTL = 10
MAX_ANSWER = 4000
ANSWER_MARK = "Answer:"                 # the captain's own words follow this line in the note
RE_OPTION = re.compile(r"(?:^|(?<=[\s;,:.?!]))\(?([A-Za-z]|[1-9])\)\s+")
ANSWER_STATES = ("sent", "unwoken", "received", "replied")


def option_run(text):
    """The first run of two or more consecutive labels from A, a or 1 in `text`, as (lead, rows, tail):
    the words before the first label, each label with its text (trailing `;`, `,` and `or` dropped), and
    what follows the last item's sentence. The run is ([], ...) when the text lists nothing."""
    text = text or ""
    marks = list(RE_OPTION.finditer(text))
    for i, m in enumerate(marks):
        if m.group(1) not in ("A", "a", "1"):
            continue
        run = [m]
        for n in marks[i + 1:]:
            if ord(n.group(1)) == ord(run[-1].group(1)) + 1:
                run.append(n)
        if len(run) < 2:
            continue
        rows, tail = [], ""
        for j, o in enumerate(run):
            part = text[o.end():run[j + 1].start() if j + 1 < len(run) else len(text)]
            if j + 1 == len(run):
                bits = re.split(r"(?<=[.!?])\s", part, 1)
                part, tail = bits[0], (bits[1].strip() if len(bits) > 1 else "")
            part = re.sub(r"(?:[\s;,]|\b(?:or|ou)\b)+$", "", part).rstrip(".").strip()
            if part:
                rows.append({"label": o.group(1).upper(), "text": part})
        return (text[:m.start()].strip(), rows, tail) if len(rows) >= 2 else ("", [], "")
    return "", [], ""


def decision_options(text):
    """The ready options a decision line lists as `A) .. B) ..` (also `(a) ..` or `1) ..`): the first
    run of two or more consecutive labels from A, a or 1, each with its text, the last one ending with
    its sentence. A line without such a run has no options and is answered in words."""
    return option_run(text)[1]


def split_reason(text):
    """A decision's words cut for reading: {"lead", "items": [{label, text}], "tail"}. A text that lists
    `(1) .. (2) ..` (or `A) .. B) ..`) gives the words before the list, one item per label and what comes
    after; any other text is all lead."""
    lead, rows, tail = option_run(text)
    if not rows:
        return {"lead": (text or "").strip(), "items": [], "tail": ""}
    return {"lead": lead, "items": rows, "tail": tail}


# A path or an id inside a decision (`data/X-1/report.html`, `ME-INVENTARIO-DECISOES`) is set in mono and
# allowed to break anywhere, so one long token never pushes the drawer wider than the screen.
RE_CODE = re.compile(r"(?<![\w/.-])/?(?:[A-Za-z_.~][\w.-]*/)+[\w.-]*[\w-]|(?<![\w.-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![\w-])|(?<![\w/.-])[\w-]+\.(?:html|md|json|py|sh|css|jsonl|txt)\b")


def rich(text):
    """`text` escaped for HTML, with every path and id wrapped in <code class="sm-code">."""
    out, at = [], 0
    for m in RE_CODE.finditer(text or ""):
        out.append(esc(text[at:m.start()]))
        out.append('<code class="sm-code">%s</code>' % esc(m.group(0)))
        at = m.end()
    out.append(esc((text or "")[at:]))
    return "".join(out)


def reason_html(text):
    """A decision's words as the drawer shows them: the lead in a paragraph, a numbered list for the
    items it lists, what follows in a paragraph."""
    r = split_reason(text)
    return "".join(
        ['<p class="sm-reason__lead">%s</p>' % rich(r["lead"])] if r["lead"] else []) + (
        '<ol class="sm-items">%s</ol>' % "".join(
            '<li><b>%s)</b> <span>%s</span></li>' % (esc(i["label"]), rich(i["text"])) for i in r["items"]) if r["items"] else "") + (
        '<p class="sm-reason__tail">%s</p>' % rich(r["tail"]) if r["tail"] else "")


def agent_waiting_since(a):
    """Since when a flagged agent has waited on the captain: its oldest open decision line or, flagged
    by its crew state alone, since it went quiet."""
    ats = [d["at"] for d in a.get("decisions") or [] if d.get("at")]
    return min(ats) if ats else a.get("last_activity")


def captain_asks(s, tid):
    """What the item `tid` waits on the captain for, each one a decision to answer: an agent's open
    needs-decision and blocked lines (its crew state when it is parked, blocked or failed with no line
    open), or a queued task's captain hold. None when no card has that id, [] when it waits on nobody."""
    a = next((x for x in s.get("agents") or [] if x["id"] == tid), None)
    if a is not None:
        asks = []
        for i, d in enumerate(a.get("decisions") or []):
            ref = d.get("key") or ("at%d" % d["at"] if d.get("at") else "line%d" % i)
            asks.append({"ref": ref, "kind": "line", "state": d["state"], "key": d.get("key"), "at": d.get("at"),
                         "text": d["text"], "options": decision_options(d["text"])})
        if not asks and a.get("crew") in CAPTAIN_STATES:
            asks.append({"ref": "crew", "kind": "crew", "state": a["crew"], "key": None, "at": a.get("last_activity"),
                         "text": "", "options": []})
        return asks
    q = next((x for x in s.get("queue") or [] if x["id"] == tid), None)
    if q is None:
        return None
    if not any(r[0] == "decision" for r in q.get("reasons") or []):
        return []
    text = q.get("hold_reason") or ""
    return [{"ref": "hold", "kind": "hold", "state": "captain hold", "key": None, "at": q.get("waiting_since"),
             "text": text, "options": decision_options(text)}]


def item_title(s, tid):
    x = next((x for x in (s.get("agents") or []) + (s.get("queue") or []) if x["id"] == tid), None)
    return re.sub(r"\s+", " ", (x or {}).get("title") or "").strip() or tid


def utc_text(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "an unknown time"


def answer_note(tid, title, ask, option, words):
    """The note firstmate receives, in English whatever the page's language: the task and the
    decision it answers, how to reply, then the captain's answer after an `Answer:` line."""
    if ask["kind"] == "line":
        about = "its %s line%s of %s: %s" % (ask["state"], " [key=%s]" % ask["key"] if ask["key"] else "",
                                            utc_text(ask["at"]), ask["text"])
    elif ask["kind"] == "crew":
        about = "its crew state, %s, with no needs-decision or blocked line open" % ask["state"]
    else:
        about = "its captain hold: %s" % (ask["text"] or "no reason recorded")
    answer = []
    if option:
        answer.append("%s) %s" % (option["label"], option["text"]))
    if words:
        answer.append(words)
    return "\n".join(["Captain's answer from shipmate on %s (%s)." % (tid, title),
                      "Decision: %s" % about,
                      "Reply to the captain with `bin/fm-inbox.sh reply <this note's id> <text>`; shipmate shows it.",
                      ANSWER_MARK] + answer)


def note_words(body):
    """The captain's own words in an answer note: what follows its `Answer:` line."""
    head, sep, words = (body or "").partition("\n%s\n" % ANSWER_MARK)
    return words if sep else body or ""


def prepare_answer(s, tid, ref, option, words):
    """The note for an answer to decision `ref` of item `tid`, checked against the snapshot:
    {"body": ...} or {"error": ...}."""
    asks = captain_asks(s, tid)
    if asks is None:
        return {"error": tr("this item is no longer on the board")}
    ask = next((x for x in asks if x["ref"] == ref), None)
    if ask is None:
        return {"error": tr("this decision is no longer open; reopen the drawer")}
    chosen = None
    if option:
        chosen = next((o for o in ask["options"] if o["label"] == option), None)
        if chosen is None:
            return {"error": tr("that option is not on this decision")}
    if not chosen and not words:
        return {"error": tr("pick an option or write an answer")}
    if len(words) > MAX_ANSWER:
        return {"error": tr("the answer is longer than %d characters") % MAX_ANSWER}
    return {"body": answer_note(tid, item_title(s, tid), ask, chosen, words)}


def answer_request_id(form_id, tid):
    return "%s%s:%s" % (ANSWER_PREFIX, form_id, tid)


def answers_from_receipts(doc):
    """The captain's answers sent from shipmate, by task and oldest first, out of the receipts JSON,
    each with what became of it: sent, unwoken (saved but firstmate was not woken), received
    (firstmate took it from its inbox) or replied."""
    by = {}
    for n in (doc.get("pending") or []) + (doc.get("handled") or []):
        rid = n.get("request_id") or ""
        if not rid.startswith(ANSWER_PREFIX):
            continue
        tid = rid[len(ANSWER_PREFIX):].partition(":")[2]
        if not valid_id(tid):
            continue
        reply = n.get("reply") or None
        state = ("replied" if reply else "received" if n.get("acknowledged")
                 else "unwoken" if n.get("announced") is False else "sent")
        by.setdefault(tid, []).append({
            "note": n.get("id") or "", "at": parse_ts(n.get("at")), "words": note_words(n.get("body")), "state": state,
            "reply": {"at": parse_ts(reply.get("at")), "text": reply.get("body") or ""} if reply else None})
    for rows in by.values():
        rows.sort(key=lambda x: (x["at"] or 0, x["note"]))
    return by


def read_receipts(cfg):
    """(receipts JSON, error): every pending and handled note with its reply, never a bounded page."""
    rc, out, err = run([cfg.bin("fm-inbox.sh"), "receipts", "--all-pending", "--all-handled"], env=cfg.env())
    if rc != 0:
        return None, (err or out).strip()[:300] or "exit %d" % rc
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    return (doc, None) if isinstance(doc, dict) else (None, tr("the inbox receipts are not JSON"))


def read_answers(cfg):
    """(answers by task, error): every pending and handled note, since an answer's state must not
    drop out of a bounded page while its decision is still open."""
    doc, err = read_receipts(cfg)
    return (answers_from_receipts(doc), None) if doc is not None else ({}, err)


class ReceiptsCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.at = 0.0
        self.val = ({}, None)

    def get(self, cfg):
        with self.lock:
            if time.time() - self.at > RECEIPTS_TTL:
                self.val, self.at = read_answers(cfg), time.time()
            return self.val

    def drop(self):
        with self.lock:
            self.at = 0.0


def live_answer(answers, since):
    """The latest answer sent since the item began to wait on the captain, or None: an answer to an
    earlier decision says nothing about the one open now."""
    fresh = [x for x in answers or [] if since is None or (x["at"] or 0) >= since]
    return fresh[-1] if fresh else None


def answer_marks(s):
    """The state of the live answer of every item that waits on the captain, by id."""
    answers, marks = s.get("answers") or {}, {}
    for a in s.get("agents") or []:
        if a["id"] in answers and captain_reasons(a):
            x = live_answer(answers[a["id"]], agent_waiting_since(a))
            if x:
                marks[a["id"]] = x["state"]
    for q in s.get("queue") or []:
        if q["id"] in answers and any(r[0] == "decision" for r in q.get("reasons") or []):
            x = live_answer(answers[q["id"]], q.get("waiting_since"))
            if x:
                marks[q["id"]] = x["state"]
    return marks


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
    """A PR's state and its checks counted by outcome, with when the earliest of them started and
    when the last finished one finished (`gh` gives a check run startedAt and completedAt, a commit
    status only startedAt, which is when it was set), so the wait that follows can be timed."""
    counts = {"pass": 0, "fail": 0, "pending": 0, "skipped": 0}
    failing, started, ended = [], [], []
    def when(v):                                    # a check not started yet says 0001-01-01
        t = parse_ts(v)
        return t if t and t > 0 else None
    for c in doc.get("statusCheckRollup") or []:
        start = when(c.get("startedAt"))
        if c.get("__typename") == "StatusContext":
            st = (c.get("state") or "").upper()
            bucket = "pass" if st == "SUCCESS" else "pending" if st in ("PENDING", "EXPECTED") else "fail"
            name = c.get("context") or "?"
            end = start if bucket != "pending" else None
        else:
            name = c.get("name") or "?"
            if (c.get("status") or "").upper() != "COMPLETED":
                bucket, end = "pending", None
            else:
                con = (c.get("conclusion") or "").upper()
                bucket = ("pass" if con in ("SUCCESS", "NEUTRAL") else "skipped" if con == "SKIPPED" else "fail")
                end = when(c.get("completedAt"))
        counts[bucket] += 1
        if bucket == "fail":
            failing.append(name)
        started += [start] if start else []
        ended += [end] if end else []
    return {"state": doc.get("state"), "draft": doc.get("isDraft"), "counts": counts, "failing": failing[:12],
            "started": min(started) if started else None, "ended": max(ended) if ended else None, "error": None}


RE_GITHUB_PR = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+$")


def checks_verdict(summary):
    """One word for a PR's checks: pass when every check that ran passed, fail when one failed,
    pending while one runs; None when there are none or no answer."""
    if not summary or summary.get("error"):
        return None
    counts = summary.get("counts") or {}
    if counts.get("fail"):
        return "fail"
    if counts.get("pending"):
        return "pending"
    return "pass" if counts.get("pass") else None


def phase_by_checks(a, summary):
    """A ship task with its PR open takes its phase from the PR's checks once they are read, since
    the worker often says done as it opens the PR, before CI has said anything: a check running is
    waiting for CI, even after done; a check failed is implementing again; every check passed is
    waiting for the merge once the worker said done, and the PR open while it is still at work.
    Without a check, an answer or a GitHub PR, the phase from the milestones stands. The start of a
    phase taken from the checks is when they started, when one failed, or when the last finished
    or the worker said done, whichever came later."""
    verdict = checks_verdict(summary)
    a["checks"] = verdict
    if a.get("kind") != "ship" or a.get("phase") not in (2, 3, 4) or verdict is None:
        return a
    done = a.get("crew") == "done"
    times = list(a.get("milestones") or [None] * len(SHIP_PHASES))
    started, ended = summary.get("started"), summary.get("ended")
    if verdict == "pending":
        phase, since, why = 3, started, "a check on the PR is running"
    elif verdict == "fail":
        phase, since, why = 1, ended, "a check on the PR failed"
    elif done:
        phase, since, why = 4, max([t for t in (ended, times[4]) if t] or [None]), "every check on the PR passed and the worker reported done"
    else:
        phase, since, why = 2, times[2], "every check on the PR passed; the worker is still at work"
    if since is None:
        since = a.get("phase_since") if a.get("phase") == phase else times[phase]
    if phase >= 3 and since is not None:
        times[phase] = since
    a.update(phase=phase, phase_since=since, phase_why=why, milestones=times)
    return a


class ChecksCache:
    """PR state and checks, read from the forge at most every CHECKS_TTL s per PR: when a detail is
    opened (get), and for every ship task with its PR open (watch), which never waits for the forge."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.busy = set()                    # PRs being read in the background now

    def read(self, cfg, url):
        now, val = time.time(), {"error": "unreadable answer from gh"}
        try:
            rc, out, err = run([cfg.gh_cmd, "pr", "view", url, "--json", "state,isDraft,statusCheckRollup"], timeout=20)
            val = summarize_checks(json.loads(out)) if rc == 0 else {"error": (err or out).strip()[:200] or "exit %d" % rc}
        except (ValueError, AttributeError, OSError):
            pass
        finally:                                 # the answer lands with the read marked done, together
            with self.lock:
                self.data[url] = (now, val)
                self.busy.discard(url)
        return val

    def get(self, cfg, url):
        if not RE_GITHUB_PR.match(url or ""):
            return {"error": "checks are only read for GitHub pull requests"}
        now = time.time()
        with self.lock:
            hit = self.data.get(url)
            if hit and now - hit[0] < CHECKS_TTL:
                return dict(hit[1], age=int(now - hit[0]))
        return dict(self.read(cfg, url), age=0)

    def watch(self, cfg, url):
        """The latest read of a PR's checks, at once: when it is missing or older than CHECKS_TTL, a
        new read starts in the background and a later snapshot sees it. None until a first read."""
        if not RE_GITHUB_PR.match(url or ""):
            return None
        now = time.time()
        with self.lock:
            hit = self.data.get(url)
            start = (not hit or now - hit[0] >= CHECKS_TTL) and url not in self.busy
            if start:
                self.busy.add(url)
        if start:
            threading.Thread(target=self.read, args=(cfg, url), daemon=True).start()
        return dict(hit[1], age=int(now - hit[0])) if hit else None


# ---------------------------------------------------------------- clearing firstmate's context
#
# "Clear context…" in the sidebar runs three steps, in this order, and never skips one:
#   1. save: a note in firstmate's inbox (`bin/fm-inbox.sh note --request-id shipmate-clear:<form id>`)
#      asks it to run /stow and to reply to that note with CLEAR_READY once the stow is done;
#   2. clear: only after that reply, within the timeout, while firstmate's pane is idle and nothing is
#      in flight, shipmate types /clear into the pane with `bin/fm-send.sh <herdr session>:<pane> /clear`,
#      the home's verified submit (empty composer first, then Enter until the pane confirms it);
#   3. resume: the new session starts with firstmate's session-start re-emit, which reloads the queue,
#      the workers, the open decisions and memory. shipmate takes no part in it.
# Without the reply in time, or with a guard failing when the moment comes, nothing is typed.

CLEAR_PREFIX = "shipmate-clear:"        # the request's request id: shipmate-clear:<form id>
CLEAR_READY = "stowed: ready to clear"  # firstmate's reply when the stow is done
CLEAR_NOT_READY = "not ready:"          # firstmate's reply when it cannot be cleared now, with the reason
DEFAULT_CLEAR_TIMEOUT = 900             # seconds firstmate has to stow and reply
CLEAR_IDLE_WAIT = 180                   # seconds the pane has, after the reply, to end its turn
CLEAR_POLL = 3.0
CLEAR_KEEP = 1800                       # seconds a finished request stays on the page
CLEAR_ACTIVE = ("stowing", "waiting", "clearing")
CLEAR_FREE = ("idle", "done")           # herdr agent states of a pane that is not in a turn


def read_lock_pid(home):
    """Line 1 of state/.lock: the pid of the harness that holds this home's firstmate lock."""
    try:
        with open(os.path.join(home, "state", ".lock"), encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return None
    return int(line) if line.isdigit() and int(line) > 1 else None


def herdr(cfg, *args):
    """(rc, JSON or None, error) of one read-only herdr call in the configured session."""
    env = dict(os.environ, HERDR_SESSION=cfg.herdr_session)
    rc, out, err = run([cfg.herdr_cmd] + list(args) + ["--session", cfg.herdr_session], env=env, timeout=15)
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    return rc, doc if isinstance(doc, dict) else None, (err or out).strip()[:300] or "exit %d" % rc


def firstmate_pane(cfg):
    """Firstmate's own herdr pane as {"pane", "target", "status"}, or {"error"}. Two records of the
    home name it, so nothing is guessed: herdr's agent registered for the session id in
    state/.lock-session, in this home's folder, whose pane runs the pid in state/.lock in its
    foreground. Only herdr reads are made; nothing here starts, stops or types anything."""
    sid, pid = read_lock_session(cfg.home), read_lock_pid(cfg.home)
    if not sid or not pid:
        return {"error": tr("the home records no firstmate session lock (state/.lock and state/.lock-session)")}
    rc, doc, err = herdr(cfg, "agent", "list")
    agents = ((doc or {}).get("result") or {}).get("agents")
    if rc != 0 or not isinstance(agents, list):
        return {"error": tr("herdr did not list its panes: %s") % err}
    hits = [a for a in agents if isinstance(a, dict) and ((a.get("agent_session") or {}).get("value") == sid)]
    if not hits:
        return {"error": tr("herdr has no pane for firstmate's session %s") % sid[:8]}
    if len(hits) > 1:
        return {"error": tr("herdr has %d panes for firstmate's session %s; shipmate does not pick one") % (len(hits), sid[:8])}
    a, pane = hits[0], str(hits[0].get("pane_id") or "")
    if not pane or os.path.realpath(a.get("cwd") or "/nonexistent") != os.path.realpath(cfg.home):
        return {"error": tr("the pane herdr names for session %s is not in this home") % sid[:8]}
    rc, doc, err = herdr(cfg, "pane", "process-info", "--pane", pane)
    info = ((doc or {}).get("result") or {}).get("process_info") or {}
    if rc != 0 or info.get("pane_id") != pane:
        return {"error": tr("herdr did not describe pane %s: %s") % (pane, err)}
    pids = {p.get("pid") for p in info.get("foreground_processes") or [] if isinstance(p, dict)}
    if pid not in pids | {info.get("foreground_process_group_id")}:
        return {"error": tr("pane %s does not run the process that holds firstmate's lock (pid %d)") % (pane, pid)}
    return {"pane": pane, "target": "%s:%s" % (cfg.herdr_session, pane), "status": str(a.get("agent_status") or "unknown")}


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def lock_holder_alive(path):
    """Whether a live process holds a firstmate lock (a symlink to, or a directory with, `pid` and
    `pid-start`). Like the home's own check, a recorded start time that a live pid no longer has
    frees it (the pid was reused), and a start time that cannot be read now keeps it held."""
    owner = os.path.realpath(path)
    try:
        with open(os.path.join(owner, "pid"), encoding="utf-8") as fh:
            pid = int(fh.readline().strip())
    except (OSError, ValueError):
        return False
    if pid <= 1 or not pid_alive(pid):
        return False
    try:
        with open(os.path.join(owner, "pid-start"), encoding="utf-8") as fh:
            recorded = fh.readline().strip()
    except OSError:
        return True
    if not recorded or recorded.startswith("proc-starttime="):
        return True
    rc, out, _err = run(["ps", "-p", str(pid), "-o", "lstart="], env=dict(os.environ, LC_ALL="C", TZ="UTC0"), timeout=5)
    return rc != 0 or not out.strip() or out.strip() == recorded


def held_locks(home):
    """What a live firstmate control lock holds now: a task id for `state/.control-<task>.lock`,
    which a merge (`bin/fm-merge-local.sh`, `bin/fm-pr-merge.sh`) and every other control action on
    that task hold, and "away" for `state/.afk-contract.lock`, which a forge merge also holds."""
    state = os.path.join(home, "state")
    out = []
    for path in sorted(glob.glob(os.path.join(state, ".control-*.lock"))) + [os.path.join(state, ".afk-contract.lock")]:
        if os.path.lexists(path) and lock_holder_alive(path):
            name = os.path.basename(path)
            out.append(name[len(".control-"):-len(".lock")] if name.startswith(".control-") else "away")
    return out


def captain_questions(s):
    """Ids of the items whose question waits on the captain: an open needs-decision line, or a
    queued task held for the captain's decision."""
    ids = [a["id"] for a in s.get("agents") or [] if any(d["state"] == "needs-decision" for d in a.get("decisions") or [])]
    return ids + [q["id"] for q in s.get("queue") or [] if any(r[0] == "decision" for r in q.get("reasons") or [])]


def clear_blockers(s, doc, receipts_err, locks, own_note=None):
    """Why firstmate's context must not be cleared now: what is in flight with it. Empty when nothing is."""
    why = []
    if s.get("firstmate") is None:
        why.append(tr("firstmate's session is unknown"))
    asks = captain_questions(s)
    if asks:
        why.append(tr("%d question waits for the captain's answer: %s" if len(asks) == 1
                      else "%d questions wait for the captain's answer: %s") % (len(asks), ", ".join(asks[:6])))
    if doc is None:
        why.append(tr("firstmate's inbox could not be read: %s") % receipts_err)
    else:
        notes = [n.get("id") or "?" for n in doc.get("pending") or [] if isinstance(n, dict) and n.get("id") != own_note]
        if notes:
            why.append(tr("%d note waits in firstmate's inbox: %s" if len(notes) == 1
                          else "%d notes wait in firstmate's inbox: %s") % (len(notes), ", ".join(notes[:6])))
    if locks:
        why.append(tr("a merge or another control action is under way: %s") % ", ".join(locks))
    return why


def clear_note(fm, now, timeout=DEFAULT_CLEAR_TIMEOUT):
    """The request firstmate reads in its inbox, in English whatever the page's language."""
    size = "%s of %s tokens" % (fmt_tokens(fm["context"]), fmt_tokens(fm["limit"])) if fm and fm.get("context") else "unknown"
    return "\n".join([
        "Clear request from shipmate: the captain asked to clear firstmate's context (%s, confirmed %s)." % (size, utc_text(now)),
        "1. Run /stow now: persist every decision, open work item and learning that exists only in this conversation.",
        "2. When the stow is done and nothing must not be interrupted, reply to this note with exactly:",
        "   bin/fm-inbox.sh reply <this note's id> '%s'" % CLEAR_READY,
        "   and end your turn. shipmate then types /clear into this session's pane; the session-start re-emit reloads"
        " the queue, the workers, the open decisions and memory.",
        "3. If it must not be cleared now, reply '%s <reason>' instead; shipmate then types nothing." % CLEAR_NOT_READY,
        "Without the reply within %d minutes nothing is typed." % max(1, round(timeout / 60)),
    ])


def note_reply(doc, note_id):
    """The reply recorded against a note in the receipts, as text, or None."""
    for n in (doc.get("pending") or []) + (doc.get("handled") or []):
        if isinstance(n, dict) and n.get("id") == note_id and isinstance(n.get("reply"), dict):
            return str(n["reply"].get("body") or "")
    return None


class ClearFlow:
    """One clear request at a time: the save, the wait for firstmate's reply, and the /clear, driven by
    a thread of its own so closing the page changes nothing. Its state lives here only: after a restart
    shipmate never acts on an earlier request's reply."""

    def __init__(self, panel, timeout=DEFAULT_CLEAR_TIMEOUT, idle_wait=CLEAR_IDLE_WAIT, poll=CLEAR_POLL):
        self.panel, self.cfg = panel, panel.cfg
        self.timeout, self.idle_wait, self.poll = timeout, idle_wait, poll
        self.lock = threading.Lock()
        self.starting = threading.Lock()
        self.job = None
        self.thread = None
        self.stop = threading.Event()

    def view(self):
        with self.lock:
            j = self.job
            if j is None or (j["state"] not in CLEAR_ACTIVE and time.time() - j["ended"] > CLEAR_KEEP):
                return None
            return {k: v for k, v in j.items()}

    def check(self):
        """(snapshot, pane, blockers) read fresh: nothing cached decides a clear."""
        self.panel.drop()
        self.panel.inbox.drop()
        snap = self.panel.snapshot()
        pane = firstmate_pane(self.cfg)
        doc, err = read_receipts(self.cfg)
        with self.lock:
            own = self.job["note"] if self.job and self.job["state"] in CLEAR_ACTIVE else None
        return snap, pane, doc, clear_blockers(snap, doc, err, held_locks(self.cfg.home), own)

    def start(self, form_id):
        with self.starting:
            with self.lock:
                j = self.job
                if j and j["request"] == form_id:          # the same form sent again is the same request
                    return {"ok": True, "message": clear_message(j)}
                if j and j["state"] in CLEAR_ACTIVE:
                    return {"ok": False, "error": tr("a clear request is already under way")}
            snap, pane, _doc, why = self.check()
            if "error" in pane:
                why.append(pane["error"])
            if why:
                return {"ok": False, "error": tr("Not now: %s") % "; ".join(why)}
            now = time.time()
            rc, out, err = run([self.cfg.bin("fm-inbox.sh"), "note", "--request-id", CLEAR_PREFIX + form_id, "--json", "--",
                                clear_note(snap["firstmate"], now, self.timeout)],
                               env=self.cfg.env(), timeout=60)
            try:
                res = json.loads(out)
            except ValueError:
                res = None
            self.panel.inbox.drop()
            self.panel.drop()
            if not isinstance(res, dict) or rc not in (0, 3) or not res.get("saved") or not res.get("id"):
                return {"ok": False, "error": tr("the inbox refused it: %s") % ((err or out).strip()[:300] or "exit %d" % rc)}
            job = {"request": form_id, "note": res["id"], "started": now, "ended": 0.0, "state": "stowing",
                   "detail": "" if rc == 0 else tr("saved, but firstmate was not woken; it sees the request at its next drain"),
                   "context": (snap["firstmate"] or {}).get("context"), "pane": pane["target"],
                   "session": read_lock_session(self.cfg.home)}
            with self.lock:
                self.job = job
            self.stop.clear()
            self.thread = threading.Thread(target=self.drive, args=(job,), daemon=True)
            self.thread.start()
            return {"ok": True, "message": clear_message(job)}

    def cancel(self):
        with self.lock:
            j = self.job
            if not j or j["state"] not in ("stowing", "waiting"):
                return {"ok": False, "error": tr("no clear request is waiting")}
            self.finish(j, "cancelled", "", locked=True)
        self.stop.set()
        return {"ok": True, "message": clear_message(j)}

    def advance(self, job, state, detail=""):
        with self.lock:
            if job["state"] not in CLEAR_ACTIVE:        # cancelled meanwhile: the request stays where it ended
                return False
            job["state"], job["detail"] = state, detail
            return True

    def finish(self, job, state, detail, locked=False):
        if not locked:
            with self.lock:
                return self.finish(job, state, detail, locked=True)
        if job["state"] in CLEAR_ACTIVE:
            job["state"], job["detail"], job["ended"] = state, detail, time.time()
        return True

    def live(self, job):
        with self.lock:
            return job["state"] in CLEAR_ACTIVE

    def drive(self, job):
        try:
            self.steps(job)
        except Exception as exc:                          # the request must end, never hang as under way
            self.finish(job, "failed", str(exc)[:300])

    def steps(self, job):
        # 1. the stow: wait for firstmate's reply to the request
        reply = None
        while reply is None:
            if not self.live(job):
                return
            if time.time() - job["started"] > self.timeout:
                return self.finish(job, "timeout", tr("no reply from firstmate within %d min") % max(1, round(self.timeout / 60)))
            doc, _err = read_receipts(self.cfg)
            reply = note_reply(doc, job["note"]) if doc else None
            if reply is None:
                self.stop.wait(self.poll)
        text = reply.strip().strip("'\"`").strip()
        if text.lower().startswith(CLEAR_NOT_READY):
            return self.finish(job, "refused", tr("firstmate replied: %s") % text[:300])
        if not text.lower().startswith(CLEAR_READY):
            return self.finish(job, "refused", tr("firstmate replied without \"%s\": %s") % (CLEAR_READY, text[:300]))
        # 2. the clear: once the pane is out of its turn, and only while nothing is in flight
        if not self.advance(job, "waiting"):
            return
        since = time.time()
        while True:
            if not self.live(job):
                return
            pane = firstmate_pane(self.cfg)
            if "error" in pane:
                return self.finish(job, "failed", pane["error"])
            if pane["status"] in CLEAR_FREE:
                break
            if time.time() - since > self.idle_wait:
                return self.finish(job, "refused", tr("firstmate's pane stayed busy (%s) for %d s after the reply")
                                   % (pane["status"], self.idle_wait))
            self.stop.wait(self.poll)
        _snap, pane2, _doc, why = self.check()
        if read_lock_session(self.cfg.home) != job["session"]:       # a session that never stowed is not cleared
            why.append(tr("firstmate's session changed since the request"))
        if "error" in pane2 or pane2["target"] != job["pane"]:
            why.append(pane2.get("error") or tr("firstmate's pane changed"))
        elif pane2["status"] not in CLEAR_FREE:
            why.append(tr("firstmate's pane is busy (%s)") % pane2["status"])
        if why:
            return self.finish(job, "refused", "; ".join(why))
        if not self.advance(job, "clearing"):
            return
        rc, out, err = run([self.cfg.bin("fm-send.sh"), pane["target"], "/clear"], env=self.cfg.env(), timeout=120)
        self.panel.drop()
        if rc == 0:
            return self.finish(job, "cleared", "")
        if rc == 3:
            return self.finish(job, "unconfirmed", (err or out).strip()[:300])
        return self.finish(job, "failed", tr("fm-send refused: %s") % ((err or out).strip()[:300] or "exit %d" % rc))


CLEAR_TEXT = {
    "stowing": "Saving: firstmate was asked to /stow; waiting for its reply.",
    "waiting": "Saved: waiting for firstmate's turn to end before typing /clear.",
    "clearing": "Typing /clear into firstmate's pane.",
    "cleared": "Cleared: /clear reached firstmate's pane; the new session reloads its state.",
    "unconfirmed": "/clear was typed, but the pane did not confirm it: look at firstmate's pane before trying again.",
    "refused": "Not cleared, and nothing was typed.",
    "timeout": "Not cleared, and nothing was typed.",
    "failed": "Not cleared.",
    "cancelled": "Stopped waiting: nothing was typed. firstmate may still finish the stow, which keeps what it saves.",
}
CLEAR_TONE = {"stowing": "info", "waiting": "info", "clearing": "info", "cleared": "online", "unconfirmed": "warn",
              "refused": "danger", "timeout": "danger", "failed": "danger", "cancelled": "neutral"}


def clear_message(j):
    return tr(CLEAR_TEXT[j["state"]]) + ((" " + j["detail"]) if j.get("detail") else "")


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
        self.inbox = ReceiptsCache()
        self.lanes = LanesCache()
        self.last = None                         # latest snapshot, reused by the detail view while fresh
        self.built_at = 0.0                      # monotonic time of the reading in self.last
        self.built_gen = -1                      # the generation that reading started under
        self.gen = 0                             # bumped by drop(); a reading from an older one is never reused
        self.doctoring = threading.Lock()        # one `claude doctor` at a time
        self.doctored = {}                       # account label -> the last doctor run's result, shown beside it
        self.clear = ClearFlow(self, cfg.clear_timeout)

    def drop(self):
        """Forgets the cached reading: the next request reads the fleet again, even when a reading
        already under way began before the change that called this."""
        self.gen += 1

    def snapshot(self):
        """The fleet as one snapshot. Requests within cache_seconds of a reading share it, so reloads
        and the page's own refresh do not re-read the backlog, the records and the inbox each time;
        a reply the captain just sent drops it (see drop), so it is never answered with stale data."""
        with self.lock:
            ttl = self.cfg.cache_seconds
            if (ttl > 0 and self.last is not None and self.built_gen == self.gen
                    and time.monotonic() - self.built_at < ttl):
                return self.last
            gen = self.gen
            snap = self.read_fleet()
            self.built_at, self.built_gen = time.monotonic(), gen
            return snap

    def read_fleet(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            now = time.time()
            quota = ex.submit(self.quota.get, self.cfg)   # the slowest read runs beside the others
            backlog, berr = read_backlog(self.cfg)
            by_id = {r["id"]: r for r in (backlog or [])}
            snap = {
                "now": now,
                "firstmate": firstmate_session(self.cfg, self.index, now),
                "agents": live_agents(self.cfg, self.index, self.crew, by_id),
                "queue": queue_rows(self.cfg, backlog, self.lanes.get(self.cfg)),
                "backlog_error": berr,
                "projects": read_projects(self.cfg.home),
                "accounts": [account_label(path) for _line, path in self.cfg.accounts],
                "daily": daily_usage(self.cfg, self.index, now),
                "delivered": delivered(self.cfg, self.index, by_id, now),
                "closed": closed_rows(backlog, now),
                "machine": machine_load(),
            }
            for a in snap["agents"]:                       # with the PR open, the phase follows its checks
                with_pr = a["kind"] == "ship" and a["phase"] in (2, 3, 4) and a["pr"]
                phase_by_checks(a, self.checks.watch(self.cfg, a["pr"]) if with_pr else None)
            snap["answers"], snap["answers_error"] = self.inbox.get(self.cfg)
            snap["quota"] = quota.result()
            snap["doctor"] = {"accounts": sorted(doctor_accounts(self.cfg)), "results": dict(self.doctored)}
            self.last = snap
            return snap

    def doctor(self, label):
        """`claude doctor` for one account of the home, then a fresh quota reading for the page."""
        if not self.doctoring.acquire(blocking=False):
            return {"ok": False, "error": tr("claude doctor is already running for an account; try again when it ends")}
        try:
            res = run_doctor(self.cfg, label)
        finally:
            self.doctoring.release()
        if label in doctor_accounts(self.cfg):
            self.doctored[label] = dict(res, at=time.time())
            self.quota.forget()
            self.drop()
        return res

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
        answers, answers_err = self.inbox.get(self.cfg)       # fresher than the snapshot right after a send
        answer = answer_section(tid, captain_asks(snap, tid) or [], answers.get(tid, []), self.token, now, answers_err)
        agent = next((a for a in snap["agents"] if a["id"] == tid), None)
        if agent is not None:
            status = read_status(os.path.join(self.cfg.home, "state", tid + ".status"))[-STATUS_TAIL:]
            checks = self.checks.get(self.cfg, agent["pr"]) if agent["pr"] else None
            return render_agent_detail(agent, captain_intent(self.cfg.home, tid), status, checks,
                                       report_files(self.cfg.home, tid), now, answer)
        item = next((q for q in snap["queue"] if q["id"] == tid), None)
        if item is not None:
            rc, out, _err = run([self.cfg.bin("fm-tasks-axi.sh"), "show", tid, "--full"], env=self.cfg.env())
            full = parse_show(out) if rc == 0 else {}
            return render_queue_detail(item, full, captain_intent(self.cfg.home, tid),
                                       report_files(self.cfg.home, tid), now, answer)
        return None

    def answer(self, tid, ref, option, words, form_id):
        """Sends the captain's answer to one waiting decision as an inbox note. The request id comes
        from the form, so sending the same form again (a double click, a retry after an error) gives
        back the note it already made instead of a second one."""
        prep = prepare_answer(self.recent(), tid, ref, option, words)
        if "error" in prep:
            return {"ok": False, "error": prep["error"]}
        rc, out, err = run([self.cfg.bin("fm-inbox.sh"), "note", "--request-id", answer_request_id(form_id, tid),
                            "--json", "--", prep["body"]], env=self.cfg.env(), timeout=60)
        self.inbox.drop()
        self.drop()
        try:
            res = json.loads(out)
        except ValueError:
            res = None
        if not isinstance(res, dict) or rc not in (0, 3) or not res.get("saved"):
            return {"ok": False, "error": tr("the inbox refused it: %s") % ((err or out).strip()[:300] or "exit %d" % rc)}
        state = "received" if res.get("acknowledged") else "sent" if rc == 0 else "unwoken"
        return {"ok": True, "state": state, "note": res.get("id"), "message": answer_message(state)}

    def announce(self, note_id):
        """Wakes firstmate for an answer saved without its wake; only a note shipmate sent as an answer."""
        answers, _err = self.inbox.get(self.cfg)
        if not any(x["note"] == note_id for rows in answers.values() for x in rows):
            return {"ok": False, "error": tr("no answer from shipmate has that note id")}
        rc, out, err = run([self.cfg.bin("fm-inbox.sh"), "announce", "--json", note_id], env=self.cfg.env(), timeout=60)
        self.inbox.drop()
        self.drop()
        if rc != 0:
            return {"ok": False, "error": tr("firstmate was not woken: %s") % ((err or out).strip()[:300] or "exit %d" % rc)}
        return {"ok": True, "message": tr("Firstmate was woken.")}

    def clear_view(self):
        return self.clear.view()

    def clear_drawer(self):
        """The drawer of "Clear context": what it does, the context it clears, and why not now, if so."""
        job = self.clear.view()
        if job and job["state"] in CLEAR_ACTIVE:
            return render_clear(self.recent().get("firstmate"), job, None, [], self.token, time.time())
        snap, pane, _doc, why = self.clear.check()
        return render_clear(snap.get("firstmate"), job, pane, why, self.token, time.time())

    def clear_start(self, form_id):
        return self.clear.start(form_id)

    def clear_cancel(self):
        return self.clear.cancel()

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
            self.drop()
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
    "waiting for CI": "esperando o CI", "waiting for merge / delivered": "esperando o merge / entregue", "for %s": "há %s",
    "phase start unknown": "início da fase desconhecido", "%s new · %s cache": "%s novos · %s cache",
    "tokens unknown": "tokens desconhecidos", "%s: details": "%s: detalhes", "Queue": "Fila", "phase unknown": "fase desconhecida",
    "Queue unavailable: %s": "Fila indisponível: %s", "Nothing waiting.": "Nada esperando.", "+ Add task": "+ Adicionar tarefa",
    "%s: empty": "%s: vazia", "%s: nothing here": "%s: nada aqui", "%d agent · %d in the queue": "%d agente · %d na fila",
    "%d agents · %d in the queue": "%d agentes · %d na fila",
    # crew states and phases
    "working": "trabalhando", "parked": "parado", "blocked": "bloqueado", "failed": "falhou", "done": "concluído",
    "paused": "pausado", "implementing": "implementando", "waiting for merge": "esperando o merge",
    "writing the report": "escrevendo o relatório", "report delivered": "relatório entregue",
    # queue reasons
    "held until %s": "segura até %s", "captain's decision": "decisão do capitão", "waits for %s": "espera %s",
    "build slot %d/%d": "vaga de build %d/%d", "build slots for %s: %d of %d taken (%s)": "vagas de build de %s: %d de %d ocupadas (%s)",
    "ready to dispatch": "pronta para despachar", "nothing holds it: firstmate can dispatch it": "nada a segura: o firstmate pode despachá-la",
    "home lanes %d/%d": "vagas da casa %d/%d",
    "no free lane in this home (%s): %d of %d in use": "nenhuma vaga livre nesta casa (%s): %d de %d em uso",
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
    "on deck with a mop": "no convés com o esfregão", "on deck with a broom": "no convés com a vassoura",
    "on deck with a hammer, a mop or a broom": "no convés com o martelo, o esfregão ou a vassoura",
    "at the bow, signalling with flags": "na proa, sinalizando com bandeiras",
    "at the bow with a spyglass on the lighthouse: the checks are running": "na proa, de luneta no farol: os checks estão rodando",
    "fishing at the rail: waiting for the merge": "pescando na amurada: esperando o merge",
    "on deck, phase unknown": "no convés, fase desconhecida", "rowing out to sea": "remando mar adentro",
    "rowing back with the map": "voltando a remo com o mapa", "moored at the pier with the map": "atracado no cais com o mapa",
    "crew is %s": "estado da tripulação: %s", "%d hand aboard": "%d marujo a bordo", "%d hands aboard": "%d marujos a bordo",
    "%d red flag": "%d bandeira vermelha", "%d red flags": "%d bandeiras vermelhas", "%d off duty": "%d de folga",
    "%d in the queue": "%d na fila", "today": "hoje",
    "no project": "sem projeto", "; off duty": "; de folga", "; needs the captain: ": "; precisa do capitão: ",
    "; number %d in line for the captain, waiting %s": "; número %d na fila do capitão, esperando há %s",
    "%d more in line at the door, numbers %d to %d: %s. Click to find them in the log.":
        "mais %d na fila à porta, números %d a %d: %s. Clique para achá-los no diário.",
    "the captain, off duty: nothing waits on a decision": "o capitão, de folga: nada espera uma decisão",
    "the captain: %d waiting, the longest %s for %s": "o capitão: %d esperando, o mais antigo %s há %s",
    "%d waiting for you": "%d esperando você", "longest: %s, %s": "mais antigo: %s, %s",
    "LOOKOUT · exploring": "GÁVEA · explorando", "HELM · firstmate": "LEME · firstmate", "CAPTAIN'S CABIN": "CABINE DO CAPITÃO",
    "DECK · implementing": "CONVÉS · implementando", "? · phase unknown": "? · fase desconhecida",
    "RAIL · waiting for merge": "AMURADA · esperando o merge", "BOW · PR open and CI": "PROA · PR aberto e CI",
    "HOLD · quota barrels, context water": "PORÃO · barris de cota, água de contexto",
    "GANGPLANK · ashore and aboard": "PRANCHA · desembarque e embarque", "LIGHTHOUSE · CI": "FAROL · CI",
    "SCOUTS": "BATEDORES", "DELIVERED · %d days": "ENTREGUES · %d dias",
    "SLOT %s · %d/%d taken · %d waiting": "VAGA %s · %d/%d ocupadas · %d esperando",
    "HOME LANES · %d/%d in use · %d waiting": "VAGAS DA CASA · %d/%d em uso · %d esperando",
    "READY · boards next": "PRONTAS · embarcam em seguida",
    "WAITS FOR A TASK": "ESPERAM UMA TAREFA", "QUARTERS · asleep until a date": "ALOJAMENTO · dormem até a data",
    "%s waits for %s": "%s espera %s",
    "playing cards on the pier: queued today, waits for a free build slot or lane":
        "jogando cartas no cais: entrou na fila hoje, espera uma vaga de build ou da casa",
    "at the tavern bench with a mug: queued before today, waits for a free build slot or lane":
        "no banco da taverna com uma caneca: entrou na fila antes de hoje, espera uma vaga de build ou da casa",
    "at the foot of the gangplank with a sea bag: nothing holds it, ready to board":
        "ao pé da prancha com a bolsa: nada a segura, pronta para embarcar",
    "on the pier, talking up to the hand it waits for": "no cais, conversando com o marujo que ela espera",
    "on the pier: waits for a task that is not aboard": "no cais: espera uma tarefa que não está a bordo",
    "asleep in a hammock in the quarters below deck: held until its date": "dormindo numa rede no alojamento abaixo do convés: segura até a data",
    "the quarters below deck: queued tasks held until a date sleep here, the earliest date aft":
        "o alojamento abaixo do convés: aqui dormem as tarefas da fila seguras até uma data, a data mais próxima mais à popa",
    "in the captain's cabin with a sea bag: queued, waits for your decision":
        "na cabine do capitão com a bolsa: na fila, espera a sua decisão",
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
    "on deck with a hammer, a mop or a broom: making the change": "no convés com o martelo, o esfregão ou a vassoura: fazendo a mudança",
    "at the bow with signal flags: the pull request is open": "na proa com as bandeiras de sinal: o pull request está aberto",
    "at the bow with a spyglass on the lighthouse: the checks are running, and the light blinks yellow; it flashes green "
    "or red when they come back":
        "na proa, de luneta no farol: os checks estão rodando, e a luz pisca amarelo; ela acende verde ou vermelho quando "
        "eles voltam",
    "fishing at the rail: every check passed, the merge is next; one more fish in the bucket for each hour it waits, up "
    "to four":
        "pescando na amurada: todos os checks passaram, o próximo passo é o merge; mais um peixe no balde a cada hora de "
        "espera, até quatro",
    "under a question mark: no sign of its phase yet": "sob um ponto de interrogação: ainda sem sinal da fase",
    "in a skiff: rowing out to explore, back with the map, then moored at the pier with the report":
        "num bote: rema para explorar, volta com o mapa e atraca no cais com o relatório",
    "sitting with the dice: paused, or quiet for a while": "sentado com os dados: pausado, ou quieto há um tempo",
    "waiting for a slot": "esperando vaga", "ready to board": "pronta para embarcar", "waiting for a task": "esperando uma tarefa",
    "held until a date": "segura até uma data",
    "at a card table on the pier, or at the tavern bench when queued before today: waits for a free build slot or lane":
        "numa mesa de cartas no cais, ou no banco da taverna quando entrou na fila antes de hoje: espera uma vaga de build "
        "ou da casa",
    "at the foot of the gangplank with a sea bag: queued, and nothing holds it": "ao pé da prancha com a bolsa: na fila, e nada a segura",
    "on the pier talking up to the hand it waits for, or standing apart when that task is not aboard":
        "no cais conversando com o marujo que ela espera, ou de pé à parte quando essa tarefa não está a bordo",
    "asleep in a hammock in the quarters below deck: queued and held until its date":
        "dormindo numa rede no alojamento abaixo do convés: na fila e segura até a data",
    "a red flag: needs your decision; hands and queued tasks wait in your cabin, longest wait first, the rest crowd "
    "the door, and the log below lists them all":
        "uma bandeira vermelha: precisa da sua decisão; marujos e tarefas da fila esperam na sua cabine, quem espera há "
        "mais tempo primeiro, os demais se juntam na porta, e o diário abaixo lista todos",
    "%s: %s. %s": "%s: %s. %s", "Now: %s.": "Agora: %s.", "Nobody now.": "Ninguém agora.",
    "%d waits on my word. The longest, %s, has waited %s.": "%d espera a minha palavra. O mais antigo, %s, espera há %s.",
    "%d wait on my word. The longest, %s, has waited %s.": "%d esperam a minha palavra. O mais antigo, %s, espera há %s.",
    "queued, %s": "na fila, %s", "All clear. Nothing waits on my word.": "Tudo em ordem. Nada espera a minha palavra.",
    "Nobody is waiting for you.": "Ninguém está esperando você.", "Captain": "Capitão",
    "%s: one fish, waiting for the merge for %s": "%s: um peixe, esperando o merge há %s",
    "%s: %d fish, waiting for the merge for %s": "%s: %d peixes, esperando o merge há %s",
    "the lighthouse: the checks are running for %s": "o farol: os checks estão rodando para %s",
    "the lighthouse: nobody is waiting for CI": "o farol: ninguém esperando o CI",
    "the bilge pump: it works when firstmate's context is compacted": "a bomba do porão: trabalha quando o contexto do firstmate é compactado",
    "every check passed": "todos os checks passaram", "a check failed": "um check falhou", "checks running": "checks rodando",
    # clearing firstmate's context
    "Clear context…": "Limpar o contexto…", "Clear firstmate's context": "Limpar o contexto do firstmate",
    "Checking what is in flight…": "Vendo o que está em andamento…", "Session": "Sessão", "Pane": "Pane",
    "not found": "não encontrado", "%s of %s (%s window)": "%s de %s (janela %s)", "asked %s": "pedido %s",
    "Save, then clear %s": "Salvar e limpar %s", "Stop waiting": "Parar de esperar",
    "Stopping only stops shipmate: firstmate may still finish the stow.":
        "Parar só para o shipmate: o firstmate ainda pode terminar o stow.",
    "Not now: something is in flight with firstmate.": "Agora não: há algo em andamento com o firstmate.",
    "Not now: %s": "Agora não: %s",
    "Not sent: shipmate could not be reached. Try again: the same request is never sent twice.":
        "Não enviado: o shipmate não respondeu. Tente de novo: o mesmo pedido nunca é enviado duas vezes.",
    "Workers run in sessions of their own: clearing firstmate does not interrupt them. Nothing needs undoing: what firstmate keeps is on disk, and the new session reads it back.":
        "Os workers rodam em sessões próprias: limpar o firstmate não os interrompe. Não há nada a desfazer: o que o firstmate guarda está em disco, e a nova sessão lê de volta.",
    "Save: shipmate asks firstmate, in its inbox, to run /stow, which writes the decisions, open work and learnings that exist only in the conversation to the home's files.":
        "Salvar: o shipmate pede ao firstmate, pela inbox, que rode /stow, que grava nos arquivos da home as decisões, o trabalho em aberto e os aprendizados que só existem na conversa.",
    "Clear: only after firstmate replies that the stow is done, and only while its pane is idle and nothing is in flight, shipmate types /clear into that pane.":
        "Limpar: só depois que o firstmate responde que o stow terminou, e só com o pane dele ocioso e nada em andamento, o shipmate digita /clear nesse pane.",
    "Resume: the new session starts with firstmate's session-start re-emit, which reloads the queue, the workers, the open decisions and memory.":
        "Retomar: a nova sessão começa com o re-emit de início de sessão do firstmate, que recarrega a fila, os workers, as decisões abertas e a memória.",
    "Saving: firstmate was asked to /stow; waiting for its reply.": "Salvando: o firstmate recebeu o pedido de /stow; esperando a resposta.",
    "Saved: waiting for firstmate's turn to end before typing /clear.": "Salvo: esperando o turno do firstmate terminar para digitar /clear.",
    "Typing /clear into firstmate's pane.": "Digitando /clear no pane do firstmate.",
    "Cleared: /clear reached firstmate's pane; the new session reloads its state.":
        "Limpo: o /clear chegou ao pane do firstmate; a nova sessão recarrega o estado.",
    "/clear was typed, but the pane did not confirm it: look at firstmate's pane before trying again.":
        "O /clear foi digitado, mas o pane não confirmou: olhe o pane do firstmate antes de tentar de novo.",
    "Not cleared, and nothing was typed.": "Não limpo, e nada foi digitado.", "Not cleared.": "Não limpo.",
    "Stopped waiting: nothing was typed. firstmate may still finish the stow, which keeps what it saves.":
        "Parou de esperar: nada foi digitado. O firstmate ainda pode terminar o stow, e o que ele salvar fica.",
    "clear: %s": "limpeza: %s", "saving": "salvando", "saved": "salvo", "clearing": "limpando", "cleared": "limpo",
    "unconfirmed": "não confirmado", "not cleared": "não limpo", "timed out": "sem resposta", "stopped": "parado",
    "a clear request is already under way": "já há um pedido de limpeza em andamento",
    "no clear request is waiting": "nenhum pedido de limpeza está esperando",
    "%d question waits for the captain's answer: %s": "%d pergunta espera a resposta do capitão: %s",
    "%d questions wait for the captain's answer: %s": "%d perguntas esperam a resposta do capitão: %s",
    "%d note waits in firstmate's inbox: %s": "%d nota espera na inbox do firstmate: %s",
    "%d notes wait in firstmate's inbox: %s": "%d notas esperam na inbox do firstmate: %s",
    "a merge or another control action is under way: %s": "um merge ou outra ação de controle está em andamento: %s",
    "firstmate's session is unknown": "a sessão do firstmate é desconhecida",
    "firstmate's inbox could not be read: %s": "a inbox do firstmate não pôde ser lida: %s",
    "firstmate's pane changed": "o pane do firstmate mudou", "firstmate's session changed since the request": "a sessão do firstmate mudou desde o pedido", "firstmate's pane is busy (%s)": "o pane do firstmate está ocupado (%s)",
    "firstmate's pane stayed busy (%s) for %d s after the reply": "o pane do firstmate ficou ocupado (%s) por %d s depois da resposta",
    "firstmate replied: %s": "o firstmate respondeu: %s",
    "firstmate replied without \"%s\": %s": "o firstmate respondeu sem \"%s\": %s",
    "no reply from firstmate within %d min": "nenhuma resposta do firstmate em %d min",
    "saved, but firstmate was not woken; it sees the request at its next drain":
        "gravado, mas o firstmate não foi acordado; ele vê o pedido na próxima leitura da inbox",
    "fm-send refused: %s": "o fm-send recusou: %s",
    "the home records no firstmate session lock (state/.lock and state/.lock-session)":
        "a home não registra o lock de sessão do firstmate (state/.lock e state/.lock-session)",
    "herdr did not list its panes: %s": "o herdr não listou os panes: %s",
    "herdr has no pane for firstmate's session %s": "o herdr não tem pane para a sessão %s do firstmate",
    "herdr has %d panes for firstmate's session %s; shipmate does not pick one":
        "o herdr tem %d panes para a sessão %s do firstmate; o shipmate não escolhe um",
    "the pane herdr names for session %s is not in this home": "o pane que o herdr indica para a sessão %s não é desta home",
    "herdr did not describe pane %s: %s": "o herdr não descreveu o pane %s: %s",
    "pane %s does not run the process that holds firstmate's lock (pid %d)":
        "o pane %s não roda o processo que tem o lock do firstmate (pid %d)",
}

# the page template's own phrases ({{t:...}} and {{j:...}} in shipmate.html)
PT_BR.update({
    "View": "Vista", "Board": "Quadro", "Ship": "Navio", "refresh failed": "falha ao atualizar", "updated": "atualizado",
    "Work by phase": "Trabalho por fase", "The ship": "O navio", "Light a stage": "Acender uma etapa",
    "Click a stage to light its hands and take the view to them.":
        "Clique numa etapa para acender os marujos dela e levar a vista até eles.",
    "Zoom in": "Aproximar", "Zoom out": "Afastar",
    "Show the signs on the ship": "Mostrar as placas no navio",
    "signs": "placas", "Waiting for you": "Esperando você",
    "Wheel or pinch to zoom, drag to move, double click to zoom in there. Click a hand or a card in the log for its details, or the crowd at the cabin door to find it in the log.":
        "Roda ou pinça para o zoom, arraste para mover, clique duplo para aproximar ali. Clique num marujo ou num cartão do "
        "diário para ver os detalhes, ou na aglomeração da porta da cabine para achá-la no diário.",
    "Projects aboard": "Projetos a bordo",
    "Accounts aboard": "Contas a bordo", "no account aboard": "nenhuma conta a bordo",
    "A shirt takes the project's colour, and so do the stripes on the sails; a neckerchief takes the account's.":
        "A camisa tem a cor do projeto, assim como as listras das velas; o lenço do pescoço tem a cor da conta.",
    "The hat says how the hand is: a bandana at work, a straw hat off duty or queued ashore, bare-headed before the captain, a grey wool cap when its phase is unknown.":
        "O chapéu diz como o marujo está: bandana trabalhando, chapéu de palha de folga ou na fila em terra, cabeça "
        "descoberta diante do capitão, gorro cinza quando a fase é desconhecida.",
    "A red flag: the hand needs the captain (its crew is parked, blocked or failed, or a needs-decision or blocked line is still open), or a queued task waits on the captain's decision.":
        "Uma bandeira vermelha: o marujo precisa do capitão (a tripulação está parada, bloqueada ou falhou, ou uma linha "
        "needs-decision ou blocked segue aberta), ou uma tarefa da fila espera a decisão do capitão.",
    "Whoever needs the captain waits in the captain's cabin, numbered by how long it has waited: a hand aboard, or a queued task with its sea bag. Three fit in the cabin; the rest crowd the door with their count, and the log below lists everyone. The captain attends them at the desk, or sits with a mug when nobody waits.":
        "Quem precisa do capitão espera na cabine do capitão, numerado por quanto tempo espera: um marujo a bordo, ou uma "
        "tarefa da fila com a sua bolsa. Cabem três na cabine; os demais se juntam na porta com a contagem, e o diário abaixo "
        "lista todos. O capitão atende à mesa, ou senta com uma caneca quando ninguém espera.",
    "Barrels in the hold: each account's quota, amber what is left, red when it runs out before it resets.":
        "Barris no porão: a cota de cada conta, em âmbar o que resta, em vermelho quando acaba antes de renovar.",
    "Water in the hold: firstmate's context, the white line its warning threshold; the pump works when the context is compacted.":
        "Água no porão: o contexto do firstmate, a linha branca é o limite de aviso; a bomba trabalha quando o contexto é compactado.",
    "The lighthouse ahead of the bow shows CI: it blinks yellow while a hand at the bow watches it through a spyglass, waiting for its checks, flashes green or red for a few seconds when they come back, and is dark when nobody waits. Red sends the hand back to the deck, green to the rail.":
        "O farol na frente da proa mostra o CI: pisca amarelo enquanto um marujo na proa o olha pela luneta, esperando os "
        "checks dele, acende verde ou vermelho por alguns segundos quando eles voltam, e fica apagado quando ninguém espera. "
        "Vermelho manda o marujo de volta ao convés, verde para a amurada.",
    "At the rail a hand fishes while it waits for the merge: a fish in its bucket when it gets there and one more each hour, up to four; point at the bucket for the exact wait. Merged, it goes down the gangplank with its crate to the pile, and a queued task boards up the same gangplank.":
        "Na amurada o marujo pesca enquanto espera o merge: um peixe no balde ao chegar e mais um a cada hora, até quatro; "
        "passe o mouse no balde para ver a espera exata. Com o merge feito, ele desce a prancha com o caixote até a pilha, e "
        "uma tarefa da fila embarca subindo pela mesma prancha.",
    "When a hand changes place between two refreshes, it walks there; the motion button sets how much moves.":
        "Quando um marujo muda de lugar entre duas atualizações, ele anda até lá; o botão de movimento diz quanto se mexe.",
    "How much the ship moves: all, changes only, or nothing": "Quanto o navio se mexe: tudo, só as mudanças ou nada",
    "motion: all": "movimento: tudo", "motion: changes only": "movimento: só mudanças", "motion: off": "movimento: parado",
    "Scouts row out to the horizon, back with the map, and moor at the pier with it.":
        "Os batedores remam até o horizonte, voltam com o mapa e atracam no cais com ele.",
    "On the pier, the queue: cards or a mug at the tavern while it waits for a free slot, a sea bag at the gangplank when nothing holds it, talking up to the hand it waits for.":
        "No cais, a fila: cartas ou uma caneca na taverna enquanto espera uma vaga livre, a bolsa ao pé da prancha quando "
        "nada a segura, conversando com o marujo que ela espera.",
    "Hammocks in the quarters below deck: queued tasks held until a date sleep there, the date over each.":
        "Redes no alojamento abaixo do convés: ali dormem as tarefas da fila seguras até uma data, com a data sobre cada uma.",
    "Crates: tasks delivered in the last 7 days.": "Caixotes: tarefas entregues nos últimos 7 dias.",
    "The tavern's awning has the colour of the project whose slot it waits for, navy for the home's lanes; its key board has a hook per place, each key out with the task that holds it (point at it to light them).":
        "O toldo da taverna tem a cor do projeto cuja vaga ela espera, azul-marinho para as raias da home; o quadro de chaves "
        "tem um gancho por vaga, cada chave fora com a tarefa que a ocupa (aponte para ele para acendê-las).",
    "Clouds, gulls, little waves and the lamps along the pier are only scenery.":
        "Nuvens, gaivotas, ondinhas e os lampiões do cais são só cenário.",
    "The sky follows the local time, darkening over the hour around 18:00 and lightening around 06:00, and the sea the machine's load.":
        "O céu segue a hora local, escurecendo ao longo da hora em volta das 18:00 e clareando em volta das 06:00, e o mar a "
        "carga da máquina.",
    "Usage": "Uso", "per day and per delivered task": "por dia e por tarefa entregue",
    "Firstmate session and accounts": "Sessão do firstmate e contas", "Firstmate session": "Sessão do firstmate",
    "Accounts and quota": "Contas e cota", "Details": "Detalhes", "Close": "Fechar",
    "%d lane out of view, scroll the board sideways": "%d raia fora de vista, role o quadro para o lado",
    "%d lanes out of view, scroll the board sideways": "%d raias fora de vista, role o quadro para o lado",
    "+%d more below": "+%d abaixo", "Loading…": "Carregando…", "Add a task": "Adicionar tarefa",
    "This item is no longer on the board.": "Este item não está mais no quadro.",
})
# the captain's answers
PT_BR.update({
    "no account of this home is called %s": "nenhuma conta deste home se chama %s",
    "could not start %s: %s": "não deu para iniciar %s: %s",
    "claude doctor did not finish in %d s and was stopped": "o claude doctor não terminou em %d s e foi interrompido",
    "claude doctor exited with status %d": "o claude doctor saiu com status %d",
    "claude doctor is already running for an account; try again when it ends":
        "o claude doctor já está rodando para uma conta; tente de novo quando terminar",
    "Run claude doctor for this account so Claude Code renews its token, then read its quota again":
        "Rodar o claude doctor para esta conta, para o Claude Code renovar o token, e ler a cota de novo",
    "claude doctor": "claude doctor",
    "this reading is stale or unknown; its token may have expired": "esta leitura está velha ou desconhecida; o token pode ter expirado",
    "claude doctor ran %s": "claude doctor rodou %s",
    "claude doctor failed %s: %s": "claude doctor falhou %s: %s",
    "this is the demo: claude doctor was not run": "isto é a demo: o claude doctor não foi rodado",
    "Running claude doctor…": "Rodando o claude doctor…",
    "claude doctor failed: %s": "claude doctor falhou: %s",
    "this item is no longer on the board": "este item não está mais no quadro",
    "this decision is no longer open; reopen the drawer": "esta decisão não está mais aberta; reabra a gaveta",
    "that option is not on this decision": "essa opção não é desta decisão",
    "pick an option or write an answer": "escolha uma opção ou escreva uma resposta",
    "the answer is longer than %d characters": "a resposta passa de %d caracteres",
    "the inbox receipts are not JSON": "os recibos da caixa de entrada não são JSON",
    "the inbox refused it: %s": "a caixa de entrada recusou: %s",
    "no answer from shipmate has that note id": "nenhuma resposta do shipmate tem esse id de bilhete",
    "firstmate was not woken: %s": "o firstmate não foi acordado: %s", "Firstmate was woken.": "O firstmate foi acordado.",
    "sent": "enviado", "not woken": "sem aviso", "received": "recebido", "replied": "respondido",
    "no needs-decision or blocked line is open": "nenhuma linha needs-decision ou blocked aberta",
    "waiting since %s": "esperando desde %s", "Ready options": "Opções prontas", "Your answer": "Sua resposta",
    "Add your own words (optional when you pick an option)": "Complemente com suas palavras (opcional quando escolhe uma opção)",
    "Write your answer": "Escreva sua resposta",
    "Goes to firstmate as a note that names %s and this decision.": "Vai para o firstmate como bilhete que nomeia %s e esta decisão.",
    "Send to firstmate": "Enviar ao firstmate", "firstmate · %s": "firstmate · %s", "you · %s": "você · %s",
    "Saved, but firstmate was not woken.": "Gravado, mas o firstmate não foi acordado.", "Wake firstmate": "Acordar o firstmate",
    "Firstmate took it from its inbox and has not replied yet.": "O firstmate tirou da caixa de entrada e ainda não respondeu.",
    "Waiting for firstmate to take it from its inbox.": "Esperando o firstmate tirar da caixa de entrada.",
    "Answers sent": "Respostas enviadas", "Answers unavailable: %s": "Respostas indisponíveis: %s",
    "Firstmate acts on your answer under its usual rules: a merge or a destructive action still needs your explicit word on that PR.":
        "O firstmate age sobre a sua resposta com as regras de sempre: um merge ou uma ação destrutiva ainda precisa da sua "
        "palavra explícita sobre aquela PR.",
    "this form has no request id; reopen the drawer": "este formulário não tem id de envio; reabra a gaveta",
    "Demo: the answer stays on this page; nothing reached firstmate.": "Demo: a resposta fica só nesta página; nada chegou ao firstmate.",
    "this is the demo: firstmate is not here": "isto é a demo: o firstmate não está aqui",
    "Sent: firstmate was woken and will read it at its next check.": "Enviado: o firstmate foi acordado e lê na próxima verificação.",
    "Saved, but firstmate was not woken; wake it from the answer below.":
        "Gravado, mas o firstmate não foi acordado; acorde-o pela resposta abaixo.",
    "Firstmate already has this answer.": "O firstmate já tem esta resposta.",
    "build slots for %s: %d of %d keys are out, with %s; %d wait for any of them to come back":
        "vagas de build de %s: %d de %d chaves estão fora, com %s; %d esperam qualquer uma voltar",
    "home lanes: %d of %d in use; %d wait for any lane to come free":
        "raias da home: %d de %d em uso; %d esperam qualquer raia ficar livre",
    "Sending…": "Enviando…", "Not sent: %s": "Não enviado: %s", "Waking firstmate…": "Acordando o firstmate…",
    "Not sent: shipmate could not be reached. Try again: the same answer is never sent twice.":
        "Não enviado: o shipmate não respondeu. Tente de novo: a mesma resposta nunca vai duas vezes.",
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


def fmt_day_short(s):
    """A YYYY-MM-DD date as day and month, short enough to hang over a hammock: 16 Nov."""
    try:
        d = dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return ""
    return "%02d %s" % (d.day, MONTHS[LANG][d.month - 1])


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


DOCTOR_SHOWN = 15 * 60                   # seconds a doctor run's result stays beside its account


def doctor_line(label, doctor, ln, now):
    """The "claude doctor" button under an account whose reading is stale or unknown, and the last
    run's result for a while after it; nothing for an account the home does not list."""
    if label not in doctor.get("accounts", ()):
        return ""
    res = doctor.get("results", {}).get(label)
    if res and now - res["at"] > DOCTOR_SHOWN:
        res = None
    if not res and not needs_doctor(ln):
        return ""
    out = ['<div class="sm-acct__doctor">']
    if needs_doctor(ln):
        out.append('<span class="sm-warn">%s</span>' % esc(tr("this reading is stale or unknown; its token may have expired")))
    if not res or not res["ok"] or needs_doctor(ln):
        out.append('<button class="fm-btn fm-btn--sm" type="button" data-doctor="%s" title="%s">%s</button>'
                   % (esc(label), esc(tr("Run claude doctor for this account so Claude Code renews its token, then read its quota again")),
                      esc(tr("claude doctor"))))
    if res:
        out.append('<span class="sm-%s">%s</span>' % (
            "ok" if res["ok"] else "danger",
            esc(tr("claude doctor ran %s") % fmt_when(res["at"], now) if res["ok"]
                else tr("claude doctor failed %s: %s") % (fmt_when(res["at"], now), res["error"]))))
    out.append("</div>")
    return "".join(out)


def block_accounts(q, now, doctor=None):
    doctor = doctor or {}
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
            '<div class="sm-acct__sub"><span>%s</span><span class="sm-%s">%s</span></div>%s</div>'
            % (esc(wins), esc(ln["label"]), badge(ln["plan"]) if ln["plan"] else "",
               tone, (tr("%d%% left") % rem) if rem is not None else tr("unknown"),
               tone, max(0, min(100, rem or 0)),
               esc(tr("%s resets %s") % (ln["limit_label"] or tr("limit"), fmt_when(ln["resets"], now))), ptone, esc(proj),
               doctor_line(ln["label"], doctor, ln, now)))
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


LANE_NAMES = ["exploring", "implementing / writing", "PR open", "waiting for CI", "waiting for merge / delivered"]
LANE_MIN = 165       # six full lanes still fit beside the sidebar at 1400 px
CREW_TONE = {"working": "online", "paused": "info", "blocked": "danger", "failed": "danger", "done": "online"}
REASON_TONE = {"slot": "warn", "lane": "warn", "date": "info", "task": "info", "decision": "danger", "ready": "online"}


def lane_of(a):
    """Board column of an agent; a scout's three phases sit in exploring, writing and delivered."""
    if a["phase"] is None:
        return None
    return a["phase"] if a["kind"] != "scout" else {0: 0, 1: 1, 2: 4}[a["phase"]]


def agent_card(a, now, mark=None):
    tok = a["tokens"]
    meta = " · ".join(x for x in (a["project"], a["model"], a["account"]) if x)
    since = (tr("for %s") % fmt_dur(now - a["phase_since"])) if a["phase_since"] else tr("phase start unknown")
    toks = tr("%s new · %s cache") % (fmt_tokens(tok["new"]), fmt_tokens(tok["cache"])) if tok else tr("tokens unknown")
    title = a["title"] or a["id"]
    return ('<article class="sm-card" data-id="%s" tabindex="0" role="button" aria-label="%s">'
            '<div class="sm-card__top"><span class="sm-id">%s</span>%s</div>'
            '<div class="sm-card__title" title="%s">%s</div>%s'
            '<div class="sm-card__line" title="%s">%s</div>'
            '<div class="sm-card__line">%s%s</div>'
            '<div class="sm-card__line sm-mono">%s</div></article>'
            % (esc(a["id"]), esc(tr("%s: details") % a["id"]), esc(a["id"]), badge(tr(a["crew"]), CREW_TONE.get(a["crew"], "neutral")),
               esc(title), esc(title), '<div class="sm-card__ans">%s</div>' % answer_pill(mark) if mark else "", esc(meta), esc(meta), esc(since),
               (" · " + link(a["pr"], pr_text(a["pr"]))) if a["pr"] else "", esc(toks)))


def queue_card(q, mark=None):
    reasons = " ".join(badge(short, REASON_TONE.get(k, "neutral"), full) for k, short, full in q["reasons"])
    if mark:
        reasons += " " + answer_pill(mark)
    return ('<article class="sm-card sm-card--queue" data-id="%s" tabindex="0" role="button" aria-label="%s">'
            '<div class="sm-card__top"><span class="sm-id">%s</span><span class="sm-faint sm-card__repo">%s</span></div>'
            '<div class="sm-card__title" title="%s">%s</div><div class="sm-reasons">%s</div></article>'
            % (esc(q["id"]), esc(tr("%s: details") % q["id"]), esc(q["id"]), esc(q["repo"]), esc(q["title"]), esc(q["title"]), reasons))


def block_board(agents, queue, err, now, marks=None):
    """Queue lane, then one lane per phase. A phase with no card shrinks to a labelled strip so
    the lanes with work get the width, and keeps its place so the order never changes."""
    lanes = [[] for _ in LANE_NAMES]
    unknown = []
    for a in agents:
        i = lane_of(a)
        (unknown if i is None else lanes[i]).append(agent_card(a, now, (marks or {}).get(a["id"])))
    spec = [(tr("Queue"), [queue_card(q, (marks or {}).get(q["id"])) for q in queue], True)] + [(tr(n), c, False) for n, c in zip(LANE_NAMES, lanes)]
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


ANSWER_PILLS = {"sent": ("✉", "sent"), "unwoken": ("!", "not woken"), "received": ("✓", "received"), "replied": ("↩", "replied")}


def answer_pill(state):
    mark, label = ANSWER_PILLS[state]
    return '<span class="sm-ans sm-ans--%s">%s %s</span>' % (state, mark, esc(tr(label)))


ANSWER_MESSAGES = {"sent": "Sent: firstmate was woken and will read it at its next check.",
                   "unwoken": "Saved, but firstmate was not woken; wake it from the answer below.",
                   "received": "Firstmate already has this answer."}


def answer_message(state):
    return tr(ANSWER_MESSAGES[state])


def ask_head(ask, now):
    """The small line above a decision in the answer form: what kind of wait, its key, since when."""
    if ask["kind"] == "line":
        bits = [ask["state"]] + ([ask["key"]] if ask["key"] else []) + [fmt_when(ask["at"], now) if ask["at"] else tr("time unknown")]
    elif ask["kind"] == "crew":
        bits = [tr("crew is %s") % tr(ask["state"]), tr("no needs-decision or blocked line is open")]
    else:
        bits = [tr("captain's decision"), tr("waiting since %s") % fmt_when(ask["at"], now) if ask["at"] else tr("time unknown")]
    return " · ".join(bits)


def answer_form(tid, ask, now):
    opts = ""
    if ask["options"]:
        opts = '<div class="sm-opts" role="radiogroup" aria-label="%s">%s</div>' % (esc(tr("Ready options")), "".join(
            '<label class="sm-opt" title="%s"><input type="radio" name="option" value="%s"><b>%s)</b> <span>%s</span></label>'
            % (esc(o["text"]), esc(o["label"]), esc(o["label"]), rich(o["text"])) for o in ask["options"]))
    return ('<form class="sm-answer__form" method="post" action="/answer" data-answer data-ref="%s">'
            '<input type="hidden" name="request" value="%s"><input type="hidden" name="task" value="%s">'
            '<input type="hidden" name="ref" value="%s">'
            '<div class="sm-ask"><small>%s</small>%s</div>%s'
            '<textarea name="text" rows="3" maxlength="%d" aria-label="%s" placeholder="%s"></textarea>'
            '<div class="sm-answer__foot"><span class="sm-faint">%s</span>'
            '<button class="fm-btn fm-btn--sm fm-btn--primary" type="submit">%s</button></div>'
            '<p class="sm-answer__msg" role="status" aria-live="polite"></p></form>'
            % (esc(ask["ref"]), esc(secrets.token_urlsafe(8)[:10]), esc(tid), esc(ask["ref"]), esc(ask_head(ask, now)),
               '<div class="sm-reason">%s</div>' % reason_html(ask["text"]) if ask["text"] else "", opts, MAX_ANSWER, esc(tr("Your answer")),
               esc(tr("Add your own words (optional when you pick an option)") if ask["options"] else tr("Write your answer")),
               esc(tr("Goes to firstmate as a note that names %s and this decision.") % tid), esc(tr("Send to firstmate"))))


def answer_thread(answers, now):
    rows = []
    for x in answers:
        if x["state"] == "replied":
            after = ('<div class="sm-thread__fm"><b>%s</b><blockquote>%s</blockquote></div>'
                     % (esc(tr("firstmate · %s") % fmt_when(x["reply"]["at"], now)), esc(x["reply"]["text"])))
        elif x["state"] == "unwoken":
            after = ('<div class="sm-thread__warn">%s <button type="button" class="sm-linkbtn" data-announce="%s">%s</button></div>'
                     % (esc(tr("Saved, but firstmate was not woken.")), esc(x["note"]), esc(tr("Wake firstmate"))))
        elif x["state"] == "received":
            after = '<div class="sm-faint">%s</div>' % esc(tr("Firstmate took it from its inbox and has not replied yet."))
        else:
            after = '<div class="sm-faint">%s</div>' % esc(tr("Waiting for firstmate to take it from its inbox."))
        rows.append('<li><div class="sm-thread__head"><b>%s</b>%s</div><blockquote>%s</blockquote>%s</li>'
                    % (esc(tr("you · %s") % fmt_when(x["at"], now)), answer_pill(x["state"]), esc(x["words"]), after))
    return '<ol class="sm-thread" aria-label="%s">%s</ol>' % (esc(tr("Answers sent")), "".join(rows)) if rows else ""


def answer_section(tid, asks, answers, token, now, err=None):
    """The top of a drawer whose item waits on the captain, or that the captain has answered: one
    form per open decision, then every answer sent about the item and what became of it."""
    if not asks and not answers:
        return ""
    return ('<section class="sm-answer" data-token="%s" aria-label="%s"><h3>%s</h3>%s%s%s<p class="sm-faint sm-answer__rule">%s</p></section>'
            % (esc(token), esc(tr("Your answer")), esc(tr("Your answer")), "".join(answer_form(tid, a, now) for a in asks),
               answer_thread(answers, now),
               '<p class="sm-faint">%s</p>' % esc(tr("Answers unavailable: %s") % err) if err else "",
               esc(tr("Firstmate acts on your answer under its usual rules: a merge or a destructive action still needs your explicit word on that PR."))))


def pre(text, empty):
    return ('<div class="sm-pre">%s</div>' % esc(text)) if text else '<div class="sm-faint">%s</div>' % esc(empty)


def render_agent_detail(a, intent, status, checks, reports, now, answer=""):
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
        '<div class="sm-dhead"><span class="sm-id">%s</span>%s</div><h2 class="sm-dtitle">%s</h2>%s'
        '<div class="sm-facts">%s</div>'
        '<h3>%s</h3>%s<div class="sm-faint">%s</div>'
        '<h3>%s</h3><div>%s</div>'
        '<h3>%s</h3><div>%s</div>'
        '<h3>%s</h3>%s'
        '<h3>%s</h3>%s'
        % (esc(a["id"]), badge(tr(a["crew"]), CREW_TONE.get(a["crew"], "neutral")), esc(a["title"] or a["id"]), answer,
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


def render_queue_detail(q, full, intent, reports, now, answer=""):
    reasons = "".join('<li>%s%s</li>' % (badge(short, REASON_TONE.get(k, "neutral")),
                                         (' <div class="sm-reason">%s</div>' % reason_html(long_)) if long_ != short else "")
                      for k, short, long_ in q["reasons"])
    return (
        '<div class="sm-dhead"><span class="sm-id">%s</span>%s</div><h2 class="sm-dtitle">%s</h2>%s'
        '<div class="sm-facts">%s</div><h3>%s</h3><ul class="sm-reasonlist">%s</ul>'
        '<h3>%s</h3>%s%s<h3>%s</h3><div>%s</div>'
        % (esc(q["id"]), badge(tr("queued"), "info"), esc(full.get("title") or q["title"]), answer,
           "".join([fact(tr("Project"), esc(q["repo"] or "?")), fact(tr("Kind"), esc(q["kind"] or tr("not set"))),
                    fact(tr("Since"), esc(fmt_date_iso(q["created"]) if q["created"] else "?")),
                    fact(tr("Priority"), esc(full.get("priority") or "-")), fact(tr("Depends on"), esc(full.get("deps") or tr("none")))]),
           esc(tr("Why it waits")), reasons, esc(tr("Notes")),
           pre(full.get("body") if full.get("body") not in (None, "-") else None, tr("no notes on this item")),
           ("<h3>%s</h3>" % esc(tr("Captain's intent")) + pre(intent, "")) if intent else "", esc(tr("Report")), report_links(q["id"], reports)))


CLEAR_LABEL = {"stowing": "saving", "waiting": "saved", "clearing": "clearing", "cleared": "cleared",
               "unconfirmed": "unconfirmed", "refused": "not cleared", "timeout": "timed out", "failed": "failed",
               "cancelled": "stopped"}


def clear_row(job):
    """The sidebar's "Clear context…" button, with the state of the latest request beside it."""
    state = ('<span class="sm-clear-row__state" title="%s">%s</span>'
             % (esc(clear_message(job)), badge(tr("clear: %s") % tr(CLEAR_LABEL[job["state"]]), CLEAR_TONE[job["state"]]))
             if job else "")
    return ('<div class="sm-clear-row">%s<button class="fm-btn fm-btn--sm" type="button" data-action="clear">%s</button></div>'
            % (state, esc(tr("Clear context…"))))


def render_clear(fm, job, pane, why, token, now):
    """The drawer of "Clear context": the context it clears, the three steps in order, and then the
    request under way, the reasons it cannot run now, or the button that confirms it."""
    if fm and fm.get("context") is not None:
        size = tr("%s of %s (%s window)") % (fmt_tokens(fm["context"]), fmt_tokens(fm["limit"]), tr(fm["limit_how"]))
        if fm.get("percent") is not None:
            size += " · " + tr("%d%% used") % round(fm["percent"])
    else:
        size = tr("unknown")
    live = job is not None and job["state"] in CLEAR_ACTIVE
    out = ['<div class="sm-clear" data-token="%s"%s>' % (esc(token), ' data-live=""' if live else ""),
           '<div class="sm-facts">%s%s%s</div>' % (
               fact(tr("Context"), esc(size)), fact(tr("Session"), esc(fm["session"][:8] if fm else tr("unknown"))),
               fact(tr("Pane"), esc((job or {}).get("pane") or (pane or {}).get("target") or tr("not found")))),
           '<ol class="sm-clear__steps">%s</ol>' % "".join("<li>%s</li>" % esc(tr(x)) for x in (
               "Save: shipmate asks firstmate, in its inbox, to run /stow, which writes the decisions, open work and learnings that exist only in the conversation to the home's files.",
               "Clear: only after firstmate replies that the stow is done, and only while its pane is idle and nothing is in flight, shipmate types /clear into that pane.",
               "Resume: the new session starts with firstmate's session-start re-emit, which reloads the queue, the workers, the open decisions and memory."))]
    if job:
        out.append('<div class="sm-clear__job">%s <span>%s</span> <span class="sm-faint">%s</span></div>'
                   % (badge(tr(CLEAR_LABEL[job["state"]]), CLEAR_TONE[job["state"]]), esc(clear_message(job)),
                      esc(tr("asked %s") % fmt_when(job["started"], now))))
        if job["state"] in ("stowing", "waiting"):
            out.append('<div class="sm-form__foot"><span class="sm-faint">%s</span>'
                       '<button class="fm-btn fm-btn--sm" type="button" data-clear-cancel>%s</button></div>'
                       % (esc(tr("Stopping only stops shipmate: firstmate may still finish the stow.")), esc(tr("Stop waiting"))))
    if not live:
        problems = list(why) + ([pane["error"]] if pane and "error" in pane else [])
        if problems:
            out.append('<div class="sm-clear__why"><p class="sm-warnline">%s</p><ul>%s</ul></div>'
                       % (esc(tr("Not now: something is in flight with firstmate.")), "".join("<li>%s</li>" % esc(w) for w in problems)))
        else:
            out.append('<form class="sm-clear__form" data-clear><input type="hidden" name="request" value="%s">'
                       '<div class="sm-form__foot"><p class="sm-answer__msg" aria-live="polite"></p>'
                       '<button class="fm-btn fm-btn--sm fm-btn--primary" type="submit">%s</button></div></form>'
                       % (esc(secrets.token_urlsafe(6)), esc(tr("Save, then clear %s") % fmt_tokens(fm["context"]))))
    out.append('<p class="sm-faint">%s</p></div>' % esc(tr(
        "Workers run in sessions of their own: clearing firstmate does not interrupt them. Nothing needs undoing: what firstmate keeps is on disk, and the new session reads it back.")))
    return "".join(out)


def block_firstmate(fm, now, clear=None):
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
           warn, esc(tr("session %s, found by %s") % (fm["session"][:8], fm["found_by"])))) + clear_row(clear)


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
        "board": block_board(s["agents"], s["queue"], s["backlog_error"], s["now"], answer_marks(s)),
        "today": block_today(s["daily"]),
        "firstmate": block_firstmate(s.get("firstmate"), s["now"], s.get("clear")),
        "accounts": block_accounts(s["quota"], s["now"], s.get("doctor")),
        "strip": block_strip(s),
        "sumline": block_sumline(s),
        "usage": block_usage(s["daily"], s["delivered"]),
        "counts": tr("%d agent · %d in the queue" if len(s["agents"]) == 1 else "%d agents · %d in the queue") % (len(s["agents"]), len(s["queue"])),
        "time": dt.datetime.fromtimestamp(s["now"]).strftime("%H:%M:%S"),
    }


def asset(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


# the tab's icon: the header's anchor on its rust disc, in the ship's pixel art, one character a pixel
FAVICON = (
    "................",
    ".....######.....",
    "...##rrrrrr##...",
    "..#rrrrwwrrrr#..",
    ".#rrrrwrrwrrrr#.",
    ".#rrrrrwwrrrrr#.",
    "#rrrrwwwwwwrrrr#",
    "#rrrrrrwwrrrrrr#",
    "#rrrrrrwwrrrrrr#",
    "#rrwrrrwwrrrwrr#",
    "#rwwwrrwwrrwwwr#",
    ".#rwrrrwwrrrwr#.",
    ".#rrwwrwwrwwrr#.",
    "..#rrwwwwwwrr#..",
    "...##rrrrrr##...",
    ".....######.....",
)
FAVICON_INK = {"#": "#241c14", "r": "#c0452a", "w": "#fbf6ea"}


def favicon_svg():
    """The icon as an SVG data URI for the page's own <link>: a rect per run of one colour in a row."""
    rects = []
    for y, row in enumerate(FAVICON):
        for m in re.finditer(r"([^.])\1*", row):
            rects.append("<rect x='%d' y='%d' width='%d' height='1' fill='%s'/>"
                         % (m.start(), y, len(m.group(0)), FAVICON_INK[m.group(1)]))
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' shape-rendering='crispEdges'>%s</svg>"
           % "".join(rects))
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe=" /='<>,")     # no quote, # or scheme left bare


def favicon_png(scale=2):
    """The same icon as a PNG, for a browser that takes no SVG icon: each pixel a scale x scale square."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ink = {k: bytes(int(v[i:i + 2], 16) for i in (1, 3, 5)) + b"\xff" for k, v in FAVICON_INK.items()}
    ink["."] = b"\0\0\0\0"
    rows = []
    for row in FAVICON:
        line = b"\0" + b"".join(ink[c] * scale for c in row)       # filter byte 0, then RGBA pixels
        rows += [line] * scale
    side = len(FAVICON) * scale
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


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
    for k, v in [("{{CSS}}", asset("shipmate.css")), ("{{NONCE}}", nonce), ("{{FAVICON}}", favicon_svg()), ("{{TIME}}", esc(b["time"])),
                 ("{{FORM}}", form), ("{{NOTICE}}", notice), ("{{REFRESH_MS}}", str(REFRESH_SECONDS * 1000)),
                 ("{{SHIP}}", ship["ship"]), ("{{SHIPCOUNTS}}", esc(ship["counts"])),
                 ("{{SHIPPROJECTS}}", ship["projects"]), ("{{SHIPACCOUNTS}}", ship["accounts"]), ("{{SHIPSTAGES}}", ship["stages"]), ("{{SHIPLOG}}", ship["log"])] + \
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
# agent is a hand whose place on board is its phase; firstmate is at the helm; a lighthouse at the
# pier's edge shows the checks of whoever waits for CI; the quota is the
# barrels in the hold and firstmate's context the bilge water; whoever waits on the captain is in
# the captain's cabin, the rest of the queue is hands on the pier, and delivered work crates on it. ship_scene() derives what to show from the snapshot alone,
# ship_layout() places it, render_ship() draws it as pixel art in SVG.

# A ship agent's place, by ship phase index. Waiting for CI is at the bow too, beside the hands that
# signal a PR open, but with a spyglass on the lighthouse ("watch"); waiting for the merge is fishing
# at the rail. Nobody waits on the gangplank: it is the way ashore with the crate once merged, and
# the way aboard for a queued task that boards.
PLACES = ["nest", "deck", "bow", "watch", "rail"]
SCOUT_PLACES = ["out", "back", "dock"]                     # a scout's boat, by scout phase index
PLACE_TEXT = {
    "nest": "in the crow's nest with a spyglass",
    "deck": "on deck with a hammer, a mop or a broom",
    "bow": "at the bow, signalling with flags",
    "watch": "at the bow with a spyglass on the lighthouse: the checks are running",
    "rail": "fishing at the rail: waiting for the merge",
    "hatch": "on deck, phase unknown",
    "out": "rowing out to sea",
    "back": "rowing back with the map",
    "dock": "moored at the pier with the map",
}
PLACE_POSE = {"nest": "spy", "deck": "hammer", "bow": "signal", "watch": "spy", "rail": "fish", "hatch": "stand"}
# On deck the tools take turns among the hands at work, by when each reached the deck: the first
# hammers, the second mops, the third sweeps, and so on. The tool says nothing about the work; it
# only tells the hands apart, and stays with a hand while it is on deck.
DECK_TOOLS = ("hammer", "mop", "broom")
TOOL_TEXT = {"hammer": "on deck with a hammer", "mop": "on deck with a mop", "broom": "on deck with a broom"}
BUSY_FOR = 120          # seconds since the last transcript activity while a deck hand works at full pace
# The queue waits on the pier, and what a queued hand does there says why it waits. Only the
# queue's own reasons decide it (pier_activity); nothing a queued hand does looks like work.
PIER_ACTS = {
    "cards": "playing cards on the pier: queued today, waits for a free build slot or lane",
    "drink": "at the tavern bench with a mug: queued before today, waits for a free build slot or lane",
    "board": "at the foot of the gangplank with a sea bag: nothing holds it, ready to board",
    "talk": "on the pier, talking up to the hand it waits for",
    "wait": "on the pier: waits for a task that is not aboard",
}
PLACE_TEXT.update({"pier-" + k: v for k, v in PIER_ACTS.items()})
# a queued task held until a date is not on the pier either: it sleeps in a hammock in the quarters below deck
PLACE_TEXT["quarters"] = "asleep in a hammock in the quarters below deck: held until its date"
# a queued task held for the captain's decision is not on the pier: it waits in the captain's line
PLACE_TEXT["cabin"] = "in the captain's cabin with a sea bag: queued, waits for your decision"
PIER_POSE = {"cards": "cards", "drink": "drink", "board": "stand", "talk": "talk", "wait": "stand"}
PIER_STAGE = {"cards": "slot", "drink": "slot", "board": "board", "talk": "task", "wait": "task"}
CAPTAIN_STATES = {"parked", "blocked", "failed"}   # fm-crew-state.sh reports a needs-decision crew as parked
SEA_LOAD = (0.6, 1.0)                              # load per CPU where the sea turns choppy, then rough
OFF_DUTY_AFTER = 20 * 60                           # seconds without activity before an idle hand rests
OFF_DUTY_PLACES = {"nest", "deck", "bow", "hatch"}  # waiting for CI or to go ashore is the job itself
DAY_HOURS = (6, 18)                                # local hours of daylight in the picture
TWILIGHT = 30 * 60                                 # either side of those hours the light changes ...
TWILIGHT_STEPS = 6                                 # ... in this many steps, one every ten minutes
MAX_CRATES = 12
NEST_SEATS = 6
FISH_EVERY = 3600       # seconds of waiting for the merge for each fish in the bucket after the first
FISH_MAX = 4            # the bucket is full; the exact wait is in its title
# strong project colours, none of them the red that marks a red flag
PROJECT_COLORS = ["#16a4a4", "#9150dc", "#f2a114", "#2f78d8", "#3fa34d", "#e0569b", "#a0713a", "#5a6f88"]
# light and dark neckerchiefs that stand out on any of those shirts, again none of them red
ACCOUNT_COLORS = ["#f6f1e3", "#23232e", "#f6d040", "#a9d8f5", "#7a4a24", "#c7a8f0"]


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


def twilight(now):
    """How dark the picture is, from 0 (day) to TWILIGHT_STEPS (night): the night falls one step
    every ten minutes over the hour around DAY_HOURS[1] and lifts the same way around DAY_HOURS[0],
    so each refresh across dusk or dawn is a shade darker or lighter. The lamps follow is_night."""
    t = dt.datetime.fromtimestamp(now)
    sec = t.hour * 3600 + t.minute * 60 + t.second
    step = 2.0 * TWILIGHT / TWILIGHT_STEPS
    dawn, dusk = DAY_HOURS[0] * 3600, DAY_HOURS[1] * 3600
    if dusk - TWILIGHT <= sec < dusk + TWILIGHT:
        k = math.ceil((sec - (dusk - TWILIGHT)) / step)
    elif dawn - TWILIGHT <= sec < dawn + TWILIGHT:
        k = math.ceil((dawn + TWILIGHT - sec) / step)
    else:
        k = 0 if dawn + TWILIGHT <= sec < dusk - TWILIGHT else TWILIGHT_STEPS
    return max(0, min(TWILIGHT_STEPS, k))


def off_duty(a, place, flagged, now):
    """An idle hand rests: paused, or quiet for a while without a busy session. A flagged hand
    never rests (it waits on the captain), nor one whose phase is itself a wait."""
    if flagged or place not in OFF_DUTY_PLACES:
        return False
    if a.get("crew") == "paused":
        return True
    last = a.get("last_activity")
    return bool(last and now - last > OFF_DUTY_AFTER and a.get("crew") != "working")


def account_colors(s):
    """A colour per Claude account, in the order of config/claude-accounts so it stays put across
    refreshes; an account a worker reports that the list lacks comes after, in order of agents."""
    names = list(s.get("accounts") or [])
    for a in s.get("agents") or []:
        acc = a.get("account") or ""
        if acc and acc not in names:
            names.append(acc)
    return {a: ACCOUNT_COLORS[i % len(ACCOUNT_COLORS)] for i, a in enumerate(names)}


def project_colors(s):
    """A colour per project, in the order of data/projects.md so it stays put across refreshes."""
    names = list(s.get("projects") or [])
    for p in [a.get("project") for a in s.get("agents") or []] + [q.get("repo") for q in s.get("queue") or []]:
        if p and p not in names:
            names.append(p)
    return {p: PROJECT_COLORS[i % len(PROJECT_COLORS)] for i, p in enumerate(names)}


def pier_activity(q, aboard, today):
    """What a queued task does while it waits, and the hand aboard it talks to: a slot or lane wait
    plays cards when queued today and drinks at the tavern when queued on an earlier day, a task
    with nothing holding it waits to board, one that waits for a hand aboard talks up to it (or
    stands apart when what it waits for is not aboard), all on the pier; a dated hold goes below
    deck to sleep in a hammock ("hammock"). The captain's decisions never come here: they wait in
    the captain's line. `today` is the local date as YYYY-MM-DD."""
    k = q.get("reason")
    if k == "date":
        return "hammock", None
    if k == "task":
        partner = next((d for d in q.get("deps") or [] if d in aboard), None)
        return ("talk", partner) if partner else ("wait", None)
    if k in ("slot", "lane"):
        return ("drink" if q.get("created") and q["created"][:10] < today else "cards"), None
    return ("board" if k == "ready" else "wait"), None


def deck_tools(hands, now):
    """The tool of each hand at work on deck (DECK_TOOLS, by when it reached the deck, then by id)
    and its pace: full while its transcript moved in the last BUSY_FOR seconds, a stroke now and
    then after that, and none once it reported done or paused, or went off duty."""
    deck = [h for h in hands if h["place"] == "deck" and not h["line"]]
    deck.sort(key=lambda h: (h["arrived"] is None, h["arrived"] or 0, h["id"]))
    for i, h in enumerate(deck):
        h["tool"] = DECK_TOOLS[i % len(DECK_TOOLS)]
        if h["off"] or h["crew"] in ("done", "paused"):
            h["pace"] = "still"
        else:
            h["pace"] = "busy" if h["last"] is not None and now - h["last"] <= BUSY_FOR else "slow"


def ship_scene(s):
    """What the ship shows, derived from the snapshot alone; render_ship draws it."""
    now, colors, necks = s["now"], project_colors(s), account_colors(s)
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
             "account": a.get("account") or "", "neck": necks.get(a.get("account") or ""),
             "phase": tr(phases[ph] if ph is not None and ph < len(phases) else "unknown")}
        h["off"] = off_duty(a, place, h["flag"], now)
        h["checks"] = a.get("checks") if not scout else None
        h["arrived"], h["last"] = a.get("phase_since"), a.get("last_activity")
        if place == "rail":                            # a fish when it got there, and one more each hour after
            waited = now - h["arrived"] if h["arrived"] else 0
            h["fish"], h["waited"] = min(FISH_MAX, 1 + int(max(0, waited) // FISH_EVERY)), wait_text(h["arrived"], now)
        h["since"] = agent_waiting_since(a) if h["flag"] else None
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
              "reason": (q.get("reasons") or [("ready",)])[0][0], "deps": list(q.get("deps") or []),
              "slot": q.get("slot"), "created": q.get("created") or "",
              "until": next((r[1] for r in q.get("reasons") or [] if r[0] == "date"), ""),
              "until_day": q.get("hold_until") or "",
              "since": q.get("waiting_since"), "exact": q.get("waiting_exact", True)} for q in s.get("queue") or []]
    for q in queue:
        q.update(project=q["repo"], phase=tr("queued"), off=False, color=colors.get(q["repo"], "#8a8a8a"))
        if not q["flag"]:
            q["since"] = None
    crates = [{"id": r.get("id") or "?", "kind": r.get("kind") or "?"} for r in (s.get("closed") or [])]
    used = {h["project"] for h in hands + scouts}
    for x in hands + scouts + queue:
        x["line"], x["wait"] = 0, None
    line = captain_line(hands + scouts, queue)
    marks = answer_marks(s)
    for i, x in enumerate(line):
        x["line"], x["wait"] = i + 1, wait_text(x["since"], now, x["exact"])
        x["answer"] = marks.get(x["id"])
        x["tag"] = "%d · %s · %s" % (x["line"], x["id"], x["wait"])
    deck_tools(hands, now)
    # the captain's queued decisions wait in the captain's line, in the cabin; the rest of the queue is on the pier
    for q in queue:
        if q["line"]:
            q.update(place="cabin", stage="flag")
    aboard = {h["id"] for h in hands + scouts}
    today = dt.date.fromtimestamp(now).isoformat()
    pier, quarters = [], []
    for q in (q for q in queue if not q["line"]):
        q["act"], q["partner"] = pier_activity(q, aboard, today)
        if q["act"] == "hammock":
            # below deck, the earliest date first; the date alone hangs over the hammock
            q.update(place="quarters", stage="date", date=fmt_day_short(q["until_day"]) or q["until"])
            q["tag"] = "%s · %s" % (q["id"], q["date"])
            quarters.append(q)
            continue
        q.update(place="pier-" + q["act"], stage=PIER_STAGE[q["act"]])
        if q["act"] == "talk":
            q["tag"] = "%s → %s" % (q["id"], q["partner"])
        elif q["act"] == "wait":
            q["tag"] = tr("%s waits for %s") % (q["id"], ", ".join(q["deps"]) or "?")
        pier.append(q)
    quarters.sort(key=lambda q: (q["until_day"] or "9999", q["id"]))
    return {
        "now": now,
        "line": line, "captain": {"busy": bool(line), "waiting": len(line), "oldest": line[0] if line else None},
        "hands": hands, "scouts": scouts, "queue": queue, "pier": pier, "quarters": quarters, "crates": crates, "barrels": barrels,
        "firstmate": {"context": pct, "warn_at": warn, "over": pct is not None and pct >= warn,
                      "tokens": fm.get("context"), "limit": fm.get("limit"), "known": bool(fm),
                      "compaction": (fm.get("last_compaction") or {}).get("at")},
        "colors": {p: c for p, c in colors.items() if p in used},
        "accounts": {a: c for a, c in necks.items() if a in {h["account"] for h in hands + scouts}},
        "sea": sea_state(s.get("machine")), "night": is_night(now), "dark": twilight(now) / float(TWILIGHT_STEPS),
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
    parts.append(tr("%d in the queue") % len(sc["queue"]))
    return " · ".join(parts)


# -- motion: what changed between two readings of the ship, for the page to play once
#
# The ship drawn for a reading carries its print (data-print): where each hand stood, the queue,
# the rail's checks, firstmate's last compaction and whether the lamps were lit. The page sends the
# print of the drawing it shows when it asks for the next one, and ship_events compares the two.
# Every animation has one detector here and one player in the page's script, keyed by its kind,
# so an animation is added or taken out in those two places.

PRINT_FRESH = 3 * REFRESH_SECONDS   # an older print plays nothing: what changed since is old news
MAX_PRINT = 64 * 1024
WALK_PLACES = set(PLACES) | {"hatch", "cabin"}   # where a hand aboard can walk from and to


def ship_print(sc):
    """What the next reading is compared with: each hand aboard by its place (the cabin when it
    waits there) and its colours, so it can be drawn again once it has left; each scout by its
    place; each queued task by where it waits (the cabin or the pier) and, when it waits for a build
    slot, the tasks that hold that slot, and who sits in the tavern; the tasks delivered (the crates
    on the pier); each hand's checks and the fish in each bucket at the rail, firstmate's context and
    last compaction, and whether the lamps are lit."""
    fm = sc["firstmate"]
    return {"at": int(sc["now"]), "hands": {h["id"]: "cabin" if h["line"] else h["place"] for h in sc["hands"]},
            "scouts": {h["id"]: "cabin" if h["line"] else h["place"] for h in sc["scouts"]},
            "looks": {h["id"]: " ".join(c for c in (h["color"], h.get("neck")) if c) for h in sc["hands"] + sc["scouts"]},
            "queue": {q["id"]: "cabin" if q["line"] else "pier" for q in sc["queue"]},
            "holds": {q["id"]: " ".join(q["slot"]["holders"]) for q in sc["queue"]
                      if q["reason"] == "slot" and (q.get("slot") or {}).get("group") and q["slot"].get("holders")},
            "tavern": sorted(q["id"] for q in sc["pier"] if q["act"] in ("cards", "drink")),
            "crates": [c["id"] for c in sc["crates"]],
            "checks": {h["id"]: h["checks"] for h in sc["hands"] if h.get("checks")},
            "fish": {h["id"]: str(h["fish"]) for h in sc["hands"] if h.get("fish") and not h["line"]},
            "context": fm["context"], "compaction": fm["compaction"], "night": sc["night"]}


def parse_print(text):
    """The print the page sent back, or None when it is missing, too long or not one."""
    if not text or len(text) > MAX_PRINT:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _strings(doc, key, kind):
    """A field of a print sent back by the page, kept only when it has the expected shape."""
    v = doc.get(key)
    if kind is dict:
        return {k: x for k, x in v.items() if isinstance(k, str) and isinstance(x, str)} if isinstance(v, dict) else {}
    return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _boards(prev, cur):
    was_queued, was_aboard = _strings(prev, "queue", dict), _strings(prev, "hands", dict)
    return [{"kind": "board", "id": i, "from": "cabin" if was_queued[i] == "cabin" else "pier", "to": p}
            for i, p in sorted(cur["hands"].items()) if i in was_queued and i not in was_aboard and p in WALK_PLACES]


def _leaves(prev, cur):
    """Who left the ship since the last reading: a hand from wherever it stood, a scout only from
    its skiff moored at the pier (one out at sea is too far to walk); delivered when its task is
    among the crates now, that is when firstmate closed it as done. Back in the queue is no leaving."""
    gone = set(cur["hands"]) | set(cur.get("scouts") or ()) | set(cur["queue"])
    looks, crates = _strings(prev, "looks", dict), set(cur.get("crates") or ())
    out = [{"kind": "leave", "id": i, "from": p} for i, p in sorted(_strings(prev, "hands", dict).items())
           if i not in gone and p in WALK_PLACES and valid_id(i)]
    out += [{"kind": "leave", "id": i, "from": "dock"} for i, p in sorted(_strings(prev, "scouts", dict).items())
            if i not in gone and p == "dock" and valid_id(i)]
    for ev in out:
        ev["delivered"], ev["look"] = ev["id"] in crates, looks.get(ev["id"], "")
    return out


def _handovers(prev, cur):
    """A build slot handed over at the foot of the gangplank: a hand that held the slot leaves, and a
    task that waited for that very slot boards, in the same reading. Only a task without a recorded
    PR holds a slot (queue_rows counts them as firstmate does), so a hand that opened a PR gave its
    slot up when it went to the bow, and never hands it over here. Each leaver and each boarder
    takes part in one handover at most."""
    holds = {q: set(h.split()) for q, h in _strings(prev, "holds", dict).items()}
    leaves = [ev for ev in _leaves(prev, cur) if ev["from"] != "dock"]
    out, used = [], set()
    for b in _boards(prev, cur):
        if b["from"] != "pier":
            continue
        lv = next((ev for ev in leaves if ev["id"] in holds.get(b["id"], ()) and ev["id"] not in used), None)
        if lv:
            used.add(lv["id"])
            out.append({"kind": "handover", "id": lv["id"], "leave": lv, "board": b})
    return out


def board_events(prev, cur):
    """A queued task that firstmate dispatched: it walks up the gangplank to its place, or out of
    the cabin when it waited there for the captain (unless it boards in a handover)."""
    taken = {h["board"]["id"] for h in _handovers(prev, cur)}
    return [ev for ev in _boards(prev, cur) if ev["id"] not in taken]


def leave_events(prev, cur):
    """A hand that left the ship: it walks down the gangplank, with its crate to the pile on the
    pier when it was delivered and empty-handed off the pier when not (unless it hands its slot over)."""
    taken = {h["id"] for h in _handovers(prev, cur)}
    return [ev for ev in _leaves(prev, cur) if ev["id"] not in taken]


def handover_events(prev, cur):
    """A slot handed over: the one who leaves and the one who boards meet at the gangplank's foot."""
    return _handovers(prev, cur)


def arrive_events(prev, cur):
    """A task that has just joined the queue to wait for a build slot or a lane: it walks in from
    the near end of the pier to its seat in the tavern. One that was already queued, aboard or out
    in a skiff in the last reading did not arrive; it only changed places."""
    known = set(_strings(prev, "queue", dict)) | set(_strings(prev, "hands", dict)) | set(_strings(prev, "scouts", dict))
    return [{"kind": "arrive", "id": i} for i in cur.get("tavern") or () if i not in known and valid_id(i)]


def move_events(prev, cur):
    """A hand whose place changed: a new phase, a red flag that takes it to the cabin, or the
    decision answered that brings it back. One that leaves the watch on the lighthouse looks at it a
    moment longer first (`look`), as the light gives the answer; one that comes to the rail from
    anywhere but the cabin catches its first fish on arriving (`catch`)."""
    was = _strings(prev, "hands", dict)
    return [{"kind": "move", "id": i, "from": was[i], "to": p, "look": was[i] == "watch", "catch": p == "rail" and was[i] != "cabin"}
            for i, p in sorted(cur["hands"].items())
            if i in was and was[i] != p and was[i] in WALK_PLACES and p in WALK_PLACES]


def beacon_events(prev, cur):
    """A hand that watched the lighthouse and no longer does, still aboard, now that its PR's checks
    answered: the light flashes green when they all passed, red when one failed."""
    was = _strings(prev, "hands", dict)
    return [{"kind": "beacon", "id": i, "result": cur["checks"].get(i)} for i, p in sorted(was.items())
            if p == "watch" and cur["hands"].get(i) not in (None, "watch") and cur["checks"].get(i) in ("pass", "fail")]


def fish_events(prev, cur):
    """A hand at the rail that has waited one more hour for the merge: it catches one more fish."""
    was_at, was = _strings(prev, "hands", dict), _strings(prev, "fish", dict)
    out = []
    for i, n in sorted(cur["fish"].items()):
        if was_at.get(i) == "rail" and cur["hands"].get(i) == "rail" and was.get(i, "").isdigit() and int(n) > int(was[i]):
            out.append({"kind": "fish", "id": i, "n": int(n)})
    return out


def pump_events(prev, cur):
    """firstmate's context was compacted since the last reading: the bilge is pumped down."""
    at, before, after = _number(cur["compaction"]), _number(prev.get("context")), _number(cur["context"])
    if at is None or before is None or after is None or after >= before:
        return []
    last = _number(prev.get("compaction"))
    if (last is not None and at <= last) or cur["at"] - at > PRINT_FRESH:
        return []
    return [{"kind": "pump", "from": before, "to": after}]


def lights_events(prev, cur):
    """Night came since the last reading: the lamps are lit one by one."""
    return [{"kind": "lights"}] if cur["night"] and prev.get("night") is False else []


# each detector with the group it counts in; past a group's limit the rest of a refresh's changes
# just show in their new places, so a busy refresh never fills the ship with walkers
# (one lighthouse flashes one answer at a time)
EVENT_DETECTORS = ((handover_events, "leave"), (leave_events, "leave"), (board_events, "walk"), (move_events, "walk"),
                   (arrive_events, "arrive"), (beacon_events, "beacon"),
                   (fish_events, "fish"), (pump_events, "pump"), (lights_events, "lights"))
EVENT_LIMITS = {"leave": 2, "walk": 4, "arrive": 2, "beacon": 1, "fish": 3, "pump": 1, "lights": 1}


def ship_events(prev, cur):
    """The animations to play for the change from print prev to print cur, each once and at most
    EVENT_LIMITS of a group; none when there is no earlier print, or it is older than PRINT_FRESH
    or from later."""
    if not isinstance(prev, dict):
        return []
    at = _number(prev.get("at"))
    if at is None or not 0 <= cur["at"] - at <= PRINT_FRESH:
        return []
    out, used = [], {}
    for detect, group in EVENT_DETECTORS:
        for ev in detect(prev, cur):
            if used.get(group, 0) < EVENT_LIMITS[group]:
                used[group] = used.get(group, 0) + 1
                out.append(ev)
    return out


WALK_POSES = ("walk", "walk-l", "stand", "stand-l", "climb", "climb2")
CARRY_POSES = ("carry", "carry-l", "carry2", "carry2-l")
RE_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def gone_hand(ev):
    """A hand that has left the ship, drawn again from the colours its last print kept (a colour
    that is not one is dropped, since the print came back from the page)."""
    look = [c for c in (ev.get("look") or "").split() if RE_COLOR.match(c)]
    return {"id": ev["id"], "color": look[0] if look else None, "neck": look[1] if len(look) > 1 else None,
            "kind": "hand", "place": "deck", "flag": False, "off": False}


def leave_sprites(ev):
    """A leaving hand: walking, and with its crate in its arms when it was delivered; the bell's ring."""
    hand = gone_hand(ev)
    f = {k: hand_frame(hand, k) for k in WALK_POSES + ("wave", "wave-l")}
    if ev.get("delivered"):
        f.update({k: hand_frame(hand, k) for k in CARRY_POSES})
        p = Pix()
        p.shaded(0, 0, CRATE, CRATE_INK)
        f["crate"] = p.svg()
        p = Pix()
        for x, y in ((0, 0), (-1, 1), (-1, 2), (0, 3), (6, 0), (7, 1), (7, 2), (6, 3)):
            p.dot(x, y, "#fff3b0")
        f["ring"] = p.svg()
    return dict(ev, frames=f)


def catch_frames(hand):
    """The rod held and bent by a bite, and the fish that comes up."""
    p = Pix()
    p.shaded(0, 0, FISH, FISH_INK)
    return {"fish": hand_frame(hand, "fish", line_to=WL + 10 - (NEAR + 2)),
            "reel": hand_frame(hand, "reel", line_to=WL + 10 - (NEAR + 2)), "catch": p.svg()}


def event_sprites(ev, sc):
    """The frames the page needs to play one event, drawn here like every other sprite: the hand
    that walks (ashore in its straw hat first when it boards), the spyglass it holds on the
    lighthouse before it goes, the flag it waves on its way to the cabin, the bent rod and the
    catch, the pump's jet."""
    if ev["kind"] == "leave":
        return leave_sprites(ev)
    if ev["kind"] == "handover":
        return dict(ev, leave=leave_sprites(ev["leave"]), board=event_sprites(ev["board"], sc))
    if ev["kind"] == "arrive":
        q = next((q for q in sc["pier"] if q["id"] == ev["id"]), None)
        return dict(ev, frames={k: hand_frame(q, k) for k in ("walk", "stand")} if q else {})
    hand = next((h for h in sc["hands"] if h["id"] == ev.get("id")), None)
    f = {}
    if ev["kind"] in ("move", "board") and hand:
        f.update({k: hand_frame(hand, k) for k in WALK_POSES})
        if ev["kind"] == "board" and ev.get("from") == "pier":
            ashore = dict(hand, kind="queue", neck=None, flag=False, off=False)
            f.update({"ashore-" + k: hand_frame(ashore, k) for k in ("walk", "walk-l", "stand", "stand-l")})
        if hand["flag"] and ev.get("to") == "cabin":
            f["alarm"] = hand_frame(hand, "wave", flag=True)
        if ev.get("look"):
            f["look"] = hand_frame(dict(hand, flag=False, off=False), "spy")
        if ev.get("catch") and hand["place"] == "rail":
            f.update(catch_frames(hand))
        if ev.get("from") == "cabin" and not hand["flag"]:
            p = Pix()
            p.rect(-1, -1, 9, 9, "#1d140c")
            p.rect(0, 0, 7, 7, "#3fa34d")
            for x, y in ((1, 3), (2, 4), (3, 5), (4, 4), (5, 3), (5, 2), (6, 1)):
                p.dot(x, y, "#ffffff")
            f["answered"] = p.svg()
    elif ev["kind"] == "fish" and hand:
        f.update(catch_frames(hand))
    elif ev["kind"] == "pump":
        p = Pix()
        for x in range(0, 22, 2):
            p.rect(-x - 2, int(0.06 * x * x), 2, 1, "#9ad0ee")
        f["jet"] = p.svg()
    return dict(ev, frames=f)


# -- layout, in art pixels: the ship grows with its crew so nobody stands on anybody

SHIP_H, HOR, FAR, NEAR, WL, PIER = 340, 112, 176, 196, 262, 300
# The cabin has one size, so the ship never grows with the captain's line: the first CABIN_SEATS in
# line wait inside, and the rest crowd the open door with their count (the log lists everyone).
CABIN_X = 70          # the captain's cabin starts this far into the stern castle, after its windows
CABIN_W = 120         # the cabin, from its back wall to its open door onto the main deck
CABIN_DESK = 46       # the desk and the captain take this much of the cabin before the line
CABIN_STEP = 20       # between those in line in the cabin, at the least
CABIN_DOOR = 10       # the open door, at the cabin's front: nobody in line stands in it
CABIN_SEATS = 3       # in line inside the cabin, at most; fewer when wide props (a rod, signal flags) take the room
BAG_W = 4             # a queued task's sea bag, at its feet behind it
CABIN_TOP, CABIN_FLOOR = 156, 194
# Between hands at each place, so that no hand stands on another: a hand is 13 art pixels wide
# standing, and its props (flags, a rod, a hand of cards) make some poses wider.
DECK_STEP = 72        # between pairs on deck; the second of a pair stands half a step on, further back
HATCH_STEP = 39
RAIL_STEP = 48        # a hand fishing and its bucket of up to four fish
BOW_STEP = 69
WATCH_STEP = 36       # between hands at the bow with a spyglass on the lighthouse
LIGHT_GAP = 26        # from the bow to the lighthouse's tower, on the pier's edge ahead of the ship
LIGHT_W = 30          # the lighthouse's stone head on the pier
NEST_STEP = 33        # between lookouts in the crow's nest, and those that stand below it
SCOUT_STEP = 45       # between scouts' skiffs
NEST_UPTO, SKIFF_UPTO = 14, 15                    # rows of a hand shown above the nest's rim or a skiff's side
TABLE_SEATS = 3       # a card table: one each side and one standing behind
TABLE_W = 50          # a card table and its players
MAX_TABLES = 4        # card tables per group waiting for a slot; the rest are counted
MAX_ROW = 8           # hands on one bench or in one line on the pier; the rest are counted
BENCH_STEP = 20
LINE_STEP = 22
TALK_W = 22           # a hand talking up to the one it waits for
PIER_FEET = PIER + 8  # where a hand on the pier stands
TAVERN_FROM = 42      # from the stern post to the first lantern of the tavern: just right of the gangplank's foot
DECK_WALK = 190       # the feet of a hand walking along the main deck from one place to another
BOW_WALK = 168        # and along the forecastle
PUMP_FROM_END = 84    # the bilge pump stands this far from the hold's forward end, clear of the chart table
# The quarters, cut open in the hull forward of the hold: the dated holds sleep there in hammocks
# slung from the beams, in two tiers, the second half a hammock on so no rope crosses a sleeper.
QUARTERS_TOP = NEAR + 11      # under the deck planks and the hull's blue strake
QUARTERS_FLOOR = WL - 6
HAMMOCK_W = 28                # between hammocks in one tier
HAMMOCK_LOW = (21, 42)        # each tier's lowest point, below the quarters' top
MAX_HAMMOCKS = 12             # the rest are counted
QUARTERS_PAD = 14             # from the quarters' aft wall to the first hammock's hook
QUARTERS_BOW = 92             # the quarters keep this far from the stem, clear of the bow's curve and the ship's name
HOLD_KEEPS = 104              # the hold's chart table, and its first column, past the barrels


def sign_w(text):
    return int(len(text) * 2.75) + 8


def tavern_units(sc):
    """The card tables and tavern bench of each group that waits for a slot (one per project, and
    one for the home's lanes), in a fixed order, with the width each takes on the pier."""
    units = {}
    # the order the backlog lists them in must not decide who sits where: oldest first, then by id
    for q in sorted(sc["pier"], key=lambda q: (q.get("created") or "", q["id"])):
        if q["act"] in ("cards", "drink"):
            slot = q.get("slot") or {}
            u = units.setdefault(slot.get("group", ""), {"slot": slot, "cards": [], "drink": []})
            u[q["act"]].append(q)
    out = []
    for g, u in sorted(units.items(), key=lambda kv: (kv[0] == "", kv[0])):   # projects by name, the home's lanes last
        n = len(u["cards"]) + len(u["drink"])
        sl = u["slot"]
        u["sign"] = (tr("SLOT %s · %d/%d taken · %d waiting") % (g, sl.get("taken", 0), sl.get("cap", 0), n) if g else
                     tr("HOME LANES · %d/%d in use · %d waiting") % (sl.get("taken", 0), sl.get("cap", 0), n))
        u["tables"] = -(-min(len(u["cards"]), TABLE_SEATS * MAX_TABLES) // TABLE_SEATS)
        u["bench"] = min(len(u["drink"]), MAX_ROW)
        more = len(u["cards"]) > TABLE_SEATS * MAX_TABLES or len(u["drink"]) > MAX_ROW
        body = 12 + u["tables"] * TABLE_W + (BENCH_STEP * u["bench"] + 22 if u["bench"] else 0) + (12 if more else 0)
        u["w"] = max(body, sign_w(u["sign"]) + 6)
        out.append(u)
    return out


def light_span(lay):
    """The stretch of pier the lighthouse and its sign take, ahead of the bow."""
    w = max(LIGHT_W, sign_w(tr("LIGHTHOUSE · CI")))
    mid = lay["light_x"] + LIGHT_W // 2 - 5
    return mid - w // 2 - 4, mid + w // 2 + 4


def pier_layout(sc, lay, x0):
    """Where the queue stands on the pier. The tavern is anchored first, at the foot of the
    gangplank on its right, and grows to the right; then, toward the bow, the talkers, each under
    the hand it talks to, and the lines in the gaps they leave (the dated holds sleep below deck,
    see quarters_layout). Nothing overlaps: the tavern and the talkers claim their places first, and a group
    that would land on another moves toward the bow to the first gap wide enough. Returns the
    spots, the props, the signs, the counted overflow and where it ends."""
    spots, props, signs, more = {}, [], [], []
    lay["rooms"] = []
    # the tavern is anchored at the foot of the gangplank, on its right, and grows to the right: a
    # short queue adds a short stretch, and nothing is reserved west of the ship
    x = x0 + TAVERN_FROM
    for u in lay["tavern"]:
        signs.append((u["sign"], x + u["w"] // 2, ""))
        props.append(("lantern", x + 1))
        tx = x + 22
        players = u["cards"][:TABLE_SEATS * MAX_TABLES]
        for t in range(u["tables"]):
            seat = players[TABLE_SEATS * t:TABLE_SEATS * t + TABLE_SEATS]
            props.append(("table", tx))
            for i, q in enumerate(seat):
                spots[q["id"]] = [(tx - 16, PIER_FEET, "cards", None), (tx + 13, PIER_FEET, "cards-l", None),
                                  (tx + 1, PIER_FEET - 3, "stand", None)][i]
            tx += TABLE_W
        if u["bench"]:
            props.append(("bench", tx - 4, tx + BENCH_STEP * u["bench"] + 2))
            for i, q in enumerate(u["drink"][:MAX_ROW]):
                spots[q["id"]] = (tx + BENCH_STEP * i, PIER_FEET, "drink", None)
            tx += BENCH_STEP * u["bench"] + 2
            props.append(("keg", tx))
            tx += 18
        hidden = u["cards"][TABLE_SEATS * MAX_TABLES:] + u["drink"][MAX_ROW:]
        if hidden:
            more.append((tx, hidden))
        lay["rooms"].append((x, x + u["w"], u))             # its building: draw_tavern
        x += u["w"] + 14
    tavern_end = x - 14 if lay["tavern"] else x0 + TAVERN_FROM
    # from the gangplank toward the bow
    groups = []
    for act, label, cls in (("board", tr("READY · boards next"), ""), ("wait", tr("WAITS FOR A TASK"), "")):
        who = [q for q in sc["pier"] if q["act"] == act]
        if who:
            n = min(len(who), MAX_ROW)
            groups.append({"act": act, "who": who, "pref": x0 + 92, "sign": (label, cls),
                           "w": max(LINE_STEP * n + (12 if len(who) > n else 0), sign_w(label) + 4)})
    for q in sc["pier"]:
        if q["act"] == "talk":
            spot = lay["hands"].get(q["partner"]) or lay["scouts"].get(q["partner"])
            groups.append({"act": "talk", "who": [q], "pref": (spot[0] - 2) if spot else x0 + 92, "sign": None, "w": TALK_W})
    taken, end = ([(x0 + TAVERN_FROM, tavern_end + 8)] if lay["tavern"] else []), max(x0 + 92, tavern_end)
    taken.append(light_span(lay))
    order = {"talk": 0, "board": 1, "wait": 2}
    for g in sorted(groups, key=lambda g: (order[g["act"]], g["pref"])):
        gx = max(g["pref"], x0 + 92)
        for a, b in sorted(taken):                         # the first gap at or after gx that fits it
            if gx + g["w"] + 8 <= a:
                break
            gx = max(gx, b)
        taken.append((gx, gx + g["w"] + 8))
        end = max(end, gx + g["w"] + 8)
        if g["sign"]:
            signs.append((g["sign"][0], gx + g["w"] // 2, g["sign"][1]))
        shown = g["who"][:MAX_ROW]
        if g["act"] == "talk":
            spots[g["who"][0]["id"]] = (gx + 2, PIER_FEET, "talk", None)
        else:
            for i, q in enumerate(shown):
                spots[q["id"]] = (gx + LINE_STEP * i, PIER_FEET, "stand", None)
                if g["act"] == "board":
                    props.append(("bag", gx + LINE_STEP * i + 14))
        if len(g["who"]) > len(shown):
            more.append((gx + LINE_STEP * len(shown) + 4, g["who"][len(shown):]))
    return spots, props, signs, more, end


def quarters_layout(sc, out):
    """Where the quarters go and who sleeps where, when a queued task is held until a date. They
    take the hull forward of the hold, up to QUARTERS_BOW from the stem: the hold gives up the room
    it does not need for its barrels and chart table, and when that is still short the forecastle
    grows. Hammocks hang in date order from aft, alternating tiers. Sets out["quarters"] (None
    when nobody sleeps) and may change out["hold"], out["fc_len"] and out["length"]."""
    who = sc["quarters"]
    if not who:
        out["quarters"] = None
        return
    shown = who[:MAX_HAMMOCKS]
    cols = (len(shown) + 1) // 2
    label = tr("QUARTERS · asleep until a date")
    w = max(2 * QUARTERS_PAD + HAMMOCK_W * cols + HAMMOCK_W // 2 + (14 if len(who) > len(shown) else 0), sign_w(label) + 10)
    hx0, hx1 = out["hold"]
    bow = out["x0"] + out["length"] - QUARTERS_BOW
    keep = hx0 + max(150, 14 * len(sc["barrels"]) + HOLD_KEEPS)
    hold_end = min(hx1, max(keep, bow - w - 14))
    short = hold_end + 14 + w - bow
    if short > 0:
        out["fc_len"] += short
        out["length"] += short
    out["hold"] = (hx0, hold_end)
    qx = hold_end + 14
    beds = {}
    for i, q in enumerate(shown):
        tier = i % 2
        beds[q["id"]] = (qx + QUARTERS_PAD + HAMMOCK_W * (i // 2) + (HAMMOCK_W // 2 if tier else 0), QUARTERS_TOP + HAMMOCK_LOW[tier])
    out["quarters"] = {"box": (qx, qx + w), "beds": beds, "label": label, "more": who[len(shown):],
                       "more_x": qx + QUARTERS_PAD + HAMMOCK_W * cols + HAMMOCK_W // 2 + 2,
                       "chests": [qx + QUARTERS_PAD + HAMMOCK_W * i + 18 for i in range(cols)]}


def cabin_pose(x):
    """How one in the captain's line stands in the cabin, facing the desk: a hand with the prop of
    its phase, a scout with its map, a queued task standing (with its sea bag beside it)."""
    if x["kind"] == "queue":
        return "stand-l"
    return ("scroll" if x["kind"] == "scout" else PLACE_POSE.get(x["place"], "stand")) + "-l"


def cabin_line(line, put, cx0):
    """Seats the captain's line in the cabin, the longest wait nearest the desk, while they fit
    before the door and at most CABIN_SEATS of them; everyone after the first who does not fit
    waits in the crowd at the door, so the line keeps its order. Puts the seated in `put` and
    returns the ids seated and the crowd, in line order."""
    x, limit, seated = cx0 + CABIN_DESK, cx0 + CABIN_W - CABIN_DOOR, []
    for i, h in enumerate(line):
        pose = cabin_pose(h)
        w = len(POSES[pose][0]) + (BAG_W if h["kind"] == "queue" else 0)
        if len(seated) == CABIN_SEATS or x + w > limit:
            return seated, line[i:]
        put[h["id"]] = (x, CABIN_FLOOR - 2, pose, None)
        seated.append(h["id"])
        x += max(CABIN_STEP, w + 3)
    return seated, []


def ship_layout(sc):
    """Where every hand, boat and part of the ship goes. Hands get (x, feet, pose, upto) with x
    the sprite's left edge and feet its lowest painted row; nothing here depends on the clock."""
    tavern = tavern_units(sc)
    by = {k: [h for h in sc["hands"] if h["place"] == k and not h["line"]] for k in PLACES + ["hatch"]}
    n = {k: len(v) for k, v in by.items()}
    castle = CABIN_X + CABIN_W + 4
    deck_zone = max(110, DECK_STEP * ((n["deck"] + 1) // 2) + 30, 14 * len(sc["barrels"]) + 100)
    hatch_zone = 60 + HATCH_STEP * max(0, n["hatch"] - 1)
    rail_zone = max(40, RAIL_STEP * n["rail"] + 16)
    deck_len = max(260, deck_zone + hatch_zone + rail_zone)
    fc_len = max(104, BOW_STEP * n["bow"] + WATCH_STEP * n["watch"] + 18)
    x0 = 70
    length = castle + deck_len + fc_len
    deck_x, fc_x = x0 + castle, x0 + castle + deck_len
    hatch_x = deck_x + deck_zone + 4
    rail_x = deck_x + deck_zone + hatch_zone
    main_x = deck_x + deck_zone - 6
    nest_w = max(54, NEST_STEP * min(n["nest"], NEST_SEATS) + 10)
    out = {"x0": x0, "length": length, "castle": castle, "cabin": (x0 + CABIN_X, x0 + CABIN_X + CABIN_W), "deck_x": deck_x, "deck_len": deck_len, "fc_x": fc_x, "fc_len": fc_len,
           "hatch_x": hatch_x, "rail_x": rail_x, "main_x": main_x, "nest": (main_x + 2 - nest_w // 2, nest_w),
           "hold": (x0 + 98, deck_x + deck_zone + hatch_zone - 8), "hands": {}, "tavern": tavern}
    quarters_layout(sc, out)
    length, fc_len = out["length"], out["fc_len"]
    put = out["hands"]
    ladder = ((main_x - 38, FAR), (main_x - 8, 46))
    for i, h in enumerate(by["nest"]):
        if i < NEST_SEATS:
            put[h["id"]] = (out["nest"][0] + 6 + NEST_STEP * i, 43, "spy", NEST_UPTO)
        elif i < NEST_SEATS + 3:                       # more lookouts than seats climb the ladder
            t = (0.3, 0.55, 0.8)[i - NEST_SEATS]
            put[h["id"]] = (int(ladder[1][0] + (ladder[0][0] - ladder[1][0]) * t) - 4,
                            int(ladder[1][1] + (ladder[0][1] - ladder[1][1]) * t), "spy", None)
        else:
            put[h["id"]] = (main_x + 14 + NEST_STEP * (i - NEST_SEATS - 3), FAR + 6, "spy", None)
    for i, h in enumerate(by["deck"]):
        tool = h.get("tool") or "hammer"
        put[h["id"]] = (deck_x + 12 + DECK_STEP * (i // 2) + DECK_STEP // 2 * (i % 2) - pose_left(tool),
                        194 if i % 2 == 0 else 183, tool, None)
    for i, h in enumerate(by["hatch"]):
        put[h["id"]] = (hatch_x + 38 + HATCH_STEP * i, 188, "stand", None)
    for i, h in enumerate(by["rail"]):
        put[h["id"]] = (rail_x + 4 + RAIL_STEP * i, NEAR + 2, "fish", None)
    for i, h in enumerate(by["bow"]):
        put[h["id"]] = (fc_x + 8 + BOW_STEP * i, 166 + 3 * (i % 2), "signal", None)
    for i, h in enumerate(by["watch"]):                 # ahead of the signallers, nearest the lighthouse
        put[h["id"]] = (fc_x + 8 + BOW_STEP * n["bow"] + WATCH_STEP * i + pose_left("signal"), 166 + 3 * ((n["bow"] + i) % 2), "spy", None)
    out["plank"] = ((x0 + 86, NEAR + 2), (x0 + 30, PIER + 4))
    out["light_x"] = x0 + length + LIGHT_GAP
    out["seated"], out["crowd"] = cabin_line(sc["line"], put, x0 + CABIN_X)
    for h in sc["hands"]:
        if h["off"] and h["id"] in put:
            x, feet, _pose, upto = put[h["id"]]
            put[h["id"]] = (x, feet, "rest", upto)
    right = x0 + length + 70
    sb = {k: [h for h in sc["scouts"] if h["place"] == k and not h["line"]] for k in SCOUT_PLACES}
    out["scouts"] = {}
    for i, h in enumerate(sb["out"]):
        out["scouts"][h["id"]] = (right + SCOUT_STEP * i, HOR + 14, "spy", NEST_UPTO)
    out["pier"], out["pier_props"], out["pier_signs"], out["pier_more"], pier_end = pier_layout(sc, out, x0)
    right = max(right, pier_end + 30)                   # moored scouts and crates keep clear of the queue
    for i, h in enumerate(sb["back"]):                  # rowing back: out at sea, clear of the pier
        out["scouts"][h["id"]] = (right + 44 + SCOUT_STEP * i, WL + 16, "scroll", SKIFF_UPTO)
    for i, h in enumerate(sb["dock"]):
        out["scouts"][h["id"]] = (right + SCOUT_STEP * i, PIER - 2, "stand", SKIFF_UPTO)
    out["dock_x"] = right
    out["crates_x"] = right + SCOUT_STEP * max(1, len(sb["dock"])) + 10
    widest = max([1] + [len(v) for v in sb.values()] + [len(sb["back"]) + 2])
    out["width"] = max(640, right + SCOUT_STEP * widest + 9 * ((min(len(sc["crates"]), MAX_CRATES) + 2) // 3) + 70)
    # the opening frame: the deck and every hand aboard, with a margin; the nest only when manned
    # the opening frame also takes in the captain's cabin
    # and the queue on the pier below it; the tavern grows to the right of it
    every = list(put.values()) + list(out["pier"].values())
    xs = ([x0 + CABIN_X - 10, fc_x + fc_len, out["light_x"] + LIGHT_W] + [x for x, _f, _p, _u in every if x >= x0]
          + [x + len(POSES[p][0]) for x, _f, p, _u in every if x >= x0])
    ys = [FAR - 50, NEAR + 20] + [f - len(POSES[p][:u]) - 12 for _x, f, p, u in every] + [f + 8 for _x, f, _p, _u in every]
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

# A hand is 13 pixels wide and 21 tall, facing right, seen three-quarters so both eyes show.
# c shirt takes the project's colour and w its stripes, k the bandana a darker shade of it, n the
# neckerchief the account's colour, B the belt; the poses add the prop that makes each phase, and
# each wait on the pier, a different silhouette. The hat changes with the hand's state (HATS).
STAND = [
    "....kkkkk....",
    "...kkkkkkk...",
    "..Kkkkkkkkk..",
    ".KKhsssssss..",
    "..Khsssssss..",
    "...hssEssEs..",
    "...hssEssEss.",
    "...hsssssss..",
    "....sssssss..",
    ".....nnnn....",
    "...ccnnnccc..",
    "..ccwwnwwwcc.",
    "..cccccccccc.",
    "..cwwwwwwwwc.",
    "..cccccccccc.",
    "..sBBBgBBBBs.",
    "...pppppppp..",
    "...ppp..ppp..",
    "...ppp..ppp..",
    "...ppp..ppp..",
    "..bbbb..bbbb.",
]
HEAD = (3, 10)        # the head's first and last column in STAND, for where a name tag or a flag goes


def build_pose(w, h, ox, oy, sets=(), clear=(), base=STAND):
    """A w by h pose: base rows at (ox, oy), then some of their pixels cleared and others set, both
    in base coordinates (so a prop above the head has a negative y)."""
    grid = [["."] * w for _ in range(h)]
    for j, row in enumerate(base):
        for i, ch in enumerate(row):
            if ch != ".":
                grid[oy + j][ox + i] = ch
    for x, y in clear:
        grid[oy + y][ox + x] = "."
    for x, y, ch in sets:
        grid[oy + y][ox + x] = ch
    return ["".join(r) for r in grid]


def line_points(x0, y0, x1, y1):
    n = max(abs(x1 - x0), abs(y1 - y0))
    return [(round(x0 + (x1 - x0) * k / float(n)), round(y0 + (y1 - y0) * k / float(n))) for k in range(n + 1)]


ARM_R = [(11, y) for y in range(11, 16)]
ARM_L = [(2, y) for y in range(11, 16)]
LEGS = [(x, y) for y in range(16, 21) for x in range(13) if STAND[y][x] != "."]
STRIDE = ([(3, 17, "p"), (4, 17, "p"), (2, 18, "p"), (3, 18, "p"), (1, 19, "p"), (2, 19, "p")] + [(x, 20, "b") for x in range(4)]
          + [(9, 17, "p"), (10, 17, "p"), (10, 18, "p"), (11, 18, "p"), (11, 19, "p"), (12, 19, "p")] + [(x, 20, "b") for x in range(11, 15)]
          + [(x, 16, "p") for x in range(3, 11)])
# sitting: the upper body two rows lower, thighs forward, shins down, on a stool (O); in a seated
# pose's own coordinates the body starts at row 2, so these rows are written relative to it
SIT = ([(x, 15, "p") for x in range(3, 11)] + [(x, 16, "p") for x in range(3, 12)]
       + [(11, 17, "p"), (12, 17, "p"), (11, 18, "b"), (12, 18, "b"), (13, 18, "b"), (14, 18, "b")]
       + [(x, y, "O") for x in range(1, 8) for y in (17, 18)])
UPPER = STAND[:16]
# a crate held in front with both hands, carried off the ship to the pile on the pier
CRATE_HELD = ([(x, y, "O") for x in range(7, 15) for y in (10, 16)] + [(x, y, "O") for x in (7, 14) for y in range(10, 17)]
              + [(x, y, "M") for x in range(8, 14) for y in range(11, 16)] + [(x, 13, "o") for x in range(8, 14)]
              + [(8, 11, "o"), (13, 15, "o"), (6, 13, "s"), (15, 13, "s")])


def long_tool(top, bottom, head):
    """A long-handled tool held in the right hand at (13, 13): the arm reaching down and forward,
    the handle from `top` to `bottom`, then the head's pixels."""
    px = [(11, 11, "c"), (12, 12, "c"), (13, 13, "s")]
    px += [(x, y, "o") for x, y in line_points(top[0], top[1], bottom[0], bottom[1]) if (x, y) != (13, 13)]
    return px + head


def broom_head(x):
    """Straw bristles splayed on the deck, bound with a dark cord."""
    return ([(x + i, 17, "T") for i in (-1, 0, 1)] + [(x + i, 18, "g") for i in range(-2, 3)]
            + [(x + i, 19, "g") for i in range(-3, 4)] + [(x + i, 20, "T") for i in (-3, -1, 1, 3)]
            + [(x + i, 20, "g") for i in (-2, 0, 2)])


def mop_head(x, lean):
    """Grey cotton strands dragged along the deck."""
    return ([(x + i, 18, "G") for i in (-1, 0, 1)] + [(x + i, 19, "l") for i in range(-2, 3)]
            + [(x + i + lean, 20, c) for i, c in ((-3, "l"), (-2, "G"), (-1, "l"), (0, "l"), (1, "G"), (2, "l"), (3, "l"))])


MOP_BUCKET = [(x, 17, "G") for x in range(0, 6)] + [(0, 18, "G"), (5, 18, "G")] + [(x, 18, "u") for x in range(1, 5)] \
    + [(0, 19, "G"), (5, 19, "G")] + [(x, 19, "o") for x in range(1, 5)] + [(x, 20, "G") for x in range(1, 5)]


def seated(w, sets=(), clear=()):
    return build_pose(w, 21, 0, 2, list(SIT) + list(sets), clear, base=UPPER)


POSES = {
    "stand": STAND,
    "spy": build_pose(19, 21, 0, 0, [(11, 11, "c"), (12, 10, "c"), (12, 9, "c"), (12, 8, "s"), (12, 7, "s")]
                      + [(x, y, "g") for x in range(10, 17) for y in (5, 6)]
                      + [(10, 5, "T"), (10, 6, "T"), (14, 5, "T"), (14, 6, "T")] + [(17, y, "T") for y in range(4, 8)], clear=ARM_R),
    "hammer": build_pose(17, 26, 0, 5, [(11, 11, "c"), (12, 10, "c"), (12, 9, "s"), (12, 8, "s")] + [(12, y, "o") for y in range(-1, 8)]
                         + [(x, y, "G") for x in range(9, 16) for y in (-5, -4, -3)] + [(9, -2, "G"), (15, -2, "G")], clear=ARM_R),
    "signal": build_pose(23, 26, 5, 5, [(2, 11, "c"), (1, 10, "c"), (1, 9, "s"), (11, 11, "c"), (12, 10, "c"), (12, 9, "s")]
                         + [(1, y, "o") for y in range(-5, 9)] + [(12, y, "o") for y in range(-5, 9)]
                         + [(x, y, "y") for x in range(-4, 1) for y in range(-5, -1)] + [(x, -1, "y") for x in range(-3, 1)]
                         + [(x, y, "u") for x in range(13, 18) for y in range(-5, -1)] + [(x, -1, "u") for x in range(13, 17)],
                         clear=ARM_L + ARM_R),
    "fish": build_pose(26, 21, 0, 0, [(11, 13, "s"), (12, 13, "s")] + [(x, y, "o") for x, y in line_points(13, 13, 25, 0)],
                       clear=[(11, 14), (11, 15)]),
    "scroll": build_pose(19, 21, 0, 0, [(x, y, "m") for x in range(9, 18) for y in range(11, 16)]
                         + [(9, 11, "M"), (17, 11, "M"), (9, 15, "M"), (17, 15, "M"), (12, 13, "x"), (13, 12, "x"), (15, 14, "x"),
                            (10, 13, "s"), (16, 13, "s")], clear=[(11, 11), (11, 12)]),
    # off duty: sitting on a crate, a pair of dice in front
    "rest": seated(21, [(x, y, "w") for x in (16, 17, 19, 20) for y in (17, 18)] + [(16, 17, "E"), (19, 17, "E"), (20, 18, "E")]),
    # the queue on the pier, sitting on a stool: holding a hand of cards, or a mug at the mouth
    "cards": seated(17, [(11, 11, "c"), (12, 12, "c"), (13, 12, "s")] + [(x, y, "w") for x in (13, 14, 15) for y in (9, 10, 11)]
                    + [(14, 10, "r"), (15, 9, "E")], clear=ARM_R),
    "drink": seated(15, [(11, 11, "c"), (12, 10, "c"), (12, 9, "s")] + [(x, y, "l") for x in (11, 12) for y in (6, 7, 8)]
                    + [(13, 7, "l"), (11, 5, "w"), (12, 5, "w")], clear=ARM_R),
    # standing, looking up at the hand it waits for, one arm up
    "talk": build_pose(15, 21, 0, 0, [(11, 11, "c"), (12, 10, "c"), (13, 9, "s"), (13, 8, "s"),
                                      (6, 5, "E"), (9, 5, "E"), (6, 4, "E"), (9, 4, "E"), (6, 6, "s"), (9, 6, "s")], clear=ARM_R),
    # on the move between places (ship_events): a stride, the two holds of climbing the ladder, an
    # arm waving the red flag on the way to the cabin, and the rod bent by a bite at the rail
    "walk": build_pose(15, 21, 0, 0, STRIDE, clear=LEGS),
    "climb": build_pose(13, 21, 0, 0, [(2, 10, "c"), (1, 9, "c"), (1, 8, "s"), (1, 7, "s")]
                        + [(11, 11, "c"), (11, 12, "c"), (11, 13, "c"), (11, 14, "s")], clear=ARM_L),
    "climb2": build_pose(13, 21, 0, 0, [(11, 10, "c"), (12, 9, "c"), (12, 8, "s"), (12, 7, "s")]
                         + [(2, 11, "c"), (2, 12, "c"), (2, 13, "c"), (2, 14, "s")], clear=ARM_R),
    "wave": build_pose(14, 21, 0, 0, [(11, 11, "c"), (12, 10, "c"), (12, 9, "c"), (13, 8, "s"), (13, 7, "s")], clear=ARM_R),
    "reel": build_pose(26, 21, 0, 0, [(11, 13, "s"), (12, 13, "s")] + [(x, y, "o") for x, y in line_points(13, 13, 22, 4)]
                       + [(x, y, "o") for x, y in line_points(22, 4, 25, 6)], clear=[(11, 14), (11, 15)]),
    # a delivery: the crate carried down the gangplank and along the pier to the pile (ship_events)
    "carry": build_pose(16, 21, 0, 0, STRIDE + CRATE_HELD, clear=LEGS + ARM_L + ARM_R),
    "carry2": build_pose(16, 21, 0, 0, CRATE_HELD, clear=ARM_L + ARM_R),
    # the deck's tools (DECK_TOOLS), each with its second frame: the blow that throws chips, the
    # mop's stroke that leaves the deck wet behind it, the broom's sweep that raises dust
    "hammer2": build_pose(22, 26, 0, 5, [(11, 11, "c"), (12, 12, "c"), (13, 13, "s")]
                          + [(x, y, "o") for x, y in line_points(14, 14, 16, 17)]
                          + [(x, y, "G") for x in range(15, 19) for y in (18, 19)] + [(15, 20, "G"), (18, 20, "G")]
                          + [(20, 16, "t"), (21, 14, "t"), (19, 13, "t")], clear=ARM_R),
    "mop": build_pose(30, 21, 6, 0, long_tool((9, 7), (19, 18), mop_head(19, 1))
                      + [(x - 6, y, c) for x, y, c in MOP_BUCKET], clear=ARM_R),
    "mop2": build_pose(30, 21, 6, 0, long_tool((12, 7), (15, 18), mop_head(15, -1) + [(x, 20, "l") for x in range(19, 24)])
                       + [(x - 6, y, c) for x, y, c in MOP_BUCKET], clear=ARM_R),
    "broom": build_pose(24, 21, 0, 0, long_tool((9, 7), (19, 17), broom_head(19)), clear=ARM_R),
    "broom2": build_pose(24, 21, 0, 0, long_tool((12, 7), (15, 17), broom_head(15))
                         + [(19, 18, "m"), (21, 16, "m"), (22, 18, "m"), (20, 14, "m")], clear=ARM_R),
}
# the same poses facing left, for the line in the captain's cabin and across a card table
POSES.update({k + "-l": [r[::-1] for r in v] for k, v in list(POSES.items())})
# where each pose puts STAND's top left corner, so the hat, the name tag and the flag find the head
POSE_HEAD = {"walk": (0, 0), "climb": (0, 0), "climb2": (0, 0), "wave": (0, 0), "reel": (0, 0),
             "carry": (0, 0), "carry2": (0, 0), "hammer2": (0, 5), "mop": (6, 0), "mop2": (6, 0), "broom": (0, 0), "broom2": (0, 0),
             "stand": (0, 0), "spy": (0, 0), "hammer": (0, 5), "signal": (5, 5), "fish": (0, 0),
             "scroll": (0, 0), "rest": (0, 2), "cards": (0, 2), "drink": (0, 2), "talk": (0, 0)}
ROD_TIP = (25, 0)     # the fishing rod's tip in the fish pose, where the line hangs from
REEL_TIP = (25, 6)    # and in the reel pose, bent by a bite

# The hat tells the hand's state: the bandana at work, a straw hat when not working (off duty, or
# queued ashore), bare-headed before the captain, a grey wool cap when its phase is unknown. Each
# replaces the bandana (k, K) in the head's first five rows.
HATS = {
    "bandana": None,
    "straw": ["....zzzzz....", "...ZZZZZZZ...", "zzzzzzzzzzzzz", "...h........."],
    "bare": [".............", "....hhhhh....", "...hhhhhhhh..", "...h........."],
    "wool": ["....vvvvv....", "...vvvvvvv...", "...VVVVVVVV..", "...h........."],
}

# firstmate at the helm: a navy coat with gold buttons over a white shirt, a navy cap with gold trim
HELM = [
    "...aaaaaaa...",
    "..aaaaaaaaa..",
    "..ggggggggg..",
    "...hsssssss..",
    "...hsssssss..",
    "...hssEssEs..",
    "...hssEssEss.",
    "...hsssssss..",
    "....sssssss..",
    ".....llll....",
    "...AAllllAA..",
    "..AAAAlgAAAA.",
    "..AAAAlAAAAA.",
    "..AAAAlgAAAA.",
    "..AAAAlAAAAA.",
    "..sAAAAAAAAs.",
    "...pppppppp..",
    "...ppp..ppp..",
    "...ppp..ppp..",
    "...ppp..ppp..",
    "..bbbb..bbbb.",
]
HELM_INK = {"h": "#2a1d14", "s": "#d9a173", "a": "#1b2740", "A": "#26365e", "l": "#f3efe4"}


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
CAPTAIN = [
    ".....kkkkk.....",
    "....kkkkkkk....",
    "..kkkkkgkkkkk..",
    "kkkkkkkkkkkkkkk",
    "....hssssssss..",
    "....hsEsssEss..",
    "....hsEsssEsss.",
    "....hssssssss..",
    "....hhhhhhhhh..",
    ".....hhhhhhh...",
    "......hhhhh....",
    "...xxxxwxxxxx..",
    "..xxxxxwgxxxxx.",
    "..xxxxxwxxxxxx.",
    "..xxxxxwgxxxxx.",
    "..sxxxxwxxxxxs.",
    "...xxxxxxxxxx..",
    "...xxxxxxxxxx..",
    "...xxxx.xxxxx..",
    "....ppp..ppp...",
    "....ppp..ppp...",
    "....ppp..ppp...",
    "...bbbb..bbbb..",
]
# attending: a paper held up in the right hand
CAPTAIN_BUSY = paint(20, 23, CAPTAIN, [(13, 14, "x"), (14, 13, "s"), (14, 12, "s")]
                     + [(x, y, "m") for x in range(15, 20) for y in range(7, 13)]
                     + [(16, 9, "M"), (17, 9, "M"), (18, 9, "M"), (16, 11, "M"), (17, 11, "M")], clear=[(13, 15)])
# off duty: sitting on a crate, a mug in hand
CAPTAIN_OFF = paint(19, 22, CAPTAIN[:18],
                    [(x, 18, "p") for x in range(4, 13)] + [(12, 19, "p"), (13, 19, "p"), (12, 20, "p"), (13, 20, "p"),
                                                          (12, 21, "b"), (13, 21, "b"), (14, 21, "b")]
                    + [(x, y, "O") for x in range(1, 9) for y in (19, 20, 21)]
                    + [(12, 13, "s"), (13, 13, "s")] + [(x, y, "l") for x in range(14, 17) for y in (11, 12, 13)]
                    + [(17, 12, "l"), (17, 13, "l"), (14, 14, "l"), (15, 14, "l")], clear=[(13, 15)])
CAPTAIN_MUG = (15, 10)    # where the steam rises from the off-duty captain's mug
QMARK = [".www.", "w...w", "...w.", "..w..", ".....", "..w.."]
ZZZ = ["www...", "..w...", ".w.www", "www..w", "....w.", "...www"]
WHEEL = ["..o.o..", ".ooooo.", "oo.O.oo", ".oOOOo.", "oo.O.oo", ".ooooo.", "..o.o.."]
BARREL = ["..OOOOO..", ".OoooooO.", "OGGGGGGGO", "OoooooooO", "OoooooooO", "OoooooooO", "OGGGGGGGO",
          "OoooooooO", "OoooooooO", "OGGGGGGGO", ".OoooooO.", "..OOOOO.."]
BELL = ["..g..", ".ggg.", ".ggg.", "ggggg", "..T.."]
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

    def figure(self, x, y, rows, pal, outline="#140b06"):
        """A person: a dark outline, and on each material a shaded edge away from the light (its
        right side, and the bottom of a tall patch) and a lit corner toward it (upper left). Skin,
        eyes and dark marks stay flat, so a face a few pixels wide reads as a face."""
        grid = {(i, j): ch for j, row in enumerate(rows) for i, ch in enumerate(row) if ch != "."}
        for (i, j) in grid:
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (i + di, j + dj) not in grid:
                    self.px.setdefault((x + i + di, y + j + dj), outline)
        for (i, j), ch in grid.items():
            base = pal.get(ch) or INK.get(ch)
            if not base:
                continue
            c = base
            if ch not in "sEe":
                right, down, up, left = grid.get((i + 1, j)), grid.get((i, j + 1)), grid.get((i, j - 1)), grid.get((i - 1, j))
                if right != ch or (down != ch and up == ch):
                    c = dark(base, 0.3)
                elif up != ch and left != ch:
                    c = lit(base, 0.3)
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


HAT_INK = {"z": "#e6c46a", "Z": "#9c6a2a", "v": "#8c93a3", "V": "#5c6273", "B": "#3a2414"}


def hand_palette(h):
    """A hand's colours: the shirt its project's, the bandana a darker shade of it, the neckerchief
    its account's (a hand not yet aboard has none, so it takes the bandana's shade), hair and skin
    picked by its id so it keeps its looks across refreshes."""
    c = h.get("color") or "#8a8a8a"
    pal = {"c": c, "k": dark(c, 0.3), "K": dark(c, 0.55), "n": h.get("neck") or dark(c, 0.55),
           "h": pick(HAIRS, h["id"] + "h"), "s": pick(SKINS, h["id"] + "s")}
    pal.update(HAT_INK)
    return pal


def hand_hat(h):
    """The hat for a hand's state (see HATS)."""
    if h.get("flag"):
        return "bare"
    if h.get("off") or h.get("kind") == "queue":
        return "straw"
    if h.get("place") == "hatch":
        return "wool"
    return "bandana"


def hand_rows(h, pose):
    """The rows of hand h in pose, facing right or, for a pose ending in -l, left: the pose with
    its bandana swapped for the hat of the hand's state."""
    base = pose.split("-")[0]
    rows = POSES[base]
    hat = HATS[hand_hat(h)]
    if hat:
        ox, oy = POSE_HEAD[base]
        grid = [list(r) for r in rows]
        for j in range(oy, oy + 5):
            for i in range(ox, ox + len(STAND[0])):
                if grid[j][i] in "kK":
                    grid[j][i] = "."
        for j, row in enumerate(hat):
            for i, ch in enumerate(row):
                if ch != "." and 0 <= ox + i < len(grid[0]):
                    grid[oy + j][ox + i] = ch
        rows = ["".join(r) for r in grid]
    return [r[::-1] for r in rows] if pose.endswith("-l") else rows


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

    def place(self, text, cx, top, owner="", hover=False):
        """A tag over (cx, top). A hover tag shows only while its hand is hovered or focused, alone,
        so it takes no room from the others."""
        w, h = int(len(text) * 2.75) + 5, 7
        if hover:
            x0, y0 = int(cx - w / 2), top - h - 2
            self.out.append('<g class="sm-tag is-hover" data-for="%s" data-ax="%d" data-ay="%d"><rect x="%d" y="%d" width="%d" height="%d" rx="1.5"/>'
                            '<text x="%d" y="%.1f">%s</text></g>' % (esc(owner), cx, top, x0, y0, w, h, cx, y0 + 5.2, esc(text)))
            return (x0, y0, x0 + w, y0 + h)
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


def hand_sprite(p, h, x, feet, pose, upto=None):
    """The one place that paints a hand: its pose's rows (cut at `upto` rows), in its own colours
    and the hat of its state, with its feet on row `feet` and its left edge at x. Returns the rows
    painted, the top row, and the head's centre column and top row. Every hand, aboard or on the
    pier, and every chip icon of a hand comes through here, so an art style lives in POSES,
    POSE_HEAD, HATS, hand_palette and this function and nothing else."""
    rows = hand_rows(h, pose)[:upto]
    top = feet - len(rows) + 1
    p.figure(x, top, rows, hand_palette(h))
    ox, oy = POSE_HEAD[pose.split("-")[0]]
    cx = ox + (HEAD[0] + HEAD[1]) // 2
    if pose.endswith("-l"):
        cx = len(rows[0]) - 1 - cx
    return rows, top, (x + cx, top + oy)


def pose_left(pose):
    """Where a pose puts the hand's body (STAND's left edge), from the sprite's own left edge."""
    base = pose.split("-")[0]
    ox = POSE_HEAD[base][0]
    return len(POSES[base][0]) - len(STAND[0]) - ox if pose.endswith("-l") else ox


def fishing_line(p, x, top, bottom):
    """The line from a rod's tip at (x, top) down to its float on the water at bottom."""
    p.line(x, top, x, bottom, "#f2f5f8")
    p.rect(x - 1, bottom - 1, 3, 3, "#f6f1e3")


def hand_frame(h, pose, flag=False, line_to=None):
    """One frame of hand h for the page's script to move about: its body's left edge at x 0 and its
    feet on row 0, with the red flag over it, or the fishing line down to line_to rows below."""
    p = Pix()
    _rows, top, (cx, head) = hand_sprite(p, h, -pose_left(pose), 0, pose)
    if flag:
        red_flag(p, cx - 8, head - 10)
    if line_to is not None:
        tip = REEL_TIP if pose.split("-")[0] == "reel" else ROD_TIP
        fishing_line(p, tip[0] - pose_left(pose), top + tip[1] + 1, line_to)
    return p.svg()


BUCKET = ["GGGGGGGGGGGGG", "GoooooooooooG", ".GGGGGGGGGGG."]
FISH = ["..lll..l", ".lllllll", "lklllll.", ".lllllll", "..lll..l"]
FISH_INK = {"l": "#bcd2dc", "k": "#1d140c"}
FISH_UP = ["a.a", ".a.", "aad", "kad", "aad", "aad"]          # a fish in the bucket: tail up, head down
# where each fish sits, in the order they are caught: its column in the bucket, how many rows its
# tail rises over the rim, and its colour, side by side so each reads as one fish
FISH_AT = [(1, 4, "#d4e3ea"), (7, 5, "#b9d3de"), (4, 6, "#e6eef2"), (10, 4, "#c6dbe4")]


def bucket_svg(h, x, feet):
    """The bucket beside a hand fishing at the rail while it waits for the merge: a fish for when it
    got there and one more for each hour since, up to FISH_MAX; its title says the exact wait. Each
    fish is its own drawing (data-n, in the order caught), so the page can hold the newest back
    while it is being caught."""
    fish = []
    for n, (cx, up, c) in enumerate(FISH_AT[:h.get("fish") or 1], 1):
        f = Pix()
        f.sprite(x + cx, feet - 2 - up, FISH_UP, {"a": c, "d": dark(c, 0.25), "k": "#1d140c"})
        fish.append(f.svg(' class="sm-bucket__fish" data-n="%d"' % n))
    p = Pix()
    p.shaded(x, feet - 2, BUCKET, {})
    n, waited = h.get("fish") or 1, h.get("waited") or "?"
    title = (tr("%s: one fish, waiting for the merge for %s") % (h["id"], waited) if n == 1 else
             tr("%s: %d fish, waiting for the merge for %s") % (h["id"], n, waited))
    return '<g class="sm-bucket" data-for="%s"><title>%s</title>%s%s</g>' % (esc(h["id"]), esc(title), "".join(fish), p.svg())


def draw_pump(x, floor, top, bottom, stern):
    """The bilge pump on the orlop: a pipe down into the bilge, one along the hold out through the
    stern, and its handle up; the handle's other stroke is drawn hidden for the page's script, which
    works it when firstmate's context is compacted."""
    p = Pix()
    p.rect(x + 1, floor, 1, bottom - floor, "#6b3a1c")
    p.rect(stern + 1, top + 3, x - stern, 2, "#a06a32")
    p.rect(stern + 1, top + 3, x - stern, 1, "#c08a55")
    p.rect(x, top + 3, 3, floor - top - 3, "#c08a55")
    p.rect(x, top + 3, 1, floor - top - 3, "#e0aa72")
    p.rect(x, floor - 9, 3, 1, "#e0b552")
    p.rect(x - 3, floor - 2, 9, 2, "#e0b552")
    up, down = Pix(), Pix()
    up.line(x + 1, top + 4, x + 10, top, "#f0d38c")
    down.line(x + 1, top + 4, x + 10, top + 8, "#f0d38c")
    return ('<g class="sm-pump"><title>%s</title>%s%s%s</g>'
            % (esc(tr("the bilge pump: it works when firstmate's context is compacted")), p.svg(),
               up.svg(' class="sm-pump__up"'), down.svg(' class="sm-pump__down" visibility="hidden"')))


CHECKS_TEXT = {"pass": "every check passed", "fail": "a check failed", "pending": "checks running"}


def hand_title(h):
    """What a hand's tooltip says: who, where and why, and its place in the captain's line."""
    state = tr("; off duty") if h["off"] else ""
    where = TOOL_TEXT.get(h.get("tool")) if h["place"] == "deck" and h.get("tool") else PLACE_TEXT[h["place"]]
    title = "%s · %s · %s (%s%s)%s" % (h["id"], h["project"] or tr("no project"), tr(where), h["phase"], state,
                                       tr("; needs the captain: ") + "; ".join(h["why"]) if h["flag"] else "")
    if h.get("kind") == "queue":
        title += " · %s%s" % (", ".join(h["why"]), (" · " + h["title"]) if h.get("title") else "")
    if h.get("line"):
        title += tr("; number %d in line for the captain, waiting %s") % (h["line"], h["wait"])
    if h.get("waited") and h["place"] == "rail":
        title += "; " + tr("for %s") % h["waited"]
    if h.get("checks"):
        title += "; " + tr(CHECKS_TEXT[h["checks"]])
    return title


def hammock_sprite(p, h, x, low, top):
    """A hammock slung from the beams at row `top`, its lowest point at row `low` and its aft hook
    at x, with a hand of the queue asleep in it: the head aft, eyes shut, under a blanket in its
    project's colour, its feet fore. It is painted from hand_palette, like every hand."""
    pal = hand_palette(h)
    w = HAMMOCK_W - 2
    p.line(x, top, x + 3, low - 4, "#8a6a3a")
    p.line(x + w, top, x + w - 3, low - 4, "#8a6a3a")
    p.rect(x - 1, top, 3, 1, "#3e220f")
    p.rect(x + w - 1, top, 3, 1, "#3e220f")

    def sag(i):
        return round(2.6 * math.sin(math.pi * (i - 2) / float(w - 4)))
    for i in range(2, w - 1):                      # the blanket over the sleeper
        p.rect(x + i, low - 6 + sag(i), 1, 3, pal["c"])
        p.dot(x + i, low - 6 + sag(i), lit(pal["c"]))
    for i in range(1, w):                          # the canvas, wrapped round it
        p.rect(x + i, low - 3 + sag(i), 1, 3, "#e9dcb8")
        p.dot(x + i, low - 1 + sag(i), "#b8a77a")
    p.rect(x + 1, low - 5, 2, 2, "#e9dcb8")
    p.rect(x + w - 2, low - 5, 2, 2, "#e9dcb8")
    p.rect(x + 3, low - 11, 6, 2, pal["h"])        # the head, eyes shut
    p.rect(x + 3, low - 9, 6, 4, pal["s"])
    p.rect(x + 3, low - 9, 1, 3, pal["h"])
    p.rect(x + 6, low - 7, 2, 1, "#5a3418")
    p.rect(x + w - 6, low - 8 + sag(w - 6), 3, 2, dark(pal["c"]))   # the feet under the blanket
    p.sprite(x + 11, low - 14, ZZZ, {})


def hand_svg(h, spot, tags, extra=None, shadow_on=True, hover_tag=False):
    """One clickable hand at its spot, with its flag, its rest marks, and its name tag (shown only
    on hover or focus with hover_tag)."""
    x, feet, pose, upto = spot
    base, left = pose.split("-")[0], pose.endswith("-l")
    p = Pix()
    rows, top, (cx, head) = hand_sprite(p, h, x, feet, pose, upto)
    if extra:
        extra(p, top)
    if h["place"] == "hatch":
        p.sprite(cx - 2, head - 8, QMARK, {})
    if base == "rest":
        p.sprite(cx - 14 if left else cx + 8, head - 3, ZZZ, {})       # beside the face, clear of the name tag
    flag = ""
    if h["flag"]:
        f = Pix()
        red_flag(f, cx - 8, head - 10)
        flag = f.svg(' class="sm-redflag"')
    tags.place(h.get("tag") or h["id"], cx, head - (10 if h["flag"] else 1), h["id"], hover=hover_tag)
    # a tool on deck has a second frame (the blow, the stroke, the sweep): the stylesheet shows the
    # two in turn at the hand's pace, and only the first while it rests or motion is off
    tool = h.get("tool") if base == h.get("tool") else None
    alt, cls = "", ""
    if tool:
        a = Pix()
        hand_sprite(a, h, x, feet, tool + "2" + ("-l" if left else ""), upto)
        alt, cls = a.svg(' class="sm-alt"'), " tool-%s pace-%s" % (tool, h.get("pace") or "still")
    title = hand_title(h)
    stage = h.get("stage") or ("scouts" if h.get("kind") == "scout" else h["place"])
    return ('%s<g class="sm-hand pose-%s%s%s" data-id="%s" data-stage="%s" data-at="%d,%d"%s tabindex="0" role="button" aria-label="%s">'
            '<title>%s</title>%s%s%s</g>'
            % (shadow(cx, feet + 1, 16) if shadow_on else "", base, cls, " has-flag" if h["flag"] else "", esc(h["id"]), stage,
               x + pose_left(pose), feet, ' data-off="1"' if h["off"] else "", esc(tr("%s: details") % title), esc(title), p.svg(),
               alt, flag))


def boat_pix(x, wl):
    """A scout's skiff, its waterline at wl."""
    p = Pix()
    p.shaded(x, wl - 3, BOAT, {})
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


DAY_SKY = ["#6fb3e0", "#78b9e4", "#83c0e8", "#8fc8ec", "#9dd0ee", "#b3dcf2"]
NIGHT_SKY = ["#0d1631", "#111c3b", "#152244", "#1a294d", "#203157", "#283a60"]


def sky_and_sea(w, dusk, night, sea, rnd, now):
    """The sky, the hills and the sea behind the ship, `dusk` of the way from day (0) to night (1);
    the stars and the moon come out with the night (is_night), and the gulls fly only by day."""
    def tint(day, nite):
        return mix(day, nite, dusk)
    far, mid = [], []
    for i, (d, n) in enumerate(zip(DAY_SKY, NIGHT_SKY)):
        far.append(band(-300, HOR * i // 6, w + 600, HOR * (i + 1) // 6, tint(d, n)))
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
                    deco.dot(w - 90 + x, 22 + y, tint("#fff1b8", "#f2a35a") if x * x + y * y < 40 else tint("#ffe58a", "#d9783a"))
    hills = Pix()
    hc = [tint(d, n) for d, n in zip(("#8fb7c9", "#9ec2d2", "#7aa6bb"), ("#2a3b5c", "#304466", "#22314f"))]
    for x in range(-300, w + 300):
        hgt = int(9 + 8 * abs(((x * 7) % 130) - 65) / 65.0 + 4 * ((x // 37) % 2))
        hills.rect(x, HOR - hgt, 1, hgt, hc[0] if x % 210 < 130 else hc[1])
        hgt = int(4 + 4 * abs(((x * 5 + 40) % 100) - 50) / 50.0)
        hills.rect(x, HOR - hgt, 1, hgt, hc[2])
    far += [deco.svg()] + (twinkling_stars(w) if night else []) + sky_clouds(w, dusk, now) + [hills.svg()]
    if not night:
        far += gulls(w, now)
    near_c, far_c = tint("#2c6f9f", "#0c2440"), tint("#5aa6cf", "#1d3d64")
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
        caps.rect(x, y, size, 1, tint(mix("#cfe8f5", "#9bc9e4", t), mix("#5a7aa8", "#3a5a88", t)))
        if t > 0.5 and sea != "calm":
            caps.rect(x + 1, y - 1, max(1, size - 2), 1, tint("#f6fbfd", "#8aa6cc"))
    mid.append('<g class="sm-waves">%s</g>' % caps.svg())
    mid += ripples(w, sea, dusk) + [glitter(w, night)]
    return far, mid


# -- the scenery's own life: it says nothing about the work. Each loop below moves only while the
# motion switch says "all" and stops where it is in a hidden tab (shipmate.css). A loop longer than
# the page's refresh takes its phase from the clock (--late), so a new drawing goes on where the
# last one was instead of starting it over.

CLOUD_LAYERS = ((5, 1, 6, 40, 0.5), (3, 2, 18, 56, 0.28))   # clouds, size, highest and lowest row, seconds per art pixel
CLOUD_SPAN = 600                                            # the sky reaches this much past the scene, for the parallax


def clock_phase(now, period):
    """How far into a loop of `period` seconds the clock is, as a CSS animation delay."""
    return "-%.1fs" % (now % period)


def sky_clouds(w, dusk, now):
    """Two layers of clouds that cross the whole sky a pixel at a time and wrap around: the far
    layer small and slow, the near one larger and quicker. Each is drawn twice, one sky's width
    apart, so the loop never shows a seam."""
    span = w + CLOUD_SPAN
    out = []
    for k, (n, size, y0, y1, slow) in enumerate(CLOUD_LAYERS):
        rnd = random.Random(40 + k)                      # the same clouds on every refresh
        body = mix("#eef6fb" if k == 0 else "#fbfdff", "#2f3f66" if k == 0 else "#3a4a72", dusk)
        edge = mix("#d3e6f1", "#26355a", dusk)
        p = Pix()
        for i in range(n):
            cx, cy = -CLOUD_SPAN // 2 + int(span * (i + rnd.random() * 0.6) / n), rnd.randrange(y0, y1)
            for off in (0, span):
                for dx, dy, ww in ((6 * size, 0, 14 * size), (0, 4, 30 * size), (3 * size, 8, 26 * size)):
                    p.rect(cx + off + dx, cy + dy, ww, 4, body)
                p.rect(cx + off + 3 * size, cy + 12, 26 * size, 1, edge)
        period = int(span * slow)
        out.append(p.svg(' class="sm-cloud" style="--round:%ds;--px:%d;--span:-%dpx;--late:%s"'
                         % (period, span, span, clock_phase(now, period))))
    return out


TWINKLE = ((11, 6), (17, 6))     # each set of stars: the seconds between its dips, and how many stars


def twinkling_stars(w):
    """A few brighter stars that dip now and then, each set on its own beat; few, because every dip
    repaints the night sky under its tint."""
    rnd = random.Random(5)
    out = []
    for beat, n in TWINKLE:
        p = Pix()
        for _ in range(n):
            x, y = rnd.randrange(0, w), rnd.randrange(4, HOR - 30)
            p.dot(x, y, "#fffbe8")
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p.dot(x + dx, y + dy, "#c9d3f0")
        out.append(p.svg(' class="sm-twinkle" style="--beat:%ds"' % beat))
    return out


GULL_UP = ["kw.........wk", ".ww.......ww.", "..owwwwwwwg..", "....ggggg....", "............."]   # as wide as a hand
GULL_DOWN = [".............", ".............", "..owwwwwwwg..", ".ww.ggggg.ww.", "kw.........wk"]
GULL_INK = {"w": "#f7f7f2", "g": "#b9c0c8", "k": "#2a2a30", "o": "#e8a33a"}
GULLS = ((60, 46, False), (97, 74, True))    # seconds per round, the row it flies along, whether it flies east
GULL_OFF = 60                                # it starts and ends this far out of the scene


def gulls(w, now):
    """By day, a gull now and then: each crosses the sky in the first 40% of its round (shipmate.css,
    sm-fly) and is out of sight the rest, on rounds of different lengths so they rarely cross together."""
    out = []
    for period, y, east in GULLS:
        frames = []
        for k, rows in enumerate((GULL_UP, GULL_DOWN)):
            p = Pix()
            p.sprite(0, y, [r[::-1] for r in rows] if east else rows, GULL_INK)
            frames.append(p.svg(' class="sm-wing-%s"' % "ab"[k]))
        a, b = (-GULL_OFF, w + GULL_OFF) if east else (w + GULL_OFF, -GULL_OFF)
        out.append('<g class="sm-gull" style="--round:%ds;--from:%dpx;--to:%dpx;--late:%s">%s</g>'
                   % (period, a, b, clock_phase(now, period), "".join(frames)))
    return out


RIPPLES = {"calm": 14, "choppy": 9, "rough": 6}     # one little wave for this many art pixels of sea width


def ripples(w, sea, dusk):
    """Little waves that rise, roll a pixel or two and sink, in three sets a beat apart; as many as
    the sea already says (sea_state)."""
    rnd = random.Random(7)
    sets = [Pix(), Pix(), Pix()]
    for i in range(w // RIPPLES[sea]):
        y = rnd.randrange(HOR + 4, SHIP_H - 2)
        t = (y - HOR) / float(SHIP_H - HOR)
        x = rnd.randrange(-120, w + 120)
        c = mix(mix("#e6f3fa", "#b9dcef", t), mix("#6a8ab6", "#4a6a98", t), dusk)
        big = 1 if t > 0.45 else 0
        p = sets[i % 3]
        p.rect(x + 1, y, 2 + big, 1, c)                 # a crest ...
        p.dot(x, y + 1, c)                              # ... and its curl
        p.dot(x + 3 + big, y + 1, c)
    return [s.svg(' class="sm-ripple sm-ripple%d"' % k) for k, s in enumerate(sets)]


def two_frames(a, b, beat):
    """Two frames that take turns on a beat of `beat` seconds; with motion off the first stays."""
    return ('<g class="sm-amb" style="--beat:%.1fs">%s%s</g>'
            % (beat, a.svg(' class="sm-amb-a"'), b.svg(' class="sm-amb-b"')))


def glitter(w, night):
    """The sun's glitter on the sea under it, or the moon's at night."""
    sx = w - 90
    rnd = random.Random(11)
    col = ("#dfe6f2", "#aab6d8") if night else ("#fff6c8", "#ffe58a")
    a, b = Pix(), Pix()
    for y in range(HOR + 3, PIER - 1, 2):
        spread = 3 + (y - HOR) // 14
        for p in (a, b):
            if rnd.random() < 0.7:
                p.rect(sx + rnd.randrange(-spread, spread + 1), y, rnd.randrange(1, 4), 1, col[rnd.random() < 0.4])
    return '<g opacity="%s">%s</g>' % ("0.6" if night else "0.8", two_frames(a, b, 2.2))


def hull_rows(x0, length, d):
    """The hull's left and right edge on the row d pixels under the main deck's near edge."""
    left = x0 + max(0, d - 34) // 2
    right = x0 + length - int(((max(0, d) / float(WL - NEAR)) ** 1.7) * 70)
    return left, right


def hull_reflection(x0, length, dusk):
    """The hull mirrored in the water under it, broken into dashes by the waves."""
    a, b = Pix(), Pix()
    span = WL - NEAR
    for k in range(1, 15):
        d = span - k
        left, right = hull_rows(x0, length, d)
        c = mix(mix(mix("#8a5530", "#3e200d", d / float(span)), "#2c6f9f", 0.45 + k * 0.03), "#0c2440", dusk * 0.6)
        for p, off in ((a, 0), (b, 2)):
            for x in range(left + (k % 3) * 2 + off, right - 2, 7):
                p.rect(x, WL + k, 4 if k < 8 else 3, 1, c)
    return '<g opacity="0.55">%s</g>' % two_frames(a, b, 2.2)


def hull_foam(x0, length):
    """Foam where the hull meets the water, with a curl at the stem and at the stern."""
    left, right = hull_rows(x0, length, WL - 1 - NEAR)
    rnd = random.Random(3)
    a, b = Pix(), Pix()
    for x in range(left - 3, right + 4):
        for p, k in ((a, 0), (b, 1)):
            r = rnd.random()
            if r < 0.34:
                p.dot(x, WL - 1 + k, "#eef7fb")
            elif r < 0.5:
                p.dot(x, WL + 1, "#cfe6f2")
    for p, k in ((a, 0), (b, 1)):
        for dx, dy in ((0, 0), (1, -1), (2, -1), (3, 0), (4, 1)):
            p.dot(right + 1 + dx + k, WL - 1 + dy, "#f6fbfd")
            p.dot(left - 6 + dx - k, WL - 1 + dy, "#f6fbfd")
    return two_frames(a, b, 1.4)


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
    line or off duty with a mug, the first in the line that waits on a decision (hands aboard, and
    queued tasks with their sea bag), and the rest of the line crowding the door. Returns the cabin
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
    cp.figure(x, ctop, rows, CAPTAIN_INK)
    steam = ""
    if not busy:
        st = Pix()
        mx, my = CAPTAIN_MUG
        for dx, dy in ((1, 0), (0, -1), (1, -2)):
            st.dot(x + mx + dx, ctop + my + dy, "#ffffff")
        steam = st.svg(' class="sm-steam" opacity="0.8"')
    out.append('%s<g class="sm-captain%s"><title>%s</title>%s%s</g>'
               % (shadow(x + 9, feet + 1, 20), " is-busy" if busy else " is-off", esc(captain_title(sc)), cp.svg(), steam))
    if busy:
        o = sc["captain"]["oldest"]
        n = sc["captain"]["waiting"]
        balloon(tags, x + 9, ctop - 1, [tr("%d waiting for you") % n, tr("longest: %s, %s") % (o["id"], o["wait"])])
    numbers = []
    seated = set(lay["seated"])
    for h in sc["line"]:
        if h["id"] in seated:
            hx, hfeet, _pose, _upto = lay["hands"][h["id"]]
            out.append(hand_svg(h, lay["hands"][h["id"]], tags, extra=sea_bag(hx, hfeet) if h["kind"] == "queue" else None))
            numbers.append(line_number(h["line"], hx - 1, hfeet + 1))
    out.append(door_crowd(lay["crowd"], cx1))
    return "".join(out), "".join(numbers)


def sea_bag(x, feet):
    """A queued task's sea bag, on the floor behind a hand of width 13 that faces left: it is not aboard yet."""
    def paint(p, _top):
        bx = x + len(STAND[0])
        p.rect(bx, feet - 4, BAG_W, 5, "#c9b48a")
        p.rect(bx, feet - 4, BAG_W, 1, lit("#c9b48a"))
        p.rect(bx + 1, feet - 5, 2, 1, "#7a5a30")
    return paint


CROWD_HEADS = [(1, 14), (4, 12), (2, 19)]   # where the faces in the open door are, from its left edge and the cabin's top


def door_crowd(crowd, cx1):
    """The rest of the captain's line, crowding the cabin's open door: a few faces in their
    shirts, and the count over the door. A click on it finds them in the log."""
    if not crowd:
        return ""
    dx, top = cx1 - 9, CABIN_TOP
    p = Pix()
    for (ox, oy), x in zip(CROWD_HEADS, crowd):
        pal = hand_palette(x)
        p.rect(dx + ox - 1, top + oy + 5, 7, 4, pal["c"])
        p.rect(dx + ox, top + oy, 5, 2, pal["h"])
        p.rect(dx + ox, top + oy + 2, 5, 3, pal["s"])
        p.dot(dx + ox + 1, top + oy + 3, INK["e"])
        p.dot(dx + ox + 3, top + oy + 3, INK["e"])
    label = "+%d" % len(crowd)
    w = 4 + 3 * len(label)
    bx, by = dx + 5 - w // 2, top + 2
    title = tr("%d more in line at the door, numbers %d to %d: %s. Click to find them in the log.") % (
        len(crowd), crowd[0]["line"], crowd[-1]["line"], ", ".join(x["id"] for x in crowd))
    return ('<g class="sm-crowd" data-action="crowd" data-crowd="%s" tabindex="0" role="button" aria-label="%s"><title>%s</title>%s'
            '<g class="sm-num"><rect x="%d" y="%d" width="%d" height="7" rx="3.5"/><text x="%.1f" y="%.1f">%s</text></g></g>'
            % (esc(" ".join(x["id"] for x in crowd)), esc(title), esc(title), p.svg(), bx, by, w, bx + w / 2.0, by + 5.3, label))


def draw_quarters(sc, lay, tags, lights, night):
    """The quarters, cut open in the hull forward of the hold, when a queued task is held until a
    date: sea chests, a lantern, and a hammock for each, its date over it; the id shows on hover."""
    qr = lay["quarters"]
    if not qr:
        return ""
    qx0, qx1 = qr["box"]
    top, floor = QUARTERS_TOP, QUARTERS_FLOOR
    p = Pix()
    p.rect(qx0 - 2, top - 2, qx1 - qx0 + 4, floor - top + 4, "#e0b552")
    for y in range(top, floor):
        p.rect(qx0, y, qx1 - qx0, 1, mix("#2a1a10", "#4a2e18", (y - top) / float(floor - top)))
    for x in range(qx0 + 12, qx1, 26):
        p.rect(x, top, 1, floor - top - 3, "#1d1009")
    p.rect(qx0, floor - 3, qx1 - qx0, 3, "#9a6233")
    p.rect(qx0, floor - 3, qx1 - qx0, 1, "#c08a55")
    for x in qr["chests"]:                              # a sea chest under each pair of hammocks
        p.rect(x, floor - 8, 10, 6, "#7a4a24")
        p.rect(x, floor - 8, 10, 1, "#a06a32")
        p.rect(x + 4, floor - 6, 2, 2, "#e0b552")
    lx = qx1 - 10                                       # a lantern, forward
    p.rect(lx, top, 1, 5, "#2a170b")
    p.rect(lx - 2, top + 5, 5, 5, "#f2c35a")
    p.rect(lx - 1, top + 6, 2, 2, "#fff0b0")
    lights.append('<circle class="sm-glow" cx="%d" cy="%d" r="%d" fill="#ffd77a" opacity="0.22"/>' % (lx, top + 8, 16 if night else 10))
    out = ['<g><title>%s</title>%s</g>' % (esc(tr("the quarters below deck: queued tasks held until a date sleep here, the earliest date aft")), p.svg())]
    who = {q["id"]: q for q in sc["quarters"]}
    for qid, (x, low) in qr["beds"].items():
        q = who[qid]
        h = Pix()
        hammock_sprite(h, q, x, low, top + 1)
        title = hand_title(q)
        out.append('<g class="sm-hand pose-hammock" data-id="%s" data-stage="date" tabindex="0" role="button" aria-label="%s"><title>%s</title>%s</g>'
                   % (esc(qid), esc(tr("%s: details") % title), esc(title), h.svg()))
        cx = x + HAMMOCK_W // 2 - 1
        tags.place(q["tag"], cx, low - 20, qid, hover=True)     # first, so a hover finds it
        tags.place(q["date"], cx, low - 12, qid)
    if qr["more"]:
        out.append('<g><title>%s</title><text class="sm-ship__more" x="%d" y="%d">+%d</text></g>'
                   % (esc(", ".join(q["id"] for q in qr["more"])), qr["more_x"], floor - 20, len(qr["more"])))
    sign(tags, qr["label"], (qx0 + qx1) // 2, floor - 1)
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
    sign(tags, tr("RAIL · waiting for merge"), lay["rail_x"] + 30, NEAR + 2)
    sign(tags, tr("BOW · PR open and CI"), lay["fc_x"] + lay["fc_len"] // 2, NEAR + 2)
    hx0, hx1 = lay["hold"]
    sign(tags, tr("HOLD · quota barrels, context water"), (hx0 + hx1) // 2, NEAR + 16)
    sign(tags, tr("GANGPLANK · ashore and aboard"), x0 + 48, PIER + 4)
    a, b = light_span(lay)
    sign(tags, tr("LIGHTHOUSE · CI"), (a + b) // 2, PIER + 11)
    for text, cx, cls in lay["pier_signs"]:
        sign(tags, text, cx, PIER + 11, cls)
    if lay["scouts"]:
        sign(tags, tr("SCOUTS"), min(v[0] for v in lay["scouts"].values()) + 20, HOR - 4)
    sign(tags, tr("DELIVERED · %d days") % DAYS, lay["crates_x"] + 14, PIER + 10)


def pier_prop(p, kind, x, x1=None):
    """The pier's furniture: a card table, a bench, a keg, a sea bag, a lantern post."""
    if kind == "table":
        p.rect(x, PIER + 3, 14, 2, "#8a5530")
        p.rect(x, PIER + 3, 14, 1, "#b07a45")
        p.rect(x + 1, PIER + 5, 1, 4, "#5a3418")
        p.rect(x + 12, PIER + 5, 1, 4, "#5a3418")
    elif kind == "bench":
        p.rect(x, PIER + 5, x1 - x, 2, "#7a4a24")
        p.rect(x, PIER + 5, x1 - x, 1, "#a06a32")
        for lx in sorted({x + 1, x1 - 2} | set(range(x + 26, x1 - 8, 26))):
            p.rect(lx, PIER + 7, 1, 2, "#3e220f")
    elif kind == "keg":
        p.shaded(x, PIER_FEET - 11, BARREL, BARREL_INK)
    elif kind == "bag":
        p.rect(x, PIER_FEET - 4, 4, 5, "#c9b48a")
        p.rect(x, PIER_FEET - 4, 4, 1, lit("#c9b48a"))
        p.rect(x + 1, PIER_FEET - 5, 2, 1, "#7a5a30")
    elif kind == "lantern":
        p.rect(x, PIER - 12, 1, 20, "#3e220f")
        p.rect(x - 2, PIER - 17, 5, 5, "#24170d")
        p.rect(x - 1, PIER - 16, 3, 3, "#f2c35a")
        p.dot(x - 1, PIER - 16, "#fff0b0")


def talk_bubble(x, y):
    """A small speech balloon with three dots, its tail at (x, y): a hand talking."""
    p = Pix()
    p.rect(x - 1, y - 7, 10, 6, "#1d140c")
    p.rect(x, y - 6, 8, 4, "#fffdf7")
    for k in range(3):
        p.dot(x + 1 + 2 * k + (k > 0), y - 4, "#1d140c")
    p.dot(x, y - 1, "#1d140c")
    p.dot(x + 1, y - 2, "#fffdf7")
    return p.svg(' class="sm-talk"')


LAMP_STEP = 150      # between the lamp posts along the pier
LAMP_CLEAR = 8       # a lamp post keeps this far from the tavern and the other stretches it would crowd


def pier_lamp_xs(lay):
    """Where the lamp posts stand along the pier: every LAMP_STEP, but not in the tavern (it has
    its own lantern), at the foot of the gangplank, by the lighthouse, by the moored skiffs and the
    crates, or behind the near layer's lamp at the scene's edge."""
    x0, W = lay["x0"], lay["width"]
    busy = [(a - LAMP_CLEAR, b + LAMP_CLEAR) for a, b, _u in lay["rooms"]]
    busy += [(x0 + 10, x0 + 100), (lay["dock_x"] - LAMP_CLEAR, W)]
    a, b = light_span(lay)
    busy.append((a - LAMP_CLEAR, b + LAMP_CLEAR))
    return [x for x in range(46, W - 40, LAMP_STEP) if not any(a <= x <= b for a, b in busy)]


def draw_pier_lamps(lay, mid, lights, night):
    """Lamp posts along the pier: dark by day; from dusk to dawn (is_night, like the ship's lamps)
    lit, with a pool of light on the planks and their reflection in the water between the piles."""
    posts, ra, rb = Pix(), Pix(), Pix()
    for x in pier_lamp_xs(lay):
        posts.rect(x, PIER - 22, 2, 23, "#3e220f")
        posts.rect(x, PIER - 22, 1, 23, "#5a3418")
        posts.rect(x - 2, PIER - 29, 6, 7, "#24170d")
        posts.rect(x - 3, PIER - 30, 8, 1, "#24170d")
        posts.rect(x - 1, PIER - 28, 4, 5, "#f2c35a" if night else "#4a5a6a")
        posts.rect(x - 1, PIER - 28, 2, 2, "#fff0b0" if night else "#6a7d8e")
        if not night:
            continue
        lights.append('<circle class="sm-glow" cx="%d" cy="%d" r="16" fill="#ffd77a" opacity="0.24"/>' % (x + 1, PIER - 26))
        lights.append('<ellipse cx="%d" cy="%d" rx="16" ry="3" fill="#ffd77a" opacity="0.16"/>' % (x + 1, PIER + 4))
        rnd = random.Random(x)
        for y in range(PIER + 20, SHIP_H, 3):
            n = rnd.randrange(2, 5)
            ra.rect(x - 1 + rnd.randrange(-2, 3), y, n, 1, "#f2c35a")
            rb.rect(x - 1 + rnd.randrange(-2, 3), y + 1, n, 1, "#ffe08a")
    if night:
        mid.append(two_frames(ra, rb, 1.8))
    return posts.svg()


def awning_colors(slot, colors):
    """A tavern's awning: stripes in the colour of the project whose build slot it waits for, or
    navy for the home's lanes, on white canvas."""
    group = (slot or {}).get("group", "")
    return (colors.get(group, "#8a8a8a") if group else "#22345e"), "#f6f1e3"


ANCHOR = [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (-1, 1), (1, 1), (-2, 4), (2, 4), (-1, 5), (1, 5)]


def draw_tavern(sc, lay, lights, night):
    """Each tavern as a building behind its tables: an awning striped in its project's colour (navy
    with an anchor for the home's lanes), a plank wall with windows lit at night, a shelf of
    bottles, and two posts. It stands below the waterline, so it hides nothing of the hull."""
    p = Pix()
    for x0, x1, u in lay["rooms"]:
        a, b = x0 + 2, x1
        stripe, cloth = awning_colors(u["slot"], sc["colors"])
        for x in range(a, b):                                       # the wall, vertical planks
            p.rect(x, 266, 1, PIER - 266, "#3e220f" if (x - a) % 6 == 0 else "#5a3418")
        p.rect(a, 266, b - a, 1, "#2a170b")
        p.rect(a, 283, b - a, 2, "#7a4a24")                          # the shelf
        p.rect(a, 283, b - a, 1, "#a06a32")
        rnd = random.Random(x0)
        for x in range(a + 30, b - key_board_w(u["slot"]) - 14, 9):  # bottles, clear of the key board
            c = rnd.choice(["#3f7a3a", "#7a2e1a", "#2f5a8a", "#a5743f"])
            p.rect(x, 278, 3, 5, c)
            p.rect(x + 1, 276, 1, 2, c)
            p.dot(x, 279, lit(c, 0.4))
        for wx in range(a + 14, b - 16, 70):                         # windows
            p.rect(wx, 269, 12, 9, "#2a170b")
            if night:
                p.rect(wx + 1, 270, 10, 7, "#f2c35a")
                p.rect(wx + 1, 270, 4, 3, "#fff0b0")
                lights.append('<circle class="sm-glow" cx="%d" cy="274" r="13" fill="#ffd77a" opacity="0.22"/>' % (wx + 6))
            else:
                p.rect(wx + 1, 270, 10, 7, "#7fa9c4")
                p.rect(wx + 2, 271, 3, 1, "#d8ebf5")
            p.rect(wx + 6, 270, 1, 7, "#2a170b")
        if not (u["slot"] or {}).get("group"):                     # the home's anchor
            for dx, dy in ANCHOR:
                p.dot(a + 8 + dx, 269 + dy, "#e0b552")
        for y in range(258, 266):                                    # the awning, and its scalloped hem
            for x in range(a - 3, b + 3):
                c = stripe if ((x - a) // 4) % 2 == 0 else cloth
                p.dot(x, y, dark(c, 0.18) if y == 258 else c)
        for x in range(a - 3, b + 3):
            if (x - a) % 4 in (1, 2):
                p.dot(x, 266, stripe if ((x - a) // 4) % 2 == 0 else cloth)
        for px in (a - 2, b + 1):                                    # posts
            p.rect(px, 266, 2, PIER - 266, "#3e220f")
            p.rect(px, 266, 1, PIER - 266, "#5a3418")
    return p.svg()


def key_board_w(slot):
    cap = max(1, int((slot or {}).get("cap") or 1))
    return 4 + (5 if cap <= 6 else 4) * min(cap, 12)


def key_board(x1, u):
    """The tavern's key board, one hook per place it waits for: each key is out with the task that
    holds that place, so whoever waits here waits for any key to come back, where a hand talking
    up to the ship waits for that one hand. Pointing at it lights who holds the keys and who waits
    (the page script); the names are in its title."""
    slot = u["slot"] or {}
    cap = max(1, int(slot.get("cap") or 1))
    step = 5 if cap <= 6 else 4
    bw = key_board_w(slot)
    bx, by = x1 - bw - 6, 268
    p = Pix()
    p.rect(bx + bw // 2, by + 10, 2, PIER - by - 10, "#3e220f")    # its post
    p.rect(bx - 1, by - 1, bw + 2, 12, "#24170d")
    p.rect(bx, by, bw, 10, "#7a4a24")
    p.rect(bx, by, bw, 1, "#a06a32")
    for i in range(min(cap, 12)):
        hx = bx + 3 + step * i
        p.dot(hx, by + 2, "#e0b552")                                # the hook ...
        for dx, dy in ((0, 4), (-1, 5), (1, 5), (0, 6), (0, 7), (0, 8), (1, 8)):
            p.dot(hx + dx, by + dy, "#4e2c14")                      # ... and the shape of its key, gone
    holders = slot.get("holders") or []
    waiting = [q["id"] for q in u["cards"] + u["drink"]]
    if slot.get("group"):
        title = (tr("build slots for %s: %d of %d keys are out, with %s; %d wait for any of them to come back")
                 % (slot["group"], slot.get("taken", 0), cap, ", ".join(holders) or "?", len(waiting)))
    else:
        title = tr("home lanes: %d of %d in use; %d wait for any lane to come free") % (slot.get("taken", 0), cap, len(waiting))
    return ('<g class="sm-keys" data-ids="%s" tabindex="0" role="img" aria-label="%s"><title>%s</title>%s</g>'
            % (esc(" ".join(holders + waiting)), esc(title), esc(title), p.svg()))


def draw_pier(sc, lay, tags, lights, night):
    """The queue on the pier: the tavern's tables, bench and keg, the lines, the talkers with their
    balloons and a dotted line up to the hand they wait for, the dozing bench, and the counts of
    those with no room left."""
    spots, out = lay["pier"], [draw_tavern(sc, lay, lights, night)] + [key_board(x1, u) for _x0, x1, u in lay["rooms"]]
    props, cards = Pix(), Pix()
    for kind, x, *rest in lay["pier_props"]:
        pier_prop(props, kind, x, *(rest or [None]))
        if kind == "table":
            cards.rect(x + 4, PIER + 2, 2, 1, "#fbf6ea")            # cards laid on the table
            cards.rect(x + 8, PIER + 2, 2, 1, "#fbf6ea")
        if kind == "lantern" and night:
            lights.append('<circle cx="%d" cy="%d" r="12" fill="#ffd77a" opacity="0.25"/>' % (x, PIER - 14))
    who = sorted((q for q in sc["pier"] if q["id"] in spots), key=lambda q: (spots[q["id"]][1], spots[q["id"]][0]))
    behind = [q for q in who if spots[q["id"]][1] < PIER_FEET]
    for q in behind:
        out.append(hand_svg(q, spots[q["id"]], tags, hover_tag=True))
    out += [props.svg(), cards.svg(' class="sm-deal"')]
    for q in who:
        if q in behind:
            continue
        x, feet, pose, _u = spots[q["id"]]
        out.append(hand_svg(q, spots[q["id"]], tags, hover_tag=q["act"] in ("cards", "drink")))
        if q["act"] == "talk":
            to = lay["hands"].get(q["partner"]) or lay["scouts"].get(q["partner"])
            if to:
                out.append('<line class="sm-talkline" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (x + 13, feet - 14, to[0] + 6, to[1] - 16))
            out.append(talk_bubble(x + 15, feet - 5))           # beside the face, under its name tag
    for x, hidden in lay["pier_more"]:
        out.append('<g><title>%s</title><text class="sm-ship__more" x="%d" y="%d">+%d</text></g>'
                   % (esc(", ".join(q["id"] for q in hidden)), x, PIER_FEET - 3, len(hidden)))
    return "".join(out)


def render_ship(sc):
    """The ship as one SVG in art pixels; the page frames, zooms and pans it."""
    lay = ship_layout(sc)
    W, x0, L = lay["width"], lay["x0"], lay["length"]
    night, dusk = sc["night"], sc["dark"]
    rnd = random.Random(21)                     # the same stars, clouds and waves on every refresh
    tags = Tags()
    paint_signs(sc, lay, tags)
    colors = list(sc["colors"].values())
    far, mid = sky_and_sea(W, dusk, night, sc["sea"], rnd, sc["now"])
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
                       mix("#1d4f73", "#0a2238", dusk), ' opacity="0.45"'))
    mid.append(hull_reflection(x0, L, dusk))
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
    fm.figure(x0 + 28, 152 - len(HELM) + 1, HELM, HELM_INK)
    fm.shaded(x0 + 39, 152 - 10, WHEEL, {})
    f = sc["firstmate"]
    ctx = (tr("context %d%% of the window%s") % (round(f["context"]), tr(", past the %d%% warning") % f["warn_at"] if f["over"] else "")
           if f["context"] is not None else tr("context unknown"))
    people.append((152, shadow(x0 + 36, 153, 18) + '<g class="sm-fm"><title>%s</title>%s</g>'
                   % (esc(tr("firstmate at the helm: %s") % ctx), fm.svg())))
    for place in ("deck", "hatch", "bow", "watch"):
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
        left, right = hull_rows(x0, L, d)
        c = mix("#8a5530", "#3e200d", d / float(WL - NEAR))
        hull.rect(left, y, right - left, 1, dark(c, 0.25) if d % 7 == 6 else c)
    hull.rect(x0, NEAR, L, 1, "#e9c06a")
    hull.rect(x0, NEAR + 1, L, 3, "#9a6233")
    hull.rect(x0, NEAR + 6, L - 2, 3, "#22345e")
    ship.append(hull.svg())
    ship.append(hull_foam(x0, L))
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
    pump_x = hx1 - PUMP_FROM_END
    ship.append(draw_pump(pump_x, orlop, hy0, hy1, x0))
    ship += barrels
    # the quarters go before the hands at the rail, so a fishing line hangs in front of them
    ship.append(draw_quarters(sc, lay, tags, lights, night))
    ship.append('<text class="sm-ship__name" x="%d" y="%d">firstmate</text>' % (x0 + L - 60, WL - 12))
    for h in hands_at("rail"):
        x = spots[h["id"]][0]

        def line(p, t, x=x + ROD_TIP[0]):
            fishing_line(p, x, t + ROD_TIP[1] + 1, WL + 10)
        ship.append(hand_svg(h, spots[h["id"]], tags, extra=line, shadow_on=False))
        ship.append(bucket_svg(h, x - 16, spots[h["id"]][1]))
    # the pier: the queue on the pier (drawn after it), scouts back with their maps, crates of delivered work
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
    ashore.append(draw_pier_lamps(lay, mid, lights, night))
    ashore.append(draw_pier(sc, lay, tags, lights, night))
    ashore.append(lighthouse(sc, lay))
    # the newest MAX_CRATES crates, each its own drawing so the page can hold one back while its
    # hand carries it there (ship_events)
    crates = []
    for i, c in enumerate(sc["crates"][-MAX_CRATES:]):
        cx, cy = crate_spot(lay, i)
        cp = Pix()
        cp.shaded(cx, cy, CRATE, CRATE_INK)
        crates.append(cp.svg(' class="sm-crate" data-for="%s" data-at="%d,%d"' % (esc(c["id"]), cx, cy)))
    newest = [c["id"] for c in reversed(sc["crates"][-20:])]
    ashore.append('<g><title>%s</title>%s%s</g>'
                  % (esc(tr("%d task delivered in the last %d days%s" if len(sc["crates"]) == 1 else "%d tasks delivered in the last %d days%s")
                         % (len(sc["crates"]), DAYS, ": " + ", ".join(newest) if sc["crates"] else "")),
                     stuff.svg(), "".join(crates)))
    if len(sc["crates"]) > MAX_CRATES:
        ashore.append('<text class="sm-ship__more" x="%d" y="%d">+%d</text>'
                      % (lay["crates_x"] + 9 * (MAX_CRATES // 3) + 2, PIER - 4, len(sc["crates"]) - MAX_CRATES))
    bell = Pix()                                         # the ship's bell on the stern castle's rail
    bell.rect(x0 + 62, 126, 1, 8, "#3e220f")
    bell.rect(x0 + 62, 126, 9, 1, "#3e220f")
    bell.shaded(x0 + 66, 127, BELL, {})
    ship.append(bell.svg(' class="sm-bell"'))
    (px0, py0), (px1, py1) = lay["plank"]
    ashore.append(shadow(px1 + 10, py1, 24, 3, 0.25) + plank_svg(lay["plank"]))
    # the near layer: out of focus, dark and chunky, at the edges so it hides nobody
    fg = Pix()
    fg.rect(4, PIER + 13, 18, SHIP_H - PIER - 13, "#1c120a")      # below the pier's surface: the queue stands on it
    fg.rect(4, PIER + 13, 18, 3, "#2a1a10")
    for k in range(3):
        fg.rect(0, PIER + 18 + 6 * k, 28, 3, "#3a2414")
    fg.rect(W - 18, 214, 6, 126, "#1c120a")
    fg.rect(W - 26, 204, 22, 4, "#1c120a")
    fg.rect(W - 24, 208, 18, 14, "#24170d")
    fg.rect(W - 22, 210, 14, 10, "#f2c35a")
    fg.rect(W - 20, 211, 6, 4, "#fff0b0")
    for _ in range(W // 16):
        x = rnd.randrange(-40, W + 40)
        fg.rect(x, SHIP_H - 3, 14, 3, mix("#24577d", "#0a1c33", dusk))
        fg.rect(x + 2, SHIP_H - 5, 10, 2, mix("#2c6690", "#0f2846", dusk))
    near.append(fg.svg())
    lights.append('<circle class="sm-glow" cx="%d" cy="215" r="%d" fill="#fff0b0" opacity="%s"/>'
                  % (W - 15, 30 if night else 20, "0.3" if night else "0.18"))
    # the night's tint, the near layer and the lights lie over the hands: they take no clicks (see the stylesheet)
    shade = band(-300, -300, W + 600, SHIP_H + 300, "#081633", ' class="sm-shade" opacity="%.3f"' % (0.38 * dusk)) if dusk else ""
    # where the page's script walks a hand and works the pump (see ship_events)
    geo = {"ladder": [[main_x - 38, FAR], [main_x - 8, 46]], "walk": DECK_WALK, "deck_x": deck_x, "nest": nx,
           "door": lay["cabin"][1] - 8, "floor": CABIN_FLOOR - 2, "fc": [fc_x, BOW_WALK], "plank": [[px0, py0], [px1, py1]],
           "pier": PIER_FEET, "stern": x0, "wl": WL, "rod": list(ROD_TIP), "reel": list(REEL_TIP),
           "hold": [hx0, hx1, hy1, room, thr], "pump": [pump_x, hy0],
           "crates": lay["crates_x"], "dock": lay["dock_x"], "bell": [x0 + 66, 127], "edge": W}
    args = ((sc["sea"], " is-night" if night else "", W, SHIP_H, esc(tr("The ship: %s") % ship_counts(sc)), W, SHIP_H)
            + tuple(lay["crew"])
            + (esc(json.dumps(ship_print(sc), separators=(",", ":"))), esc(json.dumps(geo, separators=(",", ":"))))
            + (mix("#2c6f9f", "#0c2440", dusk), mix(DAY_SKY[0], NIGHT_SKY[0], dusk), "".join(far), "".join(mid),
               "".join(ship), "".join(ashore), shade, "".join(near), "".join(lights), tags.svg()))
    return ('<svg class="sm-ship__svg sea-%s%s" viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet" '
            'shape-rendering="crispEdges" role="group" aria-label="%s" data-w="%d" data-h="%d" data-crew="%d,%d,%d,%d" '
            'data-print="%s" data-geo="%s">'
            '<rect x="-3000" y="-3000" width="7000" height="7000" fill="%s"/>'
            '<rect x="-3000" y="-3000" width="7000" height="3010" fill="%s"/>'
            '<g class="sm-far">%s</g><g class="sm-mid">%s<g class="sm-bob">%s</g>%s</g>%s'
            '<g class="sm-near">%s</g><g class="sm-lights">%s</g><g class="sm-tags">%s</g></svg>' % args)


def crate_spot(lay, i):
    """Where the i-th crate of the pile on the pier stands: columns of three, from the left."""
    return lay["crates_x"] + 9 * (i // 3), PIER + 2 - 8 * (i % 3 + 1)


def plank_svg(plank):
    """The gangplank from the ship down to the pier, always out: a hand goes down it with its crate
    once merged, and a queued task comes up it to board (ship_events)."""
    (px0, py0), (px1, py1) = plank
    p = Pix()
    for k in range(5):
        p.line(px0 + k, py0, px1 + k, py1, ("#e0aa72", "#d6a06a", "#b07a45", "#b07a45", "#6b3a1c")[k])
    for t in (0.2, 0.4, 0.6, 0.8):
        p.rect(int(px0 + (px1 - px0) * t), int(py0 + (py1 - py0) * t), 5, 1, "#5a3418")
    return p.svg(' class="sm-plank"')


LIGHT_TALL = 104      # the lighthouse's tower, from the stone head to the gallery


def lighthouse(sc, lay):
    """The lighthouse ahead of the bow, on a stone head at the pier's edge. Its lamp says how CI
    goes for the hands at the bow with a spyglass on it: it blinks yellow while any of them waits,
    and is dark when none does; the page flashes it green or red as an answer comes (ship_events)."""
    x, base = lay["light_x"], PIER
    top = base - 4 - LIGHT_TALL
    p = Pix()
    p.rect(x - 5, base - 4, LIGHT_W, 5, "#7d7a74")                    # the stone head
    p.rect(x - 5, base - 4, LIGHT_W, 1, "#a8a49b")
    p.rect(x - 4, base + 1, LIGHT_W - 2, 2, "#5d5a55")
    for y in range(top, base - 4):                                   # the tower, red and white bands, narrower up top
        k = (base - 4 - y) * 3 // LIGHT_TALL
        w, left = 20 - k * 2, x + k
        c = "#c8402a" if ((base - 4 - y) // 16) % 2 else "#f2ece0"
        p.rect(left, y, w, 1, c)
        p.dot(left, y, lit(c))
        p.dot(left + w - 1, y, dark(c))
        p.dot(left + w - 2, y, dark(c, 0.15))
    p.rect(x + 8, base - 24, 4, 7, "#24170d")                        # a door and two windows
    p.rect(x + 9, base - 52, 2, 4, "#24170d")
    p.rect(x + 9, base - 84, 2, 4, "#24170d")
    p.rect(x - 1, top - 2, 22, 2, "#3e2a1c")                         # the gallery and its rail
    for i in range(0, 22, 3):
        p.rect(x - 1 + i, top - 6, 1, 4, "#3e2a1c")
    p.rect(x - 1, top - 6, 22, 1, "#3e2a1c")
    p.rect(x + 3, top - 16, 14, 1, "#3e2a1c")                        # the lantern room's frame
    p.rect(x + 3, top - 16, 1, 10, "#3e2a1c")
    p.rect(x + 16, top - 16, 1, 10, "#3e2a1c")
    p.rect(x + 2, top - 20, 16, 4, "#8a2a1c")                        # the cap
    p.rect(x + 2, top - 20, 16, 1, lit("#8a2a1c"))
    p.rect(x + 5, top - 22, 10, 2, "#8a2a1c")
    p.rect(x + 9, top - 25, 2, 3, "#3e2a1c")
    watchers = [h["id"] for h in sc["hands"] if h["place"] == "watch" and not h["line"]]
    title = (tr("the lighthouse: the checks are running for %s") % ", ".join(watchers) if watchers
             else tr("the lighthouse: nobody is waiting for CI"))
    return ('<g class="sm-beacon %s"><title>%s</title><circle class="sm-beacon__halo" cx="%d" cy="%d" r="13"/>%s'
            '<rect class="sm-beacon__lamp" x="%d" y="%d" width="12" height="9"/></g>'
            % ("is-running" if watchers else "is-idle", esc(title), x + 10, top - 11, p.svg(), x + 4, top - 15))


def sprite_icon(rows, pal, scale=2):
    """A small inline picture of a sprite, for the stage chips and the log."""
    p = Pix()
    p.figure(1, 1, rows, pal)
    w, h = max(len(r) for r in rows) + 2, len(rows) + 2
    return ('<svg class="sm-icon" viewBox="0 0 %d %d" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (w, h, w * scale, h * scale, p.svg()))


def pose_icon(pose, scale=2, color=None):
    """A small inline picture of a hand in a pose, painted by hand_sprite like the hands in the scene."""
    p = Pix()
    rows, _top, _head = hand_sprite(p, {"id": "key", "color": color or PROJECT_COLORS[0]}, 1, len(POSES[pose]), pose)
    w, h = max(len(r) for r in rows) + 2, len(rows) + 2
    return ('<svg class="sm-icon" viewBox="0 0 %d %d" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (w, h, w * scale, h * scale, p.svg()))


def hammock_icon(scale=2):
    """A small inline picture of a hand asleep in a hammock, for the stage chip of the dated holds."""
    p = Pix()
    hammock_sprite(p, {"id": "key", "color": PROJECT_COLORS[0]}, 1, 16, 1)
    w, h = HAMMOCK_W + 2, 19
    return ('<svg class="sm-icon" viewBox="0 0 %d %d" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (w, h, w * scale, h * scale, p.svg()))


def flag_icon(scale=2):
    p = Pix()
    red_flag(p, 7, 1)
    return ('<svg class="sm-icon" viewBox="0 0 9 15" width="%d" height="%d" shape-rendering="crispEdges" aria-hidden="true">%s</svg>'
            % (9 * scale, 15 * scale, p.svg()))


# the stages a chip lights: key, pose, name, and where a hand stands and what that means
STAGES = [
    ("nest", "spy", "exploring", "in the crow's nest with a spyglass: reading the code and planning before it builds"),
    ("deck", "hammer", "implementing", "on deck with a hammer, a mop or a broom: making the change"),
    ("bow", "signal", "PR open", "at the bow with signal flags: the pull request is open"),
    ("watch", "spy", "waiting for CI", "at the bow with a spyglass on the lighthouse: the checks are running, and the light "
                                       "blinks yellow; it flashes green or red when they come back"),
    ("rail", "fish", "waiting for merge", "fishing at the rail: every check passed, the merge is next; one more fish in the "
                                          "bucket for each hour it waits, up to four"),
    ("hatch", "stand", "phase unknown", "under a question mark: no sign of its phase yet"),
    ("scouts", "scroll", "scouts", "in a skiff: rowing out to explore, back with the map, then moored at the pier with the report"),
    ("off", "rest", "off duty", "sitting with the dice: paused, or quiet for a while"),
    ("slot", "cards", "waiting for a slot", "at a card table on the pier, or at the tavern bench when queued before today: "
                                            "waits for a free build slot or lane"),
    ("board", "stand", "ready to board", "at the foot of the gangplank with a sea bag: queued, and nothing holds it"),
    ("task", "talk", "waiting for a task", "on the pier talking up to the hand it waits for, or standing apart when that task is not aboard"),
    ("date", "hammock", "held until a date", "asleep in a hammock in the quarters below deck: queued and held until its date"),
    ("flag", None, "waiting for you", "a red flag: needs your decision; hands and queued tasks wait in your cabin, longest wait "
                                      "first, the rest crowd the door, and the log below lists them all"),
]


PIER_STAGES = ("slot", "board", "task", "date")


def stage_members(sc, key):
    if key == "flag":
        return sc["line"]
    if key == "off":
        return [h for h in sc["hands"] if h["off"]]
    if key == "scouts":
        return sc["scouts"]
    if key == "date":
        return sc["quarters"]
    if key in PIER_STAGES:
        return [q for q in sc["pier"] if q["stage"] == key]
    return [h for h in sc["hands"] if h["place"] == key]


def stage_chips(sc):
    """One chip per stage, with how many are in it; a click lights them in the scene."""
    out = []
    for key, pose, name, where in STAGES:
        who = stage_members(sc, key)
        if not who and key in ("hatch", "scouts") + PIER_STAGES:
            continue
        name, where = tr(name), tr(where)
        desc = "%s: %s. %s" % (name[0].upper() + name[1:], where,
                               (tr("Now: %s.") % ", ".join(x["id"] for x in who)) if who else tr("Nobody now."))
        out.append('<button type="button" class="sm-chip%s" data-stage="%s" data-desc="%s" aria-pressed="false"%s>%s<span>%s</span><b>%d</b></button>'
                   % (" sm-chip--red" if key == "flag" else "", key, esc(desc), "" if who else " disabled",
                      flag_icon(1) if pose is None else hammock_icon(1) if pose == "hammock" else pose_icon(pose, 1), esc(name), len(who)))
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
                icon, what, why = pose_icon("stand", 2, x["color"]), tr("queued, %s") % (x["repo"] or tr("no project")), x["hold"] or ", ".join(x["why"])
            else:
                pose = "scroll" if x["kind"] == "scout" else PLACE_POSE.get(x["place"], "stand")
                said = x["why"][1:] if x["crew"] in CAPTAIN_STATES else x["why"]      # the open line says more than the crew state
                icon, what, why = pose_icon(pose, 2, x["color"]), "%s, %s" % (x["phase"], x["project"] or tr("no project")), (said or x["why"])[0]
            cards.append('<button type="button" class="sm-logcard" data-id="%s" title="%s"><span class="sm-logcard__n">%d</span>%s'
                         '<span class="sm-logcard__body"><b>%s</b><span>%s</span><em>%s</em></span><span class="sm-logcard__wait">%s</span>%s</button>'
                         % (esc(x["id"]), esc("%s: %s" % (x["id"], why)), x["line"], icon, esc(x["id"]), esc(what), esc(why), esc(x["wait"]),
                            '<span class="sm-logcard__ans">%s</span>' % answer_pill(x["answer"]) if x.get("answer") else ""))
        body = "".join(cards)
    else:
        say, body = tr("All clear. Nothing waits on my word."), '<span class="sm-log__none">%s</span>' % esc(tr("Nobody is waiting for you."))
    return ('<div class="sm-log__capt">%s<p><b>%s</b><span>%s</span></p></div><div class="sm-log__cards">%s</div>'
            % (sprite_icon((CAPTAIN_BUSY if cap["busy"] else CAPTAIN_OFF)[:15], CAPTAIN_INK, 3), esc(tr("Captain")), esc(say), body))


def block_ship(s, prev=None):
    """The ship's parts of the page; with the print of the drawing the page shows, also the
    animations from that drawing to this one (ship_events)."""
    sc = ship_scene(s)
    events = [event_sprites(e, sc) for e in ship_events(prev, ship_print(sc))] if prev else []
    chips = "".join('<li><span class="sm-sw" style="background:%s"></span>%s</li>' % (c, esc(p))
                    for p, c in sc["colors"].items()) or '<li class="sm-faint">%s</li>' % esc(tr("no project aboard"))
    necks = "".join('<li><span class="sm-sw" style="background:%s"></span>%s</li>' % (c, esc(a))
                    for a, c in sc["accounts"].items()) or '<li class="sm-faint">%s</li>' % esc(tr("no account aboard"))
    return {"ship": render_ship(sc), "counts": ship_counts(sc), "projects": chips, "accounts": necks, "stages": stage_chips(sc),
            "log": captain_log(sc), "events": events}


# ---------------------------------------------------------------- demo (--demo)


def demo_snapshot(now=None, moved=False):
    """A made-up home with every phase, flag and gauge in use, for `--demo` and the tests.
    Every name in it is invented; nothing is read from disk. `moved` is the same home a moment
    later, so the ship's animations have something to show: a lookout went down to the deck, a
    deck hand asked the captain, the blocked hand was answered, a ready task was dispatched up the
    gangplank, the checks of one of the hands watching the lighthouse passed and it went fishing,
    the longest fisher caught one more fish, firstmate's context was compacted, a fisher was merged
    and went down the gangplank with its crate, a deck hand without a PR was delivered and handed
    its build slot to a card player, and a new task joined the tavern to wait for that slot."""
    now = time.time() if now is None else now
    h = 3600

    def agent(tid, title, project, phase, crew="working", kind="ship", decisions=(), pr=None, ago=2 * h, quiet=300):
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
                "context_how": "inferred", "last_activity": now - quiet, "checks": None,
                "decisions": [{"state": st, "key": k, "at": int(now - age), "text": t} for st, k, t, age in decisions]}

    agents = [
        agent("DEMO-SEARCH", "Find where the search index drifts", "harbor-api", 1 if moved else 0),
        agent("DEMO-AUTH", "Rotate session tokens on login", "harbor-api", 1, ago=3 * h, quiet=30),
        agent("DEMO-SYNC", "Sync the tide tables offline", "tide-charts", 1, ago=h, quiet=20),
        agent("DEMO-RATE", "Rate limit the public endpoints", "harbor-api", 1, crew="parked",
              decisions=[("needs-decision", "limits", "per user or per key? A) per user B) per key", 50 * 60)]),
        agent("DEMO-CACHE", "Cache the tide tables", "harbor-api", 2),
        agent("DEMO-LOGS", "Structured logs for the workers", "harbor-api", 4 if moved else 3, crew="done" if moved else "working"),
        agent("DEMO-I18N", "Translate the settings page", "harbor-api", 4, crew="done"),
        agent("DEMO-DARK", "Dark theme for the charts", "lighthouse-web", 0, crew="paused"),
        agent("DEMO-NAV", "Keyboard navigation in the menu", "tide-charts", 1,
              decisions=[("needs-decision", "keys", "which key opens the menu? A) Alt+M B) F10", 60)] if moved else ()),
        agent("DEMO-FORMS", "Validate the signup form", "lighthouse-web", 3),
        agent("DEMO-ICONS", "Redraw the toolbar icons", "lighthouse-web", 4, crew="done"),
        agent("DEMO-EXPORT", "Export charts as CSV", "tide-charts", 2, crew="working" if moved else "blocked",
              decisions=() if moved else [("blocked", "bucket", "the export bucket refuses writes from the worker", 3 * h + 5 * 60)]),
        agent("DEMO-AXIS", "Fix the time axis labels", "tide-charts", None, crew="unknown"),
        agent("DEMO-SCOUT-DB", "Compare two storage engines", "harbor-api", 0, kind="scout"),
        agent("DEMO-SCOUT-CDN", "Survey the image CDN costs", "lighthouse-web", 1, kind="scout"),
        agent("DEMO-SCOUT-UX", "Review the onboarding flow", "lighthouse-web", 2, crew="done", kind="scout"),
    ]
    waited = {"DEMO-LOGS": 10, "DEMO-I18N": 20 * 60, "DEMO-ICONS": (3 * h + 10 * 60) if moved else (2 * h + 50 * 60)}
    for a in agents:                                    # the PRs' checks, and how long the fishers have waited for the merge
        if a["id"] == "DEMO-LOGS":
            a["checks"] = "pass" if moved else "pending"
        elif a["id"] == "DEMO-FORMS":
            a["checks"] = "pending"
        elif a["phase"] == 4 and a["kind"] == "ship":
            a["checks"] = "pass"
        if a["phase"] == 4 and a["id"] in waited:
            a["phase_since"] = now - waited[a["id"]]
    if moved:
        agents.append(agent("DEMO-Q-THEME", "Theme picker", "lighthouse-web", 0, ago=60))
        # delivered: a fisher at the rail, and the deck hand without a PR, whose build slot the
        # first card player took
        agents = [a for a in agents if a["id"] not in ("DEMO-I18N", "DEMO-AUTH")]
        agents.append(agent("DEMO-Q-PUSH", "Push notifications", "harbor-api", 0, ago=60))
    day = dt.date.fromtimestamp(now)
    holders = ["DEMO-Q-PUSH", "DEMO-RATE"] if moved else ["DEMO-AUTH", "DEMO-RATE"]
    slot = {"group": "harbor-api", "cap": 2, "taken": 2, "holders": holders}

    def queued(tid, title, repo, reason, kind="ship", hold="", deps=(), days=0):
        short, full = {
            "slot": (tr("build slot %d/%d") % (2, 2), tr("build slots for %s: %d of %d taken (%s)") % (repo, 2, 2, ", ".join(holders))),
            "decision": (tr("captain's decision"), hold),
            "date": (tr("held until %s") % fmt_date_iso((day + dt.timedelta(days=3)).isoformat()), "held"),
            "task": (tr("waits for %s") % ", ".join(deps), tr("waits for %s") % ", ".join(deps)),
            "ready": (tr("ready to dispatch"), tr("nothing holds it: firstmate can dispatch it")),
        }[reason]
        return {"id": tid, "title": title, "repo": repo, "kind": kind, "created": (day - dt.timedelta(days=days)).isoformat(),
                "reasons": [(reason, short, full)], "hold_reason": hold, "deps": list(deps),
                "hold_until": (day + dt.timedelta(days=3)).isoformat() if reason == "date" else "",
                "slot": slot if reason == "slot" else None}

    # every way to wait: for a build slot (cards today, the tavern since an earlier day), with nothing
    # holding it, for a hand aboard or for a queued task, for a date, and for the captain
    queue = ([] if moved else [queued("DEMO-Q-PUSH", "Push notifications", "harbor-api", "slot")]) + [
        queued("DEMO-Q-BILL", "New billing plan", "harbor-api", "decision", hold="Billing plans (data/DEMO-PLANS-105/report.html): (1) which plan ships first: the flat monthly plan "
                    "(recommended), the per-seat plan or the usage plan; (2) where to start: two fronts, price the plans per region now "
                    "and one trail per tax rule (recommended), only prices or only tax; (3) the 8 product choices of the 'decide' tiers "
                    "(a recommendation on each); (4) retired plans; (5) widen the trial rule. "
                    "See also billing/plans/inventory-with-a-very-long-unbroken-name-of-one-plan.report.json."),
        queued("DEMO-Q-MAPS", "Offline maps", "lighthouse-web", "date"),
        queued("DEMO-Q-AUDIT", "Audit log", "harbor-api", "ready") if moved else
        queued("DEMO-Q-AUDIT", "Audit log", "harbor-api", "task", deps=["DEMO-AUTH"]),
    ] + ([] if moved else [queued("DEMO-Q-THEME", "Theme picker", "lighthouse-web", "ready")]) + [
        queued("DEMO-Q-TILES", "Faster chart tiles", "tide-charts", "decision", kind="scout", hold="worth a scout?"),
        queued("DEMO-Q-DOCS", "API reference pages", "harbor-api", "slot"),
        queued("DEMO-Q-WEBHOOK", "Outgoing webhooks", "harbor-api", "slot"),
        queued("DEMO-Q-QUOTAS", "Per-team quotas", "harbor-api", "slot", days=2),
        queued("DEMO-Q-EXPORT2", "Export to spreadsheets", "tide-charts", "task", deps=["DEMO-EXPORT"]),
        queued("DEMO-Q-AUDITUI", "Audit log page", "lighthouse-web", "task", deps=["DEMO-Q-AUDIT"]),
    ] + ([queued("DEMO-Q-CSV", "CSV import", "harbor-api", "slot")] if moved else [])     # it walks into the tavern

    def lane(label, rem, reset_h, runway, exhaust_h=None):
        return {"label": label, "plan": "max", "status": "fresh", "remaining": rem, "limit_label": "weekly",
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
    closed = [{"id": r["id"], "kind": r["kind"], "repo": "harbor-api", "closed": (day - dt.timedelta(days=1 + i // 2)).isoformat()}
              for i, r in enumerate(delivered_rows)]
    closed.sort(key=lambda r: (r["closed"], r["id"]))
    if moved:
        closed += [{"id": tid, "kind": "ship", "repo": "harbor-api", "closed": day.isoformat()} for tid in ("DEMO-AUTH", "DEMO-I18N")]
    fm = {"session": "demo0000-session", "found_by": tr("the demo"), "model": "model-a", "context": 144000,
          "last_call": now - 60, "limit": 200000, "limit_how": "inferred", "percent": 72.0,
          "warn_at": DEFAULT_CONTEXT_WARN, "growth": 38000, "growth_since": now - GROWTH_HOURS * h,
          "growth_since_compaction": False, "today_new": 1200000, "today_cache": 30000000,
          "compactions": 1, "last_compaction": {"at": now - 7 * h, "trigger": "auto", "before": 180000, "after": 40000}}
    if moved:
        fm.update(context=38000, percent=19.0, growth=0, growth_since=now - 40, growth_since_compaction=True, compactions=2,
                  last_compaction={"at": now - 40, "trigger": "auto", "before": 146000, "after": 38000})
    return {
        "now": now,
        "firstmate": fm,
        "agents": agents, "queue": queue, "backlog_error": None,
        "projects": ["harbor-api", "lighthouse-web", "tide-charts"], "accounts": ["account-a", "account-b", "ordinary"],
        "daily": daily, "delivered": delivered_rows, "closed": closed,
        "quota": {"lanes": [lane("account-a", 8, 30, "projected_exhaustion", 5),
                            lane("account-b", 41, 50, "through_reset"),
                            # its token ran out, so the reading is the last one taken: the doctor button shows
                            dict(lane("ordinary", 86, 90, "through_reset"), status="stale", reason="credentials_expired",
                                 auth="expired_refreshable")], "at": None, "error": None},
        "doctor": {"accounts": ["account-a", "account-b", "ordinary"], "results": {}},
        "machine": {"load": 6.0, "cpus": 8},
        # the captain has answered two of the decisions: firstmate replied to one and has read the other
        "answers": {
            "DEMO-Q-BILL": [{"note": "demo-1", "at": now - 20 * 60, "state": "replied",
                             "words": "Plan B first; plan A after the holidays.",
                             "reply": {"at": now - 12 * 60, "text": "Released DEMO-Q-BILL with plan B; plan A stays queued with a date."}}],
            "DEMO-EXPORT": [{"note": "demo-2", "at": now - 8 * 60, "state": "received",
                             "words": "I gave the worker write access to the bucket; try again.", "reply": None}],
        },
        "answers_error": None,
    }


class DemoPanel:
    """`--demo`: serves demo_snapshot() in place of a home, a moment later every other refresh; the
    add form queues nothing."""
    @property
    def notice(self):
        return '<div class="sm-notice sm-notice--warn">%s</div>' % esc(tr("Demo: a made-up home, not real data."))

    def __init__(self, port=DEFAULT_PORT):
        self.cfg = argparse.Namespace(port=port, home=os.devnull)   # no report file can exist under it
        self.token = secrets.token_urlsafe(32)
        self.last = None
        self.sent = {}                       # answers sent on this page, kept here only: task id -> rows

    def snapshot(self):
        # the made-up home changes every refresh between two moments, so the ship has moves to show
        now = time.time()
        snap = demo_snapshot(now, moved=int(now // REFRESH_SECONDS) % 2 == 1)
        for tid, rows in self.sent.items():
            snap["answers"].setdefault(tid, []).extend(rows)
        self.last = snap
        return snap

    def recent(self):
        return self.last if self.last and time.time() - self.last["now"] < REFRESH_SECONDS else self.snapshot()

    def detail(self, tid):
        snap, now = self.recent(), time.time()
        answer = answer_section(tid, captain_asks(snap, tid) or [], snap["answers"].get(tid, []), self.token, now)
        a = next((x for x in snap["agents"] if x["id"] == tid), None)
        if a is not None:
            status = [{"state": d["state"], "at": d["at"], "key": d["key"], "text": d["text"]} for d in a["decisions"]]
            return render_agent_detail(a, "A made-up task for the demo.", status, None, [], now, answer)
        q = next((x for x in snap["queue"] if x["id"] == tid), None)
        return render_queue_detail(q, {}, None, [], now, answer) if q is not None else None

    def answer(self, tid, ref, option, words, form_id):
        prep = prepare_answer(self.recent(), tid, ref, option, words)
        if "error" in prep:
            return {"ok": False, "error": prep["error"]}
        rows = self.sent.setdefault(tid, [])
        rid = answer_request_id(form_id, tid)
        if not any(x["request"] == rid for x in rows):          # the same form sent again is the same answer
            rows.append({"note": "demo-sent-%d" % (sum(map(len, self.sent.values())) + 1), "request": rid, "at": time.time(),
                         "state": "sent", "words": note_words(prep["body"]), "reply": None})
        self.last = None
        return {"ok": True, "state": "sent", "note": rows[-1]["note"],
                "message": tr("Demo: the answer stays on this page; nothing reached firstmate.")}

    def announce(self, note_id):
        return {"ok": False, "error": tr("this is the demo: firstmate is not here")}

    def add_task(self, title, project, text, request_id):
        return {"ok": False, "error": tr("this is the demo: nothing was queued")}

    def doctor(self, label):
        return {"ok": False, "error": tr("this is the demo: claude doctor was not run")}

    def clear_view(self):
        return None

    def clear_drawer(self):
        snap = self.recent()
        return render_clear(snap.get("firstmate"), None, {"target": "demo:w1:p1", "status": "idle"},
                            [tr("this is the demo: firstmate is not here")], self.token, time.time())

    def clear_start(self, form_id):
        return {"ok": False, "error": tr("this is the demo: firstmate is not here")}

    def clear_cancel(self):
        return {"ok": False, "error": tr("no clear request is waiting")}


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
            data = body if isinstance(body, bytes) else body.encode("utf-8")
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
                page, nonce = render_page(panel, dict(panel.snapshot(), clear=panel.clear_view()),
                                          getattr(panel, "notice", "") + notice_html(urllib.parse.parse_qs(u.query)))
                return self.send(200, page, "text/html; charset=utf-8", nonce=nonce)
            if u.path == "/favicon.png":
                return self.send(200, favicon_png(), "image/png")
            if u.path == "/blocks":
                return self.send(200, json.dumps(render_blocks(dict(panel.snapshot(), clear=panel.clear_view()))),
                                 "application/json")
            if u.path == "/clear":
                return self.send(200, panel.clear_drawer(), "text/html; charset=utf-8")
            if u.path == "/ship":
                prev = parse_print((urllib.parse.parse_qs(u.query).get("prev") or [""])[0])
                return self.send(200, json.dumps(block_ship(panel.recent(), prev)), "application/json")
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
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/task", "/answer", "/announce", "/doctor", "/clear", "/clear/cancel"):
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
            if path == "/answer":
                if not RE_FORM_ID.match(get("request")):
                    res = {"ok": False, "error": tr("this form has no request id; reopen the drawer")}
                else:
                    res = panel.answer(get("task"), get("ref"), get("option").strip(),
                                       get("text").replace("\r\n", "\n").strip(), get("request"))
                return self.send(200, json.dumps(res), "application/json")
            if path == "/doctor":
                # the account is a label looked up in the home's own list; the request never names a path
                return self.send(200, json.dumps(panel.doctor(get("account"))), "application/json")
            if path == "/clear":
                res = (panel.clear_start(get("request")) if RE_FORM_ID.match(get("request"))
                       else {"ok": False, "error": tr("this form has no request id; reopen the drawer")})
                return self.send(200, json.dumps(res), "application/json")
            if path == "/clear/cancel":
                return self.send(200, json.dumps(panel.clear_cancel()), "application/json")
            if path == "/announce":
                note = get("note")
                res = (panel.announce(note) if re.match(r"^[A-Za-z0-9._-]{1,128}$", note) and ".." not in note
                       else {"ok": False, "error": tr("no answer from shipmate has that note id")})
                return self.send(200, json.dumps(res), "application/json")
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
    ap.add_argument("--cache-seconds", type=float,
                    default=float(os.environ.get("SHIPMATE_CACHE_SECONDS") or DEFAULT_CACHE_SECONDS),
                    help="seconds one reading of the fleet answers every request (default 5; 0 turns the cache off)")
    ap.add_argument("--clear-timeout", type=int,
                    default=int(os.environ.get("SHIPMATE_CLEAR_TIMEOUT") or DEFAULT_CLEAR_TIMEOUT),
                    help="seconds firstmate has to stow and reply before a clear request lapses (default 900)")
    ap.add_argument("--herdr-session", default=os.environ.get("SHIPMATE_HERDR_SESSION") or "default",
                    help="the herdr session that runs firstmate (default: default)")
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
                 a.context_limit, a.context_warn, os.environ.get("SHIPMATE_GH_CMD") or "gh", max(0.0, a.cache_seconds),
                 os.environ.get("SHIPMATE_CLAUDE_CMD") or "claude", os.environ.get("SHIPMATE_HERDR_CMD") or "herdr",
                 a.herdr_session, max(60, a.clear_timeout))
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
