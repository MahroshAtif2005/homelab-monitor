// HomeLab Monitor — landing scroll effects.
// Pure vanilla, no deps. Uses IntersectionObserver so it's cheap; respects
// prefers-reduced-motion (CSS does the disabling there).

(function () {
  if (typeof IntersectionObserver === 'undefined') {
    // Old browser — just reveal everything up-front so nothing stays hidden.
    document.querySelectorAll('.hl-reveal').forEach(function (el) {
      el.classList.add('hl-in');
    });
    return;
  }

  function init() {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('hl-in');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });

    document.querySelectorAll('.hl-reveal').forEach(function (el) {
      io.observe(el);
    });
  }

  // mkdocs-material's navigation.instant SPAs the page swap — re-run on each
  // navigation so newly-inserted reveal targets are observed.
  if (window.document$ && typeof window.document$.subscribe === 'function') {
    window.document$.subscribe(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
