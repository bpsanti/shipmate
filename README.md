# shipmate

A small local dashboard for a [Firstmate](https://github.com/kunchenguid/firstmate) home.
It shows one page, read live from the home's own records every time the page loads and every 30 seconds after that:

- **A board by phase** fills the page: the queue is the first lane (what waits and why, with a "+ Add task" button that queues a new task and notifies firstmate), then one lane per phase of the live workers: exploring, implementing / writing, PR open, waiting for CI, ready / delivered.
  A phase with nothing in it shrinks to a thin labelled strip so the busy lanes get the width; a lane taller than the screen scrolls inside itself and says how many cards are below; when lanes are out of view the board says so.
- **A fixed sidebar** on the right keeps today's tokens, firstmate's own session (context against the model's window, growth, compactions, and a warning past a threshold so you know when to clear it) and every Claude account's quota (tightest first: what is left, when it resets, when it runs out at the current pace) on screen while the board scrolls.
- **A detail drawer** opens from any card: facts about that piece of work, gathered from the home's records.
  When the item waits on the captain, the drawer opens with the decision and a place to answer it, which reaches firstmate as an inbox note; firstmate's reply comes back to the same place.
- **Usage** (tokens per day and per delivered task, new tokens apart from cache re-reads) is folded under the board.
- **The ship view** ("Ship" beside "Board" in the header) draws the same snapshot as one ship in pixel art, seen from the side and cut away: each agent is a hand whose place on board is its phase, and signs painted on the ship say what each place means. Whoever waits on the captain lines up in the captain's cabin, longest wait first, and a log below the scene lists them. Chips above it light one stage at a time. It zooms and pans, and the page remembers the choice of view in the browser.

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
| `--cache-seconds N` | `SHIPMATE_CACHE_SECONDS` | `5` | seconds one reading of the fleet (backlog, records, inbox receipts) answers every request; `0` reads again on each request |
| `--once` | | | print the JSON snapshot and exit |
| `--demo` | | | serve a made-up home with every phase, flag and gauge in use, instead of a real one (no `--home` needed) |
| `--lang en\|pt-BR` | `SHIPMATE_LANG` | `en` | the page's language: English, or Brazilian Portuguese with `pt-BR` |
| | `SHIPMATE_QUOTA_CMD` | `quota-axi` | quota command |
| | `SHIPMATE_GH_CMD` | `gh` | GitHub CLI used only by the detail drawer to read a PR's checks |

Reloading the page, the page's own refresh and the ship view share one reading of the fleet for `--cache-seconds` seconds instead of reading it again each time.
Sending an answer or a new task drops that reading, so what comes back right after is never stale.
While a tab is hidden the page pauses every animation where it stands (it resumes from the same frame) and asks for fresh data ten times less often, then refreshes as soon as the tab is shown again.

Transcripts are always read from `~/.claude` and from every directory the home lists in `config/claude-accounts`.

The language covers everything the page writes itself: labels, the board's lanes and reasons, the drawer, the sidebar, dates, and the ship view's signs, stages and log.
Text read from the home's records (task titles, status lines, hold reasons, notes) is shown as it was written.

`GET /state.json` returns the same snapshot the page is built from.
`GET /ship` returns the ship view drawn from that snapshot.
`GET /detail?id=<task>` returns one card's drawer, `POST /answer` sends the captain's answer to a waiting decision, and `GET /report?id=<task>&f=md|html` shows that task's `data/<task>/report.md` or `report.html` as a sandboxed document.

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
| Agent rows | `state/<id>.meta` (project, model, effort, account, worktree, spawn time; the account also colours a hand's neckerchief in the ship view), `state/<id>.status`, `bin/fm-crew-state.sh <id>` (cached 20 s), task titles from `bin/fm-tasks-axi.sh list`. |
| Agent tokens | The worker's Claude Code transcripts in the worktree's project folder, plus their subagent transcripts. Only messages after the task's spawn count, because a pooled worktree's folder also holds earlier tasks' sessions. Each message counts once (one message is written as several lines). |
| Queue | `bin/fm-tasks-axi.sh list` (held, hold date, blocked-by) and `config/project-capacity` with the live task records that hold a place. |
| Home lanes | `bin/fm-capacity.sh` (its first `capacity:` line), read at most every 20 s and only when the home has a `config/lane-capacity`; without that file the script sets no target and shipmate does not run it. A queued task nothing else holds waits for a lane (`home lanes n/n`) when it reports no free lane. |
| How long a queued task has waited for the captain | The `Captain hold set:` time that `bin/fm-captain-hold.sh` writes first in a held task's body, read with `bin/fm-tasks-axi.sh list --state queued --fields held,body` only while some queued task is held; without that line, the day the task was created. |
| How long a hand has waited for the captain | Its oldest `needs-decision` or `blocked` status line still open; a hand flagged by its crew state alone waits since its last transcript activity. |
| The captain's answers and what became of them | `bin/fm-inbox.sh receipts --all-pending --all-handled`, cached 10 s and read again right after an answer is sent: the notes whose request id starts with `shipmate-answer:`, whether firstmate was woken, whether it took the note from its inbox, and its reply. Every note is read, so an answer's state never drops out of a bounded page while its decision is open. |
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
Apart from the answer form described below, everything in it is read-only and comes from records, never from transcript text:

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

The ship view is drawn from the snapshot the board uses.
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
| A badge on a card in the log | the captain's answer to the decision open now: sent, not woken, received or replied (see below) |
| A hand sitting with the dice | off duty: an unflagged agent exploring, implementing, at the bow or of unknown phase whose crew state is `paused`, or that has had no transcript activity for 20 minutes without a busy session |
| A hand's shirt | its project's colour, in the order of `data/projects.md` (the bandana is a darker shade of it); the sails carry one stripe per project aboard |
| A hand's neckerchief | its Claude account's colour, in the order of `config/claude-accounts` (an account a worker reports that the list lacks comes after); "Accounts aboard" below the scene names them. A queued hand has no account yet, so its neckerchief is a dark shade of its shirt |
| A hand's hat | its state: a bandana at work, a straw hat when not working (off duty, or queued on the pier), bare-headed when it waits on the captain, a grey wool cap when its phase is unknown |
| A red flag over a hand | it needs the captain: the crew state is parked, blocked or failed, or a `needs-decision` or `blocked` status line has no `resolved` line with its key yet |
| The captain's cabin, in the stern castle | the captain's line: a flagged hand leaves its place and waits here, facing the desk, numbered by how long it has waited, the longest nearest the desk; a scout waits here with its map, and a queued task held for the captain's decision stands here with its sea bag at its feet (it is not aboard yet) |
| A crowd in the cabin door, with `+N` over it | the rest of the captain's line, when the cabin is full; clicking it (or Enter on it) brings their cards in the log into view, marks them and focuses the first, and one more click opens any of them |
| The captain | at the desk with a paper and a balloon saying how many wait and who has waited longest; sitting with a mug when nobody waits |
| A skiff on the horizon, rowing back with the map, moored at the pier | a scout exploring, writing the report, report delivered |
| Barrels in the hold | one per Claude account: the amber level is the quota left; red when it runs out before it resets or has less than 15% left |
| Water in the hold | firstmate's context; the white line is the warning threshold |
| Hands on the pier | the rest of the queue, one hand per queued task in its project's colour, doing what says why it waits (see below) |
| Signs painted on the ship | what each place means (lookout, deck, bow, rail, gangplank, hold, helm, cabin, and on the pier the slot each tavern waits for, ready, waiting for a task, asleep); the "signs" button puts them away, and the browser remembers it |
| Crates on the pier | tasks delivered in the last 7 days (the session sheets) |
| Day or night | the local hour: night from 18:00 to 06:00 |
| Calm, choppy or rough sea | the machine's one-minute load per CPU: under 0.6, under 1.0, above |

The scene is larger than its frame and grows with the crew so no hand stands on another.
The cabin is the exception: it has one size, so the ship never grows with the captain's line.
Three wait inside, 20 art pixels apart between the desk and the door; a fourth would need a longer stern castle, since a hand with a fishing rod or signal flags is up to 26 pixels wide.
When wide props leave no room for the third, two wait inside, and the line keeps its order: everyone after the first who does not fit is in the crowd at the door.
It opens framed on the deck and its crew; the wheel or a pinch zooms, dragging moves, a double click zooms in there, and the buttons zoom in and out, frame the crew again, or fit everything.
Names show from a readable zoom up, never overlap, and come back on hover or focus when zoomed out.
Clicking a hand, on board, in the cabin or on the pier, opens the same drawer as its card.
The chips above the scene name each stage, say where its hands stand and who is in it now, and count them; clicking one lights those hands, fades the rest and takes the view to them, and clicking it again lets go.
The log below the scene is the captain's line as cards, longest wait first, with why each one waits and for how long; pointing at a card lights its hand (or the crowd at the cabin door it is in), and clicking it opens its drawer.
The sky, the ship and the near layer move at different speeds as you pan; the ship bobs, red flags blink, hammers and flags move, and lamps flicker, all only when the browser does not ask for reduced motion.

### The queue on the pier

Every queued task that is not held for the captain's decision is a hand on the pier (those wait in the captain's cabin), and what it does there is decided by the first reason the queue gives for it, nothing else:

| On the pier | Reason in the queue |
| --- | --- |
| Playing cards at a table at the tavern, at the foot of the gangplank, to its right (three to a table: one each side, one standing behind) | waits for a build slot (`config/project-capacity`) or a home lane (`bin/fm-capacity.sh`), and was created today |
| Drinking from a mug on the tavern bench, by the keg | the same wait, created on an earlier day: it has waited longer |
| Standing at the foot of the gangplank with a sea bag | nothing holds it: firstmate can dispatch it |
| Standing under a hand aboard, talking up to it, with a balloon and a dotted line to that hand | it waits for that task (`blocked-by`), and that task is aboard; it stands as close below it as the others allow |
| Standing apart, its tag naming what it waits for | it waits for a task that is not aboard (still queued, or gone) |
| Asleep on the bench under the forecastle, with its date | held until a date that has not come |

Each tavern has its sign with the slot it waits for (`SLOT <project> · 2/2 taken · 13 waiting`, or the home's lanes), and the other groups have theirs (ready, waiting for a task, asleep).
The tavern takes four tables and one bench of eight; the rest are counted (`+N`, with their ids on hover), and so are lines past eight.
The tavern is anchored at the foot of the gangplank, on its right, and grows to the right, so a short queue adds only a short stretch and nothing is reserved west of the ship.
The tavern's names show only on hover or focus, so a full table stays readable; the others keep their names on.
The ship moves over to make room for the tavern, and the moored scouts and crates move toward the bow past the queue.
The four stage chips for the pier (waiting for a slot, ready to board, waiting for a task, held until a date) light their hands like the others and are left out when empty.
A queued hand never takes a working pose: the cards on a table blink, a drinker lifts the mug now and then, a talker's balloon comes and goes, a sleeper breathes; nothing there looks like progress, and none of it moves when the browser asks for reduced motion.
Every hand, on board or on the pier, and every hand in a chip is painted by one function, `hand_sprite`, from the poses in `POSES` (with `POSE_HEAD`, where each pose puts the head), the hats in `HATS` and the colours in `hand_palette`, so a new art style replaces those and nothing else.
A hand is drawn on a grid 13 pixels wide and 21 tall at the ship's own pixel size, seen three-quarters so both eyes show; its skin stays one flat colour, so the face reads at any zoom, and its props (a spyglass, a hammer, flags, a rod, a pack, a map, cards, a mug) make some poses wider.
The captain and firstmate at the helm are drawn on the same scale.

## Answering the captain's decisions

The drawer of an item that waits on the captain opens with "Your answer": a hand or scout with an open `needs-decision` or `blocked` line (one form per open line), one flagged by its crew state alone, or a queued task held for the captain's decision.
Each form quotes the decision, offers its ready options when the line lists them as `A) .. B) ..` (also `(a) ..` or `1) ..`), and takes words of the captain's own; an option, words, or both.
"Send to firstmate" runs, with `FM_HOME` set:

```bash
bin/fm-inbox.sh note --request-id shipmate-answer:<form id>:<task> --json -- "<note>"
```

The note, in English whatever the page's language, names the task and its title, quotes the decision with its key and time, says how to reply, and ends with the answer after an `Answer:` line.
The form id is drawn when the drawer is drawn, so sending the same form again, after a double click or a failed request, gives back the note it already made; fm-inbox keeps one note per request id.
A half-written answer keeps its words, its choice and its focus through the 30-second refresh.

Below the forms, every answer sent about the item shows what became of it, read from `bin/fm-inbox.sh receipts`:

| State | Meaning |
| --- | --- |
| sent | the note is saved and firstmate was woken (`announced`) |
| not woken | the note is saved but the wake failed (fm-inbox exit 3); "Wake firstmate" runs `bin/fm-inbox.sh announce --json <note>`, only for a note shipmate sent as an answer |
| received | firstmate took the note from its inbox (`handled/`) |
| replied | firstmate recorded its answer with `bin/fm-inbox.sh reply <note> <text>`, shown under the captain's words |

The card of a waiting item, in the board and in the ship's log, carries the state of the latest answer sent since the item began to wait, so an answer to an earlier decision never marks a new one.
shipmate only delivers the answer: firstmate acts on it under its usual rules, so a merge or a destructive action still needs the captain's explicit word on that PR.
`POST /answer` and `POST /announce` take the same guards as the add form below.

## Adding a task

"+ Add task" at the top of the queue lane opens the form in the drawer, beside where the task will appear.
The form runs, with `FM_HOME` set:

```bash
bin/fm-tasks-axi.sh add "<title>" --mint --repo <project> --queue --json [--body "<notes>"]
bin/fm-inbox.sh note --request-id shipmate-<form id> --json -- "New task queued from shipmate: <id> ..."
```

The project list comes from `data/projects.md`.
Only `POST` from `127.0.0.1` with this server's `Host`, a matching `Origin` when the browser sends one, no cross-site `Sec-Fetch-Site`, and the per-run token embedded in the page is accepted.
A resubmitted form (same hidden request id) never queues twice.

## Tests

```bash
./shipmate.test.sh
```

The tests build a throwaway home, fake `bin/` scripts, a fake quota command, and tiny synthetic transcripts; they never read a real home or a real transcript.
