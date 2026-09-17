/*
 * DC Mobile Notary — gated review flow, page scripts (source of truth).
 *
 * These two IIFEs are embedded as page FOOTER custom code on the Webflow pages:
 *   /feedback       (page id 6aabdd2193443f86601dbaf7)  → FEEDBACK block
 *   /review-thanks  (page id 6aabdd2193443f86601dbb2b)  → THANKS block
 * Page HEAD custom code carries <meta name="robots" content="noindex, nofollow"> and the
 * state styles (.is-visible / .is-open / :hover / :focus) that Webflow's WHTML CSS parser
 * does not accept as single-class rules.
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
  var hideForm = function () { var el = q('form'); if (el) el.style.display = 'none'; };
  var stars = function (n) {
    var el = q('stars'); if (!el) return;
    if (!n) { el.style.display = 'none'; return; }
    var h = ''; for (var i = 1; i <= 5; i++) h += '<span' + (i > n ? ' class="rv-off"' : '') + '>★</span>';
    el.innerHTML = h;
  };
  var BTN = 'Send to the management team';
  if (!t) { hideForm(); show('invalid'); return; }

  fetch(API + 'r/' + encodeURIComponent(t) + '/')
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function (c) {
      if (c.name) q('title').textContent = 'Tell us what went wrong, ' + c.name;
      var svc = c.service_label ? c.service_label + ' order' : 'order';
      var tid = c.tracking_id ? ' (' + c.tracking_id + ')' : '';
      q('intro').textContent = 'Your ' + svc + tid + " didn't meet your expectations, and we want to make it right. A manager reads every message personally and will get back to you within one business day.";
      stars(c.rating);
      if (c.feedback_submitted) { hideForm(); show('success'); }
      if (c.google_url) { var p = q('public'); if (p) p.href = c.google_url; }
      try { gtag('event', 'review_feedback_view', { rating: c.rating || 0 }); } catch (_) {}
    })
    .catch(function () { hideForm(); show('invalid'); });

  var cb = document.getElementById('rv-callback');
  if (cb) cb.addEventListener('change', function () { q('phone-wrap').classList.toggle('is-open', cb.checked); });

  function setBtn(busy) { var b = q('submit'); if (!b) return; b.disabled = !!busy; b.value = busy ? 'Sending…' : BTN; }
  function fail(err, msg) { err.textContent = msg; err.classList.add('is-visible'); setBtn(false); }

  function submit(e) {
    e.preventDefault(); e.stopImmediatePropagation();  // keep Webflow's own form handler out
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
          hideForm(); show('success'); window.scrollTo({ top: 0, behavior: 'smooth' });
          try { gtag('event', 'review_feedback_submitted', { callback: call }); } catch (_) {}
        } else {
          fail(err, (x.j && x.j.detail) || 'Something went wrong. Please try again or email support@dcmobilenotary.com.');
        }
      })
      .catch(function () { fail(err, 'Network error. Please try again or email support@dcmobilenotary.com.'); });
  }
  document.addEventListener('submit', function (e) { if (e.target && e.target.id === 'rv-feedback-form') submit(e); }, true);
})();

/* ===================== /review-thanks ===================== */
(function () {
  var API = 'https://api.dcmobilenotary.net/api/reviews/';
  var t = new URLSearchParams(location.search).get('t') || '';
  var q = function (k) { return document.querySelector('[data-rv="' + k + '"]'); };
  var stars = function (n) {
    var el = q('stars'); if (!el) return;
    if (!n) { el.style.display = 'none'; return; }
    var h = ''; for (var i = 1; i <= 5; i++) h += '<span' + (i > n ? ' class="rv-off"' : '') + '>★</span>';
    el.innerHTML = h;
  };
  if (!t) { stars(0); return; }
  fetch(API + 'r/' + encodeURIComponent(t) + '/')
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function (c) {
      q('title').textContent = 'Thank you for your ' + (c.rating ? c.rating + '-star ' : '') + 'rating' + (c.name ? ', ' + c.name : '') + '!';
      var svc = c.service_label ? c.service_label + ' order' : 'order';
      var tid = c.tracking_id ? ' (' + c.tracking_id + ')' : '';
      q('intro').textContent = "We're really glad your " + svc + tid + " went well, and we truly appreciate your continued trust. We've just sent you a short email, and Trustpilot will follow with a verified review invitation.";
      stars(c.rating);
      if (c.trustpilot_url) q('trustpilot').href = c.trustpilot_url;
      if (c.google_url) q('google').href = c.google_url;
      try { gtag('event', 'review_thanks_view', { rating: c.rating || 0 }); } catch (_) {}
    })
    .catch(function () { stars(0); });
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-rv="trustpilot"],[data-rv="google"]');
    if (a) { try { gtag('event', 'review_platform_click', { platform: a.getAttribute('data-rv') }); } catch (_) {} }
  });
})();
