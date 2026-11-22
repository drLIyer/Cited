"use strict";

// Show this server's own MCP URL (works on any deployment / custom domain).
(function () {
  const url = document.getElementById("mcp-url");
  if (url && location.protocol === "https:") url.textContent = `${location.origin}/mcp`;
})();

// Copy buttons
document.querySelectorAll("button[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const text = document.getElementById(btn.dataset.copy).textContent.trim();
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
    catch { btn.textContent = "Select & copy"; }
    setTimeout(() => { btn.textContent = "Copy"; }, 1600);
  });
});

// Demo: reveal the conversation step by step, then loop.
(function () {
  const msgs = Array.from(document.querySelectorAll("#demo .msg"));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || new URLSearchParams(location.search).has("static")) {
    msgs.forEach((m) => m.classList.add("show"));
    return;
  }
  const delays = [400, 1300, 1100, 3200, 1600];
  function run() {
    msgs.forEach((m) => m.classList.remove("show"));
    let t = 0;
    msgs.forEach((m, i) => { t += delays[i] || 1200; setTimeout(() => m.classList.add("show"), t); });
    setTimeout(run, t + 6000);
  }
  run();
})();
