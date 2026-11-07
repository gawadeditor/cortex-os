/* Cortex demo redaction: run on a dashboard page before recording or taking screenshots.
 *
 * Blurs money amounts, emails and key-like tokens, hides secret inputs, and swaps
 * client names for placeholders. It keeps working while the page updates.
 *
 * One-time setup (per browser): store the names to replace, in the dashboard's console:
 *   localStorage.setItem('cortexRedact', JSON.stringify({names: {'Real Client Name': 'Client A'}}))
 * Names live in your browser only, never in this file.
 *
 * Run it: paste this file in the console, or use the bookmarklet built by
 * tools/build_redact_bookmarklet.py. Press Ctrl+Shift+H to stop blurring (reload to restore names).
 */
(function () {
  if (window.__cortexRedact) { window.__cortexRedact.toggle(); return; }

  var cfg = { names: {} };
  try { cfg = Object.assign(cfg, JSON.parse(localStorage.getItem('cortexRedact') || '{}')); } catch (e) {}

  var MONEY = /(?:\$|USD\s?|€|£)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:USD|\$)/;
  var EMAIL = /[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
  var TOKEN = /\b(?:sk|pk|rk|key|tok)[-_][A-Za-z0-9_-]{16,}\b|\b[A-Za-z0-9_-]{32,}\b/;
  var SECRET_INPUT = 'input[type=password], input[name*=key i], input[name*=token i], input[name*=secret i]';

  var style = document.createElement('style');
  style.textContent = '.__cx-blur{filter:blur(7px)!important;user-select:none!important}';
  document.head.appendChild(style);

  var on = true;
  var escaped = Object.keys(cfg.names).map(function (n) {
    return [new RegExp(n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), cfg.names[n]];
  });

  function sweep() {
    if (!on) return;
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var n, hits = [];
    while ((n = w.nextNode())) {
      var p = n.parentElement;
      if (!p || /^(SCRIPT|STYLE)$/.test(p.tagName)) continue;
      var v = n.nodeValue, orig = v;
      escaped.forEach(function (pair) { v = v.replace(pair[0], pair[1]); });
      if (v !== orig) n.nodeValue = v;
      if (MONEY.test(v) || EMAIL.test(v) || TOKEN.test(v)) hits.push(p);
    }
    hits.forEach(function (el) { el.classList.add('__cx-blur'); });
    document.querySelectorAll(SECRET_INPUT).forEach(function (el) { el.classList.add('__cx-blur'); });
  }

  var timer = null;
  var observer = new MutationObserver(function () {
    clearTimeout(timer);
    timer = setTimeout(sweep, 60);
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  sweep();

  window.__cortexRedact = {
    toggle: function () {
      on = !on;
      document.querySelectorAll('.__cx-blur').forEach(function (el) {
        el.style.filter = on ? '' : 'none';
      });
      if (on) sweep();
    }
  };
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'h') window.__cortexRedact.toggle();
  });
  console.log('[cortex-redact] active. names: ' + escaped.length);
})();
