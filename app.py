"""BudgetFlow: a small personal budgeting app built with Flask.

Track earnings, expenses and savings, set monthly and per-category budgets, save
towards goals, and see where the money went on the dashboard.
"""
import datetime as dt
import os
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from database import close_db, commit, init_db, query

RESET_LINK_MINUTES = 30
MAX_AMOUNT = 100_000_000
WARN_AT_PERCENT = 80
CHART_COLOURS = ["#7C5CD6", "#E066A6", "#6F8BF2", "#2FB5A8", "#F2A93B", "#B65CD8", "#F27D72", "#9AA5B8"]

SECTIONS = {
    "earnings": {"kind": "income", "title": "Earnings", "button": "Add earning",
                 "categories": ["Salary", "Freelance", "Business", "Gift", "Other"]},
    "expenses": {"kind": "expense", "title": "Expenses", "button": "Add expense",
                 "categories": ["Food", "Rent", "Transport", "Bills", "Shopping", "Health", "Fun", "Other"]},
    "savings": {"kind": "saving", "title": "Savings", "button": "Add to savings",
                "categories": ["Emergency fund", "Travel", "Investment", "Goal", "Other"]},
}
SECTION_OF_KIND = {info["kind"]: name for name, info in SECTIONS.items()}

# ---------------------------------------------------------------- app setup

app = Flask(__name__, static_folder="public", static_url_path="")  # public/ is served by Vercel's CDN
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

ON_VERCEL = bool(os.environ.get("VERCEL"))
app.secret_key = os.environ.get("SECRET_KEY") or (None if ON_VERCEL else "dev-only-secret")
if not app.secret_key:
    raise RuntimeError("Set SECRET_KEY in the Vercel environment variables.")
app.config.update(SESSION_COOKIE_SECURE=ON_VERCEL, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_HTTPONLY=True)
app.teardown_appcontext(close_db)
token_signer = URLSafeTimedSerializer(app.secret_key)


@app.before_request
def prepare_request():
    init_db()
    # Every form carries a CSRF token. Logout is exempt because signing out is harmless.
    if request.method != "POST" or request.endpoint == "logout":
        return
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)


@app.context_processor
def template_helpers():
    session.setdefault("csrf", secrets.token_hex(16))
    return {"csrf": session["csrf"], "uname": session.get("uname"), "inr": rupees,
            "today": dt.date.today().isoformat()}


@app.errorhandler(400)
def bad_request(_error):
    flash("Your session expired. Please try again.")
    return redirect(request.referrer or url_for("login"))


# ------------------------------------------------------------------ helpers

