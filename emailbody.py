"""The daily brief as the body of an email.

An email program ignores scripts, external CSS, flexbox, grids and most positioning, so this renderer uses nested tables, inline
styles and bgcolor attributes only. The charts are tables too: a colour grid for the ranks, a strip of coloured blocks for the last
30 days and plain bars. The interactive, zoomable charts stay in the attached HTML page.

The renderer is pure: it takes the plain `facts` dictionary that report.email_facts() builds and returns HTML. It does not read
the market data itself, so every number in the email is one the report already computed.

Size: Gmail cuts an HTML email off near 102 KB, so render() keeps the body under a byte budget and drops the least important
sections first until it fits. render() also returns the names of the sections it dropped, so the log can say so.
"""
import html as _html

EMAIL_BUDGET = 90_000       # bytes of HTML. Gmail clips an email near 102 KB.
COMPACT_BUDGET = 29_000     # the first web app version refused a summary above 30,000 characters

OUTER, CARD, PANEL, LINE = "#070A10", "#0F1521", "#161E2D", "#26324A"
TEXT, MUTED, AMBER = "#F2F4F8", "#A3ACBD", "#F6B73C"
GREEN, RED, GREY = "#2FB67C", "#E5483F", "#7C8798"
VIOLET = "#B7A3FF"
FONT = "font-family:Helvetica,Arial,sans-serif;"
MONO = "font-family:Consolas,Menlo,'Courier New',monospace;"

RANK_BG = {"hi": "#C23B33", "mid": "#9A6414", "lo": "#26324A", "none": "#161E2D"}
STRIP_BG = {4: "#E5483F", 3: "#B7791F", 2: "#3E5478", 1: "#2A3A57", 0: "#1B2538"}
STATUS_BG = {"ALERT": "#C23B33", "NEAR": "#9A6414", "CALM": "#1F6F4A"}
PILL_BG = {"red": "#C23B33", "green": "#1F6F4A", "grey": "#4A5568", "amber": "#9A6414"}
TILE_COLOR = {"neg": "#FF8A80", "pos": "#5ED0A0", "amb": AMBER, "": TEXT}

# The first section named here is the first to go when the email is over its size budget.
DROP_ORDER = ("decisions", "market", "context", "scenarios", "questions", "history", "levels", "outlook")


def esc(value):
    return _html.escape(str(value), quote=True)


def _row(inner, pad="22px 24px 0"):
    return f'<tr><td style="padding:{pad};{FONT}color:{TEXT}">{inner}</td></tr>'


def _chip(text, color=MUTED):
    return (f'<span style="display:inline-block;margin-left:8px;padding:2px 7px;border:1px solid {color};border-radius:3px;font-size:10px;'
            f'font-weight:700;letter-spacing:.08em;color:{color};white-space:nowrap">{esc(text)}</span>')


def _heading(title, *chips):
    return (f'<div style="font-size:13px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:{AMBER};{FONT}">{esc(title)}'
            + "".join(_chip(text, color) for text, color in chips) + '</div>')


def _p(text, size=14, color=MUTED, top=8, bold=False):
    weight = "font-weight:700;" if bold else ""
    return f'<div style="margin-top:{top}px;font-size:{size}px;line-height:1.5;color:{color};{weight}{FONT}">{esc(text)}</div>'


def _rank_chip(rank):
    return (f'<span style="display:inline-block;margin-left:6px;padding:1px 7px;border-radius:4px;background:{RANK_BG.get(rank["tone"], RANK_BG["lo"])};'
            f'color:#FFFFFF;font-size:12px;font-weight:700">{esc(rank["text"])}</span>')


def _table(inner, spacing=0, width="100%", extra=""):
    return (f'<table role="presentation" width="{width}" cellpadding="0" cellspacing="{spacing}" border="0" '
            f'style="border-collapse:{"separate" if spacing else "collapse"};{extra}">{inner}</table>')


def _button(label, url):
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px"><tr>'
            f'<td bgcolor="{AMBER}" style="border-radius:6px;background:{AMBER}"><a href="{esc(url)}" style="display:inline-block;padding:12px 22px;{FONT}'
            f'font-size:15px;font-weight:700;color:#0A0E16;text-decoration:none;border-radius:6px">{esc(label)}</a></td></tr></table>')


