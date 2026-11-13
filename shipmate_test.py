#!/usr/bin/env python3
"""shipmate tests: small synthetic fixtures only (no real transcript, home, or account)."""
import datetime as dt
import html
import http.client
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
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
printf '%s' "$*" | tr '\n' '|' >> "$FAKE_DIR/inbox.log"; echo >> "$FAKE_DIR/inbox.log"
echo '{"schema":"fm-inbox-note.v1","outcome":"created","id":"1-x","saved":true,"announced":true}'
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
        self.assertEqual(q["TASK-C"], ["date", "decision"])
        self.assertEqual(q["TASK-D"], ["task"])
        self.assertNotIn("TASK-A", q)
        # once TASK-A records its PR handoff the place frees
        self.meta("TASK-A", "pr=https://forge.example/o/r/pull/1\n")
        q = {r["id"]: [x[0] for x in r["reasons"]] for r in shipmate.queue_rows(self.cfg, rows)}
        self.assertEqual(q["TASK-B"], ["ready"])


    def test_how_long_a_queued_task_has_waited_for_the_captain(self):
        stamp = shipmate.parse_hold_set("Captain hold set: 2026-01-05T10:20:30Z\n\nnotes")
        self.assertEqual(stamp, (dt.datetime(2026, 1, 5, 10, 20, 30, tzinfo=dt.timezone.utc).timestamp(), True))
        self.assertEqual(shipmate.parse_hold_set("Captain hold set: 2026-01-05"),
                         (time.mktime(dt.date(2026, 1, 5).timetuple()), False))
        for body in ("", None, "notes first\nCaptain hold set: 2026-01-05T10:20:30Z", "Captain hold set: 2026-13-45T99:00:00Z"):
            self.assertEqual(shipmate.parse_hold_set(body), (None, False), body)
        rows, _ = shipmate.read_backlog(self.cfg)
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
    def test_assets_have_no_external_dependency(self):
        for name in ("shipmate.html", "shipmate.css"):
            text = shipmate.asset(name)
            self.assertNotIn("https://", text)
            self.assertNotIn("@import", text)
        self.assertIn("prefers-color-scheme: dark", shipmate.asset("shipmate.css"))


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
                         {"S0": "nest", "S1": "deck", "S2": "bow", "S3": "rail", "S4": "plank", "SX": "hatch"})
        self.assertEqual({h["id"]: h["place"] for h in sc["scouts"]}, {"C0": "out", "C1": "back", "C2": "dock", "CX": "out"})
        poses = {k: v[2] for k, v in shipmate.ship_layout(sc)["hands"].items()}
        self.assertEqual(poses, {"S0": "spy", "S1": "hammer", "S2": "signal", "S3": "fish", "S4": "pack", "SX": "stand"})
        self.assertEqual(shipmate.ship_counts(sc), "10 hands aboard · 0 boats moored")

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

    def test_the_line_stands_in_the_cabin_and_boats_tie_up_astern(self):
        flagged = [self.hand("F%d" % i, i % 5, crew="parked", last=T0 - 100 * i) for i in range(5)]
        queue = [{"id": "Q%d" % i, "reasons": [("decision", "c", "c")], "waiting_since": T0 - i} for i in range(9)]
        queue.append({"id": "FREE", "reasons": [("ready", "r", "r")]})
        sc = shipmate.ship_scene(self.snap(flagged + [self.hand("OK", 1)], queue=queue))
        lay = shipmate.ship_layout(sc)
        empty = shipmate.ship_layout(shipmate.ship_scene(self.snap([self.hand("OK", 1)])))
        one = shipmate.ship_layout(shipmate.ship_scene(self.snap([self.hand("A", 1, crew="parked")])))
        self.assertEqual(one["castle"], empty["castle"])                 # the cabin does not jump at the first request
        self.assertGreater(lay["castle"], empty["castle"])               # and grows with the line
        cabin = [lay["hands"]["F%d" % i] for i in (4, 3, 2, 1, 0)]       # longest wait nearest the desk
        self.assertEqual([c[0] for c in cabin], sorted(c[0] for c in cabin))
        self.assertTrue(all(c[2].endswith("-l") and lay["cabin"][0] <= c[0] < lay["cabin"][1] for c in cabin))
        self.assertEqual(lay["hands"]["OK"][2], "hammer")
        self.assertEqual(list(lay["astern"]), ["Q8", "Q7", "Q6", "Q5", "Q4", "Q3"][:shipmate.MAX_ASTERN])
        svg = shipmate.render_ship(sc)
        for x in flagged + queue:                                        # everyone is drawn once, the rest still moored
            self.assertEqual(svg.count('data-id="%s"' % x["id"]), 1, x["id"])
        self.assertEqual(svg.count('class="sm-num"'), 5 + shipmate.MAX_ASTERN)
        self.assertEqual(svg.count('data-flag="1"'), 9)

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
                  self.hand("FISH", 3, crew="paused"), self.hand("FLAG", 1, crew="parked", last=T0 - 3600)]
        sc = shipmate.ship_scene(self.snap(agents))
        self.assertEqual({h["id"]: h["off"] for h in sc["hands"]},
                         {"PAUSED": True, "QUIET": True, "BUSY": False, "FRESH": False, "FISH": False, "FLAG": False})
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

    def test_name_tags_never_overlap(self):
        tags = shipmate.Tags()
        boxes = [tags.place("TASK-%d" % i, 100 + 3 * i, 120) for i in range(6)]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertTrue(a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3], (a, b))

    def test_drawing_has_a_hand_per_agent_and_a_boat_per_queue_item(self):
        snap = shipmate.demo_snapshot(T0)
        svg = shipmate.block_ship(snap)["ship"]
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        for a in snap["agents"]:
            self.assertEqual(svg.count('data-id="%s" data-stage=' % a["id"]), 1, a["id"])
            self.assertEqual(svg.count('<g class="sm-tag" data-for="%s"' % a["id"]), 1, a["id"])
        for q in snap["queue"][:shipmate.MAX_QUEUE_BOATS]:
            self.assertIn('class="sm-boat" data-id="%s"' % q["id"], svg)
        self.assertEqual(svg.count('class="sm-redflag"'), sum(1 for a in snap["agents"] if shipmate.captain_reasons(a)))
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

    def test_motion_respects_reduced_motion(self):
        css = shipmate.asset("shipmate.css")
        start = css.index("@media (prefers-reduced-motion: no-preference)")
        before = css[:start]
        self.assertNotRegex(before, r"\banimation\s*:")                 # every animation sits behind the media query

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
                      'class="sm-hand', 'data-zoom="all"', "Work by phase", 'class="sm-logcard"', "data-signs"):
                self.assertIn(h, page)
            self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))
            st, ship = get("/ship")
            self.assertEqual(set(json.loads(ship)), {"ship", "counts", "projects", "stages", "log"})
            st, det = get("/detail?id=DEMO-RATE")
            self.assertEqual(st, 200)
            self.assertIn("per user or per key?", det)
            self.assertEqual(get("/detail?id=DEMO-Q-BILL")[0], 200)
            self.assertEqual(get("/report?id=DEMO-RATE&f=md")[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
