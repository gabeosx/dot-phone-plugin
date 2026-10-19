"""Readable review of the same exact scope enforced by the approval service."""

import html
import json
from decimal import Decimal

STYLE = """
body{font:17px/1.5 system-ui,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1.25rem;color:#17212b;background:#f7f8fa}
h1{font-size:1.7rem}h2{font-size:1.15rem}section,form,details{background:white;border:1px solid #d7dde4;border-radius:12px;padding:1rem;margin:1rem 0}
dl{display:grid;grid-template-columns:9rem 1fr;gap:.5rem}dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}
summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.85rem}
label{display:block}input,button{font:inherit;padding:.6rem;margin-top:.5rem;max-width:100%;box-sizing:border-box}
input{display:block;width:100%}button{background:#17212b;color:white;border:0;border-radius:6px;cursor:pointer}
@media(max-width:480px){body{margin:1rem auto}dl{grid-template-columns:1fr;gap:.2rem}dd{margin-bottom:.5rem}}
"""


def render_call_approval(call):
    """All brief fields are untrusted text, including the details JSON."""
    brief = call["brief"]

    def escape(value):
        return html.escape(str(value))

    def money(value):
        amount = Decimal(str(value))
        return f"${amount:.{max(2, -amount.as_tuple().exponent)}f}"

    evidence = call["evidence_policy"]
    rows = {
        "Call": brief["destination"],
        "Spending limit": f"{money(brief['budget_usd'])} maximum",
        "Estimate": f"{money(call['estimate_usd'])} (final billing may arrive later)",
        "Time limit": f"{brief['max_seconds']} seconds; one call, no automatic redial",
        "Valid until": brief["deadline"],
        "Recording": "On" if evidence["recording"] else "Off",
        "Transcript": "On" if evidence["transcript"] else "Off",
    }
    return (
        "<section><h2>Purpose</h2><p>"
        + escape(brief["purpose"])
        + "</p><dl>"
        + "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in rows.items())
        + "</dl></section><details><summary>Review facts, permissions and full scope</summary><pre>"
        + escape(json.dumps(brief, indent=2, ensure_ascii=False))
        + "</pre></details>"
    )
