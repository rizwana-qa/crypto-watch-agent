"""The "Ask the playbook" tab of the HTML report.

The report page has two tabs: Report and Ask the playbook. The Ask tab works from the file alone, with no server, no key and no network:
rag_static.js is a line-for-line port of the retrieval half of rag.py, and the playbook sections ride inside the page as JSON.
It shows the playbook's own words (a sentence picked by code, plus the full section on demand). It never writes an answer with AI.
AI-written answers stay on the local page (start_ask_page.bat), because a key must never be put into a file that can be forwarded.

rag.py is the source of truth. This module hands the browser the same constants, stopwords, synonyms, refusal patterns (advice, forecast,
live data, personal account, secrets), reason texts and sections, and tests/test_asktab.py runs both engines on the same questions and
requires the same answers, kind and reason included.
"""
import html
import json
from pathlib import Path

import rag

HERE = Path(__file__).resolve().parent
EXAMPLES = ["When does the agent raise an alert?", "Which words are banned in an alert message?", "What does near threshold mean?",
            "How is 30-day volatility calculated?", "When does the agent not repeat an alert?"]
LAUNCHER = "start_ask_page.bat"
LOCAL_URL = "http://127.0.0.1:8765/"


def topic_question(title):
    """The question a topic tile asks."""
    return f"What does the playbook say about {title}?"


def data(index=None):
    """Everything the browser engine needs, taken from rag.py so the two cannot drift apart."""
    index = index or rag.Index()
    return {
        "config": {"top_k": rag.TOP_K, "min_share": rag.MIN_SHARE, "k1": rag.K1, "b": rag.B, "title_repeat": rag.TITLE_REPEAT,
                   "max_question": rag.MAX_QUESTION},
        "stopwords": sorted(rag.STOPWORDS),
        "synonyms": {word: list(extra) for word, extra in rag.QUERY_SYNONYMS.items()},
        "advice": {"source": rag.ADVICE_REQUEST_RE.pattern, "flags": "i"},
        "advice_only": {"source": rag.ADVICE_ONLY_RE.pattern, "flags": "i"},      # the rest of the advice pattern is a forecast
        "personal": {"source": rag.PERSONAL_RE.pattern, "flags": "i"},
        "live": {"sources": [p.pattern for p in rag.LIVE_DATA_RES], "flags": "i"},
        "security": {"sources": [p.pattern for p in rag.SECURITY_RES], "flags": "i"},
        "reasons": dict(rag.REASON_TEXT),
        "refused": dict(rag.REFUSED_LINE),
        "declined": list(rag.DECLINED),
        "not_in_playbook": rag.NOT_IN_PLAYBOOK,
        "related": rag.RELATED,
        "default_related": rag.DEFAULT_RELATED,
        "sections": [{"title": s["title"], "text": s["text"]} for s in index.sections],
    }


