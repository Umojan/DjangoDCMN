/*!
 * DCMN Apply bridge v1.0.0
 * Routes the Webflow forms on /business-accounts and /partners to the DCMN backend
 * (POST /api/business-accounts/apply/, POST /api/partners/apply/) instead of the
 * native Webflow form submission.
 *
 * Behaviour:
 *  - runs AFTER the page validation scripts (they stop invalid submits in capture phase);
 *  - builds the JSON contract expected by the backend (svc_* checkboxes -> services[]);
 *  - 2xx  -> redirect to the form's thank-you page;
 *  - 400/429 -> shows `detail` from the API inside .w-form-fail;
 *  - network error / 5xx / 404 -> falls back to the native Webflow submission so no lead is lost.
 *
 * Source of truth: DjangoDCMN repo, frontend/dcmn-apply.js. Deployed as page footer custom code.
 */
(function () {
  "use strict";
  var API = "https://api.dcmobilenotary.net";
  var ROUTES = {
    "Business Account Application": { endpoint: "/api/business-accounts/apply/", redirect: "/business-accounts-thank-you", program: "business_account" },
    "Partner Application":          { endpoint: "/api/partners/apply/",          redirect: "/partners-thank-you",          program: "partner" }
  };
  var GENERIC_ERROR = "Something went wrong. Please try again — or call 202-247-0837 or email support@dcmobilenotary.com.";

  function serialize(form, program) {
    var out = { program: program, services: [] }, el, i, name, v;
    for (i = 0; i < form.elements.length; i++) {
      el = form.elements[i]; name = el.name;
      if (!name || el.type === "submit" || el.type === "button" || el.disabled) continue;
      if (el.type === "checkbox") {
        if (name.indexOf("svc_") === 0) { if (el.checked) out.services.push(name.slice(4)); continue; }
        out[name] = !!el.checked; continue;
      }
      if (el.type === "radio") { if (el.checked) out[name] = el.value; continue; }
      v = (el.value || "").trim();
      if (name === "cf-turnstile-response") { out.turnstile_token = v; continue; }
      if (name === "attribution") continue; // sent as an object below
      out[name] = v;
    }
    out.attribution = null;
    try { if (window.DCMNTracker && window.DCMNTracker.getAttribution) out.attribution = window.DCMNTracker.getAttribution() || null; } catch (e) {}
    out.page_url = location.href;
    out.source_page = out.source_page || location.pathname;
    return out;
  }

  function track(event, program, extra) {
    try {
      var params = { program: program };
      if (extra) { for (var k in extra) params[k] = extra[k]; }
      if (typeof window.gtag === "function") window.gtag("event", event, params);
      else if (window.dataLayer && window.dataLayer.push) window.dataLayer.push({ event: event, program: program });
    } catch (e) {}
  }

  function failBlock(form) {
    var wrap = form.closest(".w-form") || form.parentElement;
    var fail = wrap ? wrap.querySelector(".w-form-fail") : null;
    return fail ? (fail.firstElementChild || fail) : null;
  }

  function showError(form, message) {
    var block = failBlock(form);
    if (block) {
      block.textContent = message;
      block.parentElement.style.display = "block";
      block.parentElement.scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      alert(message);
    }
  }

  function hideError(form) {
    var block = failBlock(form);
    if (block) block.parentElement.style.display = "none";
  }

  function setBusy(form, busy) {
    var btn = form.querySelector('input[type="submit"], button[type="submit"]');
    if (!btn) return;
    if (busy) {
      btn.dataset.dcmnLabel = btn.value || btn.textContent;
      var wait = btn.getAttribute("data-wait") || "Sending...";
      if (btn.tagName === "INPUT") btn.value = wait; else btn.textContent = wait;
      btn.disabled = true; btn.style.opacity = "0.7";
    } else {
      if (btn.tagName === "INPUT") btn.value = btn.dataset.dcmnLabel || btn.value; else btn.textContent = btn.dataset.dcmnLabel || btn.textContent;
      btn.disabled = false; btn.style.opacity = "";
    }
  }

  function nativeFallback(form) {
    // Let Webflow's own handler take over (stores the submission in Webflow + redirect).
    form.dataset.dcmnNative = "1";
    setBusy(form, false);
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  }

  function send(form, route) {
    var payload = serialize(form, route.program);
    hideError(form);
    setBusy(form, true);
    track("application_submit", route.program);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", API + route.endpoint, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.timeout = 20000;
    xhr.onload = function () {
      var status = xhr.status, data = null;
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) {}
      if (status >= 200 && status < 300) {
        track("application_success", route.program, { application_id: data && data.id });
        location.href = form.getAttribute("redirect") || form.getAttribute("data-redirect") || route.redirect;
        return;
      }
      if (status === 400 || status === 422 || status === 429) {
        var msg = (data && (data.detail || data.error || data.message)) || GENERIC_ERROR;
        track("application_error", route.program, { status: status });
        setBusy(form, false);
        showError(form, typeof msg === "string" ? msg : GENERIC_ERROR);
        return;
      }
      // 404 (endpoint not deployed yet), 5xx, anything else -> native Webflow submission
      track("application_error", route.program, { status: status, fallback: "native" });
      nativeFallback(form);
    };
    xhr.onerror = xhr.ontimeout = function () {
      track("application_error", route.program, { status: 0, fallback: "native" });
      nativeFallback(form);
    };
    xhr.send(JSON.stringify(payload));
  }

  function bind(form, route) {
    if (form.dataset.dcmnApply) return;
    form.dataset.dcmnApply = "1";
    form.addEventListener("submit", function (e) {
      if (form.dataset.dcmnNative === "1") return; // fallback in progress -> Webflow handles it
      e.preventDefault();
      e.stopImmediatePropagation();
      send(form, route);
    }, true);
  }

  function init() {
    var forms = document.querySelectorAll("form[data-name]"), i, route;
    for (i = 0; i < forms.length; i++) {
      route = ROUTES[forms[i].getAttribute("data-name")];
      if (route) bind(forms[i], route);
    }
  }

  // Register after the validation scripts' DOMContentLoaded handlers so their
  // capture-phase listeners run first and stop invalid submissions.
  function deferredInit() { setTimeout(init, 0); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", deferredInit);
  else deferredInit();
})();
