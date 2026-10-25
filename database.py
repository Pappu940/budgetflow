"""Database access for BudgetFlow.

PostgreSQL is used when DATABASE_URL is set (production). Otherwise the app uses
a local SQLite file, so it runs with no setup while developing.
"""
import os
import sqlite3

from flask import g

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
USING_POSTGRES = bool(DATABASE_URL)
SQLITE_PATH = os.environ.get("BUDGETFLOW_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "budgetflow.db")

if USING_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

PRIMARY_KEY = "serial primary key" if USING_POSTGRES else "integer primary key"

# kind is "income", "expense" or "saving". Dates are ISO text (YYYY-MM-DD).
SCHEMA = [
    f"""create table if not exists users (
        id {PRIMARY_KEY}, name text, email text unique, pw text, budget int default 0)""",
    f"""create table if not exists transactions (
        id {PRIMARY_KEY}, user_id int, kind text, category text,
        amount int, date text, note text, goal_id int)""",
    f"""create table if not exists goals (
        id {PRIMARY_KEY}, user_id int, name text, target int, deadline text)""",
    f"""create table if not exists category_budgets (
        id {PRIMARY_KEY}, user_id int, category text, amount int)""",
]

_schema_ready = False


def get_db():
    if "db" not in g:
        if USING_POSTGRES:
            g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            g.db = sqlite3.connect(SQLITE_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db


def query(sql, params=()):
    """Run a statement. Write queries with ? placeholders for both databases."""
    if USING_POSTGRES:
        sql = sql.replace("?", "%s")
    return get_db().execute(sql, params)


def commit():
    get_db().commit()


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _add_missing_columns():
    """Upgrade databases created before these columns existed."""
    maybe = "if not exists " if USING_POSTGRES else ""
    try:
        query(f"alter table transactions add column {maybe}goal_id int")
        commit()
    except Exception:
        get_db().rollback()  # the column is already there


def init_db():
    """Create the tables. Safe to call on every request."""
    global _schema_ready
    if _schema_ready:
        return
    for statement in SCHEMA:
        query(statement)
    commit()
    _add_missing_columns()
    _schema_ready = True
