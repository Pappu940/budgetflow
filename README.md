# BudgetFlow

![Tests](https://github.com/YOUR-USERNAME/budgetflow/actions/workflows/tests.yml/badge.svg)

A personal budgeting app built with Flask. Track earnings, expenses and savings, set monthly and
per-category budgets, save towards goals, and see where the money went. Light lavender and pink
theme by default, with a dark mode toggle that remembers your choice.

**Live demo:** _add your Vercel link here_

## Features

- Sign up, log in, log out, and password reset through a signed link that expires after 30 minutes
- Dashboard with earned, spent, saved and left-to-spend totals for any month
- Charts: a spending donut by category and a six-month earned-versus-spent bar chart (plain CSS, no chart library)
- Monthly budget with a progress bar, plus per-category budgets that warn you at 80% and when you go over
- Add, edit and delete earnings, expenses and savings
- Savings goals with a target, optional deadline, progress bar and a "goal reached" message
- Left-side menu, responsive layout for phones and laptops, and light and dark themes

## Tech stack

Python, Flask, Jinja templates, plain CSS and a little JavaScript, PostgreSQL (SQLite for local
development), pytest, GitHub Actions.

## Project structure

    app.py          routes and request handling
    database.py     connection and schema (PostgreSQL or SQLite)
    templates/      Jinja pages
    public/         stylesheet
    tests/          pytest suite (runs on every push through GitHub Actions)

## Run it locally

    python -m venv .venv
    .venv\Scripts\activate           # Mac/Linux: source .venv/bin/activate
    pip install -r requirements.txt
    python app.py                    # http://127.0.0.1:5000

## Run the tests

    pip install -r requirements-dev.txt
    pytest

## Design decisions

- **One query helper for two databases.** Queries use `?` placeholders and `database.query()` adapts
  them for PostgreSQL, so local development needs no setup.
- **Charts without a library.** The donut is a CSS `conic-gradient` and the bars are sized with
  percentages computed in Python, so the pages stay fast and have no external scripts.
- **Security basics.** Passwords are hashed, every form carries a CSRF token, reset links are signed
  and expire, each user can only see, edit and delete their own data, and the login redirect only follows local paths.
- **Goal progress is calculated, not stored.** A goal's saved amount is the sum of the savings linked to it,
  so editing or deleting a deposit keeps the progress correct.
- **Dates as ISO text.** `YYYY-MM-DD` strings make month filtering a simple `substr(date, 1, 7)` match.
- **Whole-rupee amounts** keep the maths simple for a first version.

## Possible next steps

Recurring transactions, CSV export, decimal amounts, receipt photos and a profile page.
