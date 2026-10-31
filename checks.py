"""'Needs a look': things in your money worth a second glance.

Each check returns alerts like
    {"kind": "Possible duplicate", "date": datetime, "amount": 42.0,
     "title": "Uber Eats charged twice", "detail": "..."}
The monthly routine asks Claude to investigate each one and only bother you about
the ones it can't explain. Run on its own (python checks.py) to print them.
"""

from collections import defaultdict
from datetime import timedelta
from statistics import median

from config import CONFIG

# Money moving to people or accounts rather than merchants: repeats and big
# amounts are normal here, so the merchant checks skip them.
TRANSFER_CATEGORIES = {
    "Rent & housing", "Other people", "Cash withdrawals", CONFIG["categories"]["investment"],
    *CONFIG["categories"]["excluded"], *CONFIG["categories"]["netted"], *CONFIG["sent_home"]["categories"],
}


# Recurring bills whose amount naturally varies a little each month.
BILL_CATEGORIES = {"Bills & utilities"}


def is_merchant_spend(t):
    return t["spent"] > 0 and t["category"] not in TRANSFER_CATEGORIES


def duplicate_charges(transactions, last_date, days=90, minimum=20):
    """Same merchant, same amount, same account, on the same or next day, when that
    exact amount is otherwise rare there (a daily $5.50 coffee isn't a duplicate)."""
    recent = sorted((t for t in transactions if is_merchant_spend(t) and t["spent"] >= minimum
                     and t["date"] > last_date - timedelta(days=days)), key=lambda t: t["date"])
    how_often = defaultdict(int)
    for t in recent:
        how_often[(t["account"], t["narration"], round(t["spent"], 2))] += 1
    alerts, seen = [], set()
    for i, a in enumerate(recent):
        for b in recent[i + 1:]:
            if (b["date"] - a["date"]).days > 1:
                break
            key = (a["account"], a["narration"], round(a["spent"], 2))
            if (key == (b["account"], b["narration"], round(b["spent"], 2)) and how_often[key] == 2
                    and id(b) not in seen):
                seen.add(id(b))
                alerts.append({
                    "kind": "Possible duplicate", "date": b["date"], "amount": b["spent"],
                    "title": f"{a['narration']} charged ${a['spent']:,.2f} twice",
                    "detail": f"{a['date']:%d %b} and {b['date']:%d %b} on {a['account']}. "
                              "Could be two real purchases, or a double charge to dispute.",
                })
    return alerts


def subscription_changes(transactions, last_date, days=60):
    """A charge that recurs roughly monthly and whose latest amount changed."""
    by_merchant = defaultdict(list)
    for t in transactions:
        if is_merchant_spend(t) and t["date"] > last_date - timedelta(days=400):
            by_merchant[t["narration"]].append(t)
    alerts = []
    for name, charges in by_merchant.items():
        charges.sort(key=lambda t: t["date"])
        if len(charges) < 3:
            continue
        gaps = [(b["date"] - a["date"]).days for a, b in zip(charges, charges[1:])]
        if not 25 <= median(gaps) <= 35:
            continue  # not a monthly subscription
        before, prev, latest = charges[-3], charges[-2], charges[-1]
        if latest["date"] <= last_date - timedelta(days=days):
            continue
        if latest["category"] in BILL_CATEGORIES:
            # Bills wobble a little, so compare with the usual amount instead.
            usual = median(t["spent"] for t in charges[:-1])
            if latest["spent"] - usual >= 20 and latest["spent"] > usual * 1.3:
                alerts.append({
                    "kind": "Bill jump", "date": latest["date"], "amount": latest["spent"],
                    "title": f"{name} bill up to ${latest['spent']:,.2f}",
                    "detail": f"Usually about ${usual:,.2f}; ${latest['spent']:,.2f} on "
                              f"{latest['date']:%d %b} (+${latest['spent'] - usual:,.2f}).",
                })
            continue
        if abs(prev["spent"] - before["spent"]) > 0.05 * before["spent"]:
            continue  # amounts already varied (e.g. groceries), so not a fixed-price subscription
        rise = latest["spent"] - prev["spent"]
        if rise >= 2 and rise / prev["spent"] > 0.05:
            alerts.append({
                "kind": "Price rise", "date": latest["date"], "amount": latest["spent"],
                "title": f"{name} went up to ${latest['spent']:,.2f}",
                "detail": f"Was ${prev['spent']:,.2f} on {prev['date']:%d %b}; "
                          f"${latest['spent']:,.2f} on {latest['date']:%d %b} (+${rise:,.2f}/month).",
            })
    return alerts


def big_new_merchants(transactions, last_date, days=60, threshold=250):
    """A large payment to somewhere you'd never paid before."""
    cutoff = last_date - timedelta(days=days)
    seen_before = {t["narration"] for t in transactions if t["date"] <= cutoff}
    alerts = []
    for t in transactions:
        if is_merchant_spend(t) and t["date"] > cutoff and t["spent"] >= threshold and t["narration"] not in seen_before:
            alerts.append({
                "kind": "New merchant", "date": t["date"], "amount": t["spent"],
                "title": f"${t['spent']:,.2f} at {t['narration']}",
                "detail": f"First payment there ({t['date']:%d %b}, {t['account']}), filed as {t['category']}.",
            })
    return alerts


def category_spikes(months, partial_month, min_jump=300, ratio=1.5):
    """Last full month's spending in a category well above its 6-month average."""
    complete = [m for m in months if m != partial_month]
    if len(complete) < 4:
        return []
    last, before = complete[-1], complete[-7:-1]
    alerts = []
    for cat, spent in months[last]["cats"].items():
        if cat in TRANSFER_CATEGORIES:
            continue
        usual = sum(months[m]["cats"].get(cat, 0) for m in before) / len(before)
        if spent - usual >= min_jump and spent > usual * ratio:
            alerts.append({
                "kind": "Spending spike", "date": None, "amount": spent,
                "title": f"{cat}: ${spent:,.0f} in {last}",
                "detail": f"Usually about ${usual:,.0f}/month over the 6 months before "
                          f"(+${spent - usual:,.0f}).",
            })
    return alerts


def balance_breaks(bank_rows):
    """Statements that don't join up: previous balance + amount should equal the new balance."""
    alerts, last = [], {}
    for row in bank_rows:
        account, amount, balance = row["account"], float(row["amount_signed"]), float(row["balance"])
        if account in last:
            expected = last[account] + amount
            if abs(expected - balance) > 0.01:
                alerts.append({
                    "kind": "Balance gap", "date": None, "amount": abs(expected - balance),
                    "title": f"{account}: ${abs(expected - balance):,.2f} unaccounted for around {row['date']}",
                    "detail": "The balance doesn't follow from the previous transaction, usually "
                              "because a few transactions are missing between two statements.",
                })
        last[account] = balance
    return alerts


def find_alerts(transactions, months, partial_month, bank_rows):
    last_date = max(t["date"] for t in transactions)
    alerts = (balance_breaks(bank_rows) + duplicate_charges(transactions, last_date)
              + subscription_changes(transactions, last_date) + big_new_merchants(transactions, last_date)
              + category_spikes(months, partial_month))
    return alerts


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import analyze
    tx = analyze.load_transactions(analyze.load_rules())
    months = analyze.monthly_totals(tx)
    for a in find_alerts(tx, months, None, analyze.bank_rows()):
        print(f"[{a['kind']}] {a['title']}\n    {a['detail']}")
