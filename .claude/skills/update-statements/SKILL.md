---
name: update-statements
description: Monthly refresh of the money tracker. Download new CommBank, Wise, CMC and IOOF statements in the built-in browser (the user signs in), file them in the data folder, log off, then rerun analyze.py and report what changed. Use when the user says "update my statements", "refresh the report", "monthly update", or runs /update-statements.
---

# Update statements

Bring every source up to today, then rebuild the report. Explain each step briefly
as you go; the user is learning how this works.

## Ground rules (the user's standing rules)

- **The user signs in themselves.** Open the provider's login page in the built-in
  browser and wait. Never type passwords, codes or account numbers.
- **Ask before each download**, stating the file name, source and approximate size.
- **Check every file before filing it**: right account, right date range, not empty
  (a Wise Excel export that loads in read-only mode can look empty; open it fully).
- **Always log off** each site once its downloads are done.
- Statements are private: save them only to the data folder (`data_dir` in config.toml), never to
  this project, never into git, never anywhere online.

## 1. See what's out of date

Run `.venv\Scripts\python analyze.py` and read the **Data up to** list at the top.
For each source, the new statement should run from the day after its date to today.
Sources older than 31 days are flagged.

## 2. Download, per provider

Save everything to the `data_dir` folder from `config.toml` (the root,
not `Statements\`). The scripts read both folders and skip anything they've seen.

Downloads land in `%USERPROFILE%\Downloads`; check each file, then move and rename it.
Dates are `YYYY-MM-DD`; a name's last date must be the data's end date (the
scripts pick the newest CMC and IOOF file by that date).

### CommBank (built-in browser works)
NetBank > Statements > **Create transaction summary**, PDF, one per account.
Save as `SmartAccess_<from>_to_<to>.pdf` and `NetBankSaver_<from>_to_<to>.pdf`.

### CMC Markets (built-in browser works) - verified 5 Oct 2026
- Login: `https://cmcmarketsinvest.com/au/login` (CMC **Invest**, not the CFD site).
  The cookie banner has **Reject All**.
- Account > **Profit & loss** (`/trading/Manage/ProfitLoss`) > Download. Only CSV is
  offered. Save as `CMC_ProfitAndLoss_<today>.csv`.
- Account > **Accounts** (`/trading/Manage/TradingAccountsInfo`) > Download (CSV): has
  the cash "Bank Balance". Save as `CMC_Accounts_<today>.csv`.
- The pages sit in an embedded frame that page-reading tools can't see: use
  screenshots and clicks. The Download button is at the right edge of the toolbar.
- Log off: hamburger menu (top right) > **Logout**.

### Wise (Claude in Chrome works; built-in browser does NOT) - verified 1 Nov 2026
Wise's login CAPTCHA fails in the built-in browser ("Turnstile error 600010"), and a
Windows passkey prompt may pop up. Don't try to get past it. Use Claude in Chrome. If
several Chromes are connected, make sure the user can see the tab. The user signs in.
- Transactions > **Filters**: set the From and To dates and turn on **Show hidden
  transactions**, then Apply. Real spending can be hidden (an ATM withdrawal was), and
  the export follows these filters.
- **Export** > **CSV** > Export. Arrives as `transaction-history.csv`.
- Log off: go to `https://wise.com/logout`, then close the tab.
- If Chrome isn't available, ask the user to do the same and leave the file in Downloads.
- Check the file's date range from its contents, then save as `Wise_<from>_to_<to>.csv`
  (or an Excel statement as `Wise_AUD_<from>_to_<to>.xlsx`).
  Overlapping files are fine: transactions are de-duplicated by ID.
- The Wise balance comes from the newest Excel statement, carried forward through newer
  CSVs. Compare it with the balance Wise shows. A transfer paid straight from a bank card
  makes it too low; download an Excel statement to correct that.

### IOOF super (built-in browser works) - verified 5 Oct 2026
- Login: `https://login.portfolioonline.com.au/ioof` (IOOF Online, "Client login").
- Open the account (View) > menu (top left) > **Reports** > **Transaction statement**
  > period **Last 12 months** (the default) > Generate report. Takes ~10 seconds.
  Arrives as `<member number> - Transaction statement - <dd-mm-yyyy>.pdf`.
- Save as `Super_IOOF_TransactionStatement_<from>_to_<to>.pdf`.
- Log off: go to `https://client.portfolioonline.com.au/ioof/logout/`.

If a site's menus have changed, find the equivalent, then update this section so
next month is quicker.

## 3. Check and rebuild

1. Run `.venv\Scripts\python analyze.py` again.
2. Confirm every source in **Data up to** is now current.
3. Check bank balances against what the site showed (they should match to the cent).
4. If transactions show as **Uncategorised**, list them and ask the user which
   category each belongs to, then add rules to your rules file (`rules_file` in config.toml).
5. **Investigate "Needs a look"** (printed by analyze.py, produced by `checks.py`).
   For each alert, look into it yourself before involving the user:
   - Read the surrounding transactions in the statements (same merchant, same days,
     earlier months) and decide the most likely explanation.
   - Settle what the data can settle: e.g. a "duplicate" where the merchant sent a
     refund later, a "new merchant" that's an existing one under a new name (add a
     rules entry), a "balance gap" that a statement you already have closes.
   - Only bring the user the ones that need their knowledge or action, each with your
     best explanation and a suggested next step (e.g. "Telstra rose $52; check your
     plan", "Booktopia $64.95 twice on 26 Aug; dispute it if you only paid once").
   - Never dispute a charge, contact a merchant or move money yourself.
   If an alert type keeps flagging normal things, tune the threshold in checks.py and
   say what you changed.
6. Refresh `to_check` in `config.toml` with what still needs the user: remove what's done,
   add what you couldn't settle, one plain sentence each. Rerun `analyze.py` so the report shows it.
7. Summarise for the user: money you can use and total including super, change since
   last time, anything new in the insights, and the alerts you couldn't settle.
8. Commit any code changes (never statements, `report.html`, `config.toml`, your rules file
   or `known_gaps.md`).

## Known gaps

Gaps in your data (e.g. a few days missing between two statements) are noted in
`known_gaps.md` next to this project's code. It is never committed. Read it at the
start of each run, try to close what it lists, and update it as gaps are closed or found.
