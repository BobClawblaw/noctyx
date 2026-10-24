# Assistant

The assistant is an LLM that Noctyx asks for plain-text help: it triages firing alerts, explains hygiene findings,
writes the dashboard's morning brief, and answers questions in the console's **Ask** drawer, looking things up
first. It is usually one of the cluster's own model servers. You point it at any OpenAI-compatible endpoint. Every
prompt is built only from data the console already holds, and every answer is shown with the name of the model
that wrote it. In the console the assistant changes nothing: it reads and writes text. The one place it may
change something is the web setup, and there only what you approve runs.

It is also the first thing a new site sets up: during the install it explains what went wrong, on Linux and
on macOS alike.

## Set up first, by `noctyx init`

`sudo noctyx init` asks for the assistant before anything else, so that it can help with the rest of the install:

1. **Find one.** It looks for OpenAI-compatible servers on the usual ports (30000, 8000, 8080, 8888, 9501–9503, 11434,
   1234, 5000) of `127.0.0.1`, the head's own LAN address, and any machines you name, and lists those whose
   `/v1/models` serves a model, fastest first. A site's configured endpoint is always listed first. Pick a
   number, type a URL (`http://host:port/v1`), or answer `none` (setup works without one; it just cannot explain
   failures).
2. **A key**, only if the server wants a bearer token: typed at a hidden prompt, or in `NOCTYX_ASSISTANT_KEY` for
   `init --yes`. It goes to `<llm_keys>/assistant` (0600), never on a command line.
3. **Test it.** One short completion (*Reply with exactly: Noctyx assistant OK*). A server that serves several
   models asks which one.
4. **Save it.** On a new site the config is written right away with just this block, so `noctyx ask` works
   before the rest of `init` is done. `init` writes `timeout_seconds: 120`.

`sudo noctyx init --assistant-only` does just this step (on a new or an existing site). `--assistant URL [--model
ID]` picks without questions. A `--yes` re-run that finds the configured endpoint not answering keeps it (it
answered before and may be restarting) instead of failing; `noctyx setup verify` tests it each time and reports
a failure as a warning.

### Help during the install

