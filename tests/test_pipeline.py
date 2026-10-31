"""End-to-end checks on the fake data from generate_sample.py (see conftest.py)."""

import json
from collections import Counter
from datetime import timedelta

import pytest

import analyze
import checks
import holdings
import wise
from conftest import END, EVENTS, WORK


@pytest.fixture(scope="module")
def transactions():
    return analyze.load_transactions(analyze.load_rules())


@pytest.fixture(scope="module")
def alerts(transactions):
    months = analyze.monthly_totals(transactions)
    return checks.find_alerts(transactions, months, f"{END:%Y-%m}", analyze.bank_rows())


# ---------- Combining sources ----------

def test_ledger_and_newer_pdfs_join_without_repeats():
    """The PDFs overlap the ledger's last day; only what the ledger lacks is added.
    Every generated bank transaction appears exactly once, except the one left out
    of the ledger on purpose."""
    rows = analyze.bank_rows()
    keys = Counter((r["account"], r["date"], r["description"], r["amount_signed"]) for r in rows)
    assert max(keys.values()) == 1
    assert len(rows) == len(EVENTS.rows) - 1


def test_wise_top_ups_replaced_by_what_wise_paid_out(transactions):
    """Money sent from CommBank to Wise isn't spending; the Wise payouts are."""
    leftover = [t for t in transactions if t["account"] != "Wise" and "WISE" in t["narration"].upper()]
    assert leftover == []
    sent = sum(t["spent"] for t in transactions if t["account"] == "Wise" and t["kind"] == "sent")
    expected = sum(w["amount"] + w["fee"] for w in EVENTS.wise if w["kind"] == "sent")
    assert sent == pytest.approx(expected)


def test_card_funded_wise_transfer_counted_once(transactions):
    """A transfer paid straight from the bank card never touches the Wise balance, so it
    matches the transfer itself: it shows once, as the payment to the recipient."""
    hits = [t for t in transactions if t["spent"] == pytest.approx(404.30)]
    assert [(t["account"], t["narration"]) for t in hits] == [("Wise", "Sam Morgan")]


def test_wise_exports_deduplicated_across_formats():
    """The xlsx and CSV overlap and spell card IDs differently; each transaction is kept once."""
    data = WORK / "data"
    txs = wise.load_all(data)
    assert len(txs) == len({wise.dedupe_key(t["id"]) for t in txs}) == len(EVENTS.wise)


def test_cmc_transfers_count_as_invested_not_spent(transactions):
    invested = [t for t in transactions if t["category"] == analyze.INVESTMENT_CATEGORY]
    assert sum(t["spent"] for t in invested) == pytest.approx(11000)
    months = analyze.monthly_totals(transactions)
    assert analyze.INVESTMENT_CATEGORY not in {c for m in months.values() for c in m["cats"]}


def test_partner_repayments_reduce_shared_spending(transactions):
    shared = [t for t in transactions if t["category"] == "Shared with partner"]
    assert any(t["spent"] < 0 for t in shared)        # money back counts against spending
    assert all(t["received"] == 0 for t in shared)    # ...and never as income


# ---------- Balances and holdings ----------

def test_balances_read_from_each_source():
    parts, bank, cmc, sup = analyze.where_money_sits()
    last = {r["account"]: r["balance"] for r in sorted(EVENTS.rows, key=lambda r: r["date"])}
    by_name = {p["name"]: p["value"] for p in parts}
    assert by_name["Everyday"] == pytest.approx(last["Smart Access"])
    assert by_name["Savings"] == pytest.approx(last["NetBank Saver"])
    assert by_name["Wise"] > 0 and len(cmc["holdings"]) == 3
    assert sup["uninvested"] == pytest.approx(1086.25, abs=0.01)
    assert sup["closing"] - sup["opening"] == pytest.approx(
        sup["paid_in"] - sup["deducted"] + sup["returns"] + sup["uninvested"])