# ------------------------------------------------------------------------------------------------
# The sections, in the order they appear
# ------------------------------------------------------------------------------------------------
def header(f):
    return (f'<tr><td bgcolor="{OUTER}" style="padding:16px 24px;background:{OUTER};{FONT}">'
            + _table(f'<tr><td style="font-size:13px;font-weight:700;letter-spacing:.18em;color:{AMBER};{FONT}">CRYPTO WATCH</td>'
                     f'<td align="right" style="font-size:12px;color:{MUTED};{FONT}">Created by <b style="color:{TEXT}">{esc(f["credit"])}</b></td></tr>')
            + '</td></tr>')


def hero(f):
    pill = PILL_BG[f["status"]["tone"]]
    demo = f'<div style="margin-top:8px;font-size:12px;font-weight:700;color:{AMBER};{FONT}">DEMO DATA: synthetic prices</div>' if f.get("demo") else ""
    return _row(
        f'<div style="font-size:12px;font-weight:700;letter-spacing:.14em;color:{MUTED};{FONT}">DAILY BRIEF &middot; {esc(f["as_of"])} UTC CLOSE</div>'
        f'<div style="margin-top:10px;font-size:32px;line-height:1.1;font-weight:800;text-transform:uppercase;color:{TEXT};{FONT}">{esc(f["title"])}</div>'
        f'<div style="margin-top:14px"><span style="display:inline-block;padding:5px 12px;border-radius:4px;background:{pill};color:#FFFFFF;'
        f'font-size:12px;font-weight:700;letter-spacing:.1em;{FONT}">{esc(f["status"]["label"])}</span></div>{demo}'
        + _p(f["deck"], 16, "#D5DAE4", 14) + _p(f["method"], 12, MUTED, 8), "26px 24px 4px")


def actions(f):
    links = f["links"]
    if links.get("report_url"):
        report = ("Interactive report", "Zoomable charts, the full history study and every table, in your browser.", _button("Open the interactive report", links["report_url"]))
    else:
        report = ("Interactive report: attached to this email",
                  f'Open the attached file {links["attachment"]} in your browser for the zoomable charts and every table. Save it first if your mail app only previews attachments.', "")
    if links.get("ask_url"):
        ask = ("Ask the playbook", "Ask a question and get the matching playbook rules, with the section cited.", _button("Ask the playbook", links["ask_url"]))
    else:
        ask = ("Ask the playbook: a tab in the attached report",
               "Open the attached report and click the Ask the playbook tab. Type a question and it shows the matching playbook rules, word for word. "
               "It needs no server and no key, and no AI is used. "
               f'For answers written by the AI, double-click {links["launcher"]} in your crypto-watch-agent folder (or run python rag_server.py).', "")
    panels = "".join(
        f'<tr><td height="10" style="font-size:0;line-height:0">&nbsp;</td></tr><tr><td bgcolor="{PANEL}" style="padding:14px 16px;background:{PANEL};border-left:3px solid {AMBER}">'
        f'<div style="font-size:14px;font-weight:700;color:{TEXT};{FONT}">{esc(title)}</div>'
        f'<div style="margin-top:4px;font-size:13px;line-height:1.5;color:{MUTED};{FONT}">{esc(text)}</div>{button}</td></tr>'
        for title, text, button in (report, ask))
    return _row(_table(panels), "6px 24px 0")


