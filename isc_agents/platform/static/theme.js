/* Runs before CSS to apply the saved/system appearance without a bright flash. */
(() => {
  const key = 'isc-appearance';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { preference = localStorage.getItem(key) || 'system'; } catch (_) {}
  if (!['light','dark','system'].includes(preference)) preference = 'system';
  function apply(value) {
    preference = ['light','dark','system'].includes(value) ? value : 'system';
    const resolved = preference === 'system' ? (media.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.appearance = preference;
    document.querySelectorAll('[data-theme-select]').forEach(select => { select.value = preference; });
    try { localStorage.setItem(key, preference); } catch (_) {}
  }
  apply(preference);
  media.addEventListener('change', () => { if (preference === 'system') apply('system'); });
  window.addEventListener('storage', event => { if (event.key === key) apply(event.newValue || 'system'); });
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-select]').forEach(select => {
      select.value = preference;
      select.addEventListener('change', () => apply(select.value));
    });
  });
})();
