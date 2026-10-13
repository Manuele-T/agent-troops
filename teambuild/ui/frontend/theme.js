/* Theme switch: Auto (follows the system) → Light → Dark. Loaded in <head> without defer so the right
 * colours apply before the page paints. The choice is kept in this browser only (localStorage). */
(function () {
  'use strict';
  var KEY = 'tb-theme';
  var ORDER = ['auto', 'light', 'dark'];
  var ICON = { auto: '◐', light: '☀', dark: '☾' };
  var NAME = { auto: 'Auto', light: 'Light', dark: 'Dark' };

  function read() {
    try {
      var v = window.localStorage.getItem(KEY);
      return ORDER.indexOf(v) >= 0 ? v : 'auto';
    } catch (e) {
      return 'auto';
    }
  }

  function save(mode) {
    try { window.localStorage.setItem(KEY, mode); } catch (e) { /* private mode: just don't remember */ }
  }

  function apply(mode) {
    var root = document.documentElement;
    if (mode === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.textContent = ICON[mode] + ' ' + NAME[mode];
      btn.setAttribute('aria-label', 'Colour theme: ' + NAME[mode] + '. Press to change.');
    }
  }

  var current = read();
  apply(current);

  document.addEventListener('DOMContentLoaded', function () {
    apply(current);
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
      save(current);
      apply(current);
    });
  });
}());
