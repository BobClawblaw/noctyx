#!/usr/bin/env python3
"""Build the Noctyx GitHub Pages marketing & documentation site (zero dependencies).

Usage:
    python3 tools/build_site.py --out _site
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import shutil
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/BobClawblaw/noctyx"
SITE_URL = "https://bobclawblaw.github.io/noctyx"

# Documentation catalog: (slug, source relative path, nav title, category, short description)
DOCS: list[tuple[str, str, str, str, str]] = [
    ("install", "docs/guide/install.md", "Install & Setup", "Start Here",
     "Linux systemd or macOS launchd head node, init, setup all, verify, .deb agent packaging, and demo mode."),
    ("console", "docs/guide/console.md", "Web Console & Themes", "Start Here",
     "Dashboard, Machines, Cluster, Recipes, Self-repair, Configuration, Kiosk, 15 themes, and Prometheus /metrics."),
    ("site-config", "docs/guide/site-config.md", "Site Configuration", "Start Here",
     "Every block and key in site.json: hosts, slots, backup_policy, backup_targets, alerting, and paths."),
    ("recipes", "docs/guide/recipes.md", "Model Recipes & Slots", "Deploy & Cache",
     "GPU slots, build-while-serving deploys, greedy answer checks, drift adoption, and GitHub discovery."),
    ("recipe-deploy", "docs/spec/recipe-deploy.md", "Recipe [deploy] Spec v0", "Deploy & Cache",
     "Open declarative recipe.toml [deploy] specification for zero-adapter model deployments."),
    ("hf-mirror", "docs/guide/hf-mirror.md", "HF Model Mirror", "Deploy & Cache",
     "Deduplicated Hugging Face cache served as HF_ENDPOINT with optional atomic write-through caching."),
    ("backups", "docs/guide/backups.md", "Backups & 3-2-1 Targets", "Protect & Restore",
     "Restic schedules, spindle locks, throughput floor, DB dumps, retention, and S3/B2/REST/SFTP copy targets."),
    ("restore", "docs/guide/restore.md", "Restore & Bare-Metal", "Protect & Restore",
     "File and host restores, automated monthly restore rehearsals, dual-arch recovery stick, and head rebuild."),
    ("checkpoints", "docs/guide/checkpoints.md", "Checkpoints & Rollback", "Protect & Restore",
     "Automatic restic checkpoints before upgrades and apt, noctyx revert, config history, and self-rollback."),
    ("assistant", "docs/guide/assistant.md", "Assistant LLM & Ask", "Watch, AI & Heal",
     "OpenAI-compatible endpoint with fallback, alert triage, morning brief, diagnosis, and Ask Noctyx drawer."),
    ("signals", "docs/guide/signals.md", "Early Warnings & Signals", "Watch, AI & Heal",
     "NFS and NAS throughput probes, RoCE RDMA fabric counters, SSH login anomaly detection, and disk forecasts."),
    ("hygiene", "docs/guide/hygiene.md", "Fleet Hygiene", "Watch, AI & Heal",
     "Old kernels, unused packages, journal bloat, and split Docker cleanup with space-reclaimed estimates."),
    ("self-repair", "docs/guide/self-repair.md", "Self-Repair Loop", "Watch, AI & Heal",
     "Bounded autonomous playbooks, per-hour budgets, freeze protection, shadow mode, and LLM diagnosis."),
    ("accounts", "docs/guide/accounts.md", "Accounts & RBAC", "Admin & Reference",
     "scrypt password hashing, viewer/operator/admin roles, session revocation on password change, and API tokens."),
    ("upgrade", "docs/guide/upgrade.md", "Upgrades & Pins", "Admin & Reference",
     "Transactional upgrades, canary-first rollouts, component version pins, release summaries, and migration."),
    ("troubleshooting", "docs/guide/troubleshooting.md", "Troubleshooting", "Admin & Reference",
     "Common failure modes, error messages, lock recovery, TLS/SSH diagnostics, and recovery steps."),
    ("plan", "docs/PLAN.md", "Architecture & Roadmap", "Admin & Reference",
     "Design principles, changelog milestones through 0.9.6, and open items on the roadmap."),
]

SCREENSHOTS: list[tuple[str, str, str, str]] = [
    ("phosphor-dashboard.png", "Phosphor Dashboard", "themes monitoring",
     "Instrument-panel aesthetic with segmented LED meters, oscilloscope traces, LCD numerals, and subsystem color coding."),
    ("phosphor-kiosk.png", "Phosphor Kiosk Wall", "themes monitoring",
     "Full-screen wall display with optional CRT scanlines, fleet tok/s, GPU watts, per-node traces, and alerts."),
    ("phosphor-node.png", "Machine Report (Phosphor)", "themes monitoring",
     "Deep per-host inspection with subsystem-coded CPU, GPU, memory, network, power, temperature, and LLM panels."),
    ("phosphor-family.jpg", "The 10-Face Phosphor Family", "themes",
     "Phosphor, Aurora, Outrun, LCD, P1 Green, P3 Amber, Nixie, Synthwave, Arctic, and Red Ops — each with a light face."),
    ("theme-ember-light.png", "Ember Light Theme", "themes",
     "High-contrast daytime palette alongside Noctyx, Mono, GitHub, Catppuccin, and full 9-color custom themes."),
    ("dashboard.png", "Noctyx Classic Dashboard", "themes monitoring",
     "Draggable, resizable 12-column dashboard in the signature navy and neon cyan Noctyx theme."),
    ("node.png", "Machine Report & Health Tabs", "monitoring",
     "Overview, LLM, network, storage, GPU, health (SSH/RoCE/NFS signals), and backups with one-click backup/verify."),
    ("cluster.png", "Cluster Fleet Matrix", "monitoring",
     "Dense systems-only fleet view: per-node gauges, LLM queue depth, 1-hour sparklines, and fleet totals."),
    ("kiosk.png", "Kiosk (Noctyx Theme)", "monitoring",
     "Zero-flicker wall display supporting scroll/shrink layouts, single-page pinning, or ?rotation=1 cycling."),
    ("recipes.png", "Recipes & GPU Slots", "deploy",
     "Live GPU PIDs and lock waiters, greedy answer-check badges, build-while-serving deploys, and GitHub discovery."),
    ("ask.png", "Ask Noctyx LLM Drawer", "deploy monitoring",
     "Read-only cluster assistant grounded in live metrics, alerts, and logs — with secret redaction and safe action buttons."),
    ("config.png", "Configuration & 0.9.6 Controls", "config",
     "Backup policies, discovered databases, HF mirror write-through, secondary 3-2-1 targets, alerting, and self-repair."),
    ("settings.png", "Display Settings Drawer", "themes config",
     "15 themes + custom colors, Phosphor CRT effects, °F/°C toggle, browser/account/site-default persistence."),
    ("menu.png", "Keyboard-First Navigation", "config",
     "Monitor, Deploy, Configure, and Kiosk menus with instant 1–6 hotkeys and Esc drawer dismissal."),
    ("about.png", "About & Site Inventory (0.9.6)", "config",
     "Live site summary showing Noctyx 0.9.6, Python/restic versions, host & slot counts, and docs shortcuts."),
    ("login.png", "Role-Based Sign-In", "config",
     "scrypt-hashed accounts with viewer, operator, and admin roles plus immediate session invalidation on password change."),
]


def read_version() -> str:
    py = ROOT / "pyproject.toml"
    data = tomllib.loads(py.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version", "0.9.6")


def slugify(text: str, seen: dict[str, int]) -> str:
    # Strip markdown formatting before slugging (matches GitHub anchor convention closely)
    plain = re.sub(r"`([^`]+)`", r"\1", text)
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
    plain = re.sub(r"[*~]", "", plain)
    slug = re.sub(r"[^\w\s-]", "", plain.lower()).strip()
    slug = re.sub(r"[\s]+", "-", slug)
    slug = slug or "section"
    n = seen.get(slug, 0)
    seen[slug] = n + 1
    return slug if n == 0 else f"{slug}-{n}"


def rewrite_link(href: str) -> str:
    if href.startswith(("http://", "https://", "mailto:", "#")):
        return href
    path_part, sep, frag = href.partition("#")
    anchor = f"#{frag}" if sep else ""
    clean = path_part.lstrip("./")

    # Map known repo doc paths to flat docs/<slug>.html
    doc_map = {
        "PLAN.md": "plan.html",
        "docs/PLAN.md": "plan.html",
        "../PLAN.md": "plan.html",
        "recipe-deploy.md": "recipe-deploy.html",
        "spec/recipe-deploy.md": "recipe-deploy.html",
        "../spec/recipe-deploy.md": "recipe-deploy.html",
        "docs/spec/recipe-deploy.md": "recipe-deploy.html",
    }
    for slug, src, *_ in DOCS:
        name = pathlib.Path(src).name
        doc_map[name] = f"{slug}.html"
        doc_map[src] = f"{slug}.html"
        doc_map[f"guide/{name}"] = f"{slug}.html"

    if path_part in doc_map:
        return doc_map[path_part] + anchor
    if clean in doc_map:
        return doc_map[clean] + anchor
    if path_part.startswith("../screenshots/"):
        return path_part
    if path_part.startswith("docs/screenshots/"):
        return "../screenshots/" + path_part.split("docs/screenshots/", 1)[1]
    if path_part.startswith(("src/", "../../src/", "../src/", "tools/", "LICENSE")):
        rel = path_part.replace("../../", "").replace("../", "")
        return f"{REPO_URL}/blob/main/{rel}{anchor}"
    if path_part.endswith(".md"):
        return pathlib.Path(path_part).stem + ".html" + anchor
    return href


def render_inline(text: str) -> str:
    # Protect inline code spans first
    codes: list[str] = []

    def _code(m: re.Match[str]) -> str:
        idx = len(codes)
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00CODE{idx}\x00"

    text = re.sub(r"`([^`]+)`", _code, text)

    # Protect images ![alt](src)
    imgs: list[str] = []

    def _img(m: re.Match[str]) -> str:
        idx = len(imgs)
        alt = html.escape(m.group(1))
        src = html.escape(rewrite_link(m.group(2).strip()))
        imgs.append(f'<img src="{src}" alt="{alt}" loading="lazy">')
        return f"\x00IMG{idx}\x00"

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img, text)

    # Protect markdown links [label](href)
    links: list[str] = []

    def _link(m: re.Match[str]) -> str:
        idx = len(links)
        raw_label = m.group(1)
        href = rewrite_link(m.group(2).strip())
        # Allow inline formatting inside link label
        label = _format_emphasis(html.escape(raw_label))
        ext = ' target="_blank" rel="noopener"' if href.startswith(("http://", "https://")) else ""
        links.append(f'<a href="{html.escape(href)}"{ext}>{label}</a>')
        return f"\x00LINK{idx}\x00"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, text)

    # Escape remaining HTML and apply emphasis
    text = html.escape(text)
    text = _format_emphasis(text)

    # Restore tokens (links may contain code placeholders)
    for idx, val in enumerate(links):
        text = text.replace(f"\x00LINK{idx}\x00", val)
    for idx, val in enumerate(imgs):
        text = text.replace(f"\x00IMG{idx}\x00", val)
    for idx, val in enumerate(codes):
        text = text.replace(f"\x00CODE{idx}\x00", val)
    return text


def _format_emphasis(text: str) -> str:
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)
    return text


def split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    # Split on unescaped |
    cells = re.split(r"(?<!\\)\|", s)
    return [c.strip().replace(r"\|", "|") for c in cells]


def is_table_sep(line: str) -> bool:
    s = line.strip()
    if "|" not in s:
        return False
    parts = [p.strip() for p in split_table_row(s)]
    return bool(parts) and all(bool(re.fullmatch(r":?-{2,}:?", p)) for p in parts)


def render_markdown(md: str) -> tuple[str, list[tuple[int, str, str]], str]:
    """Convert markdown text to HTML, returning (html, toc, plain_text_for_search)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    plain_chunks: list[str] = []
    seen_slugs: dict[str, int] = {}

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        fence = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        if fence:
            delim = fence.group(1)[0]
            min_len = len(fence.group(1))
            lang = fence.group(2).strip() or "text"
            code_lines: list[str] = []
            i += 1
            while i < n:
                if re.match(rf"^{re.escape(delim)}{{{min_len},}}\s*$", lines[i].strip()):
                    i += 1
                    break
                code_lines.append(lines[i])
                i += 1
            escaped = html.escape("\n".join(code_lines))
            out.append(
                f'<div class="code-block">'
                f'<div class="code-head"><span>{html.escape(lang)}</span>'
                f'<button class="copy-btn" type="button">Copy</button></div>'
                f'<pre><code class="lang-{html.escape(lang)}">{escaped}</code></pre>'
                f'</div>'
            )
            plain_chunks.append(" ".join(code_lines)[:300])
            continue

        # Horizontal rule
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            out.append("<hr>")
            i += 1
            continue

        # Heading
        hm = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", stripped)
        if hm:
            lvl = len(hm.group(1))
            raw_title = hm.group(2)
            sid = slugify(raw_title, seen_slugs)
            rendered = render_inline(raw_title)
            if lvl in (2, 3):
                toc.append((lvl, sid, re.sub(r"<[^>]+>", "", rendered)))
            anchor = f'<a class="heading-anchor" href="#{sid}" aria-label="Link to section">#</a>' if lvl >= 2 else ""
            out.append(f'<h{lvl} id="{sid}">{rendered}{anchor}</h{lvl}>')
            plain_chunks.append(raw_title)
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            bq_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                bq_lines.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            inner_html, _, inner_plain = render_markdown("\n".join(bq_lines))
            out.append(f"<blockquote>{inner_html}</blockquote>")
            plain_chunks.append(inner_plain)
            continue

        # Table
        if "|" in line and i + 1 < n and is_table_sep(lines[i + 1]):
            headers = split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(split_table_row(lines[i]))
                i += 1
            thead = "".join(f"<th>{render_inline(h)}</th>" for h in headers)
            tbody_rows = []
            for r in rows:
                padded = r + [""] * max(0, len(headers) - len(r))
                tds = "".join(f"<td>{render_inline(c)}</td>" for c in padded[:len(headers)])
                tbody_rows.append(f"<tr>{tds}</tr>")
                plain_chunks.append(" ".join(r))
            out.append(
                f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead>'
                f'<tbody>{"".join(tbody_rows)}</tbody></table></div>'
            )
            continue

        # Unordered / Ordered list (supports continuation lines and nested sub-items)
        lm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if lm:
            list_html, i, list_plain = _parse_list(lines, i)
            out.append(list_html)
            plain_chunks.append(list_plain)
            continue

        # Paragraph
        p_lines: list[str] = [stripped]
        i += 1
        while i < n:
            nxt = lines[i]
            ns = nxt.strip()
            if not ns:
                break
            if re.match(r"^(#{1,4}\s|`{3,}|~{3,}|>|[-*+]\s|\d+\.\s)", ns):
                break
            if "|" in nxt and i + 1 < n and is_table_sep(lines[i + 1]):
                break
            p_lines.append(ns)
            i += 1
        joined = " ".join(p_lines)
        out.append(f"<p>{render_inline(joined)}</p>")
        plain_chunks.append(joined)

    return "\n".join(out), toc, " ".join(plain_chunks)


