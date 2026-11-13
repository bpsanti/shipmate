# shipmate

A small local dashboard for a [Firstmate](https://github.com/kunchenguid/firstmate) home.
It shows one page, read live from the home's own records every time the page loads and every 30 seconds after that:

- **A board by phase** fills the page: the queue is the first lane (what waits and why, with a "+ Add task" button that queues a new task and notifies firstmate), then one lane per phase of the live workers: exploring, implementing / writing, PR open, waiting for CI, ready / delivered.
  A phase with nothing in it shrinks to a thin labelled strip so the busy lanes get the width; a lane taller than the screen scrolls inside itself and says how many cards are below; when lanes are out of view the board says so.
- **A fixed sidebar** on the right keeps today's tokens, firstmate's own session (context against the model's window, growth, compactions, and a warning past a threshold so you know when to clear it) and every Claude account's quota (tightest first: what is left, when it resets, when it runs out at the current pace) on screen while the board scrolls.
- **A detail drawer** opens from any card: read-only facts about that piece of work, gathered from the home's records.
- **Usage** (tokens per day and per delivered task, new tokens apart from cache re-reads) is folded under the board.
- **The ship view** ("Ship" beside "Board" in the header) draws the same snapshot as one ship in pixel art, seen from the side and cut away: each agent is a hand whose place on board is its phase. It zooms and pans, and the page remembers the choice of view in the browser.

Below 1400 px of width the sidebar becomes a strip of small gauges above the board, so it never squeezes the lanes; on a phone it becomes a one-line summary that opens the full blocks, the drawer takes the whole screen, and the board scrolls sideways one lane at a time.

It looks like the Firstmate bearings board, without the board's editor around it.
It is one Python file, a template, and a stylesheet: standard library only, no build step, no web fonts, no CDN.

## Run it

```bash
FM_HOME=/path/to/your/firstmate-home python3 shipmate.py
# shipmate: http://127.0.0.1:8737/
```

Python 3.9 or newer.
It listens on `127.0.0.1` only.

| Option | Environment | Default | Meaning |
| --- | --- | --- | --- |
| `--home DIR` | `FM_HOME` | none, one is required | the Firstmate home to read |
| `--fm-root DIR` | `FM_ROOT` | the home | the Firstmate code root that holds `bin/` |
| `--claude-dir DIR` (repeatable) | `SHIPMATE_CLAUDE_DIRS` (path-separator list) | none | extra Claude config directories to read transcripts from |
| `--port N` | `SHIPMATE_PORT` | `8737` | TCP port |
| `--context-limit N` | `SHIPMATE_CONTEXT_LIMIT` | inferred | firstmate's context window in tokens |
| `--context-warn P` | `SHIPMATE_CONTEXT_WARN` | `60` | warn when firstmate's context passes P percent of the window |
| `--once` | | | print the JSON snapshot and exit |
| `--demo` | | | serve a made-up home with every phase, flag and gauge in use, instead of a real one (no `--home` needed) |
| | `SHIPMATE_QUOTA_CMD` | `quota-axi` | quota command |
| | `SHIPMATE_GH_CMD` | `gh` | GitHub CLI used only by the detail drawer to read a PR's checks |

Transcripts are always read from `~/.claude` and from every directory the home lists in `config/claude-accounts`.

`GET /state.json` returns the same snapshot the page is built from.
`GET /ship` returns the ship view drawn from that snapshot.
`GET /detail?id=<task>` returns one card's drawer and `GET /report?id=<task>&f=md|html` shows that task's `data/<task>/report.md` or `report.html` as a sandboxed document.

### Keep it running (optional)

On macOS a LaunchAgent can keep it up.
Nothing installs one for you; to add it, save a file like this as `~/Library/LaunchAgents/local.shipmate.plist`, with your own paths, then run `launchctl load ~/Library/LaunchAgents/local.shipmate.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.shipmate</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/shipmate/shipmate.py</string>
    <string>--home</string><string>/path/to/your/firstmate-home</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

The `PATH` must reach `tasks-axi`, `quota-axi`, `jq`, and whatever else the home's `bin/` scripts call.
`launchctl unload` with the same path stops it.
On Linux a `systemd --user` service with the same command does the same job.

## Where each number comes from

| On the page | Source |
| --- | --- |
| Firstmate session | The session id in the home's `state/.lock-session` (written by `bin/fm-lock.sh` for the session that holds the firstmate lock), looked up in the Claude project folder of `FM_HOME` in every Claude directory read; without a usable id, the newest session in that folder, and the page says so. Context is input plus cache writes plus cache reads of the last model call. Growth compares it with the last call before the past 3 hours, or with the size right after a compaction when one happened since. Today's tokens include that session's subagents. Compactions are the transcript's `compact_boundary` records, with their trigger and the size before and after. |
| Account bars | `quota-axi --provider claude --full --json --no-credential-refresh`, with `QUOTA_AXI_CLAUDE_PROFILES` set to the non-`ordinary` entries of `config/claude-accounts`, cached 60 s. The bar is the `all_models` effective remaining percentage; the reset is the limiting window's; the projection is quota-axi's runway. Account e-mail and identity are never shown; an account is named by its config directory. |
| Agent rows | `state/<id>.meta` (project, model, effort, account, worktree, spawn time), `state/<id>.status`, `bin/fm-crew-state.sh <id>` (cached 20 s), task titles from `bin/fm-tasks-axi.sh list`. |
| Agent tokens | The worker's Claude Code transcripts in the worktree's project folder, plus their subagent transcripts. Only messages after the task's spawn count, because a pooled worktree's folder also holds earlier tasks' sessions. Each message counts once (one message is written as several lines). |
| Queue | `bin/fm-tasks-axi.sh list` (held, hold date, blocked-by) and `config/project-capacity` with the live task records that hold a place. |
| Machine load | `os.getloadavg()` and the CPU count, read with every snapshot; only the ship view's sea uses it. |
| Per day | Every transcript under the Claude directories, by local day, each message once. `workers` are sessions launched from a Firstmate brief, `firstmate` is the home's own folder, `validation` is no-mistakes, `other` is the rest. |
| Per delivered task | The home's session sheets (`data/fichas/fichas.jsonl`), with the new/cache split recomputed from each sheet's session transcript and its subagents. |

New tokens are input plus cache writes plus output; cache reads are shown apart.

## The context window

Claude Code transcripts do not record the model's context window, so shipmate cannot read it.
Set `--context-limit` (or `SHIPMATE_CONTEXT_LIMIT`) to the real one.
Without it, the window is inferred and labelled `inferred`: 1M when the model id carries `[1m]` or when the session has already held more than 200k tokens (in a call or before a compaction), otherwise 200k.
A 1M session that has never gone past 200k therefore shows against 200k until you set the limit, which overstates how full it is rather than understating it.

## Phases

A phase is derived from recorded milestones, never from what the agent wrote in prose.
When the evidence cannot tell, the page says `unknown`.

| Ship phase | Evidence |
| --- | --- |
| exploring | a session transcript exists, with no edit inside the worktree yet |
| implementing | first `Edit`/`Write` on a file inside the worktree, or a `git commit` |
| PR open | `pr=` in the task record, a PR URL in a status line, or a `gh pr create` result |
| waiting for CI | PR open, the worker is working or paused, and its latest activity since its last edit is a CI read (`gh pr checks`, `gh run watch/view/list`) |
| ready to merge | PR open and current state `done` |

A scout goes exploring, writing the report (`data/<id>/report.md` exists), report delivered (`done`).
The bar under each agent shows the phases completed; hover `why?` for the evidence used.

## The detail drawer

Clicking a card (or Enter on a focused card) opens a drawer; Escape or the close button shuts it.
Everything in it is read-only and comes from records, never from transcript text:

| In the drawer | Source |
| --- | --- |
| Title and the captain's intent | the backlog title, and the `Captain's intent` section of `data/<task>/brief.md` |
| Phase timeline with the time of each milestone | the same evidence as the card's phase: spawn time, first edit or commit in the worktree, PR URL, first CI read, `done` |
| Latest status lines | the last 8 lines of `state/<task>.status`, newest first |
| PR with its state and checks | `gh pr view <url> --json state,isDraft,statusCheckRollup`, read only when a drawer opens and at most every 120 s per PR (GitHub PRs only) |
| Project, model, effort, account, harness | `state/<task>.meta` |
| Tokens, and the worker's context against its window | the task's transcripts, as on the card; the window is inferred like firstmate's |
| How long it has been idle | the time since the newest record in the task's transcripts |
| Report link | `data/<task>/report.md` or `report.html` when present |

A queue card's drawer shows the item from `bin/fm-tasks-axi.sh show <task> --full`: why it waits, its dependencies and its notes.

## The ship view

The ship view reads nothing new: it is drawn from the snapshot the board uses.
Each element stands for one thing:

| In the picture | What it is |
| --- | --- |
| The ship | the home; firstmate stands at the helm on the stern castle |
| A hand in the crow's nest with a spyglass | an agent exploring |
| A hand on deck with a hammer | an agent implementing |
| A hand at the bow with signal flags | an agent with a PR open |
| A hand fishing at the rail | an agent waiting for CI |
| A hand on the gangplank with a pack | an agent ready to merge |
| A hand under a question mark | an agent whose phase is unknown |
| A hand sitting with the dice | off duty: an unflagged agent exploring, implementing, at the bow or of unknown phase whose crew state is `paused`, or that has had no transcript activity for 20 minutes without a busy session |
| A hand's shirt and bandana | its project's colour, in the order of `data/projects.md`; the sails carry one stripe per project aboard |
| A red flag over a hand | it needs the captain: the crew state is parked, blocked or failed, or a `needs-decision` or `blocked` status line has no `resolved` line with its key yet |
| A skiff on the horizon, rowing back with the map, moored at the pier | a scout exploring, writing the report, report delivered |
| Barrels in the hold | one per Claude account: the amber level is the quota left; red when it runs out before it resets or has less than 15% left |
| Water in the hold | firstmate's context; the white line is the warning threshold |
| Boats moored at the pier | the queue; a red flag marks one held for the captain's decision |
| Crates on the pier | tasks delivered in the last 7 days (the session sheets) |
| Day or night | the local hour: night from 18:00 to 06:00 |
| Calm, choppy or rough sea | the machine's one-minute load per CPU: under 0.6, under 1.0, above |

The scene is larger than its frame and grows with the crew so no hand stands on another.
It opens framed on the deck and its crew; the wheel or a pinch zooms, dragging moves, a double click zooms in there, and the buttons zoom in and out, frame the crew again, or fit everything.
Names show from a readable zoom up, never overlap, and come back on hover or focus when zoomed out.
Clicking a hand or a queued boat opens the same drawer as its card.
The sky, the ship and the near layer move at different speeds as you pan; the ship bobs, red flags blink, hammers and flags move, and lamps flicker, all only when the browser does not ask for reduced motion.

## Adding a task

"+ Add task" at the top of the queue lane opens the form in the drawer, beside where the task will appear.
The form runs, with `FM_HOME` set:

```bash
bin/fm-tasks-axi.sh add "<title>" --mint --repo <project> --queue --json [--body "<notes>"]
bin/fm-inbox.sh note --request-id shipmate-<form id> --json -- "New task queued from shipmate: <id> ..."
```

The project list comes from `data/projects.md`.
Only `POST` from `127.0.0.1` with this server's `Host`, a matching `Origin` when the browser sends one, and the per-run token embedded in the page is accepted.
A resubmitted form (same hidden request id) never queues twice.

## Tests

```bash
./shipmate.test.sh
```

The tests build a throwaway home, fake `bin/` scripts, a fake quota command, and tiny synthetic transcripts; they never read a real home or a real transcript.
