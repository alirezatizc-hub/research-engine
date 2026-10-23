(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const token = new URLSearchParams(location.search).get("token") || "";
  const fmt = (n, d = 0) => (n === null || n === undefined || Number.isNaN(Number(n)))
    ? "—" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });
  const pct = (n, d = 2) => (n === null || n === undefined) ? "—" : (n > 0 ? "+" : "") + Number(n).toFixed(d) + "٪";
  const tone = n => n > 0 ? "good" : n < 0 ? "bad" : "";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTs = v => {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    const d = Number.isFinite(n) && String(v).length <= 12 ? new Date(n * 1000) : new Date(v);
    return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString("fa-IR", { hour12: false });
  };
  const timeOf = s => {
    if (!s) return "—";
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? String(s).slice(11, 19) : d.toLocaleTimeString("fa-IR", { hour12: false });
  };
  let toastTimer = null;
  function toast(msg, kind = "") {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast " + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
  }
  async function api(path, opts = {}) {
    const headers = { "X-Requested-With": "wallexbot", ...(opts.headers || {}) };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers["X-UI-Token"] = token;
    const r = await fetch(path + (token ? (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : ""), {
      method: opts.method || (opts.body !== undefined ? "POST" : "GET"),
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
    let j = {};
    try { j = await r.json(); } catch (_) { /* noop */ }
    if (!r.ok || j.ok === false) {
      const err = new Error(j.error || ("HTTP " + r.status));
      err.payload = j;
      throw err;
    }
    return j;
  }

  /* ---------------- tabs ---------------- */
  $$(".tab").forEach(t => t.addEventListener("click", () => {
    $$(".tab").forEach(x => x.classList.remove("active"));
    $$(".panel").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    $("#tab-" + t.dataset.tab).classList.add("active");
    if (t.dataset.tab === "backtest") loadBacktest($("#btPick").value);
    if (t.dataset.tab === "trades") { loadTrades(); loadDecisions(); }
    if (t.dataset.tab === "settings") { loadSettings(); loadGate(); }
    if (t.dataset.tab === "tools") { loadCommands(); }
    if (location.hash !== "#" + t.dataset.tab) history.replaceState(null, "", "#" + t.dataset.tab);
  }));
  const startTab = (location.hash || "").replace("#", "");
  if (startTab) { const t = $(`.tab[data-tab="${startTab}"]`); if (t) t.click(); }

  /* ---------------- charts ---------------- */
  function drawChart(canvas, pts, opts = {}) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || 600, H = Number(canvas.getAttribute("height")) || 240;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.height = H + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const pad = { l: 10, r: 10, t: 12, b: 20 };
    if (!pts || pts.length < 2) {
      ctx.fillStyle = "#61779a"; ctx.font = "12px Vazirmatn, sans-serif";
      ctx.fillText("داده‌ای برای نمایش نیست", W - 160, H / 2);
      return;
    }
    const xs = pts.map(p => Number(p[0])), ys = pts.map(p => Number(p[1]));
    let minY = Math.min(...ys), maxY = Math.max(...ys);
    if (minY === maxY) { minY -= 1; maxY += 1; }
    const span = maxY - minY; minY -= span * 0.08; maxY += span * 0.08;
    const X = x => pad.l + (W - pad.l - pad.r) * ((x - xs[0]) / Math.max(1, xs[xs.length - 1] - xs[0]));
    const Y = y => pad.t + (H - pad.t - pad.b) * (1 - (y - minY) / (maxY - minY));
    ctx.strokeStyle = "rgba(28,43,69,.7)"; ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const y = pad.t + (H - pad.t - pad.b) * i / 3;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    }
    const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
    grad.addColorStop(0, "rgba(34,211,166,.28)"); grad.addColorStop(1, "rgba(34,211,166,0)");
    ctx.beginPath();
    pts.forEach((p, i) => { const x = X(Number(p[0])), y = Y(Number(p[1])); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    const lastX = X(xs[xs.length - 1]);
    ctx.lineTo(lastX, H - pad.b); ctx.lineTo(pad.l, H - pad.b); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();
    ctx.beginPath();
    pts.forEach((p, i) => { const x = X(Number(p[0])), y = Y(Number(p[1])); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = opts.color || "#22d3a6"; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
    ctx.fillStyle = "#61779a"; ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.fillText(fmt(maxY, 0), 4, pad.t + 8);
    ctx.fillText(fmt(minY, 0), 4, H - pad.b - 2);
    ctx.textAlign = "right";
    ctx.fillText(opts.label || "", W - pad.r, pad.t + 8);
  }

  /* ---------------- dashboard ---------------- */
  let lastCurve = null;
  let lastStatus = null;
  function setPill(el, text, kind) {
    el.textContent = text;
    el.className = "pill " + (kind || "pill-muted");
  }
  function renderStatus(s) {
    lastStatus = s;
    const running = !!s.running;
    if (s.source === "replay") $("#modeSelect").value = "replay";
    else if (s.mode && ["shadow", "live"].includes(s.mode)) $("#modeSelect").value = s.mode;
    setPill($("#connPill"), running ? "متصل · در حال اجرا" : "آماده", running ? "pill-good" : "pill-muted");
    const mode = s.mode || "—";
    const isReplay = s.source === "replay" || mode === "replay";
    setPill($("#modePill"), mode === "live" ? "LIVE — سفارش واقعی" : isReplay ? "REPLAY — دادهٔ ضبط‌شده" : mode === "shadow" ? "SHADOW — بدون سفارش" : "—",
      mode === "live" ? "pill-live" : isReplay ? "pill-warn" : "pill-good");
    if (isReplay && s.replay && s.replay.demo_profile) {
      const b = $("#banner");
      b.classList.remove("hidden"); b.className = "banner warn";
      b.textContent = "حالت دمو: replay روی دادهٔ ضبط‌شده با پروفایل فعال‌تر؛ نتیجهٔ این اجرا سند عملکرد واقعی نیست.";
    }
    const hl = s.hours_left;
    setPill($("#clockPill"), hl === null || hl === undefined ? "بدون پایان چالش" :
      (hl > 0 ? "تا پایان: " + fmt(hl, 1) + " ساعت" : "زمان چالش گذشته"), hl !== null && hl < 6 ? "pill-warn" : "pill-muted");

    const eq = Number(s.equity || 0), ret = s.return_pct;
    $("#kpiEquity").textContent = fmt(eq);
    $("#kpiEquityFoot").textContent = "تومان · نقد: " + (s.cash === null || s.cash === undefined ? "—" : fmt(s.cash));
    $("#kpiReturn").textContent = pct(ret);
    $("#kpiReturn").className = "kpi-value " + tone(ret);
    $("#kpiReturnFoot").textContent = "نسبت به " + fmt(s.initial_capital || 5000000) + " تومان";
    $("#kpiRealized").textContent = fmt(s.realized_pnl);
    $("#kpiRealized").className = "kpi-value " + tone(s.realized_pnl);
    $("#kpiFees").textContent = "کارمزد: " + fmt(s.fees_paid) + " · باز unrealized: " + fmt(s.unrealized_pnl);
    const dd = (s.risk || {}).drawdown_pct;
    $("#kpiDrawdown").textContent = pct(dd ? -Math.abs(dd) : 0);
    $("#kpiDrawdown").className = "kpi-value " + (dd > 5 ? "bad" : dd > 2 ? "warn" : "");
    $("#kpiPeak").textContent = "اوج: " + fmt((s.risk || {}).peak);
    $("#kpiPositions").textContent = fmt((s.positions || []).length);
    $("#kpiCash").textContent = "قانون: ≤" + fmt(3) + " پوزیشن";
    $("#kpiTrades").textContent = fmt(s.trades);
    const win = s.trades ? (100 * (s.wins || 0) / s.trades) : 0;
    $("#kpiWinRate").textContent = "برد: " + (s.trades ? fmt(win, 1) + "٪" : "—") + " · امروز: " + fmt(s.trades_today);

    const rows = [
      ["وضعیت اجرا", running ? "در حال اجرا" : "متوقف"],
      ["منبع داده", s.source === "replay" ? "Replay " + ((s.replay || {}).label || "") : s.source === "runner" ? "بازار زنده" : "فایل وضعیت"],
      ["بازارها تأییدشده", s.market_ok === null || s.market_ok === undefined ? "—" : (s.market_ok ? "بله (" + (s.allowed || []).length + " نماد)" : "خیر")],
      ["حالت ریسک AI", (s.risk || {}).ai_mode || "—"],
      ["Kill-Switch", (s.risk || {}).killed ? "فعال — ورود متوقف" : "غیرفعال"],
      ["AI", s.ai ? (s.ai.enabled ? (s.ai.provider || "") + " · " + (s.ai.model || "") : "rules-only") : "—"],
      ["Heartbeat", s.heartbeat_age_s == null ? "—" : fmt(s.heartbeat_age_s, 1) + " ثانیه"],
      ["آخرین اسکن", s.last_scan ? (s.last_scan.ts || "").slice(11, 19) + " — " + (s.last_scan.candidates || []).length + " سیگنال" : "—"],
    ];
    $("#engineList").innerHTML = rows.map(([k, v]) => `<div class="status-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
    setPill($("#engineBadge"), s.halted ? "HALT" : running ? "RUNNING" : "IDLE", s.halted ? "pill-bad" : running ? "pill-good" : "pill-muted");

    const gf = s.gate_failures || [];
    $("#gateBox").innerHTML = s.demo
      ? `<div class="gate-item">حالت دمو: دروازهٔ live غیرفعال است${gf.length ? ` — ${gf.length} مورد تا فعال‌سازی` : ""}</div>`
      : gf.length
      ? `<div class="gate-item bad">دروازهٔ Live باز نیست — ${gf.length} مورد باقی مانده</div>` +
        gf.slice(0, 3).map(x => `<div class="gate-item bad">${esc(x)}</div>`).join("") +
        (gf.length > 3 ? `<div class="muted small">و ${gf.length - 3} مورد دیگر…</div>` : "")
      : `<div class="gate-item ok">همهٔ شرایط live برقرار است</div>`;

    const pos = s.positions || [];
    const tb = $("#positionsTable tbody");
    tb.innerHTML = pos.map(p => `<tr>
      <td><b>${esc(p.symbol)}</b></td><td>${esc(p.kind || "—")}</td>
      <td class="num">${fmt(p.qty, 6)}</td><td class="num">${fmt(p.entry, 0)}</td><td class="num">${fmt(p.last, 0)}</td>
      <td class="num ${tone(p.pnl_pct)}">${pct(p.pnl_pct)}</td><td class="num">${fmt(p.stop, 0)}</td>
      <td class="num">${fmt(p.take_profit, 0)}</td><td class="num">${fmt(p.value_tmn)}</td>
      <td class="num">${p.age_min == null ? "—" : fmt(p.age_min, 0) + "م"}</td></tr>`).join("");
    $("#positionsEmpty").classList.toggle("hidden", pos.length > 0);

    const lr = s.learner || {};
    const riskRows = [
      ["حالت ریسک", (s.risk || {}).ai_mode || "normal"],
      ["معاملات امروز", fmt(s.trades_today) + " از " + fmt((s.config || {}).max_trades_per_day || 12)],
      ["پارامترهای یادگیری", Object.keys(lr.overrides || {}).length ? JSON.stringify(lr.overrides) : "بدون تغییر"],
      ["کارمزد پرداختی", fmt(s.fees_paid) + " تومان"],
      ["افت از اوج", fmt(dd, 2) + "٪"],
      ["سود محقق‌شده", fmt(s.realized_pnl) + " تومان"],
    ];
    $("#riskList").innerHTML = riskRows.map(([k, v]) => `<div class="status-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");

    if (s.curve && s.curve.length > 1) {
      lastCurve = s.curve;
      $("#curveNote").textContent = s.source === "replay" ? "Replay — " + ((s.replay || {}).virtual_now || "") : "وضعیت زنده";
      drawChart($("#equityChart"), s.curve, { label: "equity" });
    } else if (!lastCurve) {
      $("#curveNote").textContent = "نمونهٔ بک‌تست (بدون اجرای زنده)";
      fetch("/api/backtest?name=real").then(r => r.json()).then(j => {
        if (j.ok && j.curve && j.curve.length > 2) drawChart($("#equityChart"), j.curve, { label: "backtest" });
      }).catch(() => {});
    } else {
      drawChart($("#equityChart"), lastCurve, { label: "equity" });
    }

    if (s.source === "replay" && s.replay) {
      const r = s.replay;
      $("#clockPill").textContent = "Replay " + fmt(r.progress_pct, 0) + "٪ · " + r.status;
    }
    if (s.error) toast(s.error, "bad");
  }

  async function refreshStatus() {
    try {
      const j = await api("/api/status");
      renderStatus(j.status);
      $("#startBtn").disabled = !!j.status.running;
      $("#stopBtn").disabled = !j.status.running;
      $("#flattenBtn").disabled = !(j.status.positions || []).length || j.status.mode === "replay" && !(j.status.replay || {}).status;
    } catch (e) {
      setPill($("#connPill"), "قطع ارتباط", "pill-bad");
    }
  }

  async function loadDecisions() {
    const action = $("#actionFilter") ? $("#actionFilter").value : "";
    try {
      const j = await api("/api/decisions?limit=120" + (action ? "&action=" + encodeURIComponent(action) : ""));
      const rows = (j.decisions || []).reverse();
      const html = rows.map(d => {
        const bits = [];
        ["symbol", "kind", "score", "qty", "fill_price", "notional", "reason", "why", "risk_mode", "note", "error"]
          .forEach(k => { if (d[k] !== undefined && d[k] !== null && d[k] !== "") bits.push(k + ": " + (typeof d[k] === "object" ? JSON.stringify(d[k]) : d[k])); });
        if (d.ai && typeof d.ai === "object") bits.push("AI: " + (d.ai.approve ? "تأیید" : "رد") + " (" + (d.ai.confidence ?? "—") + ") " + (d.ai.reason || ""));
        if (d.reasons) bits.push("دلایل: " + (Array.isArray(d.reasons) ? d.reasons.join(" · ") : d.reasons));
        if (d.issues) bits.push("issues: " + JSON.stringify(d.issues));
        return `<div class="feed-row"><span class="act ${esc(d.action || "")}">${esc(d.action || "?")}</span>
          <div class="body"><div class="kv">${bits.map(esc).join("")}</div></div>
          <span class="time">${esc((d.ts || "").slice(11, 19))}</span></div>`;
      }).join("");
      $("#decisionFeed").innerHTML = html || `<div class="empty">هنوز تصمیمی ثبت نشده است.</div>`;
      if ($("#decisionTable")) $("#decisionTable").innerHTML = html || `<div class="empty">لاگی وجود ندارد.</div>`;
    } catch (e) { /* silent */ }
  }

  async function loadTrades() {
    try {
      const j = await api("/api/trades?limit=300");
      const rows = (j.trades || []).slice().reverse();
      $("#tradesTable tbody").innerHTML = rows.map((t, i) => `<tr>
        <td class="num">${rows.length - i}</td><td><b>${esc(t.symbol)}</b></td><td>${esc(t.kind || "—")}</td>
        <td>${esc(fmtTs(t.open_ts))}</td><td>${esc(fmtTs(t.close_ts))}</td>
        <td class="num">${fmt(t.entry, 0)}</td><td class="num">${fmt(t.exit, 0)}</td>
        <td class="num ${tone(t.pnl_tmn)}">${fmt(t.pnl_tmn)}</td>
        <td class="num ${tone(t.pnl_pct)}">${pct(t.pnl_pct)}</td><td>${esc(t.exit_reason || t.reason || "—")}</td></tr>`).join("");
      $("#tradesEmpty").classList.toggle("hidden", rows.length > 0);
    } catch (e) { /* silent */ }
  }

  /* ---------------- controls ---------------- */
  $("#startBtn").addEventListener("click", async () => {
    const mode = $("#modeSelect").value;
    try {
      if (mode === "replay") {
        const speed = Number($("#replaySpeed").value) || 600;
        await api("/api/replay/start", { body: { speed, loop: true } });
      } else {
        await api("/api/bot/start", { body: { mode } });
      }
      toast("ربات شروع شد", "good");
      setTimeout(refreshStatus, 600);
    } catch (e) {
      const f = (e.payload && e.payload.failures) || [];
      toast(e.message + (f.length ? " — " + f.slice(0, 2).join(" | ") : ""), "bad");
    }
  });
  $("#stopBtn").addEventListener("click", async () => {
    try {
      const isReplay = ($("#modeSelect").value === "replay") || (lastStatus && lastStatus.source === "replay");
      if (isReplay) await api("/api/replay/stop", { body: {} });
      else await api("/api/bot/stop", { body: {} });
      toast("ربات متوقف شد", "good");
      setTimeout(refreshStatus, 400);
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#flattenBtn").addEventListener("click", async () => {
    if (!confirm("همهٔ پوزیشن‌های باز با سفارش فروش به تومان تبدیل شوند؟")) return;
    try { await api("/api/flatten", { body: {} }); toast("دستور تسویه ارسال شد", "good"); }
    catch (e) { toast(e.message, "bad"); }
  });
  $("#resetKill").addEventListener("click", async () => {
    if (!confirm("Kill-Switch ریست شود؟ اوج روی سرمایهٔ فعلی تنظیم می‌شود.")) return;
    try { const r = await api("/api/reset-kill", { body: {} }); toast(r.note || "ریست شد", "good"); loadGate(); }
    catch (e) { toast(e.message, "bad"); }
  });

  /* ---------------- settings ---------------- */
  const FIELD_MAP = [
    ["aiProvider", "ai_provider"], ["aiModel", "ai_model"], ["aiBaseUrl", "ai_base_url"],
    ["symbols", "symbols"], ["challengeEnd", "challenge_end"], ["initialCapital", "initial_capital"],
    ["maxPositions", "max_positions"], ["maxPosFrac", "max_pos_frac"], ["reserveFrac", "reserve_frac"],
    ["riskPerTrade", "risk_per_trade"], ["maxTradesPerDay", "max_trades_per_day"],
    ["maxDrawdown", "max_drawdown"], ["dailyLossLimit", "daily_loss_limit"],
    ["minScore", "min_score"], ["rulesMinScore", "rules_min_score"], ["feeRate", "fee_rate"],
    ["minAtrPct", "min_atr_pct"], ["rsiBuy", "rsi_buy"], ["bbMaxPos", "bb_max_pos"],
    ["mcpUrl", "mcp_url"], ["apiKeyHeader", "api_key_header"],
  ];
  function fillSettings(s) {
    FIELD_MAP.forEach(([id, key]) => {
      const el = $("#" + id); if (!el) return;
      let v = s[key];
      if (Array.isArray(v)) v = v.join(",");
      el.value = v ?? "";
    });
    $("#mcpEnabled").checked = !!s.mcp_enabled;
    $("#aiStatus").textContent = s.ai_key_present ? "کلید AI ثبت شده" : "بدون کلید — rules only";
    $("#aiStatus").className = "pill " + (s.ai_key_present ? "pill-good" : "pill-warn");
    $("#wallexKey").placeholder = s.wallex_key_present ? "کلید ذخیره شده — خالی بگذارید" : "کلید API والکس";
    $("#aiKey").placeholder = s.ai_key_present ? "کلید ذخیره شده — خالی بگذارید" : "کلید AI";
    $("#extraHeaders").value = s.extra_headers && Object.keys(s.extra_headers).length ? JSON.stringify(s.extra_headers) : "";
  }
  let PROFILES = {};
  async function loadProfiles() {
    try {
      const j = await api("/api/profiles");
      PROFILES = j.profiles || {};
      $("#profilePreset").innerHTML = '<option value="">— انتخاب —</option>' +
        Object.entries(PROFILES).map(([k, v]) => `<option value="${esc(k)}">${esc(v.title)}</option>`).join("");
    } catch (e) { /* noop */ }
  }
  $("#applyProfile").addEventListener("click", () => {
    const p = PROFILES[$("#profilePreset").value];
    if (!p) return toast("ابتدا یک پروفایل انتخاب کنید", "bad");
    const map = { min_atr_pct: "minAtrPct", rsi_buy: "rsiBuy", bb_max_pos: "bbMaxPos",
      min_score: "minScore", rules_min_score: "rulesMinScore", max_trades_per_day: "maxTradesPerDay" };
    Object.entries(p.params).forEach(([k, v]) => { const el = $("#" + (map[k] || "")); if (el) el.value = v; });
    $("#profileNote").textContent = p.note + " — برای اعمال، ذخیره کنید.";
  });
  async function loadSettings() {
    try { const j = await api("/api/settings"); fillSettings(j.settings); $("#saveResult").textContent = ""; }
    catch (e) { toast(e.message, "bad"); }
  }
  $("#reloadSettings").addEventListener("click", loadSettings);
  $("#saveSettings").addEventListener("click", async () => {
    const body = {};
    FIELD_MAP.forEach(([id, key]) => { const el = $("#" + id); if (el && el.value !== "") body[key] = el.value; });
    const wk = $("#wallexKey").value.trim(); if (wk) body.wallex_api_key = wk;
    const ak = $("#aiKey").value.trim(); if (ak) body.ai_api_key = ak;
    body.mcp_enabled = $("#mcpEnabled").checked;
    const eh = $("#extraHeaders").value.trim();
    if (eh) { try { body.extra_headers = JSON.parse(eh); } catch { return toast("هدرهای اضافه JSON معتبر نیست", "bad"); } }
    try {
      const j = await api("/api/settings", { body });
      toast("تنظیمات ذخیره شد — ربات را دوباره شروع کنید", "good");
      fillSettings(j.settings);
    } catch (e) {
      const d = (e.payload && e.payload.details) || [];
      toast(e.message + (d.length ? " — " + d.join(" ، ") : ""), "bad");
    }
  });
  $("#testConn").addEventListener("click", async () => {
    const box = $("#connResult"); box.classList.remove("hidden"); box.textContent = "در حال تست…";
    try {
      const j = await api("/api/test/connection", { body: {} });
      box.className = "result-box " + (j.ok ? "good" : "bad");
      const pretty = { ok: j.ok, steps: j.steps };
      box.innerHTML = `<pre style="margin:0;white-space:pre-wrap">${esc(JSON.stringify(pretty, null, 2))}</pre>`;
      if (j.steps && j.steps.profile && j.steps.profile.identifiers) {
        $("#accountInfo").innerHTML = `<pre style="margin:0;white-space:pre-wrap">${esc(JSON.stringify(j.steps.profile, null, 2))}</pre>`;
      }
      toast(j.ok ? "اتصال برقرار است" : "اتصال ناقص — جزئیات را ببینید", j.ok ? "good" : "bad");
    } catch (e) { box.className = "result-box bad"; box.textContent = e.message; }
  });
  $("#testAI").addEventListener("click", async () => {
    const box = $("#aiResult"); box.classList.remove("hidden"); box.textContent = "در حال تست…";
    try {
      const j = await api("/api/test/ai", { body: {} });
      box.className = "result-box " + (j.ok ? "good" : "bad");
      box.innerHTML = `<pre style="margin:0;white-space:pre-wrap">${esc(JSON.stringify(j, null, 2))}</pre>`;
    } catch (e) { box.className = "result-box bad"; box.textContent = e.message; }
  });
  async function loadGate() {
    try {
      const j = await api("/api/gate");
      $("#gateList").innerHTML = j.failures.length
        ? j.failures.map(f => `<div class="gate-item bad">${esc(f)}</div>`).join("")
        : `<div class="gate-item ok">همهٔ شرایط برقرار است — live مجاز است.</div>`;
    } catch (e) { $("#gateList").textContent = e.message; }
  }
  $("#reloadGate").addEventListener("click", loadGate);

  /* ---------------- backtest ---------------- */
  function renderBacktest(j) {
    const r = j.report || {};
    const kpis = [
      ["بازدهی", pct(r.return_pct), tone(r.return_pct)],
      ["سود خالص", fmt((r.final_tmn || 0) - 5000000), tone((r.final_tmn || 0) - 5000000)],
      ["معاملات", fmt(r.trades), ""],
      ["نرخ برد", r.trades ? fmt(r.win_rate_pct, 1) + "٪" : "—", ""],
      ["Profit Factor", r.profit_factor ?? "—", ""],
      ["حداکثر افت", fmt(r.max_drawdown_pct, 2) + "٪", r.max_drawdown_pct > 5 ? "bad" : ""],
    ];
    $("#btKpis").innerHTML = kpis.map(([k, v, c]) => `<div class="kpi"><div class="kpi-label">${k}</div><div class="kpi-value ${c}">${v}</div></div>`).join("");
    drawChart($("#btChart"), j.curve || [], { color: "#37bdf8", label: r.data_source || "" });
    const funnel = r.funnel || {};
    const lines = Object.entries(funnel).sort((a, b) => b[1] - a[1]).map(([k, v]) => String(v).padStart(7) + "  " + k);
    $("#btFunnel").textContent = (j.funnel || lines.join("\n") || "—");
    $("#btTrades tbody").innerHTML = (j.trades || []).map(t => `<tr>
      <td><b>${esc(t.symbol)}</b></td><td>${esc(t.kind || "—")}</td>
      <td>${esc(new Date(Number(t.entry_t) * 1000).toLocaleString("fa-IR", { hour12: false }))}</td>
      <td>${esc(new Date(Number(t.exit_t) * 1000).toLocaleString("fa-IR", { hour12: false }))}</td>
      <td class="num">${fmt(t.entry, 0)}</td><td class="num">${fmt(t.exit, 0)}</td>
      <td class="num ${tone(t.pnl_tmn)}">${fmt(t.pnl_tmn)}</td><td class="num ${tone(t.pnl_pct)}">${pct(t.pnl_pct)}</td>
      <td>${esc(t.reason || "—")}</td></tr>`).join("");
    const gate = r.ai_gate ? " · AI: " + (r.ai_gate.mode || "") + (r.ai_gate.calls !== undefined ? " (" + r.ai_gate.calls + " calls)" : "") : "";
    $("#btJob").textContent = "داده: " + (r.data_source || "—") + " · آستانهٔ استفاده‌شده: " + ((r.thresholds || {}).used ?? "—") + gate;
  }
  async function loadBacktest(name) {
    try { renderBacktest(await api("/api/backtest?name=" + encodeURIComponent(name || "real"))); }
    catch (e) {
      $("#btKpis").innerHTML = "";
      $("#btFunnel").textContent = e.message;
      $("#btTrades tbody").innerHTML = "";
      drawChart($("#btChart"), []);
    }
  }
  $("#btPick").addEventListener("change", e => loadBacktest(e.target.value));
  $("#runBacktest").addEventListener("click", async () => {
    const synthetic = $("input[name=btData]:checked").value === "synthetic";
    try {
      const j = await api("/api/backtest/run", { body: { synthetic, learn: $("#btLearn").checked, ai_gate: $("#btAI").checked } });
      $("#btJob").textContent = "در حال اجرا… (" + j.job.id + ")";
      pollJob(j.job.id, job => {
        if (job.state === "done") {
          toast("بک‌تست تمام شد", "good");
          loadBacktest("web");
        } else if (job.state === "error") toast("بک‌تست خطا داد: " + job.error, "bad");
        return job.state !== "running";
      });
    } catch (e) { toast(e.message, "bad"); }
  });
  async function pollJob(id, cb) {
    for (let i = 0; i < 300; i++) {
      await new Promise(r => setTimeout(r, 1200));
      try {
        const j = await api("/api/jobs/" + id);
        if (!j.job) return;
        if (cb(j.job)) return;
        if ($("#btJob")) $("#btJob").textContent = "در حال اجرا… " + Math.round((Date.now() - j.job.started * 1000) / 1000) + "s";
      } catch (e) { return; }
    }
  }

  /* ---------------- tools ---------------- */
  async function loadCommands() {
    try {
      const j = await api("/api/help");
      $("#commands").textContent = Object.entries(j.commands).map(([k, v]) => k + ":\n  " + (Array.isArray(v) ? v.join("\n  ") : v)).join("\n\n");
      $("#mcpUrlLabel").textContent = (document.querySelector("#mcpUrl") || {}).value || "https://api.wallex.ir/mcp";
    } catch (e) { /* noop */ }
  }
  $("#loadMcp").addEventListener("click", async () => {
    const box = $("#mcpTools"); box.classList.remove("hidden"); box.textContent = "در حال دریافت…";
    try {
      const j = await api("/api/mcp/tools");
      box.className = "result-box tall";
      $("#mcpUrlLabel").textContent = $("#mcpUrl").value || "https://api.wallex.ir/mcp";
      const tools = j.tools || [];
      box.innerHTML = `<div class="muted small">server: ${esc(JSON.stringify(j.server || {}))} · ${tools.length} ابزار</div>` +
        tools.map(t => `<div class="gate-item" style="margin-top:6px;cursor:pointer" data-tool="${esc(t.name)}"><b>${esc(t.name)}</b><div class="muted small">${esc(t.description || "")}</div></div>`).join("");
      $$("#mcpTools [data-tool]").forEach(el => el.addEventListener("click", () => { $("#mcpToolName").value = el.dataset.tool; }));
    } catch (e) { box.className = "result-box bad"; box.textContent = e.message; }
  });
  $("#mcpCall").addEventListener("click", async () => {
    const box = $("#mcpResult"); box.classList.remove("hidden"); box.textContent = "در حال فراخوانی…";
    let args = {};
    try { args = JSON.parse($("#mcpArgs").value || "{}"); } catch { return toast("آرگومان‌ها JSON معتبر نیست", "bad"); }
    try {
      const j = await api("/api/mcp/call", { body: { name: $("#mcpToolName").value, arguments: args } });
      box.className = "result-box good";
      box.innerHTML = `<pre style="margin:0;white-space:pre-wrap">${esc(JSON.stringify(j.result, null, 2))}</pre>`;
    } catch (e) { box.className = "result-box bad"; box.textContent = e.message; }
  });
  function showTestJob(j) {
    $("#testsResult").textContent = "در حال اجرا…";
    pollJob(j.job.id, job => {
      if (job.state === "done") {
        const r = job.result || {};
        $("#testsResult").textContent = (r.stdout || "") + (r.stderr ? "\n" + r.stderr : "") + "\nexit=" + r.returncode;
        toast(r.returncode === 0 ? "تست‌ها موفق بودند" : "تست‌ها خطا داشتند", r.returncode === 0 ? "good" : "bad");
        return true;
      }
      if (job.state === "error") { $("#testsResult").textContent = job.error; return true; }
      return false;
    });
  }
  $("#runTests").addEventListener("click", async () => {
    try { showTestJob(await api("/api/tests/run", { body: {} })); } catch (e) { toast(e.message, "bad"); }
  });
  $("#runSafety").addEventListener("click", async () => {
    try { showTestJob(await api("/api/tests/run", { body: { safety: true } })); } catch (e) { toast(e.message, "bad"); }
  });

  /* ---------------- boot ---------------- */
  window.addEventListener("resize", () => {
    if (lastCurve) drawChart($("#equityChart"), lastCurve, { label: "equity" });
  });
  refreshStatus();
  loadProfiles();
  loadDecisions();
  loadTrades();
  loadSettings();
  setInterval(refreshStatus, 3000);
  setInterval(loadDecisions, 6000);
})();