def _parse_list(lines: list[str], start: int) -> tuple[str, int, str]:
    n = len(lines)
    first = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[start])
    assert first is not None
    base_indent = len(first.group(1))
    ordered = first.group(2).endswith(".")
    tag = "ol" if ordered else "ul"
    items: list[str] = []
    plains: list[str] = []
    i = start

    while i < n:
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
        if not m or len(m.group(1)) != base_indent or (m.group(2).endswith(".") != ordered):
            break
        item_parts = [m.group(3).strip()]
        nested_blocks: list[str] = []
        i += 1
        while i < n:
            if not lines[i].strip():
                # Look ahead: if next non-empty line is indented more or same-level list item
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n:
                    nm = re.match(r"^(\s*)([-*+]|\d+\.)\s+", lines[j])
                    if nm and len(nm.group(1)) == base_indent and (nm.group(2).endswith(".") == ordered):
                        i = j
                        break
                    if lines[j].startswith(" " * (base_indent + 2)):
                        i = j
                        continue
                i = j
                break
            sub_m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
            if sub_m:
                sub_indent = len(sub_m.group(1))
                if sub_indent > base_indent:
                    sub_html, i, sub_plain = _parse_list(lines, i)
                    nested_blocks.append(sub_html)
                    plains.append(sub_plain)
                    continue
                break
            # Indented fenced code block inside list item
            if lines[i].startswith(" " * (base_indent + 2)) and lines[i].strip().startswith("```"):
                fence_lang = lines[i].strip()[3:].strip() or "bash"
                code_l: list[str] = []
                i += 1
                while i < n and not lines[i].strip().startswith("```"):
                    code_l.append(lines[i][base_indent + 2:] if len(lines[i]) > base_indent + 2 else lines[i])
                    i += 1
                if i < n:
                    i += 1
                nested_blocks.append(
                    f'<div class="code-block"><div class="code-head"><span>{html.escape(fence_lang)}</span>'
                    f'<button class="copy-btn" type="button">Copy</button></div>'
                    f'<pre><code>{html.escape(chr(10).join(code_l))}</code></pre></div>'
                )
                continue
            if lines[i].startswith(" " * (base_indent + 2)):
                item_parts.append(lines[i].strip())
                i += 1
                continue
            break

        text = " ".join(item_parts)
        plains.append(text)
        items.append(f"<li>{render_inline(text)}{''.join(nested_blocks)}</li>")

    return f"<{tag}>{''.join(items)}</{tag}>", i, " ".join(plains)


