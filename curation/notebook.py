"""Adjudication workbench inside a Jupyter notebook (no web server, no browser tab).

For the Mayo Clinic Platform workspace, where a local web server may not be reachable. In a notebook cell:

    from curation.notebook import Workbench
    Workbench("data", "AB")          # your initials: a cardiologist reads their cases, the lead resolves disagreements

    from curation.notebook import progress
    progress("data")                 # data scientist: cases done per outcome, agreement, gates

The same rules as the web workbench: the same case packets, the same proposals (assisted reads: Accept, Edit,
Reject: decide myself), the same validation, and every view and read goes to the audit trail. Sign-in is by
initials (governance auth.passwords false) or asks for a password when passwords are on.
"""
from __future__ import annotations

import getpass
import html
import math

from sqlalchemy import select

from . import actions, governance
from cardioai_core.security import verify_password

from .adjudication import AdjudicationError, queue_for
from .runs import current
from .store import User, log_event, open_store

CSS = """<style>
.cw table{border-collapse:collapse;font-size:12px;margin:4px 0}.cw td,.cw th{border-bottom:1px solid #ddd;padding:2px 6px;text-align:left}
.cw .num{text-align:right}.cw mark{background:#fde68a}.cw .muted{color:#666}.cw .box{border:1px solid #ccc;border-radius:6px;padding:8px;margin:6px 0}
.cw .prop{background:#eef6ff;border-color:#9cc3f5}.cw .H{color:#b91c1c;font-weight:bold}.cw h3{margin:10px 0 4px}
.cw .note{border-left:3px solid #ccc;padding:4px 8px;margin:6px 0;white-space:pre-wrap;font-size:12px}
</style>"""


def _e(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return html.escape(str(x))


def _day(d) -> str:
    return f"+{d}" if d is not None and d >= 0 else str(d)


def _table(rows: list, cols: list[tuple[str, str]], empty: str) -> str:
    if not rows:
        return f"<p class='muted'>{_e(empty)}</p>"
    head = "".join(f"<th>{_e(h)}</th>" for h, _ in cols)
    body = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in (f(r) for _, f in cols)) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def event_html(view: dict) -> str:
    P = view["packet"]
    pt = P["patient"]
    hist = ", ".join(k.replace("_", " ") for k, v in pt["history"].items() if v) or "none recorded"
    meds = ", ".join(k.replace("_", " ") for k, v in pt["baseline_meds"].items() if v) or "none"
    died = f" · <b style='color:#b91c1c'>died day {_day(pt['death_day'])}</b>" if pt.get("death_day") is not None else ""
    enc = _table(P["encounters"], [("Day", lambda e: _day(e["day"])), ("Date", lambda e: _e(e["date"])), ("Visit", lambda e: _e(e["visit"])),
                                   ("Code", lambda e: _e(e["code"])), ("Position", lambda e: _e(e["position"]))],
                 "No outcome diagnosis code in this window (case found from a procedure).")
    labs = _table(P["labs"], [("Day", lambda x: _day(x["day"])), ("Time", lambda x: _e(x["time"][5:].replace("T", " "))),
                              ("Test", lambda x: _e(x["analyte"].replace("_", " "))), ("Value", lambda x: _e(x["value"])),
                              ("Unit", lambda x: _e(x["unit"])), ("Ref high", lambda x: _e(x["ref_high"])),
                              ("", lambda x: f"<span class='{_e(x['flag'])}'>{_e(x['flag'] or '')}</span>")],
                  "No troponin / natriuretic peptide results in this window.")
    procs = _table(P["procedures"], [("Day", lambda x: _day(x["day"])), ("Date", lambda x: _e(x["date"])),
                                     ("Procedure", lambda x: _e(x["category"].replace("_", " "))), ("Code", lambda x: _e(x["code"]))],
                   "No relevant procedures in this window.")
    seg = lambda ss: "".join(f"<mark>{_e(s['t'])}</mark>" if s["h"] else _e(s["t"]) for s in ss)   # noqa: E731
    notes = "".join(
        f"<div class='note'><b>{_e(n['title'])}</b> <span class='muted'>{_e(n['date'])} · day {_day(n['day'])}</span><br>"
        + ("".join(f"<div>…{seg(x)}…</div>" for x in n["excerpts"]) if n["excerpts"] else _e(n["text"][:400]))
        + f"<details><summary>Full note</summary>{_e(n['text'])}</details></div>" for n in P["notes"]) or "<p class='muted'>No notes in this window.</p>"
    reads = ""
    if view.get("independent_reads"):
        reads = "<h3>Independent reads</h3>" + "".join(
            f"<div class='box'><b>{_e(r['reader'])}</b>: {_e(r['outcome'])}{' · ' + _e(r.get('subtype')) if r.get('subtype') else ''}"
            f"{' · ' + _e(r.get('event_date')) if r.get('event_date') else ''} <span class='muted'>({_e(r.get('confidence'))})</span>"
            f"{'<br>“' + _e(r['comment']) + '”' if r.get('comment') else ''}</div>" for r in view["independent_reads"])
    return (f"{CSS}<div class='cw'><h2 style='margin:0'>{_e(P['family_name'])} · <span class='muted'>{_e(view['case_id'])}</span></h2>"
            f"<p>Candidate date <b>{_e(P['candidate_date'])}</b> (day 0) · window {P['window_days'][0]} to +{P['window_days'][1]} days · "
            f"{_e(pt.get('age'))} y {_e(pt.get('sex') or '')} · history: {_e(hist)} · baseline meds: {_e(meds)}{died}</p>"
            f"<p class='muted'>{_e(P['definition'])}</p>{reads}"
            f"<h3>Coded encounters</h3>{enc}<h3>Biomarkers</h3>{labs}<h3>Procedures</h3>{procs}"
            f"<h3>Notes &amp; reports</h3><p class='muted'>Highlighted terms are keyword matches only: read the context (negation, history, rule-out).</p>{notes}</div>")


