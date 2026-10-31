"""Makes a realistic, entirely made-up set of statements in sample_data/, in the same
formats the real readers expect, so anyone can run the tracker (and the tests)
without real bank data.

Usage:  python generate_sample.py [--end YYYY-MM-DD] [--out sample_data]

It writes what a real month-end folder looks like:
    ledger.csv                         CommBank history (already categorised)
    SmartAccess_..._to_....pdf         CommBank transaction summaries newer than the ledger
    NetBankSaver_..._to_....pdf
    Wise_AUD_..._to_....xlsx           Wise balance statement (has running balances)
    Wise_..._to_....csv                Wise transaction history (overlaps the xlsx)
    CMC_ProfitAndLoss_<date>.csv       CMC Invest holdings
    CMC_Accounts_<date>.csv            CMC Invest cash
    Super_IOOF_TransactionStatement_..._to_....pdf

Every number comes from a fixed random seed. A few things are planted so each
"Needs a look" check has something to find: a double charge, a price rise, a bill
jump, a big first-time purchase, a travel spike and a transaction missing from the
ledger. The same end date always gives the same files.
"""

import argparse
import csv
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import openpyxl
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

SEED = 7
ME = "ALEX MORGAN"
PARTNER = "RILEY CHEN"
LEDGER_COLUMNS = ["date", "account", "description", "merchant", "category", "amount_signed", "direction", "balance"]

# (description on the statement, merchant name, category, low, high)
GROCERIES = [("WOOLWORTHS 3318 FITZROY AU", "Woolworths", "Groceries", 30, 160),
             ("COLES 0764 RICHMOND AU", "Coles", "Groceries", 25, 140),
             ("ALDI STORES CARLTON AU", "Aldi", "Groceries", 20, 90)]
EATING = [("UBER *EATS HELP.UBER.COM AU", "Uber Eats", "Eating out & cafes", 22, 58),
          ("GRILL'D CARLTON AU", "Grill'd", "Eating out & cafes", 18, 42),
          ("SUSHI SUSHI FLINDERS AU", "Sushi Sushi", "Eating out & cafes", 12, 30),
          ("THE CARLTON WINE ROOM AU", "Carlton Wine Room", "Eating out & cafes", 60, 150)]
SHOPPING = [("KMART 1042 RICHMOND AU", "Kmart", "Shopping & other", 12, 90),
            ("JB HI-FI RICHMOND AU", "JB Hi-Fi", "Shopping & other", 25, 240),
            ("AMAZON AU MARKETPLACE AU", "Amazon AU", "Shopping & other", 15, 120),
            ("BOOKTOPIA PTY LTD AU", "Booktopia", "Shopping & other", 18, 55)]


def money(x):
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def month_starts(start, end):
    d = start.replace(day=1)
    while d <= end:
        yield d
        d = (d + timedelta(days=32)).replace(day=1)


