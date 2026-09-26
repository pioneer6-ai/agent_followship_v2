# Test Patient Data Files

This folder contains sample patient list files in various formats to test the upload and LLM parsing functionality.

## 📁 Files Included:

### 1. **hospital_patients_format1.csv** 
Standard English format with clean headers
- 8 patients
- Standard date format: YYYY-MM-DD
- Common field names

### 2. **dental_clinic_patients.csv**
Chinese format (测试中文支持)
- 5 patients
- Shows multi-language support
- Chinese headers and content

### 3. **patient_list_irregular.csv**
Messy format with inconsistent naming
- 6 patients
- Mixed date formats (MM/DD/YYYY, DD-MM-YYYY, YYYY-MM-DD)
- Inconsistent field names (mobile vs phone, mail_address vs email)
- Tests LLM's ability to handle variations

### 4. **patients.json**
JSON format with nested structure
- 4 patients
- Nested contact information
- Different field naming conventions
- Tests JSON parsing capabilities

### 5. **patient_notes.txt**
Natural language format (hardest to parse)
- 5 patients
- Free-form text with patient information
- Inconsistent formatting
- Tests LLM's natural language understanding

### 6. **duplicate_test.csv**
Deliberately overlaps `hospital_patients_format1.csv` to test de-duplication
- 3 patients: `H001` and `H002` repeat rows from format 1 verbatim, plus one new
  `H999` that exists nowhere else
- Upload it *after* importing format 1 and confirm the importer skips the two
  known patients instead of creating duplicates

### 7. **messy_patients.json**
Adversarial JSON whose field *values* are shifted
- Every value sits under the wrong key (`name` holds a phone number, `phone` holds
  a name, `email` holds a number, and so on)
- Tests whether the parser maps fields by meaning rather than by position

### 8. **demo_21_critical_patients.csv**
Purpose-built for the Review Patient Messages workflow, not for parser testing
- 21 patients, all ~214 days overdue, so every one lands at `critical` urgency
- Every row's phone and email is the **verified test contact**
  (`+6583536885` / `martinchenonly1@gmail.com`), so a live run can walk the whole
  draft -> edit -> Confirm Selected workflow without messaging anyone real
- Upload it, import it, then open `/staff/outreach`: the import auto-runs a
  cycle, so 21 drafts are waiting

### 9. **escalated_cases_test/**
Escalation fixtures, not patient-upload input
- `escalated_cases.json` — 8 sample cases (3 critical, 3 high, 2 normal) with a
  metadata block describing the distribution
- `ESC-001.json` … `ESC-008.json` — the same cases as individual files
- Used to exercise the escalation path; these are loaded by the escalation tooling,
  not by the upload parser

## 🧪 How to Test:

1. Start the Flask application:
   ```bash
   python -m web.app
   ```

2. Open http://localhost:8080 in your browser

3. Click the **📤 Upload Patient List** button

4. Upload any of these test files

5. Watch the AI parse the data automatically

6. Review the parsed patients

7. Click **Import All Patients** to add them to the system

8. Check **Active Follow-up Cases** to see the new patients

## ✅ Expected Results:

All files should be parsed successfully, with the AI:
- Recognizing different field names
- Converting various date formats
- Mapping treatment descriptions to standard types
- Calculating days overdue
- Handling missing data gracefully

## 📝 Notes:

- The irregular format file is specifically designed to test the parser's robustness
- The Chinese format file tests multi-language support
- The text file tests natural language parsing (most challenging)
- All test patients have realistic data based on dental clinic scenarios