def shared_head(title: str, description: str, base: str, version: str, canonical_path: str) -> str:
    og_img = f"{SITE_URL}/assets/noctyx-social-preview.png"
    canonical = f"{SITE_URL}/{canonical_path}".rstrip("/") + ("/" if not canonical_path.endswith(".html") else "")
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="noctyx" data-base="{base}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description)}">
  <link rel="canonical" href="{html.escape(canonical)}">
  <link rel="icon" type="image/svg+xml" href="{base}/assets/noctyx-mark.svg">
  <link rel="icon" type="image/png" href="{base}/assets/noctyx-mark-256.png">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{html.escape(title)}">
  <meta property="og:description" content="{html.escape(description)}">
  <meta property="og:image" content="{og_img}">
  <meta property="og:url" content="{html.escape(canonical)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html.escape(title)}">
  <meta name="twitter:description" content="{html.escape(description)}">
  <meta name="twitter:image" content="{og_img}">
  <link rel="stylesheet" href="{base}/style.css">
</head>"""


def shared_nav(base: str, version: str, active: str = "home", is_docs: bool = False) -> str:
    home_href = "#top" if active == "home" else f"{base}/index.html"
    docs_href = f"{base}/docs/index.html"
    mobile_btn = (
        '<button class="icon-btn mobile-menu-btn" type="button" data-toggle-sidebar aria-label="Toggle docs menu">☰ Menu</button>'
        if is_docs else ""
    )
    return f"""<header class="topnav">
  <div class="topnav-inner">
    <div style="display:flex;align-items:center;gap:12px">
      {mobile_btn}
      <a class="brand" href="{home_href}">
        <img src="{base}/assets/noctyx-mark.svg" alt="Noctyx emblem">
        <span>NOCTYX</span>
        <span class="ver-pill mono">v{html.escape(version)}</span>
      </a>
    </div>
    <nav class="nav-links" aria-label="Primary">
      <a href="{base}/index.html#new-096">What's New</a>
      <a href="{base}/index.html#pillars">Features</a>
      <a href="{base}/index.html#architecture">Architecture</a>
      <a href="{base}/index.html#screenshots">Screenshots</a>
      <a href="{base}/index.html#quickstart">Quickstart</a>
      <a href="{docs_href}" class="{'active' if active == 'docs' else ''}">Docs</a>
    </nav>
    <div class="nav-actions">
      <button class="search-trigger" type="button" data-open-search aria-label="Search documentation">
        <span>Search docs</span><kbd>/</kbd>
      </button>
      <button class="icon-btn" type="button" data-cycle-theme title="Cycle site theme (Noctyx / Phosphor / Light)">
        <span>Theme: <strong data-theme-label>Noctyx</strong></span>
      </button>
      <a class="btn btn-secondary btn-sm" href="{REPO_URL}" target="_blank" rel="noopener">GitHub</a>
    </div>
  </div>