**In a browser** (`sudo noctyx init --web`), the assistant does more than explain: it probes machines, reads
the guide and proposes each change (the head, the storage, sign-in, a node, a setup step, an agent or metrics
install) for you to edit and approve. Only what you approve runs, through the same code as `noctyx init` and
`noctyx setup`. It never sees a password. See [Installing: in a browser](install.md#in-a-browser-noctyx-init---web).

**In the terminal**, once it answers, the assistant helps in three places:

- **Any question in `init`** takes `?`: the question, its default, what it is for and this machine's facts go
  to the assistant, which says what to enter and recommends a value.
- **A failing setup step** (`noctyx setup <step>`, `setup all`, `setup verify`) sends the error, this machine's
  facts and, when Noctyx runs from a checkout, the install and troubleshooting guides; the answer (at most 8
  lines, commands in backticks) is printed under the failure. It is told never to suggest turning off a
  firewall, TLS verification or another protection, and never to install restic or rest-server by hand
  (`noctyx setup tools` does). `NOCTYX_NO_ASSIST=1` turns this off; a failing step never fails harder because
  the assistant is down.
- **`noctyx ask QUESTION`**, or text piped in (`journalctl -u noctyx-console -n 50 | noctyx ask`), any time.

The facts are the platform and release, CPU architecture, Python version, whether it runs as root, whether the
site config exists, where restic and rest-server live, and the service manager: systemd units and `journalctl`
on Linux, launchd daemons (`/Library/LaunchDaemons/<launchd_prefix>.<name>.plist`, logs in `/var/log/noctyx/`)
on macOS. Setup help asks only the configured endpoint, not the fallback servers.

## Configuring the endpoint

Besides `init`, from the CLI:

```sh
sudo noctyx assistant-set --json '{
  "enabled": true,
  "url": "http://192.0.2.21:8000/v1",
  "model": "example/model-27b",
  "timeout_seconds": 60,
  "fallback": true
}'
```

| Key | Meaning | Rule / default |
|---|---|---|
| `enabled` | Use the assistant at all. Off unless set to `true`: nothing is sent to an LLM until you turn it on. | `true` / `false` |
| `url` | OpenAI-compatible base URL, ending in `/v1`. Noctyx calls `<url>/chat/completions`. | `http(s)://host:port/v1`. Empty means discovered endpoints only. |
| `model` | Served model id to ask for. If empty, the first model the endpoint lists at `/v1/models`. | Letters, digits and `. _ : / @ + -`, up to 128 characters |
| `timeout_seconds` | Per-request timeout | 5..600, default 60 |
| `fallback` | When `url` does not answer (or is unset), try every other LLM server Noctyx discovered serving (vLLM, SGLang, llama.cpp, MTPLX, TensorFold) | default `true` |
| `key_host` | Name of a key file in the `llm_keys` directory (`/etc/noctyx/llm-keys`). The key itself never passes through the command. | CLI only; `""` clears it |

Noctyx refuses `enabled` with no `url` and `fallback: false`, because there would be nothing to ask.

The command rewrites only the `assistant` block of the site config and changes only the keys you send:
`{"enabled": false}` switches the assistant off and keeps its endpoint. Send `"url": ""` or `"model": ""` to
clear one.

### From the console

Admins change the same settings under **Configuration › Assistant LLM** (`/config#sec-assistant`):

- **Use the assistant**, **Endpoint**, **Model**, **Timeout** and **Fallback**. The endpoint picker lists every
  serving model Noctyx sees, plus each recipe slot's port.
- **test** (operator) sends one short request with the values on screen, before you save. It reports the answer,
  the time it took and the models the endpoint serves.
- **save assistant** (admin) runs `assistant-set`.
- **API key** (admin) stores a bearer key root-only (mode 0600) in `<llm_keys>/assistant` and sets `key_host` to
  `assistant`. **clear key** removes it. The key is never shown, returned or logged.

## What it does

| Feature | When it runs | Where it shows | Cached |
|---|---|---|---|
| **Alert triage** | In the background, for each alert Alertmanager posts as firing | Under the alert on the dashboard, in 2-3 sentences | Kept in `alerts.jsonl` in the console state dir |
| **Explain** a hygiene finding | When someone clicks **explain** next to a finding | Under the finding, in 3-5 sentences | Stored with the finding in `hygiene.json` |
| **Morning brief** | When the Morning Brief panel loads (it polls every 10 minutes) or someone clicks **refresh** | Dashboard › Morning Brief, 5-8 lines | `digest.json`, regenerated after 1 hour. If a regeneration fails, the last brief is shown with the error. |

The console state dir is `paths.console_state`, `/var/lib/noctyx-console` by default. All three are also available
over the API: `GET /api/v1/alerts` (with triage), `POST /api/v1/explain` with
`{"kind": "hygiene", "host": "...", "id": "..."}`, and `GET /api/v1/digest[?refresh=1]` (a refresh writes a new brief, so it needs an operator).

How requests are made:

- **Order.** Noctyx tries the configured `url` first. With `fallback` on, it then tries the other discovered
  servers in turn and keeps the first non-empty answer.
- **Request settings.** Requests use temperature 0.2 and ask the server to turn thinking off
  (`chat_template_kwargs.enable_thinking: false`). They are capped at 220 to 420 tokens.
- **System prompt.** It tells the model to use only the data given, to say "not in the data" instead of guessing,
  and never to recommend rebooting a node that serves a model unless the data requires it.

## Ask Noctyx

**Ask** in the console's header opens a drawer beside the dashboard. Type a question about the site
(*did anything fail last night?*, *which GPU ran hottest today?*, *when does the NAS fill up?*) and the
assistant looks the answer up before it writes it. Each lookup shows as a line you can open to see exactly
what it read. Answers give temperatures in your display unit. **New** starts a new conversation; the drawer
keeps the current one while the tab stays open.

It works through read-only tools that read what the panels read:

| Tool | What it reads | Role |
|---|---|---|
| `cluster_now` | every host's live numbers, each LLM server's load and speed, the fleet's 24-hour totals | viewer |
| `prometheus`, `metric_names` | any PromQL query, now or over up to 7 days: at most 20 series, each with its min and max (and when) from up to 1,500 samples, its last value and 40 points | viewer |
| `host_details` | a host's site config and facts | viewer |
| `alerts`, `backups`, `storage`, `hygiene`, `versions`, `recipes`, `audit` | the panels of the same names | viewer |
| `repairs` | what `noctyx heal` found, repaired on its own or left to a person, and the changes ledger (each upgrade: ok or rolled back, and why) ([self-repair](self-repair.md)) | viewer |
| `snapshots`, `find_in_backups` | a host's snapshots, and files in them by name or glob | viewer |
| `run_log` | the end of a backup, verify, prune or deploy run's log | operator |
| `containers`, `service_log`, `container_log` | a Linux node's containers, a unit's journal, a container's log (over SSH) | operator |
| `read_guide` | this guide | viewer |

How it behaves:

- **It changes nothing.** When something should change, it says what and where (the page and the button, or the
  `noctyx` command), and you do it.
- **Arguments are checked first.** Host names must be in the site, unit and container names must be plain
  names, a run's log is named by its unit, and what goes to a node over SSH is quoted.
- **Secrets are redacted.** Keys, tokens, passwords (in URLs too) and private keys are cut out of what a tool
  returns before the model sees it.
- **Tool output is data, not instructions.** The system prompt tells the model never to follow text it reads in a
  log or a file name.
- **Without tool calls.** A model whose server refuses the tools API (HTTP 400) still answers, from its system
  prompt only, and the drawer says so.
- **Twelve rounds per question.** On the last round the model is told to answer from what it has found.
- **What it is told.** The system prompt names each machine, what it is and what it serves (each recipe slot's
  port is asked once as the conversation begins, so a model swapped in by hand shows as itself, and its
  tensor-parallel pair), the console's pages and buttons it may
  point at, and the tools your role does not give it, so it can say who could ask instead. It may name a `noctyx`
  command only when a tool showed it or the guide documents it, and it answers questions about this site only.
- **Times.** Each question goes to the model with the time it was asked, in UTC and in your browser's time zone,
  and answers give times in your zone. The system prompt itself stays the same for the whole conversation, so a
  server with a prompt cache does not read it and the tools again on every round.

A conversation belongs to whoever opened it: another user's conversation id is refused. Each message uses your
role as it is at that moment. Conversations live in the console's memory: a restart ends them, as does six hours
without use. The API is `POST /api/v1/ask` with `{"text": "...", "sid": "..."}` (no `sid` starts a new
conversation and returns one; `tunit` is `C` or `F`, `tz` an IANA time zone such as `Europe/Paris`) and
`GET /api/v1/ask/<sid>?since=<rev>`.

### Measuring a change to it

`tools/ask_eval.py` asks the site's real assistant 21 fixed questions, each in a new conversation with the
console's tools on the live data, and scores every answer: the first lookup is one the question calls for, each
`noctyx` command it writes parses with Noctyx's own command line, it has the words it must (and none it must
not), and each number it gives is found in what the tools returned (in °F, GB or % as well). Run it on the head as
the user the console runs as, before and after a prompt change; `PYTHONPATH` picks the checkout it measures:

