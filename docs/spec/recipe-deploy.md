# Recipe deploy spec — `[deploy]` in recipe.toml (draft v0)

Status: draft for comment. License of this document: CC-BY-4.0 — anyone may implement it.

A recipe is a repository that serves one model on specific hardware. This section tells any deployer
(Noctyx, a script, a person) how to build it, start it, stop it and know it is healthy, without
reading the README.

```toml
[deploy]
spec = 0                          # this document's version

# What it runs on
hardware = "gb10"                 # gb10 | sm_120 (RTX 5090 / RTX PRO Blackwell) | sm_89 | apple-m | cpu
nodes = 2                         # machines; 2 = a TP pair (rank 0 on the head, rank 1 on the worker)
memory_gib = 110                  # per node, at the default settings

# How to drive it (run in the checkout, as the deploying user)
prepare = "python3 -m spark image"   # optional; MUST NOT use the GPU: it runs while another model serves
start = "python3 -m spark up"        # returns when serving (or exits non-zero)
stop = "python3 -m spark down"       # idempotent; frees the GPU on every node
start_timeout_s = 3600

# How to tell it is healthy (HTTP on the serving port)
port_env = "PORT"                 # the deployer sets the port through this variable
health = "/health"                # 200 + {"status": "ok"} when ready
models = "/v1/models"             # OpenAI-compatible list; the served ids
served = ["Qwen3.8-Flash-Next-INT4-Mixed"]

# The weights it serves, pinned (a deployer may pre-fetch them, e.g. from a local mirror)
[[deploy.weights]]
repo = "Minachist/Qwen3.8-Flash-Next-INT4-Mixed-AutoRound"
revision = "f6f90ddc39e9d212aa5b96f43999183deb4ce192"

# What the site must supply (written to ./.env, or the recipe's own config file)
[deploy.settings.WORKER]
description = "ssh login of the worker node (rank 1)"
required = true
[deploy.settings.HCA]
description = "RDMA device(s) for NCCL"
example = "rocep1s0f1"
```

## Rules

1. `start` and `stop` are idempotent. `stop` on a stopped recipe exits 0.
2. `prepare` never touches the GPU and never writes into files a running server reads.
3. `start` reads settings from the environment (or `.env` in the checkout); it never prompts.
4. A recipe that pins weights pins a commit revision, not a branch.
5. The health endpoint answers `{"status": "ok"}` only when `models` lists every id in `served`.

## Open questions

- Multi-profile recipes (e.g. one stream vs sixteen) — `[deploy.profiles.<name>]` overrides?
- Declaring measured speed (repo + commit + hardware → a leaderboard result id)?
- Recipes with more than two nodes: naming each node's role.
