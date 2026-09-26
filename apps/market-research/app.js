(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const tokenInput = $("#token");
  const form = $("#request-form");
  const dashboardNode = $("#dashboard");
  const errorNode = $("#error-panel");
  let dashboard = null;
  let selectedMetric = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }

  function localDay(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function initializeDates() {
    const today = new Date();
    const yearAgo = new Date(today);
    yearAgo.setFullYear(today.getFullYear() - 1);
    $("#start-at").value = localDay(yearAgo);
    $("#end-at").value = localDay(today);
    $("#public-cutoff").value = localDay(today);
    $("#acquired-cutoff").value = localDay(today);
  }

  function dayStart(value) {
    const parsed = new Date(`${value}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) throw new Error("Enter a valid calendar date.");
    return parsed.getTime();
  }

  function dayEnd(value) {
    const parsed = new Date(`${value}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) throw new Error("Enter a valid calendar date.");
    parsed.setDate(parsed.getDate() + 1);
    return parsed.getTime() - 1;
  }

  function requestPayload() {
    const peers = $("#peers").value.split(",").map((value) => value.trim()).filter(Boolean);
    const endDate = $("#end-at").value;
    const payload = {
      namespace: $("#namespace").value.trim(),
      listing_id: $("#listing-id").value.trim(),
      start_ms: dayStart($("#start-at").value),
      end_ms: dayEnd(endDate),
      acquired_by_ms: dayEnd($("#acquired-cutoff").value),
      publicly_available_by_ms: dayEnd($("#public-cutoff").value),
      peer_listing_ids: peers,
      include_calculations: true,
    };
    const universe = $("#universe-id").value.trim();
    const industry = $("#industry-code").value.trim();
    const currency = $("#currency").value.trim().toUpperCase();
    if (universe) {
      payload.universe_id = universe;
      payload.universe_as_of_ms = dayEnd(endDate);
    }
    if (industry) payload.industry_code = industry;
    if (currency) payload.common_currency = currency;
    if (selectedMetric) payload.fact_metric_requests = [selectedMetric];
    if (payload.start_ms >= payload.end_ms) throw new Error("The price-window start must be earlier than its end.");
    if (payload.publicly_available_by_ms > payload.acquired_by_ms) {
      throw new Error("The public-information cutoff cannot be later than the acquired-by cutoff.");
    }
    return payload;
  }

  function setStatus(message, state = "idle") {
    const status = $("#run-state");
    status.replaceChildren();
    const icon = node("span", state === "loading" ? "···" : state === "ready" ? "✓" : state === "error" ? "!" : "↗", "state-icon");
    icon.setAttribute("aria-hidden", "true");
    status.append(icon, node("span", message));
    status.dataset.state = state;
  }

  function showError(title, detail) {
    errorNode.replaceChildren(node("strong", title), node("span", detail));
    errorNode.classList.remove("hidden");
    setStatus(title, "error");
  }

  function clearError() {
    errorNode.replaceChildren();
    errorNode.classList.add("hidden");
  }

  async function loadDashboard(event) {
    if (event) event.preventDefault();
    clearError();
    selectedMetric = selectedMetric || null;
    let payload;
    try {
      payload = requestPayload();
    } catch (error) {
      showError("Request needs attention", error.message);
      return;
    }
    const token = tokenInput.value.trim().replace(/^Bearer\s+/i, "");
    if (!token) {
      showError("Access token required", "Paste an access token for the Noesis API.");
      return;
    }
    sessionStorage.setItem("noesis.market.token", token);
    setStatus("Loading authorized, point-in-time data…", "loading");
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/v1/market/company-dashboard", {
        method: "POST",
        headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify(payload),
        credentials: "same-origin",
        cache: "no-store",
      });
      let envelope;
      try { envelope = await response.json(); } catch { throw new Error(`The API returned an unreadable response (${response.status}).`); }
      if (!response.ok || envelope.ok === false) {
        const code = envelope.error && envelope.error.code ? `${envelope.error.code}: ` : "";
        const message = envelope.error && envelope.error.message ? envelope.error.message : envelope.detail || `Request failed (${response.status}).`;
        throw new Error(`${code}${message}`);
      }
      dashboard = envelope.result || envelope;
      if (!dashboard || !Array.isArray(dashboard.panels) || dashboard.panels.length === 0) {
        throw new Error("The API response did not contain a company dashboard panel.");
      }
      renderDashboard(dashboard);
      dashboardNode.classList.remove("hidden");
      setStatus(`${dashboard.peer_count || 0} eligible peer${dashboard.peer_count === 1 ? "" : "s"} · source cutoffs applied`, "ready");
    } catch (error) {
      showError("Workspace could not be built", error.message || "Unexpected API error.");
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
    }
  }

  async function resolveTicker() {
    const token = tokenInput.value.trim().replace(/^Bearer\s+/i, "") || sessionStorage.getItem("noesis.market.token");
    const symbol = $("#symbol").value.trim();
    const namespace = $("#namespace").value.trim();
    const status = $("#lookup-status");
    const candidatesNode = $("#lookup-candidates");
    candidatesNode.replaceChildren();
    if (!token || !namespace || !symbol) {
      status.textContent = "Enter the namespace, an exact ticker and an API access token first.";
      return;
    }
    let query;
    try {
      query = new URLSearchParams({
        namespace,
        symbol,
        as_of_ms: String(dayEnd($("#end-at").value)),
        acquired_by_ms: String(dayEnd($("#acquired-cutoff").value)),
        publicly_available_by_ms: String(dayEnd($("#public-cutoff").value)),
      });
    } catch (error) {
      status.textContent = error.message;
      return;
    }
    const mic = $("#lookup-mic").value.trim();
    if (mic) query.set("mic", mic);
    status.textContent = "Resolving against authorized instrument revisions…";
    sessionStorage.setItem("noesis.market.token", token);
    try {
      const response = await fetch(`/api/v1/market/instruments/lookup?${query.toString()}`, {
        headers: { "Authorization": `Bearer ${token}`, "Accept": "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      const envelope = await response.json();
      if (!response.ok || envelope.ok === false) {
        const message = envelope.error && envelope.error.message || envelope.detail || `Lookup failed (${response.status}).`;
        throw new Error(message);
      }
      const result = envelope.result || envelope;
      const candidates = result.candidates || [];
      if (result.status === "resolved" && candidates.length === 1) {
        const candidate = candidates[0];
        $("#listing-id").value = candidate.listing.listing_id;
        status.textContent = `${candidate.listing.ticker || symbol.toUpperCase()} resolved to ${candidate.listing.listing_id} · ${candidate.listing.mic || "venue not stated"}.`;
        return;
      }
      if (!candidates.length) {
        status.textContent = `No listing resolved at these cutoffs${result.reason ? ` · ${result.reason}` : ""}. The lookup receipt is retained by the API.`;
        return;
      }
      status.textContent = `Ticker is ambiguous (${candidates.length} candidates). Choose the dated listing explicitly:`;
      for (const candidate of candidates) {
        const listing = candidate.listing || {};
        const issuer = candidate.issuer || {};
        const choice = node("button", `${listing.ticker || symbol.toUpperCase()} · ${listing.mic || "venue unknown"} · ${issuer.display_name || listing.listing_id}`, "candidate-button");
        choice.type = "button";
        choice.addEventListener("click", () => {
          $("#listing-id").value = listing.listing_id || "";
          $("#lookup-mic").value = listing.mic || "";
          status.textContent = `${listing.listing_id || "Listing selected"} · ${listing.mic || "venue not stated"}.`;
          candidatesNode.replaceChildren();
        });
        candidatesNode.append(choice);
      }
    } catch (error) {
      status.textContent = `Lookup unavailable: ${error.message || "request failed"}`;
    }
  }

  function formatNumber(value, maximumFractionDigits = 2) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(number);
  }

  function formatDate(value) {
    if (value === null || value === undefined || value === "") return "Not reported";
    const date = typeof value === "number" ? new Date(value) : new Date(String(value));
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "2-digit", timeZone: "UTC" }).format(date);
  }

  function periodText(period) {
    if (!period || typeof period !== "object") return "Period not stated";
    if (period.kind === "instant") return `As of ${period.instant_date || "date unavailable"}`;
    if (period.kind === "duration") return `${period.start_date || "?"} → ${period.end_date || "?"}`;
    return JSON.stringify(period);
  }

  function textValue(value) {
    if (value === null || value === undefined || value === "") return "Not available";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function identityFor(panel) {
    const identity = panel.identity || {};
    return {
      issuer: identity.issuer || {},
      security: identity.security || {},
      listing: identity.listing || {},
    };
  }

  function panelTitle(panel) {
    const identity = identityFor(panel);
    return identity.issuer.display_name || identity.issuer.name || identity.listing.ticker || panel.listing_id;
  }

  function renderCompanyCards(data) {
    const target = $("#company-cards");
    target.replaceChildren();
    for (const panel of data.panels) {
      const identity = identityFor(panel);
      const bars = (panel.prices && panel.prices.items) || [];
      const facts = (panel.statements && panel.statements.items) || [];
      const latestBar = bars.reduce((latest, current) => !latest || current.bar_start_ms > latest.bar_start_ms ? current : latest, null);
      const latestPrice = latestBar ? `${formatNumber(latestBar.close)} ${identity.listing.currency || latestBar.currency || ""}`.trim() : "No price data";
      const latestFact = facts.reduce((latest, current) => !latest || Number(current.public_at_ms || 0) > Number(latest.public_at_ms || 0) ? current : latest, null);
      const card = node("article", null, `company-card ${panel.role === "subject" ? "subject" : ""}`.trim());
      const top = node("div", null, "card-topline");
      top.append(node("span", panel.role || "company", "role-chip"), node("span", identity.listing.ticker || identity.listing.mic || panel.listing_id, "ticker-chip"));
      card.append(top, node("h3", panelTitle(panel)), node("p", `${identity.listing.mic || "Venue not stated"} · ${identity.security.industry_code || "Industry not stated"} · ${identity.listing.currency || "Currency not stated"}`, "security-line"));
      const stats = node("div", null, "card-stats");
      stats.append(stat("Latest close", latestPrice), stat("Price observations", bars.length), stat("Filed facts", facts.length), stat("Latest filing availability", latestFact ? formatDate(latestFact.public_at_ms) : "No statement data"));
      card.append(stats);
      const foot = node("div", null, "card-footline");
      foot.append(node("span", `Prices · ${(panel.prices || {}).state || "unknown"}`), node("span", `Statements · ${(panel.statements || {}).state || "unknown"}`, "state-pill"));
      card.append(foot);
      target.append(card);
    }
  }

  function stat(label, value) {
    const wrap = node("div", null, "card-stat");
    wrap.append(node("span", label), node("strong", value));
    return wrap;
  }

  function renderDashboard(data) {
    const selection = data.selection || {};
    $("#selection-summary").textContent = `${selection.common_currency || "Currency varies"} · ${selection.currency_conversion || "No conversion"} · ${selection.period_alignment || "Reported periods preserved"}`;
    renderCompanyCards(data);
    renderExclusions(data.exclusions || []);
    renderPricePanels(data.panels || []);
    renderStatementPanels(data.panels || []);
    renderEventPanels(data.panels || []);
    renderAudit(data);
    populateFactSelectors(data.panels && data.panels[0]);
    $("#valuation-note").textContent = "A valuation multiple requires a correctly sourced point-in-time numerator (such as market value) and a compatible trailing-period denominator. If either input is absent, the calculation remains unavailable; this workspace does not infer market capitalization from price and shares.";
  }

  function renderExclusions(exclusions) {
    const target = $("#exclusions");
    target.replaceChildren();
    if (!exclusions.length) {
      target.append(node("span", "No peer exclusions were reported.", "exclusion"));
      return;
    }
    for (const item of exclusions) {
      const id = item.object_id || "candidate";
      const reason = item.reason || "excluded";
      target.append(node("span", `${id} · ${reason}`, "exclusion"));
    }
  }

  function emptyState(message) {
    return node("div", message, "empty-copy");
  }

  function chartFor(bars, selectionChanged) {
    const width = 820;
    const height = 190;
    const left = 54;
    const right = 14;
    const top = 15;
    const bottom = 28;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Daily closing-price history. Select a point for source details.");
    svg.classList.add("price-chart");
    const values = bars.map((bar) => Number(bar.close)).filter(Number.isFinite);
    let low = Math.min(...values);
    let high = Math.max(...values);
    if (low === high) { low -= Math.max(1, Math.abs(low) * 0.05); high += Math.max(1, Math.abs(high) * 0.05); }
    const pad = (high - low) * 0.08;
    low -= pad;
    high += pad;
    for (let step = 0; step < 3; step += 1) {
      const y = top + ((height - top - bottom) * step) / 2;
      const grid = document.createElementNS(svg.namespaceURI, "line");
      grid.setAttribute("x1", left);
      grid.setAttribute("x2", width - right);
      grid.setAttribute("y1", y);
      grid.setAttribute("y2", y);
      grid.classList.add("chart-gridline");
      svg.append(grid);
      const label = document.createElementNS(svg.namespaceURI, "text");
      label.setAttribute("x", "0");
      label.setAttribute("y", String(y + 3));
      label.classList.add("chart-label");
      label.textContent = formatNumber(high - ((high - low) * step) / 2);
      svg.append(label);
    }
    const coords = bars.map((bar, index) => {
      const x = bars.length === 1 ? (left + width - right) / 2 : left + ((width - left - right) * index) / (bars.length - 1);
      const y = height - bottom - ((Number(bar.close) - low) / (high - low)) * (height - top - bottom);
      return { x, y, bar };
    }).filter((point) => Number.isFinite(Number(point.bar.close)));
    const line = document.createElementNS(svg.namespaceURI, "polyline");
    line.setAttribute("points", coords.map((point) => `${point.x},${point.y}`).join(" "));
    line.classList.add("chart-line");
    svg.append(line);
    for (let index = 0; index < coords.length; index += 1) {
      const point = coords[index];
      const circle = document.createElementNS(svg.namespaceURI, "circle");
      circle.setAttribute("cx", String(point.x));
      circle.setAttribute("cy", String(point.y));
      circle.setAttribute("r", "3.5");
      circle.setAttribute("tabindex", "0");
      circle.setAttribute("aria-label", `${formatDate(point.bar.bar_start_ms)} close ${formatNumber(point.bar.close)}`);
      circle.classList.add("chart-dot");
      circle.addEventListener("click", () => selectionChanged(point.bar, index));
      circle.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectionChanged(point.bar, index); }
      });
      svg.append(circle);
    }
    if (bars.length) {
      const first = document.createElementNS(svg.namespaceURI, "text");
      first.setAttribute("x", String(left));
      first.setAttribute("y", String(height - 5));
      first.classList.add("chart-label");
      first.textContent = formatDate(bars[0].bar_start_ms);
      const last = document.createElementNS(svg.namespaceURI, "text");
      last.setAttribute("x", String(width - right));
      last.setAttribute("y", String(height - 5));
      last.setAttribute("text-anchor", "end");
      last.classList.add("chart-label");
      last.textContent = formatDate(bars[bars.length - 1].bar_start_ms);
      svg.append(first, last);
    }
    return svg;
  }

  function renderPricePanels(panels) {
    const target = $("#price-panels");
    target.replaceChildren();
    for (const panel of panels) {
      const card = node("article", null, "panel-card");
      card.append(panelHeader(panel, "Price history", (panel.prices || {}).state));
      const bars = (panel.prices && panel.prices.items) || [];
      if (!bars.length) {
        card.append(emptyState(panelMessage(panel, "prices", "No price observations are available at these cutoffs.")));
        target.append(card);
        continue;
      }
      const chartWrap = node("div", null, "chart-wrap");
      const selected = node("div", "Select a point to inspect its revision, OHLCV and source reference.", "selected-observation");
      function selectBar(bar, index) {
        const details = [
          `Observation ${index + 1}: ${formatDate(bar.bar_start_ms)} · close ${formatNumber(bar.close)} ${bar.currency || ""}`,
          `open ${formatNumber(bar.open)} · high ${formatNumber(bar.high)} · low ${formatNumber(bar.low)} · volume ${formatNumber(bar.volume, 0)}`,
          `revision ${bar.revision_id || "not stated"} · public ${formatDate(bar.public_at_ms)} · basis ${bar.price_basis || "not stated"}`,
        ].join("\n");
        selected.replaceChildren(node("strong", details));
        appendSources(selected, bar.source_refs || []);
        const rows = card.querySelectorAll("tbody tr[data-selectable]");
        rows.forEach((row) => row.classList.toggle("selected-row", row.dataset.index === String(index)));
      }
      chartWrap.append(chartFor(bars, selectBar), selected);
      card.append(chartWrap);
      const table = node("table", null, "data-table");
      table.append(tableHead(["Session", "Open", "High", "Low", "Close", "Volume", "Revision"]));
      const body = node("tbody");
      bars.forEach((bar, index) => {
        const row = node("tr");
        row.dataset.selectable = "true";
        row.dataset.index = String(index);
        row.tabIndex = 0;
        for (const value of [formatDate(bar.bar_start_ms), formatNumber(bar.open), formatNumber(bar.high), formatNumber(bar.low), `${formatNumber(bar.close)} ${bar.currency || ""}`, formatNumber(bar.volume, 0), bar.revision_id || "—"]) {
          const cell = node("td", value);
          if (value === bar.revision_id) cell.classList.add("mono");
          row.append(cell);
        }
        row.addEventListener("click", () => selectBar(bar, index));
        row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectBar(bar, index); } });
        body.append(row);
      });
      table.append(body);
      const wrap = node("div", null, "data-table-wrap");
      wrap.append(table);
      card.append(wrap);
      selectBar(bars[bars.length - 1], bars.length - 1);
      target.append(card);
    }
  }

  function panelHeader(panel, title, state) {
    const head = node("div", null, "panel-head");
    const left = node("div");
    left.append(node("h3", panelTitle(panel)), node("p", `${title} · ${panel.listing_id}`));
    head.append(left, node("span", state || "unknown", "panel-badge"));
    return head;
  }

  function panelMessage(panel, key, fallback) {
    const error = (panel.errors || []).find((item) => item.panel === key);
    return error ? `${error.code || "Unavailable"}: ${error.message || fallback}` : fallback;
  }

  function tableHead(labels) {
    const head = node("thead");
    const row = node("tr");
    for (const label of labels) row.append(node("th", label));
    head.append(row);
    return head;
  }

  function renderStatementPanels(panels) {
    const target = $("#statement-panels");
    target.replaceChildren();
    for (const panel of panels) {
      const card = node("article", null, "panel-card");
      card.append(panelHeader(panel, "Financial facts", (panel.statements || {}).state));
      const facts = (panel.statements && panel.statements.items) || [];
      const reports = panel.metrics || {};
      const metrics = [...((reports.facts || {}).metrics || []), ...((reports.price || {}).metrics || [])];
      if (metrics.length) card.append(renderMetricList(metrics));
      if (!facts.length) {
        card.append(emptyState(panelMessage(panel, "statements", "No filed facts are available at these cutoffs.")));
        target.append(card);
        continue;
      }
      const table = node("table", null, "data-table");
      table.append(tableHead(["Statement", "Concept", "Reported value", "Unit", "Period", "Mapping", "Evidence"]));
      const body = node("tbody");
      for (const fact of facts) {
        const row = node("tr");
        row.append(
          node("td", fact.statement || "Unclassified"),
          node("td", fact.canonical_concept || fact.concept || "Unknown concept"),
          node("td", textValue(fact.value_lexical ?? fact.value)),
          node("td", fact.unit || "Not stated"),
          node("td", periodText(fact.period)),
          node("td", fact.mapping_status || "Not stated"),
        );
        const evidenceCell = node("td");
        const disclosure = node("details");
        disclosure.append(node("summary", "Inspect"));
        const grid = detailGrid([
          ["Taxonomy concept", `${fact.taxonomy || "?"}:${fact.concept || "?"}`],
          ["Filing accession", fact.filing_accession],
          ["Fiscal period", fact.fiscal_period || fact.period_class],
          ["Fact revision", fact.revision_id],
          ["Context", fact.context_id],
          ["Public at", fact.public_at_ms ? formatDate(fact.public_at_ms) : "Unknown"],
          ["Mapping status", fact.mapping_status],
          ["Quality", fact.quality_status || fact.diagnostics || "No row warning"],
        ]);
        disclosure.append(grid);
        appendSources(disclosure, fact.source_refs || []);
        evidenceCell.append(disclosure);
        row.append(evidenceCell);
        body.append(row);
      }
      table.append(body);
      const wrap = node("div", null, "data-table-wrap");
      wrap.append(table);
      card.append(wrap);
      target.append(card);
    }
  }

  function renderMetricList(metrics) {
    const list = node("div", null, "metric-list");
    for (const metric of metrics) {
      const card = node("article", null, "metric-card");
      card.append(node("span", metric.name || "Metric"), node("strong", metric.status === "available" ? `${formatNumber(metric.value, 6)} ${metric.unit || ""}`.trim() : "Unavailable"), node("small", metric.reason_code || metric.formula_version || metric.status || "Status not stated"));
      const detail = node("details");
      detail.append(node("summary", "Inputs and formula"), detailGrid([
        ["Formula version", metric.formula_version],
        ["Formula revision", metric.formula_revision_id],
        ["Numerator revision", metric.numerator_fact_revision_id],
        ["Denominator revision", metric.denominator_fact_revision_id],
        ["Price calculation", metric.quantitative_calculation_id],
        ["Period", periodText(metric.period)],
      ]));
      card.append(detail);
      list.append(card);
    }
    return list;
  }

  function populateFactSelectors(panel) {
    const facts = panel && panel.statements && panel.statements.items || [];
    for (const selector of [$("#metric-numerator"), $("#metric-denominator")]) {
      selector.replaceChildren();
      selector.append(node("option", facts.length ? "Choose an exact fact revision" : "No company facts available"));
      selector.options[0].value = "";
      for (const fact of facts) {
        if (!fact.revision_id) continue;
        const label = `${fact.canonical_concept || fact.concept || "fact"} · ${periodText(fact.period)} · ${textValue(fact.value_lexical ?? fact.value)} ${fact.unit || ""}`;
        const option = node("option", `${label} · ${String(fact.revision_id).slice(0, 22)}`);
        option.value = fact.revision_id;
        selector.append(option);
      }
    }
  }

  function renderEventPanels(panels) {
    const target = $("#event-panels");
    target.replaceChildren();
    for (const panel of panels) {
      const card = node("article", null, "panel-card");
      const events = panel.corporate_events && panel.corporate_events.items || [];
      card.append(panelHeader(panel, "Corporate events", (panel.corporate_events || {}).state));
      if (!events.length) {
        card.append(emptyState(panelMessage(panel, "corporate_events", "No corporate actions were recorded for this security in the selected period.")));
      } else {
        const list = node("div", null, "event-list");
        for (const event of events) {
          const row = node("div", null, "event-row");
          row.append(node("span", formatDate(event.effective_at_ms || event.ex_date_ms || event.recorded_at_ms), "mono"), node("strong", event.action_type || event.kind || "Corporate action"), node("span", event.description || event.status || "Terms not stated"));
          const detail = node("details");
          detail.append(node("summary", "Evidence"), detailGrid([["Revision", event.revision_id], ["Action ID", event.action_id], ["Adjustment rule", event.valuation_rule || "No approved rule"]]));
          appendSources(detail, event.source_refs || []);
          row.append(detail);
          list.append(row);
        }
        card.append(list);
      }
      target.append(card);
    }
  }

  function renderAudit(data) {
    const target = $("#audit-content");
    target.replaceChildren();
    const asOf = data.as_of || {};
    target.append(auditCard("Requested cutoffs", [
      ["Price window", `${formatDate(asOf.start_ms)} → ${formatDate(asOf.end_ms)}`],
      ["Public through", formatDate(asOf.publicly_available_by_ms)],
      ["Acquired through", formatDate(asOf.acquired_by_ms)],
      ["Currency policy", data.selection && data.selection.currency_conversion || "No conversion"],
      ["Period policy", data.selection && data.selection.period_alignment || "Source periods preserved"],
    ]));
    target.append(auditCard("Source freshness", [
      ["Distinct source references", (data.freshness || {}).source_ref_count || 0],
      ["Input record revisions", (data.drilldown || {}).source_revisions || []],
      ["Data quality", data.panels.map((panel) => `${panelTitle(panel)}: ${(panel.quality || {}).status || "review panel"}`).join("; ")],
    ]));
    const refs = data.freshness && data.freshness.source_refs || [];
    target.append(sourceAuditCard(refs));
    const errors = data.panels.flatMap((panel) => panel.errors || []);
    if (errors.length) target.append(auditCard("Panel errors", errors.map((item) => [item.panel || "panel", `${item.code || "error"}: ${item.message || "Unavailable"}`])));
    const limitations = data.limitations || [];
    if (limitations.length) target.append(auditCard("Known limitations", limitations.map((item, index) => [`${index + 1}`, item])));
  }

  function auditCard(title, rows) {
    const card = node("article", null, "audit-card");
    card.append(node("h3", title));
    const list = node("ul");
    for (const [label, value] of rows) {
      const item = node("li");
      item.append(node("strong", `${label}: `), node("span", Array.isArray(value) ? value.join(", ") || "None" : textValue(value)));
      list.append(item);
    }
    card.append(list);
    return card;
  }

  function sourceAuditCard(refs) {
    const card = node("article", null, "audit-card");
    const title = node("h3", `Source references · ${refs.length}`);
    card.append(title);
    if (!refs.length) {
      card.append(node("p", "No source references were returned for the selected cutoffs."));
      return card;
    }
    const list = node("ul");
    for (const ref of refs.slice(0, 30)) {
      const detail = `${ref.provider || "source"} · ${ref.source_revision_id || ref.source_ref_id || "revision unstated"} · public ${formatDate(ref.public_at_ms)}`;
      list.append(node("li", detail, "mono"));
    }
    if (refs.length > 30) list.append(node("li", `${refs.length - 30} additional sources retained in the API receipt.`));
    card.append(list);
    return card;
  }

  function detailGrid(pairs) {
    const grid = node("div", null, "detail-grid");
    for (const [label, value] of pairs) {
      if (value === undefined || value === null || value === "") continue;
      const item = node("div", null, "detail-item");
      item.append(node("span", label), node("strong", Array.isArray(value) ? value.join(", ") : textValue(value)));
      grid.append(item);
    }
    return grid;
  }

  function appendSources(parent, refs) {
    if (!refs.length) {
      parent.append(node("p", "No source reference attached to this record."));
      return;
    }
    const list = node("ul");
    for (const ref of refs.slice(0, 10)) {
      const item = node("li");
      const label = `${ref.provider || "source"} · ${ref.license_id || "license not stated"} · public ${formatDate(ref.public_at_ms)} · acquired ${formatDate(ref.retrieved_at_ms)}`;
      item.append(node("span", label));
      if (typeof ref.source_url === "string") {
        try {
          const url = new URL(ref.source_url, window.location.origin);
          if (url.protocol === "https:" || url.protocol === "http:") {
            const link = node("a", "Open source");
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            item.append(document.createTextNode(" · "), link);
          }
        } catch { /* malformed provider URL remains text-only */ }
      }
      item.append(node("div", `revision ${ref.source_revision_id || "not stated"} · reference ${ref.source_ref_id || "not stated"}`, "mono"));
      list.append(item);
    }
    parent.append(list);
  }

  $("#forget-token").addEventListener("click", () => {
    sessionStorage.removeItem("noesis.market.token");
    tokenInput.value = "";
    setStatus("This tab’s saved token was removed.");
  });
  $("#resolve-symbol").addEventListener("click", resolveTicker);
  form.addEventListener("submit", (event) => { selectedMetric = null; loadDashboard(event); });
  $("#metric-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!dashboard) {
      showError("Build the company workspace first", "Load a company so the metric uses its authorized fact revisions.");
      return;
    }
    const numerator = $("#metric-numerator").value;
    const denominator = $("#metric-denominator").value;
    const name = $("#metric-name").value.trim();
    if (!numerator || !denominator || !name) {
      showError("Choose two fact revisions", "A metric needs a name, numerator revision and denominator revision.");
      return;
    }
    selectedMetric = { name, kind: $("#metric-kind").value, numerator_fact_revision_id: numerator, denominator_fact_revision_id: denominator };
    loadDashboard();
  });

  initializeDates();
  const savedToken = sessionStorage.getItem("noesis.market.token");
  if (savedToken) tokenInput.value = savedToken;
})();
