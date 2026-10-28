"""
The agent's guardrails, loop and deterministic fallbacks, without calling the API.

The guardrail tests read the built warehouse, so they need python run_pipeline.py --demo first.
The loop tests replace the Anthropic client with a scripted one, so they need no API key.
"""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import config
from agent import fallbacks, loop
from agent.tools import MAX_ROWS, QueryRejected, Tools

pytestmark = pytest.mark.skipif(
    not config.WAREHOUSE_PATH.exists(),
    reason=f"no warehouse at {config.WAREHOUSE_PATH}; run: python run_pipeline.py --demo",
)


@pytest.fixture(scope="module")
def tools():
    return Tools("acme_apparel")


@pytest.mark.parametrize("sql", [
    "DROP TABLE acme_apparel_marts.fct_orders",
    "INSERT INTO fct_orders SELECT * FROM fct_orders",
    "CREATE TABLE x AS SELECT 1",
    "SELECT 1; DELETE FROM fct_orders",
    "COPY (SELECT 1) TO 'out.csv'",
    "ATTACH 'other.db'",
    "PRAGMA database_list",
    "SET threads = 1",
])
def test_only_a_single_select_is_allowed(tools, sql):
    with pytest.raises(QueryRejected):
        tools.check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM bloom_skin_marts.fct_orders",
    "SELECT * FROM fct_orders WHERE order_id IN (SELECT order_id FROM bloom_skin_marts.fct_orders)",
    "SELECT * FROM raw_acme_apparel.orders",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM stg_orders",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM duckdb_tables()",
])
def test_queries_cannot_leave_the_tenant_marts(tools, sql):
    with pytest.raises(QueryRejected):
        tools.check_sql(sql)


def test_the_connection_blocks_what_the_parser_lets_through(tools):
    # getenv parses as an ordinary scalar function, so the SQL check allows it. The connection is
    # the second layer: read-only, no file or environment access, configuration locked.
    tools.check_sql("SELECT getenv('HOME')")
    with pytest.raises(Exception):
        tools.con.execute("SELECT getenv('HOME')").fetchall()
    with pytest.raises(Exception):
        tools.con.execute("SET enable_external_access = true")


def test_unqualified_tables_and_ctes_resolve_to_the_tenant(tools):
    sql = tools.check_sql("WITH w AS (SELECT * FROM fct_orders) SELECT count(*) FROM w")
    assert "acme_apparel_marts.fct_orders" in sql


def test_the_row_cap_returns_all_rows_or_none(tools):
    # Partial results are never handed back, so nothing can be answered from part of the data.
    with pytest.raises(QueryRejected) as rejected:
        tools.run_query("SELECT * FROM fct_inventory_daily")
    assert rejected.value.kind == "row_limit"
    assert tools.run_query(f"SELECT * FROM fct_inventory_daily LIMIT {MAX_ROWS}")["row_count"] == MAX_ROWS


@pytest.mark.parametrize("sql, kind", [
    ("DELETE FROM fct_orders", "unsafe"),
    ("SELECT 1; SELECT 2", "unsafe"),
    ("SELECT * FROM bloom_skin_marts.fct_orders", "unsafe"),
    ("SELECT * FROM read_csv('/etc/passwd')", "unsafe"),
    ("SELECT * FROM stg_orders", "invalid"),
    ("SELEC 1", "invalid"),
])
def test_rejections_say_whether_they_are_unsafe_or_fixable(tools, sql, kind):
    with pytest.raises(QueryRejected) as rejected:
        tools.check_sql(sql)
    assert rejected.value.kind == kind


def test_data_health_never_reports_another_brands_tests():
    for brand in config.BRANDS:
        health = Tools(brand).get_data_health()
        assert health["brand"] == brand
        assert health["dbt_tests"]["status"] in {"known", "unknown"}
    # run_results.json holds one brand's build; at most one brand can see known results.
    known = [b for b in config.BRANDS if Tools(b).get_data_health()["dbt_tests"]["status"] == "known"]
    assert len(known) <= 1