```
NOCTYX_CONFIG=/etc/noctyx/site.json PYTHONPATH=src python3 tools/ask_eval.py --tz Europe/Paris --out /tmp/ask.json
```

The questions name machines by role (the head, a tensor-parallel pair, a Mac), so they fit any site; one whose
machine the site lacks is skipped. The report holds the site's data: keep it out of the repository.

## Who may use it

With console accounts (see [Accounts](accounts.md)):

| Action | Role |
|---|---|
| Read triage, explanations and the brief; refresh the brief | viewer |
| **Ask**, with the viewer's tools | viewer |
| **Ask** with the log tools (`run_log`, `containers`, `service_log`, `container_log`) | operator |
| **explain** a finding; **test** an endpoint | operator |
| Save the assistant settings; set or clear the API key | admin |

In open mode (`console_auth` off) anyone on the network may do all of these. In token mode, reading is open and the
other actions need the console's bearer token.

## Privacy

Everything below goes over HTTP to the configured endpoint, and with `fallback` on to any discovered LLM server.
Use an `https://` URL if the endpoint is off the trusted network.

- **Alert triage:**
  - the alert's name, severity, host, summary and start time;
  - that host's current metrics (CPU, memory, GPU utilisation and temperature, root filesystem use, failed units,
    the reboot flag, up/down);
  - the stats of its LLM servers (model, running and waiting requests, KV cache, tokens/s).
- **Explain:**
  - the host name, its OS lines and architecture, and whether it serves an LLM;
  - the finding's title, severity, detail and fix tier.
- **Morning brief:**
  - backup health per host and profile, and the active alerts;
  - per-node state (up, GPU, root filesystem, failed units, the reboot flag);
  - LLM server stats and the last 24 hours' token, request and GPU energy totals;
  - NAS free space and RAID state, and the titles of open hygiene findings.
- **Ask:** your questions and, redacted, what each lookup returned (live numbers, query results, facts, alerts,
  backup state, file names from snapshots and, for operators, log lines).
- **test:** only a fixed test prompt.
- **Setup help** (`init`'s `?`, a failing setup step, `noctyx ask`): the question or the error text (the last
  3000 characters), the machine facts listed [above](#help-during-the-install), its address and host name (for
  `?`), and the install and troubleshooting guides when Noctyx runs from a checkout. Errors can include paths and
  addresses on the head.

No snapshot contents, passwords or repository keys are sent. Host names, model names and alert text are, and a
hygiene finding's detail can include paths on that host.

The API key goes as a bearer token to the configured endpoint. The assistant key only ever goes to that
endpoint. With `fallback` on, a discovered server gets its own key file `<llm_keys>/<host>` if one exists, else
no key. Turn `fallback` off to keep the data on a single endpoint.
