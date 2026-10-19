# Assistant

The assistant is an LLM that Noctyx asks for plain-text help: it triages firing alerts, explains hygiene findings,
and writes the dashboard's morning brief. It is usually one of the cluster's own model servers. You point it at
any OpenAI-compatible endpoint. Every prompt is built only from data the console already holds, and every answer
is shown with the name of the model that wrote it. The assistant only produces text. Nothing it says is run.

## Configuring the endpoint

From the CLI:

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
| `model` | Served model id to ask for. If empty, requests carry no model id. | Letters, digits and `. _ : / @ + -`, up to 128 characters |
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
`{"kind": "hygiene", "host": "...", "id": "..."}`, and `GET /api/v1/digest[?refresh=1]`.

How requests are made:

- **Order.** Noctyx tries the configured `url` first. With `fallback` on, it then tries the other discovered
  servers in turn and keeps the first non-empty answer.
- **Request settings.** Requests use temperature 0.2 and ask the server to turn thinking off
  (`chat_template_kwargs.enable_thinking: false`). They are capped at 220 to 420 tokens.
- **System prompt.** It tells the model to use only the data given, to say "not in the data" instead of guessing,
  and never to recommend rebooting a node that serves a model unless the data requires it.

## Who may use it

With console accounts (see [Accounts](accounts.md)):

| Action | Role |
|---|---|
| Read triage, explanations and the brief; refresh the brief | viewer |
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
- **test:** only a fixed test prompt.

No snapshot contents, passwords or repository keys are sent. Host names, model names and alert text are, and a
hygiene finding's detail can include paths on that host.

The API key goes as a bearer token to the configured endpoint. With `fallback` on, a discovered server gets its own
key file `<llm_keys>/<host>` if one exists, else the assistant key. Turn `fallback` off to keep the key and the data
on a single endpoint.
