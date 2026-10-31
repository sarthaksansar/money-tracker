"""Reads CommBank "transaction summary" PDFs (Statements > Create transaction summary
in NetBank) into rows shaped like ledger.csv, so analyze.py can add whatever is newer
than the ledger.

Each transaction starts with a dated line, followed by optional detail lines:
    30 Sep 2026 NETFLIX.COM*4K2QX81 LOS GATOS CA USA -$25.99 $3,410.52
    Card xx1234 USD 16.99
    Value Date: 28/09/2026
Run on its own (python commbank.py) to print what it finds.
"""

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

from config import CONFIG

MONEY = r"-?\$[\d,]+\.\d{2}"
TX_LINE = re.compile(rf"^(\d{{2}} \w{{3}} \d{{4}}) (.*) ({MONEY}) ({MONEY})$")
ACCOUNT_TYPE = re.compile(r"^Account type (.+)$", re.MULTILINE)
# Lines that end a transaction's details: page furniture and the closing letter.
END_OF_DETAILS = ("Created ", "Any pending", "Account Number", "Page ", "Date Transaction details",
                  "While this", "we’re not", "If you have", "Kind regards", "The CommBank Team")
# Detail lines that add nothing (the ledger leaves them out too).
NOISE = re.compile(r"^(Value Date: .*|Card xx\d+)$")


def money(text):
    return float(text.replace("$", "").replace(",", ""))


def parse_pdf(path):
    """Rows like ledger.csv's (without merchant/category), or [] if it isn't a transaction summary."""
    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    account = ACCOUNT_TYPE.search(pages[0]) if pages else None
    if not account or "Transaction Summary" not in pages[0]:
        return []

    rows = []
    current = None  # the transaction that detail lines belong to
    for line in "\n".join(pages).splitlines():
        match = TX_LINE.match(line)
        if match:
            date_text, details, amount, balance = match.groups()
            current = {
                "date": datetime.strptime(date_text, "%d %b %Y").strftime("%Y-%m-%d"),
                "account": account.group(1).strip(),
                "description": details,
                "amount_signed": f"{money(amount):.2f}",
                "balance": f"{money(balance):.2f}",
                "source": Path(path).name,
            }
            rows.append(current)
        elif line.startswith(END_OF_DETAILS):
            current = None
        elif current and not NOISE.match(line):
            current["description"] += " | " + line
    return rows


def load_all(*folders):
    """Every transaction-summary PDF in the folders, each transaction kept once."""
    seen = {}
    for folder in folders:
        for path in sorted(Path(folder).glob("*.pdf")):
            for row in parse_pdf(path):
                key = (row["account"], row["date"], row["description"], row["amount_signed"], row["balance"])
                seen.setdefault(key, row)
    return sorted(seen.values(), key=lambda r: r["date"])


if __name__ == "__main__":
    finance = CONFIG["data_dir"]
    rows = load_all(finance, finance / "Statements")
    print(f"{len(rows)} CommBank transactions\n")
    for r in rows:
        print(f"{r['date']}  {r['account']:<14} {float(r['amount_signed']):>10,.2f}  {r['description']}")