def rupees(amount):
    sign = "−" if amount < 0 else ""
    return f"{sign}₹{abs(amount):,}"


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "uid" not in session:
            flash("Log in to continue.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def positive_int(value, maximum=MAX_AMOUNT):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= maximum else None


def valid_date(value):
    try:
        dt.date.fromisoformat(value or "")
        return value
    except ValueError:
        return None


def selected_month():
    """The month in the URL (?month=2026-10), or the current month."""
    value = request.args.get("month", "")
    try:
        dt.datetime.strptime(value, "%Y-%m")
        return value
    except ValueError:
        return dt.date.today().strftime("%Y-%m")


def recent_months(month, count=6):
    """The last `count` months ending at `month`, oldest first, as YYYY-MM strings."""
    year, number = int(month[:4]), int(month[5:])
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{number:02d}")
        number -= 1
        if number == 0:
            year, number = year - 1, 12
    return months[::-1]


def month_totals(user_id, month):
    """Total earned, spent and saved in a month."""
    rows = query("select kind, sum(amount) as total from transactions "
                 "where user_id=? and substr(date, 1, 7)=? group by kind", (user_id, month)).fetchall()
    totals = {row["kind"]: int(row["total"]) for row in rows}
    return totals.get("income", 0), totals.get("expense", 0), totals.get("saving", 0)


def monthly_history(user_id, month):
    """Earned and spent for each of the last six months, with bar heights as percentages."""
    months = recent_months(month)
    rows = query("select substr(date, 1, 7) as ym, kind, sum(amount) as total from transactions "
                 "where user_id=? and substr(date, 1, 7)>=? and substr(date, 1, 7)<=? "
                 "and kind in ('income', 'expense') group by ym, kind",
                 (user_id, months[0], months[-1])).fetchall()
    totals = {(row["ym"], row["kind"]): int(row["total"]) for row in rows}
    history = []
    for ym in months:
        label = dt.date(int(ym[:4]), int(ym[5:]), 1).strftime("%b")
        history.append({"label": label, "earned": totals.get((ym, "income"), 0),
                        "spent": totals.get((ym, "expense"), 0)})
    tallest = max([m["earned"] for m in history] + [m["spent"] for m in history] + [1])
    for m in history:
        m["earned_pct"] = round(m["earned"] / tallest * 100)
        m["spent_pct"] = round(m["spent"] / tallest * 100)
    return history


def category_legend(categories):
    """Colour, share and the CSS gradient for the spending donut chart."""
    total = sum(amount for _name, amount in categories)
    legend, stops, start = [], [], 0.0
    for index, (name, amount) in enumerate(categories):
        colour = CHART_COLOURS[index % len(CHART_COLOURS)]
        end = start + amount / total * 100
        stops.append(f"{colour} {start:.2f}% {end:.2f}%")
        legend.append({"name": name, "amount": amount, "colour": colour, "percent": round(amount / total * 100)})
        start = end
    gradient = "conic-gradient(" + ", ".join(stops) + ")" if stops else ""
    return legend, gradient


def category_limits(user_id, spent_by_category):
    """Each category budget with how much of it is used. state is ok, near or over."""
    rows = query("select category, amount from category_budgets where user_id=? order by category",
                 (user_id,)).fetchall()
    limits = []
    for row in rows:
        spent = spent_by_category.get(row["category"], 0)
        percent = round(spent / row["amount"] * 100)
        state = "over" if percent > 100 else "near" if percent >= WARN_AT_PERCENT else "ok"
        limits.append({"category": row["category"], "limit": row["amount"], "spent": spent,
                       "percent": percent, "state": state})
    return limits


def category_warning(user_id, category, month):
    """A message if this category is close to or over its budget this month, otherwise None."""
    row = query("select amount from category_budgets where user_id=? and category=?",
                (user_id, category)).fetchone()
    if not row:
        return None
    spent = query("select coalesce(sum(amount), 0) as total from transactions where user_id=? "
                  "and kind='expense' and category=? and substr(date, 1, 7)=?",
                  (user_id, category, month)).fetchone()["total"]
    percent = round(spent / row["amount"] * 100)
    if percent > 100:
        return f"You are {rupees(spent - row['amount'])} over your {category} budget."
    if percent >= WARN_AT_PERCENT:
        return f"Heads up: {percent}% of your {category} budget is used."
    return None


# ----------------------------------------------------------------- accounts

@app.route("/")
def home():
    return redirect(url_for("dashboard" if "uid" in session else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if len(name) < 2 or "@" not in email:
            flash("Enter your name and a valid email.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        elif query("select 1 from users where email=?", (email,)).fetchone():
            flash("An account with this email already exists. Log in instead.")
        else:
            cursor = query("insert into users (name, email, pw) values (?,?,?) returning id",
                           (name, email, generate_password_hash(password)))
            user_id = cursor.fetchone()["id"]
            commit()
            session.update(uid=user_id, uname=name)
            return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = query("select * from users where email=?", (email,)).fetchone()
        if user and check_password_hash(user["pw"], request.form["password"]):
            session.update(uid=user["id"], uname=user["name"])
            next_page = request.args.get("next", "/")
            if not next_page.startswith("/") or next_page.startswith("//"):
                next_page = "/"  # only follow local paths
            return redirect(next_page)
        flash("Email or password is incorrect.")
    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def send_reset_email(email, link):
    """Email the reset link. Without SMTP settings the link is printed to the log instead."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"\n[BudgetFlow] Password reset link for {email}: {link}\n")
        return False
    message = EmailMessage()
    message["Subject"] = "Reset your BudgetFlow password"
    message["From"] = os.environ.get("SMTP_FROM", "no-reply@budgetflow.example")
    message["To"] = email
    message.set_content(f"Use this link within {RESET_LINK_MINUTES} minutes to reset your password:\n{link}")
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        server.send_message(message)
    return True


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        if query("select 1 from users where email=?", (email,)).fetchone():
            token = token_signer.dumps(email, salt="password-reset")
            link = url_for("reset", token=token, _external=True)
            if not send_reset_email(email, link) and app.debug:
                flash(f"Dev mode, email not configured. Reset link: {link}")
        flash("If that email has an account, a reset link is on its way.")  # same reply either way
        return redirect(url_for("forgot"))
    return render_template("auth.html", mode="forgot")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    try:
        email = token_signer.loads(token, salt="password-reset", max_age=RESET_LINK_MINUTES * 60)
    except (BadSignature, SignatureExpired):
        flash("This reset link is invalid or has expired. Request a new one.")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        password = request.form["password"]
        if len(password) < 8:
            flash("Password must be at least 8 characters.")
        elif password != request.form["confirm"]:
            flash("Passwords do not match.")
        else:
            query("update users set pw=? where email=?", (generate_password_hash(password), email))
            commit()
            flash("Password updated. Log in with your new password.")
            return redirect(url_for("login"))
    return render_template("auth.html", mode="reset")


# ---------------------------------------------------------------- dashboard

@app.route("/dashboard")
@login_required
def dashboard():
    month = selected_month()
    user = query("select * from users where id=?", (session["uid"],)).fetchone()
    earned, spent, saved = month_totals(user["id"], month)
    budget = user["budget"] or 0

    rows = query("select category, sum(amount) as total from transactions "
                 "where user_id=? and kind='expense' and substr(date, 1, 7)=? "
                 "group by category order by total desc", (user["id"], month)).fetchall()
    categories = [(row["category"], int(row["total"])) for row in rows]
    legend, donut = category_legend(categories)
    recent = query("select * from transactions where user_id=? and substr(date, 1, 7)=? "
                   "order by date desc, id desc limit 6", (user["id"], month)).fetchall()

    return render_template(
        "dashboard.html", month=month, earned=earned, spent=spent, saved=saved,
        balance=earned - spent - saved, budget=budget,
        budget_used=round(spent / budget * 100) if budget else 0,
        savings_rate=round(saved / earned * 100) if earned else 0,
        legend=legend, donut=donut, history=monthly_history(user["id"], month),
        limits=category_limits(user["id"], dict(categories)),
        expense_categories=SECTIONS["expenses"]["categories"], recent=recent)


@app.route("/budget", methods=["POST"])
@login_required
def set_budget():
    amount = request.form.get("budget", "").strip()
    budget = 0 if amount == "" else positive_int(amount)
    if budget is None:
        flash("Enter a whole number for your monthly budget.")
    else:
        query("update users set budget=? where id=?", (budget, session["uid"]))
        commit()
        flash("Monthly budget saved." if budget else "Monthly budget removed.")
    return redirect(url_for("dashboard"))


@app.route("/category-budget", methods=["POST"])
@login_required
def set_category_budget():
    category = request.form.get("category")
    amount = request.form.get("amount", "").strip()
    limit = 0 if amount == "" else positive_int(amount)
    if category not in SECTIONS["expenses"]["categories"] or limit is None:
        flash("Choose a category and enter a whole number.")
        return redirect(url_for("dashboard"))
    query("delete from category_budgets where user_id=? and category=?", (session["uid"], category))
    if limit:
        query("insert into category_budgets (user_id, category, amount) values (?,?,?)",
              (session["uid"], category, limit))
    commit()
    flash(f"{category} budget saved." if limit else f"{category} budget removed.")
    return redirect(url_for("dashboard"))


# ------------------------------------------------- earnings, expenses, savings

def read_entry_form(info):
    """Return (amount, category, date, note) from the form, or None after saying what is wrong."""
    amount = positive_int(request.form.get("amount"))
    category = request.form.get("category")
    date = valid_date(request.form.get("date"))
    note = request.form.get("note", "").strip()[:80]
    if not amount:
        flash("Enter the amount as a whole number above zero.")
    elif category not in info["categories"] or not date:
        flash("Choose a category and a valid date.")
    else:
        return amount, category, date, note
    return None


def add_entry(info):
    values = read_entry_form(info)
    if not values:
        return
    amount, category, date, note = values
    query("insert into transactions (user_id, kind, category, amount, date, note) values (?,?,?,?,?,?)",
          (session["uid"], info["kind"], category, amount, date, note))
    commit()
    flash("Entry added.")
    if info["kind"] == "expense":
        warning = category_warning(session["uid"], category, date[:7])
        if warning:
            flash(warning)


def entries_page(section):
    info = SECTIONS[section]
    month = selected_month()
    if request.method == "POST":
        add_entry(info)
        return redirect(url_for(section, month=month))
    rows = query("select * from transactions where user_id=? and kind=? and substr(date, 1, 7)=? "
                 "order by date desc, id desc", (session["uid"], info["kind"], month)).fetchall()
    return render_template("entries.html", info=info, section=section, month=month, rows=rows,
                           total=sum(r["amount"] for r in rows))


@app.route("/earnings", methods=["GET", "POST"])
@login_required
def earnings():
    return entries_page("earnings")


@app.route("/expenses", methods=["GET", "POST"])
@login_required
def expenses():
    return entries_page("expenses")


@app.route("/savings", methods=["GET", "POST"])
@login_required
def savings():
    return entries_page("savings")


@app.route("/entry/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_entry(entry_id):
    entry = query("select * from transactions where id=? and user_id=?",
                  (entry_id, session["uid"])).fetchone() or abort(404)
    section = SECTION_OF_KIND[entry["kind"]]
    info = SECTIONS[section]
    if request.method == "POST":
        values = read_entry_form(info)
        if values:
            amount, category, date, note = values
            query("update transactions set amount=?, category=?, date=?, note=? where id=?",
                  (amount, category, date, note, entry_id))
            commit()
            flash("Entry updated.")
            if info["kind"] == "expense":
                warning = category_warning(session["uid"], category, date[:7])
                if warning:
                    flash(warning)
            return redirect(url_for(section, month=date[:7]))
    return render_template("edit.html", info=info, section=section, entry=entry)


@app.route("/entry/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    query("delete from transactions where id=? and user_id=?", (entry_id, session["uid"]))
    commit()
    flash("Entry deleted.")
    return redirect(request.referrer or url_for("dashboard"))


# -------------------------------------------------------------------- goals

@app.route("/goals", methods=["GET", "POST"])
@login_required
def goals():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:40]
        target = positive_int(request.form.get("target"))
        deadline = request.form.get("deadline", "").strip()
        if len(name) < 2 or not target:
            flash("Give the goal a name and a target amount.")
        elif deadline and not valid_date(deadline):
            flash("Choose a valid deadline, or leave it empty.")
        else:
            query("insert into goals (user_id, name, target, deadline) values (?,?,?,?)",
                  (session["uid"], name, target, deadline))
            commit()
            flash("Goal created.")
        return redirect(url_for("goals"))
    rows = query("select g.*, coalesce(sum(t.amount), 0) as saved from goals g "
                 "left join transactions t on t.goal_id = g.id "
                 "where g.user_id=? group by g.id order by g.id desc", (session["uid"],)).fetchall()
    goals_list = [{**dict(row), "saved": int(row["saved"]),
                   "percent": min(100, round(int(row["saved"]) / row["target"] * 100))} for row in rows]
    return render_template("goals.html", goals=goals_list)


def own_goal_or_404(goal_id):
    return query("select * from goals where id=? and user_id=?",
                 (goal_id, session["uid"])).fetchone() or abort(404)


@app.route("/goals/<int:goal_id>/add", methods=["POST"])
@login_required
def add_to_goal(goal_id):
    goal = own_goal_or_404(goal_id)
    amount = positive_int(request.form.get("amount"))
    if not amount:
        flash("Enter the amount as a whole number above zero.")
    else:
        query("insert into transactions (user_id, kind, category, amount, date, note, goal_id) "
              "values (?,?,?,?,?,?,?)",
              (session["uid"], "saving", "Goal", amount, dt.date.today().isoformat(), goal["name"], goal_id))
        commit()
        flash(f"Added {rupees(amount)} to {goal['name']}.")
    return redirect(url_for("goals"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    goal = own_goal_or_404(goal_id)
    # The money stays in your savings history, it just stops counting towards a goal.
    query("update transactions set goal_id=NULL where goal_id=? and user_id=?", (goal_id, session["uid"]))
    query("delete from goals where id=?", (goal_id,))
    commit()
    flash(f"Goal {goal['name']} deleted.")
    return redirect(url_for("goals"))


if __name__ == "__main__":
    app.run(debug=True)
