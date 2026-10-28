"""
The agent loop: the model picks a tool, we run it, the model reads the result and picks again,
until it answers in plain text or a deterministic fallback ends the question (see fallbacks.py).
"""
import json
import time
import uuid
from pathlib import Path

import anthropic

from agent import fallbacks
from agent.tools import QueryRejected, Tools, TOOL_DEFINITIONS

MODEL = "claude-opus-5-5"
MAX_TURNS = 10          # model calls per question; a loop that hasn't answered by then won't
MAX_REJECTIONS = 3      # fixable guardrail rejections per question before giving up
TOOLS = TOOL_DEFINITIONS + [fallbacks.CANNOT_ANSWER_TOOL]
LOG_PATH = Path(__file__).resolve().parent / "logs" / "transcript.jsonl"

SYSTEM_PROMPT = """\
You answer business questions about one e-commerce brand, {brand}, using its dbt marts in DuckDB.
You can only see this brand.

If the question asks you to change data, is outside these marts, is about another brand, or is
too vague to answer (no metric, or a period you can't resolve), call cannot_answer with the reason
instead of writing an answer.

Rules:
- Never state a number you did not get from a run_query result in this conversation. If a query
  returns no rows or NULL, say there is no data for that; do not estimate.
- Every number you give must say which brand and which date range it covers.
- The data can stop before today. Call get_data_health to find the latest date, and resolve
  relative dates against it, not the calendar: "last week" is the last complete Monday-Sunday
  week on or before the latest report_date; "last month" is the last complete calendar month.
  Say what you anchored to.
- End with a one-line data note: the latest date the answer is based on, and any failing or
  warning dbt tests on the models you used. If test status is unknown, say so.

For budget or "what should we do" questions, ground the recommendation in queried numbers
(recent spend, MER, spend at risk) and say it is a recommendation, not a forecast.
Keep answers short: lead with the answer, then the date range, then the data note.\
"""

# Business definitions, kept separate so the evals can measure what they are worth (--no-definitions).
DEFINITIONS = """

Definitions (the marts already implement these; use them, don't re-derive):
- Revenue means net revenue (gross minus refunds, refunds dated to the order) unless the user
  says gross. Platform-reported revenue is never revenue.
- MER = sum(net_revenue) / sum(spend). ROAS = sum(attributed_net_revenue) / sum(spend).
  For any ratio over a period, divide the sums; never average the daily ratios.
- Revenue lost to stockouts is fct_stockout_cancellations.lost_revenue: every line of each order
  cancelled because something in it was out of stock, including in-stock items lost with it.
  mart_spend_at_risk.lost_revenue_out_of_stock only has the out-of-stock SKU's own lines, so it
  undercounts. fct_stockout_cancellations.estimated_ad_cost is an estimate that overlaps
  mart_spend_at_risk.spend_at_risk; never add the two.\
"""


def log(event: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}, default=str) + "\n")


def run_tool(funcs: dict, block) -> tuple[dict, bool]:
    """Run one tool call. Errors are returned to the model, not raised: it can read them and retry."""
    try:
        return funcs[block.name](**block.input), False
    except QueryRejected as e:
        return {"error": f"query rejected: {e}", "rejected": e.kind}, True
    except Exception as e:  # a bad column name, a type error in SQL, ...
        return {"error": f"{type(e).__name__}: {e}"}, True


def ask(question: str, brand: str, client=None, verbose: bool = False, definitions: bool = True) -> dict:
    tools = Tools(brand)
    funcs = tools.functions()
    session = uuid.uuid4().hex[:8]
    calls = []
    log({"session": session, "brand": brand, "event": "question", "question": question})

    def fallback(reason: str, turns: int, detail: str | None = None) -> dict:
        """End the question with fixed text from fallbacks.py, never with text the model wrote."""
        fb = fallbacks.respond(reason, tools, question, max_turns=MAX_TURNS, max_rejections=MAX_REJECTIONS)
        report = fb["report"]
        log({"session": session, "event": "fallback", "turns": turns, "reason": reason, "detail": detail,
             "report": report and report["type"], "report_sql": report and report["executed_sql"]})
        if verbose:
            print(f"  [fallback] {reason}{': ' + detail if detail else ''}")
        return {"answer": fb["text"], "turns": turns, "tool_calls": calls,
                "fallback": {"reason": reason, "detail": detail, "report": report and report["type"]}}

    # Checked before any model call: a question naming another tenant never reaches the model.
    if other := fallbacks.other_tenant_in(question, brand):
        return fallback("other_tenant", 0, f"question names {other}")

    client = client or anthropic.Anthropic()
    # The conversation is just this list. The API is stateless: every turn we resend all of it.
    messages = [{"role": "user", "content": question}]
    rejections = []

    for turn in range(1, MAX_TURNS + 1):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT.format(brand=brand) + (DEFINITIONS if definitions else ""),
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},    # turn N+1 resends turn N's prefix; cache it
            # Model fallback, not a deterministic one: if this model declines, the API retries the
            # same request on another Claude model. A refusal that still reaches us is handled below.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # Append the whole response, not just its text: it holds the tool_use blocks (whose ids the
        # tool_results must reference) and any thinking blocks the model needs to keep its reasoning.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":       # declined even after the model fallback
            return fallback("model_declined", turn)
        if resp.stop_reason == "max_tokens":    # a cut-off answer is not shown
            return fallback("answer_incomplete", turn)
        if resp.stop_reason != "tool_use":
            answer = "".join(b.text for b in resp.content if b.type == "text")
            log({"session": session, "event": "answer", "turns": turn,
                 "stop_reason": resp.stop_reason, "answer": answer})
            return {"answer": answer, "turns": turn, "tool_calls": calls, "fallback": None}

        # The model may ask for several tools in one turn. Run them all and return every result in
        # ONE user message, each tagged with the tool_use id it answers.
        results, stop = [], None
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "cannot_answer":   # the model picks the reason; the words are fixed
                calls.append({"tool": block.name, "input": block.input, "error": False})
                log({"session": session, "event": "tool_call", "turn": turn, "tool": block.name,
                     "input": block.input, "is_error": False})
                stop = stop or (block.input["reason"], None)
                continue
            out, is_error = run_tool(funcs, block)
            calls.append({"tool": block.name, "input": block.input, "error": is_error})
            log({"session": session, "event": "tool_call", "turn": turn, "tool": block.name,
                 "input": block.input, "is_error": is_error,
                 **({"executed_sql": out.get("executed_sql"), "row_count": out.get("row_count")}
                    if block.name == "run_query" else {}),
                 **({"error": out["error"]} if is_error else {})})
            if verbose:
                detail = block.input.get("sql", "") if block.name == "run_query" else ""
                print(f"  [turn {turn}] {block.name} {detail}{'  -> ERROR ' + out['error'] if is_error else ''}")
            if out.get("rejected") == "unsafe":
                stop = ("unsafe_query", out["error"])
            elif out.get("rejected"):
                rejections.append(out["rejected"])
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(out, default=str), "is_error": is_error})
        if stop:
            return fallback(stop[0], turn, stop[1])
        if len(rejections) >= MAX_REJECTIONS:
            reason = "row_limit" if rejections[-1] == "row_limit" else "query_rejected"
            return fallback(reason, turn, ", ".join(rejections))
        messages.append({"role": "user", "content": results})

    return fallback("turn_limit", MAX_TURNS)
