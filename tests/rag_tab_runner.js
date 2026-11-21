// Runs rag_tab.js (the Ask tab's page code) in Node with a very small fake page, for tests/test_fix_rag_reasons.py.
// It is NOT a real browser: it proves what text the code puts into the answer box, not how a browser draws it.
// Reads {data, questions} as JSON on stdin. For each question it submits the form and prints the text and classes in the answer box.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

class Node_ {
  constructor(tag) {
    this.tagName = String(tag || "#text").toUpperCase(); this.children = []; this.className = ""; this.hidden = false;
    this.attrs = {}; this.handlers = {}; this.style = {}; this._text = ""; this.value = "";
  }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  scrollIntoView() {}
  focus() {}
  find(test, out) {
    out = out || [];
    if (test(this)) { out.push(this); }
    this.children.forEach((c) => c.find(test, out));
    return out;
  }
}

let input = "";
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const { data, questions } = JSON.parse(input);
  const ids = {};
  ["ask-data", "ask-answer", "ask-q", "ask-form", "panel-ask"].forEach((id) => { ids[id] = new Node_("div"); });
  ids["ask-data"]._text = JSON.stringify(data);
  ids["ask-answer"].hidden = true;
  const document = {
    getElementById: (id) => ids[id] || null,
    createElement: (tag) => new Node_(tag),
    createTextNode: (text) => { const n = new Node_("#text"); n._text = String(text); return n; },
    querySelectorAll: () => [],
  };
  const sandbox = { document: document, JSON: JSON, Set: Set, Map: Map, Array: Array, Object: Object, RegExp: RegExp, Math: Math, String: String, Number: Number };
  sandbox.window = sandbox;
  sandbox.window.pageYOffset = 0;
  sandbox.window.scrollTo = () => {};
  vm.createContext(sandbox);
  const root = path.join(__dirname, "..");
  vm.runInContext(fs.readFileSync(path.join(root, "rag_static.js"), "utf8"), sandbox);
  vm.runInContext(fs.readFileSync(path.join(root, "rag_tab.js"), "utf8"), sandbox);
  const results = questions.map((q) => {
    ids["ask-q"].value = q;
    ids["ask-form"].handlers.submit({ preventDefault() {} });
    const box = ids["ask-answer"];
    const reasons = box.find((n) => /(^| )reason( |$)/.test(n.className)).map((n) => ({ tag: n.tagName, text: n.textContent, label: n.children.length ? n.children[0].textContent : "" }));
    return { text: box.textContent, hidden: box.hidden, reasons: reasons,
             subs: box.find((n) => n.className === "sub").map((n) => n.textContent) };
  });
  process.stdout.write(JSON.stringify({ results: results }));
});