def last_day(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


class Bank:
    """Collects CommBank and Wise events, then writes them in each provider's format."""

    def __init__(self, start, end, rng):
        self.start, self.end, self.rng = start, end, rng
        self.rows = []    # CommBank: dicts with date, account, description, merchant, category, amount
        self.wise = []    # Wise: dicts with id, date, kind, who, amount, fee, category, funded_by_card
        self.next_id = 50001

    def cb(self, d, account, description, merchant, category, amount, card=False):
        if self.start <= d <= self.end:
            self.rows.append({"date": d, "account": account, "description": description, "merchant": merchant,
                              "category": category, "amount": round(amount, 2), "card": card})

    def spend(self, d, choice, low=None, high=None, amount=None):
        description, merchant, category, lo, hi = choice
        value = amount if amount is not None else self.rng.uniform(low or lo, high or hi)
        self.cb(d, "Smart Access", description, merchant, category, -value, card=True)

    def wise_tx(self, d, kind, who, amount, fee=0.0, category="", funded_by_card=False):
        if not self.start <= d <= self.end:
            return
        prefix = "CARD" if kind == "card" else "TRANSFER"
        self.wise.append({"id": f"{prefix}-{self.next_id}", "date": d, "kind": kind, "who": who,
                          "amount": round(amount, 2), "fee": round(fee, 2), "category": category,
                          "funded_by_card": funded_by_card,
                          "time": datetime.combine(d, time(9 + self.next_id % 10, self.next_id % 60))})
        self.next_id += 1


def make_events(start, end, rng):
    """Everything that happens in the made-up life, day by day."""
    b = Bank(start, end, rng)
    # The last complete month: this one if the data ends on its last day, otherwise the one before.
    last_full = end.replace(day=1) if end == last_day(end) else (end.replace(day=1) - timedelta(days=1)).replace(day=1)

    for m in month_starts(start, end):
        months_in = (m.year - start.year) * 12 + m.month - start.month
        salary = 3450.00 if months_in < 12 else 3610.00
        for pay_day in (m.replace(day=15), last_day(m)):
            b.cb(pay_day, "Smart Access", "SALARY ACME ANALYTICS PTY LTD", "Acme Analytics", "Salary", salary)
            b.cb(pay_day + timedelta(days=1), "Smart Access", "TRANSFER TO NETBANK SAVER", "NetBank Saver",
                 "Savings transfer", -500)
            b.cb(pay_day + timedelta(days=1), "NetBank Saver", "TRANSFER FROM SMART ACCESS", "Smart Access",
                 "Savings transfer", 500)
        b.cb(m, "Smart Access", "RENT RAY WHITE FITZROY", "Ray White Fitzroy", "Rent & housing", -2080)
        b.cb(m.replace(day=3), "Smart Access", "FITNESS FIRST AU", "Fitness First", "Health & fitness", -79.95, True)
        b.cb(m.replace(day=5), "Smart Access", f"TRANSFER TO {PARTNER} SHARED BILLS", "Riley Chen",
             "Transfers to people", -280)
        if rng.random() < 0.6:
            b.cb(m.replace(day=rng.randint(6, 26)), "Smart Access", f"TRANSFER FROM {PARTNER}", "Riley Chen",
                 "Transfers received", round(rng.uniform(30, 90), 2))

        # Subscriptions and bills, with a price rise and a bill jump on the latest charge.
        latest = lambda day: (m.replace(day=day) + timedelta(days=31)).replace(day=day) > end
        b.cb(m.replace(day=9), "Smart Access", "NETFLIX.COM MELBOURNE AU", "Netflix", "Entertainment",
             -(22.99 if latest(9) else 18.99), True)
        b.cb(m.replace(day=21), "Smart Access", "SPOTIFY P2B4C9 STOCKHOLM SE", "Spotify", "Entertainment", -13.99, True)
        b.cb(m.replace(day=12), "Smart Access", "TELSTRA CORP LTD AU", "Telstra", "Bills & utilities",
             -(141.30 if latest(12) else 89.00))
        if months_in % 2 == 1:
            b.cb(m.replace(day=18), "Smart Access", "ORIGIN ENERGY AU", "Origin Energy", "Bills & utilities",
                 -round(rng.uniform(150, 240), 2))
        b.cb(last_day(m), "NetBank Saver", "CREDIT INTEREST", "Interest", "Interest",
             round(30 + months_in * 2.1 + rng.uniform(0, 3), 2))

        # Wise: a top-up from CommBank, then money sent overseas from the Wise balance.
        top_up = m.replace(day=20)
        b.cb(top_up, "Smart Access", "WISE AUSTRALIA PTY LTD AU", "Wise", "International transfers", -1000)
        b.wise_tx(top_up + timedelta(days=rng.randint(0, 1)), "added", "Alex Morgan", 1000)
        b.wise_tx(m.replace(day=22), "sent", "Jordan Morgan", 600, fee=4.80)
        if months_in % 2 == 0:
            b.wise_tx(m.replace(day=24), "sent", "Alex Morgan", 250, fee=2.90)
        if months_in % 3 == 1:
            b.wise_tx(m.replace(day=25), "sent", "Sam Morgan", 150, fee=2.10)
        for _ in range(rng.randint(1, 3)):
            who, cat = rng.choice([("Steam Games", "Entertainment"), ("Apple.com/bill", "Entertainment"),
                                   ("Muji Online", "Shopping"), ("Lime Scooters", "Transport")])
            b.wise_tx(m.replace(day=rng.randint(1, 27)), "card", who, round(rng.uniform(8, 45), 2), category=cat)

        # Investing: CMC Invest every other month.
        if months_in % 2 == 0:
            b.cb(m.replace(day=16), "Smart Access", f"TRANSFER TO {ME} CMC MARKETS", "CMC Markets",
                 "Transfers to people", -1000)

    # Everyday spending.
    d = start
    while d <= end:
        wd = d.weekday()
        if wd < 5 and rng.random() < 0.7:
            b.spend(d, ("PATRICIA COFFEE BREWERS AU", "Patricia Coffee", "Eating out & cafes", 0, 0), amount=5.50)
        if wd in (2, 5) and rng.random() < 0.8:
            b.spend(d, rng.choice(GROCERIES))
        if rng.random() < 0.13:
            b.spend(d, EATING[0])
        if wd in (4, 5) and rng.random() < 0.4:
            b.spend(d, rng.choice(EATING[1:]))
        if d.day % 10 == 2:
            b.spend(d, ("MYKI TOP UP AU", "Myki", "Transport", 0, 0), amount=50.00)
        if rng.random() < 0.06:
            b.spend(d, ("UBER *TRIP HELP.UBER.COM AU", "Uber", "Transport", 12, 38))
        if rng.random() < 0.12:
            b.spend(d, ("7-ELEVEN 2207 FITZROY AU", "7-Eleven", "Convenience & tobacco", 3, 14))
        if rng.random() < 0.06:
            b.spend(d, rng.choice(SHOPPING))
        if rng.random() < 0.04:
            b.spend(d, ("CHEMIST WAREHOUSE RICHMOND AU", "Chemist Warehouse", "Health & fitness", 9, 55))
        d += timedelta(days=1)

    # Trips: two earlier ones, and a bigger one last month (the travel spike).
    qantas = ("QANTAS AIRWAYS MASCOT AU", "Qantas", "Travel", 0, 0)
    hotel = ("BOOKING.COM HOTEL AMSTERDAM NL", "Booking.com", "Travel", 0, 0)
    for trip, flight, stay in ((start + timedelta(days=120), 420.40, 610.00),
                               (start + timedelta(days=330), 298.20, 455.50),
                               (last_full + timedelta(days=8), 712.60, 980.00)):
        b.spend(trip, qantas, amount=flight)
        b.spend(trip + timedelta(days=2), hotel, amount=stay)

    # The planted surprises.
    dup = end - timedelta(days=40)
    b.spend(dup, SHOPPING[3], amount=64.95)
    b.spend(dup + timedelta(days=1), SHOPPING[3], amount=64.95)
    b.spend(end - timedelta(days=12), ("HARVEY NORMAN RICHMOND AU", "Harvey Norman", "Shopping & other", 0, 0),
            amount=1299.00)
    # Sent through Wise straight from the CommBank card, so it never touches the Wise balance.
    card_send = end - timedelta(days=9)
    b.cb(card_send, "Smart Access", "WISE PAYMENTS LTD AU", "Wise", "International transfers", -404.30)
    b.wise_tx(card_send, "sent", "Sam Morgan", 400, fee=4.30, funded_by_card=True)
    return b


def add_balances(rows, opening):
    order = {"Smart Access": 0, "NetBank Saver": 1}
    rows.sort(key=lambda r: (r["date"], order[r["account"]], r["amount"] < 0))
    balance = dict(opening)
    for r in rows:
        balance[r["account"]] = round(balance[r["account"]] + r["amount"], 2)
        r["balance"] = balance[r["account"]]


# ---------- Writers ----------

def write_ledger(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(LEDGER_COLUMNS)
        for r in rows:
            w.writerow([f"{r['date']:%Y-%m-%d}", r["account"], r["description"], r["merchant"], r["category"],
                        f"{r['amount']:.2f}", "debit" if r["amount"] < 0 else "credit", f"{r['balance']:.2f}"])


def write_pdf(path, lines, lines_per_page=52):
    """Plain text lines on A4 pages, one line per row, like the real statements' text layer."""
    c = canvas.Canvas(str(path), pagesize=A4)
    pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[]]
    for n, page in enumerate(pages, 1):
        c.setFont("Helvetica", 8.5)
        y = 800
        for line in page:
            c.drawString(40, y, line)
            y -= 14
        c.drawString(40, 30, f"Page {n} of {len(pages)}")
        c.showPage()
    c.save()


def write_commbank_pdf(path, account, rows, created):
    lines = ["Transaction Summary", f"Account type {account}", "Account Number 06 0000 0000",
             "Date Transaction details Amount Balance"]
    for r in rows:
        lines.append(f"{r['date']:%d %b %Y} {r['description']} {money(r['amount'])} {money(r['balance'])}")
        if r["card"]:
            # Noise lines the reader drops, as on real summaries.
            lines += ["Card xx1234", f"Value Date: {r['date'] - timedelta(days=1):%d/%m/%Y}"]
    lines += [f"Created {created:%d %b %Y}", "Any pending transactions are not shown."]
    write_pdf(path, lines)


def write_wise_xlsx(path, txs):
    """Wise's AUD balance statement: fees are their own FEE- rows, with a running balance."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ID", "Date", "Date Time", "Amount", "Currency", "Description", "Payment Reference",
               "Running Balance", "Exchange From", "Exchange To", "Exchange Rate", "Total Fees", "Payer Name",
               "Payee Name", "Payee Account Number", "Merchant", "Card Last Four Digits", "Card Holder Full Name",
               "Attachment", "Note", "Exchange To Amount", "Transaction Type", "Transaction Details Type"])
    balance = 0.0
    for t in txs:
        when = datetime.combine(t["date"], time())
        def row(tx_id, amount, description, payer=None, payee=None, merchant=None, kind="TRANSFER"):
            nonlocal balance
            balance = round(balance + amount, 2)
            ws.append([tx_id, when, t["time"], amount, "AUD", description, "", balance, None, None, None,
                       t["fee"], payer, payee, None, merchant, None, None, None, None, None,
                       "CREDIT" if amount > 0 else "DEBIT", kind])
        if t["kind"] == "added":
            row(t["id"], t["amount"], "Topped up balance", payer=t["who"], kind="DEPOSIT")
        elif t["kind"] == "sent":
            row(t["id"], -t["amount"], f"Sent money to {t['who']}", payee=t["who"])
        else:
            row(t["id"], -t["amount"], f"Card transaction of {t['amount']} AUD issued by {t['who']}",
                merchant=t["who"], kind="CARD")
        if t["fee"]:
            row(f"FEE-{t['id']}", -t["fee"], f"Wise fee for {t['id']}")
    wb.save(path)


def write_wise_csv(path, txs):
    """Wise's transaction history export: card IDs read CARD_TRANSACTION-, as in the real files."""
    columns = ["ID", "Status", "Direction", "Created on", "Finished on", "Source fee amount", "Source fee currency",
               "Target fee amount", "Target fee currency", "Source name", "Source amount (after fees)",
               "Source currency", "Target name", "Target amount (after fees)", "Target currency", "Exchange rate",
               "Reference", "Batch", "Created by", "Category", "Note"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, columns)
        w.writeheader()
        for t in txs:
            out = t["kind"] != "added"
            w.writerow({
                "ID": t["id"].replace("CARD-", "CARD_TRANSACTION-"), "Status": "COMPLETED",
                "Direction": "OUT" if out else "IN", "Created on": f"{t['time']:%Y-%m-%d %H:%M:%S}",
                "Finished on": f"{t['time']:%Y-%m-%d %H:%M:%S}", "Source fee amount": f"{t['fee']:.2f}",
                "Source fee currency": "AUD", "Source name": "Alex Morgan" if out else t["who"],
                "Source amount (after fees)": f"{t['amount']:.2f}", "Source currency": "AUD",
                "Target name": t["who"] if out else "Alex Morgan", "Target amount (after fees)": f"{t['amount']:.2f}",
                "Target currency": "AUD", "Exchange rate": "1", "Reference": "",
                "Created by": "Alex Morgan", "Category": t["category"] or ("General" if out else "Money added"),
            })


def write_cmc(out, as_at, invested, rng):
    """Holdings bought with what went in, at made-up gains; the rest sits as cash."""
    cash = 184.35
    plan = [("VAS", 0.5, 101.20, 1.081), ("NDQ", 0.3, 44.10, 1.142), ("IOZ", 0.2, 31.80, 0.968)]
    with open(out / f"CMC_ProfitAndLoss_{as_at:%Y-%m-%d}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["Account Number", "Account Name", "CHESS HIN", "Code", "Last", "FX Rate", "Units Held",
                    "Net Avg Price AUD", "Cost AUD", "Market Value AUD", "Day Gain AUD", "P&L AUD", "P&L %"])
        total_cost = total_value = 0.0
        for code, share, price, growth in plan:
            units = int((invested - cash) * share / price)
            cost, value = round(units * price, 2), round(units * price * growth, 2)
            total_cost += cost
            total_value += value
            w.writerow(["0000000", "Alex Morgan", "X0000000000", code, f"{price * growth:.3f}", "1", units,
                        f"{price:.3f}", f"{cost:.2f}", f"{value:.2f}", f"{rng.uniform(-20, 20):.2f}",
                        f"{value - cost:.2f}", f"{(value / cost - 1) * 100:.2f}"])
        w.writerow(["TOTALS", "", "", "", "", "", "", "", f"{total_cost:.2f}", f"{total_value:.2f}", "",
                    f"{total_value - total_cost:.2f}", ""])
    with open(out / f"CMC_Accounts_{as_at:%Y-%m-%d}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["Account", "Name", "Cash Account Number", "Options Account Number", "HIN", "Bank Balance",
                    "Direct Debit Limit", "Unsettled", "Today's Trade Value", "Today's Trade Count",
                    "Open Buys Value", "Open Buys Count", "Holdings", "Available"])
        w.writerow(["0000000", "Alex Morgan", "", "", "X0000000000", f"{cash:.2f}", "", "0", "0", "0", "0", "0",
                    f"{total_value:.2f}", f"{cash:.2f}"])
        w.writerow(["TOTALS", "", "", "", "", f"{cash:.2f}", "", "", "", "", "", "", "", ""])


