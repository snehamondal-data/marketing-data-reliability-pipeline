"""
Deterministic fallbacks: what the agent says when it can't answer safely.

Not to be confused with the model fallback in loop.py (`fallbacks="default"`), where the API retries
a declined request on another Claude model. That is still the model answering. Everything here is
the opposite: when a trigger fires, the user gets a fixed explanation or clarifying question written
in this file, never text the model wrote. For a few known question types, a fixed, tested query
also runs and its result is attached, labelled as a fallback report rather than an agent answer.

The model can choose a reason (through the cannot_answer tool); it never chooses the words.
"""
import calendar
import re
from datetime import date, timedelta

import config

REPORT_LABEL = "Fallback report, not an agent answer"

# reason -> fixed text. {brand}, {first_day}, {last_day}, {max_turns}, {max_rejections} are filled in.
EXPLANATIONS = {
    # Triggers the code detects.
    "other_tenant": (
        "This session can only see {brand}'s data, so I can't answer questions about other brands."
    ),
    "unsafe_query": (
        "I can't do that. This agent can only read {brand}'s reporting tables: it can't change data, "
        "or read other brands, raw tables or files. The blocked query is in the log."
    ),
    "query_rejected": (
        "I couldn't write a valid query for this in {max_rejections} attempts, so I'm not giving an "
        "answer. Each attempt is in the log."
    ),
    "row_limit": (
        "Answering this needs more rows than I'm allowed to read at once, and I couldn't summarise it "
        "within the limit. Try a narrower question: a shorter date range, one campaign or one product."
    ),
    "turn_limit": (
        "I stopped after {max_turns} steps without reaching an answer I could check, so I'm not "
        "giving one."
    ),
    "model_declined": (
        "The model declined to answer this question, so there is no answer."
    ),
    "answer_incomplete": (
        "The answer was cut off before it finished, so it isn't shown."
    ),
    # Reasons the model can give through the cannot_answer tool.
    "out_of_scope": (
        "That's outside what I can answer. I answer questions about {brand}'s sales, ad spend, "
        "campaigns, inventory and stockouts, from its reporting tables."
    ),
    "needs_date_range": (
        "Which period do you mean? {brand}'s data covers {first_day} to {last_day}. For example: "
        "'last week', 'last month', 'August 2026', or two dates like 2026-08-01 to 2026-08-15."
    ),
    "needs_metric": (
        "What would you like to know? For example: revenue, ad spend, MER or ROAS, orders and AOV, "
        "or which products are at risk of stocking out, for a period like last week or August 2026."
    ),
}
MODEL_REASONS = ["out_of_scope", "other_tenant", "needs_date_range", "needs_metric"]
# No report for these: the question isn't one the reports answer, or isn't about this brand.
NO_REPORT = {"other_tenant", "out_of_scope", "needs_metric", "unsafe_query"}

CANNOT_ANSWER_TOOL = {
    "name": "cannot_answer",
    "description": (
        "Call this instead of answering when the question asks you to change data, is outside these "
        "marts, is about another brand, or is too vague to answer (no metric, or a period you can't "
        "resolve). The user gets a fixed explanation or clarifying question for the reason you pick; "
        "don't write your own. Don't use it when a query simply returns no data: say so instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string", "enum": MODEL_REASONS}},
        "required": ["reason"],
        "additionalProperties": False,
    },
    "strict": True,
}

# --- triggers the code checks itself ------------------------------------------------------------


def other_tenant_in(question: str, brand: str) -> str | None:
    """Another brand named in the question, matching 'bloom_skin', 'Bloom Skin' or 'bloom-skin'."""
    for other in config.BRANDS:
        if other == brand:
            continue
        pattern = r"\b" + r"[\s_-]*".join(map(re.escape, other.split("_"))) + r"\b"
        if re.search(pattern, question, re.IGNORECASE):
            return other
    return None


# --- fixed reports ------------------------------------------------------------------------------

REPORT_SQL = {
    "revenue_for_period": """
        SELECT min(report_date) AS first_day, max(report_date) AS last_day, count(*) AS days,
               sum(net_revenue) AS net_revenue, sum(gross_revenue) AS gross_revenue,
               sum(refunded_amount) AS refunds, sum(orders) AS orders, sum(spend) AS spend,
               sum(net_revenue) / nullif(sum(spend), 0) AS mer
        FROM mart_marketing_daily
        WHERE report_date BETWEEN ? AND ?""",
    "spend_at_risk": """
        SELECT report_date, campaign_id, sku, risk_reason, spend_at_risk, on_hand, days_of_cover
        FROM mart_spend_at_risk
        WHERE report_date = (SELECT max(report_date) FROM mart_spend_at_risk)
        ORDER BY spend_at_risk DESC""",
}
DATA_RANGE_SQL = "SELECT min(report_date), max(report_date) FROM mart_marketing_daily"


