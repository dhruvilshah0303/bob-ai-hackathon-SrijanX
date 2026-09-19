
(function () {
  var TILT_SELECTOR = '.card, .scenario-card, .fleet-item, .kpi, .rec-hero';
  var MAX_DEG = 7;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var activeEl = null;

  if (reduceMotion) return;

  function applyTilt(el, clientX, clientY) {
    var rect = el.getBoundingClientRect();
    var px = (clientX - rect.left) / rect.width;   // 0..1 across the card
    var py = (clientY - rect.top) / rect.height;    // 0..1 down the card

    el.style.setProperty('--tilt-x', (px * 100).toFixed(1) + '%');
    el.style.setProperty('--tilt-y', (py * 100).toFixed(1) + '%');
  }

  function resetTilt(el) {
  }

  document.addEventListener('mousemove', function (e) {
    var el = e.target.closest ? e.target.closest(TILT_SELECTOR) : null;

    if (el !== activeEl) {
      if (activeEl) resetTilt(activeEl);
      activeEl = el;
    }
    if (el) applyTilt(el, e.clientX, e.clientY);
  });

  document.addEventListener('mouseleave', function () {
    if (activeEl) {
      resetTilt(activeEl);
      activeEl = null;
    }
  });
})();