</header>"""


def shared_modals_and_footer(base: str, version: str) -> str:
    return f"""<footer class="site-footer">
  <div class="container">
    <div class="footer-grid">
      <div class="footer-col">
        <a class="brand" href="{base}/index.html" style="margin-bottom:10px">
          <img src="{base}/assets/noctyx-mark.svg" alt="Noctyx emblem">
          <span>NOCTYX</span>
          <span class="ver-pill mono">v{html.escape(version)}</span>
        </a>
        <p style="margin:8px 0 0;font-size:0.88rem;color:var(--text-muted)">
          The night-shift operator for your AI cluster. Deploy, cache, protect, watch, and heal — pure Python stdlib on the head, bash + restic on the nodes.
        </p>
        <div class="btc-box">
          <div class="btc-head">
            <span class="btc-label">₿ Bitcoin Donationware</span>
            <button class="copy-btn" type="button" data-copy-text="bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff">Copy</button>
          </div>
          <code class="btc-addr mono">bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff</code>
        </div>
      </div>
      <div class="footer-col">
        <h4>Start Here</h4>
        <ul>
          <li><a href="{base}/docs/install.html">Install &amp; Setup</a></li>
          <li><a href="{base}/docs/console.html">Web Console &amp; Themes</a></li>
          <li><a href="{base}/docs/site-config.html">Site Configuration</a></li>
          <li><a href="{base}/docs/recipes.html">Model Recipes &amp; Slots</a></li>
          <li><a href="{base}/docs/recipe-deploy.html">Recipe [deploy] Spec v0</a></li>
        </ul>
      </div>
      <div class="footer-col">
        <h4>Protect &amp; Watch</h4>
        <ul>
          <li><a href="{base}/docs/backups.html">Backups &amp; 3-2-1 Targets</a></li>
          <li><a href="{base}/docs/restore.html">Restore &amp; Bare-Metal</a></li>
          <li><a href="{base}/docs/hf-mirror.html">HF Model Mirror</a></li>
          <li><a href="{base}/docs/assistant.html">Assistant LLM &amp; Ask</a></li>
          <li><a href="{base}/docs/self-repair.html">Self-Repair Loop</a></li>
        </ul>
      </div>
      <div class="footer-col">
        <h4>Project</h4>
        <ul>
          <li><a href="{REPO_URL}" target="_blank" rel="noopener">GitHub Repository</a></li>
          <li><a href="{REPO_URL}/releases" target="_blank" rel="noopener">Releases (v{html.escape(version)})</a></li>
          <li><a href="{base}/docs/plan.html">Architecture &amp; Roadmap</a></li>
          <li><a href="{base}/docs/troubleshooting.html">Troubleshooting</a></li>
          <li><a href="{REPO_URL}/blob/main/LICENSE" target="_blank" rel="noopener">PolyForm Noncommercial 1.0.0</a></li>
        </ul>
      </div>
    </div>
    <div class="footer-bottom">
      <span>Licensed under PolyForm Noncommercial 1.0.0 — free donationware via Bitcoin. Selling Noctyx or paid support is not permitted.</span>
      <span>Built from repository docs with zero external dependencies.</span>
    </div>
  </div>
</footer>

<div class="modal-backdrop" id="search-modal" role="dialog" aria-modal="true" aria-label="Search documentation">
  <div class="search-modal">
    <div class="search-input-wrap">
      <span class="mono" style="color:var(--accent)">/</span>
      <input id="search-input" type="search" placeholder="Search 17 guides, CLI commands, config keys, troubleshooting…" autocomplete="off">
      <kbd style="font-size:0.72rem;padding:2px 6px;border-radius:5px;border:1px solid var(--border);color:var(--text-dim)">ESC</kbd>
    </div>
    <div class="search-results" id="search-results"></div>
  </div>
</div>

<div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="Image preview">
  <div class="lightbox-controls">
    <button class="lb-btn" type="button" data-lb-close aria-label="Close">✕</button>
  </div>
  <button class="lb-btn lb-nav lb-prev" type="button" data-lb-prev aria-label="Previous image">‹</button>
  <div class="lightbox-stage"><img id="lb-img" src="" alt=""></div>
  <button class="lb-btn lb-nav lb-next" type="button" data-lb-next aria-label="Next image">›</button>
  <div class="lightbox-bar" id="lb-cap"></div>
</div>

<script src="{base}/app.js"></script>
</body>
</html>"""


def render_landing(version: str) -> str:
    shot_cards = []
    for fname, title, cats, desc in SCREENSHOTS:
        shot_cards.append(f"""<article class="shot-card" data-cats="{cats}">
  <div class="shot-thumb"><img src="screenshots/{fname}" alt="{html.escape(title)}" loading="lazy"></div>
  <div class="shot-body">
    <h3>{html.escape(title)}</h3>
    <p>{html.escape(desc)}</p>
  </div>
</article>""")

    doc_cards = []
    for slug, _, title, cat, desc in DOCS:
        doc_cards.append(f"""<a class="doc-hub-card" href="docs/{slug}.html">
  <span class="doc-hub-cat">{html.escape(cat)}</span>
  <h3>{html.escape(title)}</h3>
  <p>{html.escape(desc)}</p>
</a>""")

    head = shared_head(
        f"Noctyx {version} — The night-shift operator for your AI cluster",
        "Deploy model recipes safely, cache Hugging Face models locally, protect nodes with 3-2-1 restic backups, watch your fleet with LLM triage, and heal drift automatically.",
        ".",
        version,
        "",
    )
    nav = shared_nav(".", version, active="home")
    footer = shared_modals_and_footer(".", version)

    return f"""{head}
<body id="top">
{nav}

