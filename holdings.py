"""Where your money sits: latest balances for each account, read from your statements.

Run on its own (python holdings.py) to print them.
"""

import csv
import re
from datetime import datetime
from pathlib import Path

import pdfplumber

import wise
from config import CONFIG

NUMBER = r"([\d,]+\.\d+)"


def money(text):
    return float(text.replace(",", ""))


def latest_file(folders, pattern):
    """The newest matching file across the folders. File names end in their date
    (e.g. CMC_ProfitAndLoss_2026-06-21.pdf), so sorting by name puts the newest last."""
    files = sorted((p for f in folders for p in Path(f).glob(pattern)), key=lambda p: p.name)
    return files[-1] if files else None


def bank_balances(rows):
    """Last balance per account, plus each account's end-of-day balance history,
    from ledger-shaped rows in date order."""
    history = {}
    for row in rows:
        # Later rows on the same day overwrite earlier ones, leaving the end-of-day balance.
        history.setdefault(row["account"], {})[row["date"]] = float(row["balance"])
    return {account: sorted(days.items()) for account, days in history.items()}


def wise_balance(*folders):
    """Latest AUD balance: the newest running balance in the Wise Excel exports (the PDFs
    don't include balances), carried forward through any newer CSV exports (which list
    transactions but no balance).

    A CSV can't show which transfers were paid straight from a bank card, and those never
    touched the balance, so check the result against the balance Wise shows."""
    latest = None
    in_excel = set()
    for path in (p for f in folders for p in Path(f).glob("Wise*.xlsx")):
        for t in wise.parse_xlsx(path):
            in_excel.add(wise.dedupe_key(t["id"]))
            if t.get("balance") is not None and (latest is None or t["time"] > latest["time"]):
                latest = t
    if not latest:
        return (0.0, None)

    balance, date = latest["balance"], latest["date"]
    newer = {}
    for path in (p for f in folders for p in Path(f).glob("Wise*.csv")):
        for t in wise.parse_csv(path):
            key = wise.dedupe_key(t["id"])
            if key not in in_excel and t["date"] >= latest["date"]:
                newer[key] = t
    for t in newer.values():
        # Same convention as parse_csv: card and sent amounts already include the fee.
        balance += -t["amount"] if t["kind"] in ("card", "sent") else t["amount"]
        date = max(date, t["date"])
    return (round(balance, 2), date)


def date_in_name(path):
    """'CMC_Accounts_2026-10-05.csv' -> '2026-10-05' (the last date in the file name)."""
    found = re.findall(r"\d{4}-\d{2}-\d{2}", path.name)
    return found[-1] if found else ""


def cmc(*folders):
    """Holdings and cash from the newest CMC files: the Profit & Loss report (CSV or PDF)
    for holdings, and the Accounts CSV or Portfolio Report PDF for cash."""
    pnl = max(((date_in_name(p), p) for f in folders for p in Path(f).glob("CMC_ProfitAndLoss*")
               if p.suffix in (".csv", ".pdf")), default=(None, None))[1]
    if not pnl:
        return None
    cash_file = max(((date_in_name(p), p) for f in folders
                     for p in [*Path(f).glob("CMC_Accounts*.csv"), *Path(f).glob("CMC_PortfolioReport*.pdf")]),
                    default=(None, None))[1]
    if pnl.suffix == ".csv":
        return cmc_from_csv(pnl, cash_file)
    with pdfplumber.open(pnl) as pdf:
        text = pdf.pages[0].extract_text()
    as_at = re.search(r"as at (\d{4}.\d{2}.\d{2})", text)
    holdings = []
    # e.g. "VAS 101.250 AUD 40 95.310 3,812.40 4,050.00 237.60 6.23"
    for line in text.splitlines():
        m = re.match(rf"^([A-Z]+(?::[A-Z]+)?) [\d.]+ [A-Z]{{3}} (?:[\d.]+ )?(\d+) [\d.]+ {NUMBER} {NUMBER}", line)
        if m:
            code, units, cost, value = m.groups()
            holdings.append({"code": code, "units": int(units), "cost": money(cost), "value": money(value)})

    date = datetime.strptime(re.sub(r"\D", "-", as_at.group(1)), "%Y-%m-%d") if as_at else None
    return {"as_at": date, "holdings": holdings, "cash": cmc_cash(cash_file)}


