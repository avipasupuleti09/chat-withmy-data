# NL2SQL-Viz (Snowflake TPCH): NL → SQL → Smart Visuals + Insights

A Streamlit app that converts natural language questions into **guardrailed Snowflake SQL** against `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000`, executes it, and renders **smart visualizations** with **automatic insights**. Includes optional **audit logging** to a writable database.

## Features
- Guardrailed NL → SQL (whitelist TPCH tables, SELECT‑only, forced LIMIT, statement timeout)
- Smart visuals (Trend, Ranking + Pareto, Scatter + Correlation heatmap, Histogram fallback)
- Insights (trend delta %, Pareto 80/20, strongest correlation, outlier count)
- Advanced panel to show generated SQL + raw results
- Audit log to configurable AUDIT_DB.AUDIT_SCHEMA.AUDIT_TABLE using server-side CURRENT_TIMESTAMP()

## Quickstart
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # fill Snowflake creds and AUDIT_* values
streamlit run app/app.py

## Docker
docker build -t nl2sql-viz:local .
docker run --rm -it -p 8501:8501 --env-file .env nl2sql-viz:local

## Audit notes
Use a **writable** DB (not SNOWFLAKE_SAMPLE_DATA). Grant USAGE on DB/SCHEMA and CREATE TABLE/INSERT on the schema to your role, or run sql/create_audit_table.sql.
