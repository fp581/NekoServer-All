/* The setup window's theme: Night (the default, as on the dashboard) or Lamplight,
   switched with the ☾ / ☀ button at the top right and remembered in this browser.
   Loaded in the page's head, before anything is drawn, so a Lamplight choice never
   flashes dark first. (setup.js does the rest of the page.) */
(() => {
  'use strict';
  const KEY = 'neko-theme'; // the dashboard's name and format (a JSON string)
  const root = document.documentElement;

  /* The browser may refuse storage (a private window, a locked-down profile): the
     choice then lasts as long as the window. */
  function stored() {
    try {
      const theme = JSON.parse(localStorage.getItem(KEY));
      return theme === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  }
  function store(theme) {
    try {
      localStorage.setItem(KEY, JSON.stringify(theme));
    } catch {
      /* not kept; it still applies to this window */
    }
  }

  function apply(theme) {
    root.dataset.nekoTheme = theme;
    // On the root itself too: no stylesheet outranks it.
    root.style.colorScheme = theme;
    const button = document.getElementById('theme-toggle');
    if (button) {
      const toLight = theme === 'dark';
      button.title = toLight ? 'Switch to Lamplight' : 'Switch to Neko night';
      button.setAttribute(
        'aria-label',
        toLight ? 'Switch to the light theme (Lamplight)' : 'Switch to the dark theme (Neko night)'
      );
    }
  }

  function toggle() {
    const theme = root.dataset.nekoTheme === 'light' ? 'dark' : 'light';
    store(theme);
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
      apply(theme);
      return;
    }
    if (typeof document.startViewTransition === 'function') {
      document.startViewTransition(() => apply(theme));
      return;
    }
    root.classList.add('theme-easing');
    apply(theme);
    setTimeout(() => root.classList.remove('theme-easing'), 500);
  }

  apply(stored());
  document.addEventListener('DOMContentLoaded', () => {
    apply(root.dataset.nekoTheme);
    document.getElementById('theme-toggle').addEventListener('click', toggle);
  });
})();
