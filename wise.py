"""Reads Wise statements (PDF transaction history and Excel exports) into transaction dicts.

Each Wise transaction takes two lines of text:
    Sent | Jordan Morgan 512.40 EUR                         <- header: type and who
    28 May 2026 | Transaction: TRANSFER-10001 | Category: General 4.12 AUD 850 AUD
                                                               <- date, ID, fee, amount
Run on its own (python wise.py) to print what it finds.
"""

import csv
import re
import warnings
from datetime import datetime
from pathlib import Path

import openpyxl
import pdfplumber

from config import CONFIG

DATE_LINE = re.compile(r"^(\d{1,2} \w+ \d{4}) \| Transaction: ([A-Z_]+-\d+)(.*)$")
AUD = re.compile(r"([\d,]+(?:\.\d+)?) AUD")
CATEGORY = re.compile(r"Category: ([A-Za-z &]+?)(?= [\d,]| ?$)")
REFERENCE = re.compile(r"Reference: (.*?) \|")
PAGE_FOOTER = re.compile(r"^\d+ / \d+$")


def fee_and_amount(header, rest):
    """The 'fee AUD  amount AUD' pair is on the header line for cards and top-ups,
    and on the date line for transfers sent. Use whichever line has both."""
    for text in (header, rest):
        found = AUD.findall(text)
        if len(found) >= 2:
            return float(found[-2].replace(",", "")), float(found[-1].replace(",", ""))
    found = AUD.findall(header + " " + rest)
    return 0.0, float(found[-1].replace(",", "")) if found else 0.0


def page_text(page):
    # The diagonal "not an official statement" watermark is huge light-grey text;
    # real text is small, so keep only characters under 20pt.
    return page.filter(lambda o: o.get("object_type") != "char" or o["size"] < 20).extract_text() or ""


def parse_pdf(path):
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            # Drop page footers like "2 / 14" so a transaction split across pages stays intact.
            lines += [l for l in page_text(page).splitlines() if not PAGE_FOOTER.match(l)]

    transactions = []
    for i, line in enumerate(lines):
        match = DATE_LINE.match(line)
        if not match:
            continue
        date_text, tx_id, rest = match.groups()
        header = lines[i - 1]
        # "Money added" sits on the line after the date line for top-ups.
        next_line = lines[i + 1] if i + 1 < len(lines) else ""

        fee, amount = fee_and_amount(header, rest)
        who = re.sub(r"^(Sent|Received|Added)? ?\| ?", "", header)
        who = re.sub(r"\s+[\d,.]+ [A-Z]{3}\b.*$", "", who).strip()

        # Card payments have CARD_TRANSACTION IDs; a TRANSFER is either money
        # you sent out ("Sent | ...") or money that came in.
        if tx_id.startswith("CARD"):
            kind = "card"
        elif header.startswith("Sent"):
            kind = "sent"
        elif next_line.startswith("Money added") or "Money added" in rest or tx_id.startswith("TRANSFER"):
            kind = "added"
        else:
            kind = "other"

        category = CATEGORY.search(rest)
        reference = REFERENCE.search(rest)
        transactions.append({
            "id": tx_id,
            "date": datetime.strptime(date_text, "%d %B %Y"),
            "kind": kind,
            "who": who,
            "amount": amount,
            "fee": fee,
            "category": category.group(1).strip() if category else "",
            "reference": reference.group(1) if reference else "",
            "source": Path(path).name,
        })
    return transactions


def parse_xlsx(path):
    """Read a Wise Excel export. Only AUD exports are used: other currencies are
    funded from the AUD balance, so their spending already shows up there."""
    # read_only mode silently returns just the header for Wise's files, so load fully.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # harmless "no default style" warning from Wise's files
        sheet = openpyxl.load_workbook(path).active
    header, *data = sheet.iter_rows(values_only=True)
    rows = [dict(zip(header, r)) for r in data if r[0]]
    if not rows or rows[0]["Currency"] != "AUD":
        return []

    # Fees are separate rows ("FEE-TRANSFER-123"); fold each into its transaction.
    fees = {r["ID"][4:]: -r["Amount"] for r in rows if r["ID"].startswith("FEE-")}
    transactions = []
    for r in rows:
        if r["ID"].startswith("FEE-"):
            continue
        fee = fees.get(r["ID"], 0.0)
        detail = r["Transaction Details Type"]
        if detail == "CARD":
            kind = "card"
        elif detail == "DEPOSIT":
            kind = "added"
        elif detail == "TRANSFER" and r["Amount"] < 0:
            kind = "sent"
        else:
            kind = "other"
        # Same sign convention as the PDFs: amount is what left (or arrived), fee included.
        # A card refund comes through as a negative card amount.
        amount = -r["Amount"] + fee if kind in ("card", "sent") else r["Amount"]
        transactions.append({
            "id": r["ID"],
            "date": r["Date"],
            "kind": kind,
            "who": r["Payee Name"] or r["Merchant"] or r["Payer Name"] or r["Description"],
            "amount": amount,
            "fee": fee,
            "category": "",
            "reference": r["Payment Reference"] or "",
            "source": Path(path).name,
            "balance": r["Running Balance"],
            "time": r["Date Time"],
        })
    return transactions


def parse_csv(path):
    """Read a Wise 'transaction history' CSV export. Unlike the balance statements, it
    also lists transfers paid straight from a bank card that never touched the balance."""
    transactions = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            # Cancelled and refunded transfers never moved money; other currencies are
            # funded from the AUD balance, so their spending already shows up in AUD.
            if r["Status"] != "COMPLETED" or r["Source currency"] != "AUD":
                continue
            amount = float(r["Source amount (after fees)"] or 0)
            fee = float(r["Source fee amount"] or 0)
            if r["Direction"] == "OUT":
                kind = "card" if r["ID"].startswith("CARD") else "sent"
                who = r["Target name"]
                amount += fee  # same convention as the other readers: fee included
            else:
                kind = "added" if r["Category"] == "Money added" else "received"
                who = r["Source name"]
            transactions.append({
                "id": r["ID"],
                "date": datetime.strptime(r["Created on"][:10], "%Y-%m-%d"),
                "kind": kind,
                "who": who,
                "amount": amount,
                "fee": fee,
                "category": r["Category"],
                "reference": r["Reference"],
                "source": Path(path).name,
            })
    return transactions


def dedupe_key(tx_id):
    # PDFs say CARD_TRANSACTION-123 where Excel exports say CARD-123.
    return tx_id.replace("CARD_TRANSACTION-", "CARD-")


PARSERS = {".pdf": parse_pdf, ".xlsx": parse_xlsx, ".csv": parse_csv}


def load_all(*folders):
    """Parse every Wise PDF, Excel and CSV export in the folders, keeping each transaction once."""
    seen = {}
    files = [p for ext in PARSERS for f in folders for p in sorted(Path(f).glob(f"Wise*{ext}"))]
    for path in files:
        parse = PARSERS[path.suffix]
        for t in parse(path):
            seen.setdefault(dedupe_key(t["id"]), t)
    return sorted(seen.values(), key=lambda t: t["date"])


if __name__ == "__main__":
    txs = load_all(CONFIG["data_dir"], CONFIG["data_dir"] / "Statements")
    print(f"{len(txs)} Wise transactions, {txs[0]['date']:%d %b %Y} to {txs[-1]['date']:%d %b %Y}\n")
    for t in txs:
        print(f"{t['date']:%Y-%m-%d}  {t['kind']:<8} {t['amount']:>9,.2f}  fee {t['fee']:>6,.2f}  "
              f"{t['who'][:28]:<28} {t['category']:<14} {t['reference']}")