def scoreboard(f):
    blocks = []
    for c in f["scoreboard"]:
        if c.get("error"):
            body = f'<td style="padding:12px 0;font-size:13px;color:{MUTED};{FONT}">Data unavailable: {esc(c["error"])}. No statement is made for this coin.</td>'
            blocks.append(f'<tr><td width="96" valign="top" style="padding:12px 0;border-top:1px solid {LINE};{FONT}"><div style="font-size:20px;font-weight:800;color:{TEXT}">{esc(c["coin"])}</div></td>{body}</tr>')
            continue
        lines = "".join(f'<tr><td style="padding:3px 0;font-size:13px;color:{MUTED};{FONT}">{esc(x["label"])}</td>'
                        f'<td align="right" style="padding:3px 0;font-size:14px;font-weight:600;color:{TEXT};{FONT}">{esc(x["value"])}{_rank_chip(x["rank"])}</td></tr>' for x in c["lines"])
        price = f'<div style="margin-top:4px;font-size:12px;color:{MUTED};{FONT}">{esc(c["price"])}</div>' if c.get("price") else ""
        blocks.append(
            f'<tr><td width="96" valign="top" style="padding:12px 12px 12px 0;border-top:1px solid {LINE};{FONT}"><div style="font-size:20px;font-weight:800;color:{TEXT}">{esc(c["coin"])}</div>'
            f'<div style="font-size:12px;color:{MUTED}">{esc(c["name"])}</div>'
            f'<div style="margin-top:6px"><span style="display:inline-block;padding:2px 8px;border-radius:3px;background:{STATUS_BG.get(c["status"], GREY)};color:#FFFFFF;font-size:11px;font-weight:700;letter-spacing:.08em">{esc(c["status"])}</span></div>{price}</td>'
            f'<td valign="top" style="padding:10px 0;border-top:1px solid {LINE}">{_table(lines)}</td></tr>')
    return _row(_heading("Breach scoreboard") + '<div style="height:6px;font-size:0;line-height:0">&nbsp;</div>' + _table("".join(blocks)) + _p(f["scoreboard_note"], 12, MUTED, 8), "20px 24px 0")


def headlines(f):
    rows = "".join(
        f'<tr><td width="34" valign="top" style="padding:12px 0;border-top:1px solid {LINE};{MONO}font-size:13px;color:{AMBER}">0{i + 1}</td>'
        f'<td style="padding:12px 0;border-top:1px solid {LINE};{FONT}"><div style="font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:{MUTED}">{esc(item["kicker"])}</div>'
        f'<div style="margin-top:3px;font-size:17px;line-height:1.35;font-weight:600;color:{TEXT}">{esc(item["text"])}</div></td></tr>' for i, item in enumerate(f["headlines"]))
    return _row(_heading("Top 3 headlines", ("COMPUTED BY CODE", MUTED)) + '<div style="height:4px;font-size:0;line-height:0">&nbsp;</div>' + _table(rows), "22px 24px 0")


def _tile(t, width):
    sub = f'<div style="margin-top:4px;font-size:12px;line-height:1.4;color:{MUTED};{FONT}">{esc(t["sub"])}</div>' if t.get("sub") else ""
    sub2 = f'<div style="margin-top:3px;font-size:12px;line-height:1.4;color:{MUTED};{FONT}">{esc(t["sub2"])}</div>' if t.get("sub2") else ""
    return (f'<td width="{width}" valign="top" bgcolor="{PANEL}" style="padding:14px 14px;background:{PANEL};border-top:2px solid {LINE}">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:{MUTED};{FONT}">{esc(t["label"])}</div>'
            f'<div style="margin-top:4px;font-size:24px;line-height:1.1;font-weight:800;color:{TILE_COLOR.get(t.get("tone", ""), TEXT)};{FONT}">{esc(t["value"])}</div>{sub}{sub2}</td>')


def _tile_grid(tiles):
    rows = []
    for i in range(0, len(tiles), 2):
        pair = tiles[i:i + 2]
        cells = _tile(pair[0], "50%") + (f'<td width="2%" style="font-size:0">&nbsp;</td>' + _tile(pair[1], "48%") if len(pair) > 1 else '<td width="50%"></td>')
        rows.append(f"<tr>{cells}</tr>" + ('<tr><td colspan="3" height="8" style="font-size:0;line-height:0">&nbsp;</td></tr>' if i + 2 < len(tiles) else ""))
    return _table("".join(rows))


def tiles(f):
    return _row(_tile_grid(f["tiles"]), "20px 24px 0") if f.get("tiles") else ""


def heat(f):
    h = f["heat"]
    head = ('<tr><td></td>' + "".join(f'<td align="center" style="padding:0 0 4px;font-size:10px;line-height:1.2;color:{MUTED};{FONT}">{esc(c)}</td>' for c in h["columns"]) + '</tr>')
    body = "".join(
        f'<tr><td width="52" style="font-size:14px;font-weight:800;color:{TEXT};{FONT}">{esc(r["coin"])}</td>'
        + "".join(f'<td align="center" bgcolor="{RANK_BG.get(c["tone"], RANK_BG["lo"])}" style="padding:11px 0;background:{RANK_BG.get(c["tone"], RANK_BG["lo"])};'
                  f'font-size:15px;font-weight:700;color:#FFFFFF;{FONT}">{esc(c["text"])}</td>' for c in r["cells"]) + "</tr>" for r in h["rows"])
    legend = (f'<div style="margin-top:8px;font-size:12px;line-height:1.5;color:{MUTED};{FONT}">'
              f'<span style="color:#FF9C94;font-weight:700">Red</span> is rank {esc(h["alert"])} or higher, <span style="color:{AMBER};font-weight:700">amber</span> is {esc(h["near"])} to below {esc(h["alert"])}, dark is ordinary. '
              f'The first three columns can raise an alert. The last two are context only.</div>')
    return _row(_heading("Rank heatmap", ("COMPUTED BY CODE", MUTED)) + '<div style="height:8px;font-size:0;line-height:0">&nbsp;</div>' + _table(head + body, 4) + legend, "22px 24px 0")