<main>
  <!-- HERO -->
  <section class="hero">
    <div class="container">
      <div class="eyebrow">
        <span class="eyebrow-dot"></span>
        <span>Free Noncommercial Donationware · PolyForm NC 1.0 · Zero Dependencies · v{html.escape(version)}</span>
      </div>
      <h1>The <span class="gradient-text">night-shift operator</span> for your AI cluster.</h1>
      <p class="hero-sub">
        One self-hosted console that <strong>deploys</strong> model recipes safely, <strong>caches</strong> every Hugging Face model locally, <strong>protects</strong> your Linux nodes with 3-2-1 encrypted backups, <strong>watches</strong> GPU &amp; LLM telemetry with an assistant LLM, and <strong>heals</strong> what breaks.
      </p>
      <div class="hero-ctas">
        <a class="btn btn-primary" href="docs/install.html">Get Started →</a>
        <a class="btn btn-secondary" href="docs/index.html">Read Documentation (17 Guides)</a>
        <a class="btn btn-secondary" href="#quickstart">Try Demo Locally</a>
      </div>

      <div class="pillar-strip" aria-label="Five Pillars">
        <button class="pillar-chip active" type="button" data-scroll="#pillar-deploy"><span class="pillar-dot" style="background:#3ff0ff"></span>DEPLOY</button>
        <button class="pillar-chip" type="button" data-scroll="#pillar-cache"><span class="pillar-dot" style="background:#5b8cff"></span>CACHE</button>
        <button class="pillar-chip" type="button" data-scroll="#pillar-protect"><span class="pillar-dot" style="background:#8f6bff"></span>PROTECT</button>
        <button class="pillar-chip" type="button" data-scroll="#pillar-watch"><span class="pillar-dot" style="background:#c25cff"></span>WATCH</button>
        <button class="pillar-chip" type="button" data-scroll="#pillar-heal"><span class="pillar-dot" style="background:#ff3fd2"></span>HEAL</button>
      </div>

      <!-- INTERACTIVE HERO SHOWCASE -->
      <div class="hero-showcase">
        <div class="showcase-chrome">
          <div class="chrome-dots" aria-hidden="true"><span></span><span></span><span></span></div>
          <div class="showcase-tabs" role="tablist" aria-label="Console preview views">
            <button class="showcase-tab active" type="button"
              data-src="screenshots/phosphor-dashboard.png"
              data-caption="Phosphor Dashboard — Segmented LED meters, oscilloscope traces, fleet tok/s, GPU power, and storage tiers.">Phosphor Dashboard</button>
            <button class="showcase-tab" type="button"
              data-src="screenshots/phosphor-kiosk.png"
              data-caption="Phosphor Kiosk — Zero-flicker wall display with CRT scanlines, live GPU watts, and per-model throughput.">Kiosk Wall</button>
            <button class="showcase-tab" type="button"
              data-src="screenshots/recipes.png"
              data-caption="Recipes &amp; GPU Slots — Build while serving, GPU lock waiters, greedy answer checks, and measured-speed discovery.">Recipes</button>
            <button class="showcase-tab" type="button"
              data-src="screenshots/ask.png"
              data-caption="Ask Noctyx — Read-only LLM assistant grounded in live Prometheus metrics, alerts, logs, and safe action buttons.">Ask Noctyx</button>
            <button class="showcase-tab" type="button"
              data-src="screenshots/theme-ember-light.png"
              data-caption="Ember Light — One of 15 built-in themes (plus 9-color custom themes) with light, dark, and system modes.">Ember Light</button>
          </div>
        </div>
        <div class="showcase-img-wrap">
          <img data-showcase-img src="screenshots/phosphor-dashboard.png" alt="Phosphor Dashboard — Segmented LED meters, oscilloscope traces, fleet tok/s, GPU power, and storage tiers.">
        </div>
        <div class="showcase-caption">
          <span data-showcase-caption>Phosphor Dashboard — Segmented LED meters, oscilloscope traces, fleet tok/s, GPU power, and storage tiers.</span>
          <span class="mono" style="font-size:0.76rem;color:var(--accent);white-space:nowrap">Click image to zoom ↗</span>
        </div>
      </div>

      <!-- STATS RIBBON -->
      <div class="stats-ribbon">
        <div class="stat-card"><div class="stat-val mono">0</div><div class="stat-lbl">External Python Deps</div></div>
        <div class="stat-card"><div class="stat-val mono">15</div><div class="stat-lbl">Instrument-Panel Themes</div></div>
        <div class="stat-card"><div class="stat-val mono">3-2-1</div><div class="stat-lbl">Encrypted Restic Tiers</div></div>
        <div class="stat-card"><div class="stat-val mono">Linux + Mac</div><div class="stat-lbl">systemd &amp; launchd Head</div></div>
        <div class="stat-card"><div class="stat-val mono">x86 + ARM</div><div class="stat-lbl">Dual-Arch Recovery ISO</div></div>
        <div class="stat-card"><div class="stat-val mono">290+</div><div class="stat-lbl">Automated Cluster Tests</div></div>
      </div>
    </div>
  </section>

  <!-- NEW IN 0.9.6 -->
  <section class="section" id="new-096">
    <div class="container">
      <div class="section-head">
        <div class="section-kicker">Release Spotlight</div>
        <h2>What's new in Noctyx {html.escape(version)}</h2>
        <p>Built from production GPU cluster operations — shipping declarative recipes, 3-2-1 offsite copies, write-through model caching, phone/chat alerting, and least-privilege node packaging.</p>
      </div>
      <div class="bento-grid">
        <div class="card">
          <span class="card-tag">Deploy · Spec v0</span>
          <h3>Declarative <code>recipe.toml</code> <code>[deploy]</code></h3>
          <p>Repositories declaring the open <code>[deploy]</code> spec are discovered automatically by <code>noctyx recipe-discover</code> and deployed with zero custom shell adapters.</p>
          <a class="card-link" href="docs/recipe-deploy.html">Read the spec →</a>
        </div>
        <div class="card">
          <span class="card-tag">Protect · 3-2-1</span>
          <h3>Secondary Offsite Backup Targets</h3>
          <p>Copy primary restic snapshots incrementally (<code>--copy-chunker-params</code>) to S3, Backblaze B2, remote REST, SFTP, or USB disks via <code>noctyx copy --target</code> or the Config UI.</p>
          <a class="card-link" href="docs/backups.html#secondary-3-2-1-backup-targets-copy---target">Configure targets →</a>
        </div>
        <div class="card">
          <span class="card-tag">Cache · Mirror</span>
          <h3>Atomic Write-Through HF Mirror</h3>
          <p>Enable <code>hf_mirror.write_through</code> to stream cache misses directly into the shared store with SHA-256 verification and atomic rename — first pull caches for the whole cluster.</p>
          <a class="card-link" href="docs/hf-mirror.html">Mirror guide →</a>
        </div>
        <div class="card">
          <span class="card-tag">Watch · Alerting</span>
          <h3>Phone, Chat &amp; Email Routing + macOS</h3>
          <p>Route Alertmanager notifications to ntfy, Pushover, Slack, Telegram, SMTP, or webhooks — now including full Alertmanager <code>launchd</code> support on macOS head nodes.</p>
          <a class="card-link" href="docs/install.html#packaging-least-privilege-nodes-096">Alerting setup →</a>
        </div>
        <div class="card">
          <span class="card-tag">Ops · Packaging</span>
          <h3>Pure-Stdlib <code>.deb</code> &amp; Scoped Sudoers</h3>
          <p>Build an installable <code>noctyx-agent_&lt;ver&gt;_all.deb</code> anywhere with <code>noctyx build-deb</code> (no <code>dpkg-deb</code> required) and install least-privilege <code>/etc/sudoers.d/noctyx</code> rules.</p>
          <a class="card-link" href="docs/install.html">Packaging guide →</a>
        </div>
        <div class="card">
          <span class="card-tag">Security · Core</span>
          <h3>Session Revocation &amp; Split Assets</h3>
          <p>Password changes immediately invalidate old sessions via <code>session_epoch</code>, admin actions are tightly gated, and the console frontend is cleanly split into dedicated CSS/JS assets.</p>
          <a class="card-link" href="docs/accounts.html">Accounts &amp; RBAC →</a>
        </div>
      </div>
    </div>
  </section>

  <!-- FIVE PILLARS -->
  <section class="section" id="pillars">
    <div class="container">
      <div class="section-head">
        <div class="section-kicker">End-to-End Cluster Ops</div>
        <h2>Five pillars, one console, zero glue scripts</h2>
        <p>Built for DGX Spark TP pairs, RTX workstations, Apple Silicon Macs, NAS-backed home labs, and small inference clusters.</p>
      </div>
      <div class="pillars-grid">
        <div class="pillar-card" id="pillar-deploy" style="--p-color:#3ff0ff">
          <h3>Deploy</h3>
          <ul>
            <li><strong>Zero-downtime builds:</strong> build &amp; pull the next recipe while the current model keeps serving.</li>
            <li><strong>GPU lock &amp; rollback:</strong> switch cleanly under lock, health-check, auto-rollback on failure.</li>
            <li><strong>Answer checks:</strong> hourly greedy fixed-prompt checks catch silent model regressions.</li>
            <li><strong>Measured discovery:</strong> rank public GitHub recipes by measured speed, not README claims.</li>
          </ul>
          <a class="card-link" href="docs/recipes.html">Recipes guide →</a>
        </div>
        <div class="pillar-card" id="pillar-cache" style="--p-color:#5b8cff">
          <h3>Cache</h3>
          <ul>
            <li><strong>One cluster copy:</strong> deduplicated Hugging Face mirror served as <code>HF_ENDPOINT</code>.</li>
            <li><strong>Atomic write-through:</strong> verify SHA-256 on cache misses and promote atomically.</li>
            <li><strong>Nightly harvest:</strong> absorb any local node downloads into the shared NAS store.</li>
            <li><strong>Offline resilience:</strong> keep serving weights even when Hugging Face Hub is down.</li>
          </ul>
          <a class="card-link" href="docs/hf-mirror.html">HF mirror guide →</a>
        </div>
        <div class="pillar-card" id="pillar-protect" style="--p-color:#8f6bff">
          <h3>Protect</h3>
          <ul>
            <li><strong>Spindle-safe restic:</strong> one writer per spindle, schedule windows, and link-speed floor.</li>
            <li><strong>App-consistent dumps:</strong> Postgres, MySQL/MariaDB, Redis/Valkey, SQLite, and Docker volumes.</li>
            <li><strong>3-2-1 offsite copy:</strong> S3, Backblaze B2, REST, SFTP, and USB targets with root-only credentials.</li>
            <li><strong>Proven restores:</strong> monthly automated restore rehearsals + dual-arch UEFI recovery stick.</li>
          </ul>
          <a class="card-link" href="docs/backups.html">Backups guide →</a>
        </div>
        <div class="pillar-card" id="pillar-watch" style="--p-color:#c25cff">
          <h3>Watch</h3>
          <ul>
            <li><strong>Live GPU &amp; LLM HUD:</strong> tok/s, prefill, KV cache, watts, temps, RoCE RDMA, and NFS probes.</li>
            <li><strong>15 instrument themes:</strong> Phosphor LED meters, CRT effects, Kiosk wall display, and light themes.</li>
            <li><strong>Ask Noctyx:</strong> read-only LLM drawer with secret redaction, morning brief, and alert triage.</li>
            <li><strong>Apple Silicon Macs:</strong> monitor MLX/mtplx tok/s, power, thermals, and Time Machine freshness.</li>
          </ul>
          <a class="card-link" href="docs/console.html">Console guide →</a>
        </div>
        <div class="pillar-card" id="pillar-heal" style="--p-color:#ff3fd2">
          <h3>Heal</h3>
          <ul>
            <li><strong>Transactional upgrades:</strong> snapshot first, canary one node, verify scrape, auto-rollback on error.</li>
            <li><strong>5-minute repair loop:</strong> restores drifted config, restarts failed units, clears safe disk bloat.</li>
            <li><strong>Bounded safety:</strong> per-hour repair budget, failure freeze, shadow mode, and full audit log.</li>
            <li><strong>Instant undo:</strong> <code>noctyx revert</code>, config history diffs, and <code>noctyx self-rollback</code>.</li>
          </ul>
          <a class="card-link" href="docs/self-repair.html">Self-repair guide →</a>
        </div>
      </div>
    </div>
  </section>

  <!-- ARCHITECTURE -->
  <section class="section" id="architecture">
    <div class="container">
      <div class="section-head">
        <div class="section-kicker">System Topology</div>
        <h2>Clean architecture with zero runtime bloat</h2>
        <p>No Postgres, no Redis, no Kubernetes, no Docker-in-Docker required. Just standard library Python on the head node and lightweight bash + restic on Linux nodes.</p>
      </div>
      <div class="arch-frame">
        <img src="assets/noctyx-architecture.svg" alt="Noctyx 0.9.6 Architecture Diagram — Head node, GPU slots & managed nodes, 3-2-1 storage and external alerting">
      </div>
      <div class="arch-notes">
        <div class="card">
          <h3>Head Node (Linux or macOS)</h3>
          <p>Runs the HTTPS Console (<code>:20996</code>), Orchestrator &amp; Self-Repair loop, HF Mirror (<code>:20995</code>), and pinned Prometheus + Alertmanager under <code>systemd</code> or <code>launchd</code>.</p>
        </div>
        <div class="card">
          <h3>GPU Fleet &amp; Managed Nodes</h3>
          <p>DGX Spark TP pairs, RTX hosts, and Linux servers run the tiny <code>.deb</code> / SSH agent and collectors. Apple Silicon Macs are monitored non-invasively alongside Time Machine.</p>
        </div>
        <div class="card">
          <h3>3-2-1 Protect &amp; Outreach</h3>
          <p>Primary TLS <code>rest-server</code> with <code>--append-only</code> protection, incremental secondary copies to S3/B2/REST/SFTP/USB, dual-arch recovery stick, and phone/chat alerts.</p>
        </div>
      </div>
    </div>
  </section>

  <!-- SCREENSHOT GALLERY -->
  <section class="section" id="screenshots">
    <div class="container">
      <div class="section-head">
        <div class="section-kicker">Visual Tour</div>
        <h2>Every screen, every theme — captured from live demo data</h2>
        <p>Filter by area or click any screenshot to inspect full-resolution in the keyboard-navigable lightbox (<code>←</code> <code>→</code> <code>Esc</code>).</p>
      </div>

      <div class="filter-bar" role="tablist" aria-label="Filter screenshots">
        <button class="filter-btn active" type="button" data-filter="all">All ({len(SCREENSHOTS)})</button>
        <button class="filter-btn" type="button" data-filter="themes">Phosphor &amp; Themes</button>
        <button class="filter-btn" type="button" data-filter="monitoring">Monitoring &amp; Kiosk</button>
        <button class="filter-btn" type="button" data-filter="deploy">Deploy &amp; AI</button>
        <button class="filter-btn" type="button" data-filter="config">Config &amp; Auth</button>
      </div>

      <div class="gallery-grid">
        {"".join(shot_cards)}
      </div>
    </div>
  </section>

  <!-- QUICKSTART -->
  <section class="section" id="quickstart">
    <div class="container">
      <div class="section-head">
        <div class="section-kicker">Run It In Minutes</div>
        <h2>Try the interactive demo or deploy to your cluster</h2>
        <p>Zero third-party Python packages to install. Test every panel on realistic anonymized cluster fixtures right on your laptop, or install on a Linux/macOS head node.</p>
      </div>

      <div class="terminal-box">
        <div class="terminal-tabs" role="tablist">
          <button class="term-tab active" type="button" data-term="demo">1. Try Demo (No Cluster)</button>
          <button class="term-tab" type="button" data-term="head">2. Install Head Node</button>
          <button class="term-tab" type="button" data-term="agent">3. .deb Agent &amp; Sudoers</button>
          <button class="term-tab" type="button" data-term="ops">4. 3-2-1 Copy &amp; Alerts</button>
        </div>

        <div class="term-panel active" data-term="demo">
          <div class="code-head"><span>bash · read-only demo at http://127.0.0.1:20993</span><button class="copy-btn" type="button">Copy</button></div>
          <pre><code>git clone {REPO_URL}.git