# --- the loop, with a scripted client ---------------------------------------------------------

def tool_use(id_, name, **input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def text(t):
    return SimpleNamespace(type="text", text=t)


class ScriptedClient:
    """Returns canned responses in order and records every request it was sent."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        if not self.responses:
            return SimpleNamespace(stop_reason="tool_use", content=[tool_use("again", "list_tables")])
        stop_reason, content = self.responses.pop(0)
        return SimpleNamespace(stop_reason=stop_reason, content=content)


@pytest.fixture(autouse=True)
def tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "LOG_PATH", tmp_path / "transcript.jsonl")
    return tmp_path / "transcript.jsonl"


def test_loop_runs_tools_and_feeds_results_back_by_id(tmp_log):
    client = ScriptedClient(
        ("tool_use", [tool_use("t1", "run_query", sql="SELECT sum(spend) AS s FROM mart_marketing_daily")]),
        ("end_turn", [text("Spend was $X.")]),
    )
    result = loop.ask("spend?", "acme_apparel", client=client)

    assert result["answer"] == "Spend was $X."
    assert result["turns"] == 2
    second_request = client.requests[1]["messages"]
    tool_results = second_request[-1]["content"]
    assert tool_results[0]["tool_use_id"] == "t1"
    assert tool_results[0]["is_error"] is False
    assert "executed_sql" in tmp_log.read_text()


def test_a_fixable_rejection_goes_back_to_the_model_as_an_error(tmp_log):
    client = ScriptedClient(
        ("tool_use", [tool_use("t1", "run_query", sql="SELECT * FROM stg_orders")]),
        ("end_turn", [text("Fixed it.")]),
    )
    result = loop.ask("orders?", "acme_apparel", client=client)

    returned = client.requests[1]["messages"][-1]["content"][0]
    assert returned["is_error"] is True
    assert "query rejected" in returned["content"]
    assert result["answer"] == "Fixed it."
    assert result["fallback"] is None


def test_parallel_tool_calls_come_back_in_one_message():
    client = ScriptedClient(
        ("tool_use", [tool_use("a", "list_tables"), tool_use("b", "get_data_health")]),
        ("end_turn", [text("done")]),
    )
    loop.ask("q", "acme_apparel", client=client)
    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_the_loop_stops_at_max_turns():
    client = ScriptedClient()   # asks for a tool forever
    result = loop.ask("q", "acme_apparel", client=client)
    assert result["turns"] == loop.MAX_TURNS
    assert len(client.requests) == loop.MAX_TURNS
    assert result["fallback"]["reason"] == "turn_limit"


# --- deterministic fallbacks -------------------------------------------------------------------
# Each trigger must end the question with fixed text from agent/fallbacks.py, never the model's words.

class NoCallClient:
    """Fails the test if the model is called at all."""
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        raise AssertionError("the model should not have been called")


def fixed_text(reason, brand="acme_apparel"):
    first_day, last_day = fallbacks.data_range(Tools(brand))
    return fallbacks.EXPLANATIONS[reason].format(
        brand=brand, first_day=first_day, last_day=last_day,
        max_turns=loop.MAX_TURNS, max_rejections=loop.MAX_REJECTIONS)


@pytest.mark.parametrize("question", [
    "What was bloom_skin's revenue in August 2026?",
    "How much did Bloom Skin spend on ads?",
    "compare us with BLOOM-SKIN",
])
def test_a_question_about_another_tenant_never_reaches_the_model(question):
    result = loop.ask(question, "acme_apparel", client=NoCallClient())
    assert result["fallback"]["reason"] == "other_tenant"
    assert result["turns"] == 0
    # The reply doesn't confirm the other brand exists: that would itself leak across tenants.
    assert "bloom" not in result["answer"].lower()
    assert result["answer"] == fixed_text("other_tenant")


def test_an_unsafe_query_ends_the_question_at_once():
    client = ScriptedClient(
        ("tool_use", [text("Deleting now."), tool_use("t1", "run_query", sql="DELETE FROM fct_orders")]),
        ("end_turn", [text("Done, I deleted them.")]),
    )
    result = loop.ask("Delete the refunded orders", "acme_apparel", client=client)
    assert len(client.requests) == 1
    assert result["fallback"]["reason"] == "unsafe_query"
    assert result["answer"] == fixed_text("unsafe_query")


def test_repeated_fixable_rejections_fall_back():
    bad = ("tool_use", [tool_use("t", "run_query", sql="SELECT * FROM stg_orders")])
    client = ScriptedClient(bad, bad, bad, ("end_turn", [text("a guess")]))
    result = loop.ask("How many orders?", "acme_apparel", client=client)
    assert len(client.requests) == loop.MAX_REJECTIONS
    assert result["fallback"]["reason"] == "query_rejected"
    assert result["answer"] == fixed_text("query_rejected")


def test_hitting_the_row_cap_repeatedly_falls_back_with_a_report():
    big = ("tool_use", [tool_use("t", "run_query", sql="SELECT * FROM mart_spend_at_risk, fct_inventory_daily")])
    client = ScriptedClient(big, big, big)
    result = loop.ask("Which SKUs are we spending on that are about to stock out?", "bloom_skin", client=client)
    assert result["fallback"]["reason"] == "row_limit"
    assert result["fallback"]["report"] == "spend_at_risk"
    assert result["answer"].startswith(fixed_text("row_limit", "bloom_skin"))
    assert fallbacks.REPORT_LABEL in result["answer"]
    assert "SERUM-VITC-30" in result["answer"]


def test_a_row_cap_hit_the_model_then_fixes_is_not_a_fallback():
    client = ScriptedClient(
        ("tool_use", [tool_use("t1", "run_query", sql="SELECT * FROM fct_inventory_daily")]),
        ("tool_use", [tool_use("t2", "run_query", sql="SELECT count(*) AS n FROM fct_inventory_daily")]),
        ("end_turn", [text("There are N rows.")]),
    )
    result = loop.ask("How many inventory rows?", "acme_apparel", client=client)
    assert result["fallback"] is None


def test_the_turn_limit_falls_back_with_a_revenue_report():
    result = loop.ask("What was revenue last week?", "acme_apparel", client=ScriptedClient())
    assert result["fallback"] == {"reason": "turn_limit", "detail": None, "report": "revenue_for_period"}
    assert result["answer"].startswith(fixed_text("turn_limit"))
    assert "Fallback report, not an agent answer" in result["answer"]
    assert "$19,995.03" in result["answer"]


def test_a_refusal_after_the_model_fallback_is_a_deterministic_fallback():
    client = ScriptedClient(("refusal", [text("I'd rather guess: about $20k.")]))
    result = loop.ask("What was revenue last week?", "acme_apparel", client=client)
    assert result["fallback"]["reason"] == "model_declined"
    assert "$20k" not in result["answer"]


def test_a_cut_off_answer_is_not_shown():
    client = ScriptedClient(("max_tokens", [text("Revenue was $19,99")]))
    result = loop.ask("What was revenue last week?", "acme_apparel", client=client)
    assert result["fallback"]["reason"] == "answer_incomplete"
    assert "$19,99" not in result["answer"].split("\n\n")[0]


@pytest.mark.parametrize("reason", ["out_of_scope", "needs_metric", "needs_date_range"])
def test_cannot_answer_returns_fixed_text_not_the_models(reason):
    client = ScriptedClient(("tool_use", [text("My own wording."), tool_use("c", "cannot_answer", reason=reason)]))
    result = loop.ask("Something I can't answer", "acme_apparel", client=client)
    assert result["fallback"]["reason"] == reason
    assert result["answer"] == fixed_text(reason)
    assert "My own wording" not in result["answer"]


def test_the_clarifying_question_names_the_data_range():
    assert "2026-07-01 to 2026-08-29" in fixed_text("needs_date_range")


def test_every_fallback_is_logged_with_its_reason(tmp_log):
    loop.ask("What was revenue last week?", "acme_apparel", client=ScriptedClient())
    events = [json.loads(line) for line in tmp_log.read_text().splitlines()]
    fallback_events = [e for e in events if e["event"] == "fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0]["reason"] == "turn_limit"
    assert fallback_events[0]["report"] == "revenue_for_period"
    assert "acme_apparel_marts.mart_marketing_daily" in fallback_events[0]["report_sql"]


def test_cannot_answer_reasons_all_have_fixed_text():
    reasons = fallbacks.CANNOT_ANSWER_TOOL["input_schema"]["properties"]["reason"]["enum"]
    assert set(reasons) <= set(fallbacks.EXPLANATIONS)


# --- the fixed reports --------------------------------------------------------------------------

def test_the_revenue_report_matches_an_independent_query(tools):
    report = fallbacks.build_report(tools, "What was revenue last week?")
    assert report["params"] == ["2026-08-17", "2026-08-23"]
    net_revenue = dict(zip(report["columns"], report["rows"][0]))["net_revenue"]
    from_orders = tools.con.execute(
        "SELECT sum(net_revenue) FROM acme_apparel_marts.fct_orders "
        "WHERE order_date BETWEEN '2026-08-17' AND '2026-08-23'").fetchone()[0]
    assert net_revenue == pytest.approx(float(from_orders))


def test_the_revenue_report_says_when_there_is_no_data(tools):
    report = fallbacks.build_report(tools, "revenue in March 2026")
    assert "No data for 2026-03-01 to 2026-03-31" in fallbacks.render_report(report, "acme_apparel")


def test_the_spend_at_risk_report_is_scoped_to_the_tenant():
    report = fallbacks.build_report(Tools("bloom_skin"), "what spend is at risk of a stockout?")
    assert "bloom_skin_marts.mart_spend_at_risk" in report["executed_sql"]
    assert {row[2] for row in report["rows"]} == {"SERUM-VITC-30"}


@pytest.mark.parametrize("question", [
    "How much revenue did we lose to stockouts?",   # lost revenue isn't a fixed report
    "What was ROAS by campaign?",
    "revenue in August",                            # no year, no period: no report
])
def test_no_report_without_a_known_type_and_period(tools, question):
    assert fallbacks.build_report(tools, question) is None


@pytest.mark.parametrize("question, expected", [
    ("last week", (date(2026, 8, 17), date(2026, 8, 23))),
    ("last month", (date(2026, 7, 1), date(2026, 7, 31))),
    ("in July 2026", (date(2026, 7, 1), date(2026, 7, 31))),
    ("from 2026-08-01 to 2026-08-15", (date(2026, 8, 1), date(2026, 8, 15))),
    ("may we see revenue", None),
])
def test_periods_resolve_against_the_latest_loaded_date(question, expected):
    assert fallbacks.resolve_period(question, date(2026, 8, 29)) == expected


# --- the eval grader's fallback rules -----------------------------------------------------------

def eval_case(case_id):
    from evals.questions import CASES
    return next(c for c in CASES if c["id"] == case_id)


@pytest.mark.parametrize("case_id", ["other_tenant", "other_tenant_name_variant"])
def test_the_other_tenant_evals_pass_without_calling_the_model(tools, case_id):
    from evals.run_evals import grade
    case = eval_case(case_id)
    result = loop.ask(case["question"], case["brand"], client=NoCallClient())
    assert grade(case, result, tools.con) == []


def test_the_grader_fails_a_fallback_on_an_answerable_question(tools):
    from evals.run_evals import grade
    result = {"answer": "x", "fallback": {"reason": "turn_limit", "detail": None, "report": None}}
    assert any("fell back" in f for f in grade(eval_case("spend_july"), result, tools.con))


def test_the_grader_fails_a_model_answer_where_a_fallback_was_expected(tools):
    from evals.run_evals import grade
    result = {"answer": "The weather will be sunny.", "fallback": None}
    assert any("expected a fallback" in f for f in grade(eval_case("out_of_scope"), result, tools.con))