def history(f):
    h = f["history"]
    rows = []
    for r in h["rows"]:
        cells = "".join(f'<td height="18" bgcolor="{STRIP_BG[v]}" style="background:{STRIP_BG[v]};font-size:0;line-height:0">&nbsp;</td>' for v in r["cells"])
        rows.append(f'<tr><td width="44" style="padding:5px 0;font-size:13px;font-weight:800;color:{TEXT};{FONT}">{esc(r["coin"])}</td>'
                    f'<td style="padding:5px 0">{_table(f"<tr>{cells}</tr>", 1)}</td>'
                    f'<td width="92" align="right" style="padding:5px 0 5px 8px;font-size:12px;color:{MUTED};{FONT}">now <b style="color:{TEXT}">{esc(r["now"])}</b><br>peak {esc(r["peak"])}</td></tr>')
    return _row(_heading(f'Highest rank, last {h["days"]} days') + _p(h["note"], 12, MUTED, 6) + '<div style="height:6px;font-size:0;line-height:0">&nbsp;</div>' + _table("".join(rows)), "22px 24px 0")


def ai(f):
    a = f["ai"]
    chips = (("WRITTEN BY AI", VIOLET),) + ((("PASSED THE CODE GUARDRAILS", GREEN),) if a.get("verdict") == "passed" else (("BLOCKED BY A GUARDRAIL", AMBER),) if a.get("verdict") == "blocked" else ())
    if a.get("note"):
        inner = _heading("AI analysis", *chips) + _p(a["note"], 14, MUTED, 8)
    else:
        parts = [_heading("AI analysis", *chips), _p("The AI model writes the analysis and the code decides what is allowed through. This is the latest saved decision.", 12, MUTED, 8)]
        parts.append(f'<div style="margin-top:10px;font-size:16px;line-height:1.5;color:{TEXT};{FONT}">{esc(a["message"])}</div>')
        parts.append(f'<div style="margin-top:10px;font-size:13px;line-height:1.5;color:#D5DAE4;{FONT}"><b style="color:{VIOLET}">Reasoning given by the AI.</b> {esc(a["reasoning"])}</div>')
        parts.append(f'<div style="margin-top:6px;font-size:13px;line-height:1.5;color:#D5DAE4;{FONT}"><b style="color:{VIOLET}">Playbook rules cited.</b> {esc(a["sources"])}</div>')
        if a.get("cross"):
            parts.append(f'<div style="margin-top:6px;font-size:13px;line-height:1.5;color:#D5DAE4;{FONT}"><b style="color:{VIOLET}">Cross-check by code: {esc(a["cross"][0])}.</b> {esc(a["cross"][1])}</div>')
        if a.get("blocked"):
            parts.append(f'<div style="margin-top:6px;font-size:13px;line-height:1.5;color:{AMBER};{FONT}"><b>Guardrail result.</b> {esc(a["blocked"])}</div>')
        parts.append(f'<div style="margin-top:10px;font-size:11px;line-height:1.6;color:{MUTED};{FONT}">{" &middot; ".join(esc(x) for x in a["facts"])}</div>')
        inner = "".join(parts)
    return _row(_table(f'<tr><td bgcolor="#1A1530" style="padding:16px 18px;background:#1A1530;border-left:4px solid #7C5CD6">{inner}</td></tr>'), "22px 24px 0")


