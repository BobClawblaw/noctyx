# Noctyx

The night-shift operator for your AI cluster. One console that **deploys** model recipes safely,
**caches** every model locally, **protects** your machines and data, and **watches** all of it with an
LLM that explains what it sees. Built for DGX Spark pairs, RTX workstations, NAS-backed home labs and
small inference clusters.

> **Status: pre-alpha.** The code is being extracted from a single-site implementation that has run a
> production GPU cluster since 2026-09. See [docs/PLAN.md](docs/PLAN.md).

| | |
|---|---|
| **Deploy** | Build the next recipe while the current one serves; switch under the GPU lock; roll back automatically. Discover public recipes for your hardware, ranked by measured speed |
| **Cache** | One copy of every Hugging Face model on the cluster, served back as `HF_ENDPOINT`; keeps working when the Hub does not |
| **Protect** | Nightly snapshots of every node, Docker volumes, consistent database dumps, bare-metal restore |
| **Watch** | Live fleet console: temperatures, power, tokens/s per model, alerts with LLM triage, a morning brief |

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
