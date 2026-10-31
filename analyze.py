"""Money Tracker: turns your statements into a spending and net worth report.

Usage:  .venv\\Scripts\\python analyze.py
Reads ledger.csv (CommBank), your Wise, CMC and IOOF statements from the data folder
named in config.toml, applies any overrides in your rules file, prints the key
insights and writes report.html (designed in report_template.html).
Without a config.toml it uses config.example.toml and the fake data from
generate_sample.py.

CommBank transaction-summary PDFs newer than the ledger are added on top of it,
using the category the ledger already gives that merchant.

Money sent from CommBank to Wise is replaced by what Wise actually paid out, so
nothing is counted twice. Money sent to CMC is counted as invested, not spent.
"""

import argparse
import calendar
import csv
import json
import statistics
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path

import checks
import commbank
import holdings
import wise
from config import CONFIG

# Path(__file__).parent = "the folder this script lives in", so the script
# works no matter which folder you run it from.
HERE = Path(__file__).parent
RULES_FILE = CONFIG["rules_file"]
TEMPLATE_FILE = HERE / "report_template.html"
REPORT_FILE = HERE / "report.html"

# Your real data stays where it is, outside this project (and outside git).
FINANCE_DIR = CONFIG["data_dir"]
LEDGER_FILE = FINANCE_DIR / "ledger.csv"
STATEMENTS_DIR = FINANCE_DIR / "Statements"

# Money moving between your own accounts isn't spending or income,
# so these categories are left out of every total.
EXCLUDED_CATEGORIES = set(CONFIG["categories"]["excluded"])

# Counted separately from spending.
INVESTMENT_CATEGORY = CONFIG["categories"]["investment"]

# Money coming back in these categories reduces what you spent there,
# instead of counting as income (e.g. a partner paying you back).
NETTED_CATEGORIES = set(CONFIG["categories"]["netted"])

# Ledger categories shown under a different name.
RENAMED_CATEGORIES = CONFIG["categories"]["renamed"]

# How your own name appears on transfers between your accounts.
OWN_NAMES = [n.upper() for n in CONFIG["own_names"]]

# Who you send money to through Wise, and what it counts as.
WISE_RECIPIENTS = {k.upper(): v for k, v in CONFIG["wise_recipients"].items()}

# Categories for the "money sent home" insight, and its wording.
SENT_HOME = CONFIG["sent_home"]
REPORT_TEXT = CONFIG["report"]

# Wise's own card categories, mapped onto the ledger's.
WISE_CARD_CATEGORIES = {
    "Eating": "Eating out & cafes",
    "Groceries": "Groceries",
    "Trips": "Travel",
    "Transport": "Transport",
    "Bills": "Bills & utilities",
    "Personal": "Health & fitness",
    "Entertainment": "Entertainment",
}


# ---------- Reading and categorising ----------

def load_rules():
    """Read rules.csv into a list of (KEYWORD, category) pairs."""
    with open(RULES_FILE, newline="", encoding="utf-8") as f:
        return [(row["keyword"].upper(), row["category"]) for row in csv.DictReader(f)]


def categorise(description, existing_category, rules):
    """A matching rule in rules.csv wins; otherwise keep the existing category."""
    text = description.upper()
    for keyword, category in rules:
        if keyword in text:
            return category
    return RENAMED_CATEGORIES.get(existing_category, existing_category) or "Uncategorised"


def is_own_name(text):
    return any(name in text for name in OWN_NAMES)


def is_to_wise(row):
    text = row["description"].upper()
    return "WISE" in text or (is_own_name(text) and "CMC" not in text)


def find_wise_deposit(date, amount, deposits, used):
    """Index of the Wise deposit matching a CommBank payment (same amount, or amount
    plus Wise's top-up fee, within 5 days), or None if Wise has no record of it."""
    for i, d in enumerate(deposits):
        if i in used or abs((d["date"] - date).days) > 5:
            continue
        if min(abs(d["amount"] - amount), abs(d["amount"] + d["fee"] - amount)) < 1.5:
            return i
    return None