def test_wise_balance_carried_forward_through_newer_csv(tmp_path, monkeypatch):
    """Excel statements hold the balance; newer CSV rows move it, skipping overlaps and non-payments."""
    from datetime import datetime

    (tmp_path / "Wise_AUD.xlsx").touch()
    day = datetime(2030, 1, 10)
    monkeypatch.setattr(wise, "parse_xlsx", lambda path: [
        {"id": "CARD-1", "date": day, "time": day, "balance": 100.0}])
    header = ("ID,Status,Direction,Created on,Source fee amount,Source amount (after fees),"
              "Source currency,Source name,Target name,Category,Reference\n")
    (tmp_path / "Wise_new.csv").write_text(header + "".join([
        "CARD_TRANSACTION-1,COMPLETED,OUT,2030-01-10,0,40,AUD,Me,Already in Excel,General,\n",
        "CARD_TRANSACTION-0,COMPLETED,OUT,2030-01-09,0,40,AUD,Me,Older,General,\n",
        "CARD_TRANSACTION-2,COMPLETED,OUT,2030-01-11,0.50,9.50,AUD,Me,Shop,General,\n",
        "CARD_TRANSACTION-3,CANCELLED,OUT,2030-01-12,0,30,AUD,Me,Declined,General,\n",
        "TRANSFER-4,COMPLETED,IN,2030-01-13,0,25,AUD,Me,Me,Money added,\n",
    ]), encoding="utf-8")
    assert holdings.wise_balance(tmp_path) == (115.0, datetime(2030, 1, 13))



def test_focus_gives_each_finding_a_next_step(transactions):
    months = analyze.monthly_totals(transactions)
    parts, bank, _, _ = analyze.where_money_sits()
    last_date = max(t["date"] for t in transactions)
    focus = analyze.build_focus(transactions, months, last_date, parts, bank)
    assert [f["label"] for f in focus][:2] == ["Spending vs regular income", "Cash buffer"]
    assert 2 <= len(focus) <= 4
    assert all(f["value"] and f["why"] and f["next"] for f in focus)
    habit = next(f for f in focus if f["label"] == "Biggest repeat habit")
    assert "Woolworths" not in habit["value"]  # groceries are an essential, not a habit


# ---------- Needs a look ----------

def test_every_planted_problem_is_flagged(alerts):
    titles = {(a["kind"], a["title"]) for a in alerts}
    assert ("Possible duplicate", "Booktopia charged $64.95 twice") in titles
    assert ("Price rise", "Netflix went up to $22.99") in titles
    assert ("Bill jump", "Telstra bill up to $141.30") in titles
    assert ("New merchant", "$1,299.00 at HARVEY NORMAN RICHMOND AU") in titles
    assert any(k == "Spending spike" and t.startswith("Travel") for k, t in titles)
    assert [a["kind"] for a in alerts].count("Balance gap") == 1


def test_routine_spending_is_not_flagged(alerts):
    """Daily coffee at the same price, rent, and stable subscriptions are normal."""
    flagged = " ".join(a["title"] for a in alerts)
    for name in ("Patricia Coffee", "Spotify", "Fitness First", "Ray White", "Myki"):
        assert name not in flagged


def test_duplicate_check_needs_a_rare_amount():
    """The same amount twice in a row is only suspicious when that amount is otherwise rare."""
    day = END - timedelta(days=5)
    t = lambda d: {"date": d, "account": "Smart Access", "narration": "Cafe", "category": "Eating out & cafes",
                   "spent": 24.0, "received": 0.0}
    assert len(checks.duplicate_charges([t(day), t(day + timedelta(days=1))], day + timedelta(days=1))) == 1
    many = [t(day - timedelta(days=i)) for i in range(5)]
    assert checks.duplicate_charges(many, day) == []


# ---------- The report ----------

def test_report_builds_with_data_and_no_placeholders(tmp_path):
    out = tmp_path / "report.html"
    analyze.main(["--no-open", "--out", str(out)])
    html = out.read_text(encoding="utf-8")
    assert "__DATA__" not in html
    data = json.loads(next(l for l in html.splitlines() if l.startswith("const D = "))[len("const D = "):-1])
    assert data["asOf"] == f"{END:%Y-%m-%d}"
    assert {i["label"] for i in data["insights"]} >= {"Cash buffer", "Money sent home", "Super returns, last 12 months"}
    assert len(data["alerts"]) >= 6