cd noctyx
PYTHONPATH=src python3 -m noctyx.cli demo
# Open http://127.0.0.1:20993 — explore all views, 15 themes, kiosk &amp; Ask Noctyx</code></pre>
        </div>

        <div class="term-panel" data-term="head">
          <div class="code-head"><span>bash · Linux systemd or macOS launchd head node</span><button class="copy-btn" type="button">Copy</button></div>
          <pre><code>git clone {REPO_URL}.git /opt/noctyx-src
sudo python3 -m venv /opt/noctyx
sudo /opt/noctyx/bin/pip install /opt/noctyx-src &amp;&amp; sudo ln -sf /opt/noctyx/bin/noctyx /usr/local/bin/noctyx

sudo noctyx init                # or: sudo noctyx init --web
sudo noctyx setup all           # tools, TLS, restic repos, services, Prometheus &amp; Alertmanager
sudo noctyx setup verify        # full end-to-end backup + restore round-trip check
sudo noctyx install-agent       # deploy restic + agent + collectors to Linux nodes</code></pre>
        </div>

        <div class="term-panel" data-term="agent">
          <div class="code-head"><span>bash · Pure-stdlib Debian package &amp; least-privilege sudoers</span><button class="copy-btn" type="button">Copy</button></div>
          <pre><code># Build a zero-dependency .deb package on any Linux or macOS machine
