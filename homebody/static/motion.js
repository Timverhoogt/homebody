/* Homebody motion primitives: one-shot entrances and button feedback (see docs/scenes.md).

   Decoration only. Nothing here fetches, stores, observes the DOM or decides an outcome: callers say
   when a real request starts and how it really ended. Feedback is keyed to the element and a token,
   so a late or superseded result cannot repaint a newer one. Reduced motion and hidden pages skip
   entrances; the pending/confirmed attribute stays as a static, readable state. */
(() => {
  const CONFIRM_MS = 1400;
  const ENTER_MS = 320;
  const reduceQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const tracked = new Map(); // element -> { token, timer }
  const entering = new Map(); // element -> timer
  let tokens = 0;

  function still() { return Boolean(reduceQuery?.matches) || document.hidden; }

  function clear(element) {
    const entry = tracked.get(element);
    if (!entry) return false;
    window.clearTimeout(entry.timer);
    tracked.delete(element);
    element.removeAttribute("data-hb-motion");
    return true;
  }

  /** Mark a real request as in flight. Returns a token for settle(); null without an element. */
  function pending(element) {
    if (!element) return null;
    clear(element);
    const token = ++tokens;
    tracked.set(element, { token, timer: 0 });
    element.setAttribute("data-hb-motion", "pending");
    return { element, token };
  }

  /**
   * End feedback for `handle`. outcome: "confirmed" (the server accepted it), "failed", or "cleared".
   * Returns false if the handle was superseded, cleared or reset meanwhile.
   */
  function settle(handle, outcome = "cleared") {
    const entry = handle && tracked.get(handle.element);
    if (!entry || entry.token !== handle.token) return false;
    const { element } = handle;
    window.clearTimeout(entry.timer);
    if (outcome !== "confirmed" && outcome !== "failed") return clear(element);
    element.setAttribute("data-hb-motion", outcome);
    entry.timer = window.setTimeout(() => {
      if (tracked.get(element)?.token === handle.token) clear(element);
    }, CONFIRM_MS);
    return true;
  }

  /** Play one entrance on an element that has just become visible. */
  function enter(element) {
    if (!element || still() || element.hidden || !element.isConnected) return false;
    window.clearTimeout(entering.get(element));
    element.classList.remove("hb-enter");
    void element.offsetWidth;
    element.classList.add("hb-enter");
    entering.set(element, window.setTimeout(() => {
      element.classList.remove("hb-enter");
      entering.delete(element);
    }, ENTER_MS));
    return true;
  }

  /** Opt-in disclosure: the opened content eases in. Bound once per element; returns an unbind. */
  const bound = new WeakSet();
  function disclosure(details) {
    if (!details || bound.has(details)) return () => {};
    const onToggle = () => {
      if (!details.open) return;
      for (const child of details.children) if (child.tagName !== "SUMMARY") enter(child);
    };
    details.addEventListener("toggle", onToggle);
    bound.add(details);
    return () => { details.removeEventListener("toggle", onToggle); bound.delete(details); };
  }

  /** Owner loss or privacy: drop every pending/confirmed mark and timer. */
  function reset() {
    [...tracked.keys()].forEach(clear);
    entering.forEach((timer, element) => { window.clearTimeout(timer); element.classList.remove("hb-enter"); });
    entering.clear();
  }

  document.querySelectorAll("details[data-hb-disclosure]").forEach(disclosure);

  window.HomebodyMotion = Object.freeze({
    pending,
    settle,
    clear,
    enter,
    disclosure,
    reset,
    activeCount() { return tracked.size + entering.size; },
  });
})();
