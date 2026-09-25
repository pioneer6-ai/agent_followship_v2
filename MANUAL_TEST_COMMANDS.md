# Manual Test Commands

## Run Date Shift Tests (Already Completed)
```powershell
.venv\Scripts\python.exe -m pytest tests/test_calendar_date_shift.py -v --tb=short --junit-xml=test_results_date_shift.xml > test_date_shift_output.txt 2>&1
echo "Exit code: $LASTEXITCODE" >> test_date_shift_output.txt
```

**Result**: test_date_shift_output.txt created - **5 passed, 2 failed (CSRF issue), Exit code: 1**

## Run Booking Protection Tests
```powershell
.venv\Scripts\python.exe -m pytest tests/test_booking_protection.py -v --tb=short --junit-xml=test_results_booking.xml > test_booking_output.txt 2>&1
echo "Exit code: $LASTEXITCODE" >> test_booking_output.txt
```

## Run Full Test Suite
```powershell
.venv\Scripts\python.exe -m pytest tests/ --ignore=tests/test_aws_tools.py -v --tb=line --junit-xml=test_results_full.xml > test_full_output.txt 2>&1
echo "Exit code: $LASTEXITCODE" >> test_full_output.txt
```

## View Results
```powershell
# View date shift test results
Get-Content test_date_shift_output.txt

# View booking test results
Get-Content test_booking_output.txt

# View full test results (last 100 lines)
Get-Content test_full_output.txt -Tail 100

# Check exit codes
Select-String "Exit code:" test_date_shift_output.txt, test_booking_output.txt, test_full_output.txt
```

## JUnit XML Files
- `test_results_date_shift.xml` - Date shift regression tests
- `test_results_booking.xml` - Booking protection tests
- `test_results_full.xml` - Full test suite

## Notes
- Date shift tests use Asia/Singapore timezone (UTC+8) to reproduce the bug
- 2 CSRF-related failures in date shift tests - these are test infrastructure issues, not date logic issues
- The JavaScript fix in calendar.js CANNOT be verified by Python-only tests
- Browser verification required for JavaScript date conversion fix
