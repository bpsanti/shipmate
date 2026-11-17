#!/usr/bin/env python3
"""shipmate tests: small synthetic fixtures only (no real transcript, home, or account)."""
import datetime as dt
import html
import http.client
import json
import os
import random
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shipmate  # noqa: E402

WT = "/work/pool/demo-app/1/demo-app"
T0 = 1_900_000_000          # a fixed epoch for every fixture


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode:
        os.chmod(path, mode)
    return path


class Transcript:
    """Minimal JSONL in the shape Claude Code writes: one line per content block, same message id."""
    ids = 0     # message ids are unique across every transcript, as the API's are

    def __init__(self):
        self.lines = []
        self.n = 0

    def brief(self, t, tid):
        self.lines.append({"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "FIRSTMATE_OP: v1 launch-brief: inbox `/h/state/%s.inbox`" % tid}]}})

    def call(self, t, tools=(), inp=10, cw=100, cr=1000, out=20, model="claude-model-a"):
        self.n += 1
        Transcript.ids += 1
        mid = "msg_%06d" % Transcript.ids
        blocks = [{"type": "tool_use", "id": "tu_%d_%d" % (self.n, i), "name": n, "input": x} for i, (n, x) in enumerate(tools)] \
            or [{"type": "text", "text": "."}]
        for i, b in enumerate(blocks):
            usage = {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
                     "output_tokens": out if i == len(blocks) - 1 else 1}
            self.lines.append({"type": "assistant", "timestamp": iso(t), "message": {
                "id": mid, "model": model, "content": [b], "usage": usage}})
        return ["tu_%d_%d" % (self.n, i) for i in range(len(blocks))]

    def result(self, t, tool_id, text):
        self.lines.append({"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}})

    def save(self, path):
        return write(path, "".join(json.dumps(x) + "\n" for x in self.lines))


FAKE_TASKS = r"""#!/bin/sh
# fake fm-tasks-axi.sh: canned list, logged add
case "$1" in
  list)
    case "$*" in
      *body*) cat "$FAKE_DIR/bodies.toon" 2>/dev/null || exit 1 ;;
      *) cat "$FAKE_DIR/list.toon" ;;
    esac ;;
  show) cat "$FAKE_DIR/show.txt" ;;
  add)
    printf '%s' "$*" | tr '\n' '|' >> "$FAKE_DIR/add.log"; echo >> "$FAKE_DIR/add.log"
    cat "$FAKE_DIR/add.json" ;;
  *) exit 2 ;;
esac
"""
FAKE_CREW = r"""#!/bin/sh
f="$FAKE_DIR/crew/$1"
if [ -f "$f" ]; then cat "$f"; else echo "state: working · source: pane · busy"; fi
"""
FAKE_INBOX = r"""#!/bin/sh
# fake fm-inbox.sh: canned receipts; note and announce logged; a repeated --request-id replays, as the real one does
if [ "$1" = receipts ]; then
  printf '%s\n' "$*" >> "$FAKE_DIR/receipts.log"
  cat "$FAKE_DIR/receipts.json" 2>/dev/null || echo '{"pending":[],"handled":[]}'
  exit 0
fi
printf '%s' "$*" | tr '\n' '|' >> "$FAKE_DIR/inbox.log"; echo >> "$FAKE_DIR/inbox.log"
if [ "$1" = announce ]; then echo '{"outcome":"created","id":"'"$3"'","saved":true,"announced":true}'; exit 0; fi
out=created
if [ "$2" = "--request-id" ]; then
  mkdir -p "$FAKE_DIR/requests"
  if [ -e "$FAKE_DIR/requests/$3" ]; then out=replay; else : > "$FAKE_DIR/requests/$3"; fi
fi
rc=$(cat "$FAKE_DIR/note.rc" 2>/dev/null || echo 0)
ann=true; [ "$rc" = 3 ] && ann=false
echo '{"schema":"fm-inbox-note.v1","outcome":"'$out'","id":"1-x","saved":true,"announced":'$ann',"acknowledged":false}'
exit "$rc"
"""
FAKE_GH = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DIR/gh.log"
cat "$FAKE_DIR/gh.json"
"""
SHOW = """task:
  id: TASK-D
  title: "After A"
  state: queued
  priority: "-"
  deps: "blocked-by:TASK-A"
  body: "First line of the notes.\\nSecond line, with \\"quotes\\"."
"""
GH = {"state": "OPEN", "isDraft": False, "statusCheckRollup": [
    {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
    {"__typename": "CheckRun", "name": "e2e", "status": "IN_PROGRESS", "conclusion": None},
    {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
    {"__typename": "StatusContext", "context": "deploy", "state": "PENDING"}]}

FAKE_QUOTA = r"""#!/bin/sh
printf '%s\n' "$QUOTA_AXI_CLAUDE_PROFILES" > "$FAKE_DIR/quota.env"
cat "$FAKE_DIR/quota.json"
"""

LIST = """count: 5
tasks[5]{id,state,kind,repo,title,blocked,blocked_by,created,deps,held,hold_kind,hold_reason,hold_until,priority,links}:
  TASK-A,in_flight,ship,demo-app,"Fix the thing, carefully",no,none,2026-01-01,none,no,"-","-","-","-",none
  TASK-B,queued,ship,demo-app,"A very long title that goes on\\n... (truncated, 120 chars total - use show TASK-B --full to see complete text)",no,none,2026-01-02,none,no,"-","-","-","-",none
  TASK-C,queued,scout,demo-app,Wait for a call,no,none,2026-01-03,none,yes,captain,"Pick option a or b",2999-01-01,"-",none
  TASK-D,queued,ship,demo-app,After A,yes,TASK-A,2026-01-04,blocked-by:TASK-A,no,"-","-","-","-",none
  TASK-E,done,ship,demo-app,Shipped,no,none,"-",none,no,"-","-","-","-","pr:https://forge.example/o/r/pull/7"
"""

QUOTA = {"generatedAt": "2026-01-01T00:00:00Z", "providers": [{
    "provider": "claude", "plan": "max",
    "account": {"email": "someone@example.invalid"},
    "windows": [{"id": "seven_day", "label": "week", "percentRemaining": 12, "resetsAt": iso(T0 + 86400)},
                {"id": "five_hour", "label": "session", "percentRemaining": 80, "resetsAt": iso(T0 + 3600)}],
    "state": {"status": "fresh"},
    "accountLocator": {"kind": "config-dir", "path": "/accounts/.claude-two"},
    "quotaSemantics": {"effectiveAvailability": [{
        "scope": "all_models", "effectivePercentRemaining": 12, "limitingWindowIds": ["seven_day"],
        "runway": {"status": "projected_exhaustion", "projectedExhaustedAt": iso(T0 + 3 * 3600)}}]},
}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shipmate-test-")
        self.old_env = dict(os.environ)
        os.environ["HOME"] = os.path.join(self.tmp, "user")          # ~/.claude never points at a real one
        self.fake = os.path.join(self.tmp, "fake")
        os.environ["FAKE_DIR"] = self.fake
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "root")
        self.acct = os.path.join(self.tmp, "accounts", ".claude-two")
        write(os.path.join(self.root, "bin", "fm-tasks-axi.sh"), FAKE_TASKS, 0o755)
        write(os.path.join(self.root, "bin", "fm-crew-state.sh"), FAKE_CREW, 0o755)
        write(os.path.join(self.root, "bin", "fm-inbox.sh"), FAKE_INBOX, 0o755)
        self.quota_cmd = write(os.path.join(self.fake, "quota-axi"), FAKE_QUOTA, 0o755)
        self.gh_cmd = write(os.path.join(self.fake, "gh"), FAKE_GH, 0o755)
        write(os.path.join(self.fake, "show.txt"), SHOW)
        write(os.path.join(self.fake, "gh.json"), json.dumps(GH))
        write(os.path.join(self.fake, "list.toon"), LIST)
        write(os.path.join(self.fake, "quota.json"), json.dumps(QUOTA))
        write(os.path.join(self.fake, "add.json"), json.dumps({"ok": True, "task": {"id": "new-task-q1"}}))
        write(os.path.join(self.home, "config", "claude-accounts"), self.acct + "\n")
        write(os.path.join(self.home, "config", "project-capacity"), "# one build at a time\ndemo-app 1\n")
        write(os.path.join(self.home, "data", "projects.md"), "# Projects\n\n- demo-app [direct-PR] - a demo\n")
        self.cfg = shipmate.Config(self.home, self.root, port=0, quota_cmd=self.quota_cmd, gh_cmd=self.gh_cmd)
        self.folder = os.path.join(self.acct, "projects", shipmate.encode_dir(WT))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def meta(self, tid, extra=""):
        write(os.path.join(self.home, "state", tid + ".meta"),
              "worktree=%s\nproject=/clones/demo-app\nharness=claude\nkind=ship\nmodel=model-a\neffort=high\n"
              "account=%s\nspawn_gen=s%d.1.2\n%s" % (WT, self.acct, T0, extra))

    def status(self, tid, lines):
        write(os.path.join(self.home, "state", tid + ".status"), "".join(l + "\n" for l in lines))


class TestReaders(Base):
    def test_toon_rows_quotes_escapes_and_truncation(self):
        rows, err = shipmate.read_backlog(self.cfg)
        self.assertIsNone(err)
        by = {r["id"]: r for r in rows}
        self.assertEqual(by["TASK-A"]["title"], "Fix the thing, carefully")
        self.assertEqual(by["TASK-B"]["title"], "A very long title that goes on…")
        self.assertEqual(by["TASK-C"]["hold_reason"], "Pick option a or b")
        self.assertEqual(by["TASK-C"]["priority"], "")
        self.assertIn("pull/7", by["TASK-E"]["links"])

    def test_status_lines_and_spawn(self):
        self.status("TASK-A", ["working [at=5]: started", "paused [at=9 key=x]: waiting", "garbage"])
        st = shipmate.read_status(os.path.join(self.home, "state", "TASK-A.status"))
        self.assertEqual([(s["state"], s["at"]) for s in st], [("working", 5), ("paused", 9)])
        self.assertEqual(shipmate.spawn_epoch({"spawn_gen": "s123.4.5"}), 123)
        self.assertIsNone(shipmate.spawn_epoch({}))

    def test_queue_reasons(self):
        self.meta("TASK-A")
        rows, _ = shipmate.read_backlog(self.cfg)
        q = {r["id"]: [x[0] for x in r["reasons"]] for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual(q["TASK-B"], ["slot"])                 # demo-app admits 1 and TASK-A holds it
        self.assertEqual(q["TASK-C"], ["date"])                 # captain hold with a future date is scheduled, not a live decision
        self.assertEqual(q["TASK-D"], ["task"])
        self.assertNotIn("TASK-A", q)
        full = {r["id"]: r for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual(full["TASK-D"]["deps"], ["TASK-A"])                 # what it waits for, by id, for the ship view
        self.assertEqual(full["TASK-B"]["slot"], {"group": "demo-app", "cap": 1, "taken": 1, "holders": ["TASK-A"]})
        self.assertIsNone(full["TASK-C"]["slot"])
        # once TASK-A records its PR handoff the place frees
        self.meta("TASK-A", "pr=https://forge.example/o/r/pull/1\n")
        q = {r["id"]: [x[0] for x in r["reasons"]] for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual(q["TASK-B"], ["ready"])

    def test_home_lanes_hold_back_what_nothing_else_holds(self):
        line = "capacity: free_lanes=0 reason=full lanes=4 target=4 secondmates=1 load1=1.2 cores=8 mem_free_pct=40 disk_free_mb=9\nLanes: 4 of 4\n"
        self.assertEqual(shipmate.parse_capacity_line(line), {"free": 0, "reason": "full", "lanes": 4, "target": 4})
        self.assertEqual(shipmate.parse_capacity_line("capacity: free_lanes=2 reason=ok lanes=1 target=3")["free"], 2)
        for text in ("capacity: free_lanes=unknown reason=no-target lanes=5 target=none", "capacity: target=invalid lanes=1", "", None,
                     "Lanes: 1"):
            self.assertIsNone(shipmate.parse_capacity_line(text), text)
        write(os.path.join(self.root, "bin", "fm-capacity.sh"),
              '#!/bin/sh\necho run >> "$FAKE_DIR/capacity.log"\necho "capacity: free_lanes=0 reason=cpu lanes=2 target=3"\n', 0o755)
        log = os.path.join(self.fake, "capacity.log")
        cache = shipmate.LanesCache()
        self.assertIsNone(cache.get(self.cfg))                               # no config/lane-capacity: no target, no call
        self.assertFalse(os.path.exists(log))
        write(os.path.join(self.home, "config", "lane-capacity"), "3\n")
        lanes = cache.get(self.cfg)
        self.assertEqual(lanes, {"free": 0, "reason": "cpu", "lanes": 2, "target": 3})
        cache.get(self.cfg)
        with open(log) as fh:
            self.assertEqual(fh.read().count("run"), 1)                      # read at most every LANES_TTL seconds
        self.meta("TASK-A", "pr=https://forge.example/o/r/pull/1\n")          # demo-app's slot is free, so only the lanes hold TASK-B
        rows, _ = shipmate.read_backlog(self.cfg)
        q = {r["id"]: r for r in shipmate.queue_rows(self.cfg, rows, lanes)}
        self.assertEqual(q["TASK-B"]["reasons"][0][:2], ("lane", "home lanes 2/3"))
        self.assertIn("(cpu)", q["TASK-B"]["reasons"][0][2])
        self.assertEqual(q["TASK-B"]["slot"], {"group": "", "cap": 3, "taken": 2, "holders": []})
        self.assertEqual([x[0] for x in q["TASK-D"]["reasons"]], ["task"])  # a lane never hides a stronger reason
        q = {r["id"]: r for r in shipmate.queue_rows(self.cfg, rows, dict(lanes, free=1))}
        self.assertEqual(q["TASK-B"]["reasons"][0][0], "ready")

    def test_captain_hold_enters_the_captain_queue_only_when_its_date_arrives(self):
        today = shipmate.dt.date.today()
        def row(tid, until):
            return {"id": tid, "state": "queued", "held": "yes", "hold_kind": "captain", "hold_reason": "call it",
                    "hold_until": until, "repo": "other"}
        rows = [row("F", (today + shipmate.dt.timedelta(days=3)).isoformat()), row("T", today.isoformat()),
                row("P", (today - shipmate.dt.timedelta(days=3)).isoformat()), row("N", "")]
        q = {r["id"]: r for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual([x[0] for x in q["F"]["reasons"]], ["date"])
        self.assertTrue(q["F"]["reasons"][0][1].startswith("held until"))
        for tid in ("T", "P", "N"):
            self.assertEqual([x[0] for x in q[tid]["reasons"]], ["decision"])


    def test_how_long_a_queued_task_has_waited_for_the_captain(self):
        stamp = shipmate.parse_hold_set("Captain hold set: 2026-01-05T10:20:30Z\n\nnotes")
        self.assertEqual(stamp, (dt.datetime(2026, 1, 5, 10, 20, 30, tzinfo=dt.timezone.utc).timestamp(), True))
        self.assertEqual(shipmate.parse_hold_set("Captain hold set: 2026-01-05"),
                         (time.mktime(dt.date(2026, 1, 5).timetuple()), False))
        for body in ("", None, "notes first\nCaptain hold set: 2026-01-05T10:20:30Z", "Captain hold set: 2026-13-45T99:00:00Z"):
            self.assertEqual(shipmate.parse_hold_set(body), (None, False), body)
        rows, _ = shipmate.read_backlog(self.cfg)
        # TASK-C's date has arrived, so its hold is a live decision (a future date would be scheduled, not waiting)
        rows = [dict(r, hold_until=dt.date.today().isoformat()) if r["id"] == "TASK-C" else r for r in rows]
        # without a stamp, a held task waits since the day it was created; nothing else waits
        q = {r["id"]: (r["waiting_since"], r["waiting_exact"]) for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual(q["TASK-C"], (time.mktime(dt.date(2026, 1, 3).timetuple()), False))
        self.assertEqual({k: v[0] for k, v in q.items() if k != "TASK-C"}, {"TASK-B": None, "TASK-D": None})
        write(os.path.join(self.fake, "bodies.toon"),
              'count: 2\ntasks[2]{id,state,kind,repo,title,held,body}:\n'
              '  TASK-C,queued,scout,demo-app,Wait for a call,yes,"Captain hold set: 2026-01-05T10:20:30Z\\n\\nPick a or b"\n'
              '  TASK-B,queued,ship,demo-app,Free,no,"Captain hold set: 2026-01-04T00:00:00Z"\n')
        self.assertEqual(shipmate.hold_stamps(self.cfg), {"TASK-C": stamp})      # only a held task's stamp counts
        q = {r["id"]: (r["waiting_since"], r["waiting_exact"]) for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual(q["TASK-C"], stamp)


class TestTranscripts(Base):
    def test_tokens_only_after_spawn_once_per_message_with_subagents(self):
        old = Transcript()                       # an earlier task in the same pooled folder
        old.brief(T0 - 7200, "OTHER")
        for i in range(5):
            old.call(T0 - 7000 + i, cr=50000)
        old.save(os.path.join(self.folder, "old-session.jsonl"))
        cur = Transcript()
        cur.brief(T0 + 10, "TASK-A")
        cur.call(T0 + 20, [("Bash", {"command": "ls"}), ("Bash", {"command": "pwd"})])   # 2 lines, 1 message
        cur.call(T0 + 30)
        cur.save(os.path.join(self.folder, "cur-session.jsonl"))
        sub = Transcript()
        sub.call(T0 + 40, inp=1, cw=2, cr=3, out=4)
        sub.save(os.path.join(self.folder, "cur-session", "subagents", "agent-1.jsonl"))
        self.meta("TASK-A")
        index = shipmate.TranscriptIndex(self.cfg.claude_dirs)
        fds, cites, spawn, _mains = shipmate.task_sessions(index, shipmate.read_kv(os.path.join(self.home, "state", "TASK-A.meta")), "TASK-A")
        self.assertTrue(cites)
        self.assertEqual(spawn, T0)
        tok = shipmate.sum_tokens(fds, since=spawn - shipmate.SPAWN_SLACK)
        self.assertEqual(tok["calls"], 3)
        self.assertEqual(tok["new"], 2 * (10 + 100 + 20) + (1 + 2 + 4))
        self.assertEqual(tok["cache"], 2 * 1000 + 3)
        self.assertEqual(tok["model"], "claude-model-a")

    def test_ship_phases_from_milestones(self):
        self.meta("TASK-A")
        t = Transcript()
        t.brief(T0 + 5, "TASK-A")
        t.call(T0 + 10, [("Read", {"file_path": WT + "/README.md"})])
        path = t.save(os.path.join(self.folder, "s.jsonl"))
        meta = shipmate.read_kv(os.path.join(self.home, "state", "TASK-A.meta"))
        index = shipmate.TranscriptIndex(self.cfg.claude_dirs)

        def phase(status, crew="working"):
            fds, _, spawn, _m = shipmate.task_sessions(index, meta, "TASK-A")
            return shipmate.derive_phase("ship", meta, status, {"state": crew}, fds, self.home, "TASK-A", spawn)[:2]

        self.assertEqual(phase([]), (0, T0))
        t.call(T0 + 100, [("Write", {"file_path": "/tmp/scratch/notes.md"})])      # scratch edit: still exploring
        t.save(path)
        self.assertEqual(phase([])[0], 0)
        t.call(T0 + 200, [("Edit", {"file_path": WT + "/src/main.rs"})])
        t.save(path)
        self.assertEqual(phase([]), (1, T0 + 200))
        st = [{"state": "working", "at": T0 + 300, "text": "PR https://forge.example/o/r/pull/9 opened"}]
        self.assertEqual(phase(st), (2, T0 + 300))
        t.call(T0 + 400, [("Bash", {"command": "for i in $(seq 1 9); do out=$(gh pr checks 9 2>&1); sleep 30; done"})])
        t.save(path)
        self.assertEqual(phase(st, "paused"), (3, T0 + 400))
        t.call(T0 + 500, [("Bash", {"command": "grep -n 'gh pr checks' notes.txt"})])  # a mention, not a CI read
        t.call(T0 + 600, [("Edit", {"file_path": WT + "/src/main.rs"})])              # fixing after red CI
        t.save(path)
        self.assertEqual(phase(st, "working")[0], 2)
        st.append({"state": "done", "at": T0 + 900, "text": "PR https://forge.example/o/r/pull/9 green"})
        self.assertEqual(phase(st, "done"), (4, T0 + 900))

    def test_unknown_phase_without_transcript_or_pr(self):
        self.meta("TASK-A")
        meta = shipmate.read_kv(os.path.join(self.home, "state", "TASK-A.meta"))
        idx, since, why = shipmate.derive_phase("ship", meta, [], {"state": "working"}, [], self.home, "TASK-A", T0)
        self.assertIsNone(idx)
        self.assertIsNone(since)

    def test_scout_phase_report(self):
        meta = {"worktree": WT}
        self.assertEqual(shipmate.derive_phase("scout", meta, [], {"state": "working"}, [shipmate.FileData()],
                                               self.home, "TASK-S", T0)[0], 0)
        write(os.path.join(self.home, "data", "TASK-S", "report.md"), "# r\n")
        self.assertEqual(shipmate.derive_phase("scout", meta, [], {"state": "working"}, [], self.home, "TASK-S", T0)[0], 1)
        st = [{"state": "done", "at": T0 + 5, "text": "done"}]
        self.assertEqual(shipmate.derive_phase("scout", meta, st, {"state": "done"}, [], self.home, "TASK-S", T0)[:2], (2, T0 + 5))

    def test_daily_usage_and_delivered(self):
        now = time.time()
        w = Transcript()
        w.brief(now - 60, "TASK-A")
        w.call(now - 50, inp=1, cw=1, cr=10, out=1)
        w.save(os.path.join(self.folder, "abcd1234-0000.jsonl"))
        f = Transcript()
        f.call(now - 40, inp=5, cw=5, cr=50, out=5)
        f.save(os.path.join(self.acct, "projects", shipmate.encode_dir(self.home), "fm-session.jsonl"))
        index = shipmate.TranscriptIndex(self.cfg.claude_dirs)
        days = shipmate.daily_usage(self.cfg, index, now)
        self.assertEqual(len(days), shipmate.DAYS)
        today = days[0]
        self.assertEqual(today["by"]["workers"], [3, 10])
        self.assertEqual(today["by"]["firstmate"], [15, 50])
        self.assertEqual((today["new"], today["cache"]), (18, 60))
        write(os.path.join(self.home, "data", "fichas", "fichas.jsonl"), "\n".join([
            json.dumps({"id": "TASK-E", "sessao": "abcd1234", "inicio": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(now - 60)),
                        "tipo": "entrega", "modelo": "model-a", "minutos": {"parede": 12}, "tokens": {"total": 13}, "pr": 7}),
            json.dumps({"id": "TASK-OLD", "sessao": "ffff0000", "inicio": "2000-01-01T00:00Z", "tokens": {"total": 1}}),
            "not json"]) + "\n")
        rows = shipmate.delivered(self.cfg, index, {"TASK-E": {"links": "pr:https://forge.example/o/r/pull/7"}}, now)
        self.assertEqual([r["id"] for r in rows], ["TASK-E"])
        self.assertEqual((rows[0]["split"]["new"], rows[0]["split"]["cache"]), (3, 10))
        self.assertEqual(rows[0]["kind"], "ship")
        self.assertTrue(rows[0]["pr"].endswith("/pull/7"))


class TestQuota(Base):
    def test_lane_and_profiles_env(self):
        q = shipmate.QuotaCache().get(self.cfg)
        self.assertIsNone(q["error"])
        lane = q["lanes"][0]
        self.assertEqual(lane["label"], ".claude-two")
        self.assertEqual(lane["remaining"], 12)
        self.assertEqual(lane["runway"], "projected_exhaustion")
        self.assertEqual(lane["resets"], T0 + 86400)
        with open(os.path.join(self.fake, "quota.env")) as fh:
            self.assertEqual(fh.read().strip(), self.acct)
        html_out = shipmate.block_accounts(q, T0)
        self.assertIn("12% left", html_out)
        self.assertIn("runs out", html_out)
        self.assertNotIn("example.invalid", html_out)   # account identity never reaches the page

    def test_quota_failure_is_shown_not_raised(self):
        write(os.path.join(self.fake, "quota.json"), "not json")
        q = shipmate.QuotaCache().get(self.cfg)
        self.assertTrue(q["error"])
        self.assertIn("unavailable", shipmate.block_accounts(q, T0))


class TestBoardAndDetail(Base):
    """The board's lanes and cards, and the read-only detail drawer behind each card."""

    def agent(self, **kw):
        a = {"id": "TASK-A", "title": "Fix the thing, carefully", "kind": "ship", "project": "demo-app", "model": "model-a",
             "model_seen": "claude-model-a", "effort": "high", "harness": "claude", "account": ".claude-two",
             "phases": shipmate.SHIP_PHASES, "phase": 2, "phase_since": T0 + 300, "phase_why": "PR open",
             "milestones": [T0, T0 + 200, T0 + 300, None, None], "crew": "working", "spawn": T0,
             "pr": "https://github.com/o/r/pull/9", "tokens": {"new": 1300, "cache": 9000, "calls": 3, "model": None},
             "cites": True, "sessions": 1, "context": 150000, "context_limit": 200000, "context_how": "inferred",
             "last_activity": T0 + 900}
        a.update(kw)
        return a

    def test_board_lanes_cards_and_short_badges(self):
        rows, _ = shipmate.read_backlog(self.cfg)
        self.meta("TASK-A")
        queue = shipmate.queue_rows(self.cfg, rows)
        scout = self.agent(id="TASK-S", kind="scout", phases=shipmate.SCOUT_PHASES, phase=2, pr=None)
        board = shipmate.block_board([self.agent(), scout], queue, None, T0 + 1000)
        # queue, exploring (empty), implementing (empty), PR open (1), CI (empty), ready/delivered (the scout)
        self.assertIn("grid-template-columns:minmax(165px, 1fr) 44px 44px minmax(165px, 1fr) 44px minmax(165px, 1fr)", board)
        self.assertEqual(board.count('class="sm-lane sm-lane--empty"'), 3)
        self.assertIn('data-action="add"', board)
        self.assertIn('title="build slots for demo-app: 1 of 1 taken (TASK-A)">build slot 1/1<', board)
        self.assertIn('data-id="TASK-S"', board)
        unknown = shipmate.block_board([self.agent(phase=None)], [], None, T0)
        self.assertIn("phase unknown", unknown)

    def test_accounts_tightest_first(self):
        q = {"error": None, "lanes": [
            {"label": "roomy", "plan": None, "remaining": 80, "limit_label": "week", "resets": None, "runway": None, "exhausts": None, "windows": []},
            {"label": "tight", "plan": None, "remaining": 5, "limit_label": "week", "resets": None, "runway": None, "exhausts": None, "windows": []}]}
        out = shipmate.block_accounts(q, T0)
        self.assertLess(out.index("tight"), out.index("roomy"))

    def test_intent_and_reports(self):
        write(os.path.join(self.home, "data", "TASK-A", "brief.md"),
              "# Task\n## Captain's intent\nMake the thing <safe>.\n\nSecond paragraph.\n\n## Firstmate spec\n- not this\n")
        write(os.path.join(self.home, "data", "TASK-A", "report.md"), "# report\n")
        self.assertEqual(shipmate.captain_intent(self.home, "TASK-A"), "Make the thing <safe>.\n\nSecond paragraph.")
        self.assertIsNone(shipmate.captain_intent(self.home, "NOPE"))
        self.assertEqual(shipmate.report_files(self.home, "TASK-A"), ["md"])

    def test_agent_detail_html(self):
        status = [{"state": "working", "at": T0 + 10, "text": "started <b>"}, {"state": "paused", "at": T0 + 400, "text": "waiting"}]
        checks = shipmate.summarize_checks(GH)
        out = shipmate.render_agent_detail(self.agent(), "Make it <safe>.", status, dict(checks, age=5), ["md"], T0 + 1000)
        for piece in ("Fix the thing, carefully", "demo-app", "model-a (model-a)", "high", ".claude-two", "claude",
                      "1k new · 9k cache reads", "150k of 200k (inferred window), 75%", "last action 1 min ago",
                      "PR #9", "open · 1 passed", "1 failed", "2 running", "1 skipped", "failing: lint",
                      "/report?id=TASK-A&amp;f=md", "Make it &lt;safe&gt;.", "started &lt;b&gt;", "not reached"):
            self.assertIn(piece, out)
        self.assertLess(out.index("waiting"), out.index("started &lt;b&gt;"))      # newest status first

    def test_checks_are_cached_and_github_only(self):
        cache = shipmate.ChecksCache()
        a = cache.get(self.cfg, "https://github.com/o/r/pull/9")
        b = cache.get(self.cfg, "https://github.com/o/r/pull/9")
        self.assertEqual(a["counts"], {"pass": 1, "fail": 1, "pending": 2, "skipped": 1})
        self.assertEqual(b["counts"], a["counts"])
        with open(os.path.join(self.fake, "gh.log")) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)                      # second read came from the cache
        self.assertIn("error", cache.get(self.cfg, "https://forge.example/o/r/-/merge_requests/3"))

    def test_the_rail_reads_checks_in_the_background_and_never_waits(self):
        cache = shipmate.ChecksCache()
        url = "https://github.com/o/r/pull/9"
        self.assertIsNone(cache.watch(self.cfg, url))                           # no read yet: nothing, at once
        for _ in range(200):
            if url not in cache.busy:
                break
            time.sleep(0.02)
        got = cache.watch(self.cfg, url)
        self.assertEqual(got["counts"], {"pass": 1, "fail": 1, "pending": 2, "skipped": 1})
        self.assertEqual(shipmate.checks_verdict(got), "fail")
        cache.get(self.cfg, url)
        with open(os.path.join(self.fake, "gh.log")) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)                    # one read serves the rail and the drawer
        self.assertIsNone(cache.watch(self.cfg, "https://forge.example/o/r/-/merge_requests/3"))
        verdict = shipmate.checks_verdict
        self.assertEqual(verdict({"counts": {"pass": 3, "fail": 0, "pending": 0, "skipped": 1}}), "pass")
        self.assertEqual(verdict({"counts": {"pass": 3, "fail": 0, "pending": 1, "skipped": 0}}), "pending")
        self.assertIsNone(verdict({"counts": {"pass": 0, "fail": 0, "pending": 0, "skipped": 2}}))   # no check ran
        self.assertIsNone(verdict({"error": "gh failed"}))
        self.assertIsNone(verdict(None))

    def test_checks_say_when_they_started_and_when_the_last_one_finished(self):
        doc = {"statusCheckRollup": [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS",
             "startedAt": iso(T0 + 100), "completedAt": iso(T0 + 400)},
            {"__typename": "CheckRun", "name": "e2e", "status": "COMPLETED", "conclusion": "SUCCESS",
             "startedAt": iso(T0 + 120), "completedAt": iso(T0 + 900)},
            {"__typename": "StatusContext", "context": "deploy", "state": "SUCCESS", "startedAt": iso(T0 + 600)},
            {"__typename": "CheckRun", "name": "docs", "status": "QUEUED", "conclusion": None, "startedAt": "0001-01-01T00:00:00Z"}]}
        got = shipmate.summarize_checks(doc)
        self.assertEqual((got["started"], got["ended"]), (T0 + 100, T0 + 900))          # one not started yet says year 1
        self.assertEqual(shipmate.summarize_checks(GH)["ended"], None)                 # gh without the times: unknown

    def test_the_phase_follows_the_prs_checks(self):
        def phase(verdict_counts, crew="working", ph=4, ended=T0 + 500, started=T0 + 350):
            a = self.agent(phase=ph, crew=crew, milestones=[T0, T0 + 200, T0 + 300, T0 + 350, T0 + 320 if crew == "done" else None])
            summary = {"counts": dict({"pass": 0, "fail": 0, "pending": 0, "skipped": 0}, **verdict_counts),
                       "started": started, "ended": ended, "error": None}
            shipmate.phase_by_checks(a, summary)
            return a["phase"], a["phase_since"], a["checks"]
        # the worker said done as it opened the PR: CI still runs, so it waits for CI, not the merge
        self.assertEqual(phase({"pass": 1, "pending": 1}, crew="done"), (3, T0 + 350, "pending"))
        self.assertEqual(phase({"pass": 1, "fail": 1}, crew="done"), (1, T0 + 500, "fail"))      # red: back to work
        self.assertEqual(phase({"pass": 2}, crew="done"), (4, T0 + 500, "pass"))              # the merge, since the last check
        self.assertEqual(phase({"pass": 2}, crew="done", ended=T0 + 100), (4, T0 + 320, "pass"))   # or since done, if later
        self.assertEqual(phase({"pass": 2}, ph=3), (2, T0 + 300, "pass"))                    # green, still at work: PR open
        self.assertEqual(phase({"pending": 1}, ph=2, started=None), (3, T0 + 350, "pending"))
        # no check, no answer: the milestones stand
        a = self.agent(phase=4, crew="done")
        self.assertEqual(shipmate.phase_by_checks(a, {"error": "gh failed"})["phase"], 4)
        self.assertEqual(shipmate.phase_by_checks(self.agent(phase=1), {"counts": {"fail": 1}})["phase"], 1)   # no PR phase yet
        scout = self.agent(kind="scout", phases=shipmate.SCOUT_PHASES, phase=2, pr=None)
        self.assertEqual(shipmate.phase_by_checks(scout, None)["phase"], 2)
        # the board follows: a red PR sits in implementing
        red = shipmate.phase_by_checks(self.agent(phase=4, crew="done"), {"counts": {"fail": 1}, "error": None})
        board = shipmate.block_board([red], [], None, T0 + 1000)
        self.assertIn("grid-template-columns:minmax(165px, 1fr) 44px minmax(165px, 1fr) 44px 44px 44px", board)

    def test_the_fleet_reads_the_checks_of_every_pr_open(self):
        url = "https://github.com/o/r/pull/9"
        self.meta("TASK-A", "pr=%s\n" % url)
        write(os.path.join(self.fake, "crew", "TASK-A"), "state: done · source: status · done\n")
        panel = shipmate.Panel(self.cfg)

        def read(checks):
            write(os.path.join(self.fake, "gh.json"), json.dumps({"state": "OPEN", "isDraft": False, "statusCheckRollup": checks}))
            panel.checks = shipmate.ChecksCache()
            panel.checks.get(self.cfg, url)                                     # read now, as the background read would
            a = next(a for a in panel.read_fleet()["agents"] if a["id"] == "TASK-A")
            return a["phase"], a["checks"]
        run = {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "conclusion": None}
        self.assertEqual(read([run]), (3, "pending"))
        self.assertEqual(read([dict(run, status="COMPLETED", conclusion="FAILURE")]), (1, "fail"))
        self.assertEqual(read([dict(run, status="COMPLETED", conclusion="SUCCESS", completedAt=iso(T0 + 50))]), (4, "pass"))
        self.assertEqual(read([]), (4, None))                                       # no checks: done with a PR is the merge

    def test_queue_detail_reads_the_full_item(self):
        rows, _ = shipmate.read_backlog(self.cfg)
        item = next(q for q in shipmate.queue_rows(self.cfg, rows) if q["id"] == "TASK-D")
        rc, out, _ = shipmate.run([self.cfg.bin("fm-tasks-axi.sh"), "show", "TASK-D", "--full"], env=self.cfg.env())
        full = shipmate.parse_show(out)
        self.assertEqual(full["body"], 'First line of the notes.\nSecond line, with "quotes".')
        html_out = shipmate.render_queue_detail(item, full, None, [], T0)
        for piece in ("After A", "waits for TASK-A", "First line of the notes.", "blocked-by:TASK-A"):
            self.assertIn(piece, html_out)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.meta("TASK-A")
        self.panel = shipmate.Panel(self.cfg)
        self.srv = shipmate.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
        self.cfg.port = self.srv.server_address[1]
        self.srv.RequestHandlerClass = shipmate.make_handler(self.panel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=30)
        h = {"Host": "127.0.0.1:%d" % self.cfg.port}
        h.update(headers or {})
        if body is not None:
            body = urllib.parse.urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r, data

    def form(self, **kw):
        f = {"token": self.panel.token, "request": "req1", "title": "Add a  thing", "project": "demo-app", "text": "line 1\r\nline 2"}
        f.update(kw)
        return f

    def log(self, name):
        try:
            with open(os.path.join(self.fake, name)) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def test_page_renders_the_board_and_sidebar_without_external_resources(self):
        r, page = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        for h in ("Firstmate session", "Accounts and quota", "Work by phase", "Queue", "Usage", "+ Add task", "build slot 1/1",
                  'data-id="TASK-B"', 'id="sm-drawer"'):
            self.assertIn(h, page)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))
        self.assertNotIn("@import", page)
        self.assertNotIn("<script src", page)
        r, blocks = self.req("GET", "/blocks")
        self.assertEqual(set(json.loads(blocks)), {"board", "today", "firstmate", "accounts", "strip", "sumline", "usage", "counts", "time"})

    def fake_claude(self, body="exit 0"):
        self.cfg.claude_cmd = write(os.path.join(self.fake, "claude"),
                                    '#!/bin/sh\nprintf "%%s|%%s|%%s\\n" "$*" "${CLAUDE_CONFIG_DIR-unset}" "$PWD" >> "$FAKE_DIR/claude.log"\n%s\n'
                                    % body, 0o755)

    def test_doctor_runs_only_for_an_account_the_home_lists(self):
        self.fake_claude()
        os.makedirs(self.acct, exist_ok=True)
        origin = {"Origin": "http://127.0.0.1:%d" % self.cfg.port, "Sec-Fetch-Site": "same-origin"}
        for account in ("nobody", self.acct, "/etc", "../.claude-two", ".claude-two; touch pwned", ""):
            r, body = self.req("POST", "/doctor", {"token": self.panel.token, "account": account}, origin)
            self.assertEqual(r.status, 200)
            self.assertFalse(json.loads(body)["ok"], account)
            self.assertIn("no account of this home is called", json.loads(body)["error"])
        self.assertEqual(self.log("claude.log"), [])                        # an unknown account runs nothing
        r, _ = self.req("POST", "/doctor", {"token": self.panel.token, "account": ".claude-two"}, {"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/doctor", {"token": "wrong", "account": ".claude-two"}, origin)
        self.assertEqual(r.status, 403)
        r, _ = self.req("GET", "/doctor?account=.claude-two")
        self.assertEqual(r.status, 404)
        self.assertEqual(self.log("claude.log"), [])
        self.panel.quota.get(self.cfg)
        r, body = self.req("POST", "/doctor", {"token": self.panel.token, "account": ".claude-two"}, origin)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(self.log("claude.log"), ["doctor|%s|%s" % (self.acct, os.path.realpath(self.acct))])
        self.assertIsNone(self.panel.quota.value)                           # the quota is read again
        self.assertTrue(self.panel.snapshot()["doctor"]["results"][".claude-two"]["ok"])
        self.fake_claude("echo 'token sk-ant-secret' >&2; exit 3")
        r, body = self.req("POST", "/doctor", {"token": self.panel.token, "account": ".claude-two"}, origin)
        self.assertEqual(json.loads(body), {"ok": False, "error": "claude doctor exited with status 3"})
        self.assertNotIn("sk-ant", body)                                      # its output is never passed on
        self.assertNotIn("sk-ant", json.dumps(self.panel.snapshot()["doctor"]))

    def test_doctor_is_an_argument_list_without_a_shell(self):
        write(os.path.join(self.home, "config", "claude-accounts"), "ordinary\n%s\n/x/a/.dup\n/x/b/.dup\n" % self.acct)
        cfg = shipmate.Config(self.home, self.root, port=0, claude_cmd="/opt/claude")
        self.assertEqual(set(shipmate.doctor_accounts(cfg)), {"ordinary", ".claude-two"})   # a shared label names neither
        os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        argv, env = shipmate.doctor_command(cfg, "ordinary", os.path.expanduser("~/.claude"))
        self.assertEqual(argv, ["/opt/claude", "doctor"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)                         # the default account takes no config dir
        argv, env = shipmate.doctor_command(cfg, self.acct, self.acct)
        self.assertEqual((argv, env["CLAUDE_CONFIG_DIR"]), (["/opt/claude", "doctor"], self.acct))
        seen = []

        class Proc:
            pid = 0

            def wait(self, timeout=None):
                return 0

        def popen(args, **kw):
            seen.append((args, kw))
            return Proc()
        real = shipmate.subprocess.Popen
        shipmate.subprocess.Popen = popen
        try:
            self.assertEqual(shipmate.run_doctor(cfg, ".claude-two"), {"ok": True})
        finally:
            shipmate.subprocess.Popen = real
        (args, kw), = seen
        self.assertEqual(args, ["/opt/claude", "doctor"])
        self.assertFalse(kw.get("shell", False))
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)   # no terminal, nothing kept
        self.assertEqual(kw["env"]["CLAUDE_CONFIG_DIR"], self.acct)
        self.assertEqual(shipmate.run_doctor(shipmate.Config(self.home, self.root, claude_cmd="/no/such/claude"), ".claude-two")["ok"],
                         False)                                              # a missing command is an error, not a crash

    def test_doctor_that_hangs_is_stopped(self):
        self.fake_claude("sleep 30 & wait")
        t = time.monotonic()
        res = shipmate.run_doctor(self.cfg, ".claude-two", timeout=1)
        self.assertLess(time.monotonic() - t, 10)
        self.assertEqual(res, {"ok": False, "error": "claude doctor did not finish in 1 s and was stopped"})
        self.assertFalse(self.panel.doctoring.locked())

    def test_doctor_button_shows_beside_a_stale_or_unknown_listed_account(self):
        now = T0
        doctor = {"accounts": [".claude-two"], "results": {}}

        def lane(label, **kw):
            return dict({"label": label, "plan": None, "status": "fresh", "remaining": 50, "limit_label": "week", "resets": None,
                         "runway": None, "exhausts": None, "windows": []}, **kw)
        html_ = shipmate.block_accounts({"lanes": [lane(".claude-two")]}, now, doctor)
        self.assertNotIn("data-doctor", html_)
        for stale in ({"status": "stale"}, {"remaining": None}, {"reason": "credentials_expired"}, {"auth": "expired_refreshable"}):
            self.assertIn('data-doctor=".claude-two"', shipmate.block_accounts({"lanes": [lane(".claude-two", **stale)]}, now, doctor))
        self.assertNotIn("data-doctor", shipmate.block_accounts({"lanes": [lane("other", status="stale")]}, now, doctor))
        done = dict(doctor, results={".claude-two": {"ok": False, "error": "claude doctor exited with status 3", "at": now - 60}})
        out = shipmate.block_accounts({"lanes": [lane(".claude-two", status="stale")]}, now, done)
        self.assertIn("exited with status 3", out)
        self.assertIn("data-doctor", out)                                   # it can be tried again
        self.assertIn('data-doctor="ordinary"', shipmate.render_blocks(shipmate.demo_snapshot(T0))["accounts"])

    def test_detail_and_report_endpoints(self):
        write(os.path.join(self.home, "data", "TASK-A", "report.md"), "# the report\n")
        write(os.path.join(self.home, "data", "TASK-A", "brief.md"), "## Captain's intent\nDo it.\n")
        r, body = self.req("GET", "/detail?id=TASK-A")
        self.assertEqual(r.status, 200)
        self.assertIn("Do it.", body)
        self.assertIn("/report?id=TASK-A&amp;f=md", body)
        r, body = self.req("GET", "/detail?id=TASK-D")                      # a queue card
        self.assertEqual(r.status, 200)
        self.assertIn("First line of the notes.", body)
        for bad in ("/detail?id=NOPE", "/detail?id=../state", "/detail?id="):
            self.assertEqual(self.req("GET", bad)[0].status, 404)
        r, body = self.req("GET", "/report?id=TASK-A&f=md")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, "# the report\n")
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))
        self.assertTrue(r.getheader("Content-Type").startswith("text/plain"))
        for bad in ("/report?id=TASK-A&f=html", "/report?id=../home&f=md", "/report?id=TASK-A&f=../brief"):
            self.assertEqual(self.req("GET", bad)[0].status, 404)

    def test_foreign_host_and_origin_are_refused(self):
        r, _ = self.req("GET", "/", headers={"Host": "evil.example:%d" % self.cfg.port})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/task", self.form(), {"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/task", self.form(), {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/task", self.form(token="wrong"))
        self.assertEqual(r.status, 403)
        self.assertEqual(self.log("add.log"), [])

    def test_add_queues_notifies_once(self):
        origin = {"Origin": "http://127.0.0.1:%d" % self.cfg.port}
        r, _ = self.req("POST", "/task", self.form(), origin)
        self.assertEqual(r.status, 303)
        self.assertIn("added=new-task-q1", r.getheader("Location"))
        self.assertIn("notified=yes", r.getheader("Location"))
        adds = self.log("add.log")
        self.assertEqual(len(adds), 1)
        self.assertIn("add Add a thing --mint --repo demo-app --queue --json --body line 1|line 2", adds[0])
        notes = self.log("inbox.log")
        self.assertEqual(len(notes), 1)
        self.assertIn("note --request-id shipmate-req1 --json -- New task queued from shipmate: new-task-q1", notes[0])
        r, _ = self.req("POST", "/task", self.form(), origin)        # the same form sent again
        self.assertEqual(r.status, 303)
        self.assertEqual(len(self.log("add.log")), 1)
        r, page = self.req("GET", r.getheader("Location"))
        self.assertIn("new-task-q1 is queued", page)

    def test_add_refuses_unknown_project_and_existing_id(self):
        r, _ = self.req("POST", "/task", self.form(project="elsewhere", request="r2"))
        self.assertIn("error=", r.getheader("Location"))
        self.assertEqual(self.log("add.log"), [])
        write(os.path.join(self.fake, "add.json"), json.dumps({"ok": True, "already": True, "task": {"id": "x"}}))
        r, _ = self.req("POST", "/task", self.form(request="r3"))
        self.assertIn("already+exists", r.getheader("Location"))
        self.assertEqual(self.log("inbox.log"), [])

    def waiting(self):
        self.status("TASK-A", ["working [at=%d]: go" % T0, "needs-decision [key=pick] [at=%d]: which way? A) left B) right" % (T0 + 5)])

    def answer(self, **kw):
        f = {"token": self.panel.token, "request": "f0rm1", "task": "TASK-A", "ref": "pick", "option": "B", "text": "carefully\r\nplease"}
        f.update(kw)
        r, body = self.req("POST", "/answer", f, {"Origin": "http://127.0.0.1:%d" % self.cfg.port, "Sec-Fetch-Site": "same-origin"})
        return r, json.loads(body) if r.status == 200 else body

    def test_an_answer_becomes_one_note_however_often_it_is_sent(self):
        self.waiting()
        r, res = self.answer()
        self.assertEqual(r.status, 200)
        self.assertEqual((res["ok"], res["state"], res["note"]), (True, "sent", "1-x"))
        notes = self.log("inbox.log")
        self.assertEqual(len(notes), 1)
        self.assertIn("note --request-id shipmate-answer:f0rm1:TASK-A --json -- Captain's answer from shipmate on TASK-A", notes[0])
        self.assertIn("Decision: its needs-decision line [key=pick]", notes[0])
        self.assertIn("Answer:|B) right|carefully|please", notes[0])
        self.answer()                                                    # a second click sends the same request id
        notes = self.log("inbox.log")
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0].split(" --json")[0], notes[1].split(" --json")[0])
        self.assertEqual(len(os.listdir(os.path.join(self.fake, "requests"))), 1)   # so the inbox keeps one note
        self.answer(request="f0rm2", option="", text="another thought")
        self.assertEqual(len(os.listdir(os.path.join(self.fake, "requests"))), 2)   # a new form is a new answer

    def test_an_answer_saved_without_its_wake_says_so(self):
        self.waiting()
        write(os.path.join(self.fake, "note.rc"), "3")
        r, res = self.answer()
        self.assertEqual((res["ok"], res["state"]), (True, "unwoken"))
        write(os.path.join(self.fake, "note.rc"), "1")
        r, res = self.answer(request="f0rm2")
        self.assertFalse(res["ok"])
        self.assertIn("the inbox refused it", res["error"])

    def test_answers_are_refused_when_they_answer_nothing_or_come_from_elsewhere(self):
        self.waiting()
        for kw, err in (({"ref": "gone"}, "no longer open"), ({"task": "TASK-B"}, "no longer open"), ({"task": "NOPE"}, "no longer on the board"),
                        ({"option": "Z"}, "not on this decision"), ({"option": "", "text": " "}, "pick an option"),
                        ({"request": "../x"}, "no request id"), ({"request": "x" * 11}, "no request id")):
            r, res = self.answer(**kw)
            self.assertFalse(res["ok"], kw)
            self.assertIn(err, res["error"], kw)
        r, _ = self.answer(token="wrong")
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/answer", {"token": self.panel.token}, {"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/answer", {"token": self.panel.token}, {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.log("inbox.log"), [])

    def test_receipts_come_back_to_the_drawer_the_cards_and_the_wake(self):
        self.waiting()
        at = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        write(os.path.join(self.fake, "receipts.json"), json.dumps({"pending": [
            {"id": "n9", "at": at(T0 + 60), "request_id": "shipmate-answer:f9:TASK-A", "body": "x\nAnswer:\nB) right",
             "announced": False, "acknowledged": False, "reply": None}], "handled": [
            {"id": "n8", "at": at(T0 + 30), "request_id": "shipmate-answer:f8:TASK-A", "body": "x\nAnswer:\nwait",
             "announced": True, "acknowledged": True, "reply": {"at": at(T0 + 40), "body": "waited"}}]}))
        r, body = self.req("GET", "/detail?id=TASK-A")
        for piece in ("Your answer", 'data-ref="pick"', "B) right", "waited", 'data-announce="n9"', "sm-ans--replied", "sm-ans--unwoken"):
            self.assertIn(piece, body)
        self.assertLess(body.index("Your answer"), body.index("Phases"))
        self.assertIn("--all-pending --all-handled", self.log("receipts.log")[0])
        r, blocks = self.req("GET", "/blocks")
        self.assertIn("sm-ans--unwoken", json.loads(blocks)["board"])          # the latest answer to the open decision
        r, ship = self.req("GET", "/ship")
        self.assertIn("sm-ans--unwoken", json.loads(ship)["log"])
        post = lambda note: json.loads(self.req("POST", "/announce", {"token": self.panel.token, "note": note})[1])
        self.assertTrue(post("n9")["ok"])
        self.assertEqual(self.log("inbox.log"), ["announce --json n9"])
        for bad in ("someone-elses-note", "../n9"):
            self.assertFalse(post(bad)["ok"])
        self.assertEqual(len(self.log("inbox.log")), 1)