noctyx build-deb --out dist/
sudo dpkg -i dist/noctyx-agent_{html.escape(version)}_all.deb

# Preview and deploy scoped /etc/sudoers.d/noctyx rules (no blanket NOPASSWD: ALL)
noctyx sudoers
sudo noctyx sudoers --install</code></pre>
        </div>

        <div class="term-panel" data-term="ops">
          <div class="code-head"><span>bash · Offsite 3-2-1 targets &amp; phone/chat alert routing</span><button class="copy-btn" type="button">Copy</button></div>
          <pre><code># Add an offsite Backblaze B2 / S3 / REST / SFTP target and run incremental copy
sudo noctyx target-set --name offsite --kind b2 --repo b2:cluster-backups:/restic --env-file /etc/noctyx/offsite.env
sudo noctyx copy --target offsite
sudo noctyx verify-target --target offsite

# Route Alertmanager alerts to your phone (ntfy, Pushover, Slack, Telegram, SMTP, webhook)
sudo noctyx alerting-set --json '{{"ntfy": {{"url": "https://ntfy.sh/my-cluster-alerts"}}}}'
sudo noctyx setup alerting
sudo noctyx alerting-test</code></pre>
        </div>
      </div>
    </div>
  </section>

  <!-- EXTENSIVE DOCUMENTATION HUB -->
  <section class="section" id="docs-hub">
    <div class="container">
      <div class="section-head">
        <div class="section-kicker">Extensive Documentation</div>
        <h2>17 complete operational guides &amp; open specs</h2>
        <p>Every guide is rendered straight from the repository with instant full-text search (press <kbd>/</kbd> or <kbd>⌘K</kbd>), copyable commands, and cross-linked anchors.</p>
      </div>
      <div class="docs-hub-grid">
        {"".join(doc_cards)}
      </div>
    </div>
  </section>
</main>

{footer}"""


def build_sidebar(current_slug: str) -> str:
    groups: dict[str, list[tuple[str, str]]] = {}
    for slug, _, title, cat, _ in DOCS:
        groups.setdefault(cat, []).append((slug, title))

    parts = [
        '<div class="sidebar-group">',
        '<p class="sidebar-group-title">Documentation</p>',
        '<ul class="sidebar-nav">',
        f'<li><a href="index.html" class="{"active" if current_slug == "index" else ""}">Overview &amp; Index</a></li>',
        '</ul></div>',
    ]
    for cat, items in groups.items():
        parts.append(f'<div class="sidebar-group"><p class="sidebar-group-title">{html.escape(cat)}</p><ul class="sidebar-nav">')
        for slug, title in items:
            cls = ' class="active"' if slug == current_slug else ""
            parts.append(f'<li><a href="{slug}.html"{cls}>{html.escape(title)}</a></li>')
        parts.append("</ul></div>")
    return "\n".join(parts)


def render_docs_index(version: str) -> str:
    head = shared_head(
        f"Documentation — Noctyx {version}",
        "Complete documentation for Noctyx: installation, web console, site config, GPU recipes, HF mirror, 3-2-1 backups, bare-metal restore, self-repair, and troubleshooting.",
        "..",
        version,
        "docs/index.html",
    )
    nav = shared_nav("..", version, active="docs", is_docs=True)
    sidebar = build_sidebar("index")
    footer = shared_modals_and_footer("..", version)

    groups: dict[str, list[tuple[str, str, str]]] = {}
    for slug, _, title, cat, desc in DOCS:
        groups.setdefault(cat, []).append((slug, title, desc))

    sections = []
    for cat, items in groups.items():
        cards = "".join(
            f'<a class="doc-hub-card" href="{slug}.html"><h3>{html.escape(title)}</h3><p>{html.escape(desc)}</p></a>'
            for slug, title, desc in items
        )
        sections.append(
            f'<h2 id="{re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-")}">{html.escape(cat)}</h2>'
            f'<div class="docs-hub-grid" style="grid-template-columns:repeat(auto-fill,minmax(250px,1fr));margin-bottom:28px">{cards}</div>'
        )

    return f"""{head}
