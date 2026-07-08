(() => {
  // Remove old labels from a previous step
  document.querySelectorAll('[data-agent-id]').forEach(el => el.removeAttribute('data-agent-id'));

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity) === 0) return false;
    // must be within the scrollable page (not necessarily current viewport - we allow scrolling)
    return true;
  };

  const INTERACTIVE_SELECTOR = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]',
    '[role=tab]', '[role=menuitem]', '[role=option]', '[role=combobox]',
    '[onclick]', '[tabindex]:not([tabindex="-1"])', 'summary', 'label'
  ].join(',');

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const isDisabled = (el) => {
    if (el.disabled === true) return true;
    if (el.hasAttribute('disabled')) return true;
    if (el.getAttribute('aria-disabled') === 'true') return true;
    return false;
  };

  const els = Array.from(document.querySelectorAll(INTERACTIVE_SELECTOR));
  const seen = new Set();
  const results = [];
  let idCounter = 1;

  for (const el of els) {
    if (!isVisible(el)) continue;
    if (seen.has(el)) continue;
    seen.add(el);

    const tag = el.tagName.toLowerCase();
    const rect = el.getBoundingClientRect();
    const id = idCounter++;
    el.setAttribute('data-agent-id', String(id));

    let text = clean(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('alt') || '');
    if (text.length > 120) text = text.slice(0, 120) + '…';

    results.push({
      id,
      tag,
      type: el.getAttribute('type') || null,
      role: el.getAttribute('role') || null,
      text,
      placeholder: el.getAttribute('placeholder') || null,
      name: el.getAttribute('name') || null,
      href: tag === 'a' ? el.getAttribute('href') : null,
      value: (tag === 'input' || tag === 'textarea') ? clean(el.value || '') : null,
      checked: el.checked === true,
      disabled: isDisabled(el),
      x: Math.round(rect.x + rect.width / 2),
      y: Math.round(rect.y + rect.height / 2),
      inViewport: rect.top >= 0 && rect.top < window.innerHeight
    });
  }

  return {
    url: window.location.href,
    title: document.title,
    scrollY: window.scrollY,
    scrollMaxY: document.documentElement.scrollHeight - window.innerHeight,
    elements: results
  };
})();