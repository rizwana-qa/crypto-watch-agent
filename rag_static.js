/* Ask the playbook, in the browser: the retrieval half of rag.py, ported line by line. No AI, no network, no key.
 *
 * rag.py is the source of truth. asktab.py hands this file the same constants, stopwords, synonyms, refusal patterns, reason texts and
 * playbook sections that rag.py uses, and tests/test_asktab.py runs both on the same questions and requires identical answers,
 * the kind and the reason code included.
 * Plain ES2015 on purpose: no regex look-behind and no newer syntax, so it also runs in older phone browsers.
 *
 * No log line is written here. rag.py and rag_server.py write "[ask] kind=... reason=..." to stderr, but this engine runs in the
 * reader's own browser and makes no network call, so nothing a reader types ever leaves the page. That is on purpose.
 */
(function (root) {
  "use strict";

  function createEngine(data) {
    var cfg = data.config;
    var stop = new Set(data.stopwords);
    var advice = new RegExp(data.advice.source, data.advice.flags);
    var adviceOnly = new RegExp(data.advice_only.source, data.advice_only.flags);   // an advice match that is not this one is a forecast
    var personal = new RegExp(data.personal.source, data.personal.flags);
    var live = data.live.sources.map(function (p) { return new RegExp(p, data.live.flags); });
    var security = data.security.sources.map(function (p) { return new RegExp(p, data.security.flags); });
    var sections = data.sections;

    function stem(word) {
      var n = word.length;
      if (n > 4 && /(ches|shes|sses|xes|zes)$/.test(word)) {
        word = word.slice(0, -2);
      } else if (n > 3 && word.slice(-1) === "s" && !/(ss|us|is)$/.test(word)) {
        word = word.slice(0, -1);
      }
      var suffixes = ["ing", "ed"];
      for (var i = 0; i < suffixes.length; i++) {
        var s = suffixes[i];
        if (word.length > s.length + 3 && word.slice(-s.length) === s) { return word.slice(0, -s.length); }
      }
      return word;
    }

    function terms(text) {
      var found = [];
      var words = String(text).toLowerCase().match(/[a-z0-9]+/g) || [];
      words.forEach(function (w) {
        if (stop.has(w) || (w.length < 3 && !(/^[0-9]+$/.test(w) && w.length >= 2))) { return; }
        found.push(stem(w));
      });
      return found;
    }

    // ---- the index: BM25 over the sections, the title counting TITLE_REPEAT times ----
    var docs = sections.map(function (s) {
      var title = terms(s.title), out = [];
      for (var i = 0; i < cfg.title_repeat; i++) { out = out.concat(title); }
      return out.concat(terms(s.text));
    });
    var counts = docs.map(function (doc) {
      var m = new Map();
      doc.forEach(function (t) { m.set(t, (m.get(t) || 0) + 1); });
      return m;
    });
    var size = docs.length;
    var average = docs.reduce(function (sum, d) { return sum + d.length; }, 0) / size;
    var frequency = new Map();
    counts.forEach(function (m) { m.forEach(function (_n, t) { frequency.set(t, (frequency.get(t) || 0) + 1); }); });

    function idf(term) {
      var df = frequency.get(term) || 0;
      return Math.log(1 + (size - df + 0.5) / (df + 0.5));
    }

    function scores(question) {
      var wanted = new Set(terms(question));
      Array.from(wanted).forEach(function (word) {
        if (Object.prototype.hasOwnProperty.call(data.synonyms, word)) {
          data.synonyms[word].forEach(function (extra) { wanted.add(extra); });
        }
      });
      return docs.map(function (doc, order) {
        var length = doc.length, score = 0;
        wanted.forEach(function (term) {
          var tf = counts[order].get(term) || 0;
          if (tf) { score += idf(term) * tf * (cfg.k1 + 1) / (tf + cfg.k1 * (1 - cfg.b + cfg.b * length / average)); }
        });
        return { score: score, order: order };
      });
    }

    function search(question) {
      var ranked = scores(question).filter(function (x) { return x.score > 0; })
        .sort(function (a, b) { return (b.score - a.score) || (a.order - b.order); });
      if (!ranked.length) { return []; }
      var floor = ranked[0].score * cfg.min_share;
      return ranked.slice(0, cfg.top_k).filter(function (x) { return x.score >= floor; }).map(function (x) {
        return { title: sections[x.order].title, text: sections[x.order].text, score: Math.round(x.score * 1000) / 1000, order: x.order };
      });
    }

    // ---- quotes: a sentence of the section, word for word, picked by code ----
    function trimEnds(s) { return s.replace(/^[ \t-]+|[ \t-]+$/g, ""); }

    function sentences(text) {
      var out = [];
      text.replace(/([.!?])\s+/g, "$1\n").split(/\n+/).forEach(function (part) {
        var s = trimEnds(part);
        if (s.length > 25 && part.trim().toLowerCase().indexOf("keywords:") !== 0) { out.push(s); }
      });
      return out;
    }

    function quoteFor(section, question) {
      var wanted = new Set(terms(question)), best = null, bestScore = -1;
      sentences(section.text).forEach(function (sentence) {
        var have = new Set(terms(sentence)), score = 0;
        wanted.forEach(function (t) { if (have.has(t)) { score++; } });
        if (score > bestScore) { best = sentence; bestScore = score; }
      });
      return (best || section.text.slice(0, 200)).trim();
    }

    // ---- answers ----
    function cleanQuestion(q) { return String(q === null || q === undefined ? "" : q).replace(/\s+/g, " ").trim().slice(0, cfg.max_question); }
    function bare(q) { return q.toLowerCase().replace(/\?+$/, ""); }

    function evidence(hits, question, cited) {
      var wanted = cited || hits.map(function (h) { return h.title; });
      var out = [];
      hits.forEach(function (h, i) {
        if (wanted.indexOf(h.title) !== -1) { out.push({ n: i + 1, title: h.title, quote: quoteFor(h, question) }); }
      });
      return out;
    }

    function reasonText(code) {
      return code && Object.prototype.hasOwnProperty.call(data.reasons, code) ? data.reasons[code] : "";
    }

    function anyMatch(list, text) {
      for (var i = 0; i < list.length; i++) { if (list[i].test(text)) { return true; } }
      return false;
    }

    // The reason code when the question is declined before any search, or an empty string. Same order as refusal_code in rag.py.
    function refusalCode(question) {
      if (anyMatch(security, question)) { return "security_request"; }
      if (advice.test(question)) { return adviceOnly.test(question) ? "advice_request" : "price_forecast"; }
      if (personal.test(question)) { return "personal_account"; }
      if (anyMatch(live, question)) { return "live_data_request"; }
      return "";
    }

    function answer(question, kind, bottomLine, why, hits, note, reason) {
      var key = why.length ? why[0].section : (hits.length ? hits[0].title : "");
      var related = (Object.prototype.hasOwnProperty.call(data.related, key) ? data.related[key] : data.default_related)
        .filter(function (q) { return bare(q) !== bare(question); });
      var cited = why.length ? why.map(function (w) { return w.section; }) : null;
      return { question: question, kind: kind, bottom_line: bottomLine, why: why, related: related,
               evidence: hits.length ? evidence(hits, question, cited) : [], note: note || "", reason: reason || "", reason_text: reasonText(reason) };
    }

    function ask(raw) {
      var question = cleanQuestion(raw);
      if (question.length < 3) {
        return { question: question, kind: "error", bottom_line: "Type a question about the playbook.", why: [],
                 related: data.default_related.slice(), evidence: [], note: "", reason: "empty_question", reason_text: reasonText("empty_question") };
      }
      var code = refusalCode(question);
      if (code === "advice_request" || code === "price_forecast") {
        var wording = sections.filter(function (s) { return s.title === "Wording rules"; });
        var hits0 = wording.length ? [{ title: wording[0].title, text: wording[0].text, score: 0, order: 0 }] : [];
        var why0 = hits0.length ? [{ text: data.declined[1], section: "Wording rules" }] : [];
        return answer(question, "declined", data.declined[0], why0, hits0, "", code);
      }
      if (code) { return answer(question, "declined", data.refused[code], [], [], "", code); }
      var hits = search(question);
      if (!hits.length) {
        return answer(question, "no_match", data.not_in_playbook, [], [], "No section of the playbook matches these words. Try other words.", "off_topic");
      }
      var why = hits.slice(0, 3).map(function (h) { return { text: quoteFor(h, question), section: h.title }; });
      return answer(question, "retrieval", "The closest playbook section is " + hits[0].title + ".", why, hits,
                    "No AI is used in this tab. It shows the matching playbook text, found by code.", "ai_off");
    }

    return { ask: ask, search: search, terms: terms, stem: stem, sentences: sentences, refusalCode: refusalCode };
  }

  if (typeof module !== "undefined" && module.exports) { module.exports = { createEngine: createEngine }; }
  root.CWA_createEngine = createEngine;
})(typeof window !== "undefined" ? window : this);
