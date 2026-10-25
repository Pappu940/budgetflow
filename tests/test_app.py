"""Tests for BudgetFlow. Run them with:  pytest

They use a throwaway SQLite file, so they never touch real data. To run them against
PostgreSQL instead, set TEST_DATABASE_URL to an empty test database.
"""
import datetime as dt
import os
import re
import tempfile

import pytest

if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.pop("DATABASE_URL", None)
    os.environ["BUDGETFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("POSTGRES_URL", None)

import database  # noqa: E402
from app import app, rupees, token_signer  # noqa: E402


@pytest.fixture
def client():
    """A fresh app and empty tables for every test."""
    with app.app_context():
        for table in ("transactions", "goals", "category_budgets", "users"):
            database.query(f"drop table if exists {table}")
        database.commit()
    database._schema_ready = False
    app.config["TESTING"] = True
    return app.test_client()


def csrf_token(client, path):
    page = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="(\w+)"', page).group(1)


def sign_up(client, name="Asha Verma", email="asha@example.com"):
    token = csrf_token(client, "/register")
    client.post("/register", data={"csrf": token, "name": name, "email": email, "password": "password1"})
    return token


def post(client, token, path, **data):
    return client.post(path, data={"csrf": token, **data}, follow_redirects=True)


TODAY = dt.date.today().isoformat()


def add_entry(client, token, section, amount, category, **extra):
    return post(client, token, f"/{section}", amount=str(amount), category=category, date=TODAY, **extra)


# ------------------------------------------------------------ accounts

def test_public_pages_load(client):
    assert [client.get(p).status_code for p in ("/login", "/register", "/forgot")] == [200, 200, 200]


def test_pages_need_login(client):
    for path in ("/dashboard", "/earnings", "/goals"):
        assert client.get(path).status_code == 302


def test_wrong_password_is_rejected(client):
    sign_up(client)
    client.post("/logout")
    token = csrf_token(client, "/login")
    page = post(client, token, "/login", email="asha@example.com", password="wrong-password")
    assert b"incorrect" in page.data


def test_password_reset_with_signed_link(client):
    sign_up(client)
    client.post("/logout")
    token = csrf_token(client, "/login")
    link = "/reset/" + token_signer.dumps("asha@example.com", salt="password-reset")
    client.post(link, data={"csrf": token, "password": "brand-new-pass", "confirm": "brand-new-pass"})
    login = client.post("/login", data={"csrf": token, "email": "asha@example.com", "password": "brand-new-pass"})
    assert login.status_code == 302
    assert b"invalid or has expired" in client.get("/reset/not-a-token", follow_redirects=True).data


def test_form_without_csrf_token_is_refused(client):
    sign_up(client)
    page = client.post("/budget", data={"budget": "100"}, follow_redirects=True)
    assert b"session expired" in page.data


def test_rupee_formatting():
    assert rupees(1234567) == "₹1,234,567"
    assert rupees(-500) == "−₹500"


# ------------------------------------------------------ dashboard totals

def test_dashboard_totals_and_budget(client):
    token = sign_up(client)
    add_entry(client, token, "earnings", 50000, "Salary")
    add_entry(client, token, "expenses", 12000, "Rent")
    add_entry(client, token, "savings", 8000, "Emergency fund")
    post(client, token, "/budget", budget="20000")
    page = client.get("/dashboard").get_data(as_text=True)
    assert "₹50,000" in page and "₹12,000" in page and "₹8,000" in page
    assert "₹30,000" in page  # earned minus spent minus saved
    assert "(60%)" in page


def test_over_budget_warning(client):
    token = sign_up(client)
    add_entry(client, token, "expenses", 5000, "Food")
    post(client, token, "/budget", budget="3000")
    assert b"over budget" in client.get("/dashboard").data


def test_invalid_amounts_and_categories_are_rejected(client):
    token = sign_up(client)
    assert b"whole number" in add_entry(client, token, "expenses", -5, "Food").data
    assert b"Choose a category" in add_entry(client, token, "expenses", 10, "Hacking").data


def test_charts_render(client):
    token = sign_up(client)
    add_entry(client, token, "earnings", 1000, "Salary")
    add_entry(client, token, "expenses", 400, "Food")
    page = client.get("/dashboard").get_data(as_text=True)
    assert "conic-gradient" in page and "Last 6 months" in page


# -------------------------------------------------------- edit and delete

def test_entry_can_be_edited(client):
    token = sign_up(client)
    add_entry(client, token, "expenses", 500, "Food")
    page = post(client, token, "/entry/1/edit", amount="750", category="Bills", date=TODAY, note="fixed")
    assert b"Entry updated" in page.data and "₹750".encode() in page.data and b"Bills" in page.data


def test_entry_can_be_deleted(client):
    token = sign_up(client)
    add_entry(client, token, "expenses", 500, "Food")
    assert b"Entry deleted" in post(client, token, "/entry/1/delete").data


def test_users_cannot_touch_each_others_entries(client):
    token = sign_up(client)
    add_entry(client, token, "expenses", 500, "Food")
    other = app.test_client()
    other_token = sign_up(other, "Ravi Rao", "ravi@example.com")
    assert other.get("/entry/1/edit").status_code == 404
    post(other, other_token, "/entry/1/delete")
    assert b"Food" in client.get("/expenses").data  # still there


# ----------------------------------------------------------------- goals

def test_goal_progress(client):
    token = sign_up(client)
    post(client, token, "/goals", name="Laptop", target="60000", deadline="")
    page = post(client, token, "/goals/1/add", amount="15000")
    assert "₹15,000 of ₹60,000 (25%)" in page.get_data(as_text=True)
    assert b"Laptop" in client.get("/savings").data  # the deposit shows in savings history


def test_goal_reached_message(client):
    token = sign_up(client)
    post(client, token, "/goals", name="Phone", target="1000", deadline="")
    assert b"Goal reached" in post(client, token, "/goals/1/add", amount="1000").data


def test_deleting_goal_keeps_savings_history(client):
    token = sign_up(client)
    post(client, token, "/goals", name="Trip", target="5000", deadline="")
    post(client, token, "/goals/1/add", amount="2000")
    post(client, token, "/goals/1/delete")
    assert "₹2,000".encode() in client.get("/savings").data


def test_goal_validation(client):
    token = sign_up(client)
    assert b"name and a target" in post(client, token, "/goals", name="X", target="0", deadline="").data
    assert client.post("/goals/99/add", data={"csrf": token, "amount": "5"}).status_code == 404


# ------------------------------------------------------ category budgets

def test_category_budget_warns_when_close_and_when_over(client):
    token = sign_up(client)
    post(client, token, "/category-budget", category="Food", amount="1000")
    assert b"Heads up: 90%" in add_entry(client, token, "expenses", 900, "Food").data
    assert b"over your Food budget" in add_entry(client, token, "expenses", 300, "Food").data
    assert b"Over by" in client.get("/dashboard").data


def test_category_budget_can_be_removed(client):
    token = sign_up(client)
    post(client, token, "/category-budget", category="Food", amount="1000")
    post(client, token, "/category-budget", category="Food", amount="")
    assert b"Set a limit for a category" in client.get("/dashboard").data