def cmc_cash(path):
    """Cash balance from a CMC Accounts CSV ('Bank Balance' column) or Portfolio Report PDF."""
    if not path:
        return 0.0
    if path.suffix == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r["Account"] != "TOTALS"]
        return sum(float(r["Bank Balance"]) for r in rows)
    with pdfplumber.open(path) as pdf:
        found = re.search(rf"Bank Balance {NUMBER}", pdf.pages[0].extract_text())
    return money(found.group(1)) if found else 0.0


def cmc_from_csv(pnl, cash_file):
    """CMC Invest's Profit & Loss CSV: one row per holding, plus a TOTALS row."""
    with open(pnl, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["Account Number"] != "TOTALS"]
    holdings = [{"code": r["Code"], "units": int(float(r["Units Held"])),
                 "cost": float(r["Cost AUD"]), "value": float(r["Market Value AUD"])} for r in rows]
    # The CSV has no date inside, so use the one in its file name.
    as_at = datetime.strptime(date_in_name(pnl), "%Y-%m-%d")
    return {"as_at": as_at, "holdings": holdings, "cash": cmc_cash(cash_file)}


def super_fund(*folders):
    """Opening and closing balance from the latest IOOF transaction statement."""
    statement = latest_file(folders, "Super_IOOF_TransactionStatement*.pdf")
    if not statement:
        return None
    with pdfplumber.open(statement) as pdf:
        text = pdf.pages[0].extract_text()
    # The PDF squashes spaces: "Closingbalance $48,120.55" ... "30Sep2026"
    closing = re.search(rf"Closingbalance \${NUMBER}", text)
    opening = re.search(rf"Openingbalance \${NUMBER}", text)
    dates = re.findall(r"\b(\d{1,2}[A-Z][a-z]{2}\d{4})\b", text)
    parse = lambda d: datetime.strptime(d, "%d%b%Y")

    # Unit transactions, e.g. "15Sep2026 Buys 512.3301 2.4400 -1,250.08 -". Buys are money
    # paid in (contributions); Sells are small monthly deductions (fees, insurance).
    # Pending transactions have no units ("- - -"), so they don't match.
    paid_in = deducted = 0.0
    for kind, amount in re.findall(r"^\d{1,2}[A-Z][a-z]{2}\d{4} (Buys|Sells) -?[\d,.]+ [\d.]+ (-?[\d,]+\.\d+) -$",
                                   text, re.MULTILINE):
        if kind == "Buys":
            paid_in -= money(amount)
        else:
            deducted += money(amount)
    # Value of the units held; the headline closing balance also counts money not yet invested.
    invested = re.search(rf"Closingbalance [\d,.]+ [\d.]+ - {NUMBER}", text)

    result = {
        "closing": money(closing.group(1)),
        "opening": money(opening.group(1)),
        # The first two dates printed are the closing date, then the opening date.
        "as_at": parse(dates[0]),
        "opening_date": parse(dates[1]),
        "paid_in": None, "deducted": None, "returns": None, "uninvested": None,
    }
    if invested:
        units_value = money(invested.group(1))
        result.update(paid_in=paid_in, deducted=deducted,
                      returns=units_value - result["opening"] - paid_in + deducted,
                      uninvested=result["closing"] - units_value)
    return result


if __name__ == "__main__":
    base = CONFIG["data_dir"]
    with open(base / "ledger.csv", newline="", encoding="utf-8-sig") as f:
        ledger = list(csv.DictReader(f))
    for account, days in bank_balances(ledger).items():
        print(f"{account:<14} ${days[-1][1]:>10,.2f}  as at {days[-1][0]}")
    folders = (base, base / "Statements")
    balance, date = wise_balance(*folders)
    print(f"{'Wise':<14} ${balance:>10,.2f}  as at {date:%Y-%m-%d}")
    c = cmc(*folders)
    print(f"{'CMC':<14} ${sum(h['value'] for h in c['holdings']) + c['cash']:>10,.2f}  as at {c['as_at']:%Y-%m-%d}  {c['holdings']}")
    s = super_fund(*folders)
    print(f"{'IOOF super':<14} ${s['closing']:>10,.2f}  as at {s['as_at']:%Y-%m-%d} (was ${s['opening']:,.2f} on {s['opening_date']:%Y-%m-%d})")
