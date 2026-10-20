/* Settings navigation: one visible category at a time, a side list on wide screens and a picker on
   phones. This controller only toggles [hidden], opens <details> and moves focus. It never saves,
   grants, connects, starts media or moves the robot, and it never writes to a form field. */
(() => {
  const panel = document.getElementById('panel-settings');
  if (!panel) return;
  const categories = [...panel.querySelectorAll('[data-settings-category]')];
  const buttons = [...panel.querySelectorAll('[data-settings-nav]')];
  const picker = document.getElementById('settings-category-select');
  const saveBar = document.getElementById('settings-save');
  const formMessage = document.getElementById('form-message');
  const STORAGE_KEY = 'homebody-settings-category';
  const DEFAULT_CATEGORY = 'everyday';
  let current = '';

  const byKey = key => categories.find(category => category.dataset.settingsCategory === key);

  /** Show one category. ``userInitiated`` scrolls to the top like a page change; restores do not. */
  function select(key, { userInitiated = false } = {}) {
    const category = byKey(key) || byKey(DEFAULT_CATEGORY) || categories[0];
    if (!category) return null;
    key = category.dataset.settingsCategory;
    categories.forEach(candidate => { candidate.hidden = candidate !== category; });
    buttons.forEach(button => {
      const active = button.dataset.settingsNav === key;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    if (picker && picker.value !== key) picker.value = key;
    // Save covers the settings-form payload; it is only offered where that form has fields.
    if (saveBar) saveBar.hidden = !category.querySelector('#settings-form, [form="settings-form"]');
    current = key;
    try { window.localStorage.setItem(STORAGE_KEY, key); } catch (_) { /* private mode */ }
    if (userInitiated) window.scrollTo({ top: 0, behavior: 'instant' });
    return category;
  }

  /** Select the category containing ``target`` (element or id), open every ancestor disclosure and
      bring the control into view. Returns the element, or null when it is not inside Settings. */
  function reveal(target, { focus = true } = {}) {
    const element = typeof target === 'string' ? document.getElementById(target) : target;
    if (!element || !panel.contains(element)) return null;
    const category = element.closest('[data-settings-category]');
    if (category) select(category.dataset.settingsCategory);
    for (let parent = element.parentElement; parent && parent !== panel; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    element.scrollIntoView({ block: 'center', behavior: still ? 'auto' : 'smooth' });
    if (focus) {
      const focusable = element.matches('input, select, textarea, button, a[href], [tabindex]')
        ? element
        : element.querySelector('input:not([type="hidden"]), select, textarea, button, [tabindex]') || element;
      focusable.focus({ preventScroll: true });
    }
    return element;
  }

  buttons.forEach((button, index) => {
    button.addEventListener('click', () => select(button.dataset.settingsNav, { userInitiated: true }));
    button.addEventListener('keydown', event => {
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let next = index;
      if (event.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length;
      if (event.key === 'ArrowDown') next = (index + 1) % buttons.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = buttons.length - 1;
      buttons[next].focus();
      select(buttons[next].dataset.settingsNav, { userInitiated: true });
    });
  });
  picker?.addEventListener('change', () => select(picker.value, { userInitiated: true }));

  // Browser validation cannot focus a control in a hidden category; reveal it first so the native
  // message lands on the field. Fields associated through form="settings-form" live outside the
  // <form>, so listen on the panel (invalid does not bubble).
  let invalidBatch = false;
  panel.addEventListener('invalid', event => {
    const field = event.target;
    if (!(field instanceof Element) || field.form?.id !== 'settings-form') return;
    // One submit fires invalid for every bad field; only the first one chooses the category, so the
    // field the browser then focuses is the one that was revealed.
    if (invalidBatch) return;
    invalidBatch = true;
    queueMicrotask(() => { invalidBatch = false; });
    if (!field.closest('[data-settings-category]')?.hidden && !field.closest('details:not([open])')) return;
    reveal(field, { focus: false });
    if (formMessage) {
      formMessage.textContent = 'Not saved: check the highlighted field.';
      formMessage.className = 'message error';
    }
  }, true);

  let restored = DEFAULT_CATEGORY;
  try { restored = window.localStorage.getItem(STORAGE_KEY) || DEFAULT_CATEGORY; } catch (_) { /* private mode */ }
  select(restored);

  window.HomebodySettings = { reveal, select, current: () => current };
})();
