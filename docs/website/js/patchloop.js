// SOCBench vuln-harness page interactive layer.
// Tab switching + two step-through animations (harness verification loop, and a
// side-by-side vs tool-calling agent). Extracted from the original patchloop
// demo with two changes:
//   1. `.tab` and `.tabs` renamed to `.harness-tab` and `.harness-tabs` so the
//      controller cannot capture any landing-style segmented control dropped
//      into a panel later.
//   2. Em-dashes and en-dashes in string literals substituted with periods,
//      colons, or hyphens (site-wide copy rule).
// All original behavior preserved.

(() => {
  const $ = (id) => document.getElementById(id);

  // ---- tabs ----
  document.querySelectorAll(".harness-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".harness-tab").forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      document.querySelectorAll(".panel").forEach((p) => {
        p.classList.toggle("active", p.id === "panel-" + tab.dataset.tab);
      });
    });
  });

  // ============================================================
  // TAB 1. harness animation
  // ============================================================
  const nodes = ["n-finding", "n-router", "n-propose", "n-apply", "n-verify"].map($);
  const verifiers = { rescan: $("v-rescan"), regress: $("v-regress"), repro: $("v-repro") };
  const logEl = $("log");

  function log(text, cls) {
    const d = document.createElement("div");
    d.textContent = text;
    if (cls) d.className = cls;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
  }
  function clearActive() {
    nodes.forEach((n) => n.classList.remove("active", "failed", "passed"));
  }
  function focusNode(id) {
    clearActive();
    if (id) $(id).classList.add("active");
  }
  function setVerifier(name, state) {
    const v = verifiers[name];
    v.classList.remove("running", "pass", "fail", "skipped");
    if (state) v.classList.add(state);
  }
  function resetVerifiers() {
    for (const name of Object.keys(verifiers)) setVerifier(name, null);
  }
  function litRule(n, on) {
    $("r" + n).classList.toggle("lit", on);
  }

  const timeline = [
    { fn: () => { focusNode("n-finding"); log("finding CVE-2099-0001 decoded (Schema.Class). Untrusted JSON validated at the edge", "sys"); } },
    { fn: () => { focusNode("n-router"); $("k-cve").classList.add("lit"); log('router: kind "cve" -> CVE patching harness'); } },

    { fn: () => { $("attemptNum").textContent = "1"; focusNode("n-propose"); resetVerifiers(); log("attempt 1: propose. direct-edit request (feedback = null)"); } },
    { fn: () => { focusNode("n-apply"); log("apply: 2 edits onto workspace ✓"); } },
    { fn: () => { focusNode("n-verify"); setVerifier("rescan", "running"); log("verify: trivy rescan...", "wrn"); } },
    { fn: () => { setVerifier("rescan", "fail"); setVerifier("regress", "skipped"); setVerifier("repro", "skipped");
                  $("n-verify").classList.add("failed");
                  log("rescan FAIL: CVE-2099-0001 still reported", "err");
                  log("fail-fast: never paid for tests or PoC", "sys"); } },
    { fn: () => { $("feedback").classList.add("active"); litRule(2, true);
                  log("feedback <- verbatim scanner output + cumulative diff", "wrn");
                  log("rule 2: repair ON TOP of patched state (no reset)", "sys"); } },

    { fn: () => { $("feedback").classList.remove("active"); $("attemptNum").textContent = "2";
                  focusNode("n-propose"); resetVerifiers(); litRule(2, false);
                  log("attempt 2: propose. repair prompt carries current (patched) files"); } },
    { fn: () => { focusNode("n-apply"); log("apply: 1 edit onto patched state ✓"); } },
    { fn: () => { focusNode("n-verify"); setVerifier("rescan", "running"); log("verify: trivy rescan...", "wrn"); } },
    { fn: () => { setVerifier("rescan", "pass"); setVerifier("regress", "running"); log("rescan PASS. finding gone", "ok"); log("verify: make test...", "wrn"); } },
    { fn: () => { setVerifier("regress", "fail"); setVerifier("repro", "skipped"); $("n-verify").classList.add("failed");
                  log("regression FAIL: 2 tests broken. patch deleted the feature", "err");
                  log("a rescan-passing patch is not a done patch", "sys"); } },
    { fn: () => { $("feedback").classList.add("active"); litRule(4, true);
                  log("feedback <- verbatim test output + cumulative diff", "wrn"); } },

    { fn: () => { $("feedback").classList.remove("active"); $("attemptNum").textContent = "3";
                  focusNode("n-propose"); resetVerifiers(); litRule(4, false);
                  log("attempt 3: propose. restore behavior, keep the sanitization"); } },
    { fn: () => { focusNode("n-apply"); log("apply: 1 edit ✓"); } },
    { fn: () => { focusNode("n-verify"); setVerifier("rescan", "running"); log("verify: trivy rescan...", "wrn"); } },
    { fn: () => { setVerifier("rescan", "pass"); setVerifier("regress", "running"); log("rescan PASS", "ok"); log("verify: make test...", "wrn"); } },
    { fn: () => { setVerifier("regress", "pass"); setVerifier("repro", "running"); log("regression PASS. 214/214 tests", "ok"); log("verify: run exploit PoC...", "wrn"); } },
    { fn: () => { setVerifier("repro", "pass"); $("n-verify").classList.add("passed"); litRule(5, true);
                  log("reproduction PASS. exploit no longer works", "ok"); } },
    { fn: () => { clearActive();
                  const o = $("outcome");
                  o.classList.add("success");
                  o.textContent = "✓ verified diff -> open pull request (your side of the fence)";
                  log("result: success=true, attempts=3, finalDiff -> PR", "ok"); } },
  ];

  let i = 0, timer = null, playing = false;
  const playBtn = $("playBtn");

  function step() {
    if (i >= timeline.length) { stop(); return; }
    timeline[i++].fn();
    if (playing && i < timeline.length) {
      timer = setTimeout(step, parseInt($("speedSel").value, 10));
    } else if (i >= timeline.length) {
      stop();
    }
  }
  function stop() {
    playing = false;
    clearTimeout(timer);
    playBtn.innerHTML = "&#9654; Play";
  }
  function reset() {
    stop();
    i = 0;
    clearActive();
    resetVerifiers();
    [1, 2, 3, 4, 5].forEach((n) => litRule(n, false));
    $("k-cve").classList.remove("lit");
    $("feedback").classList.remove("active");
    $("attemptNum").textContent = "-";
    const o = $("outcome");
    o.classList.remove("success");
    o.textContent = "awaiting verified diff...";
    logEl.innerHTML = '<div class="sys">// press Play to run a remediation</div>';
  }
  playBtn.addEventListener("click", () => {
    if (playing) { stop(); return; }
    if (i >= timeline.length) reset();
    playing = true;
    playBtn.innerHTML = "&#10074;&#10074; Pause";
    step();
  });
  $("stepBtn").addEventListener("click", () => { stop(); if (i >= timeline.length) reset(); else step(); });
  $("resetBtn").addEventListener("click", reset);

  // ============================================================
  // TAB 2. vs tool-calling agent
  // ============================================================
  const SYS = 20000;
  const AGENT_STEPS = [
    { tool: "Think", add: 1200 },
    { tool: "Glob",  add: 400 },
    { tool: "Think", add: 900 },
    { tool: "Read",  add: 2100 },
    { tool: "Think", add: 1400 },
    { tool: "Grep",  add: 600 },
    { tool: "Think", add: 1100 },
    { tool: "Edit",  add: 800 },
    { tool: "Think", add: 1000 },
    { tool: "Bash",  add: 1500 },
    { tool: "Think", add: 1300 },
    { tool: "Edit",  add: 700 },
    { tool: "text",  add: 400 },
  ];
  const PATCH_STEPS = [
    { add: 3000 },
    { add: 0 },
    { add: 0 },
    { add: 3500 },
    { add: 0 },
  ];
  const USD_PER_TOK = 5 / 1_000_000;
  const MAX_TOK = 48000;

  let vsTimer = null;
  let vsPlaying = false;

  function fmtTok(n) {
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k";
    return String(n);
  }
  function fmtUsd(n) {
    return "$" + n.toFixed(2);
  }
  function setMeter(tokId, barId, costId, n) {
    $(tokId).textContent = fmtTok(n);
    $(barId).style.width = Math.min(100, (n / MAX_TOK) * 100) + "%";
    $(costId).textContent = fmtUsd(n * USD_PER_TOK);
  }

  function vsReset() {
    vsPlaying = false;
    clearTimeout(vsTimer);
    $("vsPlay").innerHTML = "&#9654; Run both on the same CVE";
    document.querySelectorAll("#agentTurns .turn, #patchTurns .turn").forEach((t) => {
      t.classList.remove("active", "done", "final");
      t.classList.add("ghost");
    });
    document.querySelectorAll("#agentTools .tool-chip").forEach((c) => c.classList.remove("lit"));
    setMeter("agentTok", "agentBar", "agentCost", 0);
    setMeter("patchTok", "patchBar", "patchCost", 0);
    $("punchAgent").textContent = "~45k tok";
    $("punchPatch").textContent = "~7k tok";
    $("punchRatio").textContent = "~10x";
  }

  function litTool(name) {
    document.querySelectorAll("#agentTools .tool-chip").forEach((c) => {
      c.classList.toggle("lit", c.dataset.t === name || (name === "text" && false));
    });
  }

  function vsRun() {
    if (vsPlaying) {
      vsPlaying = false;
      clearTimeout(vsTimer);
      $("vsPlay").innerHTML = "&#9654; Run both on the same CVE";
      return;
    }
    vsReset();
    vsPlaying = true;
    $("vsPlay").innerHTML = "&#10074;&#10074; Pause";

    let agentTok = 0;
    let patchTok = 0;
    let a = 0;
    let p = 0;
    agentTok = SYS;
    setMeter("agentTok", "agentBar", "agentCost", agentTok);

    const agentTurns = [...document.querySelectorAll("#agentTurns .turn")];
    const patchTurns = [...document.querySelectorAll("#patchTurns .turn")];

    function tickAgent() {
      if (!vsPlaying) return;
      if (a >= AGENT_STEPS.length) {
        maybeDone();
        return;
      }
      const stp = AGENT_STEPS[a];
      const sysTax = a === 0 ? 0 : Math.round(SYS * 0.15);
      agentTok += sysTax + stp.add;
      agentTurns.forEach((t, idx) => {
        t.classList.remove("active", "ghost");
        if (idx < a) t.classList.add("done");
        else if (idx === a) t.classList.add("active");
        else t.classList.add("ghost");
      });
      litTool(stp.tool);
      setMeter("agentTok", "agentBar", "agentCost", agentTok);
      a++;
      if (a >= AGENT_STEPS.length) {
        agentTurns.forEach((t) => { t.classList.remove("active"); t.classList.add("done"); });
        agentTurns[agentTurns.length - 1].classList.add("final");
        litTool("");
        $("punchAgent").textContent = "~" + fmtTok(agentTok) + " tok";
      }
      vsTimer = setTimeout(tickAgent, 420);
      maybeStartPatch();
    }

    let patchStarted = false;
    function maybeStartPatch() {
      if (patchStarted || a < 2) return;
      patchStarted = true;
      tickPatch();
    }

    function tickPatch() {
      if (!vsPlaying) return;
      if (p >= PATCH_STEPS.length) {
        maybeDone();
        return;
      }
      const stp = PATCH_STEPS[p];
      patchTok += stp.add;
      patchTurns.forEach((t, idx) => {
        t.classList.remove("active", "ghost");
        if (idx < p) t.classList.add("done");
        else if (idx === p) t.classList.add("active");
        else t.classList.add("ghost");
      });
      setMeter("patchTok", "patchBar", "patchCost", patchTok);
      p++;
      if (p >= PATCH_STEPS.length) {
        patchTurns.forEach((t) => { t.classList.remove("active"); t.classList.add("done"); });
        patchTurns[patchTurns.length - 1].classList.add("final");
        $("punchPatch").textContent = "~" + fmtTok(patchTok) + " tok";
        const ratio = Math.round(agentTok / Math.max(patchTok, 1));
        $("punchRatio").textContent = "~" + ratio + "x";
      }
      vsTimer = setTimeout(tickPatch, 700);
    }

    function maybeDone() {
      if (a >= AGENT_STEPS.length && p >= PATCH_STEPS.length) {
        vsPlaying = false;
        $("vsPlay").innerHTML = "&#9654; Run both on the same CVE";
        const ratio = Math.round(agentTok / Math.max(patchTok, 1));
        $("punchAgent").textContent = "~" + fmtTok(agentTok) + " tok";
        $("punchPatch").textContent = "~" + fmtTok(patchTok) + " tok";
        $("punchRatio").textContent = "~" + ratio + "x";
      }
    }

    tickAgent();
  }

  $("vsPlay").addEventListener("click", vsRun);
  $("vsReset").addEventListener("click", vsReset);

  // ============================================================
  // GitHub star count for the nav pill (shared with landing).
  // Public REST API, 5-minute localStorage cache to avoid re-hitting.
  // ============================================================
  (function loadStarCount() {
    const el = document.getElementById("repo-star-count");
    if (!el) return;
    const REPO = "DeepTempo/socbench";
    const CACHE_KEY = `gh-stars-${REPO}`;
    const TTL_MS = 5 * 60 * 1000;
    function fmt(n) {
      if (n == null) return "";
      if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
      return String(n);
    }
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        const { count, at } = JSON.parse(raw);
        if (Date.now() - at <= TTL_MS) {
          el.textContent = fmt(count);
          return;
        }
      }
    } catch {}
    fetch(`https://api.github.com/repos/${REPO}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => {
        const c = d.stargazers_count;
        el.textContent = fmt(c);
        try { localStorage.setItem(CACHE_KEY, JSON.stringify({ count: c, at: Date.now() })); } catch {}
      })
      .catch(() => {});
  })();
})();
