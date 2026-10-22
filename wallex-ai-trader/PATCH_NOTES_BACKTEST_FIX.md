# Backtest reporting fix (2026-10-22)

## Changes
- The final equity-curve point is now updated/appended after final liquidation, so the curve ends at the same post-liquidation cash balance used by `backtest_report.json`.
- JSON/CSV output writers in `write_outputs` now use explicit UTF-8 and context managers, avoiding Windows cp1256 Unicode encoding failures and unclosed output handles.
- The manifest reader now uses a context manager.
- Added a regression test that leaves a position open until end-of-backtest and verifies the final equity point agrees with the report.

## Validation
- `python -m unittest discover -s tests -t . -p "test_*.py" -v`
- Result: 58 tests run, OK, 1 skipped (the optional real-captured-fixture test; no fixture was available in the test package).

## Important
This patch fixes reporting/encoding consistency; it does not improve strategy performance or establish profitability. Do not use synthetic data as evidence of profitability. Keep the bot in shadow mode until a longer real-data backtest and paper/shadow forward test have been reviewed.
