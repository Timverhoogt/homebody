/* Typed I Spy input: RAM only, session-bound, no microphone/STT or browser storage. */
(() => {
  const form = document.getElementById("kids-ispy-guess-form");
  const input = document.getElementById("kids-ispy-guess");
  const send = document.getElementById("kids-ispy-guess-send");
  const message = document.getElementById("kids-ispy-guess-message");
  const label = document.getElementById("kids-ispy-guess-label");
  let active = false, pending = false, refreshing = false, epoch = 0, token = "";
  function controls() {
    input.disabled = !active || !token || pending;
    send.disabled = input.disabled || !input.value.trim();
  }
  async function refresh() {
    if (!active || pending || refreshing) return;
    refreshing = true;
    const started = epoch;
    try {
      const response = await fetch("/api/kids/ispy/input", { cache: "no-store" });
      if (!response.ok) throw new Error("unavailable");
      const body = await response.json();
      if (started !== epoch || !active || pending) return;
      token = (body.available === true || body.stop_only === true) && typeof body.token === "string" ? body.token : "";
      if (token && body.stop_only === true) message.textContent = "Reachy is getting the next game ready. Only “stop” works right now.";
      else if (!token && !input.value) message.textContent = "Wait until Reachy finishes looking or speaking.";
      else if (token && (message.textContent.startsWith("Wait until") || message.textContent.startsWith("Reachy is getting"))) message.textContent = "";
    } catch (_) {
      if (started === epoch) { token = ""; message.textContent = "I Spy input is unavailable. Waiting to reconnect…"; }
    } finally { refreshing = false; controls(); }
  }
  window.HomebodyISpyInput = {
    update(kids) {
      // Same ephemeral capability for I Spy guesses and for Play together (game names, stop, answers).
      const hosted = kids.host?.enabled === true;
      const next = Boolean(kids.active) && (kids.profile?.activity === "ispy" || hosted);
      if (next !== active) { epoch++; token = ""; input.value = ""; message.textContent = ""; }
      active = next;
      label.textContent = hosted ? "Type to Reachy" : "Your I Spy guess";
      input.placeholder = hosted ? "Type a game, an answer, or stop" : "Type an object, hint, or yes/no";
      send.textContent = hosted ? "Send" : "Guess";
      form.hidden = !active;
      controls();
      if (active) refresh();
    },
  };
  input.addEventListener("input", controls);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const text = input.value.trim();
    if (!active || pending || !token || !text || text.length > 80) return;
    const started = epoch, admittedToken = token;
    pending = true; token = ""; controls();
    message.textContent = "Checking your guess…";
    try {
      const response = await fetch("/api/kids/ispy/guess", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, token: admittedToken }),
      });
      const body = await response.json();
      if (started !== epoch || !active) return;
      if (!response.ok || body.accepted !== true) throw new Error(body.detail || "This guess could not be completed.");
      input.value = "";
      message.textContent = label.textContent === "Type to Reachy" ? "Sent. Reachy is replying aloud." : "Guess checked. Reachy has replied.";
    } catch (error) {
      if (started === epoch && active) message.textContent = String(error.message || "This guess could not be completed.");
    } finally { pending = false; controls(); if (active) refresh(); }
  });
})();