def _bars(items):
    rows = []
    for label, pct in items:
        width = max(1, min(100, round(pct)))
        bar = (f'<tr><td width="{width}%" height="12" bgcolor="{AMBER}" style="background:{AMBER};font-size:0;line-height:0">&nbsp;</td>'
               f'<td height="12" bgcolor="#1B2538" style="background:#1B2538;font-size:0;line-height:0">&nbsp;</td></tr>')
        rows.append(f'<tr><td width="82" style="padding:4px 0;font-size:12px;color:{MUTED};{FONT}">{esc(label)}</td>'
                    f'<td style="padding:4px 0">{_table(bar)}</td>'
                    f'<td width="44" align="right" style="padding:4px 0 4px 8px;font-size:12px;font-weight:700;color:{TEXT};{FONT}">{pct:.0f}%</td></tr>')
    return _table("".join(rows))


def outlook(f):
    o = f["outlook"]
    head = _heading(o.get("title", "What the history says"), ("COMPUTED BY CODE", MUTED), ("PAST DATA, NOT A FORECAST", AMBER))
    if o.get("note"):
        return _row(head + _p(o["note"], 13, MUTED, 8), "22px 24px 0")
    parts = [head, f'<div style="margin-top:10px;font-size:20px;line-height:1.3;font-weight:700;color:{AMBER};{FONT}">{esc(o["headline"])}</div>', _p(o["summary"], 14, "#D5DAE4", 8),
             '<div style="height:12px;font-size:0;line-height:0">&nbsp;</div>', _tile_grid(o["tiles"])]
    if o.get("persist"):
        parts += [f'<div style="margin-top:16px;font-size:13px;font-weight:700;color:{TEXT};{FONT}">How many days the alert rank lasts</div>', _p(o["persist_note"], 12, MUTED, 2), '<div style="height:6px;font-size:0;line-height:0">&nbsp;</div>', _bars(o["persist"])]
    if o.get("outcomes"):
        rows = "".join(f'<tr><td style="padding:6px 8px 6px 0;border-top:1px solid {LINE};font-size:13px;font-weight:700;color:{TEXT};{FONT}">{esc(x["label"])}</td>'
                       f'<td style="padding:6px 8px 6px 0;border-top:1px solid {LINE};font-size:13px;color:{TEXT};{FONT}">{esc(x["median"])}</td>'
                       f'<td style="padding:6px 8px 6px 0;border-top:1px solid {LINE};font-size:13px;color:{MUTED};{FONT}">{esc(x["middle"])}</td>'
                       f'<td style="padding:6px 0;border-top:1px solid {LINE};font-size:13px;color:{MUTED};{FONT}">{esc(x["range"])}</td></tr>' for x in o["outcomes"])
        head_row = "".join(f'<td style="padding:0 8px 4px 0;font-size:11px;color:{MUTED};{FONT}">{esc(t)}</td>' for t in ("After an episode starts", "Median", "Middle half", "Full range"))
        parts += [f'<div style="margin-top:16px;font-size:13px;font-weight:700;color:{TEXT};{FONT}">Where prices went afterwards</div>', _p(o["outcomes_note"], 12, MUTED, 2),
                  '<div style="height:6px;font-size:0;line-height:0">&nbsp;</div>', _table(f"<tr>{head_row}</tr>{rows}")]
    if o.get("trust"):
        parts += [f'<div style="margin-top:16px;font-size:13px;font-weight:700;color:{TEXT};{FONT}">How far to trust this</div>']
        parts += [f'<div style="margin-top:4px;font-size:12px;line-height:1.5;color:{MUTED};{FONT}">&bull; {esc(t)}</div>' for t in o["trust"]]
    return _row("".join(parts), "26px 24px 0")


def questions(f):
    items = "".join(f'<tr><td style="padding:10px 0;border-top:1px solid {LINE};{FONT}"><div style="font-size:14px;font-weight:700;color:{TEXT}">{esc(q)}</div>'
                    f'<div style="margin-top:3px;font-size:13px;line-height:1.5;color:{MUTED}">{esc(a)}</div></td></tr>' for q, a in f["questions"])
    return _row(_heading("Five questions this data raises") + _p("Questions for the reader, not instructions. The report does not tell anyone to act.", 12, MUTED, 4) + _table(items), "22px 24px 0")


def watch(f):
    w = f["watch"]
    lines = "".join(f'<div style="margin-top:6px;font-size:13px;line-height:1.5;color:#D5DAE4;{FONT}">&bull; {esc(x)}</div>' for x in w["lines"])
    return _row(_heading("What to watch at the next review", ("MONITORING TRIGGERS, NOT FORECASTS", AMBER)) + lines, "22px 24px 0")


