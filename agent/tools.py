"""
The three tools the model can call, and the guardrails around them.

Every tool is scoped to one tenant at construction time. The model never chooses the tenant:
it gets a Tools object that can only see <brand>_marts.
"""
import json
import threading
import time
from datetime import date, datetime
from decimal import Decimal

import duckdb
import sqlglot
from sqlglot import exp

import config

MAX_ROWS = 200            # rows handed back to the model per query
QUERY_TIMEOUT_SECONDS = 10
RUN_RESULTS = config.DBT_PROJECT_DIR / "target" / "run_results.json"
MANIFEST = config.DBT_PROJECT_DIR / "target" / "manifest.json"
DATE_COLUMNS = ["report_date", "snapshot_date", "order_date", "alert_date", "last_spend_date"]

# The tool definitions are the contract with the model: names, inputs, and what each one promises.
TOOL_DEFINITIONS = [
    {
        "name": "list_tables",
        "description": (
            "List the tables you may query for this brand: each table's dbt description and its "
            "columns with types. Call this before writing SQL; do not guess table or column names."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "run_query",
        "description": (
            "Run one read-only DuckDB SELECT against this brand's marts and get the rows back as JSON. "
            "Only SELECT (WITH ... SELECT and UNION are fine). Only tables returned by list_tables, "
            f"qualified or not. If more than {MAX_ROWS} rows match, no rows come back and the query is "
            f"rejected: aggregate in SQL or add a LIMIT. Queries are cancelled after {QUERY_TIMEOUT_SECONDS}s. "
            "A rejected query returns an error explaining why; fix it and try again. Statements that "
            "change data, or read outside this brand's marts, end the question with no answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_data_health",
        "description": (
            "Freshness and data-quality status for this brand: the latest date in each table, when "
            "ingestion last saw a record, and the latest dbt test results (failures and warnings, and "
            "which model each test covers). Use it to anchor relative dates like 'last week' and to "
            "caveat numbers that come from a model with failing or warning tests."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
]


class QueryRejected(Exception):
    """The SQL broke a guardrail.

    kind is "unsafe" (not a read, or outside this tenant's marts: the loop stops), or a fixable
    mistake the model may correct: "invalid", "row_limit" or "timeout".
    """
    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def connect(warehouse_path=config.WAREHOUSE_PATH):
    """Read-only, no file or environment access, configuration locked. DuckDB applies settings per
    database file in a process, so every connection to the warehouse goes through here."""
    return duckdb.connect(str(warehouse_path), read_only=True,
                          config={"enable_external_access": False, "lock_configuration": True})


class Tools:
    def __init__(self, brand: str, warehouse_path=config.WAREHOUSE_PATH):
        if brand not in config.BRANDS:
            raise ValueError(f"unknown brand {brand!r}; expected one of {sorted(config.BRANDS)}")
        self.brand = brand
        self.schema = f"{brand}_marts"
        # Defence in depth behind the SQL check: a read-only connection that can't touch files.
        self.con = connect(warehouse_path)
        self.tables = {
            name for (name,) in self.con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = ?", [self.schema]
            ).fetchall()
        }
        if not self.tables:
            raise ValueError(f"no tables in {self.schema}; has the pipeline been run?")

    def functions(self) -> dict:
        return {
            "list_tables": self.list_tables,
            "run_query": self.run_query,
            "get_data_health": self.get_data_health,
        }

    # --- list_tables -------------------------------------------------------------------------

    def list_tables(self) -> dict:
        docs = _model_docs()
        rows = self.con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? ORDER BY table_name, ordinal_position", [self.schema]
        ).fetchall()
        tables = {}
        for table, column, dtype in rows:
            doc = docs.get(table, {})
            entry = tables.setdefault(table, {"description": doc.get("description", ""), "columns": []})
            col = {"name": column, "type": dtype}
            if doc.get("columns", {}).get(column):
                col["description"] = doc["columns"][column]
            entry["columns"].append(col)
        return {"schema": self.schema, "tables": tables}

    # --- run_query ---------------------------------------------------------------------------

    def check_sql(self, sql: str) -> str:
        """Return the SQL to execute (with a LIMIT), or raise QueryRejected."""
        try:
            statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
        except sqlglot.errors.ParseError as e:
            raise QueryRejected(f"could not parse SQL: {e}") from None
        if len(statements) > 1:
            raise QueryRejected("send exactly one statement", "unsafe")
        if not statements:
            raise QueryRejected("empty query")
        tree = statements[0]
        if not isinstance(tree, exp.Query):
            raise QueryRejected(f"only SELECT is allowed, got {tree.key.upper()}", "unsafe")
        if any(tree.find_all(exp.DDL, exp.DML, exp.Command, exp.Pragma, exp.Set, exp.Use)):
            raise QueryRejected("only SELECT is allowed", "unsafe")

        cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                raise QueryRejected(f"table functions are not allowed: {table.this.sql('duckdb')}", "unsafe")
            if table.catalog or (table.db and table.db != self.schema):
                raise QueryRejected(f"{table.sql('duckdb')} is outside {self.schema}", "unsafe")
            if not table.db and table.name in cte_names:
                continue
            if table.name not in self.tables:
                raise QueryRejected(f"unknown table {table.name!r}; call list_tables")
            table.set("db", exp.to_identifier(self.schema))   # resolve unqualified names here, not via search_path

        # Cap the rows read. MAX_ROWS + 1 lets run_query tell that more rows matched.
        existing = tree.args.get("limit")
        cap = MAX_ROWS + 1
        if existing is None or not (isinstance(existing.expression, exp.Literal)
                                    and int(existing.expression.this) <= cap):
            tree = tree.limit(cap)
        return tree.sql("duckdb")

    def run_query(self, sql: str, params: list | None = None) -> dict:
        """params is only for the fixed fallback reports; the model's tool schema has sql alone."""
        executed = self.check_sql(sql)
        timer = threading.Timer(QUERY_TIMEOUT_SECONDS, self.con.interrupt)
        timer.start()
        started = time.monotonic()
        try:
            cursor = self.con.execute(executed, params or [])
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
        except duckdb.InterruptException:
            raise QueryRejected(f"query cancelled after {QUERY_TIMEOUT_SECONDS}s; aggregate or filter more",
                                "timeout") from None
        finally:
            timer.cancel()
        # All or nothing: a partial result could be answered from as if it were the whole.
        if len(rows) > MAX_ROWS:
            raise QueryRejected(f"more than {MAX_ROWS} rows match, so none were returned; aggregate "
                                "in SQL or add a LIMIT", "row_limit")
        return {
            "executed_sql": executed,
            "columns": columns,
            "rows": [[_jsonable(v) for v in row] for row in rows],
            "row_count": len(rows),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    # --- get_data_health ---------------------------------------------------------------------

    def get_data_health(self) -> dict:
        latest = {}
        for table in sorted(self.tables):
            cols = {c for (c,) in self.con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ?",
                [self.schema, table]).fetchall()}
            date_col = next((c for c in DATE_COLUMNS if c in cols), None)
            if date_col:
                value = self.con.execute(f'SELECT max("{date_col}") FROM {self.schema}."{table}"').fetchone()[0]
                latest[table] = {"column": date_col, "max": _jsonable(value)}

        watermarks = {}
        for path in sorted(config.CHECKPOINT_DIR.glob(f"{self.brand}__*.json")):
            watermarks[path.stem.split("__", 1)[1]] = json.loads(path.read_text()).get("updated_at")

        return {
            "brand": self.brand,
            "latest_date_per_table": latest,
            "ingestion_watermarks": watermarks,
            "dbt_tests": self._dbt_test_status(),
        }

    def _dbt_test_status(self) -> dict:
        if not RUN_RESULTS.exists():
            return {"status": "unknown", "reason": "no dbt run_results.json found"}
        run = json.loads(RUN_RESULTS.read_text())
        run_brand = (run.get("args", {}).get("vars") or {}).get("brand")
        # run_results.json only holds the most recent dbt invocation. If that was another brand,
        # its results say nothing about this one, and reporting them would leak across tenants.
        if run_brand != self.brand:
            return {"status": "unknown",
                    "reason": f"the latest dbt run was for a different brand; no test results for {self.brand}"}

        nodes = json.loads(MANIFEST.read_text())["nodes"] if MANIFEST.exists() else {}
        tests = [r for r in run["results"] if r["unique_id"].startswith("test.")]
        problems = []
        for r in tests:
            if r["status"] != "pass":
                node = nodes.get(r["unique_id"], {})
                covers = [d.split(".")[-1] for d in node.get("depends_on", {}).get("nodes", [])
                          if d.startswith("model.")]
                problems.append({"test": node.get("name", r["unique_id"]), "status": r["status"],
                                 "failing_rows": r.get("failures"), "models": covers})
        counts = {}
        for r in tests:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"status": "known", "run_at": run["metadata"]["generated_at"],
                "counts": counts, "not_passing": problems}


def _model_docs() -> dict:
    """Model and column descriptions from the dbt manifest; documentation is brand-independent."""
    if not MANIFEST.exists():
        return {}
    docs = {}
    for node in json.loads(MANIFEST.read_text())["nodes"].values():
        if node["resource_type"] == "model":
            docs[node["name"]] = {
                "description": node.get("description", ""),
                "columns": {c: v.get("description", "") for c, v in node.get("columns", {}).items()},
            }
    return docs