def wise_category(t):
    if t["kind"] == "sent":
        return WISE_RECIPIENTS.get(t["who"].upper(), "Other people")
    return WISE_CARD_CATEGORIES.get(t["category"], "Shopping & other")


def merchant_key(description):
    """'NETFLIX.COM*4K2QX81 LOS GATOS CA USA | Card ...' -> 'LOS GATOS CA': the first
    line without reference numbers, so repeat visits to a merchant look the same."""
    words = [w for w in description.split(" | ")[0].upper().split() if not any(c.isdigit() for c in w)]
    return " ".join(words[:3])


def newer_commbank_rows(ledger_rows):
    """Rows from CommBank PDFs dated after each account's last ledger day, with the
    merchant name and category the ledger last used for that merchant."""
    last_day = {}
    known = {}
    for row in ledger_rows:
        last_day[row["account"]] = max(last_day.get(row["account"], ""), row["date"])
        known[merchant_key(row["description"])] = row
    # On the ledger's last day, some transactions may have posted after it was made.
    # Count what the ledger already has that day so only the missing ones are added.
    on_last_day = defaultdict(int)
    for row in ledger_rows:
        if row["date"] == last_day[row["account"]]:
            on_last_day[(row["account"], row["date"], float(row["amount_signed"]))] += 1
    rows = []
    for row in commbank.load_all(FINANCE_DIR, STATEMENTS_DIR):
        day = last_day.get(row["account"], "")
        if row["date"] < day:
            continue
        if row["date"] == day:
            key = (row["account"], row["date"], float(row["amount_signed"]))
            if on_last_day[key]:
                on_last_day[key] -= 1
                continue
        match = known.get(merchant_key(row["description"]))
        row["merchant"] = match["merchant"] if match else row["description"].split(" | ")[0]
        row["category"] = match["category"] if match else ""
        rows.append(row)
    return rows


