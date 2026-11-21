// Runs rag_static.js in Node for tests/test_asktab.py. Reads {data, questions, texts} as JSON on stdin and prints the engine's results.
// "ms" is the time each question took in the engine, so a test can prove that an adversarial question does not make a pattern slow.
const { createEngine } = require("../rag_static.js");
let input = "";
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const { data, questions, texts } = JSON.parse(input);
  const engine = createEngine(data);
  const ms = [];
  const answers = questions.map((q) => {
    const start = process.hrtime.bigint();
    const answer = engine.ask(q);
    ms.push(Number(process.hrtime.bigint() - start) / 1e6);
    return answer;
  });
  process.stdout.write(JSON.stringify({
    answers: answers,
    ms: ms,
    hits: questions.map((q) => engine.search(q).map((h) => ({ title: h.title, score: h.score }))),
    terms: texts.map((t) => engine.terms(t)),
    sentences: texts.map((t) => engine.sentences(t)),
  }));
});