def levels(f):
    lv = f["levels"]
    blocks = []
    for r in lv["rows"]:
        rows = "".join(f'<tr><td style="padding:3px 0;font-size:12px;color:{MUTED};{FONT}">{esc(col)}</td><td align="right" style="padding:3px 0;font-size:13px;font-weight:600;color:{TEXT};{FONT}">{esc(cell)}</td></tr>'
                       for col, cell in zip(lv["columns"], r["cells"]))
        blocks.append(f'<tr><td bgcolor="{PANEL}" style="padding:12px 14px;background:{PANEL}"><div style="font-size:14px;font-weight:800;color:{TEXT};{FONT}">{esc(r["coin"])} '
                      f'<span style="font-size:12px;font-weight:400;color:{MUTED}">last {esc(r["last"])}</span></div>{_table(rows)}</td></tr><tr><td height="8" style="font-size:0;line-height:0">&nbsp;</td></tr>')
    return _row(_heading("Closes that would reach the alert rank", ("CONDITIONS, NOT PREDICTIONS", AMBER)) + _p(lv["note"], 12, MUTED, 6) + '<div style="height:8px;font-size:0;line-height:0">&nbsp;</div>' + _table("".join(blocks)), "22px 24px 0")


def scenarios(f):
    items = "".join(f'<tr><td style="padding:10px 0;border-top:1px solid {LINE};{FONT}"><div style="font-size:14px;font-weight:700;color:{TEXT}">{esc(a)}</div>'
                    f'<div style="margin-top:3px;font-size:13px;line-height:1.5;color:{MUTED}"><b style="color:#D5DAE4">Observe:</b> {esc(b)}<br><b style="color:#D5DAE4">The agent:</b> {esc(c)}<br>'
                    f'<b style="color:#D5DAE4">No longer applies when:</b> {esc(d)}</div></td></tr>' for a, b, c, d in f["scenarios"])
    return _row(_heading("Conditional watch scenarios", ("NOT PREDICTIONS", AMBER)) + _table(items), "22px 24px 0")


def context(f):
    c = f["context"]
    blocks = "".join(
        f'<tr><td bgcolor="{PANEL}" style="padding:12px 14px;background:{PANEL}"><div style="font-size:14px;font-weight:800;color:{TEXT};{FONT}">{esc(r["coin"])} '
        f'<span style="font-size:12px;font-weight:400;color:{MUTED}">{esc(r["trend"])}</span></div>'
        + _table(''.join(f'<tr><td style="padding:3px 0;font-size:12px;color:{MUTED};{FONT}">{esc(k)}</td><td align="right" style="padding:3px 0;font-size:13px;font-weight:600;color:{TEXT};{FONT}">{esc(v)}{_rank_chip(rk) if rk else ""}</td></tr>'
                         for k, v, rk in r["lines"])) + '</td></tr><tr><td height="8" style="font-size:0;line-height:0">&nbsp;</td></tr>' for r in c["rows"])
    pairs = ""
    if c.get("pairs"):
        items = "".join(f'<tr><td style="padding:4px 0;font-size:13px;font-weight:700;color:{TEXT};{FONT}">{esc(p["pair"])}</td>'
                        f'<td align="right" style="padding:4px 0;font-size:13px;color:{TEXT};{FONT}">{esc(p["corr"])}{_rank_chip(p["rank"])} <span style="color:{MUTED}">{esc(p["reading"])}</span></td></tr>' for p in c["pairs"])
        pairs = _p(c["pairs_note"], 12, MUTED, 8) + _table(items)
    elif c.get("pairs_note"):
        pairs = _p(c["pairs_note"], 12, MUTED, 8)
    return _row(_heading("Context: trend, volume, relationships", ("COMPUTED BY CODE", MUTED)) + _p(c["note"], 12, MUTED, 6) + '<div style="height:8px;font-size:0;line-height:0">&nbsp;</div>' + _table(blocks) + pairs, "22px 24px 0")