def proposal_html(p: dict, P: dict) -> str:
    d = p["decision"]
    if "outcome" in d:
        what = {"event": f"Confirmed event · {_e(P['subtypes'].get(d['subtype'], d['subtype']))} · {_e(d['event_date'])}",
                "not_event": "Not an event"}.get(d["outcome"], "Cannot determine")
        crit = "".join(f"<li>{_e(P['criteria'].get(c, c))}</li>" for c in d.get("criteria") or [])
        what += f" <span class='muted'>· {_e(d['confidence'])} confidence</span>" + (f"<ul>{crit}</ul>" if crit else "")
    else:
        what = f"LVEF {_e(d['lvef']['value'])} · CAC {_e(d['cac_score']['value'])}"
    agrees = f" · agrees with {_e(', '.join(p['agrees_with']))}" if p.get("agrees_with") else ""
    note = f"<br><b>{_e(p['note'])}</b>" if p.get("note") else ""
    return (f"{CSS}<div class='cw'><div class='box prop'><b>Proposed decision (assisted read)</b><br>{what}"
            f"<div class='muted'>{_e(p['rationale'])}{agrees}</div>{note}</div></div>")


class Workbench:
    """Read cases in the notebook. `Workbench("data", "AB")` displays itself."""

    def __init__(self, data_dir: str, initials: str, password: str | None = None, display_now: bool = True):
        import ipywidgets as w
        self.w, self.data_dir = w, data_dir
        self.S = open_store(data_dir)
        with self.S() as db:
            u = db.scalar(select(User).where(User.username == initials)) or db.scalar(select(User).where(User.username == initials.upper()))
            if not u or not u.active:
                raise ValueError(f"No active user {initials!r}")
            if governance.load(data_dir).get("auth", {}).get("passwords", False):
                if not verify_password(password or getpass.getpass(f"Password for {u.username}: "), u.password_hash):
                    raise PermissionError("Wrong password")
                log_event(db, u.username, "auth.login", "notebook", {"method": "password"})
            else:
                log_event(db, u.username, "auth.login", "notebook", {"method": "initials", "authenticated": False})
            db.commit()
            self.user_id, self.username, self.role = u.id, u.username, u.role
        if self.role not in ("cardiologist", "lead_cardiologist"):
            raise PermissionError(f"{self.username} is a {self.role}: use progress(data_dir) instead")
        self.header, self.case_out, self.form = w.HTML(), w.Output(), w.VBox()
        self.status = w.HTML()
        self.picker = w.Dropdown(description="Case", layout=w.Layout(width="420px"))
        self.picker.observe(lambda ch: ch["new"] and self.load(ch["new"]), names="value")
        nxt = w.Button(description="Next open case", icon="forward")
        nxt.on_click(lambda _: self.next_case())
        self.ui = w.VBox([self.header, w.HBox([self.picker, nxt]), w.HBox([self.case_out, self.form],
                         layout=w.Layout(align_items="flex-start")), self.status])
        self.case_out.layout = w.Layout(width="64%", max_height="900px", overflow="auto")
        self.form.layout = w.Layout(width="34%", padding="0 0 0 12px")
        self.refresh()
        self.next_case()
        if display_now:
            from IPython.display import display
            display(self.ui)

    # ---------------------------------------------------------------- queue
    def _db_user_run(self, db):
        return db.get(User, self.user_id), current(db)

    def refresh(self) -> None:
        with self.S() as db:
            u, run = self._db_user_run(db)
            q = queue_for(db, run.id, u)
        self.queue = q["disagreements"] + q["open"] if self.role == "lead_cardiologist" else q["open"]
        lead = f" · <b>{len(q['disagreements'])}</b> disagreements to resolve" if self.role == "lead_cardiologist" else ""
        self.header.value = (f"{CSS}<div class='cw'><b>{_e(self.username)}</b> ({self.role.replace('_', ' ')}) · "
                             f"<b>{len(q['open'])}</b> open · {q['completed']} completed{lead}</div>")
        opts = [(f"{c['case_id']} · {'report check' if c['type'] == 'annotation' else c['family']}"
                 + (" · disagreement" if c in q["disagreements"] else ""), c["case_id"]) for c in self.queue[:500]]
        self.picker.unobserve_all("value")
        self.picker.options = opts or [("No open cases. Thank you!", None)]
        self.picker.value = None
        self.picker.observe(lambda ch: ch["new"] and self.load(ch["new"]), names="value")

    def next_case(self) -> None:
        if self.queue:
            self.load(self.queue[0]["case_id"])
        else:
            self.case_out.clear_output()
            self.form.children = [self.w.HTML("<b>No open cases.</b> Thank you!")]

    # ---------------------------------------------------------------- one case
    def load(self, case_id: str) -> None:
        from IPython.display import HTML, display
        with self.S() as db:
            u, run = self._db_user_run(db)
            try:
                self.view = actions.open_case(db, self.data_dir, run, u, case_id)
            except (actions.ActionError, AdjudicationError) as e:
                self.status.value = f"<span style='color:#b91c1c'>{_e(e)}</span>"
                return
        self.case_id = case_id
        self.case_out.clear_output()
        with self.case_out:
            if self.view["case_type"] == "annotation":
                P = self.view["packet"]
                display(HTML(f"{CSS}<div class='cw'><h2 style='margin:0'>Report value check · <span class='muted'>{_e(case_id)}</span></h2>"
                             f"<p>Check the LVEF / calcium score in this report ({_e(P['date'])}); leave a field blank if the report "
                             f"does not state it.</p><div class='note'>{_e(P['text'])}</div></div>"))
            else:
                display(HTML(event_html(self.view)))
        self.status.value = ""
        self.form.children = self._annotation_form() if self.view["case_type"] == "annotation" else self._event_form()

    def _proposal_buttons(self, accept, edit, reject):
        w = self.w
        b1, b2, b3 = (w.Button(description="Accept", button_style="success"), w.Button(description="Edit…"),
                      w.Button(description="Reject: decide myself"))
        b1.on_click(lambda _: accept())
        b2.on_click(lambda _: edit())
        b3.on_click(lambda _: reject())
        return w.HBox([b1, b2, b3])

    def _event_form(self):
        w, P, prop = self.w, self.view["packet"], self.view.get("proposal")
        if self.view.get("my_read"):
            r = self.view["my_read"]
            return [w.HTML(f"<b>Submitted</b>: {_e(r.get('outcome'))} {_e(r.get('subtype') or '')} {_e(r.get('event_date') or '')}")]
        outcome = w.RadioButtons(options=[("Confirmed event", "event"), ("Not an event", "not_event"),
                                          ("Cannot determine from the record", "indeterminate")], value=None)
        subtype = w.Dropdown(options=[(v, k) for k, v in P["subtypes"].items()], description="Subtype")
        date = w.Text(value=P["candidate_date"], description="Event date")
        crit = [w.Checkbox(description=v, value=False, indent=False, layout=w.Layout(width="auto")) for v in P["criteria"].values()]
        crit_keys = list(P["criteria"])
        conf = w.ToggleButtons(options=[("High", "high"), ("Medium", "medium"), ("Low", "low")], value="medium")
        cmt = w.Textarea(placeholder="Required if the outcome cannot be determined", layout=w.Layout(width="100%"))
        ev_box = w.VBox([subtype, date, w.HTML("<b>Criteria met</b>"), *crit], layout=w.Layout(display="none"))
        outcome.observe(lambda ch: setattr(ev_box.layout, "display", "" if ch["new"] == "event" else "none"), names="value")
        submit = w.Button(description="Submit decision", button_style="primary", layout=w.Layout(width="100%"))
        own = w.VBox([w.HTML(f"<b>{'Lead adjudication' if self.view['mode'] == 'lead' else 'Your decision'}</b>"),
                      outcome, ev_box, w.HTML("<b>Confidence</b>"), conf, w.HTML("<b>Comment</b>"), cmt, submit])

        def fill(d):
            outcome.value = d["outcome"]
            if d.get("subtype") in P["subtypes"]:
                subtype.value = d["subtype"]
            if d.get("event_date"):
                date.value = d["event_date"]
            for k, c in zip(crit_keys, crit):
                c.value = k in (d.get("criteria") or [])
            conf.value = d.get("confidence") or "medium"

        def decision():
            d = {"outcome": outcome.value, "confidence": conf.value, "comment": cmt.value}
            if outcome.value == "event":
                d.update(subtype=subtype.value, event_date=date.value.strip(),
                         criteria=[k for k, c in zip(crit_keys, crit) if c.value])
            return d

        submit.on_click(lambda _: self.submit(decision()))
        if not prop:
            return [own]
        own.layout.display = "none"
        show = lambda: setattr(own.layout, "display", "")   # noqa: E731

        def accept():
            if not prop["acceptable"]:
                fill(prop["decision"]); show()                       # noqa: E702
                self.status.value = f"<b>{_e(prop['note'])}</b>"
                return
            self.submit(prop["decision"])
        return [w.HTML(proposal_html(prop, P)),
                self._proposal_buttons(accept, lambda: (fill(prop["decision"]), show()), show), own]

    def _annotation_form(self):
        w, prop = self.w, self.view.get("proposal")
        if self.view.get("my_read"):
            return [w.HTML("<b>Submitted</b>")]
        ext = self.view["packet"]["extracted"]
        lvef = w.Text(value="" if ext.get("lvef") is None else str(ext["lvef"]), description="LVEF %")
        cac = w.Text(value="" if ext.get("cac_score") is None else str(ext["cac_score"]), description="CAC")
        cmt = w.Textarea(layout=w.Layout(width="100%"))
        sub = w.Button(description="Submit", button_style="primary", layout=w.Layout(width="100%"))
        sub.on_click(lambda _: self.submit({"lvef": {"value": lvef.value}, "cac_score": {"value": cac.value}, "comment": cmt.value}))
        items = [w.HTML(f"Extracted: LVEF {_e(ext.get('lvef'))} · CAC {_e(ext.get('cac_score'))}"), lvef, cac, w.HTML("<b>Comment</b>"), cmt, sub]
        if prop:
            acc = w.Button(description="Accept proposal", button_style="success")
            acc.on_click(lambda _: self.submit(prop["decision"]))
            items = [w.HTML(proposal_html(prop, self.view["packet"])), acc, w.HTML("or correct the values and submit:")] + items
        return items

    def submit(self, decision: dict) -> None:
        with self.S() as db:
            u, run = self._db_user_run(db)
            try:
                st = actions.submit_case_read(db, self.data_dir, run, u, self.case_id, decision)
            except (actions.ActionError, AdjudicationError, ValueError) as e:
                self.status.value = f"<span style='color:#b91c1c'>{_e(e)}</span>"
                return
        msg = "Submitted. Readers disagreed: the case goes to the lead cardiologist." if st == "disagreement" else "Submitted."
        self.refresh()
        self.next_case()
        self.status.value = f"<span style='color:#15803d'>{_e(msg)}</span>"


def progress(data_dir: str) -> None:
    """Data scientist / lead: adjudication progress, agreement and auto-label accuracy per outcome."""
    import pandas as pd
    from IPython.display import HTML, display
    S = open_store(data_dir)
    with S() as db:
        run = current(db)
        m = actions.run_metrics(db, data_dir, run)
    rows = [{"outcome": f, "final / cases": f"{x['final']}/{x['cases']}", "open": x["open"], "disagreements": x["disagreement"],
             "kappa": x["kappa"], "PPV auto-confirmed": x["ppv_auto_confirmed"], "NPV auto-rejected": x["npv_auto_rejected"],
             "lead-resolved": x["lead_resolved"], "proposal accepted": x["proposal_accept_rate"]}
            for f, x in m["families"].items() if x["cases"]]
    display(HTML(f"<b>Label run {run.id}</b> · {m['totals']['final']}/{m['totals']['cases']} cases final · "
                 f"{m['totals']['open']} open · {m['totals']['disagreement']} disagreements"))
    display(pd.DataFrame(rows).round(3))
