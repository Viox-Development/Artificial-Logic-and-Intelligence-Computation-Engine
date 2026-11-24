"use strict";
// Text shown in the app, in the user's language (#214). Same rules as alice/i18n.py: source strings are English and
// wrapped in _(), translations come from alice/locales/<lang>.json, a missing one shows the English text, and
// placeholders are {name}:  _("{n} files changed", { n: 3 })
// The server puts {"lang": ..., "catalog": {...}} into the page as <script type="application/json" id="alice-i18n">;
// this file is loaded before every other app script so they can all call _().
(function () {
  let data = { lang: "en", catalog: {} };
  try {
    const el = document.getElementById("alice-i18n");
    if (el) data = Object.assign(data, JSON.parse(el.textContent || "{}"));
  } catch (e) { /* a broken block leaves the app in English rather than not starting */ }
  const fill = (text, vars) => text.replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? String(vars[k]) : m));
  window.ALICE_LANG = data.lang || "en";
  window.ALICE_LOCALE = window.ALICE_LANG === "tr" ? "tr-TR" : "en-US";  // dates, numbers, case-insensitive search
  window._ = function (text, vars) {
    const out = (data.catalog && data.catalog[text]) || text;
    return vars ? fill(out, vars) : out;
  };
  // Static text in the HTML: <a data-i18n>Projects</a> translates the text, data-i18n-attr="title placeholder"
  // translates those attributes; the English in the HTML is the key.
  window.i18nApply = function (root) {
    for (const el of (root || document).querySelectorAll("[data-i18n]")) el.textContent = window._(el.textContent.trim());
    for (const el of (root || document).querySelectorAll("[data-i18n-attr]")) {
      for (const name of el.dataset.i18nAttr.split(/\s+/).filter(Boolean)) {
        if (el.hasAttribute(name)) el.setAttribute(name, window._(el.getAttribute(name)));
      }
    }
  };
  if (data.lang !== "en") document.addEventListener("DOMContentLoaded", () => window.i18nApply());
})();
