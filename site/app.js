/* Noctyx GitHub Pages — interactive enhancements (zero dependencies) */
(function () {
  "use strict";

  const THEMES = ["noctyx", "phosphor", "light"];
  const THEME_LABELS = { noctyx: "Noctyx", phosphor: "Phosphor", light: "Light" };

  function applyTheme(t) {
    if (!THEMES.includes(t)) t = "noctyx";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("noctyx-site-theme", t); } catch (_) {}
    document.querySelectorAll("[data-theme-label]").forEach((el) => {
      el.textContent = THEME_LABELS[t];
    });
  }

  const savedTheme = (() => {
    try { return localStorage.getItem("noctyx-site-theme"); } catch (_) { return null; }
  })();
  applyTheme(savedTheme || "noctyx");

  document.addEventListener("click", (e) => {
    const tBtn = e.target.closest("[data-cycle-theme]");
    if (tBtn) {
      const cur = document.documentElement.getAttribute("data-theme") || "noctyx";
      const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
      applyTheme(next);
      return;
    }

    // Mobile docs sidebar toggle
    const sbBtn = e.target.closest("[data-toggle-sidebar]");
    if (sbBtn) {
      const sb = document.querySelector(".docs-sidebar");
      if (sb) sb.classList.toggle("open");
      return;
    }

    // Copy code / address buttons
    const copyBtn = e.target.closest("[data-copy-target], .copy-btn");
    if (copyBtn) {
      let text = copyBtn.getAttribute("data-copy-text");
      if (!text) {
        const block = copyBtn.closest(".code-block, .term-panel");
        const code = block ? block.querySelector("code, pre") : null;
        text = code ? code.innerText : "";
      }
      if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text.trim()).then(() => {
          const orig = copyBtn.textContent;
          copyBtn.textContent = "Copied";
          copyBtn.classList.add("copied");
          setTimeout(() => {
            copyBtn.textContent = orig;
            copyBtn.classList.remove("copied");
          }, 1500);
        });
      }
      return;
    }

    // Hero showcase switcher
    const scTab = e.target.closest(".showcase-tab");
    if (scTab) {
      const wrap = scTab.closest(".hero-showcase");
      wrap.querySelectorAll(".showcase-tab").forEach((b) => b.classList.remove("active"));
      scTab.classList.add("active");
      const img = wrap.querySelector("[data-showcase-img]");
      const cap = wrap.querySelector("[data-showcase-caption]");
      if (img) {
        img.src = scTab.getAttribute("data-src");
        img.alt = scTab.getAttribute("data-caption");
      }
      if (cap) cap.textContent = scTab.getAttribute("data-caption");
      return;
    }

    // Quickstart terminal tabs
    const termTab = e.target.closest(".term-tab");
    if (termTab) {
      const box = termTab.closest(".terminal-box");
      const idx = termTab.getAttribute("data-term");
      box.querySelectorAll(".term-tab").forEach((b) => b.classList.toggle("active", b === termTab));
      box.querySelectorAll(".term-panel").forEach((p) => p.classList.toggle("active", p.getAttribute("data-term") === idx));
      return;
    }

    // Gallery category filter
    const fBtn = e.target.closest(".filter-btn");
    if (fBtn) {
      const cat = fBtn.getAttribute("data-filter");
      document.querySelectorAll(".filter-btn").forEach((b) => b.classList.toggle("active", b === fBtn));
      document.querySelectorAll(".shot-card").forEach((c) => {
        const cats = (c.getAttribute("data-cats") || "").split(" ");
        c.classList.toggle("hidden", cat !== "all" && !cats.includes(cat));
      });
      return;
    }

    // Pillar quick scroll
    const pChip = e.target.closest(".pillar-chip");
    if (pChip) {
      const target = document.querySelector(pChip.getAttribute("data-scroll"));
      if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
  });

  // ── Fullscreen Lightbox ──────────────────────────────────────────
  const lb = document.getElementById("lightbox");
  const lbImg = document.getElementById("lb-img");
  const lbCap = document.getElementById("lb-cap");
  let lbItems = [];
  let lbIndex = 0;

  function openLightbox(items, idx) {
    if (!lb || !lbImg || !items.length) return;
    lbItems = items;
    lbIndex = Math.max(0, Math.min(idx, items.length - 1));
    renderLightbox();
    lb.classList.add("open");
    document.body.style.overflow = "hidden";
  }

  function closeLightbox() {
    if (!lb) return;
    lb.classList.remove("open");
    document.body.style.overflow = "";
  }

  function renderLightbox() {
    const it = lbItems[lbIndex];
    if (!it) return;
    lbImg.src = it.src;
    lbImg.alt = it.caption || "";
    const counter = lbItems.length > 1 ? ` (${lbIndex + 1} / ${lbItems.length})` : "";
    lbCap.textContent = (it.caption || "") + counter;
    document.querySelectorAll(".lb-nav").forEach((btn) => {
      btn.style.display = lbItems.length > 1 ? "" : "none";
    });
  }

  if (lb) {
    document.addEventListener("click", (e) => {
      const card = e.target.closest(".shot-card");
      if (card) {
        const visible = Array.from(document.querySelectorAll(".shot-card:not(.hidden)"));
        const items = visible.map((el) => ({
          src: el.querySelector("img").src,
          caption: (el.querySelector("h3")?.textContent || "") + " — " + (el.querySelector("p")?.textContent || ""),
        }));
        openLightbox(items, visible.indexOf(card));
        return;
      }

      const heroWrap = e.target.closest(".showcase-img-wrap");
      if (heroWrap) {
        const img = heroWrap.querySelector("img");
        openLightbox([{ src: img.src, caption: img.alt }], 0);
        return;
      }

      const archWrap = e.target.closest(".arch-frame");
      if (archWrap) {
        const img = archWrap.querySelector("img");
        openLightbox([{ src: img.src, caption: img.alt }], 0);
        return;
      }

      const docImg = e.target.closest(".prose img");
      if (docImg) {
        const all = Array.from(document.querySelectorAll(".prose img"));
        const items = all.map((el) => ({ src: el.src, caption: el.alt || "" }));
        openLightbox(items, all.indexOf(docImg));
        return;
      }

      if (e.target.closest("[data-lb-close]") || e.target === lb) {
        closeLightbox();
      } else if (e.target.closest("[data-lb-prev]")) {
        lbIndex = (lbIndex - 1 + lbItems.length) % lbItems.length;
        renderLightbox();
      } else if (e.target.closest("[data-lb-next]")) {
        lbIndex = (lbIndex + 1) % lbItems.length;
        renderLightbox();
      }
    });
  }

  // ── Search Modal (⌘K / /) ───────────────────────────────────────
  const searchModal = document.getElementById("search-modal");
  const searchInput = document.getElementById("search-input");
  const searchResults = document.getElementById("search-results");
  let searchData = null;
  let selectedIdx = 0;

  async function loadSearch() {
    if (searchData) return searchData;
    const base = document.documentElement.getAttribute("data-base") || ".";
    try {
      const resp = await fetch(`${base}/search-index.json`);
      searchData = await resp.json();
    } catch (_) {
      searchData = [];
    }
    return searchData;
  }

  function openSearch() {
    if (!searchModal) return;
    searchModal.classList.add("open");
    loadSearch().then(() => renderSearch(searchInput.value));
    setTimeout(() => searchInput && searchInput.focus(), 20);
  }

  function closeSearch() {
    if (!searchModal) return;
    searchModal.classList.remove("open");
  }

  function renderSearch(q) {
    if (!searchResults || !searchData) return;
    const base = document.documentElement.getAttribute("data-base") || ".";
    const terms = (q || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    let matches;
    if (!terms.length) {
      matches = searchData.slice(0, 8);
    } else {
      matches = searchData
        .map((item) => {
          const hay = `${item.title} ${item.category} ${item.headings} ${item.text}`.toLowerCase();
          let score = 0;
          for (const t of terms) {
            if (!hay.includes(t)) return null;
            if (item.title.toLowerCase().includes(t)) score += 10;
            if (item.headings.toLowerCase().includes(t)) score += 4;
            score += 1;
          }
          return { item, score };
        })
        .filter(Boolean)
        .sort((a, b) => b.score - a.score)
        .slice(0, 12)
        .map((x) => x.item);
    }
    selectedIdx = 0;
    if (!matches.length) {
      searchResults.innerHTML = `<div class="search-empty">No matching documentation for "${(q || "").replace(/</g, "&lt;")}".</div>`;
      return;
    }
    searchResults.innerHTML = matches
      .map((it, i) => {
        const href = `${base}/${it.url}`;
        const snippet = it.summary.length > 140 ? it.summary.slice(0, 140) + "…" : it.summary;
        return `<a class="search-item${i === 0 ? " selected" : ""}" href="${href}">
          <div class="search-item-title"><span>${it.title}</span><span class="search-item-cat">${it.category}</span></div>
          <div class="search-item-snippet">${snippet}</div>
        </a>`;
      })
      .join("");
  }

  if (searchModal) {
    document.addEventListener("click", (e) => {
      if (e.target.closest("[data-open-search]")) openSearch();
      else if (e.target === searchModal) closeSearch();
    });
    if (searchInput) {
      searchInput.addEventListener("input", () => renderSearch(searchInput.value));
    }
  }

  document.addEventListener("keydown", (e) => {
    if (lb && lb.classList.contains("open")) {
      if (e.key === "Escape") closeLightbox();
      else if (e.key === "ArrowLeft" && lbItems.length > 1) { lbIndex = (lbIndex - 1 + lbItems.length) % lbItems.length; renderLightbox(); }
      else if (e.key === "ArrowRight" && lbItems.length > 1) { lbIndex = (lbIndex + 1) % lbItems.length; renderLightbox(); }
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (searchModal && searchModal.classList.contains("open")) closeSearch();
      else openSearch();
      return;
    }
    if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      if (tag !== "input" && tag !== "textarea") {
        e.preventDefault();
        openSearch();
        return;
      }
    }
    if (searchModal && searchModal.classList.contains("open")) {
      const items = Array.from(searchResults.querySelectorAll(".search-item"));
      if (e.key === "Escape") { closeSearch(); return; }
      if (e.key === "ArrowDown" && items.length) {
        e.preventDefault();
        selectedIdx = (selectedIdx + 1) % items.length;
        items.forEach((el, i) => el.classList.toggle("selected", i === selectedIdx));
        items[selectedIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "ArrowUp" && items.length) {
        e.preventDefault();
        selectedIdx = (selectedIdx - 1 + items.length) % items.length;
        items.forEach((el, i) => el.classList.toggle("selected", i === selectedIdx));
        items[selectedIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && items[selectedIdx]) {
        e.preventDefault();
        window.location.href = items[selectedIdx].href;
      }
    }
  });

  // ── Docs Right-Rail ToC Active Scroll-Spy ───────────────────────
  const tocLinks = Array.from(document.querySelectorAll(".toc-list a"));
  if (tocLinks.length && "IntersectionObserver" in window) {
    const map = new Map();
    tocLinks.forEach((a) => {
      const id = a.getAttribute("href").slice(1);
      const target = document.getElementById(id);
      if (target) map.set(target, a);
    });
    const obs = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            tocLinks.forEach((l) => l.classList.remove("active"));
            const link = map.get(entry.target);
            if (link) link.classList.add("active");
          }
        });
      },
      { rootMargin: "-80px 0px -70% 0px", threshold: 0 }
    );
    map.forEach((_, el) => obs.observe(el));
  }
})();
