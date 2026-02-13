/**
 * RepDoctor2 -- Frontend JavaScript
 * Vanilla JS, no frameworks. Retro terminal aesthetic.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  /**
   * Safely encode text for insertion via innerHTML.
   */
  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  /**
   * POST JSON to a route and return the parsed response.
   * Rejects with an Error whose message is the server-provided error text.
   */
  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) {
          throw new Error(data.error || "Server error (" + resp.status + ")");
        }
        return data;
      });
    });
  }

  /**
   * GET JSON from a route.
   */
  function getJson(url) {
    return fetch(url, {
      credentials: "same-origin",
    }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) {
          throw new Error(data.error || "Server error (" + resp.status + ")");
        }
        return data;
      });
    });
  }

  /**
   * Set a button into a loading state, returning a restore function.
   */
  function setButtonLoading(button, loadingText) {
    var original = button.textContent;
    var wasDisabled = button.disabled;
    button.textContent = loadingText;
    button.disabled = true;
    button.classList.add("btn-loading");
    return function restore(text) {
      button.textContent = text !== undefined ? text : original;
      button.disabled = wasDisabled;
      button.classList.remove("btn-loading");
    };
  }

  /**
   * Show a brief flash of feedback text on a button, then revert.
   */
  function flashButton(button, text, durationMs) {
    var original = button.textContent;
    button.textContent = text;
    setTimeout(function () {
      button.textContent = original;
    }, durationMs || 1500);
  }

  /**
   * Display an inline error message near a given element.
   * If no container is found it falls back to window.alert.
   */
  function showError(message, nearElement) {
    // Try to find or create an error container near the element
    if (nearElement) {
      var container = nearElement.closest(".branch-card") || nearElement.parentElement;
      if (container) {
        var existing = container.querySelector(".inline-error");
        if (existing) {
          existing.textContent = "ERR: " + message;
          return;
        }
        var errDiv = document.createElement("div");
        errDiv.className = "inline-error";
        errDiv.textContent = "ERR: " + message;
        container.appendChild(errDiv);
        // Auto-remove after 8 seconds
        setTimeout(function () {
          if (errDiv.parentNode) {
            errDiv.parentNode.removeChild(errDiv);
          }
        }, 8000);
        return;
      }
    }
    alert("Error: " + message);
  }

  // ---------------------------------------------------------------------------
  // 1. Copy to Clipboard
  // ---------------------------------------------------------------------------

  /**
   * Copy text to clipboard and show "COPIED!" feedback on the triggering button.
   *
   * @param {string}      text   - The text to copy.
   * @param {HTMLElement}  button - The button element that was clicked.
   */
  window.copyToClipboard = function copyToClipboard(text, button) {
    if (!text) return;

    var doCopy;

    if (navigator.clipboard && window.isSecureContext) {
      doCopy = navigator.clipboard.writeText(text);
    } else {
      // Fallback for non-HTTPS / older browsers
      doCopy = new Promise(function (resolve, reject) {
        var textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        try {
          var ok = document.execCommand("copy");
          document.body.removeChild(textarea);
          ok ? resolve() : reject(new Error("execCommand returned false"));
        } catch (err) {
          document.body.removeChild(textarea);
          reject(err);
        }
      });
    }

    doCopy
      .then(function () {
        if (button) {
          flashButton(button, "COPIED!", 1500);
          button.classList.add("btn-copied");
          setTimeout(function () {
            button.classList.remove("btn-copied");
          }, 1500);
        }
      })
      .catch(function () {
        if (button) {
          flashButton(button, "COPY FAILED", 1500);
        }
      });
  };

  // ---------------------------------------------------------------------------
  // 2. AI Analysis
  // ---------------------------------------------------------------------------

  /**
   * Return the CSS class for a risk-level badge.
   */
  function riskBadgeClass(level) {
    switch ((level || "").toUpperCase()) {
      case "LOW":
        return "badge-low";
      case "MEDIUM":
        return "badge-medium";
      case "HIGH":
        return "badge-high";
      default:
        return "badge-unknown";
    }
  }

  /**
   * Return the CSS class for a feature-assessment badge.
   */
  function assessmentBadgeClass(assessment) {
    switch ((assessment || "").toUpperCase()) {
      case "SHOULD_MERGE":
        return "badge-merge";
      case "OPTIONAL":
        return "badge-optional";
      case "OBSOLETE":
        return "badge-obsolete";
      case "UNCLEAR":
        return "badge-unclear";
      default:
        return "badge-unknown";
    }
  }

  /**
   * Build the HTML that replaces the analysis placeholder inside a branch card.
   */
  function buildAnalysisHtml(analysis, branchName) {
    var safeId = branchName.replace(/[^a-zA-Z0-9_-]/g, "_");
    var riskClass = riskBadgeClass(analysis.risk_level);
    var assessClass = assessmentBadgeClass(analysis.feature_assessment);

    var html = '<div class="analysis-results">';

    // Badges row
    html += '<div class="analysis-badges">';
    html +=
      '<span class="badge ' +
      riskClass +
      '">RISK: ' +
      escapeHtml(analysis.risk_level || "?") +
      "</span> ";
    html +=
      '<span class="badge ' +
      assessClass +
      '">' +
      escapeHtml((analysis.feature_assessment || "?").replace(/_/g, " ")) +
      "</span>";
    html += "</div>";

    // Summary
    html +=
      '<div class="analysis-summary">' +
      escapeHtml(analysis.plain_english_summary || "") +
      "</div>";

    // Conflict prediction
    if (analysis.conflict_prediction) {
      html +=
        '<div class="analysis-conflicts"><strong>&gt; Conflicts:</strong> ' +
        escapeHtml(analysis.conflict_prediction) +
        "</div>";
    }

    // Merge strategy
    if (analysis.merge_strategy) {
      html +=
        '<div class="analysis-strategy"><strong>&gt; Strategy:</strong> ' +
        escapeHtml(analysis.merge_strategy) +
        "</div>";
    }

    // Spec alignment
    if (analysis.spec_alignment) {
      html +=
        '<div class="analysis-spec"><strong>&gt; Spec alignment:</strong> ' +
        escapeHtml(analysis.spec_alignment) +
        "</div>";
    }

    // Claude Code instructions (collapsible)
    if (analysis.claude_code_instructions) {
      var instrId = "instr-" + safeId;
      html += '<div class="analysis-instructions">';
      html +=
        '<button class="btn btn-sm btn-outline" onclick="toggleInstructions(\'' +
        instrId +
        "')\">CLAUDE CODE INSTRUCTIONS</button>";
      html += ' <button class="btn btn-sm btn-outline" onclick="copyToClipboard(' +
        escapeAttr(analysis.claude_code_instructions) +
        ', this)">COPY INSTRUCTIONS</button>';
      html +=
        '<pre id="' +
        instrId +
        '" class="instructions-block" style="display:none;">' +
        escapeHtml(analysis.claude_code_instructions) +
        "</pre>";
      html += "</div>";
    }

    html += "</div>";
    return html;
  }

  /**
   * Escape a string for safe embedding inside an HTML attribute / onclick.
   * Returns the string wrapped in a JSON-encoded form suitable for JS.
   */
  function escapeAttr(str) {
    return (
      "decodeURIComponent('" +
      encodeURIComponent(str).replace(/'/g, "%27") +
      "')"
    );
  }

  /**
   * Analyze a single branch via the /analyze endpoint.
   *
   * @param {string}      owner      - Repository owner.
   * @param {string}      repoName   - Repository name.
   * @param {string}      branchName - Branch name.
   * @param {string}      commitSha  - HEAD commit SHA of the branch.
   * @param {HTMLElement}  button     - The button that triggered the action.
   */
  window.analyzeBranch = function analyzeBranch(
    owner,
    repoName,
    branchName,
    commitSha,
    button
  ) {
    var restore = setButtonLoading(button, "ANALYZING...");

    postJson("/analyze", {
      owner: owner,
      repo_name: repoName,
      branch_name: branchName,
      commit_sha: commitSha,
    })
      .then(function (data) {
        var analysis = data.analysis;
        var card = button.closest(".branch-card");

        if (card) {
          // Insert analysis results
          var target =
            card.querySelector(".analysis-placeholder") ||
            card.querySelector(".branch-actions");
          if (target) {
            var wrapper = document.createElement("div");
            wrapper.innerHTML = buildAnalysisHtml(analysis, branchName);
            if (target.classList.contains("analysis-placeholder")) {
              target.innerHTML = wrapper.innerHTML;
            } else {
              target.parentNode.insertBefore(wrapper, target.nextSibling);
            }
          }

          // Add a cached indicator to the button
          if (data.from_cache) {
            restore("ANALYZED (CACHED)");
          } else {
            restore("ANALYZED");
          }
          button.disabled = true;
        } else {
          restore("ANALYZED");
          button.disabled = true;
        }

        // Update session cost display
        updateSessionCostDisplay();
      })
      .catch(function (err) {
        restore();
        showError(err.message, button);
      });
  };

  /**
   * Estimate the cost of analyzing a branch.
   *
   * @param {string}      repoName   - Repository name.
   * @param {string}      branchName - Branch name.
   * @param {HTMLElement}  button     - The button that triggered the action.
   */
  window.estimateCost = function estimateCost(repoName, branchName, button) {
    var restore = setButtonLoading(button, "ESTIMATING...");

    postJson("/estimate", {
      repo_name: repoName,
      branch_name: branchName,
    })
      .then(function (data) {
        var msg =
          "Est. " +
          data.estimated_tokens.toLocaleString() +
          " tokens | ~$" +
          data.estimated_cost.toFixed(4) +
          " (" +
          data.model +
          ")";
        restore(msg);
        // Auto-revert after a few seconds
        setTimeout(function () {
          button.textContent = "ESTIMATE COST";
          button.disabled = false;
        }, 5000);
      })
      .catch(function (err) {
        restore();
        showError(err.message, button);
      });
  };

  // ---------------------------------------------------------------------------
  // 3. Archive
  // ---------------------------------------------------------------------------

  /**
   * Archive a branch by creating a tag, then display delete instructions.
   *
   * @param {string}      owner      - Repository owner.
   * @param {string}      repoName   - Repository name.
   * @param {string}      branchName - Branch name.
   * @param {string}      commitSha  - HEAD commit SHA.
   * @param {HTMLElement}  button     - The triggering button.
   */
  window.archiveBranch = function archiveBranch(
    owner,
    repoName,
    branchName,
    commitSha,
    button
  ) {
    var note = prompt(
      "Optional archive note for '" + branchName + "' (leave blank to skip):"
    );
    if (note === null) return; // User cancelled

    var restore = setButtonLoading(button, "ARCHIVING...");

    postJson("/archive/create", {
      owner: owner,
      repo_name: repoName,
      branch_name: branchName,
      commit_sha: commitSha,
      note: note || "",
    })
      .then(function (data) {
        restore("ARCHIVED");
        button.disabled = true;

        // Show delete instructions in the branch card
        var card = button.closest(".branch-card");
        if (card && data.delete_instructions) {
          var instrDiv = document.createElement("div");
          instrDiv.className = "archive-result";
          instrDiv.innerHTML =
            '<div class="archive-success">' +
            '<span class="badge badge-archived">ARCHIVED AS: ' +
            escapeHtml(data.tag_name) +
            "</span>" +
            "</div>" +
            '<div class="archive-delete-instructions">' +
            "<strong>&gt; Delete instructions (copy and paste into terminal):</strong>" +
            '<pre class="instructions-block">' +
            escapeHtml(data.delete_instructions) +
            "</pre>" +
            '<button class="btn btn-sm btn-outline" onclick="copyToClipboard(' +
            escapeAttr(data.delete_instructions) +
            ", this)\">COPY DELETE INSTRUCTIONS</button>" +
            "</div>";
          card.appendChild(instrDiv);
        }
      })
      .catch(function (err) {
        restore();
        showError(err.message, button);
      });
  };

  /**
   * Fetch reinstate instructions for an archived branch.
   *
   * @param {string} owner      - Repository owner.
   * @param {string} repoName   - Repository name.
   * @param {string} branchName - Original branch name.
   * @param {string} tagName    - Archive tag name.
   */
  window.reinstateInstructions = function reinstateInstructions(
    owner,
    repoName,
    branchName,
    tagName
  ) {
    // Find or create the container for instructions
    var safeId =
      "reinstate-" + tagName.replace(/[^a-zA-Z0-9_-]/g, "_");
    var existing = document.getElementById(safeId);
    if (existing) {
      // Toggle visibility
      existing.style.display =
        existing.style.display === "none" ? "block" : "none";
      return;
    }

    postJson("/archive/reinstate-instructions", {
      owner: owner,
      repo_name: repoName,
      branch_name: branchName,
      tag_name: tagName,
    })
      .then(function (data) {
        // Find the archive card for this tag
        var cards = document.querySelectorAll(".archive-card");
        var targetCard = null;
        for (var i = 0; i < cards.length; i++) {
          if (cards[i].getAttribute("data-tag") === tagName) {
            targetCard = cards[i];
            break;
          }
        }

        var instrHtml =
          '<div id="' +
          safeId +
          '" class="reinstate-instructions">' +
          "<strong>&gt; Reinstate instructions:</strong>" +
          '<pre class="instructions-block">' +
          escapeHtml(data.instructions) +
          "</pre>" +
          '<button class="btn btn-sm btn-outline" onclick="copyToClipboard(' +
          escapeAttr(data.instructions) +
          ", this)\">COPY INSTRUCTIONS</button>" +
          "</div>";

        if (targetCard) {
          var wrapper = document.createElement("div");
          wrapper.innerHTML = instrHtml;
          targetCard.appendChild(wrapper.firstChild);
        } else {
          // Fallback: append after the clicked button's parent
          var body = document.querySelector(".archive-list") || document.body;
          var wrapper2 = document.createElement("div");
          wrapper2.innerHTML = instrHtml;
          body.appendChild(wrapper2.firstChild);
        }
      })
      .catch(function (err) {
        alert("Error: " + err.message);
      });
  };

  // ---------------------------------------------------------------------------
  // 4. Mark as Done
  // ---------------------------------------------------------------------------

  /**
   * Mark a branch as "done" -- dims the card and moves it to the bottom.
   *
   * @param {string}      repoName   - Repository name.
   * @param {string}      branchName - Branch name.
   * @param {HTMLElement}  checkbox   - The checkbox or button element.
   */
  window.markDone = function markDone(repoName, branchName, checkbox) {
    var card = checkbox.closest(".branch-card");
    var isDone;

    if (checkbox.type === "checkbox") {
      isDone = checkbox.checked;
    } else {
      // Toggle button variant
      isDone = !checkbox.classList.contains("done");
    }

    postJson("/api/mark-done", {
      repo_name: repoName,
      branch_name: branchName,
    })
      .then(function () {
        if (!card) return;

        if (isDone) {
          card.classList.add("branch-done");
          // Move card to bottom of its container
          var parent = card.parentElement;
          if (parent) {
            parent.appendChild(card);
          }
          if (checkbox.type !== "checkbox") {
            checkbox.classList.add("done");
            checkbox.textContent = "DONE";
          }
        } else {
          card.classList.remove("branch-done");
          if (checkbox.type !== "checkbox") {
            checkbox.classList.remove("done");
            checkbox.textContent = "MARK DONE";
          }
        }
      })
      .catch(function (err) {
        // Revert checkbox
        if (checkbox.type === "checkbox") {
          checkbox.checked = !isDone;
        }
        showError(err.message, checkbox);
      });
  };

  // ---------------------------------------------------------------------------
  // 5. Analyze All
  // ---------------------------------------------------------------------------

  /**
   * Sequentially analyze every un-analyzed branch in a repository.
   *
   * @param {string} owner    - Repository owner.
   * @param {string} repoName - Repository name.
   */
  window.analyzeAll = function analyzeAll(owner, repoName) {
    var buttons = document.querySelectorAll(
      '.branch-card .btn-analyze:not([disabled])'
    );

    if (buttons.length === 0) {
      alert("All branches have already been analyzed.");
      return;
    }

    var total = buttons.length;
    var completed = 0;

    // Find the "Analyze All" button to show progress
    var allBtn = document.querySelector(".btn-analyze-all");
    var restoreAll;
    if (allBtn) {
      restoreAll = setButtonLoading(
        allBtn,
        "ANALYZING 0/" + total + "..."
      );
    }

    function updateProgress() {
      completed++;
      if (allBtn && restoreAll) {
        if (completed < total) {
          allBtn.textContent = "ANALYZING " + completed + "/" + total + "...";
        } else {
          restoreAll("ALL ANALYZED (" + total + ")");
          allBtn.disabled = true;
        }
      }
    }

    // Process buttons sequentially to avoid rate limits
    var queue = Array.prototype.slice.call(buttons);

    function processNext() {
      if (queue.length === 0) return;

      var btn = queue.shift();
      var card = btn.closest(".branch-card");

      // Extract data attributes from the button or card
      var branchName =
        btn.getAttribute("data-branch") ||
        (card ? card.getAttribute("data-branch") : null);
      var commitSha =
        btn.getAttribute("data-sha") ||
        (card ? card.getAttribute("data-sha") : null);

      if (!branchName || !commitSha) {
        updateProgress();
        processNext();
        return;
      }

      var restore = setButtonLoading(btn, "ANALYZING...");

      postJson("/analyze", {
        owner: owner,
        repo_name: repoName,
        branch_name: branchName,
        commit_sha: commitSha,
      })
        .then(function (data) {
          var analysis = data.analysis;

          if (card) {
            var target =
              card.querySelector(".analysis-placeholder") ||
              card.querySelector(".branch-actions");
            if (target) {
              var wrapper = document.createElement("div");
              wrapper.innerHTML = buildAnalysisHtml(analysis, branchName);
              if (target.classList.contains("analysis-placeholder")) {
                target.innerHTML = wrapper.innerHTML;
              } else {
                target.parentNode.insertBefore(wrapper, target.nextSibling);
              }
            }
          }

          if (data.from_cache) {
            restore("ANALYZED (CACHED)");
          } else {
            restore("ANALYZED");
          }
          btn.disabled = true;

          updateProgress();
          updateSessionCostDisplay();
          processNext();
        })
        .catch(function (err) {
          restore("FAILED");
          btn.classList.add("btn-error");
          updateProgress();
          processNext();
        });
    }

    processNext();
  };

  // ---------------------------------------------------------------------------
  // 6. Toggle Instruction Panels
  // ---------------------------------------------------------------------------

  /**
   * Expand or collapse an instruction code block by its element ID.
   *
   * @param {string} id - The DOM id of the instruction block.
   */
  window.toggleInstructions = function toggleInstructions(id) {
    var el = document.getElementById(id);
    if (!el) return;

    if (el.style.display === "none" || el.style.display === "") {
      el.style.display = "block";
    } else {
      el.style.display = "none";
    }
  };

  // ---------------------------------------------------------------------------
  // 7. Session Cost Updater
  // ---------------------------------------------------------------------------

  /**
   * Fetch the latest session cost from the server and update the footer.
   */
  function updateSessionCostDisplay() {
    getJson("/api/session-cost")
      .then(function (data) {
        var el = document.getElementById("session-cost-display");
        if (!el) return;

        var parts = [];
        parts.push("$" + data.total_cost.toFixed(4));
        parts.push(data.analyses_count + " analyses");
        parts.push(
          (data.total_input_tokens + data.total_output_tokens).toLocaleString() +
            " tokens"
        );

        el.textContent = parts.join(" | ");
        el.title =
          "Input: " +
          data.total_input_tokens.toLocaleString() +
          " | Output: " +
          data.total_output_tokens.toLocaleString();
      })
      .catch(function () {
        // Silently ignore -- the footer just won't update
      });
  }

  // Periodic cost refresh (every 30 seconds)
  var _costInterval = null;

  function startCostPolling() {
    if (_costInterval) return;
    // Initial fetch
    updateSessionCostDisplay();
    _costInterval = setInterval(updateSessionCostDisplay, 30000);
  }

  function stopCostPolling() {
    if (_costInterval) {
      clearInterval(_costInterval);
      _costInterval = null;
    }
  }

  // Start polling when the page loads (only if the display element exists)
  document.addEventListener("DOMContentLoaded", function () {
    if (document.getElementById("session-cost-display")) {
      startCostPolling();
    }
  });

  // Stop polling when the page is hidden (battery/perf friendly)
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopCostPolling();
    } else if (document.getElementById("session-cost-display")) {
      startCostPolling();
    }
  });

  // ---------------------------------------------------------------------------
  // 8. Archive Search / Filter
  // ---------------------------------------------------------------------------

  /**
   * Initialise the archive search box if we are on the archive page.
   * Filters .archive-card elements by text content matching the query.
   */
  function initArchiveSearch() {
    var input = document.getElementById("archive-search");
    if (!input) return;

    var debounceTimer = null;

    input.addEventListener("input", function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        filterArchives(input.value);
      }, 200);
    });

    // Allow clearing with Escape
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        input.value = "";
        filterArchives("");
      }
    });
  }

  /**
   * Show/hide archive cards based on a search query.
   *
   * @param {string} query - Text to match against repo name, branch name, etc.
   */
  function filterArchives(query) {
    var cards = document.querySelectorAll(".archive-card");
    var normalised = (query || "").toLowerCase().trim();
    var visibleCount = 0;

    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      if (!normalised) {
        card.style.display = "";
        visibleCount++;
        continue;
      }

      // Search across data attributes and visible text
      var searchable = [
        card.getAttribute("data-repo") || "",
        card.getAttribute("data-branch") || "",
        card.getAttribute("data-tag") || "",
        card.textContent || "",
      ]
        .join(" ")
        .toLowerCase();

      if (searchable.indexOf(normalised) !== -1) {
        card.style.display = "";
        visibleCount++;
      } else {
        card.style.display = "none";
      }
    }

    // Update the result count if a counter element exists
    var counter = document.getElementById("archive-count");
    if (counter) {
      if (normalised) {
        counter.textContent = visibleCount + " of " + cards.length + " archives";
      } else {
        counter.textContent = cards.length + " archives";
      }
    }
  }

  // Expose filterArchives for external use if needed
  window.filterArchives = filterArchives;

  document.addEventListener("DOMContentLoaded", initArchiveSearch);

  // ---------------------------------------------------------------------------
  // 9. Keyboard Shortcuts & UX Niceties
  // ---------------------------------------------------------------------------

  document.addEventListener("keydown", function (e) {
    // Ctrl+K or Cmd+K -- focus the archive search box if present
    if ((e.ctrlKey || e.metaKey) && e.key === "k") {
      var searchInput = document.getElementById("archive-search");
      if (searchInput) {
        e.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    }
  });

  // ---------------------------------------------------------------------------
  // 10. Auto-collapse long commit lists
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var commitLists = document.querySelectorAll(".commit-list");
    for (var i = 0; i < commitLists.length; i++) {
      var list = commitLists[i];
      var items = list.querySelectorAll("li");
      if (items.length > 5) {
        // Hide items beyond the first 5
        for (var j = 5; j < items.length; j++) {
          items[j].style.display = "none";
          items[j].classList.add("commit-hidden");
        }
        // Add "show more" toggle
        var toggle = document.createElement("li");
        toggle.className = "commit-toggle";
        toggle.textContent =
          "+ " + (items.length - 5) + " more commits...";
        toggle.style.cursor = "pointer";
        toggle.addEventListener(
          "click",
          (function (parentList, toggleItem) {
            return function () {
              var hidden = parentList.querySelectorAll(".commit-hidden");
              var isExpanded = toggleItem.getAttribute("data-expanded") === "1";
              for (var k = 0; k < hidden.length; k++) {
                hidden[k].style.display = isExpanded ? "none" : "";
              }
              if (isExpanded) {
                toggleItem.textContent =
                  "+ " + hidden.length + " more commits...";
                toggleItem.setAttribute("data-expanded", "0");
              } else {
                toggleItem.textContent = "- show fewer";
                toggleItem.setAttribute("data-expanded", "1");
              }
            };
          })(list, toggle)
        );
        list.appendChild(toggle);
      }
    }
  });
})();