<body>
{nav}
<div class="docs-shell">
  <aside class="docs-sidebar" aria-label="Documentation navigation">{sidebar}</aside>
  <main class="docs-main">
    <div class="docs-breadcrumb"><a href="../index.html">Home</a> <span>/</span> <span>Documentation</span></div>
    <article class="prose">
      <h1>Noctyx {html.escape(version)} Documentation</h1>
      <p>Comprehensive reference for installing, operating, backing up, monitoring, and automating AI clusters with Noctyx. Press <kbd>/</kbd> or <kbd>⌘K</kbd> anywhere to search across all {len(DOCS)} guides, CLI commands, and config keys.</p>
      {"".join(sections)}
    </article>
  </main>
  <aside class="docs-toc" aria-label="On this page">
    <p class="toc-title">Categories</p>
    <ul class="toc-list">
      {"".join(f'<li><a href="#{re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-")}">{html.escape(c)}</a></li>' for c in groups)}
    </ul>
  </aside>
</div>
{footer}"""


def render_doc_page(idx: int, version: str) -> tuple[str, dict[str, str]]:
    slug, src_rel, nav_title, category, desc = DOCS[idx]
    src_path = ROOT / src_rel
    md_text = src_path.read_text(encoding="utf-8")
    body_html, toc, plain = render_markdown(md_text)

    head = shared_head(
        f"{nav_title} — Noctyx {version} Docs",
        desc,
        "..",
        version,
        f"docs/{slug}.html",
    )
    nav = shared_nav("..", version, active="docs", is_docs=True)
    sidebar = build_sidebar(slug)
    footer = shared_modals_and_footer("..", version)

    toc_items = []
    for lvl, sid, label in toc:
        cls = ' class="toc-h3"' if lvl == 3 else ""
        toc_items.append(f'<li><a href="#{sid}"{cls}>{html.escape(label)}</a></li>')
    toc_html = (
        f'<aside class="docs-toc" aria-label="On this page"><p class="toc-title">On this page</p>'
        f'<ul class="toc-list">{"".join(toc_items)}</ul></aside>'
        if toc_items else '<aside class="docs-toc"></aside>'
    )

    prev_html = "<div></div>"
    if idx > 0:
        ps, _, pt, _, _ = DOCS[idx - 1]
        prev_html = f'<a class="doc-pager prev" href="{ps}.html"><small>← Previous</small><strong>{html.escape(pt)}</strong></a>'
    next_html = "<div></div>"
    if idx + 1 < len(DOCS):
        ns, _, nt, _, _ = DOCS[idx + 1]
        next_html = f'<a class="doc-pager next" href="{ns}.html"><small>Next →</small><strong>{html.escape(nt)}</strong></a>'

    gh_edit = f"{REPO_URL}/blob/main/{src_rel}"
    page_html = f"""{head}
<body>
{nav}
<div class="docs-shell">
  <aside class="docs-sidebar" aria-label="Documentation navigation">{sidebar}</aside>
  <main class="docs-main">
    <div class="docs-breadcrumb">
      <a href="../index.html">Home</a> <span>/</span>
      <a href="index.html">Docs</a> <span>/</span>
      <span>{html.escape(category)}</span>
    </div>
    <article class="prose">
      {body_html}
    </article>
    <div class="doc-footer-nav">
      {prev_html}
      {next_html}
    </div>
    <div class="edit-row">
      <span>Source: <code>{html.escape(src_rel)}</code></span>
      <a href="{gh_edit}" target="_blank" rel="noopener">View or edit this page on GitHub ↗</a>
    </div>
  </main>
  {toc_html}
</div>
{footer}"""

    search_entry = {
        "title": nav_title,
        "category": category,
        "summary": desc,
        "url": f"docs/{slug}.html",
        "headings": " ".join(lbl for _, _, lbl in toc),
        "text": re.sub(r"\s+", " ", plain)[:6000],
    }
    return page_html, search_entry


def render_404(version: str) -> str:
    head = shared_head(f"Page Not Found — Noctyx {version}", "Page not found on Noctyx documentation.", ".", version, "404.html")
    nav = shared_nav(".", version, active="404")
    footer = shared_modals_and_footer(".", version)
    return f"""{head}
<body>
{nav}
<main class="container" style="padding:110px 24px;text-align:center">
  <p class="eyebrow"><span class="eyebrow-dot"></span><span>404 · Route Not Found</span></p>
  <h1 style="font-size:2.6rem;margin:0 0 14px">That page isn't on this cluster.</h1>
  <p style="color:var(--text-muted);max-width:520px;margin:0 auto 28px">Press <kbd>/</kbd> to search all 17 guides, or jump back to the home page.</p>
  <div class="hero-ctas">
    <a class="btn btn-primary" href="index.html">Back to Home</a>
    <a class="btn btn-secondary" href="docs/index.html">Browse Documentation</a>
  </div>
</main>
{footer}"""


def build(out_dir: pathlib.Path) -> None:
    version = read_version()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "docs").mkdir(parents=True, exist_ok=True)

    # Copy static assets
    shutil.copytree(ROOT / "docs" / "assets", out_dir / "assets")
    shutil.copytree(ROOT / "docs" / "screenshots", out_dir / "screenshots")
    shutil.copy2(ROOT / "site" / "style.css", out_dir / "style.css")
    shutil.copy2(ROOT / "site" / "app.js", out_dir / "app.js")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    # Render landing & docs index & 404
    (out_dir / "index.html").write_text(render_landing(version), encoding="utf-8")
    (out_dir / "docs" / "index.html").write_text(render_docs_index(version), encoding="utf-8")
    (out_dir / "404.html").write_text(render_404(version), encoding="utf-8")

    # Render every doc page and collect search index
    search_index: list[dict[str, str]] = [
        {
            "title": "Noctyx Home & Overview",
            "category": "Landing",
            "summary": "The night-shift operator for your AI cluster: deploy, cache, protect, watch, heal, architecture, and all 16 screenshots.",
            "url": "index.html",
            "headings": "What's new in 0.9.6 Five pillars Architecture Screenshots Quickstart Demo",
            "text": "DGX Spark RTX Apple Silicon restic 3-2-1 HF mirror write-through Alertmanager ntfy Pushover Slack Telegram deb sudoers",
        }
    ]
    for idx in range(len(DOCS)):
        slug = DOCS[idx][0]
        page_html, entry = render_doc_page(idx, version)
        (out_dir / "docs" / f"{slug}.html").write_text(page_html, encoding="utf-8")
        search_index.append(entry)

    (out_dir / "search-index.json").write_text(json.dumps(search_index, ensure_ascii=False), encoding="utf-8")
    print(f"Built Noctyx {version} site -> {out_dir} ({len(DOCS)} guides, {len(SCREENSHOTS)} screenshots)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Noctyx GitHub Pages site")
    ap.add_argument("--out", default="_site", help="Output directory (default: _site)")
    args = ap.parse_args()
    build(pathlib.Path(args.out))


if __name__ == "__main__":
    main()
