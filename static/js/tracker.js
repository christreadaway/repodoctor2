/* ============================================================
   Codebase Tracker — client-side behaviour.

   Responsibilities:
   - Tab switching (8 tabs).
   - Filters on Next Actions (priority + status) and Modules (status + search).
   - Show/hide prompt body, copy prompt to clipboard with server-side event log.
   - Cross-tab jumps (Overview CTAs → other tabs; row anchors).
   - Debug surface "Copy for Claude Code" formatter.

   Buffers events in window.__trackerLog so the debug surface can render
   the in-session timeline even without server log access.
   ============================================================ */

(function () {
  "use strict";

  if (window.__trackerLog === undefined) {
    window.__trackerLog = [];
  }
  function logEvent(verb, fields) {
    var entry = Object.assign({ ts: new Date().toISOString(), event: verb }, fields || {});
    window.__trackerLog.push(entry);
    if (window.__trackerLog.length > 200) {
      window.__trackerLog.splice(0, window.__trackerLog.length - 200);
    }
  }

  // Escape text before inserting it via innerHTML.
  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str == null ? "" : String(str)));
    return div.innerHTML;
  }

  // ---------- Tabs ----------
  function switchTab(name) {
    document.querySelectorAll(".tracker-tab").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".tracker-panel").forEach(function (p) {
      p.classList.toggle("active", p.dataset.panel === name);
    });
    logEvent("tab_view", { tab: name });
  }
  window.trackerSwitchTab = switchTab;

  document.addEventListener("click", function (e) {
    var tab = e.target.closest(".tracker-tab");
    if (tab) {
      switchTab(tab.dataset.tab);
      return;
    }

    var jump = e.target.closest(".tracker-jump");
    if (jump) {
      e.preventDefault();
      var t = jump.dataset.tab;
      if (t) switchTab(t);
      var row = jump.dataset.row;
      if (row) {
        setTimeout(function () {
          var el = document.getElementById("row-" + row);
          if (el) {
            el.scrollIntoView({ behavior: "smooth", block: "center" });
            el.classList.add("tracker-row-flash");
            setTimeout(function () { el.classList.remove("tracker-row-flash"); }, 1600);
          }
        }, 30);
      }
      return;
    }

    var toggle = e.target.closest(".tracker-toggle-prompt");
    if (toggle) {
      var target = document.getElementById(toggle.dataset.target);
      if (target) {
        target.hidden = !target.hidden;
        toggle.textContent = target.hidden ? "SHOW PROMPT" : "HIDE PROMPT";
      }
      return;
    }

    var copyBtn = e.target.closest(".tracker-copy-btn");
    if (copyBtn) {
      var actionId = copyBtn.dataset.actionId;
      var preId = "prompt-" + actionId;
      var pre = document.getElementById(preId);
      var text = pre ? pre.textContent : "";
      if (!text) return;

      var done = function (ok) {
        copyBtn.textContent = ok ? "COPIED!" : "COPY FAILED";
        setTimeout(function () { copyBtn.textContent = "COPY PROMPT"; }, 1500);
        logEvent(ok ? "copy_prompt" : "copy_prompt_failed", { actionId: actionId });
        var owner = copyBtn.dataset.owner;
        var repo = copyBtn.dataset.repo;
        if (owner && repo) {
          fetch("/api/tracker/" + owner + "/" + repo + "/copy-event", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action_id: actionId, ok: ok })
          }).catch(function () {});
        }
      };

      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
        document.body.removeChild(ta);
        done(ok);
      }
      return;
    }
  });

  // ---------- Next-action filters ----------
  // Status filter values:
  //   OPEN      = neither shipped nor dismissed (default view)
  //   BLOCKED   = status == 'blocked'
  //   DISMISSED = status == 'dismissed'
  //   ALL       = show everything (including shipped + dismissed)
  // Note: shipped items live in their own tab now and are excluded from the
  // Next Actions panel template-side, so SHIPPED filter is no longer needed here.
  function applyNextFilters() {
    var prioBtn = document.querySelector('.tracker-panel[data-panel="next"] .tracker-filter-chip.active[data-filter="priority"]');
    var statusBtn = document.querySelector('.tracker-panel[data-panel="next"] .tracker-filter-chip.active[data-filter="status"]');
    var prio = prioBtn ? prioBtn.dataset.value : "ALL";
    var stat = statusBtn ? statusBtn.dataset.value : "OPEN";
    document.querySelectorAll('.tracker-panel[data-panel="next"] .tracker-next-card').forEach(function (card) {
      var rowPrio = card.dataset.priority;
      var rowStat = card.dataset.status;
      var prioOk = prio === "ALL" || rowPrio === prio;
      var statOk = true;
      if (stat === "OPEN") statOk = (rowStat !== "shipped" && rowStat !== "dismissed");
      else if (stat === "BLOCKED") statOk = rowStat === "blocked";
      else if (stat === "DISMISSED") statOk = rowStat === "dismissed";
      // ALL → show everything
      card.style.display = (prioOk && statOk) ? "" : "none";
    });
  }

  function applyModuleFilters() {
    var btn = document.querySelector('.tracker-filter-chip.active[data-filter="mod-status"]');
    var status = btn ? btn.dataset.value : "ALL";
    var search = document.getElementById("tracker-module-search");
    var q = search ? search.value.trim().toLowerCase() : "";
    document.querySelectorAll(".tracker-module-card").forEach(function (card) {
      var s = card.dataset.status;
      var n = card.dataset.name || "";
      var statusOk = status === "ALL" || s === status;
      var qOk = !q || n.indexOf(q) !== -1;
      card.style.display = (statusOk && qOk) ? "" : "none";
    });
    document.querySelectorAll(".tracker-module-group").forEach(function (g) {
      var anyVisible = Array.prototype.some.call(
        g.querySelectorAll(".tracker-module-card"),
        function (c) { return c.style.display !== "none"; }
      );
      g.style.display = anyVisible ? "" : "none";
    });
  }

  document.addEventListener("click", function (e) {
    var chip = e.target.closest(".tracker-filter-chip");
    if (!chip) return;
    var group = chip.dataset.filter;
    // Scope the active-class toggle to the same panel so filter chips in
    // Next Actions don't fight filter chips in Modules.
    var scope = chip.closest(".tracker-panel") || document;
    scope.querySelectorAll('.tracker-filter-chip[data-filter="' + group + '"]')
      .forEach(function (c) { c.classList.remove("active"); });
    chip.classList.add("active");
    logEvent("filter_change", { filter: group, value: chip.dataset.value });
    if (group === "priority" || group === "status") applyNextFilters();
    if (group === "mod-status") applyModuleFilters();
  });

  // ---------- Clickable headline stats ----------
  // Each stat in the page header jumps to the right tab and pre-applies
  // the filter that matches its label (e.g. "9 P0 open" → Next Actions tab
  // with priority=P0 and status=Open).
  function jumpToFilter(tab, filterKey, filterValue) {
    switchTab(tab);
    if (!filterKey) return;
    var scope = document.querySelector('.tracker-panel[data-panel="' + tab + '"]') || document;
    var target = scope.querySelector(
      '.tracker-filter-chip[data-filter="' + filterKey + '"][data-value="' + filterValue + '"]'
    );
    if (target) {
      // simulate the same click handler that drives chip-active state
      target.click();
    }
  }

  document.addEventListener("click", function (e) {
    var stat = e.target.closest(".tracker-stat-jump");
    if (!stat) return;
    var tab = stat.dataset.jumpTab;
    var filterKey = stat.dataset.jumpFilter;
    var filterValue = stat.dataset.jumpValue;
    if (tab) jumpToFilter(tab, filterKey, filterValue);
  });

  var search = document.getElementById("tracker-module-search");
  if (search) {
    search.addEventListener("input", function () {
      applyModuleFilters();
      logEvent("filter_change", { filter: "module-search", value: search.value });
    });
  }

  // ---------- Debug copy-for-Claude-Code ----------
  window.trackerCopyDebug = function () {
    var pre = document.getElementById("tracker-debug-log");
    var meta = document.querySelector(".tracker-meta-list");
    var errsBlock = document.querySelector(".tracker-debug-errors");
    var lines = ["```text", "[tracker debug — " + new Date().toISOString() + "]"];
    if (meta) {
      meta.querySelectorAll("li").forEach(function (li) {
        lines.push("- " + li.textContent.replace(/\s+/g, " ").trim());
      });
    }
    if (errsBlock) {
      lines.push("");
      lines.push("Integrity errors:");
      errsBlock.querySelectorAll("li").forEach(function (li) {
        lines.push("- " + li.textContent.trim());
      });
    }
    lines.push("");
    lines.push("Last events:");
    if (pre && pre.textContent) {
      lines.push(pre.textContent.trim());
    } else {
      lines.push("(no server-side log events)");
    }
    lines.push("```");
    var text = lines.join("\n");
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text);
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
    }
    var btn = document.querySelector(".tracker-log-actions .btn");
    if (btn) {
      var orig = btn.textContent;
      btn.textContent = "COPIED!";
      setTimeout(function () { btn.textContent = orig; }, 1500);
    }
  };

  // ---------- Generation loading overlay ----------
  // GENERATE TRACKER takes 20-90 seconds. Without feedback the page just sits
  // there and the user wonders if anything's happening. We show a centered
  // overlay with the repo name, an animated "ANALYZING" pulse, and a live
  // elapsed-time counter. The form still submits normally; the overlay stays
  // until the page reloads with the result.
  function showGenerateOverlay(repo, model) {
    if (document.getElementById("tracker-overlay")) return;
    var overlay = document.createElement("div");
    overlay.id = "tracker-overlay";
    overlay.className = "tracker-overlay";
    overlay.innerHTML =
      '<div class="tracker-overlay-card">' +
        '<div class="tracker-overlay-spinner"></div>' +
        '<h2 class="tracker-overlay-title">ANALYZING<span class="tracker-overlay-dots"><span>.</span><span>.</span><span>.</span></span></h2>' +
        '<p class="tracker-overlay-repo">' + escapeHtml(repo) + '</p>' +
        '<p class="tracker-overlay-detail">Reading docs + file tree + recent commits, then asking ' +
          '<code>' + escapeHtml(model || "the configured model") + '</code> to produce the tracker JSON.</p>' +
        '<p class="tracker-overlay-elapsed">Elapsed: <span id="tracker-overlay-time">0s</span></p>' +
        '<p class="tracker-overlay-foot">Typical: 20-90 seconds. Don\'t close this tab.</p>' +
      '</div>';
    document.body.appendChild(overlay);

    var t0 = Date.now();
    var timeEl = document.getElementById("tracker-overlay-time");
    var iv = setInterval(function () {
      if (!timeEl || !document.body.contains(overlay)) {
        clearInterval(iv);
        return;
      }
      var s = Math.floor((Date.now() - t0) / 1000);
      timeEl.textContent = s + "s";
    }, 1000);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target.closest(".tracker-gen-form");
    if (!form) return;
    var btn = form.querySelector("button[type=submit]");
    if (btn) {
      btn.disabled = true;
      btn.dataset._origText = btn.textContent;
      var lt = btn.getAttribute("data-loading-text");
      if (lt) btn.textContent = lt;
    }
    var modelSelect = form.querySelector(".tracker-model-select");
    var modelLabel = "";
    if (modelSelect) {
      var opt = modelSelect.options[modelSelect.selectedIndex];
      modelLabel = opt ? opt.textContent.replace(/^—.*?—\s*/, "").trim() : "";
    }
    // Pull the repo name from the page so the overlay is specific.
    var repoSel = document.getElementById("tracker-pick");
    var repoLabel = "this repo";
    if (repoSel && repoSel.value) {
      repoLabel = repoSel.value;
    } else {
      var titleEl = document.querySelector(".page-header h1");
      if (titleEl) repoLabel = titleEl.textContent.trim();
    }
    showGenerateOverlay(repoLabel, modelLabel);
    logEvent("generate_start_clicked", { repo: repoLabel, model: modelLabel });
  });

  // ---------- Init ----------
  // If URL has a hash like #row-N5, scroll there once the panel is correct.
  if (window.location.hash) {
    var id = window.location.hash.slice(1);
    var el = document.getElementById(id);
    if (el) {
      var panel = el.closest(".tracker-panel");
      if (panel) switchTab(panel.dataset.panel);
      setTimeout(function () { el.scrollIntoView({ block: "center" }); }, 50);
    }
  }

  logEvent("page_ready");
})();