class TestAnswers(unittest.TestCase):
    """The captain's answer: ready options, what waits, the note firstmate gets, and its receipts."""

    def agent(self, tid="TASK-A", crew="working", decisions=(), last=T0 - 60):
        return {"id": tid, "title": "Fix  the thing", "crew": crew, "last_activity": last,
                "decisions": [{"state": st, "key": k, "at": at, "text": t} for st, k, at, t in decisions]}

    def snap(self, agents=(), queue=(), answers=None):
        return {"now": T0, "agents": list(agents), "queue": list(queue), "answers": answers or {}}

    def test_ready_options_from_the_decision_line(self):
        opts = lambda t: [(o["label"], o["text"]) for o in shipmate.decision_options(t)]
        self.assertEqual(opts("per user or per key? A) per user B) per key"), [("A", "per user"), ("B", "per key")])
        self.assertEqual(opts("Pick one: (a) keep the cache; (b) drop it, or (c) ask first. I lean to b."),
                         [("A", "keep the cache"), ("B", "drop it"), ("C", "ask first")])
        self.assertEqual(opts("1) ship now 2) wait for the review"), [("1", "ship now"), ("2", "wait for the review")])
        self.assertEqual(opts("A) the card (fast; cramped); B) the drawer (room). Recommend B."),
                         [("A", "the card (fast; cramped)"), ("B", "the drawer (room)")])
        for none in ("per user or per key?", "only a) one", "see f(x) and g(y)", "B) starts late C) and goes on", ""):
            self.assertEqual(opts(none), [], none)

    def test_a_reason_is_cut_into_lead_items_and_tail(self):
        r = shipmate.split_reason("Inventory (data/X-1/report.html): (1) what to do; (2) where to start, two fronts (recommended), or one; (3) dead states. See the report.")
        self.assertEqual(r["lead"], "Inventory (data/X-1/report.html):")
        self.assertEqual([(i["label"], i["text"]) for i in r["items"]],
                         [("1", "what to do"), ("2", "where to start, two fronts (recommended), or one"), ("3", "dead states")])
        self.assertEqual(r["tail"], "See the report.")
        plain = shipmate.split_reason("per user or per key?")
        self.assertEqual(plain, {"lead": "per user or per key?", "items": [], "tail": ""})
        self.assertEqual(shipmate.split_reason("see f(x) and g(y)")["items"], [])
        self.assertEqual(shipmate.split_reason("")["lead"], "")
        # the options and the list read the same items
        t = "Pick: (a) keep it; (b) drop it"
        self.assertEqual([i["text"] for i in shipmate.split_reason(t)["items"]], [o["text"] for o in shipmate.decision_options(t)])

    def test_paths_and_ids_are_set_in_breaking_mono_and_the_rest_is_escaped(self):
        out = shipmate.rich("see data/ME-INVENTARIO-105/report.html and ME-INVENTARIO-DECISOES, then <b>report.md</b> & more.")
        self.assertIn('<code class="sm-code">data/ME-INVENTARIO-105/report.html</code>', out)
        self.assertIn('<code class="sm-code">ME-INVENTARIO-DECISOES</code>', out)
        self.assertIn('<code class="sm-code">report.md</code>', out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("&amp; more.", out)
        self.assertNotIn("<b>", out)
        self.assertEqual(shipmate.rich("plain words, 5 calls (and 1/2 of it)"), "plain words, 5 calls (and 1/2 of it)")

    def test_a_long_decision_drawer_lists_the_items_and_keeps_all_the_words(self):
        text = ("Inventory (data/ME-INVENTARIO-105/report.html): (1) what the rule does with the 50 that are not lifecycle, " + "x" * 300 +
                "; (2) where to start; (3) dead states.")
        ask = [{"ref": "hold", "kind": "hold", "state": "captain hold", "key": None, "at": None, "text": text, "options": shipmate.decision_options(text)}]
        out = shipmate.answer_section("TASK-A", ask, [], "tok", T0)
        self.assertEqual(out.count('<ol class="sm-items">'), 1)
        self.assertIn("<li><b>3)</b> <span>dead states</span></li>", out)
        self.assertIn("x" * 300, out)
        self.assertEqual(out.count('class="sm-opt"'), 3)
        self.assertNotIn("white-space: nowrap", open(os.path.join(os.path.dirname(os.path.abspath(shipmate.__file__)), "shipmate.css")).read().split(".sm-opt span")[1].split("}")[0])

    def test_a_queued_hold_keeps_its_whole_text_in_the_snapshot(self):
        self.assertGreater(shipmate.DECISION_TEXT, 650)

    def test_what_waits_on_the_captain(self):
        a = self.agent(decisions=[("needs-decision", "pick", T0 - 50, "A) x B) y"), ("blocked", None, T0 - 20, "stuck")])
        asks = shipmate.captain_asks(self.snap([a]), "TASK-A")
        self.assertEqual([(x["ref"], x["kind"], len(x["options"])) for x in asks], [("pick", "line", 2), ("at%d" % (T0 - 20), "line", 0)])
        parked = self.agent(crew="parked")
        self.assertEqual([(x["ref"], x["kind"]) for x in shipmate.captain_asks(self.snap([parked]), "TASK-A")], [("crew", "crew")])
        self.assertEqual(shipmate.captain_asks(self.snap([self.agent()]), "TASK-A"), [])
        held = {"id": "Q1", "title": "Q", "reasons": [("decision", "captain's decision", "x")], "hold_reason": "1) now 2) later",
                "waiting_since": T0 - 900}
        free = {"id": "Q2", "title": "Q", "reasons": [("ready", "ready", "x")], "hold_reason": ""}
        s = self.snap(queue=[held, free])
        self.assertEqual([(x["ref"], x["at"], len(x["options"])) for x in shipmate.captain_asks(s, "Q1")], [("hold", T0 - 900, 2)])
        self.assertEqual(shipmate.captain_asks(s, "Q2"), [])
        self.assertIsNone(shipmate.captain_asks(s, "NOPE"))

    def test_the_note_names_the_task_and_the_decision(self):
        a = self.agent(decisions=[("needs-decision", "pick", T0, "which way? A) left B) right")])
        s = self.snap([a])
        body = shipmate.prepare_answer(s, "TASK-A", "pick", "B", "and mind the step\nplease")["body"]
        lines = body.split("\n")
        self.assertEqual(lines[0], "Captain's answer from shipmate on TASK-A (Fix the thing).")
        self.assertEqual(lines[1], "Decision: its needs-decision line [key=pick] of %s: which way? A) left B) right"
                         % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(T0)))
        self.assertIn("bin/fm-inbox.sh reply", lines[2])
        self.assertEqual(shipmate.note_words(body), "B) right\nand mind the step\nplease")
        self.assertEqual(shipmate.note_words(shipmate.prepare_answer(s, "TASK-A", "pick", "", "just words")["body"]), "just words")
        for ref, opt, words, err in (("gone", "", "x", "no longer open"), ("pick", "C", "", "not on this decision"),
                                     ("pick", "", "", "pick an option"), ("pick", "", "x" * 4001, "longer than")):
            self.assertIn(err, shipmate.prepare_answer(s, "TASK-A", ref, opt, words)["error"])
        self.assertIn("no longer on the board", shipmate.prepare_answer(s, "NOPE", "pick", "", "x")["error"])
        crew = shipmate.prepare_answer(self.snap([self.agent(crew="failed")]), "TASK-A", "crew", "", "retry it")["body"]
        self.assertIn("Decision: its crew state, failed, with no needs-decision or blocked line open", crew)
        held = {"id": "Q1", "title": "Bill", "reasons": [("decision", "d", "x")], "hold_reason": "which plan", "waiting_since": T0}
        self.assertIn("Decision: its captain hold: which plan", shipmate.prepare_answer(self.snap(queue=[held]), "Q1", "hold", "", "B")["body"])
        self.assertEqual(shipmate.answer_request_id("f0rm-1d", "TASK-A"), "shipmate-answer:f0rm-1d:TASK-A")

    def test_receipts_become_answers_with_their_state(self):
        note = lambda nid, rid, at, **kw: dict({"id": nid, "at": iso(at)[:19] + "Z", "request_id": rid,
                                                 "body": "head\nAnswer:\nsaid " + nid, "announced": True, "reply": None}, **kw)
        doc = {"pending": [note("n3", "shipmate-answer:f3:TASK-A", T0 + 30),
                           note("n4", "shipmate-answer:f4:TASK-A", T0 + 40, announced=False),
                           note("n5", "shipmate-x", T0 + 50), note("n6", "shipmate-answer:f6:../x", T0 + 60),
                           note("n7", None, T0 + 70)],
               "handled": [note("n2", "shipmate-answer:f2:TASK-A", T0 + 20, acknowledged=True),
                           note("n1", "shipmate-answer:f1:Q1", T0 + 10, acknowledged=True,
                                reply={"at": iso(T0 + 99)[:19] + "Z", "body": "done it"})]}
        got = shipmate.answers_from_receipts(doc)
        self.assertEqual(sorted(got), ["Q1", "TASK-A"])
        self.assertEqual([(x["note"], x["state"], x["words"]) for x in got["TASK-A"]],
                         [("n2", "received", "said n2"), ("n3", "sent", "said n3"), ("n4", "unwoken", "said n4")])
        self.assertEqual((got["Q1"][0]["state"], got["Q1"][0]["reply"]), ("replied", {"at": T0 + 99, "text": "done it"}))

    def test_a_card_shows_the_answer_to_the_decision_open_now(self):
        old = {"note": "n1", "at": T0 - 500, "state": "replied", "words": "x", "reply": None}
        new = {"note": "n2", "at": T0 + 10, "state": "received", "words": "y", "reply": None}
        ask = self.agent(decisions=[("needs-decision", "k", T0, "?")])
        self.assertEqual(shipmate.answer_marks(self.snap([ask], answers={"TASK-A": [old]})), {})   # it answered an earlier decision
        self.assertEqual(shipmate.answer_marks(self.snap([ask], answers={"TASK-A": [old, new]})), {"TASK-A": "received"})
        self.assertEqual(shipmate.answer_marks(self.snap([self.agent()], answers={"TASK-A": [new]})), {})   # nothing waits any more
        held = {"id": "Q1", "title": "Q", "reasons": [("decision", "d", "x")], "hold_reason": "", "waiting_since": T0 - 900}
        self.assertEqual(shipmate.answer_marks(self.snap(queue=[held], answers={"Q1": [old]})), {"Q1": "replied"})
        sc = shipmate.ship_scene(dict(shipmate.demo_snapshot(T0)))
        self.assertEqual({x["id"]: x["answer"] for x in sc["line"]},
                         {"DEMO-Q-BILL": "replied", "DEMO-EXPORT": "received", "DEMO-RATE": None, "DEMO-Q-TILES": None})
        log = shipmate.captain_log(sc)
        self.assertIn('class="sm-ans sm-ans--replied"', log)
        self.assertIn('class="sm-ans sm-ans--received"', log)

    def test_the_drawer_asks_and_shows_the_thread(self):
        ask = shipmate.captain_asks(self.snap([self.agent(decisions=[("needs-decision", "pick", T0, "A) left B) right")])]), "TASK-A")
        answers = [{"note": "n1", "at": T0 + 5, "state": "replied", "words": "go <left>", "reply": {"at": T0 + 9, "text": "went"}},
                   {"note": "n2", "at": T0 + 6, "state": "unwoken", "words": "w", "reply": None}]
        out = shipmate.answer_section("TASK-A", ask, answers, "tok", T0 + 10)
        for piece in ('data-token="tok"', 'data-ref="pick"', 'name="task" value="TASK-A"', 'value="A"', "<b>B)</b> <span>right</span>",
                      "Send to firstmate", "go &lt;left&gt;", "went", 'data-announce="n2"', "explicit word on that PR"):
            self.assertIn(piece, out)
        self.assertRegex(out, r'name="request" value="[A-Za-z0-9_-]{1,10}"')
        self.assertEqual(shipmate.answer_section("TASK-A", [], [], "tok", T0), "")
        thread_only = shipmate.answer_section("TASK-A", [], answers[:1], "tok", T0 + 10)
        self.assertNotIn("<form", thread_only)
        self.assertIn("went", thread_only)


