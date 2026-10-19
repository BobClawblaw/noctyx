<p align="center"><img src="docs/assets/noctyx-banner.svg" alt="Noctyx — the night-shift operator for your AI cluster" width="100%"></p>

# Noctyx

The night-shift operator for your AI cluster. One console that **deploys** model recipes safely,
**caches** every model locally, **protects** your machines and data, and **watches** all of it with an
LLM that explains what it sees. Built for DGX Spark pairs, RTX workstations, NAS-backed home labs and
small inference clusters.

> **Status: alpha (0.1).** Extracted from a single-site implementation that has run a production GPU
> cluster since 2026-09; that cluster now runs on Noctyx. See [docs/PLAN.md](docs/PLAN.md).

| | |
|---|---|
| **Deploy** | Build the next recipe while the current one serves; switch under the GPU lock; roll back automatically. Discover public recipes for your hardware, ranked by measured speed |
| **Cache** | One copy of every Hugging Face model on the cluster, served back as `HF_ENDPOINT`; keeps working when the Hub does not |
| **Protect** | Nightly snapshots of every node, Docker volumes, consistent database dumps, bare-metal restore |
| **Watch** | Live fleet console: temperatures, power, tokens/s per model, alerts with LLM triage, a morning brief |

## Install (head node)

```bash
pip install noctyx                       # or: PYTHONPATH=src from a checkout
sudo mkdir -p /etc/noctyx && sudo cp examples/site.example.json /etc/noctyx/site.json   # then edit it
noctyx                                   # the banner and the command list
sudo noctyx install-head                 # shows the systemd units it would install; --apply installs them
sudo noctyx install-agent --host gpu-01  # each node: restic, the agent, keys (never on a command line)
```

The console is at `https://<head>:20996`. Everything a site differs in lives in its config
(`NOCTYX_CONFIG`, default `/etc/noctyx/site.json`): hosts, the NAS, schedules, retention, backup policy,
the HF mirror, recipe slots and catalog, the assistant's LLM endpoint.

**An existing layout.** Every file, unit and user name Noctyx touches is in
[`src/noctyx/paths.py`](src/noctyx/paths.py); a site's `"paths"` block overrides any of them, so a cluster
set up by an earlier tool keeps its directories, agent and unit names and moves when it wants to.
`noctyx paths` prints what is in effect.

## License

Noctyx is **source-available** under the [Business Source License 1.1](LICENSE):

- **Free** for personal, hobby and home-lab use, and for teaching and research at non-profit
  institutions — at any scale.
- **Free** for anyone to evaluate, develop with and test.
- **Commercial use** (by or for a for-profit organization) and offering Noctyx as a hosted service
  need a commercial license — see [COMMERCIAL.md](COMMERCIAL.md).
- Every release becomes **Apache 2.0** four years after it ships.

## Donations

Noctyx is donationware for everyone who uses it free. If it saves you a night, a re-download or a
dead node, tips keep it going:

**Bitcoin:** `bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff`

Commercial licenses are paid to the same address — see [COMMERCIAL.md](COMMERCIAL.md).

"Noctyx" is a trademark of its Licensor; the license grants no rights in the name or logo.
