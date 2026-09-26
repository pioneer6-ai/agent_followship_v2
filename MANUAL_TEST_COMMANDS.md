# Manual Test Commands

Commands for the calendar date-shift and booking-protection regressions, plus the
full suite. Results below are stamped with the revision and date they were
observed. Commands are POSIX shell; on Windows use your interpreter's path
(`.venv\Scripts\python.exe`) and `Get-Content` instead of `cat`.

Any interpreter that satisfies `requirements.txt` works — a conda env or a
`.venv` both do. Substitute your own in place of `python`.

## Date Shift Tests

```bash
python -m pytest tests/test_calendar_date_shift.py -v --tb=short \
  --junit-xml=test_results_date_shift.xml > test_date_shift_output.txt 2>&1
echo "Exit code: $?" >> test_date_shift_output.txt
```

**Result** (`c39c91f`, 2026-09-26): **7 passed, 0 failed, exit code 0**

> An earlier revision of this document recorded `5 passed, 2 failed (CSRF issue),
> Exit code: 1` on Windows. Those two failures are resolved; the file passes.

## Booking Protection Tests

```bash
python -m pytest tests/test_booking_protection.py -v --tb=short \
  --junit-xml=test_results_booking.xml > test_booking_output.txt 2>&1
echo "Exit code: $?" >> test_booking_output.txt
```

**Result** (`c39c91f`, 2026-09-26): **13 passed, 0 failed, exit code 0**

Both files together: `20 passed in 0.70s`.

## Full Test Suite

```bash
python -m pytest tests/ -q --junit-xml=test_results_full.xml > test_full_output.txt 2>&1
echo "Exit code: $?" >> test_full_output.txt
```

**Result** (`c39c91f`, 2026-09-26): **952 passed, 1 skipped, exit code 0** — see
`ARCHITECTURE.md` for the running count.

> Do not add `--ignore=tests/test_aws_tools.py`. AWS tests mock the SDK and pass
> without credentials, so excluding them would silently reduce coverage.

## View Results

```bash
cat test_date_shift_output.txt
cat test_booking_output.txt
tail -100 test_full_output.txt
grep -H "Exit code:" test_date_shift_output.txt test_booking_output.txt test_full_output.txt
```

These output files are working-tree scratch, not committed artifacts. Delete them
after reading.

## JUnit XML Files
- `test_results_date_shift.xml` - Date shift regression tests
- `test_results_booking.xml` - Booking protection tests
- `test_results_full.xml` - Full test suite

## Notes
- Date shift tests use Asia/Singapore timezone (UTC+8) to reproduce the bug
- The JavaScript fix in `calendar.js` CANNOT be verified by Python-only tests
- Browser verification required for the JavaScript date conversion fix

