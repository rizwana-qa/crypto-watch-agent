/* The two tabs of the report and the Ask tab's page code. Everything the user types is shown with textContent, never as HTML.
 * Needs rag_static.js (window.CWA_createEngine) and the playbook data in the element with the id ask-data.
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) { e.className = cls; } if (text !== undefined) { e.textContent = text; } return e; }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  // ---- the tabs: Report and Ask the playbook ----
  var bar = $("tabbar");
  if (bar) {
    var names = ["report", "ask"], buttons = {}, panels = {}, where = { report: 0, ask: 0 }, current = "report";
    names.forEach(function (n) { buttons[n] = $("tab-" + n); panels[n] = $("panel-" + n); });

    var select = function (name, focus) {
      if (!panels[name]) { name = "report"; }
      if (name === current && !panels[name].hidden) { return name; }
      where[current] = window.pageYOffset || 0;
      names.forEach(function (n) {
        var on = n === name;
        buttons[n].setAttribute("aria-selected", on ? "true" : "false");
        buttons[n].tabIndex = on ? 0 : -1;
        panels[n].hidden = !on;
      });
      current = name;
      if (focus) { buttons[name].focus(); }
      // charts drawn while their tab was hidden need to measure themselves again
      try { window.dispatchEvent(new Event("resize")); } catch (e) { /* older browser: the charts keep their size */ }
      window.scrollTo(0, where[name]);
      return name;
    };
    var fromHash = function () { return window.location.hash === "#ask" ? "ask" : "report"; };
    var go = function (name, focus) {
      name = select(name, focus);
      try { window.history.replaceState(null, "", "#" + name); } catch (e) { window.location.hash = name; }
    };

    bar.hidden = false;
    Array.prototype.forEach.call(document.querySelectorAll("[data-tab]"), function (node) {
      node.addEventListener("click", function (ev) {
        ev.preventDefault();
        go(node.getAttribute("data-tab"), false);
        if (node.tagName === "A") { var q = $("ask-q"); if (q) { q.focus(); } }
      });
    });
    bar.addEventListener("keydown", function (ev) {
      var i = names.indexOf(current), next = -1;
      if (ev.key === "ArrowRight") { next = (i + 1) % names.length; }
      else if (ev.key === "ArrowLeft") { next = (i + names.length - 1) % names.length; }
      else if (ev.key === "Home") { next = 0; }
      else if (ev.key === "End") { next = names.length - 1; }
      if (next >= 0) { ev.preventDefault(); go(names[next], true); }
    });
    window.addEventListener("hashchange", function () { select(fromHash(), false); });
    select(fromHash(), false);
  }

  // ---- the Ask tab ----
  var holder = $("ask-data"), box = $("ask-answer");
  if (!holder || !box || !window.CWA_createEngine) { return; }
  var data = JSON.parse(holder.textContent), engine = window.CWA_createEngine(data), byTitle = {};
  data.sections.forEach(function (s) { byTitle[s.title] = s; });

  function fullText(title) {
    var wrap = el("div", "full"), list = null, section = byTitle[title];
    if (!section) { return wrap; }
    section.text.split("\n").forEach(function (raw) {
      var line = raw.trim();
      if (!line) { list = null; return; }
      if (line.indexOf("- ") === 0) {
        if (!list) { list = el("ul"); wrap.appendChild(list); }
        list.appendChild(el("li", "", line.slice(2)));
        return;
      }
      list = null;
      wrap.appendChild(el("p", line.toLowerCase().indexOf("keywords:") === 0 ? "kw" : "", line));
    });
    return wrap;
  }

  // "Why: <reason_text>" for every answer that is not a normal answer. The word Why and the amber bar are both there, so the reason is
  // never shown by colour alone. It sits inside the live region (the answer box), so a screen reader announces it with the answer.
  function reasonLine(text) {
    var line = el("p", "reason");
    line.appendChild(el("b", "", "Why:"));
    line.appendChild(document.createTextNode(" " + text));
    return line;
  }

  function render(a) {
    clear(box);
    box.hidden = false;
    var head = el("h2", "", "Answer");
    head.appendChild(el("span", "chip code", "Computed by code"));
    head.appendChild(el("span", "chip check", "No AI"));
    box.appendChild(head);
    if (a.question) { box.appendChild(el("blockquote", "q", a.question)); }
    box.appendChild(el("p", "bl", a.bottom_line));
    if (a.reason_text && a.kind !== "answer") { box.appendChild(reasonLine(a.reason_text)); }
    if (a.kind === "declined" && a.why.length) { box.appendChild(el("p", "sub", a.why[0].text)); }
    // the reason line already says what the note says, except when the AI failed and the note names the error
    if (a.note && a.kind !== "declined" && (!a.reason_text || a.reason === "ai_unavailable")) { box.appendChild(el("p", "sub", a.note)); }
    if (a.evidence.length) {
      box.appendChild(el("span", "cap", a.kind === "declined" ? "The rule behind this" : "From the playbook, word for word"));
      var hits = el("div", "hits");
      a.evidence.forEach(function (e, i) {
        var card = el("article", i === 0 ? "hit lead" : "hit"), top = el("div", "top");
        top.appendChild(el("span", "n", ("0" + e.n).slice(-2)));
        top.appendChild(el("h3", "", e.title));
        card.appendChild(top);
        card.appendChild(el("blockquote", "", e.quote));
        var more = el("details");
        if (i === 0) { more.open = true; }      // the best match shows its whole section at once
        more.appendChild(el("summary", "", "Full section"));
        more.appendChild(fullText(e.title));
        card.appendChild(more);
        hits.appendChild(card);
      });
      box.appendChild(hits);
    }
    if (a.related.length) {
      box.appendChild(el("span", "cap", "Also ask"));
      var row = el("div", "qchips");
      row.style.marginTop = "0";
      a.related.forEach(function (q) { var b = el("button", "", q); b.type = "button"; b.setAttribute("data-q", q); row.appendChild(b); });
      box.appendChild(row);
    }
    box.appendChild(el("p", "muted", "Quotes are word for word from playbook.md, picked by code. This describes the rules; it is not investment advice."));
  }

  function ask(question) {
    var a = engine.ask(question);
    $("ask-q").value = a.question;
    render(a);
    box.scrollIntoView({ block: "start" });
    box.focus({ preventScroll: true });
  }

  $("ask-form").addEventListener("submit", function (ev) { ev.preventDefault(); ask($("ask-q").value); });
  $("panel-ask").addEventListener("click", function (ev) {
    var node = ev.target.closest ? ev.target.closest("[data-q]") : null;
    if (node) { ask(node.getAttribute("data-q")); }
  });
})();