@cache
def bank_rows():
    """All CommBank rows: the ledger, then anything newer from the PDFs."""
    with open(LEDGER_FILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows + newer_commbank_rows(rows)


def make_transaction(date, account, narration, category, amount, kind=""):
    """amount is signed: negative for money out, positive for money in."""
    if category in NETTED_CATEGORIES:
        # Money back reduces spending in this category rather than counting as income.
        spent, received = -amount, 0.0
    else:
        spent, received = max(-amount, 0.0), max(amount, 0.0)
    return {"date": date, "account": account, "narration": narration, "category": category,
            "spent": spent, "received": received, "kind": kind}


def load_transactions(rules):
    """Read the CommBank ledger and Wise statements; return a list of transaction dicts."""
    wise_txs = wise.load_all(FINANCE_DIR, STATEMENTS_DIR)
    deposits = [t for t in wise_txs if t["kind"] == "added"]
    # Transfers paid straight from a CommBank card skip the Wise balance, so the
    # CommBank payment matches the transfer itself rather than a deposit.
    sends = [t for t in wise_txs if t["kind"] == "sent"]
    used, used_sends = set(), set()

    transactions = []
    for row in bank_rows():
        category = categorise(row["description"], row["category"], rules)
        if category in EXCLUDED_CATEGORIES:
            continue
        amount = float(row["amount_signed"])
        date = datetime.strptime(row["date"], "%Y-%m-%d")
        text = row["description"].upper()

        if amount < 0 and is_own_name(text) and "CMC" in text:
            category = INVESTMENT_CATEGORY
        elif amount < 0 and is_to_wise(row):
            hit = find_wise_deposit(date, -amount, deposits, used)
            if hit is not None:
                # Skip it: the Wise transactions below show where it really went.
                used.add(hit)
                continue
            hit = find_wise_deposit(date, -amount, sends, used_sends)
            if hit is not None:
                used_sends.add(hit)
                continue

        narration = (row["merchant"] or row["description"]).replace(" (transfers)", "")
        transactions.append(make_transaction(date, row["account"], narration, category, amount))

    for t in wise_txs:
        if t["kind"] not in ("card", "sent") or not t["amount"]:
            continue
        # A negative card amount is a refund, so it becomes money in.
        category = categorise(t["who"], wise_category(t), rules)
        transactions.append(make_transaction(t["date"], "Wise", t["who"], category, -t["amount"], t["kind"]))
    return transactions


# ---------- Summaries ----------

def monthly_totals(transactions):
    """{"2026-03": {"in": .., "out": .., "cats": {category: spent}}}, investments left out."""
    months = defaultdict(lambda: {"in": 0.0, "out": 0.0, "cats": defaultdict(float)})
    for t in transactions:
        if t["category"] == INVESTMENT_CATEGORY:
            continue
        m = months[f"{t['date']:%Y-%m}"]
        m["in"] += t["received"]
        m["out"] += t["spent"]
        m["cats"][t["category"]] += t["spent"]
    return dict(sorted(months.items()))


def average(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def dollars(x):
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def percent(x):
    return f"{x:.0%}"


def where_money_sits():
    """Latest balance of every account you can use, for the net worth panel.
    Super is locked until retirement, so it is kept apart (see sup) rather than mixed in."""
    bank = holdings.bank_balances(bank_rows())
    wise_bal, wise_date = holdings.wise_balance(FINANCE_DIR, STATEMENTS_DIR)
    cmc = holdings.cmc(FINANCE_DIR, STATEMENTS_DIR)
    sup = holdings.super_fund(FINANCE_DIR, STATEMENTS_DIR)
    cmc_value = sum(h["value"] for h in cmc["holdings"]) + cmc["cash"]
    parts = [
        {"name": "Savings", "value": bank["NetBank Saver"][-1][1], "asAt": bank["NetBank Saver"][-1][0], "group": "Cash"},
        {"name": "Shares (CMC)", "value": cmc_value, "asAt": f"{cmc['as_at']:%d %b %Y}", "group": "Invested"},
        {"name": "Everyday", "value": bank["Smart Access"][-1][1], "asAt": bank["Smart Access"][-1][0], "group": "Cash"},
        {"name": "Wise", "value": wise_bal, "asAt": f"{wise_date:%d %b %Y}", "group": "Cash"},
    ]
    return parts, bank, cmc, sup


# A source older than this needs a fresh download (see /update-statements).
STALE_AFTER_DAYS = 31


def data_freshness(bank, parts, sup):
    """How current each source is: [{"source", "asAt", "stale"}], oldest first."""
    as_at = lambda name: next(p["asAt"] for p in parts if p["name"] == name)
    sources = {
        "CommBank": min(days[-1][0] for days in bank.values()),
        "Wise": datetime.strptime(as_at("Wise"), "%d %b %Y").strftime("%Y-%m-%d"),
        "CMC": datetime.strptime(as_at("Shares (CMC)"), "%d %b %Y").strftime("%Y-%m-%d"),
        "IOOF super": f"{sup['as_at']:%Y-%m-%d}",
    }
    today = datetime.now()
    return sorted(({"source": s, "asAt": d,
                    "stale": (today - datetime.strptime(d, "%Y-%m-%d")).days > STALE_AFTER_DAYS}
                   for s, d in sources.items()), key=lambda f: f["asAt"])


def build_insights(transactions, months, last_date, parts, cmc, sup):
    """Plain-English findings. Averages use complete months only."""
    partial = last_date.day < calendar.monthrange(last_date.year, last_date.month)[1]
    current = f"{last_date:%Y-%m}"
    complete = [m for m in months if not (partial and m == current)]
    last6, prev6, last12 = complete[-6:], complete[-12:-6], complete[-12:]
    spend = lambda ms: average(months[m]["out"] for m in ms)
    income = lambda ms: average(months[m]["in"] for m in ms)
    insights = []

    cash = sum(p["value"] for p in parts if p["group"] == "Cash")
    insights.append({
        "label": "Cash buffer", "value": f"{cash / spend(last6):.1f} months",
        "text": f"Your {dollars(cash)} in cash (everyday, savings and Wise) would cover "
                f"{cash / spend(last6):.1f} months at your recent spending of {dollars(spend(last6))}/month.",
        "tone": "warn" if cash / spend(last6) < 3 else "good"})

    rate6 = 1 - spend(last6) / income(last6)
    rate_all = 1 - sum(months[m]["out"] for m in complete) / sum(months[m]["in"] for m in complete)
    insights.append({
        "label": "Savings rate, last 6 months", "value": percent(rate6),
        "text": f"You kept {percent(rate6)} of what came in over the last 6 full months "
                f"({percent(rate_all)} since {datetime.strptime(complete[0], '%Y-%m'):%b %Y}). Spending was higher than income in "
                f"{sum(months[m]['out'] > months[m]['in'] for m in complete)} of {len(complete)} months.",
        "tone": "good" if rate6 >= 0.2 else "warn"})

    change = spend(last6) / spend(prev6) - 1 if prev6 else 0
    insights.append({
        "label": "Monthly spending trend", "value": f"{'+' if change >= 0 else '−'}{abs(change):.0%}",
        "text": f"You spent {dollars(spend(last6))}/month over the last 6 months, "
                f"vs {dollars(spend(prev6))}/month in the 6 before.",
        "tone": "warn" if change > 0.05 else "good"})

    cats12 = defaultdict(float)
    for m in last12:
        for c, v in months[m]["cats"].items():
            cats12[c] += v
    total12 = sum(cats12.values())
    top = sorted(cats12.items(), key=lambda kv: -kv[1])[:3]
    insights.append({
        "label": "Biggest costs, last 12 months", "value": percent(sum(v for _, v in top) / total12),
        "text": "Three categories make up " + percent(sum(v for _, v in top) / total12) + " of spending: "
                + ", ".join(f"{c} {dollars(v / len(last12))}/mo" for c, v in top) + ".",
        "tone": "neutral"})

    rent = cats12.get("Rent & housing", 0) / max(sum(months[m]["in"] for m in last12), 1)
    insights.append({
        "label": "Rent share of income", "value": percent(rent),
        "text": f"Rent took {percent(rent)} of everything that came in over the last 12 months "
                f"({dollars(cats12.get('Rent & housing', 0) / len(last12))}/month on average).",
        "tone": "warn" if rent > 0.3 else "neutral"})

    if SENT_HOME["categories"]:
        sent_home = sum(cats12.get(c, 0) for c in SENT_HOME["categories"])
        insights.append({
            "label": SENT_HOME["label"], "value": f"{dollars(sent_home / len(last12))}/mo",
            "text": f"{SENT_HOME['description']} came to {dollars(sent_home)} over "
                    f"12 months, {percent(sent_home / total12)} of all spending.",
            "tone": "neutral"})

    last3, prev3 = complete[-3:], complete[-6:-3]
    rises = sorted(((average(months[m]["cats"].get(c, 0) for m in last3)
                     - average(months[m]["cats"].get(c, 0) for m in prev3), c) for c in cats12), reverse=True)
    rise, cat = rises[0]
    insights.append({
        "label": "Fastest-rising category", "value": cat,
        "text": f"{cat} is up {dollars(rise)}/month over the last 3 months compared with the 3 before.",
        "tone": "warn"})

    if partial:
        day = last_date.day
        so_far = sum(t["spent"] for t in transactions
                     if f"{t['date']:%Y-%m}" == current and t["category"] != INVESTMENT_CATEGORY)
        usual = average(sum(t["spent"] for t in transactions
                            if f"{t['date']:%Y-%m}" == m and t["date"].day <= day
                            and t["category"] != INVESTMENT_CATEGORY) for m in last6)
        insights.append({
            "label": f"{last_date:%B} so far", "value": dollars(so_far),
            "text": f"Spent by {day} {last_date:%b}, vs {dollars(usual)} by the same day in a typical recent month "
                    f"({'over' if so_far > usual else 'under'} by {dollars(abs(so_far - usual))})."
                    + (f" {REPORT_TEXT['salary_note']}" if REPORT_TEXT.get("salary_note") else ""),
            "tone": "warn" if so_far > usual else "good"})

    invested = sum(t["spent"] for t in transactions if t["category"] == INVESTMENT_CATEGORY)
    cmc_value = sum(h["value"] for h in cmc["holdings"]) + cmc["cash"]
    gain = cmc_value - invested
    insights.append({
        "label": "CMC shares", "value": f"{'+' if gain >= 0 else '−'}{dollars(abs(gain))}",
        "text": f"{dollars(invested)} put in, worth {dollars(cmc_value)} at the last statement "
                f"({cmc['as_at']:%d %b %Y}).",
        "tone": "good" if gain >= 0 else "warn"})

    if sup["returns"] is not None:
        insights.append({
            "label": "Super returns, last 12 months", "value": dollars(sup["returns"]),
            "text": f"Of the {dollars(sup['closing'] - sup['opening'])} your super grew to {sup['as_at']:%d %b %Y}, "
                    f"{dollars(sup['paid_in'])} was contributions paid in and {dollars(sup['returns'])} was "
                    f"investment returns, less {dollars(sup['deducted'])} in fees"
                    + (f"; {dollars(sup['uninvested'])} more is waiting to be invested." if sup["uninvested"] > 1 else "."),
            "tone": "good" if sup["returns"] >= 0 else "warn"})
    return insights, (current if partial else None)


# ---------- Output ----------

def build_focus(transactions, months, last_date, parts, bank_history):
    """The few findings worth acting on, each with one next step. Works from complete
    months so a half-finished month can't skew it."""
    partial = last_date.day < calendar.monthrange(last_date.year, last_date.month)[1]
    complete = [m for m in months if not (partial and m == f"{last_date:%Y-%m}")]
    last6, last12 = complete[-6:], complete[-12:]
    if not last6:
        return []
    per_month = lambda cats, ms: sum(months[m]["cats"].get(c, 0) for m in ms for c in cats) / len(ms)
    sent_home = set(SENT_HOME["categories"])
    own_cats = {WISE_RECIPIENTS[n] for n in OWN_NAMES if n in WISE_RECIPIENTS} & sent_home
    # Costs nobody chooses month to month, money moved elsewhere, and money that nets out.
    not_a_choice = {"Rent & housing", INVESTMENT_CATEGORY} | sent_home | NETTED_CATEGORIES
    focus = []

    # 1. Is regular income covering spending? The median ignores one-off bonuses.
    pay = statistics.median(months[m]["in"] for m in last12)
    spend = average(months[m]["out"] for m in last6)
    gap = spend - pay
    since = datetime.strptime(last6[0], "%Y-%m")
    big = sorted((t for t in transactions if t["spent"] >= 500 and t["category"] not in not_a_choice
                  and t["date"] >= since), key=lambda t: -t["spent"])
    why = f"Regular income is about {dollars(pay)} a month. Spending averaged {dollars(spend)} over the last 6 months."
    if own_cats and per_month(own_cats, last6) >= 1:
        why += f" {dollars(per_month(own_cats, last6))} a month of that went to your own overseas account."
    if big:
        why += f" {len(big)} one-off payments of $500 or more came to {dollars(sum(t['spent'] for t in big))}."
    focus.append({
        "label": "Spending vs regular income",
        "value": f"{dollars(gap)}/month over" if gap > 0 else f"{dollars(-gap)}/month under",
        "why": why, "tone": "warn" if gap > 0 else "good",
        "next": "Choose a monthly spending limit, then use the cards below to see where the gap could close."
                if gap > 0 else "Keep doing what you're doing."})

    # 2. How long would the cash last, and is it growing?
    cash = sum(p["value"] for p in parts if p["group"] == "Cash")
    why = f"{dollars(cash)} in cash covers {cash / spend:.1f} months of recent spending"
    essentials = per_month(REPORT_TEXT.get("essentials", []), last12)
    why += f", or {cash / essentials:.1f} months of essentials." if essentials else "."
    history = bank_history.get("NetBank Saver", [])
    start = f"{last_date - timedelta(days=91):%Y-%m-%d}"
    before = [b for d, b in history if d <= start]
    falling = bool(before and history[-1][1] < before[-1])
    if before:
        moved = [float(r["amount_signed"]) for r in bank_rows()
                 if r["account"] == "NetBank Saver" and r["date"] > start]
        why += (f" Savings went from {dollars(before[-1])} to {dollars(history[-1][1])} in 3 months: "
                f"{dollars(sum(a for a in moved if a > 0))} in, {dollars(-sum(a for a in moved if a < 0))} out.")
    focus.append({
        "label": "Cash buffer", "value": f"{cash / spend:.1f} months", "why": why,
        "tone": "warn" if cash / spend < 3 else "good",
        "next": "Choose a balance savings shouldn't drop below, and check each withdrawal against it."
                if falling else "Choose the buffer you want, so you can tell when it's enough."})

    # 3. The biggest repeat habit: small enough to change, frequent enough to matter.
    since = datetime.strptime(last12[0], "%Y-%m")
    habits = defaultdict(list)
    skip = not_a_choice | set(REPORT_TEXT.get("essentials", []))
    for t in transactions:
        if t["spent"] > 0 and t["kind"] != "sent" and t["category"] not in skip and t["date"] >= since:
            habits[t["narration"]].append(t)
    habits = {k: v for k, v in habits.items() if len(v) >= 24}
    if habits:
        name, rows = max(habits.items(), key=lambda kv: sum(t["spent"] for t in kv[1]))
        total = sum(t["spent"] for t in rows)
        category = rows[0]["category"]
        why = (f"{len(rows)} payments in {len(last12)} months, about {dollars(total / len(rows))} each, "
               f"{dollars(total)} in all. {category} comes to {dollars(per_month([category], last12))} a month")
        groceries = per_month(["Groceries"], last12)
        why += f", against {dollars(groceries)} on groceries." if groceries and category != "Groceries" else "."
        focus.append({
            "label": "Biggest repeat habit", "value": f"{name}: {dollars(total / len(last12))}/month", "why": why,
            "tone": "neutral",
            "next": f"Choose a monthly limit for {name}. Each payment skipped keeps about {dollars(total / len(rows))}."})

    # 4. Money sent home: often large, and often uneven.
    if sent_home:
        total = sum(months[m]["cats"].get(c, 0) for m in last12 for c in sent_home)
        each = [sum(months[m]["cats"].get(c, 0) for c in sent_home) for m in last12]
        own = sum(months[m]["cats"].get(c, 0) for m in last12 for c in own_cats)
        spent12 = sum(months[m]["out"] for m in last12)
        why = (f"{dollars(total)} over {len(last12)} months, {percent(total / spent12)} of spending. "
               f"It ranged from {dollars(min(each))} to {dollars(max(each))} a month.")
        next_step = "Choose a regular monthly amount, so it stops swinging your cash balance."
        if own:
            why += f" {dollars(own)} of it went to your own account, so it's still yours, but the tracker counts it as spent."
            next_step += " Add your overseas account's balance so your total is complete."
        focus.append({"label": SENT_HOME["label"], "value": f"{dollars(total / len(last12))}/month", "why": why,
                      "tone": "neutral", "next": next_step})
    return focus


def build_report_data(transactions, months, insights, partial_month, parts, bank, cmc, sup, last_date):
    wise_rows = [t for t in transactions if t["account"] == "Wise"]
    recipients, card = defaultdict(lambda: [0.0, 0]), defaultdict(lambda: [0.0, 0])
    for t in wise_rows:
        group = recipients[t["narration"]] if t["kind"] == "sent" else card[t["category"]]
        group[0] += t["spent"] - t["received"]
        group[1] += 1
    wise_bal = next(p["value"] for p in parts if p["name"] == "Wise")
    return {
        "asOf": f"{last_date:%Y-%m-%d}",
        "partialMonth": partial_month,
        "investmentCategory": INVESTMENT_CATEGORY,
        "nettedCategories": sorted(NETTED_CATEGORIES),
        "transferCategories": sorted(checks.TRANSFER_CATEGORIES),
        # Wording from config.toml, so the template holds nothing personal.
        "text": {"superFund": REPORT_TEXT["super_fund"], "balancesNote": REPORT_TEXT["balances_note"],
                 "nettedNote": REPORT_TEXT["netted_note"]},
        "toCheck": REPORT_TEXT.get("to_check", []),
        "networth": parts,
        "insights": insights,
        # [date, account, description, category, signed amount]
        "tx": [[f"{t['date']:%Y-%m-%d}", t["account"], t["narration"], t["category"],
                round(t["received"] - t["spent"], 2)]
               for t in sorted(transactions, key=lambda t: t["date"])],
        "balances": {a: [[d, b] for d, b in days] for a, days in bank.items()},
        "wise": {
            "balance": wise_bal,
            "recipients": sorted(([k, round(v[0]), v[1]] for k, v in recipients.items()), key=lambda r: -r[1]),
            "card": sorted(([k, round(v[0]), v[1]] for k, v in card.items()), key=lambda r: -r[1]),
        },
        "cmc": {
            "asAt": f"{cmc['as_at']:%d %b %Y}", "cash": cmc["cash"], "holdings": cmc["holdings"],
            "invested": sum(t["spent"] for t in transactions if t["category"] == INVESTMENT_CATEGORY),
        },
        "super": {"closing": sup["closing"], "opening": sup["opening"],
                  "asAt": f"{sup['as_at']:%d %b %Y}", "openingDate": f"{sup['opening_date']:%d %b %Y}",
                  "paidIn": sup["paid_in"], "deducted": sup["deducted"], "returns": sup["returns"],
                  "uninvested": sup["uninvested"]},
    }


def write_report(data, path=REPORT_FILE):
    """Drop the numbers into the HTML template and save the report."""
    html = TEMPLATE_FILE.read_text(encoding="utf-8").replace("__DATA__", json.dumps(data))
    path.write_text(html, encoding="utf-8")


def print_freshness(freshness):
    print("\nData up to:")
    for f in freshness:
        flag = f"   <- older than {STALE_AFTER_DAYS} days, download a new statement" if f["stale"] else ""
        print(f"  {f['source']:<12} {f['asAt']}{flag}")


def print_summary(parts, sup, insights):
    usable = sum(p["value"] for p in parts)
    print(f"\nMoney you can use: {dollars(usable)}")
    for p in parts:
        print(f"  {p['name']:<14} {dollars(p['value']):>10}   ({p['group'].lower()}, as at {p['asAt']})")
    print(f"Retirement (super): {dollars(sup['closing'])}   (locked, as at {sup['as_at']:%d %b %Y})")
    print(f"Total including super: {dollars(usable + sup['closing'])}")
    print("\nInsights:")
    for i in insights:
        print(f"  • {i['label']}: {i['value']}\n      {i['text']}")


def print_alerts(alerts):
    print(f"\nNeeds a look ({len(alerts)}):" if alerts else "\nNeeds a look: nothing unusual.")
    for a in alerts:
        print(f"  [{a['kind']}] {a['title']}\n      {a['detail']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the money report from your statements.")
    parser.add_argument("--out", type=Path, default=REPORT_FILE, help="where to save the report")
    parser.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    args = parser.parse_args(argv)
    # Windows falls back to an old character set when output is redirected (e.g. to a file),
    # which can't print "−" or "•". UTF-8 always can.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    transactions = load_transactions(load_rules())
    if not transactions:
        print(f"No transactions found in {LEDGER_FILE}")
        return
    months = monthly_totals(transactions)
    last_date = max(t["date"] for t in transactions)
    parts, bank, cmc, sup = where_money_sits()
    insights, partial_month = build_insights(transactions, months, last_date, parts, cmc, sup)
    freshness = data_freshness(bank, parts, sup)
    focus = build_focus(transactions, months, last_date, parts, bank)
    alerts = checks.find_alerts(transactions, months, partial_month, bank_rows())
    print_freshness(freshness)
    print_alerts(alerts)
    print_summary(parts, sup, insights)
    print("\nWhat to focus on:")
    for f in focus:
        print(f"  • {f['label']}: {f['value']}\n      {f['why']}\n      Next: {f['next']}")
    data = build_report_data(transactions, months, insights, partial_month, parts, bank, cmc, sup, last_date)
    data["freshness"] = freshness
    data["focus"] = focus
    data["alerts"] = [{**a, "date": f"{a['date']:%Y-%m-%d}" if a["date"] else None} for a in alerts]
    write_report(data, args.out)
    print(f"\nReport saved to {args.out}")
    if not args.no_open:
        webbrowser.open(args.out.resolve().as_uri())


# This line means "only run main() when the file is run directly".
if __name__ == "__main__":
    main()