def write_super(out, as_at, rng):
    """An IOOF-style transaction statement. Its text layer squashes spaces ("Closingbalance"),
    like the real one: unit purchases from contributions, small monthly fee sales, and the
    latest contribution still pending (not yet in units)."""
    opened = as_at - timedelta(days=365)
    opening = 31240.10
    price, units = 2.21, opening / 2.21
    paid_in = deducted = 0.0
    rows = []
    for m in list(month_starts(opened + timedelta(days=1), as_at))[:-1]:
        price *= 1 + rng.uniform(-0.004, 0.012)
        day = m.replace(day=15)
        bought = 1086.25 / price
        units += bought
        paid_in += 1086.25
        rows.append(f"{day:%d%b%Y} Buys {bought:.4f} {price:.4f} -1,086.25 -")
        fee = round(rng.uniform(11, 16), 2)
        sold = fee / price
        units -= sold
        deducted += fee
        rows.append(f"{last_day(m):%d%b%Y} Sells {sold:.4f} {price:.4f} {fee:.2f} -")
    pending = 1086.25
    units_value = round(units * price, 2)
    closing = units_value + pending
    lines = ["Transactionstatement", f"Closingdate {as_at:%d%b%Y} Openingdate {opened:%d%b%Y}",
             f"Closingbalance ${closing:,.2f}", f"Openingbalance ${opening:,.2f}",
             "Date Transaction Units Unitprice Amount Pending", *rows,
             f"{as_at - timedelta(days=2):%d%b%Y} Contribution - - - {pending:,.2f}",
             f"Closingbalance {units:,.4f} {price:.4f} - {units_value:,.2f}"]
    write_pdf(out / f"Super_IOOF_TransactionStatement_{opened:%Y-%m-%d}_to_{as_at:%Y-%m-%d}.pdf", lines)