def market(f):
    m = f["market"]
    if m.get("note"):
        return _row(_heading("Market context", ("COMPUTED BY CODE", MUTED)) + _p(m["note"], 13, MUTED, 8), "22px 24px 0")
    blocks = "".join(
        f'<tr><td bgcolor="{PANEL}" style="padding:12px 14px;background:{PANEL}"><div style="font-size:14px;font-weight:800;color:{TEXT};{FONT}">{esc(r["coin"])}</div>'
        + _table("".join(f'<tr><td valign="top" width="38%" style="padding:3px 8px 3px 0;font-size:12px;color:{MUTED};{FONT}">{esc(k)}</td><td align="right" style="padding:3px 0;font-size:12px;line-height:1.4;color:{TEXT};{FONT}">{esc(v)}</td></tr>'
                         for k, v in r["lines"])) + '</td></tr><tr><td height="8" style="font-size:0;line-height:0">&nbsp;</td></tr>' for r in m["rows"])
    return _row(_heading("Market context", ("COMPUTED BY CODE", MUTED)) + _p(m["intro"], 12, MUTED, 6) + '<div style="height:8px;font-size:0;line-height:0">&nbsp;</div>' + _table(blocks)
                + _p(m["mood"], 13, "#D5DAE4", 4) + _p(m["flags"], 13, "#D5DAE4", 6), "22px 24px 0")


def decisions(f):
    rows = "".join(
        f'<tr><td valign="top" width="104" style="padding:8px 8px 8px 0;border-top:1px solid {LINE};font-size:12px;color:{MUTED};white-space:nowrap;{FONT}">{esc(d["time"])}</td>'
        f'<td valign="top" style="padding:8px 8px 8px 0;border-top:1px solid {LINE};font-size:12px;font-weight:700;color:{AMBER if d["alert"] else MUTED};{FONT}">{esc(d["result"])}</td>'
        f'<td valign="top" style="padding:8px 0;border-top:1px solid {LINE};font-size:12px;line-height:1.4;color:#D5DAE4;{FONT}">{esc(d["coins"])}{(": " + esc(d["message"])) if d["message"] else ""}</td></tr>' for d in f["decisions"])
    return _row(_heading("Decision history", ("FROM THE SHEET", MUTED)) + '<div style="height:6px;font-size:0;line-height:0">&nbsp;</div>' + _table(rows), "22px 24px 0")


def footer(f):
    return (f'<tr><td style="padding:26px 24px 24px;{FONT}"><div style="border-top:1px solid {LINE};padding-top:16px;font-size:12px;line-height:1.6;color:{MUTED}">'
            f'<div style="font-size:13px;color:{TEXT}">Created by <b>{esc(f["credit"])}</b></div>'
            f'<div style="margin-top:8px"><b style="color:{TEXT}">{esc(f["disclaimer_lead"])}</b> {esc(f["disclaimer"])}</div></div></td></tr>')


# (name, renderer, the facts entry that has to be present for the section to appear)
SECTIONS = (("header", header, "credit"), ("hero", hero, "title"), ("actions", actions, "links"), ("scoreboard", scoreboard, "scoreboard"),
            ("headlines", headlines, "headlines"), ("tiles", tiles, "tiles"), ("heat", heat, "heat"), ("history", history, "history"), ("ai", ai, "ai"),
            ("outlook", outlook, "outlook"), ("questions", questions, "questions"), ("watch", watch, "watch"), ("levels", levels, "levels"),
            ("scenarios", scenarios, "scenarios"), ("context", context, "context"), ("market", market, "market"), ("decisions", decisions, "decisions"))


def _compose(f, skip):
    rows = [fn(f) for name, fn, needs in SECTIONS if name not in skip and f.get(needs)]
    rows.append(footer(f))
    return (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;font-size:1px;line-height:1px">{esc(f["preheader"])}</div>'
            f'<div bgcolor="{OUTER}" style="background:{OUTER};padding:16px 8px;{FONT}">'
            f'<table role="presentation" width="640" align="center" cellpadding="0" cellspacing="0" border="0" bgcolor="{CARD}" '
            f'style="max-width:640px;width:100%;background:{CARD};border-collapse:collapse;color:{TEXT}">' + "".join(rows) + '</table></div>')


def render(facts, budget=EMAIL_BUDGET):
    """(html, dropped): the email body, and the names of the sections left out to stay under `budget` bytes."""
    skip, html = [], _compose(facts, ())
    for key in DROP_ORDER:
        if len(html.encode("utf-8")) <= budget:
            break
        skip.append(key)
        html = _compose(facts, skip)
    return html, [k for k in skip if facts.get(k)]