class TestFirstmateSession(Base):
    """The supervisor's own session, found through the home's lock in the home's project folder."""

    def setUp(self):
        super().setUp()
        self.hf = os.path.join(self.acct, "projects", shipmate.encode_dir(self.home))
        self.now = time.time()

    def session(self, name, calls, compactions=(), mtime=None):
        t = Transcript()
        for ts, ctx in calls:
            t.call(ts, inp=1, cw=ctx // 10, cr=ctx - 1 - ctx // 10, out=5)
        for ts, pre, post in compactions:
            t.lines.append({"type": "system", "subtype": "compact_boundary", "timestamp": iso(ts),
                            "compactMetadata": {"trigger": "auto", "preTokens": pre, "postTokens": post}})
        t.lines.sort(key=lambda x: x["timestamp"])
        path = t.save(os.path.join(self.hf, name + ".jsonl"))
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def snap(self, **kw):
        for k, v in kw.items():
            setattr(self.cfg, k, v)
        return shipmate.firstmate_session(self.cfg, shipmate.TranscriptIndex(self.cfg.claude_dirs), self.now)

    def test_lock_session_context_growth_compactions(self):
        n = self.now
        self.session("sess-locked", [(n - 5 * 3600, 150000), (n - 4 * 3600, 300000), (n - 3600, 60000), (n - 60, 90000)],
                     compactions=[(n - 2 * 3600, 320000, 40000)], mtime=n - 600)
        self.session("sess-newer", [(n - 30, 10000)], mtime=n)            # newer, but not the lock's
        write(os.path.join(self.home, "state", ".lock-session"), "sess-locked\n")
        fm = self.snap()
        self.assertEqual(fm["session"], "sess-locked")
        self.assertEqual(fm["found_by"], "the session lock")
        self.assertEqual(fm["context"], 90000)                           # input + cache of the last call
        self.assertEqual((fm["limit"], fm["limit_how"]), (1000000, "inferred"))   # it already held 320k
        self.assertEqual(fm["compactions"], 1)
        self.assertEqual(fm["last_compaction"]["after"], 40000)
        self.assertTrue(fm["growth_since_compaction"])                   # compaction inside the 3 h window
        self.assertEqual(fm["growth"], 50000)
        self.assertEqual(fm["today_new"] > 0, True)
        html_out = shipmate.block_firstmate(fm, n)
        self.assertIn("9% used", html_out)
        self.assertIn("1 compaction,", html_out)
        self.assertNotIn("sm-warnline", html_out)

    def test_small_session_infers_200k_warns_and_honors_config(self):
        n = self.now
        self.session("s1", [(n - 4 * 3600, 50000), (n - 120, 150000)])
        write(os.path.join(self.home, "state", ".lock-session"), "s1\n")
        fm = self.snap()
        self.assertEqual(fm["limit"], 200000)
        self.assertEqual(fm["compactions"], 0)
        self.assertFalse(fm["growth_since_compaction"])
        self.assertEqual(fm["growth"], 100000)
        self.assertIn("sm-warnline", shipmate.block_firstmate(fm, n))     # 75% is past the default 60%
        fm = self.snap(context_limit=1000000, context_warn=80)
        self.assertEqual((fm["limit"], fm["limit_how"]), (1000000, "configured"))
        self.assertNotIn("sm-warnline", shipmate.block_firstmate(fm, n))

    def test_without_lock_uses_newest_and_without_folder_is_unknown(self):
        self.assertIsNone(self.snap())
        self.assertIn("unknown", shipmate.block_firstmate(None, self.now))
        n = self.now
        self.session("old", [(n - 900, 1000)], mtime=n - 900)
        self.session("new", [(n - 30, 2000)], mtime=n)
        fm = self.snap()
        self.assertEqual(fm["session"], "new")
        self.assertIn("newest", fm["found_by"])
        self.assertIsNone(fm["growth"])                                  # nothing before the last 3 h


class TestAssets(unittest.TestCase):
    def test_the_page_plays_the_lighthouse_the_fish_and_walks_the_gangplank(self):
        page = shipmate.asset("shipmate.html")
        players = page.split("var PLAYERS = {")[1].split("};")[0]
        for kind in ("beacon: playBeacon", "fish: playFish", "leave: playLeave", "board: playWalk", "handover: playHandover"):
            self.assertIn(kind, players)
        self.assertNotIn("checks", players)
        self.assertEqual(set(re.findall(r"(\w+): play", players)), {k for _d, k in shipmate.EVENT_DETECTORS} - {"walk", "leave"}
                         | {"board", "move", "leave", "handover"})
        self.assertNotIn("plankOut", page)                                  # the gangplank is always out now
        self.assertIn('place === "watch" ? "bow"', page)                     # the watch walks like the bow
        css = shipmate.asset("shipmate.css")
        for cls in (".sm-beacon.is-running", ".sm-beacon.is-pass", ".sm-beacon.is-fail", ".motion-all .sm-beacon.is-running"):
            self.assertIn(cls, css)

    def test_assets_have_no_external_dependency(self):
        for name in ("shipmate.html", "shipmate.css"):
            text = shipmate.asset(name)
            self.assertNotIn("https://", text)
            self.assertNotIn("@import", text)
        self.assertIn("prefers-color-scheme: dark", shipmate.asset("shipmate.css"))


def svg_hit(svg, css, x, y):
    """Which hand a click at art pixel (x, y) of the ship reaches, decided as a browser does: the
    last shape painted there, in document order, among those the stylesheet (a rule of plain class
    selectors with `pointer-events: none`) or an attribute does not take out of hit-testing. Returns
    that shape's hand id, or the shape's tag and class when it belongs to no hand."""
    import xml.etree.ElementTree as ET
    off = set()
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
        if re.search(r"pointer-events:\s*none", body):
            off.update(sel.strip()[1:] for sel in head.split(",") if re.fullmatch(r"\s*\.[\w-]+\s*", sel))
    found = [None]

    def covers(el):
        a = el.attrib
        if el.tag == "rect" and a.get("fill") != "none":
            return float(a["x"]) <= x < float(a["x"]) + float(a["width"]) and float(a["y"]) <= y < float(a["y"]) + float(a["height"])
        if el.tag == "path":
            return any(int(px) <= x < int(px) + int(w) and int(py) <= y < int(py) + 1
                       for px, py, w in re.findall(r"M(-?\d+) (-?\d+)h(\d+)v1h-\d+z", a["d"]))
        if el.tag == "circle":
            return (x - float(a["cx"])) ** 2 + (y - float(a["cy"])) ** 2 <= float(a["r"]) ** 2
        if el.tag == "polygon":
            pts = [tuple(map(float, p.split(","))) for p in a["points"].split()]
            inside = False
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
            return inside
        if el.tag == "line":
            (x1, y1, x2, y2) = (float(a[k]) for k in ("x1", "y1", "x2", "y2"))
            dx, dy = x2 - x1, y2 - y1
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / float(dx * dx + dy * dy or 1)))
            return (x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2 <= 0.25
        return False

    def walk(el, hand, quiet):
        cls = set((el.get("class") or "").split())
        quiet = quiet or bool(cls & off) or el.get("pointer-events") == "none"
        if "sm-hand" in cls:
            hand = el.get("data-id")
        if not quiet and covers(el):
            found[0] = hand or "%s.%s" % (el.tag, ".".join(sorted(cls)))
        for child in el:
            walk(child, hand, quiet)
    root = ET.fromstring(svg)
    assert "transform" not in svg, "the hit test reads shapes at their own coordinates"
    walk(root, None, False)
    return found[0]


class TestShip(unittest.TestCase):
    """The ship view: where each hand stands, the red flags, rest, colours, layout and drawing."""

    def hand(self, tid, phase, kind="ship", project="demo-app", crew="working", decisions=(), last=None):
        phases = shipmate.SCOUT_PHASES if kind == "scout" else shipmate.SHIP_PHASES
        return {"id": tid, "title": "t " + tid, "kind": kind, "project": project, "phases": phases, "phase": phase,
                "crew": crew, "decisions": list(decisions), "last_activity": T0 - 60 if last is None else last}

    def snap(self, agents, queue=(), lanes=(), fm=None, machine=None, delivered=(), projects=(), now=T0):
        return {"now": now, "agents": agents, "queue": list(queue), "quota": {"lanes": list(lanes)},
                "firstmate": fm, "machine": machine, "delivered": list(delivered), "projects": list(projects)}

    def test_status_fields_in_separate_brackets_and_open_decisions(self):
        path = os.path.join(tempfile.mkdtemp(prefix="shipmate-test-"), "x.status")
        write(path, "working [at=1]: go\nneeds-decision [key=a] [at=2]: pick a\nblocked [at=3]: stuck\n"
                    "needs-decision [at=4 key=b]: pick b\nresolved [key=a] [at=5]: a done\nworking [at=6]: on\n")
        st = shipmate.read_status(path)
        self.assertEqual([(x["state"], x["at"], x["key"]) for x in st],
                         [("working", 1, None), ("needs-decision", 2, "a"), ("blocked", 3, None),
                          ("needs-decision", 4, "b"), ("resolved", 5, "a"), ("working", 6, None)])
        # a resolved line closes only its own key, and a later working line closes nothing
        self.assertEqual([(d["state"], d["key"]) for d in shipmate.open_decisions(st)],
                         [("blocked", None), ("needs-decision", "b")])
        st.append({"state": "resolved", "at": 7, "key": None, "text": "unstuck"})
        self.assertEqual([d["key"] for d in shipmate.open_decisions(st)], ["b"])

    def test_phase_to_place_and_pose(self):
        agents = [self.hand("S%d" % i, i) for i in range(5)] + [self.hand("SX", None)]
        agents += [self.hand("C%d" % i, i, kind="scout") for i in range(3)] + [self.hand("CX", None, kind="scout")]
        sc = shipmate.ship_scene(self.snap(agents))
        self.assertEqual({h["id"]: h["place"] for h in sc["hands"]},
                         {"S0": "nest", "S1": "deck", "S2": "bow", "S3": "watch", "S4": "rail", "SX": "hatch"})
        self.assertEqual({h["id"]: h["place"] for h in sc["scouts"]}, {"C0": "out", "C1": "back", "C2": "dock", "CX": "out"})
        poses = {k: v[2] for k, v in shipmate.ship_layout(sc)["hands"].items()}
        self.assertEqual(poses, {"S0": "spy", "S1": "hammer", "S2": "signal", "S3": "spy", "S4": "fish", "SX": "stand"})
        self.assertEqual(shipmate.ship_counts(sc), "10 hands aboard · 0 in the queue")

    def test_red_flags_for_the_captain(self):
        agents = [self.hand("OK", 1), self.hand("PARK", 1, crew="parked"), self.hand("FAIL", 2, crew="failed"),
                  self.hand("ASK", 1, decisions=[{"state": "needs-decision", "key": "k", "at": 1, "text": "a or b"}]),
                  self.hand("DONE", 4, crew="done")]
        queue = [{"id": "Q1", "reasons": [("decision", "captain's decision", "x")]},
                 {"id": "Q2", "reasons": [("date", "held until Mon", "x")]}]
        sc = shipmate.ship_scene(self.snap(agents, queue=queue))
        self.assertEqual({h["id"]: h["flag"] for h in sc["hands"]},
                         {"OK": False, "PARK": True, "FAIL": True, "ASK": True, "DONE": False})
        self.assertIn("needs-decision k: a or b", next(h for h in sc["hands"] if h["id"] == "ASK")["why"])
        self.assertEqual([q["flag"] for q in sc["queue"]], [True, False])
        self.assertEqual(sc["flags"], 4)
        self.assertIn("4 red flags", shipmate.ship_counts(sc))

    def test_who_waits_on_the_captain_and_in_what_order(self):
        ask = lambda at: {"state": "needs-decision", "key": None, "at": at, "text": "a or b"}
        agents = [self.hand("OK", 1, decisions=[]),
                  self.hand("PARK", 1, crew="parked", last=T0 - 600),                  # no line: since it went quiet
                  self.hand("ASK", 0, decisions=[ask(T0 - 3000)]),
                  self.hand("ASK2", 2, decisions=[ask(T0 - 100), ask(T0 - 5000)]),     # its oldest open line counts
                  self.hand("TIE-B", 3, decisions=[ask(T0 - 3000)]),
                  self.hand("SCOUT", 1, kind="scout", crew="blocked", last=T0 - 200)]
        queue = [{"id": "Q1", "reasons": [("decision", "captain's decision", "x")], "waiting_since": T0 - 4000, "waiting_exact": True},
                 {"id": "Q2", "reasons": [("date", "held until Mon", "x")], "waiting_since": T0 - 9999},
                 {"id": "Q3", "reasons": [("decision", "captain's decision", "x")], "waiting_since": None},
                 {"id": "Q4", "reasons": [("decision", "captain's decision", "x")], "waiting_since": T0 - 86400 * 2,
                  "waiting_exact": False}]
        sc = shipmate.ship_scene(self.snap(agents, queue=queue))
        self.assertEqual([(x["id"], x["line"]) for x in sc["line"]],
                         [("Q4", 1), ("ASK2", 2), ("Q1", 3), ("ASK", 4), ("TIE-B", 5), ("PARK", 6), ("SCOUT", 7), ("Q3", 8)])
        self.assertEqual([x["wait"] for x in sc["line"]], ["2d", "1h 23m", "1h 06m", "50m", "50m", "10m", "3m", "?"])
        self.assertEqual(sc["line"][1]["tag"], "2 · ASK2 · 1h 23m")
        not_in_line = [h for h in sc["hands"] + sc["queue"] if h["id"] in ("OK", "Q2")]
        self.assertEqual([(h["line"], h["since"]) for h in not_in_line], [(0, None), (0, None)])
        self.assertEqual(sc["flags"], len(sc["line"]))
        self.assertEqual((sc["captain"]["busy"], sc["captain"]["waiting"], sc["captain"]["oldest"]["id"]), (True, 8, "Q4"))
        # the same snapshot gives the same line
        self.assertEqual([x["id"] for x in shipmate.ship_scene(self.snap(agents, queue=queue))["line"]], [x["id"] for x in sc["line"]])

    def test_the_captain_is_off_duty_when_nobody_waits(self):
        sc = shipmate.ship_scene(self.snap([self.hand("OK", 1)], queue=[{"id": "Q", "reasons": [("slot", "s", "s")]}]))
        self.assertEqual((sc["line"], sc["captain"]), ([], {"busy": False, "waiting": 0, "oldest": None}))
        svg = shipmate.render_ship(sc)
        self.assertIn("sm-captain is-off", svg)
        self.assertNotIn("sm-balloon", svg)
        self.assertNotIn("sm-num", svg)
        self.assertIn("All clear", shipmate.captain_log(sc))
        busy = shipmate.ship_scene(self.snap([self.hand("ASK", 1, crew="parked")]))
        svg = shipmate.render_ship(busy)
        self.assertIn("sm-captain is-busy", svg)
        self.assertIn("1 waiting for you", svg)
        self.assertIn('data-id="ASK"', shipmate.captain_log(busy))
        self.assertIn("1 waits on my word", shipmate.captain_log(busy))

    def test_the_line_waits_in_the_cabin_and_the_rest_crowd_the_door(self):
        flagged = [self.hand("F%d" % i, 1, crew="parked", last=T0 - 100 * i) for i in range(5)]
        queue = [{"id": "Q%d" % i, "repo": "demo-app", "reasons": [("decision", "c", "c")], "waiting_since": T0 - i} for i in range(9)]
        queue.append({"id": "FREE", "reasons": [("ready", "r", "r")]})
        sc = shipmate.ship_scene(self.snap(flagged + [self.hand("OK", 1)], queue=queue))
        order = [x["id"] for x in sc["line"]]
        self.assertEqual(sorted(order), sorted([h["id"] for h in flagged] + ["Q%d" % i for i in range(9)]))
        lay = shipmate.ship_layout(sc)
        empty = shipmate.ship_layout(shipmate.ship_scene(self.snap([self.hand("OK", 1)])))
        one = shipmate.ship_layout(shipmate.ship_scene(self.snap([self.hand("A", 1, crew="parked")])))
        self.assertEqual({lay["castle"], one["castle"]}, {empty["castle"]})     # the cabin never grows, so neither does the ship
        self.assertEqual(lay["seated"], order[:shipmate.CABIN_SEATS])           # the longest waits inside, in line order
        self.assertEqual([x["id"] for x in lay["crowd"]], order[shipmate.CABIN_SEATS:])   # the rest crowd the door
        cx0, cx1 = lay["cabin"]
        xs = [lay["hands"][i][0] for i in lay["seated"]]
        self.assertEqual(xs, sorted(xs))                                        # the first in line nearest the desk
        for i in lay["seated"]:
            x, _feet, pose, _u = lay["hands"][i]
            self.assertTrue(pose.endswith("-l") and cx0 <= x and x + len(shipmate.POSES[pose][0]) <= cx1 - shipmate.CABIN_DOOR, i)
        for x in lay["crowd"]:                                                  # nobody in the crowd stands anywhere else
            self.assertNotIn(x["id"], lay["hands"])
            self.assertNotIn(x["id"], lay["pier"])
        self.assertEqual(lay["hands"]["OK"][2], "hammer")
        self.assertEqual([q["id"] for q in sc["pier"]], ["FREE"])                # no decision waits on the pier
        self.assertIn(lay["pier"]["FREE"][0], range(lay["x0"] + 92, lay["width"]))
        svg = shipmate.render_ship(sc)
        for i in lay["seated"]:
            self.assertEqual(svg.count('data-id="%s"' % i), 1, i)
        for x in lay["crowd"]:
            self.assertNotIn('data-id="%s"' % x["id"], svg)
        crowd = re.search(r'<g class="sm-crowd" data-action="crowd" data-crowd="([^"]*)"', svg)
        self.assertEqual(crowd.group(1).split(" "), order[3:])
        self.assertIn(">+11</text>", svg)
        self.assertIn("numbers 4 to 14", svg)
        self.assertEqual(svg.count('class="sm-num"'), 3 + 1)                    # the three inside, and the crowd's count
        self.assertNotIn("sm-boat", svg)
        self.assertNotIn("ASTERN", svg)

    def test_a_queued_decision_in_the_cabin_opens_its_own_card(self):
        queue = [{"id": "QA", "title": "t QA", "repo": "demo-app", "reasons": [("decision", "c", "which one")], "waiting_since": T0 - 9}]
        sc = shipmate.ship_scene(self.snap([self.hand("F", 1, crew="parked", last=T0 - 1)], queue=queue))
        q = sc["queue"][0]
        self.assertEqual((q["place"], q["stage"], q["line"]), ("cabin", "flag", 1))
        lay = shipmate.ship_layout(sc)
        self.assertEqual((lay["seated"], lay["crowd"]), (["QA", "F"], []))
        self.assertEqual(lay["hands"]["QA"][2], "stand-l")
        svg = shipmate.render_ship(sc)
        # the one clickable group for QA is a hand the page opens by its id, flagged, with its number and its bag
        group = re.search(r'<g class="sm-hand pose-stand has-flag" data-id="QA" data-stage="flag"[^>]*>(.*?)</g>', svg)
        self.assertIsNotNone(group)
        self.assertIn("cabin with a sea bag: queued, waits for your decision", group.group(0))
        self.assertIn("#c9b48a", group.group(1))
        self.assertIn(".sm-hand[data-id]", shipmate.asset("shipmate.html").split("var OPENS = ")[1].split(";")[0])
        self.assertIn('data-id="QA"', shipmate.captain_log(sc))
        self.assertEqual(svg.count('class="sm-num"'), 2)

    def test_the_crowd_leads_to_every_grouped_card_in_the_log(self):
        snap = shipmate.demo_snapshot(T0)
        sc = shipmate.ship_scene(snap)
        lay = shipmate.ship_layout(sc)
        self.assertEqual(len(sc["line"]), 4)
        self.assertEqual([x["id"] for x in lay["crowd"]], [sc["line"][3]["id"]])
        svg, log = shipmate.render_ship(sc), shipmate.captain_log(sc)
        ids = re.search(r'data-crowd="([^"]*)"', svg).group(1).split(" ")
        for i in ids:                                                       # one click finds it in the log, one more opens it
            self.assertIn('class="sm-logcard" data-id="%s"' % i, log)
        page = shipmate.asset("shipmate.html")
        self.assertIn('else if (a === "crowd") toLog(act.getAttribute("data-crowd"));', page)
        self.assertIn(".sm-logcard[data-id=", page.split("function toLog")[1].split("function openAdd")[0])
        self.assertIn(".sm-crowd", page.split("function members")[1].split("}")[0])  # the red-flag chip lights it too

    def test_the_lighthouse_ahead_of_the_bow_shows_who_waits_for_ci(self):
        signal = [self.hand("B%d" % i, 2) for i in range(2)]
        watch = [self.hand("W%d" % i, 3) for i in range(3)]
        queue = [{"id": "R%d" % i, "reasons": [("ready", "ready", "x")]} for i in range(9)]
        sc = shipmate.ship_scene(self.snap(signal + watch, queue=queue))
        lay = shipmate.ship_layout(sc)
        body = {k: x + shipmate.pose_left(p) for k, (x, _f, p, _u) in lay["hands"].items()}
        # on the forecastle, the spyglasses ahead of the flags, nearest the lighthouse, which stands past the bow
        self.assertTrue(max(body["B0"], body["B1"]) < min(body["W0"], body["W1"], body["W2"]))
        self.assertTrue(all(lay["fc_x"] < body[k] and body[k] + 19 <= lay["x0"] + lay["length"] for k in body))
        self.assertGreater(lay["light_x"] - 5, lay["x0"] + lay["length"])
        self.assertGreaterEqual(lay["crew"][0] + lay["crew"][2], lay["light_x"] + shipmate.LIGHT_W)   # in the opening frame
        # the queue on the pier and the lamp posts keep clear of it, even where the line would stand
        with mock.patch.object(shipmate, "LIGHT_GAP", 100 - lay["length"]):
            near = shipmate.ship_layout(sc)
        a, b = shipmate.light_span(near)
        self.assertLess(a, near["x0"] + 92 + shipmate.LINE_STEP)                       # right where the line begins
        self.assertTrue(all(x >= b for x, _f, _p, _u in near["pier"].values()))         # so the line steps past it
        for lay_ in (lay, near):
            a, b = shipmate.light_span(lay_)
            self.assertFalse([x for x in shipmate.pier_lamp_xs(lay_) if a - shipmate.LAMP_CLEAR <= x <= b + shipmate.LAMP_CLEAR])
        svg = shipmate.render_ship(sc)
        self.assertIn('<g class="sm-beacon is-running"><title>the lighthouse: the checks are running for W0, W1, W2</title>', svg)
        self.assertIn("LIGHTHOUSE · CI", svg)
        self.assertIn("BOW · PR open and CI", svg)
        self.assertRegex(shipmate.stage_chips(sc), r'data-stage="watch"[^>]*>.*?<span>waiting for CI</span><b>3</b>')
        self.assertIn("W0 · demo-app · at the bow with a spyglass on the lighthouse: the checks are running", svg)
        idle = shipmate.render_ship(shipmate.ship_scene(self.snap(signal)))
        self.assertIn('<g class="sm-beacon is-idle"><title>the lighthouse: nobody is waiting for CI</title>', idle)
        # the gangplank stays out with nobody on it: the way ashore with the crate, and aboard from the queue
        self.assertIn('<g class="sm-plank">', idle)
        self.assertNotIn("pose-pack", svg + idle)

    def test_wide_props_seat_fewer_and_the_line_keeps_its_order(self):
        fishers = [self.hand("R%d" % i, 4, crew="parked", last=T0 - 100 + i) for i in range(3)]
        narrow = self.hand("N", 1, crew="parked", last=T0 - 10)
        sc = shipmate.ship_scene(self.snap(fishers + [narrow]))
        lay = shipmate.ship_layout(sc)
        self.assertEqual(lay["seated"], ["R0", "R1"])                        # three rods do not fit before the door
        self.assertEqual([x["id"] for x in lay["crowd"]], ["R2", "N"])         # and the narrow one does not jump the line
        self.assertEqual(lay["castle"], shipmate.ship_layout(shipmate.ship_scene(self.snap([])))["castle"])

    def test_wait_text(self):
        self.assertEqual([shipmate.wait_text(T0 - d, T0) for d in (5, 600, 3 * 3600 + 300, 2 * 86400 + 4 * 3600)],
                         ["<1m", "10m", "3h 05m", "2d 4h"])
        self.assertEqual(shipmate.wait_text(None, T0), "?")
        self.assertEqual(shipmate.wait_text(T0 - 3600, T0, exact=False), "today" if dt.date.fromtimestamp(T0 - 3600) == dt.date.fromtimestamp(T0) else "1d")

    def test_stage_chips_count_and_describe_each_stage(self):
        sc = shipmate.ship_scene(shipmate.demo_snapshot(T0))
        chips = shipmate.stage_chips(sc)
        for key, _pose, name, where in shipmate.STAGES:
            self.assertIn('data-stage="%s"' % key, chips)
            self.assertIn(html.escape(where), chips)
        self.assertRegex(chips, r'data-stage="flag"[^>]*>.*?<b>%d</b>' % len(sc["line"]))
        quiet = shipmate.stage_chips(shipmate.ship_scene(self.snap([self.hand("A", 1)])))
        self.assertNotIn('data-stage="hatch"', quiet)                    # an empty unusual stage is left out
        self.assertRegex(quiet, r'data-stage="flag"[^>]*disabled')       # an empty usual one is greyed

    def test_off_duty(self):
        agents = [self.hand("PAUSED", 1, crew="paused"), self.hand("QUIET", 0, crew="unknown", last=T0 - 3600),
                  self.hand("BUSY", 0, crew="working", last=T0 - 3600), self.hand("FRESH", 1, crew="unknown"),
                  self.hand("FISH", 4, crew="paused"), self.hand("SPY", 3, crew="paused"),
                  self.hand("FLAG", 1, crew="parked", last=T0 - 3600)]
        sc = shipmate.ship_scene(self.snap(agents))
        self.assertEqual({h["id"]: h["off"] for h in sc["hands"]},       # waiting for CI or the merge is the job itself
                         {"PAUSED": True, "QUIET": True, "BUSY": False, "FRESH": False, "FISH": False, "SPY": False, "FLAG": False})
        self.assertEqual(shipmate.ship_layout(sc)["hands"]["PAUSED"][2], "rest")
        self.assertIn("2 off duty", shipmate.ship_counts(sc))

    def test_project_colours_follow_the_project_list(self):
        agents = [self.hand("A", 1, project="beta"), self.hand("B", 1, project="zeta"), self.hand("C", 1, project="beta")]
        sc = shipmate.ship_scene(self.snap(agents, projects=["alpha", "beta"]))
        cols = shipmate.PROJECT_COLORS
        self.assertEqual(sc["colors"], {"beta": cols[1], "zeta": cols[2]})      # alpha has nobody aboard
        self.assertEqual({h["id"]: h["color"] for h in sc["hands"]}, {"A": cols[1], "B": cols[2], "C": cols[1]})
        self.assertNotIn(shipmate.INK["r"], cols)                              # red stays for red flags

    def test_day_night_barrels_bilge_and_sea(self):
        noon = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))
        self.assertFalse(shipmate.is_night(noon))
        self.assertTrue(shipmate.is_night(noon + 11 * 3600))
        lanes = [{"label": "b", "remaining": 50, "runway": "through_reset", "resets": T0 + 9, "exhausts": None},
                 {"label": "a", "remaining": 40, "runway": "projected_exhaustion", "resets": T0 + 9, "exhausts": T0 + 5},
                 {"label": "c", "remaining": 10, "runway": "through_reset", "resets": T0 + 9, "exhausts": None}]
        sc = shipmate.ship_scene(self.snap([], lanes=lanes, fm={"percent": 61.0, "warn_at": 60, "context": 1, "limit": 2},
                                           machine={"load": 3.0, "cpus": 4}))
        self.assertEqual([(b["label"], b["empty"], b["early"]) for b in sc["barrels"]],
                         [("c", True, False), ("a", True, True), ("b", False, False)])
        self.assertTrue(sc["firstmate"]["over"])
        self.assertEqual(sc["sea"], "choppy")
        self.assertFalse(shipmate.ship_scene(self.snap([], fm={"percent": 59.0, "warn_at": 60}))["firstmate"]["over"])
        self.assertEqual(shipmate.sea_state(None), "calm")
        self.assertEqual(shipmate.sea_state({"load": 9.0, "cpus": 8}), "rough")

    def test_layout_gives_every_hand_its_own_spot_and_grows_with_the_crew(self):
        small = shipmate.ship_layout(shipmate.ship_scene(self.snap([self.hand("A", 1)])))
        crowd = [self.hand("H%02d" % i, i % 5) for i in range(40)] + [self.hand("U%d" % i, None) for i in range(4)]
        big = shipmate.ship_layout(shipmate.ship_scene(self.snap(crowd)))
        self.assertGreater(big["length"], small["length"])
        self.assertGreater(big["width"], small["width"])
        spots = [(x, feet) for x, feet, _pose, _upto in big["hands"].values()]
        self.assertEqual(len(big["hands"]), 44)
        self.assertEqual(len(set(spots)), len(spots))
        for x, feet in spots:
            self.assertTrue(0 <= x < big["width"] and 0 < feet < shipmate.SHIP_H, (x, feet))

    def test_hands_on_one_row_never_stand_on_each_other(self):
        crowd = ([self.hand("D%02d" % i, 1) for i in range(12)] + [self.hand("R%d" % i, 3) for i in range(6)]
                 + [self.hand("B%d" % i, 2) for i in range(5)] + [self.hand("N%d" % i, 0) for i in range(6)]
                 + [self.hand("P%d" % i, 4) for i in range(6)] + [self.hand("U%d" % i, None) for i in range(4)]
                 + [self.hand("F%d" % i, 1, crew="parked") for i in range(5)])
        lay = shipmate.ship_layout(shipmate.ship_scene(self.snap(crowd)))
        rows = {}
        for k, (x, feet, pose, _upto) in lay["hands"].items():
            rows.setdefault(feet, []).append((x, x + len(shipmate.POSES[pose][0]), k))
        for feet, row in rows.items():
            row.sort()
            for (_a0, a1, a), (b0, _b1, b) in zip(row, row[1:]):
                self.assertLessEqual(a1, b0, (feet, a, b))

    def test_a_click_on_a_hand_reaches_that_hand_by_day_and_by_night(self):
        css = shipmate.asset("shipmate.css")
        for hour in (12, 22):
            now = time.mktime((2026, 11, 13, hour, 0, 0, 0, 0, -1))
            sc = shipmate.ship_scene(shipmate.demo_snapshot(now))
            self.assertEqual(sc["night"], hour == 22)
            lay = shipmate.ship_layout(sc)
            svg = shipmate.render_ship(sc)
            who = {h["id"]: h for h in sc["hands"] + sc["pier"]}
            # on deck, at the rail, in line in the cabin, standing on the pier, at the tavern's table
            for tid in ("DEMO-AUTH", "DEMO-LOGS", "DEMO-RATE", "DEMO-Q-THEME", "DEMO-Q-PUSH"):
                x, feet, pose, upto = lay["hands"].get(tid) or lay["pier"][tid]
                rows = shipmate.hand_rows(who[tid], pose)[:upto]
                top = feet - len(rows) + 1
                inside = [(x + i + 0.5, top + j + 0.5) for j, row in enumerate(rows) for i, ch in enumerate(row) if ch in "Ec"]
                self.assertTrue(inside, tid)
                for px, py in inside:
                    self.assertEqual(svg_hit(svg, css, px, py), tid, (hour, tid, px, py))

    def test_every_pose_is_drawn_on_the_hand_grid(self):
        self.assertEqual((len(shipmate.STAND[0]), len(shipmate.STAND)), (13, 21))
        self.assertEqual((len(shipmate.HELM[0]), len(shipmate.HELM)), (13, 21))
        bases = [k for k in shipmate.POSES if not k.endswith("-l")]
        self.assertEqual(set(bases), set(shipmate.POSE_HEAD))
        for k in bases:
            rows = shipmate.POSES[k]
            self.assertEqual(len({len(r) for r in rows}), 1, k)
            self.assertEqual(shipmate.POSES[k + "-l"], [r[::-1] for r in rows], k)
            ox, oy = shipmate.POSE_HEAD[k]
            self.assertEqual(rows[oy + 3][ox + 3], "h", k)                      # the hair behind the face, where POSE_HEAD says
        for rows in (shipmate.CAPTAIN_BUSY, shipmate.CAPTAIN_OFF):
            self.assertEqual(len({len(r) for r in rows}), 1)
        tip = shipmate.POSES["fish"][shipmate.ROD_TIP[1]][shipmate.ROD_TIP[0]]
        self.assertEqual(tip, "o")                                              # the line hangs from the rod

    def test_the_hat_follows_the_state(self):
        hat = shipmate.hand_hat
        self.assertEqual(hat({"id": "a", "place": "deck"}), "bandana")
        self.assertEqual(hat({"id": "a", "place": "deck", "off": True}), "straw")
        self.assertEqual(hat({"id": "a", "place": "pier-cards", "kind": "queue"}), "straw")
        self.assertEqual(hat({"id": "a", "place": "hatch"}), "wool")
        self.assertEqual(hat({"id": "a", "place": "deck", "off": True, "flag": True}), "bare")
        self.assertEqual(hat({"id": "a", "place": "cabin", "kind": "queue", "flag": True}), "bare")
        for pose in ("stand", "hammer", "signal", "drink", "cards-l"):
            base = pose.split("-")[0]
            ox, oy = shipmate.POSE_HEAD[base]
            work = shipmate.hand_rows({"id": "a", "place": "deck"}, pose)
            off = shipmate.hand_rows({"id": "a", "place": "deck", "off": True}, pose)
            self.assertEqual(work, shipmate.POSES[pose])
            head = "".join(off[oy:oy + 5])
            self.assertNotIn("k", head, pose)
            self.assertIn("z", head, pose)
            self.assertEqual(off[oy + 6:], work[oy + 6:], pose)                 # only the head changes

    def test_neckerchief_follows_the_account_list(self):
        agents = [dict(self.hand("A", 1), account="acct-b"), dict(self.hand("B", 1), account="acct-z"),
                  dict(self.hand("C", 1), account="acct-b"), self.hand("D", 1)]
        snap = dict(self.snap(agents, queue=[{"id": "Q", "reasons": [("ready", "r", "r")]}]), accounts=["acct-a", "acct-b"])
        sc = shipmate.ship_scene(snap)
        cols = shipmate.ACCOUNT_COLORS
        self.assertEqual(sc["accounts"], {"acct-b": cols[1], "acct-z": cols[2]})   # acct-a has nobody aboard
        self.assertEqual({h["id"]: h["neck"] for h in sc["hands"]}, {"A": cols[1], "B": cols[2], "C": cols[1], "D": None})
        self.assertEqual(shipmate.hand_palette(sc["hands"][0])["n"], cols[1])
        queued = sc["pier"][0]
        self.assertEqual(shipmate.hand_palette(queued)["n"], shipmate.dark(queued["color"], 0.55))
        self.assertNotIn(shipmate.INK["r"], cols)                              # red stays for red flags
        self.assertIn("acct-b", shipmate.block_ship(snap)["accounts"])

    def test_name_tags_never_overlap(self):
        tags = shipmate.Tags()
        boxes = [tags.place("TASK-%d" % i, 100 + 3 * i, 120) for i in range(6)]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertTrue(a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3], (a, b))

    def test_drawing_has_a_hand_per_agent_and_per_queue_item(self):
        snap = shipmate.demo_snapshot(T0)
        svg = shipmate.block_ship(snap)["ship"]
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        for a in snap["agents"]:
            self.assertEqual(svg.count('data-id="%s" data-stage=' % a["id"]), 1, a["id"])
            self.assertEqual(svg.count('<g class="sm-tag" data-for="%s"' % a["id"]), 1, a["id"])
        sc = shipmate.ship_scene(snap)
        crowd = {x["id"] for x in shipmate.ship_layout(sc)["crowd"]}
        for q in snap["queue"]:                                           # a hand in the cabin or on the pier, once, or in the crowd
            self.assertEqual(svg.count('data-id="%s" data-stage=' % q["id"]), 0 if q["id"] in crowd else 1, q["id"])
        self.assertEqual(crowd, {"DEMO-Q-TILES"})
        self.assertEqual(svg.count('class="sm-redflag"'), len(sc["line"]) - len(crowd))
        for layer in ("sm-far", "sm-mid", "sm-bob", "sm-near", "sm-tags"):
            self.assertIn('class="%s"' % layer, svg)
        self.assertNotIn("http", svg)
        self.assertNotIn("<image", svg)
        self.assertEqual(svg, shipmate.block_ship(snap)["ship"])   # the same snapshot draws the same picture
        night = dict(snap, now=time.mktime((2026, 11, 13, 23, 0, 0, 0, 0, -1)))
        self.assertIn("is-night", shipmate.block_ship(night)["ship"])

    def test_the_demo_covers_every_place(self):
        sc = shipmate.ship_scene(shipmate.demo_snapshot(T0))
        places = {h["place"] for h in sc["hands"]} | {h["place"] for h in sc["scouts"]}
        self.assertEqual(places, set(shipmate.PLACES) | {"hatch"} | set(shipmate.SCOUT_PLACES))
        self.assertTrue(sc["firstmate"]["over"])
        self.assertTrue(any(b["empty"] for b in sc["barrels"]))
        self.assertGreaterEqual(sc["off"], 1)
        self.assertGreaterEqual(sc["flags"], 3)
        self.assertEqual(len(sc["colors"]), 3)
        self.assertEqual({q["act"] for q in sc["pier"]}, set(shipmate.PIER_ACTS))   # its decisions wait in the cabin

    def queue_item(self, tid, reason, created="2030-01-01", deps=(), slot=None, until="", day=""):
        short = until or reason
        return {"id": tid, "title": "t " + tid, "repo": "demo-app", "created": created, "deps": list(deps), "slot": slot,
                "reasons": [(reason, short, short)], "waiting_since": T0 - 60 if reason == "decision" else None,
                "hold_until": day}

    def test_what_a_queued_hand_does_on_the_pier(self):
        today = dt.date.fromtimestamp(T0).isoformat()
        act = shipmate.pier_activity
        self.assertEqual(act({"reason": "slot", "created": today}, set(), today), ("cards", None))
        self.assertEqual(act({"reason": "lane", "created": ""}, set(), today), ("cards", None))      # no date: not known to be old
        self.assertEqual(act({"reason": "slot", "created": "2000-01-01"}, set(), today), ("drink", None))
        self.assertEqual(act({"reason": "ready"}, set(), today), ("board", None))
        self.assertEqual(act({"reason": "date"}, set(), today), ("hammock", None))
        self.assertEqual(act({"reason": "task", "deps": ["GONE", "LIVE"]}, {"LIVE"}, today), ("talk", "LIVE"))
        self.assertEqual(act({"reason": "task", "deps": ["GONE"]}, {"LIVE"}, today), ("wait", None))
        old = dt.date.fromtimestamp(T0 - 3 * 86400).isoformat()
        slot = {"group": "demo-app", "cap": 2, "taken": 2, "holders": ["LIVE", "OTHER"]}
        queue = [self.queue_item("CARD", "slot", today, slot=slot), self.queue_item("MUG", "slot", old, slot=slot),
                 self.queue_item("GO", "ready"), self.queue_item("TALK", "task", deps=["LIVE"]),
                 self.queue_item("ALONE", "task", deps=["NOWHERE"]),
                 self.queue_item("NAP", "date", until="held until Mon 2 Jan", day="2040-01-02"),
                 self.queue_item("ASK", "decision")]
        sc = shipmate.ship_scene(self.snap([self.hand("LIVE", 1)], queue=queue))
        pier = {q["id"]: q for q in sc["pier"]}
        self.assertNotIn("ASK", pier)                                     # the captain's decision waits in the cabin, not on the pier
        self.assertNotIn("NAP", pier)                                     # a dated hold sleeps below deck, not on the pier
        self.assertEqual([(q["id"], q["place"], q["stage"], q["tag"]) for q in sc["quarters"]],
                         [("NAP", "quarters", "date", "NAP · 02 Jan")])
        self.assertEqual([(x["id"], x["place"]) for x in sc["line"]], [("ASK", "cabin")])
        self.assertEqual({k: (q["act"], q["stage"]) for k, q in pier.items()},
                         {"CARD": ("cards", "slot"), "MUG": ("drink", "slot"), "GO": ("board", "board"), "TALK": ("talk", "task"),
                          "ALONE": ("wait", "task")})
        self.assertEqual(pier["TALK"]["partner"], "LIVE")
        self.assertEqual((pier["TALK"]["tag"], pier["ALONE"]["tag"]), ("TALK → LIVE", "ALONE waits for NOWHERE"))
        self.assertEqual(pier["CARD"]["place"], "pier-cards")
        self.assertEqual(pier["CARD"]["color"], shipmate.PROJECT_COLORS[0])  # a queued hand wears its project's colour
        chips = shipmate.stage_chips(sc)
        for key, n in (("slot", 2), ("board", 1), ("task", 2), ("date", 1)):
            self.assertRegex(chips, r'data-stage="%s"[^>]*>.*?<b>%d</b>' % (key, n))
        quiet = shipmate.stage_chips(shipmate.ship_scene(self.snap([self.hand("A", 1)])))
        for key in shipmate.PIER_STAGES:                                  # an empty pier stage is left out
            self.assertNotIn('data-stage="%s"' % key, quiet)

    def test_the_tavern_stands_the_same_whatever_order_the_backlog_lists_the_queue_in(self):
        today = dt.date.fromtimestamp(T0).isoformat()
        one = {"group": "demo-app", "cap": 2, "taken": 2, "holders": []}
        two = {"group": "alpha-app", "cap": 1, "taken": 1, "holders": []}
        home = {"group": "", "cap": 3, "taken": 3, "holders": []}
        queue = ([self.queue_item("A%d" % i, "slot", today, slot=one) for i in range(4)]
                 + [self.queue_item("B%d" % i, "slot", "2000-01-01", slot=two) for i in range(3)]
                 + [self.queue_item("L%d" % i, "lane", today, slot=home) for i in range(2)])
        agents = [self.hand("H1", 1)]
        def where(q):
            lay = shipmate.ship_layout(shipmate.ship_scene(self.snap(agents, queue=q)))
            return lay["x0"], lay["pier"], [u["sign"] for u in lay["tavern"]]
        want = where(queue)
        self.assertTrue(want[2][0].startswith("SLOT alpha-app") and want[2][-1].startswith("HOME LANES"), want[2])
        for seed in range(6):
            shuffled = list(queue)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(where(shuffled), want, seed)

    def test_the_tavern_grows_to_the_right_and_the_scene_widens_with_the_queue(self):
        today = dt.date.fromtimestamp(T0).isoformat()
        slot = {"group": "demo-app", "cap": 2, "taken": 2, "holders": []}
        agents = [self.hand("H1", 1)]
        def lay_of(n):
            queue = [self.queue_item("C%02d" % i, "slot", today, slot=slot) for i in range(n)]
            return shipmate.ship_layout(shipmate.ship_scene(self.snap(agents, queue=queue)))
        bare = lay_of(0)
        lays = {n: lay_of(n) for n in (1, 3, 15)}
        for n, lay in lays.items():
            self.assertEqual(lay["x0"], bare["x0"], n)                    # nothing is reserved west of the ship
            self.assertTrue(all(x > lay["plank"][1][0] for x, _f, _p, _u in lay["pier"].values()), n)
            self.assertEqual(len(lay["pier"]), min(n, shipmate.TABLE_SEATS * shipmate.MAX_TABLES), n)
        for n, lay in lays.items():                                       # right next to the gangplank's foot, not far down the pier
            self.assertLess(lay["pier"]["C00"][0] - lay["plank"][1][0], 40, n)
        # the same queue seats the same way: first in, first at the anchor
        firsts = {n: lay["pier"]["C00"][0] for n, lay in lays.items()}
        self.assertEqual(len(set(firsts.values())), 1, firsts)
        # compact: a short queue never widens the scene, and the tavern's reach follows how many wait
        w0 = bare["width"]
        self.assertEqual(lays[1]["width"], w0)
        self.assertEqual(lays[3]["width"], w0)
        reach = {n: max(x for x, _f, _p, _u in lay["pier"].values()) for n, lay in lays.items()}
        self.assertLessEqual(reach[1], reach[3])
        self.assertLess(reach[3], reach[15])
        self.assertLessEqual(lays[1]["width"], lays[3]["width"])
        self.assertLessEqual(lays[3]["width"], lays[15]["width"])
        self.assertLessEqual(lays[15]["width"] - w0, 2 * shipmate.TABLE_W * shipmate.MAX_TABLES)

    def tavern_scene(self, now, holders=("H1", "H2")):
        today = dt.date.fromtimestamp(now).isoformat()
        slot = {"group": "demo-app", "cap": 2, "taken": 2, "holders": list(holders)}
        lanes = {"group": "", "cap": 6, "taken": 6, "holders": []}
        queue = ([self.queue_item("C%d" % i, "slot", today, slot=slot) for i in range(4)]
                 + [self.queue_item("L%d" % i, "lane", today, slot=lanes) for i in range(2)])
        agents = [self.hand("H1", 1), self.hand("H2", 1)]
        return shipmate.ship_scene(self.snap(agents, queue=queue, projects=["demo-app"], now=now))

    def test_the_tavern_is_a_building_in_the_colour_of_what_it_waits_for(self):
        self.assertEqual(shipmate.awning_colors({"group": "demo-app"}, {"demo-app": "#16a4a4"}), ("#16a4a4", "#f6f1e3"))
        self.assertEqual(shipmate.awning_colors({"group": ""}, {"demo-app": "#16a4a4"})[0], "#22345e")   # the home's lanes: navy
        noon = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))
        for now, night in ((noon, False), (noon + 10 * 3600, True)):
            sc = self.tavern_scene(now)
            lay = shipmate.ship_layout(sc)
            self.assertEqual([u["slot"]["group"] for _a, _b, u in lay["rooms"]], ["demo-app", ""])
            lights = []
            drawn = shipmate.draw_tavern(sc, lay, lights, night)
            self.assertIn('fill="%s"' % sc["colors"]["demo-app"], drawn)
            self.assertIn('fill="#22345e"', drawn)
            windows = sum(len(range(a + 2 + 14, b - 16, 70)) for a, b, _u in lay["rooms"])
            self.assertGreater(windows, 1)
            self.assertEqual(len(lights), windows if night else 0)                 # lit from dusk to dawn
            # it stands under the waterline, so the hull and the hold above it stay in view
            ys = [int(m) for m in re.findall(r"M\d+ (\d+)h", drawn)]
            self.assertGreater(min(ys), shipmate.WL - 6)

    def test_the_key_board_hangs_a_hook_per_place_and_names_who_has_the_keys(self):
        sc = self.tavern_scene(T0)
        lay = shipmate.ship_layout(sc)
        (_a, x1, slot_u), (_c, y1, lane_u) = lay["rooms"]
        board = shipmate.key_board(x1, slot_u)
        hooks = re.search(r'<path fill="#e0b552" d="([^"]*)"', board).group(1)
        self.assertEqual(hooks.count("h1v1h-1z"), 2)                                  # one hook per build slot
        self.assertIn('data-ids="H1 H2 C0 C1 C2 C3"', board)                           # who holds a key, then who waits
        self.assertIn("build slots for demo-app: 2 of 2 keys are out, with H1, H2; 4 wait for any of them to come back", board)
        lanes = shipmate.key_board(y1, lane_u)
        hooks = re.search(r'<path fill="#e0b552" d="([^"]*)"', lanes).group(1)
        self.assertEqual(hooks.count("h1v1h-1z"), 6)
        self.assertIn("home lanes: 6 of 6 in use; 2 wait for any lane to come free", lanes)
        self.assertIn('data-ids="L0 L1"', lanes)
        drawn = shipmate.render_ship(sc)
        self.assertEqual(drawn.count('class="sm-keys"'), 2)
        self.assertIn('tabindex="0" role="img" aria-label="build slots for demo-app', drawn)

    def test_the_pier_lamps_keep_clear_and_light_only_at_night(self):
        noon = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))
        sc = self.tavern_scene(noon)
        lay = shipmate.ship_layout(sc)
        xs = shipmate.pier_lamp_xs(lay)
        self.assertTrue(xs)
        for x in xs:
            for a, b, _u in lay["rooms"]:
                self.assertFalse(a - shipmate.LAMP_CLEAR <= x <= b + shipmate.LAMP_CLEAR, x)   # the tavern has its own lantern
            self.assertFalse(lay["x0"] + 10 <= x <= lay["x0"] + 100, x)                         # the gangplank's foot
            self.assertLess(x, lay["dock_x"] - shipmate.LAMP_CLEAR)                             # the skiffs and the crates
        for now, lit in ((noon, 0), (noon + 10 * 3600, len(xs))):
            mid, lights = [], []
            posts = shipmate.draw_pier_lamps(lay, mid, lights, shipmate.is_night(now))
            self.assertEqual(sum('r="16"' in x for x in lights), lit)
            self.assertEqual(len(mid), 1 if lit else 0)                               # their reflection between the piles
            self.assertIn('fill="#f2c35a"' if lit else 'fill="#4a5a6a"', posts)

    def test_the_scenery_lives_without_saying_anything_about_the_work(self):
        noon = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))
        day = shipmate.render_ship(self.tavern_scene(noon))
        night = shipmate.render_ship(self.tavern_scene(noon + 10 * 3600))
        for drawn in (day, night):
            self.assertNotIn("sm-drift", drawn)                                        # the clouds step, they no longer slide
            self.assertEqual(drawn.count('class="sm-cloud"'), len(shipmate.CLOUD_LAYERS))
            self.assertEqual(drawn.count('class="sm-ripple '), 3)
        self.assertEqual(day.count('class="sm-gull"'), len(shipmate.GULLS))             # gulls by day only
        self.assertNotIn("sm-gull", night)
        self.assertEqual(night.count('class="sm-twinkle"'), len(shipmate.TWINKLE))     # the stars twinkle at night only
        self.assertNotIn("sm-twinkle", day)
        # foam, the hull's reflection and the glitter by day; the pier lamps' reflection too at night
        self.assertEqual(day.count('class="sm-amb"'), 3)
        self.assertEqual(night.count('class="sm-amb"'), 4)
        # the sea's state still says the load, and the little waves follow it
        sizes = [len("".join(shipmate.ripples(998, sea, 0))) for sea in ("calm", "choppy", "rough")]
        self.assertLess(sizes[0], sizes[1])
        self.assertLess(sizes[1], sizes[2])
        # a loop longer than the refresh goes on from the clock, so a new drawing does not restart it
        for period, _y, _east in shipmate.GULLS:
            self.assertGreater(period, shipmate.REFRESH_SECONDS)
            a = re.findall(r"--round:%ds;[^\"]*--late:-([\d.]+)s" % period, shipmate.gulls(998, noon)[0] + shipmate.gulls(998, noon)[1])
            b = re.findall(r"--round:%ds;[^\"]*--late:-([\d.]+)s" % period, "".join(shipmate.gulls(998, noon + 30)))
            self.assertAlmostEqual((float(b[0]) - float(a[0])) % period, 30 % period, places=0)
        for c in shipmate.sky_clouds(998, 0, noon):
            span = int(re.search(r"--span:-(\d+)px", c).group(1))
            self.assertEqual(span, 998 + shipmate.CLOUD_SPAN)
            self.assertIn("--px:%d;" % span, c)                                       # one art pixel a step

    def test_the_pier_gives_every_queued_hand_its_own_spot(self):
        today = dt.date.fromtimestamp(T0).isoformat()
        slot = {"group": "demo-app", "cap": 2, "taken": 2, "holders": ["H1", "H2"]}
        queue = ([self.queue_item("C%02d" % i, "slot", today, slot=slot) for i in range(16)]
                 + [self.queue_item("D%d" % i, "slot", "2000-01-01", slot=slot) for i in range(3)]
                 + [self.queue_item("R%d" % i, "ready") for i in range(3)]
                 + [self.queue_item("T1", "task", deps=["H1"]), self.queue_item("T2", "task", deps=["H1"]),
                    self.queue_item("T3", "task", deps=["H2"])]
                 + [self.queue_item("N%d" % i, "date") for i in range(2)])
        agents = [self.hand("H1", 1), self.hand("H2", 3)]
        sc = shipmate.ship_scene(self.snap(agents, queue=queue))
        lay = shipmate.ship_layout(sc)
        bare = shipmate.ship_layout(shipmate.ship_scene(self.snap(agents)))
        spots = lay["pier"]
        self.assertEqual(lay["x0"], bare["x0"])                          # the tavern reserves nothing west of the ship
        plank_foot = lay["plank"][1][0]
        tavern = [k for k in spots if k[0] in "CD"]
        self.assertEqual(len(tavern), shipmate.TABLE_SEATS * shipmate.MAX_TABLES + 3)
        self.assertTrue(all(spots[k][0] > plank_foot for k in tavern))        # the tavern is ashore, east of the gangplank
        self.assertEqual([len(h) for _x, h in lay["pier_more"]], [16 - shipmate.TABLE_SEATS * shipmate.MAX_TABLES])
        self.assertEqual({spots[k][2] for k in tavern}, {"cards", "cards-l", "stand", "drink"})
        # nobody stands on anybody: the same row never holds two sprites over the same pixels
        rows = {}
        for k, (x, feet, pose, _u) in spots.items():
            rows.setdefault(feet, []).append((x, x + len(shipmate.POSES[pose][0]), k))
        for row in rows.values():
            row.sort()
            for (a0, a1, ka), (b0, b1, kb) in zip(row, row[1:]):
                self.assertLessEqual(a1, b0 + 2, (ka, kb))              # a seat's edge may touch its table's neighbour
        # a talker stands under the hand it talks to, or as close as the others allow
        self.assertLess(abs(spots["T3"][0] - lay["hands"]["H2"][0]), 160)     # the tavern, anchored first, may push it past itself
        self.assertLess(abs(spots["T1"][0] - lay["hands"]["H1"][0]), 160)    # with a full tavern at the gangplank it moves past it
        for k, (x, feet, _p, _u) in spots.items():
            self.assertTrue(0 < x < lay["width"] and feet == shipmate.PIER_FEET - (3 if spots[k][2] == "stand" and k[0] == "C" else 0), k)
        self.assertGreater(min(v[0] for v in lay["scouts"].values()) if lay["scouts"] else lay["crates_x"],
                           max(x for x, _f, _p, _u in spots.values()))     # the moored scouts and crates stay past the queue
        svg = shipmate.render_ship(sc)
        self.assertEqual(svg.count('class="sm-talk"'), 3)
        self.assertEqual(svg.count('class="sm-talkline"'), 3)
        self.assertEqual(svg.count('class="sm-deal"'), 1)
        self.assertIn('class="sm-tag is-hover" data-for="C00"', svg)      # the tavern's names show on hover only
        self.assertIn('<g class="sm-tag" data-for="T1"', svg)
        self.assertIn('class="sm-hand pose-hammock" data-id="N0" data-stage="date"', svg)
        self.assertFalse(any(k[0] == "N" for k in spots))                 # the dated holds sleep below deck
        self.assertIn(">+%d</text>" % (16 - shipmate.TABLE_SEATS * shipmate.MAX_TABLES), svg)
        self.assertIn("SLOT demo-app · 2/2 taken · 19 waiting", svg)
        self.assertIn("READY · boards next", svg)
        self.assertIn("QUARTERS · asleep until a date", svg)

    def test_the_dated_holds_sleep_in_hammocks_in_the_quarters_below_deck(self):
        agents = [self.hand("H1", 1), self.hand("FISH", 4)]
        dates = ["2040-01-05", "2040-01-02", "2040-01-09", "2040-01-02"]
        queue = [self.queue_item("N%d" % i, "date", until="held until " + d, day=d) for i, d in enumerate(dates)]
        sc = shipmate.ship_scene(self.snap(agents, queue=queue))
        self.assertEqual([q["id"] for q in sc["quarters"]], ["N1", "N3", "N0", "N2"])      # the earliest date first, then by id
        self.assertEqual(sc["pier"], [])
        bare = shipmate.ship_layout(shipmate.ship_scene(self.snap(agents)))
        self.assertIsNone(bare["quarters"])                               # nobody asleep: no quarters, the hold as it was
        lay = shipmate.ship_layout(sc)
        qx0, qx1 = lay["quarters"]["box"]
        hx0, hx1 = lay["hold"]
        self.assertEqual(hx0, bare["hold"][0])
        self.assertLess(hx1, qx0)                                         # the quarters stand forward of the hold
        self.assertGreaterEqual(hx1 - hx0, 14 * len(sc["barrels"]) + shipmate.HOLD_KEEPS)
        self.assertLessEqual(qx1, lay["x0"] + lay["length"] - shipmate.QUARTERS_BOW)
        self.assertEqual(lay["length"], bare["length"])                   # a few hammocks fit without a longer ship
        beds = lay["quarters"]["beds"]
        self.assertEqual(list(beds), ["N1", "N3", "N0", "N2"])
        lows = sorted({low for _x, low in beds.values()})
        self.assertEqual(len(lows), 2)                                    # two tiers
        for low in lows:
            xs = sorted(x for x, l in beds.values() if l == low)
            self.assertTrue(all(b - a >= shipmate.HAMMOCK_W for a, b in zip(xs, xs[1:])), xs)
        self.assertTrue(all(qx0 < x and x + shipmate.HAMMOCK_W <= qx1 for x, _l in beds.values()))
        svg = shipmate.render_ship(sc)
        self.assertEqual(svg.count('class="sm-hand pose-hammock"'), 4)
        self.assertIn('<g class="sm-tag is-hover" data-for="N1"', svg)    # the id on hover, found first
        self.assertIn('<text x="%d" y="' % (beds["N1"][0] + shipmate.HAMMOCK_W // 2 - 1), svg)
        self.assertIn(">02 Jan</text>", svg)                              # the date stays on
        self.assertIn("QUARTERS · asleep until a date", svg)
        # the fishing line of a hand at the rail hangs in front of the quarters
        self.assertLess(svg.index("pose-hammock"), svg.index('data-id="FISH"'))
        self.assertRegex(shipmate.stage_chips(sc), r'data-stage="date"[^>]*><svg class="sm-icon" viewBox="0 0 %d 19"'
                         r'.*?<b>4</b>' % (shipmate.HAMMOCK_W + 2))

    def test_the_quarters_grow_the_forecastle_only_when_the_hold_cannot_spare_the_room(self):
        agents = [self.hand("H1", 1)]
        bare = shipmate.ship_layout(shipmate.ship_scene(self.snap(agents)))
        queue = [self.queue_item("N%02d" % i, "date", day="2040-01-%02d" % (i + 1)) for i in range(20)]
        sc = shipmate.ship_scene(self.snap(agents, queue=queue))
        lay = shipmate.ship_layout(sc)
        qr = lay["quarters"]
        self.assertEqual(len(qr["beds"]), shipmate.MAX_HAMMOCKS)
        self.assertEqual([q["id"] for q in qr["more"]], ["N%02d" % i for i in range(shipmate.MAX_HAMMOCKS, 20)])
        self.assertGreater(lay["length"], bare["length"])
        self.assertEqual(lay["fc_x"], bare["fc_x"])                       # the forecastle grows toward the bow
        self.assertEqual(lay["hold"][1] - lay["hold"][0], max(150, 14 * len(sc["barrels"]) + shipmate.HOLD_KEEPS))
        self.assertLessEqual(qr["box"][1], lay["x0"] + lay["length"] - shipmate.QUARTERS_BOW)
        svg = shipmate.render_ship(sc)
        self.assertIn(">+%d</text>" % (20 - shipmate.MAX_HAMMOCKS), svg)
        shipmate.LANG = "pt-BR"
        try:
            sc = shipmate.ship_scene(self.snap(agents, queue=queue[:1]))
            svg = shipmate.render_ship(sc)
            self.assertIn("ALOJAMENTO · dormem até a data", svg)
            self.assertIn(">01 jan</text>", svg)
            self.assertIn("dormindo numa rede no alojamento abaixo do convés", svg)
        finally:
            shipmate.LANG = "en"

    def test_every_hand_is_painted_in_one_place(self):
        calls, sleepers = [], []
        real, real_hammock = shipmate.hand_sprite, shipmate.hammock_sprite

        def spy(p, h, x, feet, pose, upto=None):
            calls.append(h["id"])
            return real(p, h, x, feet, pose, upto)

        def spy_hammock(p, h, x, low, top):
            sleepers.append(h["id"])
            return real_hammock(p, h, x, low, top)
        shipmate.hand_sprite, shipmate.hammock_sprite = spy, spy_hammock
        try:
            snap = shipmate.demo_snapshot(T0)
            sc = shipmate.ship_scene(snap)
            shipmate.render_ship(sc)
        finally:
            shipmate.hand_sprite, shipmate.hammock_sprite = real, real_hammock
        crowd = {x["id"] for x in shipmate.ship_layout(sc)["crowd"]}       # faces in the door, not hands
        asleep = {x["id"] for x in sc["quarters"]}                         # in a hammock, painted by hammock_sprite
        drawn = [x["id"] for x in sc["hands"] + sc["scouts"] + sc["queue"] if x["id"] not in crowd | asleep]
        self.assertEqual(sorted(set(calls)), sorted(drawn))
        self.assertEqual(sorted(sleepers), sorted(asleep))
        self.assertTrue(asleep)

    def test_motion_respects_reduced_motion(self):
        css = re.sub(r"/\*.*?\*/", "", shipmate.asset("shipmate.css"), flags=re.S)
        rules = re.findall(r"^([^{}@/][^{}]*)\{([^}]*)\}", css, re.M)
        moving = [sel.strip() for sel, body in rules if re.search(r"\banimation(-duration)?\s*:", body)]
        self.assertGreater(len(moving), 10)
        self.assertEqual([m for m in moving if not m.startswith(".motion-all ")], [])   # every loop sits behind the switch
        page = shipmate.asset("shipmate.html")
        self.assertIn('matchMedia("(prefers-reduced-motion: reduce)")', page)            # which starts off for that reader
        self.assertIn("data-motion", page)
        self.assertRegex(page, r'if \(mode === "off" \|\| document\.hidden')        # and then no change plays either

    def test_stopping_the_motion_goes_straight_to_the_final_state(self):
        page = shipmate.asset("shipmate.html")
        stop = page.split("function stop() {")[1].split("\n    }")[0]
        self.assertIn("undo.splice(0)", stop)
        self.assertIn("release()", stop)                                        # the queue of changes is let go
        self.assertIn('undo.push(function () { layer.remove(); });', page)     # no walker is left standing
        self.assertIn("Promise.race([Promise.all(runs)", page)                 # a handover waiting on its partner cannot hold the ship
        # the scroll to the log follows the switch and the browser's own request
        self.assertNotIn('behavior: "smooth" }', page)
        self.assertIn('motion.smooth() ? "smooth" : "auto"', page)
        smooth = page.split("function smooth() {")[1].split("\n    }")[0]
        self.assertIn("prefers-reduced-motion", smooth)
        self.assertIn('mode !== "off"', smooth)

    def test_the_zoom_tools_are_in_and_out_only(self):
        page = shipmate.asset("shipmate.html")
        self.assertEqual(re.findall(r'data-zoom="(\w+)"', page), ["in", "out"])
        self.assertNotIn("fit all", page)
        self.assertNotIn("Frame the crew", page)

    def test_signs_put_away_stay_away_at_every_zoom(self):
        # the far view shows every tag a hand is not hiding, signs among them; the switch must outrank it
        css = re.sub(r"/\*.*?\*/", "", shipmate.asset("shipmate.css"), flags=re.S)
        rules = [(sel.strip(), body) for sels, body in re.findall(r"^([^{}@/][^{}]*)\{([^}]*)\}", css, re.M)
                 for sel in sels.split(",")]

        def weight(sel):          # class, attribute and pseudo-class selectors; these rules use no id or element
            return len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!not\()[\w-]+", sel))
        hide = [w for sel, body in rules if ".no-signs" in sel for w in [weight(sel)] if "display: none" in body]
        self.assertEqual(len(hide), 1)
        shows = [sel for sel, body in rules if re.search(r"display:\s*(?!none)\w", body)
                 and re.search(r"\.sm-(tag|sign)\b", sel.split()[-1])]
        self.assertTrue(any("is-far" in sel for sel in shows))
        for sel in shows:
            self.assertGreater(hide[0], weight(sel), sel)
        svg = shipmate.render_ship(shipmate.ship_scene(shipmate.demo_snapshot(1_700_000_000)))
        signs = re.findall(r'class="([^"]*\bsm-sign\b[^"]*)"', svg)
        self.assertTrue(signs)
        self.assertEqual([c for c in signs if "sm-tag" not in c.split()], [])   # the rule's selector meets every sign

    def test_favicon_is_the_anchor_in_both_formats(self):
        self.assertEqual({len(r) for r in shipmate.FAVICON}, {16})
        self.assertEqual(len(shipmate.FAVICON), 16)
        uri = shipmate.favicon_svg()
        self.assertTrue(uri.startswith("data:image/svg+xml,<svg "))
        for bare in ('"', "#", "http:"):
            self.assertNotIn(bare, uri)                                  # safe in an attribute, no fragment, no scheme
        png = shipmate.favicon_png()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (32, 32))
        self.assertIn('href="{{FAVICON}}"', shipmate.asset("shipmate.html"))

    def test_a_refresh_with_nothing_new_keeps_the_drawing_and_its_loops(self):
        page = shipmate.asset("shipmate.html")
        self.assertIn("b.ship === shipDrawn", page)
        self.assertIn("if (!same) document.getElementById(\"b-ship\").innerHTML = b.ship;", page)
        self.assertIn("if (!same) motion.play(b.events, before);", page)
        sc = shipmate.ship_scene(shipmate.demo_snapshot(1_700_000_000))
        self.assertEqual(shipmate.render_ship(sc), shipmate.render_ship(sc))   # the same scene draws the same markup

    def test_demo_server_serves_the_page_ship_and_details(self):
        panel = shipmate.DemoPanel(0)
        srv = shipmate.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
        panel.cfg.port = srv.server_address[1]
        srv.RequestHandlerClass = shipmate.make_handler(panel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.1", panel.cfg.port, timeout=30)
                c.request("GET", path, headers={"Host": "127.0.0.1:%d" % panel.cfg.port})
                r = c.getresponse()
                body = r.read().decode()
                c.close()
                return r.status, body
            st, page = get("/")
            self.assertEqual(st, 200)
            for h in ("Demo: a made-up home", 'data-view="ship"', 'id="sm-view-ship"', "The ship", 'class="sm-chip',
                      'class="sm-hand', 'data-zoom="in"', "Work by phase", 'class="sm-logcard"', "data-signs",
                      '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,', 'href="/favicon.png"'):
                self.assertIn(h, page)
            self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))
            c = http.client.HTTPConnection("127.0.0.1", panel.cfg.port, timeout=30)
            c.request("GET", "/favicon.png", headers={"Host": "127.0.0.1:%d" % panel.cfg.port})
            r = c.getresponse()
            self.assertEqual((r.status, r.getheader("Content-Type"), r.read()), (200, "image/png", shipmate.favicon_png()))
            c.close()
            st, ship = get("/ship")
            self.assertEqual(set(json.loads(ship)), {"ship", "counts", "projects", "accounts", "stages", "log", "events"})
            self.assertEqual(json.loads(ship)["events"], [])                                 # nothing to compare with
            now = time.time()
            other = shipmate.ship_print(shipmate.ship_scene(shipmate.demo_snapshot(now, moved=int(now // 30) % 2 == 0)))
            other["at"] = int(now) - 5
            st, moved = get("/ship?prev=" + urllib.parse.quote(json.dumps(other)))
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(moved)["events"])                                    # the demo moved since
            self.assertEqual(json.loads(get("/ship?prev=%7Bnot%20json")[1])["events"], [])
            st, det = get("/detail?id=DEMO-RATE")
            self.assertEqual(st, 200)
            self.assertIn("per user or per key?", det)
            st, bill = get("/detail?id=DEMO-Q-BILL")
            self.assertEqual(st, 200)
            self.assertIn("Released DEMO-Q-BILL with plan B", bill)
            self.assertIn('<b>B)</b> <span>per key</span>', det)
            c = http.client.HTTPConnection("127.0.0.1", panel.cfg.port, timeout=30)
            c.request("POST", "/answer", body=urllib.parse.urlencode({"token": panel.token, "request": "d1", "task": "DEMO-RATE",
                                                                      "ref": "limits", "option": "B", "text": "keys"}),
                      headers={"Host": "127.0.0.1:%d" % panel.cfg.port, "Content-Type": "application/x-www-form-urlencoded"})
            res = json.loads(c.getresponse().read().decode())
            c.close()
            self.assertEqual((res["ok"], res["state"]), (True, "sent"))
            self.assertIn("nothing reached firstmate", res["message"])
            st, det = get("/detail?id=DEMO-RATE")
            self.assertIn("B) per key\nkeys", det)
            self.assertIn("sm-ans--sent", json.loads(get("/ship")[1])["log"])
            self.assertEqual(get("/report?id=DEMO-RATE&f=md")[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()

class TestDeckTools(unittest.TestCase):
    """On deck the tools take turns by when each hand reached the deck (deck_tools)."""

    def hand(self, hid, arrived, last=T0 - 10, crew="working", place="deck", off=False, line=0):
        return {"id": hid, "place": place, "arrived": arrived, "last": last, "crew": crew, "off": off, "line": line}

    def test_first_hammer_then_mop_then_broom_by_arrival(self):
        hands = [self.hand("D", T0 - 100), self.hand("A", T0 - 400), self.hand("C", T0 - 200), self.hand("B", T0 - 300),
                 self.hand("X", None), self.hand("N", T0 - 900, place="nest"), self.hand("L", T0 - 999, line=1)]
        shipmate.deck_tools(hands, T0)
        tools = {h["id"]: h.get("tool") for h in hands}
        self.assertEqual(tools, {"A": "hammer", "B": "mop", "C": "broom", "D": "hammer", "X": "mop", "N": None, "L": None})
        # a later arrival takes the next tool and leaves the others theirs
        hands.append(self.hand("E", T0 - 50))
        shipmate.deck_tools(hands, T0)
        self.assertEqual({h["id"]: h.get("tool") for h in hands if h["id"] in "ABCDE"},
                         {"A": "hammer", "B": "mop", "C": "broom", "D": "hammer", "E": "mop"})

    def test_the_pace_follows_the_transcript_and_stops_when_done(self):
        hands = [self.hand("A", 1, last=T0 - 30), self.hand("B", 2, last=T0 - 600), self.hand("C", 3, crew="done"),
                 self.hand("D", 4, crew="paused"), self.hand("E", 5, off=True), self.hand("F", 6, last=None)]
        shipmate.deck_tools(hands, T0)
        self.assertEqual([h["pace"] for h in hands], ["busy", "slow", "still", "still", "still", "slow"])

    def test_a_tool_on_deck_is_drawn_with_its_second_frame(self):
        snap = shipmate.demo_snapshot(T0)
        sc = shipmate.ship_scene(snap)
        tools = {h["id"]: (h["tool"], h["pace"]) for h in sc["hands"] if h.get("tool")}
        self.assertEqual(tools, {"DEMO-AUTH": ("hammer", "busy"), "DEMO-NAV": ("mop", "slow"), "DEMO-SYNC": ("broom", "busy")})
        svg = shipmate.render_ship(sc)
        for hid, (tool, pace) in tools.items():
            at = svg.find('<g class="sm-hand pose-%s tool-%s pace-%s" data-id="%s"' % (tool, tool, pace, hid))
            self.assertGreaterEqual(at, 0, hid)
            nxt = svg.find('class="sm-hand ', at + 10)
            self.assertIn('<g class="sm-alt">', svg[at:nxt if nxt > 0 else len(svg)], hid)
        self.assertEqual(shipmate.PT_BR.get("on deck with a mop"), "no convés com o esfregão")
        lay = shipmate.ship_layout(sc)
        bodies = sorted((x + shipmate.pose_left(p), f) for hid, (x, f, p, _u) in lay["hands"].items() if hid in tools)
        self.assertEqual(len({b for b in bodies}), 3)                                 # each body on its own spot


class TestMotion(unittest.TestCase):
    """What changed between two readings of the ship, which the page plays once (ship_events)."""

    def print_(self, at=T0, hands=None, queue=(), checks=None, fish=None, context=50.0, compaction=None, night=False):
        return {"at": at, "hands": dict(hands or {}), "queue": queue if isinstance(queue, dict) else {q: "pier" for q in queue},
                "checks": dict(checks or {}), "fish": dict(fish or {}),
                "context": context, "compaction": compaction, "night": night}

    def kinds(self, prev, cur):
        return [(e["kind"], e.get("id")) for e in shipmate.ship_events(prev, cur)]

    def test_nothing_plays_without_a_recent_earlier_reading(self):
        cur = self.print_(hands={"A": "deck"})
        self.assertEqual(shipmate.ship_events(None, cur), [])
        self.assertEqual(shipmate.ship_events({}, cur), [])
        old = self.print_(at=T0 - shipmate.PRINT_FRESH - 1, hands={"A": "nest"})
        self.assertEqual(shipmate.ship_events(old, cur), [])                           # old news
        self.assertEqual(shipmate.ship_events(self.print_(at=T0 + 5, hands={"A": "nest"}), cur), [])   # from later
        self.assertEqual(self.kinds(self.print_(at=T0 - 30, hands={"A": "nest"}), cur), [("move", "A")])
        junk = {"at": T0 - 30, "hands": ["A"], "queue": "A", "checks": 3, "context": "x", "night": "yes"}
        self.assertEqual(shipmate.ship_events(junk, cur), [])                          # a print of the wrong shape plays nothing

    def test_a_hand_walks_when_its_place_changes(self):
        prev = self.print_(at=T0 - 30, hands={"A": "nest", "B": "deck", "C": "cabin", "D": "rail", "S": "out"},
                           queue={"Q": "pier", "R": "pier", "C2": "cabin"})
        cur = self.print_(hands={"A": "deck", "B": "cabin", "C": "bow", "D": "rail", "Q": "nest", "S": "back", "N": "deck"},
                          queue=["R"])
        ev = shipmate.ship_events(prev, cur)
        self.assertEqual(ev, [{"kind": "board", "id": "Q", "from": "pier", "to": "nest"},
                              {"kind": "move", "id": "A", "from": "nest", "to": "deck", "look": False, "catch": False},
                              {"kind": "move", "id": "B", "from": "deck", "to": "cabin", "look": False, "catch": False},
                              {"kind": "move", "id": "C", "from": "cabin", "to": "bow", "look": False, "catch": False}])
        held = self.print_(at=T0 - 30, queue={"H": "cabin"})
        self.assertEqual(shipmate.ship_events(held, self.print_(hands={"H": "nest"})),
                         [{"kind": "board", "id": "H", "from": "cabin", "to": "nest"}])   # out of the cabin, not the pier
        # a task that appears aboard without having been queued, and a scout's boat, do not walk
        many = self.print_(at=T0 - 30, hands={"H%d" % i: "nest" for i in range(9)})
        moved = self.print_(hands={"H%d" % i: "deck" for i in range(9)})
        self.assertEqual(len(shipmate.ship_events(many, moved)), shipmate.EVENT_LIMITS["walk"])

    def test_the_lighthouse_flashes_the_answer_as_the_watch_walks_off(self):
        prev = self.print_(at=T0 - 30, hands={"A": "watch", "B": "watch", "C": "watch", "D": "bow"},
                           checks={"A": "pending", "B": "pending", "C": "pending"})
        passed = self.print_(hands={"A": "rail", "B": "watch", "C": "watch", "D": "watch"},
                             checks={"A": "pass", "B": "pending", "C": "pending", "D": "pending"}, fish={"A": "1"})
        ev = shipmate.ship_events(prev, passed)
        self.assertEqual(ev, [{"kind": "move", "id": "A", "from": "watch", "to": "rail", "look": True, "catch": True},
                              {"kind": "move", "id": "D", "from": "bow", "to": "watch", "look": False, "catch": False},
                              {"kind": "beacon", "id": "A", "result": "pass"}])
        failed = self.print_(hands={"A": "watch", "B": "deck", "C": "deck", "D": "bow"},
                             checks={"A": "pending", "B": "fail", "C": "fail"})
        self.assertEqual([(e["kind"], e["id"], e.get("result")) for e in shipmate.ship_events(prev, failed)],
                         [("move", "B", None), ("move", "C", None), ("beacon", "B", "fail")])    # one light, one answer at a time
        # still running, gone to the cabin before an answer, or no answer from gh: no flash
        quiet = self.print_(hands={"A": "watch", "B": "cabin", "C": "bow", "D": "bow"}, checks={"A": "pending", "B": "pending"})
        self.assertEqual([e["kind"] for e in shipmate.ship_events(prev, quiet)], ["move", "move"])
        # back from the cabin to the rail: no new first fish
        back = shipmate.ship_events(self.print_(at=T0 - 30, hands={"A": "cabin"}), self.print_(hands={"A": "rail"}, fish={"A": "3"}))
        self.assertEqual(back, [{"kind": "move", "id": "A", "from": "cabin", "to": "rail", "look": False, "catch": False}])

    def test_one_more_fish_for_each_hour_waiting_for_the_merge(self):
        prev = self.print_(at=T0 - 30, hands={"A": "rail", "B": "rail", "C": "rail", "D": "deck"}, fish={"A": "1", "B": "2", "C": "4"})
        cur = self.print_(hands={"A": "rail", "B": "rail", "C": "rail", "D": "rail"}, fish={"A": "2", "B": "2", "C": "4", "D": "1"})
        self.assertEqual([(e["kind"], e["id"], e.get("n")) for e in shipmate.ship_events(prev, cur)],
                         [("move", "D", None), ("fish", "A", 2)])
        junk = self.print_(at=T0 - 30, hands={"A": "rail"}, fish={"A": "x"})
        self.assertEqual(shipmate.ship_events(junk, self.print_(hands={"A": "rail"}, fish={"A": "2"})), [])
        # the scene: a fish when it got to the rail and one more each hour, up to four
        def fisher(waited):
            return {"now": T0, "queue": [], "quota": {"lanes": []}, "firstmate": None, "machine": None, "agents": [
                {"id": "A", "kind": "ship", "project": "p", "phases": shipmate.SHIP_PHASES, "phase": 4, "crew": "done",
                 "decisions": [], "phase_since": T0 - waited, "last_activity": T0 - 60}]}
        for waited, n in ((0, 1), (3599, 1), (3600, 2), (2 * 3600 + 5, 3), (9 * 3600, 4)):
            sc = shipmate.ship_scene(fisher(waited))
            self.assertEqual(sc["hands"][0]["fish"], n, waited)
            self.assertEqual(shipmate.ship_print(sc)["fish"], {"A": str(n)})
        drawn = shipmate.render_ship(sc)
        self.assertEqual(drawn.count('class="sm-bucket__fish"'), 4)
        self.assertIn("<title>A: 4 fish, waiting for the merge for 9h 00m</title>", drawn)
        self.assertIn("<title>A: one fish, waiting for the merge for 10m</title>", shipmate.render_ship(shipmate.ship_scene(fisher(600))))

    def test_the_pump_works_only_for_a_new_compaction(self):
        prev = self.print_(at=T0 - 30, context=72.0, compaction=T0 - 7 * 3600)
        fresh = self.print_(context=19.0, compaction=T0 - 20)
        self.assertEqual(shipmate.ship_events(prev, fresh), [{"kind": "pump", "from": 72.0, "to": 19.0}])
        self.assertEqual(shipmate.ship_events(prev, self.print_(context=80.0, compaction=T0 - 20)), [])       # it grew
        self.assertEqual(shipmate.ship_events(prev, self.print_(context=19.0, compaction=T0 - 7 * 3600)), [])  # same one
        stale = self.print_(context=19.0, compaction=T0 - shipmate.PRINT_FRESH - 60)
        self.assertEqual(shipmate.ship_events(self.print_(at=T0 - 30, context=72.0), stale), [])   # long ago, newly seen
        self.assertEqual(shipmate.ship_events(self.print_(at=T0 - 30, context=None), fresh), [])

    def test_the_lamps_are_lit_when_night_falls(self):
        self.assertEqual(self.kinds(self.print_(at=T0 - 30, night=False), self.print_(night=True)), [("lights", None)])
        self.assertEqual(self.kinds(self.print_(at=T0 - 30, night=True), self.print_(night=True)), [])
        self.assertEqual(self.kinds(self.print_(at=T0 - 30, night=True), self.print_(night=False)), [])
        prev = self.print_(at=T0 - 30)
        del prev["night"]
        self.assertEqual(self.kinds(prev, self.print_(night=True)), [])

    def test_the_night_falls_and_lifts_in_steps(self):
        def at(h, m):
            return time.mktime((2026, 11, 13, h, m, 0, 0, 0, -1))
        steps = [(12, 0, 0), (17, 30, 0), (17, 35, 1), (17, 50, 2), (18, 0, 3), (18, 25, 6), (23, 0, 6),
                 (5, 30, 6), (5, 45, 5), (6, 0, 3), (6, 29, 1), (6, 30, 0)]
        self.assertEqual([(h, m, shipmate.twilight(at(h, m))) for h, m, _k in steps], steps)
        self.assertEqual([shipmate.is_night(at(h, m)) for h, m in ((17, 59), (18, 0), (5, 59), (6, 0))],
                         [False, True, True, False])                                   # the lamps keep their hours
        noon = shipmate.render_ship(shipmate.ship_scene(shipmate.demo_snapshot(at(12, 0))))
        dusk = shipmate.render_ship(shipmate.ship_scene(shipmate.demo_snapshot(at(17, 45))))
        night = shipmate.render_ship(shipmate.ship_scene(shipmate.demo_snapshot(at(22, 0))))
        self.assertNotIn("sm-shade", noon)
        self.assertIn('class="sm-shade" opacity="0.127"', dusk)
        self.assertIn('class="sm-shade" opacity="0.380"', night)
        self.assertNotIn("is-night", dusk)
        self.assertIn("is-night", night)

    def test_the_demo_moves_and_each_change_comes_with_its_frames(self):
        now = T0
        a = shipmate.ship_scene(shipmate.demo_snapshot(now))
        b_snap = shipmate.demo_snapshot(now + 30, moved=True)
        b = shipmate.ship_scene(b_snap)
        ev = shipmate.block_ship(b_snap, shipmate.ship_print(a))["events"]
        self.assertEqual([(e["kind"], e.get("id")) for e in ev],
                         [("handover", "DEMO-AUTH"), ("leave", "DEMO-I18N"),
                          ("board", "DEMO-Q-THEME"), ("move", "DEMO-EXPORT"), ("move", "DEMO-LOGS"), ("move", "DEMO-NAV"),
                          ("arrive", "DEMO-Q-CSV"), ("beacon", "DEMO-LOGS"), ("fish", "DEMO-ICONS"), ("pump", None)])
        hand = ev[0]
        self.assertEqual((hand["leave"]["id"], hand["leave"]["delivered"], hand["board"]["id"]), ("DEMO-AUTH", True, "DEMO-Q-PUSH"))
        frames = {(e["kind"], e.get("id")): e["frames"] for e in ev if e["kind"] != "handover"}
        frames[("handover-leave", "DEMO-AUTH")] = hand["leave"]["frames"]
        frames[("handover-board", "DEMO-Q-PUSH")] = hand["board"]["frames"]
        self.assertTrue(set(shipmate.CARRY_POSES) | {"crate", "ring", "walk", "stand"} <= set(frames[("leave", "DEMO-I18N")]))
        self.assertTrue({"ashore-walk", "ashore-stand", "walk"} <= set(frames[("handover-board", "DEMO-Q-PUSH")]))
        self.assertTrue(set(shipmate.WALK_POSES) | {"ashore-stand", "ashore-walk"} <= set(frames[("board", "DEMO-Q-THEME")]))
        self.assertIn("alarm", frames[("move", "DEMO-NAV")])                            # the red flag on the way to the cabin
        self.assertIn("answered", frames[("move", "DEMO-EXPORT")])                      # the answer on the way out
        self.assertTrue({"look", "fish", "reel", "catch"} <= set(frames[("move", "DEMO-LOGS")]))   # it looks, walks, catches
        self.assertEqual(set(frames[("fish", "DEMO-ICONS")]), {"fish", "reel", "catch"})
        self.assertEqual(frames[("beacon", "DEMO-LOGS")], {})
        self.assertIn("jet", frames[("pump", None)])
        self.assertEqual(set(frames[("arrive", "DEMO-Q-CSV")]), {"walk", "stand"})
        for f in frames.values():
            for svg in f.values():
                self.assertTrue(svg.startswith("<g>") and "<path" in svg)
        # the drawing it plays over: the buckets at the rail and their fish, the lighthouse, the
        # gangplank, the pump, every hand's anchor
        drawn = shipmate.render_ship(b)
        self.assertIn('class="sm-bucket" data-for="DEMO-LOGS"', drawn)
        self.assertNotIn('class="sm-bucket" data-for="DEMO-FORMS"', drawn)              # it watches the lighthouse
        self.assertRegex(drawn, r'data-for="DEMO-ICONS">.*?class="sm-bucket__fish" data-n="4"')
        self.assertIn('<g class="sm-beacon is-running"><title>the lighthouse: the checks are running for DEMO-FORMS</title>', drawn)
        self.assertRegex(drawn, r'<g class="sm-plank">')
        self.assertNotIn('visibility="hidden" class="sm-plank"', drawn)
        self.assertIn('class="sm-pump__down" visibility="hidden"', drawn)
        lay = shipmate.ship_layout(b)
        for hid, (x, feet, pose, _u) in lay["hands"].items():
            self.assertIn('data-id="%s" data-stage="' % hid, drawn)
            self.assertRegex(drawn, r'data-id="%s" data-stage="[^"]*" data-at="%d,%d"' % (hid, x + shipmate.pose_left(pose), feet))
        printed = json.loads(html.unescape(re.search(r'data-print="([^"]*)"', drawn).group(1)))
        self.assertEqual(printed, json.loads(json.dumps(shipmate.ship_print(b))))
        geo = json.loads(html.unescape(re.search(r'data-geo="([^"]*)"', drawn).group(1)))
        self.assertEqual(set(geo), {"ladder", "walk", "deck_x", "nest", "door", "floor", "fc", "plank", "pier", "stern", "wl",
                                    "rod", "reel", "hold", "pump", "crates", "dock", "bell", "edge"})
        # the delivered crates are drawn one by one, so the page can hold back the one on its way
        for cid in ("DEMO-AUTH", "DEMO-I18N"):
            self.assertRegex(drawn, r'<g class="sm-crate" data-for="%s" data-at="\d+,\d+">' % cid)
        # and back: the changes the other way walk too, but nobody boards and no pump works
        back = shipmate.ship_events(shipmate.ship_print(b), dict(shipmate.ship_print(a), at=now + 60))
        self.assertEqual([(e["kind"], e["id"]) for e in back],
                         [("move", "DEMO-EXPORT"), ("move", "DEMO-LOGS"), ("move", "DEMO-NAV"), ("move", "DEMO-SEARCH")])

    def test_a_task_that_joins_the_queue_for_a_slot_walks_into_the_tavern(self):
        prev = dict(self.print_(at=T0 - 30, hands={"A": "deck"}, queue={"Q": "pier"}), scouts={"S": "dock"})
        cur = dict(self.print_(hands={}, queue=["Q", "N", "A", "S", "M", "P"]), scouts={}, crates=[],
                   tavern=["A", "M", "N", "Q", "S"])
        # new to the queue only: Q was queued already, A came back from the deck, S from its skiff, P is not in the tavern
        self.assertEqual([(e["kind"], e["id"]) for e in shipmate.ship_events(prev, cur) if e["kind"] == "arrive"],
                         [("arrive", "M"), ("arrive", "N")])
        many = dict(cur, tavern=["N%d" % i for i in range(5)])
        self.assertEqual(sum(e["kind"] == "arrive" for e in shipmate.ship_events(prev, many)), shipmate.EVENT_LIMITS["arrive"])
        self.assertEqual(shipmate.ship_events(None, cur), [])                          # nothing plays when the page just opened
        self.assertEqual([e for e in shipmate.ship_events(prev, dict(cur, tavern=["../x"]))if e["kind"] == "arrive"], [])
        sc = shipmate.ship_scene(shipmate.demo_snapshot(T0, moved=True))
        ev = shipmate.event_sprites({"kind": "arrive", "id": "DEMO-Q-CSV"}, sc)
        self.assertEqual(set(ev["frames"]), {"walk", "stand"})
        self.assertIn("#e6c46a", ev["frames"]["walk"])                                 # still ashore: the straw hat

    def test_a_hand_that_leaves_carries_its_crate_only_when_delivered(self):
        prev = dict(self.print_(at=T0 - 30, hands={"A": "rail", "B": "deck", "C": "nest", "D": "cabin"}),
                    scouts={"S": "dock", "T": "out"}, looks={"A": "#16a4a4 #f6f1e3", "B": "#9150dc", "S": "#f2a114"})
        cur = dict(self.print_(hands={"D": "cabin"}, queue=["C"]), scouts={}, crates=["OLD", "A", "S"])
        self.assertEqual(shipmate.ship_events(prev, cur),
                         [{"kind": "leave", "id": "A", "from": "rail", "delivered": True, "look": "#16a4a4 #f6f1e3"},
                          {"kind": "leave", "id": "B", "from": "deck", "delivered": False, "look": "#9150dc"}])
        # a scout leaves only from its skiff at the pier (one at sea just goes), past the hands, two at most a refresh
        cur = dict(cur, hands={"A": "rail", "B": "deck", "D": "cabin"})
        self.assertEqual(self.kinds(prev, cur), [("leave", "S")])
        many = dict(self.print_(at=T0 - 30, hands={"H%d" % i: "deck" for i in range(5)}), looks={})
        self.assertEqual(len(shipmate.ship_events(many, dict(self.print_(), scouts={}, crates=[]))), shipmate.EVENT_LIMITS["leave"])
        # an older print without the new fields, or an id that is not one, plays no departure
        self.assertEqual(self.kinds(self.print_(at=T0 - 30, hands={"../x": "deck", "A": "deck"}), self.print_()),
                         [("leave", "A")])

    def test_a_slot_is_handed_over_only_by_the_hand_that_held_it(self):
        def prev(holds):
            return dict(self.print_(at=T0 - 30, hands={"B": "deck", "P": "rail"}, queue={"Q": "pier", "H": "cabin"}),
                        holds=holds, looks={})
        cur = dict(self.print_(hands={"Q": "nest"}), scouts={}, crates=["B", "P"])
        ev = shipmate.ship_events(prev({"Q": "B R"}), cur)
        self.assertEqual([(e["kind"], e["id"]) for e in ev], [("handover", "B"), ("leave", "P"), ])
        self.assertEqual((ev[0]["leave"]["id"], ev[0]["leave"]["delivered"], ev[0]["board"]),
                         ("B", True, {"kind": "board", "id": "Q", "from": "pier", "to": "nest"}))
        # the hand with a PR gave its slot up at the bow: it leaves, and the other boards, each on its own
        self.assertEqual(self.kinds(prev({"Q": "R"}), cur), [("leave", "B"), ("leave", "P"), ("board", "Q")])
        # one held for the captain walks out of the cabin, not up from the pier: no handover
        held = dict(self.print_(hands={"H": "nest"}), scouts={}, crates=[])
        self.assertEqual(self.kinds(prev({"H": "B"}), held), [("leave", "B"), ("leave", "P"), ("board", "H")])

    def test_a_departure_is_drawn_from_the_colours_its_print_kept(self):
        sc = shipmate.ship_scene(shipmate.demo_snapshot(T0))
        ev = shipmate.event_sprites({"kind": "leave", "id": "GONE", "from": "rail", "delivered": True,
                                     "look": "#16a4a4 #f6f1e3"}, sc)
        self.assertTrue(set(shipmate.CARRY_POSES) | set(shipmate.WALK_POSES) | {"wave", "crate", "ring"} <= set(ev["frames"]))
        self.assertIn('fill="#16a4a4"', ev["frames"]["carry"])
        empty = shipmate.event_sprites({"kind": "leave", "id": "GONE", "from": "deck", "delivered": False,
                                        "look": '"/><script>x</script> #zzz'}, sc)
        self.assertNotIn("carry", empty["frames"])
        self.assertFalse(any("script" in f for f in empty["frames"].values()))       # a colour that is not one is dropped

    def test_the_crates_count_what_firstmate_closed_as_done(self):
        backlog = [{"id": "OLD", "state": "done", "closed": "2026-11-01"},
                   {"id": "B", "state": "done", "closed": "2026-11-13", "kind": "ship", "repo": "r"},
                   {"id": "A", "state": "done", "closed": "2026-11-13T09:00Z", "kind": "scout", "repo": "r"},
                   {"id": "C", "state": "done", "closed": "2026-11-08"},
                   {"id": "Q", "state": "queued"}, {"id": "N", "state": "done", "closed": ""}]
        now = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))
        self.assertEqual([r["id"] for r in shipmate.closed_rows(backlog, now)], ["C", "A", "B"])
        snap = dict(shipmate.demo_snapshot(T0), closed=[{"id": "X", "kind": "ship"}], delivered=[{"id": "SHEET"}])
        self.assertEqual([c["id"] for c in shipmate.ship_scene(snap)["crates"]], ["X"])

    def test_a_pose_puts_the_body_where_the_frames_expect_it(self):
        self.assertEqual([shipmate.pose_left(p) for p in ("stand", "hammer", "hammer-l", "signal", "signal-l", "mop", "walk-l")],
                         [0, 0, 4, 5, 5, 6, 2])
        for pose in shipmate.WALK_POSES:
            rows = shipmate.POSES[pose]
            self.assertEqual(len(rows), 21, pose)                                      # same height: feet stay on the deck


class TestLanguage(unittest.TestCase):
    """--lang pt-BR (or SHIPMATE_LANG) shows the interface in Brazilian Portuguese."""

    def tearDown(self):
        shipmate.LANG = "en"

    def test_every_phrase_the_page_can_show_has_a_translation(self):
        with open(shipmate.__file__, encoding="utf-8") as fh:
            src = fh.read()
        phrases = set()
        for m in re.finditer(r"(?<![\w.])tr\(", src):                  # the literals inside each tr(...) argument
            depth, i = 1, m.end()
            while depth:
                if src[i] == '"':
                    i = src.index('"', i + 1) if src[i + 1] != '"' else i + 1
                    while src[i - 1] == "\\":
                        i = src.index('"', i + 1)
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                i += 1
            arg = re.sub(r'\["[^"]*"\]', "", src[m.end():i - 1])            # keys like h["id"] are not phrases
            phrases |= set(re.findall(r'"((?:[^"\\]|\\.)*)"', arg))
        phrases |= {x for _k, _p, name, where in shipmate.STAGES for x in (name, where)}
        phrases |= set(shipmate.PLACE_TEXT.values()) | set(shipmate.LANE_NAMES) | set(shipmate.SHIP_PHASES)
        phrases |= set(shipmate.SCOUT_PHASES) | set(shipmate.CREW_TONE) | set(shipmate.CLASSES) | {"unknown", "inferred", "configured"}
        phrases |= {label for _mark, label in shipmate.ANSWER_PILLS.values()} | set(shipmate.ANSWER_MESSAGES.values())
        phrases |= set(shipmate.CHECKS_TEXT.values())
        phrases |= set(shipmate.CLEAR_TEXT.values()) | set(shipmate.CLEAR_LABEL.values())
        phrases |= set(m[1] for m in shipmate.RE_PHRASE.findall(shipmate.asset("shipmate.html")))
        phrases = {p.replace('\\"', '"') for p in phrases if p}
        self.assertGreater(len(phrases), 150)
        self.assertEqual(sorted(p for p in phrases if p not in shipmate.PT_BR), [])
        for en, pt in shipmate.PT_BR.items():                           # a translation keeps the same placeholders
            self.assertEqual(re.findall(r"%[0-9]*[sd%]", en), re.findall(r"%[0-9]*[sd%]", pt), en)
            self.assertNotIn("\u2014", pt)

    def test_the_page_in_portuguese(self):
        snap = shipmate.demo_snapshot(T0)
        panel = shipmate.DemoPanel(0)
        english, _ = shipmate.render_page(panel, snap, panel.notice)
        shipmate.LANG = "pt-BR"
        snap = shipmate.demo_snapshot(T0)
        page, _ = shipmate.render_page(panel, snap, panel.notice)
        self.assertIn('<html lang="pt-BR">', page)
        self.assertNotRegex(page, r"\{\{[tj]:")
        for phrase in ("Trabalho por fase", "Adicionar tarefa", "Sessão do firstmate", "esperando você", "Capitão",
                       "CABINE DO CAPITÃO", "Uma home inventada".lower(), "marujos a bordo", "Carregando",
                       "FAROL · CI", "O farol na frente da proa mostra o CI", "Na amurada o marujo pesca enquanto espera o merge",
                       "esperando o merge / entregue"):
            self.assertIn(phrase, page.replace("Demo: uma home", "demo: uma home"))
        for phrase in ("Work by phase", "+ Add task", "waiting for you", "hands aboard"):
            self.assertNotIn(phrase, page)
            self.assertIn(phrase, english)
        self.assertEqual(shipmate.fmt_date_iso("2026-11-13"), "sex 13 nov")
        self.assertIn("estado da tripulação: parado", " ".join(shipmate.captain_reasons({"crew": "parked"})))
        det = panel.detail("DEMO-RATE")
        for phrase in ("Sua resposta", "Enviar ao firstmate", "Opções prontas"):
            self.assertIn(phrase, det)

    def test_the_language_is_a_setting(self):
        with open(os.path.join(os.path.dirname(shipmate.__file__), "README.md"), encoding="utf-8") as fh:
            self.assertIn("--lang", fh.read())
        with self.assertRaises(SystemExit):
            shipmate.main(["--demo", "--once", "--lang", "fr"])
        old = os.environ.get("SHIPMATE_LANG")
        os.environ["SHIPMATE_LANG"] = "fr"
        try:
            self.assertEqual(shipmate.main(["--demo", "--once"]), 2)
        finally:
            os.environ.pop("SHIPMATE_LANG")
            if old is not None:
                os.environ["SHIPMATE_LANG"] = old


class TestFleetCache(Base):
    """One reading of the fleet answers every request for a few seconds, never a reply just sent."""

    def setUp(self):
        super().setUp()
        self.meta("TASK-A")
        self.reads = 0
        real = shipmate.read_backlog
        def counting(cfg):
            self.reads += 1
            return real(cfg)
        self.patch = mock.patch.object(shipmate, "read_backlog", counting)
        self.patch.start()
        self.clock = [1000.0]
        self.mono = mock.patch.object(shipmate.time, "monotonic", lambda: self.clock[0])
        self.mono.start()

    def tearDown(self):
        self.mono.stop()
        self.patch.stop()
        super().tearDown()

    def test_requests_inside_the_window_share_one_reading(self):
        panel = shipmate.Panel(self.cfg)
        first = panel.snapshot()
        self.clock[0] += self.cfg.cache_seconds - 0.5
        self.assertIs(panel.snapshot(), first)
        self.assertEqual(self.reads, 1)
        self.clock[0] += 1.0
        self.assertIsNot(panel.snapshot(), first)
        self.assertEqual(self.reads, 2)

    def test_zero_seconds_turns_the_cache_off(self):
        self.cfg.cache_seconds = 0
        panel = shipmate.Panel(self.cfg)
        panel.snapshot()
        panel.snapshot()
        self.assertEqual(self.reads, 2)

    def test_dropping_forces_a_fresh_reading(self):
        panel = shipmate.Panel(self.cfg)
        panel.snapshot()
        panel.drop()
        panel.snapshot()
        self.assertEqual(self.reads, 2)

    def test_a_reading_begun_before_a_drop_is_not_reused_after_it(self):
        panel = shipmate.Panel(self.cfg)
        real = panel.read_fleet
        def slow():
            snap = real()
            panel.drop()            # the captain's reply lands while this reading is still under way
            return snap
        panel.read_fleet = slow
        panel.snapshot()
        panel.read_fleet = real
        panel.snapshot()
        self.assertEqual(self.reads, 2)

    def test_sending_a_task_drops_the_reading(self):
        panel = shipmate.Panel(self.cfg)
        panel.snapshot()
        res = panel.add_task("A new thing", "demo-app", "", "req-cache")
        self.assertTrue(res["ok"], res)
        panel.snapshot()
        self.assertEqual(self.reads, 2)

    def test_the_option_sets_the_window(self):
        old = dict(os.environ)
        seen = {}
        try:
            os.environ["SHIPMATE_CACHE_SECONDS"] = "12"
            with mock.patch.object(shipmate, "serve", lambda panel, what: seen.update(ttl=panel.cfg.cache_seconds) or 0):
                shipmate.main(["--home", self.home, "--fm-root", self.root, "--port", "0"])
                self.assertEqual(seen["ttl"], 12.0)
                shipmate.main(["--home", self.home, "--fm-root", self.root, "--port", "0", "--cache-seconds", "0"])
                self.assertEqual(seen["ttl"], 0.0)
        finally:
            os.environ.clear()
            os.environ.update(old)


class TestAwayTab(unittest.TestCase):
    """The page pauses its scene and slows its refresh when the tab is hidden (checked on the page's source)."""

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "shipmate.html"), encoding="utf-8") as fh:
            self.html = fh.read()
        with open(os.path.join(here, "shipmate.css"), encoding="utf-8") as fh:
            self.css = fh.read()

    def test_a_hidden_tab_pauses_every_animation_and_resumes_in_place(self):
        self.assertRegex(self.css, r"\.is-away \*[^{]*\{[^}]*animation-play-state:\s*paused")
        self.assertNotIn("animation-delay", self.css.split(".is-away")[1].split("}")[0])   # paused, not restarted
        self.assertIn('classList.toggle("is-away", document.hidden)', self.html)
        self.assertIn('"visibilitychange"', self.html)

    def test_a_hidden_tab_refreshes_far_less_often(self):
        self.assertIn("document.hidden ? awayEvery : every", self.html)
        m = re.search(r"var awayEvery = every \* (\d+);", self.html)
        self.assertTrue(m and int(m.group(1)) >= 4)
        self.assertNotIn("setInterval(tick", self.html)


FAKE_HERDR = r"""#!/bin/sh
# fake herdr: read-only answers from canned JSON; every call logged
printf '%s\n' "$*" >> "$FAKE_DIR/herdr.log"
case "$1 $2" in
  "agent list") cat "$FAKE_DIR/herdr-agents.json" ;;
  "pane process-info") cat "$FAKE_DIR/herdr-process.json" ;;
  *) echo '{"error":{"code":"unexpected"}}'; exit 1 ;;
esac
"""
FAKE_SEND = r"""#!/bin/sh
# fake fm-send.sh: logs the target and the text, exits with send.rc
printf '%s\n' "$*" >> "$FAKE_DIR/send.log"
exit "$(cat "$FAKE_DIR/send.rc" 2>/dev/null || echo 0)"
"""
CLEAN_LIST = """count: 1
tasks[1]{id,state,kind,repo,title,blocked,blocked_by,created,deps,held,hold_kind,hold_reason,hold_until,priority,links}:
  TASK-B,queued,ship,demo-app,Ready one,no,none,2026-01-02,none,no,"-","-","-","-",none
"""


class TestClearContext(Base):
    """"Clear context": save first (an inbox note asking for /stow), clear only after firstmate's ready
    reply (fm-send types /clear into its herdr pane), and refuse whatever is in flight."""

    def setUp(self):
        super().setUp()
        write(os.path.join(self.root, "bin", "fm-send.sh"), FAKE_SEND, 0o755)
        self.cfg.herdr_cmd = write(os.path.join(self.fake, "herdr"), FAKE_HERDR, 0o755)
        write(os.path.join(self.fake, "list.toon"), CLEAN_LIST)
        write(os.path.join(self.home, "state", ".lock"), "4242\n")
        write(os.path.join(self.home, "state", ".lock-session"), "sess-fm\n")
        t = Transcript()
        t.call(time.time() - 60, inp=1, cw=20000, cr=292999)
        t.save(os.path.join(self.acct, "projects", shipmate.encode_dir(self.home), "sess-fm.jsonl"))
        self.agents(status="idle")
        self.process(4242)
        self.panel = shipmate.Panel(self.cfg)
        self.flow = self.panel.clear = shipmate.ClearFlow(self.panel, timeout=20, idle_wait=5, poll=0.02)

    def tearDown(self):
        self.flow.stop.set()
        if self.flow.thread:
            self.flow.thread.join(5)
        super().tearDown()

    def agents(self, status="idle", sessions=("sess-fm",), cwd=None):
        rows = [{"agent": "claude", "agent_session": {"kind": "id", "value": s}, "agent_status": status,
                 "cwd": cwd or self.home, "pane_id": "w1:p%d" % (i + 1)} for i, s in enumerate(sessions)]
        rows.append({"agent": "claude", "agent_session": {"kind": "id", "value": "sess-worker"}, "agent_status": "working",
                     "cwd": "/work/pool/x", "pane_id": "w9:p2"})
        write(os.path.join(self.fake, "herdr-agents.json"), json.dumps({"id": "cli:agent:list", "result": {"agents": rows}}))

    def process(self, pid, pane="w1:p1"):
        write(os.path.join(self.fake, "herdr-process.json"), json.dumps({"result": {"type": "pane_process_info", "process_info": {
            "pane_id": pane, "shell_pid": 100, "foreground_process_group_id": pid, "foreground_processes": [{"pid": pid}]}}}))

    def receipts(self, pending=(), handled=()):
        write(os.path.join(self.fake, "receipts.json"), json.dumps({"pending": list(pending), "handled": list(handled)}))

    def reply(self, text):
        self.receipts(handled=[{"id": "1-x", "request_id": "shipmate-clear:f1", "acknowledged": True,
                                "reply": {"at": iso(time.time()), "body": text}}])

    def log(self, name):
        try:
            with open(os.path.join(self.fake, name)) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def until(self, *states):
        end = time.time() + 10
        while time.time() < end:
            j = self.flow.view()
            if j and j["state"] in states:
                return j
            time.sleep(0.02)
        self.fail("the request never reached %s: %r" % (states, self.flow.view()))

    def test_the_pane_is_the_one_both_lock_records_name(self):
        p = shipmate.firstmate_pane(self.cfg)
        self.assertEqual(p, {"pane": "w1:p1", "target": "default:w1:p1", "status": "idle"})
        self.assertIn("--session default", self.log("herdr.log")[0])
        self.agents(sessions=("sess-fm", "sess-fm"))                   # two panes claim it: no pick
        self.assertIn("2 panes", shipmate.firstmate_pane(self.cfg)["error"])
        self.agents(sessions=("sess-other",))
        self.assertIn("no pane for firstmate's session", shipmate.firstmate_pane(self.cfg)["error"])
        self.agents(cwd="/somewhere/else")
        self.assertIn("not in this home", shipmate.firstmate_pane(self.cfg)["error"])
        self.agents()
        self.process(777)                                              # the pane runs another process
        self.assertIn("pid 4242", shipmate.firstmate_pane(self.cfg)["error"])
        self.process(4242)
        os.remove(os.path.join(self.home, "state", ".lock-session"))
        self.assertIn("no firstmate session lock", shipmate.firstmate_pane(self.cfg)["error"])
        self.assertTrue(all(l.split()[:2] in (["agent", "list"], ["pane", "process-info"]) for l in self.log("herdr.log")))

    def test_what_is_in_flight_blocks_the_request(self):
        snap = self.panel.snapshot()
        doc = {"pending": [], "handled": []}
        self.assertEqual(shipmate.clear_blockers(snap, doc, None, []), [])
        write(os.path.join(self.fake, "list.toon"), CLEAN_LIST.replace("count: 1", "count: 2").replace("tasks[1]", "tasks[2]")
              + '  TASK-C,queued,scout,demo-app,Wait for a call,no,none,2026-01-03,none,yes,captain,"Pick a or b","-","-",none\n')
        self.meta("TASK-A")
        self.status("TASK-A", ["needs-decision [at=%d] [key=pick]: A) one B) two" % T0])
        self.panel.drop()
        why = shipmate.clear_blockers(self.panel.snapshot(), doc, None, [])
        self.assertEqual(why, ["2 questions wait for the captain's answer: TASK-A, TASK-C"])
        why = shipmate.clear_blockers(snap, {"pending": [{"id": "9-n"}, {"id": "1-x"}]}, None, ["TASK-M", "away"], own_note="1-x")
        self.assertEqual(why, ["1 note waits in firstmate's inbox: 9-n", "a merge or another control action is under way: TASK-M, away"])
        self.assertIn("could not be read", shipmate.clear_blockers(snap, None, "exit 1", [])[0])
        self.assertEqual(shipmate.clear_blockers(dict(snap, firstmate=None), doc, None, []), ["firstmate's session is unknown"])

    def test_a_merge_lock_counts_only_while_its_holder_lives(self):
        state = os.path.join(self.home, "state")
        def lock(name, pid, start=None):
            owner = os.path.join(state, name + ".owner.abc")
            write(os.path.join(owner, "pid"), "%d\n" % pid)
            if start is not None:
                write(os.path.join(owner, "pid-start"), start + "\n")
            os.symlink(owner, os.path.join(state, name))
        rc, out, _ = shipmate.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], env=dict(os.environ, LC_ALL="C", TZ="UTC0"))
        lock(".control-TASK-M.lock", os.getpid(), out.strip())
        lock(".control-TASK-OLD.lock", 999999)                               # its holder is gone
        lock(".control-TASK-REUSED.lock", os.getpid(), "Thu Jan  1 00:00:00 1970")   # the pid now belongs to another process
        lock(".afk-contract.lock", os.getpid())
        self.assertEqual(shipmate.held_locks(self.home), ["TASK-M", "away"])

    def test_save_then_clear_in_that_order(self):
        res = self.flow.start("f1")
        self.assertTrue(res["ok"], res)
        note = self.log("inbox.log")[0]
        self.assertTrue(note.startswith("note --request-id shipmate-clear:f1 --json -- Clear request from shipmate"))
        for part in ("Run /stow now", "bin/fm-inbox.sh reply <this note's id> 'stowed: ready to clear'", "313k of 1.00M tokens",
                     "'not ready: <reason>'", "within 1 minutes"):
            self.assertIn(part, note)
        self.assertEqual(self.flow.view()["state"], "stowing")
        self.assertEqual(self.flow.start("f1")["ok"], True)                 # the same form again is the same request
        self.assertEqual(len(self.log("inbox.log")), 1)
        self.assertIn("already under way", self.flow.start("f2")["error"])
        time.sleep(0.2)
        self.assertEqual(self.log("send.log"), [])                          # no /clear before the reply
        self.agents(status="working")                                       # firstmate is still ending its turn
        self.reply("stowed: ready to clear")
        self.until("waiting")
        time.sleep(0.2)
        self.assertEqual(self.log("send.log"), [])                          # never into a busy pane
        self.agents(status="idle")
        j = self.until("cleared", "refused", "failed")
        self.assertEqual(j["state"], "cleared", j)
        self.assertEqual(self.log("send.log"), ["default:w1:p1 /clear"])

    def test_nothing_is_typed_without_the_ready_reply(self):
        for reply, state, words in (("not ready: a merge is mine to finish", "refused", "a merge is mine"),
                                    ("I stowed most of it", "refused", "without \"stowed: ready to clear\""),
                                    (None, "timeout", "no reply from firstmate")):
            with self.subTest(reply=reply):
                self.receipts()
                self.flow.timeout = 0.3
                self.assertTrue(self.flow.start("f1" if reply else "f3")["ok"])
                if reply:
                    self.reply(reply)
                j = self.until("refused", "timeout", "cleared", "failed")
                self.assertEqual(j["state"], state)
                self.assertIn(words, j["detail"])
                self.assertEqual(self.log("send.log"), [])
                self.flow.thread.join(5)
                self.flow.job["request"] = "done"                            # the next subtest starts a new request

    def test_the_guards_are_read_again_before_typing(self):
        self.assertTrue(self.flow.start("f1")["ok"])
        self.receipts(pending=[{"id": "7-new", "request_id": "x"}],
                      handled=[{"id": "1-x", "request_id": "shipmate-clear:f1", "reply": {"at": iso(time.time()), "body": "stowed: ready to clear"}}])
        j = self.until("refused", "cleared")
        self.assertEqual(j["state"], "refused")
        self.assertIn("7-new", j["detail"])
        self.assertEqual(self.log("send.log"), [])

    def test_a_session_that_never_stowed_is_never_cleared(self):
        self.assertTrue(self.flow.start("f1")["ok"])
        self.agents(sessions=("sess-new",))                                  # firstmate restarted meanwhile
        write(os.path.join(self.home, "state", ".lock-session"), "sess-new\n")
        self.reply("stowed: ready to clear")
        j = self.until("refused", "cleared")
        self.assertIn("session changed since the request", j["detail"])
        self.assertEqual(self.log("send.log"), [])

    def test_a_pane_that_stays_busy_is_never_cleared(self):
        self.flow.idle_wait = 0.3
        self.assertTrue(self.flow.start("f1")["ok"])
        self.agents(status="working")
        self.reply("stowed: ready to clear")
        j = self.until("refused", "cleared")
        self.assertIn("stayed busy (working)", j["detail"])
        self.assertEqual(self.log("send.log"), [])

    def test_an_unconfirmed_or_refused_send_is_reported_and_never_retried(self):
        for rc, state in (("3", "unconfirmed"), ("1", "failed")):
            with self.subTest(rc=rc):
                write(os.path.join(self.fake, "send.rc"), rc)
                os.makedirs(os.path.join(self.fake, "requests"), exist_ok=True)
                self.receipts()
                self.assertTrue(self.flow.start("f1")["ok"])
                self.reply("stowed: ready to clear")
                self.assertEqual(self.until("unconfirmed", "failed", "cleared")["state"], state)
                self.assertEqual(len(self.log("send.log")), 1)
                os.remove(os.path.join(self.fake, "send.log"))
                self.flow.thread.join(5)
                self.flow.job["request"] = "done"

    def test_stop_waiting_types_nothing(self):
        self.assertTrue(self.flow.start("f1")["ok"])
        self.assertTrue(self.flow.cancel()["ok"])
        self.reply("stowed: ready to clear")
        time.sleep(0.3)
        self.assertEqual(self.flow.view()["state"], "cancelled")
        self.assertEqual(self.log("send.log"), [])
        self.assertFalse(self.flow.cancel()["ok"])

    def test_refused_up_front_with_the_reasons(self):
        self.receipts(pending=[{"id": "5-n"}])
        res = self.flow.start("f1")
        self.assertFalse(res["ok"])
        self.assertIn("1 note waits in firstmate's inbox: 5-n", res["error"])
        self.receipts()
        self.agents(sessions=())
        self.assertIn("no pane for firstmate's session", self.flow.start("f1")["error"])
        self.assertEqual(self.log("inbox.log"), [])                          # nothing asked of firstmate
        drawer = self.panel.clear_drawer()
        self.assertIn("Not now", drawer)
        self.assertNotIn("data-clear>", drawer)

    def test_the_drawer_and_the_server(self):
        self.panel.clear = self.flow
        srv = shipmate.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
        self.cfg.port = srv.server_address[1]
        srv.RequestHandlerClass = shipmate.make_handler(self.panel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def req(method, path, body=None):
                c = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=30)
                h = {"Host": "127.0.0.1:%d" % self.cfg.port}
                if body is not None:
                    body = urllib.parse.urlencode(body)
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                c.request(method, path, body=body, headers=h)
                r = c.getresponse()
                return r, r.read().decode()
            _r, page = req("GET", "/")
            self.assertIn('data-action="clear"', page)
            _r, drawer = req("GET", "/clear")
            for part in ("313k of 1.00M", "Save: shipmate asks firstmate", "Clear: only after firstmate replies",
                         "Resume: the new session", "default:w1:p1", "Save, then clear 313k", "does not interrupt them"):
                self.assertIn(part, drawer)
            r, _ = req("POST", "/clear", {"token": "nope", "request": "f1"})
            self.assertEqual(r.status, 403)
            _r, body = req("POST", "/clear", {"token": self.panel.token, "request": "bad id!"})
            self.assertFalse(json.loads(body)["ok"])
            _r, body = req("POST", "/clear", {"token": self.panel.token, "request": "f1"})
            self.assertTrue(json.loads(body)["ok"], body)
            _r, drawer = req("GET", "/clear")
            self.assertIn("data-live", drawer)
            self.assertIn("Stop waiting", drawer)
            self.assertIn("clear: saving", json.loads(req("GET", "/blocks")[1])["firstmate"])
            _r, body = req("POST", "/clear/cancel", {"token": self.panel.token})
            self.assertTrue(json.loads(body)["ok"])
            self.assertIn("clear: stopped", json.loads(req("GET", "/blocks")[1])["firstmate"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_the_demo_types_nothing(self):
        demo = shipmate.DemoPanel(0)
        self.assertIn("this is the demo", demo.clear_drawer())
        self.assertFalse(demo.clear_start("f1")["ok"])

    def test_the_readme_says_what_the_button_does(self):
        with open(os.path.join(os.path.dirname(shipmate.__file__), "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        for part in ("## Clearing firstmate's context", shipmate.CLEAR_READY, "bin/fm-send.sh", "--clear-timeout"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