def generate(out, end):
    rng = random.Random(SEED)
    start = (end - timedelta(days=640)).replace(day=1)
    b = make_events(start, end, rng)
    add_balances(b.rows, {"Smart Access": 4400.00, "NetBank Saver": 6500.00})

    ledger_end = end - timedelta(days=20)
    # The ledger was made partway through its last day, so it holds only that day's first
    # transactions; the newer PDFs start on that day and hold them all.
    last_day_rows = [r for r in b.rows if r["date"] == ledger_end]
    ledger = [r for r in b.rows if r["date"] < ledger_end] + last_day_rows[:len(last_day_rows) // 2]
    # One transaction missing from the ledger (as when a statement was skipped): a balance gap.
    gap = next(r for r in ledger if r["date"] >= end - timedelta(days=150) and r["merchant"] == "Coles")
    ledger.remove(gap)

    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*"):
        old.unlink()
    write_ledger(out / "ledger.csv", ledger)
    for account, name in (("Smart Access", "SmartAccess"), ("NetBank Saver", "NetBankSaver")):
        rows = [r for r in b.rows if r["account"] == account and r["date"] >= ledger_end]
        write_commbank_pdf(out / f"{name}_{ledger_end:%Y-%m-%d}_to_{end:%Y-%m-%d}.pdf", account, rows, end)

    wise = sorted(b.wise, key=lambda t: t["time"])
    xlsx_end, csv_start = end - timedelta(days=20), end - timedelta(days=50)
    write_wise_xlsx(out / f"Wise_AUD_{start:%Y-%m-%d}_to_{xlsx_end:%Y-%m-%d}.xlsx",
                    [t for t in wise if t["date"] <= xlsx_end and not t["funded_by_card"]])
    write_wise_csv(out / f"Wise_{csv_start:%Y-%m-%d}_to_{end:%Y-%m-%d}.csv",
                   [t for t in wise if t["date"] >= csv_start])

    invested = -sum(r["amount"] for r in b.rows if "CMC" in r["description"])
    write_cmc(out, end - timedelta(days=1), invested, rng)
    write_super(out, end - timedelta(days=3), rng)
    low = min(r["balance"] for r in b.rows if r["account"] == "Smart Access")
    print(f"Wrote {len(list(out.iterdir()))} files to {out} ({start} to {end}, "
          f"{len(b.rows)} bank and {len(b.wise)} Wise transactions; lowest everyday balance ${low:,.2f})")
    return b


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1),
                        help="last day of data (default: yesterday)")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "sample_data")
    args = parser.parse_args()
    generate(args.out, args.end)


if __name__ == "__main__":
    main()
