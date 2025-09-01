import os
import re
import time
from datetime import datetime
from typing import Dict, Tuple, List

import pandas as pd
import plotly.express as px
import streamlit as st

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Optional: OpenAI (or compatible) for NL→SQL
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# Snowflake connector
import snowflake.connector

# ---------------------------
# Page config
# ---------------------------
st.set_page_config(page_title="AnswerLens ", layout="wide")
st.title("AnswerLens :mag_right:")
st.caption("Ask a question; I’ll generate safe SQL for Snowflake, run it, pick suitable visuals, and suggest insights. Focus on what matters in your data.")

# ---------------------------
# Helpers to get configuration from: Streamlit secrets, Env/.env, or manual input
# ---------------------------
SF_KEYS = [
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
]

def read_from_secrets() -> Dict[str, str]:
    cfg = {}
    try:
        # Support both nested [snowflake] and top-level keys
        sf = st.secrets.get("snowflake", {})
    except Exception:
        # No secrets configured; return empty defaults
        empty = {k: "" for k in SF_KEYS}
        empty.update({"OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4o-mini", "AUDIT_DB": "DATA_LOADS_DB", "AUDIT_SCHEMA": "AUDIT_SCHEMA", "AUDIT_TABLE": "CHAT_DATA_AUDIT"})
        return empty
    
    for k in SF_KEYS:
        cfg[k] = (
            sf.get(k)
            if isinstance(sf, dict) and sf.get(k) is not None
            else st.secrets.get(k, "")
        )
    # Optional sections
    openai_section = st.secrets.get("openai", {})
    cfg["OPENAI_API_KEY"] = (
        openai_section.get("api_key")
        if isinstance(openai_section, dict) and openai_section.get("api_key") is not None
        else st.secrets.get("OPENAI_API_KEY", "")
    )
    cfg["OPENAI_MODEL"] = st.secrets.get("OPENAI_MODEL", "gpt-4o-mini")
    cfg["AUDIT_DB"] = st.secrets.get("AUDIT_DB", "DATA_LOADS_DB")
    cfg["AUDIT_SCHEMA"] = st.secrets.get("AUDIT_SCHEMA", "AUDIT_SCHEMA")
    cfg["AUDIT_TABLE"] = st.secrets.get("AUDIT_TABLE", "CHAT_DATA_AUDIT")
    return cfg

