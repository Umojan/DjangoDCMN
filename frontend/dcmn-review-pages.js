/*
 * DC Mobile Notary — gated review flow, page scripts (source of truth).
 *
 * These two IIFEs are embedded as page FOOTER custom code on the Webflow pages:
 *   /feedback       (page id 6aabdd2193443f86601dbaf7)  → FEEDBACK block
 *   /review-thanks  (page id 6aabdd2193443f86601dbb2b)  → THANKS block
 * Page HEAD custom code carries <meta name="robots" content="noindex, nofollow"> and the
 * state styles (.is-visible / .is-open / :hover / :focus, Webflow's .w-checkbox wrapper) that the
 * WHTML CSS parser does not accept as single-class rules. Markup: centered card (.fb-* / .rt-* classes),
 * elements addressed by data-rv="title|stars|pill|intro|form|option|phone-wrap|error|submit|success|invalid|public|trustpilot|google".
 *
 * Both pages receive ?t=<signed token> from the backend redirect
 * (POST /api/reviews/r/<token>/<stars>/ → 302). They call:
 *   GET  /api/reviews/r/<token>/   → { name, service_label, tracking_id, rating, route,
 *                                     feedback_submitted, google_url, trustpilot_url }
 *   POST /api/reviews/feedback/    → { token, message, callback_requested, phone } → { ok } | { detail }
 *
 * When changing this file, update the Webflow page footer code too.
 */

/* ===================== /feedback ===================== */
(function () {
  var API = 'https://api.dcmobilenotary.net/api/reviews/';
  var t = new URLSearchParams(location.search).get('t') || '';
  var q = function (k) { return document.querySelector('[data-rv="' + k + '"]'); };
  var show = function (k) { var el = q(k); if (el) el.classList.add('is-visible'); };
  var hide = function (k) { var el = q(k); if (el) el.style.display = 'none'; };
  var stars = function (n) {
    var el = q('stars'); if (!el) return;
    if (!n) { el.style.display = 'none'; return; }
    var h = ''; for (var i = 1; i <= 5; i++) h += '<span' + (i > n ? ' class="fb-off"' : '') + '>★</span>';
    el.innerHTML = h;
  };
  var BTN = 'Send to the management team', busy = false;
  // Webflow strips label[for]; restore from data-for.
  document.querySelectorAll('[data-for]').forEach(function (l) { l.htmlFor = l.getAttribute('data-for'); });
  // Webflow's site-wide Turnstile disables every form submit button until the challenge passes.
  // This form never goes through Webflow (see the capture-phase submit handler), so keep it enabled.
  setInterval(function () { var b = q('submit'); if (b && !busy && b.disabled) b.disabled = false; }, 300);
  if (!t) { stars(0); hide('pill'); hide('form'); show('invalid'); return; }

  fetch(API + 'r/' + encodeURIComponent(t) + '/')
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function (c) {
      if (c.name) q('title').textContent = 'Tell us what went wrong, ' + c.name;
      var pill = [c.service_label, c.tracking_id].filter(Boolean).join(' · ');
      if (pill) q('pill').textContent = pill; else hide('pill');
      q('intro').textContent = "Your order didn't meet your expectations, and we want to make it right. A manager reads every message personally and will get back to you within one business day.";
      stars(c.rating);
      if (c.feedback_submitted) { hide('form'); show('success'); }
      if (c.google_url) { var p = q('public'); if (p) p.href = c.google_url; }
      try { gtag('event', 'review_feedback_view', { rating: c.rating || 0 }); } catch (_) {}
    })
    .catch(function () { stars(0); hide('pill'); hide('form'); show('invalid'); });

  var cb = document.getElementById('rv-callback');
  if (cb) cb.addEventListener('change', function () {
    q('phone-wrap').classList.toggle('is-open', cb.checked);
    var o = q('option'); if (o) o.classList.toggle('is-on', cb.checked);
    if (cb.checked) { var p = document.getElementById('rv-phone'); if (p) p.focus(); }
  });

  function setBtn(b) { busy = !!b; var el = q('submit'); if (!el) return; el.disabled = busy; el.value = busy ? 'Sending…' : BTN; }
  function fail(err, msg) { err.textContent = msg; err.classList.add('is-visible'); setBtn(false); }

  function submit(e) {
    e.preventDefault(); e.stopImmediatePropagation();  // keep Webflow's own form handler out
    if (busy) return;
    var msg = (document.getElementById('rv-message').value || '').trim();
    var call = !!(cb && cb.checked);
    var phone = (document.getElementById('rv-phone').value || '').trim();
    var err = q('error'); err.classList.remove('is-visible');
    if (!msg && !call) { fail(err, 'Please tell us what went wrong, or ask us to call you back.'); return; }
    setBtn(true);
    fetch(API + 'feedback/', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: t, message: msg, callback_requested: call, phone: phone })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (x) {
        if (x.ok) {
          setBtn(false); hide('form'); show('success'); window.scrollTo({ top: 0, behavior: 'smooth' });
          try { gtag('event', 'review_feedback_submitted', { callback: call }); } catch (_) {}
        } else {
          fail(err, (x.j && x.j.detail) || 'Something went wrong. Please try again or email support@dcmobilenotary.com.');
        }
      })
      .catch(function () { fail(err, 'Network error. Please try again or email support@dcmobilenotary.com.'); });
  }
  document.addEventListener('submit', function (e) { if (e.target && e.target.id === 'rv-feedback-form') submit(e); }, true);
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest && e.target.closest('[data-rv="submit"]');
    if (b) { e.preventDefault(); submit(e); }
  }, true);
})();

/* ===================== /review-thanks ===================== */
(function () {
  var API = 'https://api.dcmobilenotary.net/api/reviews/';
  var t = new URLSearchParams(location.search).get('t') || '';
  var q = function (k) { return document.querySelector('[data-rv="' + k + '"]'); };
  var hide = function (k) { var el = q(k); if (el) el.style.display = 'none'; };
  var stars = function (n) {
    var el = q('stars'); if (!el) return;
    if (!n) { el.style.display = 'none'; return; }
    var h = ''; for (var i = 1; i <= 5; i++) h += '<span' + (i > n ? ' class="rt-off"' : '') + '>★</span>';
    el.innerHTML = h;
  };
  if (!t) { stars(0); hide('pill'); return; }
  fetch(API + 'r/' + encodeURIComponent(t) + '/')
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function (c) {
      q('title').textContent = 'Thank you for the ' + (c.rating ? c.rating + '-star ' : '') + 'rating' + (c.name ? ', ' + c.name : '') + '!';
      var pill = [c.service_label, c.tracking_id].filter(Boolean).join(' · ');
      if (pill) q('pill').textContent = pill; else hide('pill');
      stars(c.rating);
      if (c.trustpilot_url) q('trustpilot').href = c.trustpilot_url;
      if (c.google_url) q('google').href = c.google_url;
      try { gtag('event', 'review_thanks_view', { rating: c.rating || 0 }); } catch (_) {}
    })
    .catch(function () { stars(0); hide('pill'); });
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-rv="trustpilot"],[data-rv="google"]');
    if (a) { try { gtag('event', 'review_platform_click', { platform: a.getAttribute('data-rv') }); } catch (_) {} }
  });
})();