def report_type(question: str) -> str | None:
    q = question.lower()
    if re.search(r"\b(lost|lose|losing)\b", q):        # lost revenue isn't covered by a fixed report
        return None
    if (re.search(r"stock ?outs?|out of stock|run(ning)? out|at risk", q)
            and re.search(r"spend|spending|\bads?\b|campaigns?", q)):
        return "spend_at_risk"
    if re.search(r"\b(revenue|sales)\b", q):
        return "revenue_for_period"
    return None


def resolve_period(question: str, latest: date) -> tuple[date, date] | None:
    """The same rules the system prompt gives the model, anchored to the latest loaded date."""
    q = question.lower()
    if m := re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:to|through|and|–|-)\s*(\d{4}-\d{2}-\d{2})", q):
        return date.fromisoformat(m[1]), date.fromisoformat(m[2])
    if "last week" in q:        # last complete Monday-Sunday week on or before the latest date
        end = latest - timedelta(days=(latest.weekday() + 1) % 7)
        return end - timedelta(days=6), end
    if "last month" in q:       # last complete calendar month
        month_end = latest if (latest + timedelta(days=1)).day == 1 else latest.replace(day=1) - timedelta(days=1)
        return month_end.replace(day=1), month_end
    months = "|".join(m.lower() for m in calendar.month_name if m)
    if m := re.search(rf"\b({months})\s+(\d{{4}})\b", q):   # a year is required: "may" is also a verb
        month, year = list(calendar.month_name).index(m[1].capitalize()), int(m[2])
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    return None


def data_range(tools) -> tuple[date, date]:
    return tools.con.execute(tools.check_sql(DATA_RANGE_SQL)).fetchone()


def build_report(tools, question: str) -> dict | None:
    kind = report_type(question)
    if kind is None:
        return None
    first_day, last_day = data_range(tools)
    params = []
    if kind == "revenue_for_period":
        period = resolve_period(question, last_day)
        if period is None:
            return None
        params = [period[0].isoformat(), period[1].isoformat()]
    # The same guardrail and connection as the model's queries: tenant-scoped, read-only, capped.
    result = tools.run_query(REPORT_SQL[kind], params=params)
    return {"type": kind, "params": params, "executed_sql": result["executed_sql"],
            "columns": result["columns"], "rows": result["rows"], "data_through": last_day.isoformat()}


def _money(value) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def render_report(report: dict, brand: str) -> str:
    rows = [dict(zip(report["columns"], row)) for row in report["rows"]]
    head = f"{REPORT_LABEL}. Fixed query `{report['type']}`, {brand}, data through {report['data_through']}."
    if report["type"] == "revenue_for_period":
        start, end = report["params"]
        r = rows[0]
        if not r["days"]:
            return f"{head}\nNo data for {start} to {end}."
        mer = "n/a" if r["mer"] is None else f"{r['mer']:.2f}"
        return (f"{head}\nPeriod asked: {start} to {end}; days with data: {r['first_day']} to "
                f"{r['last_day']} ({r['days']} days).\n"
                f"Net revenue {_money(r['net_revenue'])} (gross {_money(r['gross_revenue'])} minus "
                f"refunds {_money(r['refunds'])}), {r['orders']} orders, ad spend "
                f"{_money(r['spend'])}, MER {mer}.")
    if not rows:
        return f"{head}\nNo spend at risk recorded."
    lines = [f"{head}\nLatest day with spend at risk: {rows[0]['report_date']}."]
    for r in rows:
        cover = "out of stock" if r["on_hand"] == 0 else f"{r['days_of_cover']:.1f} days of cover"
        lines.append(f"- {r['sku']} (campaign {r['campaign_id']}): {_money(r['spend_at_risk'])} "
                     f"at risk, {cover} [{r['risk_reason']}]")
    return "\n".join(lines)


def respond(reason: str, tools, question: str, **limits) -> dict:
    """The fixed text for a reason, plus a fixed report when the question has one."""
    first_day, last_day = data_range(tools)
    text = EXPLANATIONS[reason].format(brand=tools.brand, first_day=first_day, last_day=last_day, **limits)
    report = None if reason in NO_REPORT else build_report(tools, question)
    if report:
        text += "\n\n" + render_report(report, tools.brand)
    return {"reason": reason, "text": text, "report": report}