def read_from_env() -> Dict[str, str]:
    cfg = {k: os.getenv(k, "") for k in SF_KEYS}
    cfg["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "")
    cfg["OPENAI_MODEL"] = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    cfg["AUDIT_DB"] = os.getenv("AUDIT_DB","DATA_LOADS_DB")
    cfg["AUDIT_SCHEMA"] = os.getenv("AUDIT_SCHEMA", "AUDIT_SCHEMA")
    cfg["AUDIT_TABLE"] = os.getenv("AUDIT_TABLE", "CHAT_DATA_AUDIT")
    return cfg

def sf_connect(cfg: Dict[str, str]):
    return snowflake.connector.connect(
        account=cfg.get("SNOWFLAKE_ACCOUNT", ""),
        user=cfg.get("SNOWFLAKE_USER", ""),
        password=cfg.get("SNOWFLAKE_PASSWORD", ""),
        warehouse=cfg.get("SNOWFLAKE_WAREHOUSE", ""),
        database=cfg.get("SNOWFLAKE_DATABASE", "SNOWFLAKE_SAMPLE_DATA"),
        schema=cfg.get("SNOWFLAKE_SCHEMA", "TPCH_SF1000"),
        client_session_keep_alive=True,
        application="NL2SQLViz",
    )

# ---------------------------
# Sidebar: connections & settings
# ---------------------------
with st.sidebar:
    st.header("🔐 Connections")
    # Autodetect preferred default
    # Safe check for secrets without calling bool(len(st.secrets)), which raises when missing.
    try:
        # Try reading a bogus key. If secrets file exists, this raises KeyError (which means secrets ARE present).
        _ = st.secrets["__probe__"]
        secrets_present = True
    except KeyError:
        secrets_present = True  # secrets file exists but key missing -> OK
    except Exception:
        secrets_present = False
    
    default_source_ix = 0 if secrets_present else 1
    source = st.radio(
        "Credential source",
        ["Streamlit secrets", ".env / OS env", "Manual input"],
        index=default_source_ix,
        help="On Streamlit Cloud, add credentials in Settings → Secrets.",
    )

    # seed defaults
    base_cfg = read_from_secrets() if source == "Streamlit secrets" else read_from_env()

    # manual inputs
    if source == "Manual input":
        st.markdown("**Snowflake**")
        base_cfg["SNOWFLAKE_ACCOUNT"] = st.text_input("SNOWFLAKE_ACCOUNT", value=base_cfg.get("SNOWFLAKE_ACCOUNT") or "KHIGKSW-TF63553")
        base_cfg["SNOWFLAKE_USER"] = st.text_input("SNOWFLAKE_USER", value=base_cfg.get("SNOWFLAKE_USER") or "SNFLUSER2025")
        base_cfg["SNOWFLAKE_PASSWORD"] = st.text_input("SNOWFLAKE_PASSWORD", type="password", value=base_cfg.get("SNOWFLAKE_PASSWORD", ""))
        base_cfg["SNOWFLAKE_WAREHOUSE"] = st.text_input("SNOWFLAKE_WAREHOUSE", value=base_cfg.get("SNOWFLAKE_WAREHOUSE") or "ETL_RUN_WH")
        base_cfg["SNOWFLAKE_DATABASE"] = st.text_input("SNOWFLAKE_DATABASE", value=base_cfg.get("SNOWFLAKE_DATABASE") or "SNOWFLAKE_SAMPLE_DATA")
        base_cfg["SNOWFLAKE_SCHEMA"] = st.text_input("SNOWFLAKE_SCHEMA", value=base_cfg.get("SNOWFLAKE_SCHEMA") or "TPCH_SF100")
        st.markdown("**OpenAI (optional)**")
        base_cfg["OPENAI_API_KEY"] = st.text_input("OPENAI_API_KEY (optional)", type="password", value=base_cfg.get("OPENAI_API_KEY", ""))
        base_cfg["OPENAI_MODEL"] = st.text_input("OPENAI_MODEL", value=base_cfg.get("OPENAI_MODEL", "gpt-4o-mini"))

    st.divider()
    st.header("⚙️ Settings")
    max_rows = st.number_input("Max rows to fetch", min_value=100, max_value=50000, value=5000, step=100)
    hard_limit = st.number_input("Hard LIMIT injected into SQL (to protect UI)", min_value=100, max_value=100000, value=5000, step=100)
    timeout_s = st.number_input("Statement timeout (seconds)", min_value=5, max_value=600, value=60, step=5)
    enable_audit = st.toggle("Write audit logs (PROMPT/SQL/ROWCOUNT)", value=False, help="Writes to the configured AUDIT_DB.AUDIT_SCHEMA.AUDIT_TABLE")
    audit_db = st.text_input("AUDIT_DB", value=base_cfg.get("AUDIT_DB") or "DATA_LOADS_DB")
    audit_schema = st.text_input("AUDIT_SCHEMA", value=base_cfg.get("AUDIT_SCHEMA") or "AUDIT_SCHEMA")
    audit_table = st.text_input("AUDIT_TABLE", value=base_cfg.get("AUDIT_TABLE") or "CHAT_DATA_AUDIT")
    audit_debug = st.toggle("Show audit errors", value=True)

# ---------------------------
# TPCH schema (helps the LLM stay on rails)
# ---------------------------
TPCH_TABLES = {
    "CUSTOMER": ["C_CUSTKEY","C_NAME","C_ADDRESS","C_NATIONKEY","C_PHONE","C_ACCTBAL","C_MKTSEGMENT","C_COMMENT"],
    "ORDERS": ["O_ORDERKEY","O_CUSTKEY","O_ORDERSTATUS","O_TOTALPRICE","O_ORDERDATE","O_ORDERPRIORITY","O_CLERK","O_SHIPPRIORITY","O_COMMENT"],
    "LINEITEM": [
        "L_ORDERKEY","L_PARTKEY","L_SUPPKEY","L_LINENUMBER","L_QUANTITY","L_EXTENDEDPRICE","L_DISCOUNT","L_TAX",
        "L_RETURNFLAG","L_LINESTATUS","L_SHIPDATE","L_COMMITDATE","L_RECEIPTDATE","L_SHIPINSTRUCT","L_SHIPMODE","L_COMMENT"
    ],
    "NATION": ["N_NATIONKEY","N_NAME","N_REGIONKEY","N_COMMENT"],
    "REGION": ["R_REGIONKEY","R_NAME","R_COMMENT"],
    "PART": ["P_PARTKEY","P_NAME","P_MFGR","P_BRAND","P_TYPE","P_SIZE","P_CONTAINER","P_RETAILPRICE","P_COMMENT"],
    "PARTSUPP": ["PS_PARTKEY","PS_SUPPKEY","PS_AVAILQTY","PS_SUPPLYCOST","PS_COMMENT"],
    "SUPPLIER": ["S_SUPPKEY","S_NAME","S_ADDRESS","S_NATIONKEY","S_PHONE","S_ACCTBAL","S_COMMENT"],
}
def schema_block(database: str, schema: str) -> str:
    lines = [f"You can ONLY query from {database}.{schema} and only these tables/columns:"]
    for t, cols in TPCH_TABLES.items():
        lines.append(f"- {database}.{schema}.{t} (" + ", ".join(cols) + ")")
    return "\n".join(lines)

# ---------------------------
# NL → SQL
# ---------------------------
SQL_SYSTEM_PROMPT = '''
You are a senior Snowflake SQL generator. Convert the user's question into a **single** safe SQL SELECT statement for Snowflake.
STRICT RULES:
- Only read from the whitelisted schema and tables the user provides.
- Output **ONLY** the SQL, no backticks, no commentary.
- Use fully qualified names <DATABASE>.<SCHEMA>.<TABLE>.
- Prefer aggregated results (GROUP BY) with explicit column aliases.
- Push filters into WHERE; use DATE or YEAR extraction as appropriate.
- Never use DDL/DML (CREATE/UPDATE/DELETE/INSERT/MERGE/COPY) or CALL.
- Always end with a LIMIT if not provided, using the limit hint supplied.
- If the question is ambiguous, choose a reasonable default and continue.
'''.strip()

def call_llm_for_sql(user_question: str, database: str, schema: str, limit_hint: int, openai_api_key: str, openai_model: str) -> str:
    whitelist = schema_block(database, schema)
    user_prompt = f'''
{whitelist}
Generate a single Snowflake SQL (no comments) answering:
"{user_question.strip()}"
Ensure the query ends with "LIMIT {limit_hint}" if not already.
'''.strip()

    if OPENAI_AVAILABLE and openai_api_key:
        client = OpenAI(api_key=openai_api_key)
        model = openai_model or "gpt-4o-mini"
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SQL_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        sql = completion.choices[0].message.content.strip()
    else:
        # Simple heuristic fallback
        uq = user_question.lower()
        if "revenue" in uq or "sales" in uq:
            sql = f'''
SELECT c.C_MKTSEGMENT AS SEGMENT,
       SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM {database}.{schema}.CUSTOMER c
JOIN {database}.{schema}.ORDERS o ON o.O_CUSTKEY = c.C_CUSTKEY
JOIN {database}.{schema}.LINEITEM l ON l.L_ORDERKEY = o.O_ORDERKEY
GROUP BY 1
ORDER BY 2 DESC
LIMIT {limit_hint}
'''.strip()
        elif "top" in uq and "customers" in uq:
            sql = f'''
SELECT c.C_NAME AS CUSTOMER,
       SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM {database}.{schema}.CUSTOMER c
JOIN {database}.{schema}.ORDERS o ON o.O_CUSTKEY = c.C_CUSTKEY
JOIN {database}.{schema}.LINEITEM l ON l.L_ORDERKEY = o.O_ORDERKEY
GROUP BY 1
ORDER BY 2 DESC
LIMIT {limit_hint}
'''.strip()
        elif "monthly" in uq and ("revenue" in uq or "sales" in uq):
            sql = f'''
SELECT DATE_TRUNC('month', o.O_ORDERDATE) AS MONTH,
       SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM {database}.{schema}.ORDERS o
JOIN {database}.{schema}.LINEITEM l ON l.L_ORDERKEY = o.O_ORDERKEY
GROUP BY 1
ORDER BY 1
LIMIT {limit_hint}
'''.strip()
        else:
            sql = f"SELECT * FROM {database}.{schema}.CUSTOMER LIMIT {limit_hint}"

    # Safety checks
    sql = sql.split(";")[0].strip()
    if not re.match(r"^select\s", sql, re.IGNORECASE):
        raise ValueError("Generated SQL is not a SELECT.")
    if re.search(r"\blimit\b\s+\d+\s*$", sql, re.IGNORECASE) is None:
        sql += f"\nLIMIT {limit_hint}"
    # Qualify tables if missing
    lowered = sql.lower()
    if f" {database.lower()}.{schema.lower()}." not in lowered:
        for t in TPCH_TABLES:
            sql = re.sub(rf"(?i)\b{t}\b", f"{database}.{schema}.{t}", sql)
    return sql

# ---------------------------
# Snowflake helpers
# ---------------------------
def run_query_df(cfg: Dict[str, str], sql: str, timeout_seconds: int, max_rows: int) -> pd.DataFrame:
    ctx = sf_connect(cfg)
    try:
        cs = ctx.cursor()
        try:
            cs.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS={int(timeout_seconds)}")
            cs.execute(sql)
            cols = [c[0] for c in cs.description]
            rows = cs.fetchmany(max_rows)
            return pd.DataFrame(rows, columns=cols)
        finally:
            cs.close()
    finally:
        ctx.close()

def try_audit_log(cfg: Dict[str, str], enabled: bool, prompt: str, sql: str, rowcount: int, qname: str, show_errors: bool):
    if not enabled:
        return
    try:
        ctx = sf_connect(cfg)
        cur = ctx.cursor()
        try:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {qname} (TS TIMESTAMP_NTZ, PROMPT STRING, SQL_TEXT STRING, ROWCOUNT INTEGER)")
            cur.execute(
                f"INSERT INTO {qname} (TS, PROMPT, SQL_TEXT, ROWCOUNT) VALUES (CURRENT_TIMESTAMP(), %(prompt)s, %(sql)s, %(rc)s)",
                {"prompt": prompt, "sql": sql, "rc": int(rowcount)},
            )
            try:
                ctx.commit()
            except Exception:
                pass
        finally:
            cur.close(); ctx.close()
    except Exception as e:
        if show_errors:
            st.warning(f"Audit log failed for {qname}: {e}")

# ---------------------------
# Smart viz & insights (same as before)
# ---------------------------
NUMERIC_HINTS = ("revenue","amount","price","total","sum","avg","average","count","qty","quantity","score","rate","value","metric")

def to_datetime_if_possible(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    try:
        return pd.to_datetime(series, errors="raise")
    except Exception:
        return series

def classify_columns(df: pd.DataFrame):
    numerics, dates, cats = [], [], []
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_numeric_dtype(s):
            numerics.append(col)
        else:
            s2 = to_datetime_if_possible(s)
            if pd.api.types.is_datetime64_any_dtype(s2):
                df[col] = s2
                dates.append(col)
            else:
                cats.append(col)
    return numerics, dates, cats

def pick_metric(numerics):
    if not numerics:
        return ""
    for hint in NUMERIC_HINTS:
        for c in numerics:
            if hint in c.lower():
                return c
    return numerics[0]

def pick_dimension(cats, df, max_card=50):
    if not cats:
        return ""
    candidates = sorted(cats, key=lambda c: df[c].nunique())
    for c in candidates:
        if 2 <= df[c].nunique() <= max_card:
            return c
    return candidates[0]

def pareto_dataframe(df, dim, metric, top_n=20):
    tmp = df.groupby(dim, dropna=False)[metric].sum().reset_index()
    tmp = tmp.sort_values(metric, ascending=False)
    tmp["cum_pct"] = tmp[metric].cumsum() / tmp[metric].sum() * 100.0
    return tmp.head(top_n)

def correlation_pairs(df, numerics):
    if len(numerics) < 2:
        return ("","",0.0)
    corr = df[numerics].corr(numeric_only=True)
    best = (None, None, 0.0)
    for i, a in enumerate(numerics):
        for b in numerics[i+1:]:
            v = abs(corr.loc[a,b])
            if pd.notna(v) and v > best[2]:
                best = (a,b,float(corr.loc[a,b]))
    if best[0] is None: return ("","",0.0)
    return best

def generate_insights(df: pd.DataFrame):
    insights = []
    if df.empty:
        return ["No rows returned. Try broadening filters or lowering the LIMIT."]
    numerics, dates, cats = classify_columns(df.copy())

    # Time trend
    if dates and numerics:
        x = dates[0]; y = pick_metric(numerics)
        d = df[[x,y]].dropna().sort_values(x)
        if len(d) >= 2:
            first, last = d[y].iloc[0], d[y].iloc[-1]
            change = (last - first)
            pct = (change / first * 100.0) if first not in (0,None) else None
            if pct is not None and pd.notna(pct):
                direction = "increased" if change > 0 else "decreased" if change < 0 else "stayed flat"
                insights.append(f"Time trend: **{y}** has {direction} by **{abs(change):,.2f}** ({abs(pct):.1f}%).")
            else:
                insights.append(f"Time trend: **{y}** changed by **{change:,.2f}** over the period.")

    # Pareto / top categories
    if cats and numerics:
        dim = pick_dimension(cats, df)
        metric = pick_metric(numerics)
        p = pareto_dataframe(df, dim, metric, top_n=10)
        if not p.empty:
            top = p.iloc[0]
            insights.append(f"Top **{dim}** by **{metric}** is **{top[dim]}** at **{top[metric]:,.2f}**.")
            eighty = p[p["cum_pct"] >= 80.0]
            if not eighty.empty:
                k = eighty.index[0] + 1
                insights.append(f"Top **{k} {dim}** contribute ~**80%** of total **{metric}** (Pareto).")

    # Correlation
    if len(numerics) >= 2:
        a,b,r = correlation_pairs(df, numerics)
        if a and b:
            if abs(r) >= 0.6:
                relation = "positively" if r > 0 else "negatively"
                insights.append(f"Strong correlation: **{a}** and **{b}** are {relation} correlated (r≈{r:.2f}).")
            else:
                insights.append(f"Weak correlation across numeric fields (strongest |r|≈{abs(r):.2f} between **{a}** and **{b}**).")

    # Outliers via IQR
    if numerics:
        m = pick_metric(numerics)
        s = df[m].dropna().astype(float)
        if len(s) >= 5:
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            upper = q3 + 1.5*iqr
            n_out = int((s > upper).sum())
            if n_out > 0:
                insights.append(f"Outliers: **{n_out}** values in **{m}** exceed the upper IQR fence (~{upper:,.2f}).")

    if not insights:
        insights.append("No strong signals detected; consider refining the question or grouping/aggregating the results.")
    return insights

def render_smart_visuals(df: pd.DataFrame):
    if df.empty:
        st.info("No data to visualize.")
        return
    numerics, dates, cats = classify_columns(df.copy())
    tabs = []

    if dates and numerics:
        x = dates[0]; y = pick_metric(numerics)
        tabs.append(("Trend", px.line(df.sort_values(x), x=x, y=y)))
    if cats and numerics:
        dim = pick_dimension(cats, df); metric = pick_metric(numerics)
        dff = df.groupby(dim, dropna=False)[metric].sum().reset_index()
        dff = dff.sort_values(metric, ascending=False).head(50)
        tabs.append(("Ranking", px.bar(dff, x=dim, y=metric)))
        p = pareto_dataframe(df, dim, metric, top_n=20)
        tabs.append(("Pareto", px.line(p, x=dim, y="cum_pct")))
    if len(numerics) >= 2:
        a,b,_ = correlation_pairs(df, numerics)
        if a and b:
            tabs.append(("Scatter", px.scatter(df, x=a, y=b, trendline=None)))
            corr = df[numerics].corr(numeric_only=True)
            tabs.append(("Correlation", px.imshow(corr, text_auto=True)))

    if not tabs and numerics:
        m = pick_metric(numerics)
        tabs.append(("Distribution", px.histogram(df, x=m)))

    if not tabs:
        st.dataframe(df, use_container_width=True)
        return

    st.write("### Visuals")
    labels = [t[0] for t in tabs]
    figures = [t[1] for t in tabs]
    st_tabs = st.tabs(labels)
    for tab, fig in zip(st_tabs, figures):
        with tab:
            st.plotly_chart(fig, use_container_width=True)

# ---------------------------
# UI - ask + run
# ---------------------------
example_queries = [
    "Top 10 customers by revenue",
    "Monthly revenue trend for 1995",
    "Revenue by nation",
    "Average discount by ship mode",
    "Top 15 parts by total sales",
]

st.subheader("Ask a question")
col1, col2 = st.columns([4,1])
with col1:
    question = st.text_input("Your question", placeholder="e.g., Revenue by nation in 1995")
with col2:
    st.markdown("**Quick picks**")
    for q in example_queries:
        if st.button(q, use_container_width=True):
            question = q

advanced = st.expander("Advanced: generated SQL & raw data")

if st.button("🚀 Run", type="primary"):
    # Active configuration after sidebar choices
    cfg = base_cfg.copy()

    # Validate
    missing = [k for k in SF_KEYS if not cfg.get(k)]
    if missing:
        st.error("Please provide Snowflake connection details (missing: " + ", ".join(missing) + ").")
        st.stop()

    if not (question and question.strip()):
        st.warning("Please enter a question.")
        st.stop()

    # Fetch optional OpenAI config
    openai_api_key = cfg.get("OPENAI_API_KEY", "")
    openai_model = cfg.get("OPENAI_MODEL", "gpt-4o-mini")

    with st.spinner("Generating SQL from your question…"):
        try:
            database = cfg.get("SNOWFLAKE_DATABASE", "SNOWFLAKE_SAMPLE_DATA")
            schema = cfg.get("SNOWFLAKE_SCHEMA", "TPCH_SF1000")
            sql = call_llm_for_sql(question, database, schema, int(hard_limit), openai_api_key, openai_model)
        except Exception as e:
            st.error(f"Failed to generate SQL: {e}")
            st.stop()

    banned = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|COPY|CALL|GRANT|REVOKE)\b", re.IGNORECASE)
    if banned.search(sql):
        st.error("Query blocked by guardrails (non-SELECT detected).")
        st.stop()

    st.success("SQL generated")
    with advanced:
        st.code(sql, language="sql")

    with st.spinner("Running on Snowflake…"):
        t0 = time.time()
        try:
            df = run_query_df(cfg, sql, timeout_s, max_rows)
        except Exception as e:
            st.error(f"Query failed: {e}")
            st.stop()
        dt = time.time() - t0

    st.markdown(f"**Returned {len(df):,} rows in {dt:0.2f}s**")

    # Visuals
    render_smart_visuals(df)

    # Insights
    st.write("### Insights")
    for bullet in generate_insights(df):
        st.write(f"- {bullet}")

    # Audit (after success)
    qname = f"{audit_db}.{audit_schema}.{audit_table}"
    try_audit_log(cfg, enable_audit, question, sql, len(df), qname, audit_debug)

    # Raw
    with advanced:
        st.subheader("Raw results")
        st.dataframe(df, use_container_width=True)