def as_json(value):
    """JSON that is safe inside a <script> element: no raw '<', so nothing in it can close the tag or open a comment."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")


def e(value):
    return html.escape(str(value), quote=True)


CSS = """
[hidden]{display:none!important}
.tabbar{position:sticky;top:0;z-index:20;background:var(--bar);border-bottom:1px solid #1B2433}
.tabbar .wrap{display:flex;flex-wrap:wrap;gap:4px 8px}
.tabbar button{min-height:48px;padding:0 18px;background:transparent;border:0;border-bottom:3px solid transparent;color:var(--muted);font:600 15px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;letter-spacing:.02em;cursor:pointer}
.tabbar button:hover{color:var(--text)}
.tabbar button[aria-selected=true]{color:var(--text);border-bottom-color:var(--amber)}
.tabbar button:focus-visible{outline:3px solid #8FE3D8;outline-offset:-3px}
.askform label{display:block;margin:4px 0 8px}
.askrow{display:flex;flex-wrap:wrap;gap:10px}
.askrow input{flex:1 1 320px;min-width:0;min-height:48px;padding:0 16px;border-radius:6px;border:1px solid #3A465C;background:#0A0E16;color:var(--text);font:500 16px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.askrow input::placeholder{color:#8993A6}
.askrow input:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.askbtn{min-height:48px;padding:0 26px;border-radius:6px;border:0;background:var(--amber);color:#0A0E16;font:700 15px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;cursor:pointer}
.askbtn:hover{background:#FFC95A}
.askbtn:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.qchips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.qchips button{min-height:36px;padding:6px 14px;border-radius:999px;border:1px solid #3A465C;background:transparent;color:var(--soft);font:500 13px Inter,"Segoe UI",Arial,sans-serif;text-align:left;cursor:pointer}
.qchips button:hover{border-color:var(--amber);color:var(--text)}
.qchips button:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
#ask-answer{scroll-margin-top:72px;border-top:3px solid var(--teal)}
#ask-answer:focus{outline:none}
#ask-answer .bl{margin:6px 0 4px;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:600;font-size:clamp(26px,3.4vw,34px);line-height:1.2;color:var(--text)}
#ask-answer .cap{display:block;margin:20px 0 10px}
.hits{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:14px}
.hit{background:#0D1420;border:1px solid var(--line);border-left:3px solid var(--teal);border-radius:6px;padding:16px 18px}
.hit.lead{grid-column:1/-1}
.hit .top{display:flex;align-items:baseline;gap:10px}
.hit .n{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:13px;color:var(--amber)}
.hit h3{margin:0;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:22px;line-height:1.15;text-transform:uppercase;letter-spacing:.02em}
.hit blockquote{margin:10px 0;padding:0;font-size:15px;line-height:1.55;color:var(--soft)}
.hit details{margin-top:8px;font-size:14px}
.hit summary{cursor:pointer;color:#8FE3D8;font-weight:600;min-height:32px;display:flex;align-items:center}
.hit summary:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.hit .full{margin-top:6px;color:var(--soft)}
.hit .full p{margin:8px 0;line-height:1.55}
.hit .full ul{margin:6px 0;padding-left:20px}.hit .full li{margin:5px 0;line-height:1.5}
.hit .full .kw{color:var(--muted);font-size:12px}
.topics{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:12px}
.topic{display:flex;flex-direction:column;gap:4px;text-align:left;padding:14px 16px;border-radius:6px;border:1px solid var(--line);background:#0D1420;color:var(--soft);font:400 13px/1.45 Inter,"Segoe UI",Arial,sans-serif;cursor:pointer}
.topic:hover{border-color:var(--amber)}
.topic:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.topic .n{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12px;color:var(--amber)}
.topic .t{font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:20px;line-height:1.15;text-transform:uppercase;color:var(--text)}
.ref{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:10px 24px;margin:0}
.ref div{display:flex;justify-content:space-between;gap:14px;border-bottom:1px solid var(--line);padding:8px 0;font-size:14px}
.ref dt{color:var(--muted)}.ref dd{margin:0;text-align:right;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;color:var(--text)}
.words{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.words span{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12px;padding:2px 8px;border-radius:4px;background:#1A2231;color:var(--soft)}
#ask-answer .reason{margin:12px 0 4px;padding:10px 14px;border-left:4px solid var(--amber);background:rgba(246,183,60,.10);border-radius:4px;font-size:15px;line-height:1.5;color:var(--text)}
#ask-answer .reason b{font-weight:700;color:var(--amber);margin-right:4px}
@media print{.tabbar{display:none}}
"""


def tab_bar():
    """Hidden until the script runs: without JavaScript the report simply shows and there are no dead tabs."""
    return ('<nav class="tabbar" id="tabbar" aria-label="Report sections" hidden><div class="wrap" role="tablist">'
            '<button type="button" role="tab" id="tab-report" aria-controls="panel-report" aria-selected="true" data-tab="report">Report</button>'
            '<button type="button" role="tab" id="tab-ask" aria-controls="panel-ask" aria-selected="false" tabindex="-1" data-tab="ask">Ask the playbook</button>'
            '</div></nav>')


def link():
    """The brand-bar link: opens the tab in this same file."""
    return '<a href="#ask" data-tab="ask" title="Opens the Ask tab in this report. No server, no key, no AI.">Ask the playbook &rarr;</a>'


def panel(index=None):
    index = index or rag.Index()
    ref = rag.reference()
    chips = "".join(f'<button type="button" data-q="{e(q)}">{e(q)}</button>' for q in EXAMPLES)
    topics = "".join(
        f'<button type="button" class="topic" data-q="{e(topic_question(t["title"]))}"><span class="n">{t["n"]:02d}</span>'
        f'<span class="t">{e(t["title"])}</span><span>{e(t["line"])}</span></button>' for t in rag.topics(index))
    rows = [("Alert rank", f'{ref["alert_rank"]:g} to 100'), ("Near threshold", f'{ref["near_rank"]:g} to under {ref["alert_rank"]:g}'),
            ("Alert message length", f'{ref["max_chars"]} characters at most'), ("Decisions remembered", str(ref["history_limit"])),
            ("History needed to rank", f'{ref["min_history_days"]} daily candles')]
    reference = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in rows)
    words = "".join(f"<span>{e(w)}</span>" for w in ref["banned_words"])
    return (
        '<main class="wrap" id="panel-ask" role="tabpanel" aria-labelledby="tab-ask" hidden>'
        '<section class="card"><h2>Ask the playbook<span class="chip code">Computed by code</span><span class="chip check">No AI in this tab</span></h2>'
        '<p class="sub">Ask how the agent works: its alert rules, what each metric means, what the wording rules ban. The tab finds the matching sections of '
        'playbook.md and shows their own words. It describes the rules. It does not give advice and it does not forecast prices.</p>'
        '<form id="ask-form" class="askform" role="search" autocomplete="off"><label for="ask-q" class="cap">Your question</label>'
        '<div class="askrow"><input id="ask-q" type="text" maxlength="300" minlength="3" required placeholder="For example: When does the agent raise an alert?">'
        '<button type="submit" class="askbtn">Ask</button></div></form>'
        f'<div class="qchips" aria-label="Example questions">{chips}</div></section>'
        '<section class="card" id="ask-answer" tabindex="-1" aria-live="polite" hidden></section>'
        f'<section class="card"><h2>Topics in the playbook<span class="chip code">{len(index.sections)} sections</span></h2>'
        f'<p class="sub">Tap a topic to see what the playbook says about it.</p><div class="topics">{topics}</div></section>'
        f'<section class="card"><h2>The numbers the agent uses<span class="chip code">From the agent settings</span></h2><dl class="ref">{reference}</dl>'
        f'<div class="cap" style="margin-top:14px">Words an alert message may not use</div><div class="words">{words}</div></section>'
        '<section class="card"><h2>Want written answers?</h2>'
        '<p class="sub">This tab never calls an AI, so it is safe to forward. For answers written by the AI and checked by code, run the local page: '
        f'double-click <b>{e(LAUNCHER)}</b> in your crypto-watch-agent folder (or run <b>python rag_server.py</b>). It opens at '
        f'<a href="{LOCAL_URL}">{e(LOCAL_URL)}</a> and needs a Groq key that stays on your computer.</p></section>'
        '</main>')


def script(index=None):
    """The engine, its data and the tab code, as one <script> block placed after the page content."""
    engine = (HERE / "rag_static.js").read_text(encoding="utf-8")
    ui = (HERE / "rag_tab.js").read_text(encoding="utf-8")
    for name, code in (("rag_static.js", engine), ("rag_tab.js", ui)):
        if "</script" in code.lower() or "<!--" in code:
            raise ValueError(f"{name} must not contain '</script' or '<!--'")
    return f'<script type="application/json" id="ask-data">{as_json(data(index))}</script><script>{engine}\n{ui}</script>'
