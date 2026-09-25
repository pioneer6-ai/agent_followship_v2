"""
LLM-powered patient data parser.

This module uses Large Language Models to intelligently parse
patient data from various formats (CSV, Excel, JSON, plain text).
The LLM can handle different schemas, naming conventions, and
data structures automatically.
"""

import json
import csv
import io
import os
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional
import re

from core.models import PatientRecord, ContactChannel


def _cell_text(value: Any) -> str:
    """
    Render one worksheet cell as text for :meth:`_standardize_patient_data`.

    Excel stores everything typed, so a date arrives as a ``datetime`` and an
    integer id as a float. Left alone, a date would become
    ``2024-01-15 00:00:00``, which the date parser cannot read, and an id would
    gain a ``.0``.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.time() == datetime.min.time():
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


class LLMPatientParser:
    """
    Intelligent patient data parser using LLM.
    
    This parser can handle:
    - Different file formats (CSV, Excel, JSON, TXT)
    - Various column names and schemas
    - Missing or incomplete data
    - Different date formats
    - Inconsistent naming conventions
    """
    
    def __init__(self, use_llm: bool = False, api_key: Optional[str] = None):
        """
        Initialize the parser.
        
        Args:
            use_llm: Whether to use actual LLM API (requires API key)
            api_key: API key for LLM service. If None, reads from env vars:
                     AGENT_LLM_API_KEY -> LLM_API_KEY -> OPENAI_API_KEY
        """
        self.use_llm = use_llm
        # Resolve API key from env if not provided
        self.api_key = (
            api_key
            or os.environ.get("AGENT_LLM_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self.base_url = os.environ.get("AGENT_LLM_BASE_URL") or None
        self.model = os.environ.get("AGENT_LLM_MODEL") or "gpt-4o-mini"
    
    def parse_file(self, file_content: bytes, filename: str) -> List[Dict[str, Any]]:
        """
        Parse a patient data file using intelligent field mapping.
        
        Args:
            file_content: Raw file bytes
            filename: Original filename (used to determine format)
            
        Returns:
            List of patient dictionaries with standardized fields
        """
        # Determine file type
        if filename.endswith('.csv'):
            return self._parse_csv(file_content)
        elif filename.lower().endswith(('.xlsx', '.xlsm', '.xls')):
            return self._parse_excel(file_content, filename)
        elif filename.endswith('.json'):
            return self._parse_json(file_content)
        elif filename.endswith('.txt'):
            return self._parse_text(file_content)
        else:
            raise ValueError(f"Unsupported file format: {filename}")
    
    def _parse_csv(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse CSV file with intelligent column mapping."""
        text = content.decode('utf-8')
        csv_file = io.StringIO(text)
        reader = csv.DictReader(csv_file)

        rows = list(reader)
        if not rows:
            return []

        if self.use_llm and self.api_key:
            try:
                return self._parse_all_with_llm(rows)
            except Exception as e:
                print(f"[llm_parser] LLM batch parse failed, falling back to rules: {e}")

        return [self._rule_based_standardize(row) for row in rows]
    
    def _parse_excel(self, content: bytes, filename: str = "") -> List[Dict[str, Any]]:
        """
        Parse an ``.xlsx`` / ``.xlsm`` workbook.

        ``openpyxl`` is imported lazily so that the rest of the project keeps
        working when it is absent -- and so that the failure explains itself
        instead of surfacing as a ``UnicodeDecodeError`` from reading the ZIP
        container as text.
        """
        if filename.lower().endswith('.xls'):
            raise ValueError(
                "Legacy .xls workbooks are not supported. Save the sheet as "
                ".xlsx (or .csv) and upload that."
            )
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError(
                "Reading .xlsx needs the openpyxl package: pip install openpyxl. "
                "Alternatively save the patient list as .csv."
            ) from exc

        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:
            raise ValueError(f"Could not read the workbook: {exc}") from exc

        try:
            rows = workbook.active.iter_rows(values_only=True)
            try:
                header = [_cell_text(cell) for cell in next(rows)]
            except StopIteration:
                return []

            patients: List[Dict[str, Any]] = []
            for row in rows:
                values = [_cell_text(cell) for cell in (row or ())]
                if not any(values):
                    continue
                record = {
                    header[index]: values[index]
                    for index in range(min(len(header), len(values)))
                    if header[index]
                }
                patients.append(record)

            if self.use_llm and self.api_key and patients:
                try:
                    return self._parse_all_with_llm(patients)
                except Exception as e:
                    print(f"[llm_parser] LLM batch parse failed, falling back to rules: {e}")

            return [self._rule_based_standardize(p) for p in patients]
        finally:
            workbook.close()
    
    def _parse_json(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse JSON file with flexible schema."""
        text = content.decode('utf-8')
        data = json.loads(text)
        
        # Handle both array of patients and nested structures
        if isinstance(data, list):
            patients = data
        elif isinstance(data, dict):
            # Try common keys
            patients = data.get('patients', data.get('data', data.get('records', [data])))
        else:
            raise ValueError("Unexpected JSON structure")

        if self.use_llm and self.api_key and patients:
            try:
                return self._parse_all_with_llm(patients)
            except Exception as e:
                print(f"[llm_parser] LLM batch parse failed, falling back to rules: {e}")

        return [self._rule_based_standardize(p) for p in patients]
    
    def _parse_text(self, content: bytes) -> List[Dict[str, Any]]:
        """
        Parse plain text file using LLM or pattern matching.
        
        Can handle formats like:
        - Line-separated records
        - Natural language descriptions
        - Mixed formats
        """
        text = content.decode('utf-8')
        lines = text.strip().split('\n')
        
        patients = []
        current_patient = {}
        
        for line in lines:
            line = line.strip()
            if not line:
                if current_patient:
                    patients.append(current_patient)
                    current_patient = {}
                continue
            
            # Try to extract key-value pairs
            if ':' in line:
                key, value = line.split(':', 1)
                current_patient[key.strip()] = value.strip()
            else:
                # Use LLM to parse natural language
                # For demo, try simple pattern matching
                current_patient.setdefault('raw_text', []).append(line)
        
        if current_patient:
            patients.append(current_patient)

        if self.use_llm and self.api_key and patients:
            try:
                return self._parse_all_with_llm(patients)
            except Exception as e:
                print(f"[llm_parser] LLM batch parse failed, falling back to rules: {e}")

        return [self._rule_based_standardize(p) for p in patients]
    
    def _standardize_patient_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Standardize a single record. Used only for the single-record fallback path.
        Batch files go through _parse_all_with_llm instead.
        """
        return self._rule_based_standardize(raw_data)

    def _parse_with_llm_api(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        NOT used directly — batch parsing is handled by _parse_all_with_llm.
        This exists as a single-record fallback only.
        """
        return self._parse_all_with_llm([raw_data])[0]

    def _parse_all_with_llm(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        ONE LLM call for the entire file.

        Strategy:
        1. Collect every unique field name and sample values across all records.
        2. Ask the LLM to benchmark/classify each field name -> what category it
           actually holds (name, phone, email, date, treatment, id, interval,
           noshows, language, unknown).
        3. Apply that field-type map to every record deterministically — no more
           LLM calls needed per record.

        This costs exactly 1 API call regardless of how many patients are in
        the file, staying well within free-tier quota limits.
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError(
                "The 'openai' package is required for LLM parsing. "
                "Install it with: pip install openai"
            )

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        # --- Step 1: Build a sample of all field names + example values ---
        field_samples: Dict[str, List[str]] = {}
        for record in records:
            for key, val in record.items():
                if val is not None and str(val).strip():
                    field_samples.setdefault(key, [])
                    if len(field_samples[key]) < 3:   # max 3 samples per field
                        field_samples[key].append(str(val).strip())

        # --- Step 2: Ask LLM to classify each field name ---
        field_list = json.dumps(field_samples, indent=2, ensure_ascii=False)

        prompt = f"""You are a medical data schema analyst.

Below is a dictionary where each key is a field name from a patient data file,
and the value is a list of sample values found in that field across multiple records.
The field LABELS may be wrong or completely random — the VALUES are real patient data.

Your task: For each field name, classify what the VALUES actually represent.

Categories:
- "patient_id"       : alphanumeric ID codes like "PT-001", "A00192", "P99012"
- "name"             : human full names like "John Smith", "Tan Wei Ming"
- "phone"            : phone numbers (digits, +, dashes, spaces) — NOT email
- "email"            : strings containing @ symbol
- "whatsapp"         : phone numbers designated for WhatsApp
- "last_visit_date"  : dates in any format
- "treatment_type"   : medical/dental procedure names
- "recall_interval_days" : small integers representing days (7–365)
- "no_show_history"  : very small integers (0–10) representing missed appointments
- "language"         : 2-letter language codes (en, ms, zh) or language names
- "unknown"          : anything that doesn't fit above

Field names and sample values:
{field_list}

Return ONLY a valid JSON object mapping each field name to its category. Example:
{{
  "patient_id": "name",
  "name": "phone",
  "phone": "email",
  ...
}}

Return only the JSON object, no explanation."""

        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0,
        )

        raw_response = response.choices[0].message.content.strip()
        if raw_response.startswith("```"):
            raw_response = re.sub(r"^```[a-z]*\n?", "", raw_response)
            raw_response = re.sub(r"\n?```$", "", raw_response)

        field_type_map: Dict[str, str] = json.loads(raw_response)
        print(f"[llm_parser] Field type map inferred: {field_type_map}")

        # --- Step 3: Apply field_type_map to every record ---
        results = []
        for record in records:
            results.append(self._apply_field_map(record, field_type_map))
        return results

    def _apply_field_map(
        self, record: Dict[str, Any], field_type_map: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Use the LLM-inferred field_type_map to build a standardized patient
        dict from one raw record. Pure deterministic logic — no LLM calls.
        """
        # Bucket values by their inferred category
        buckets: Dict[str, List[str]] = {}
        for key, val in record.items():
            category = field_type_map.get(key, "unknown")
            if val is not None and str(val).strip():
                buckets.setdefault(category, []).append(str(val).strip())

        def first(cat: str) -> Optional[str]:
            vals = buckets.get(cat, [])
            return vals[0] if vals else None

        # Build contact_info
        contact_info: Dict[str, str] = {}
        phone = first("phone")
        email = first("email")
        whatsapp = first("whatsapp")
        if phone:
            contact_info["sms"] = phone
            contact_info["phone_call"] = phone
        if whatsapp:
            contact_info["whatsapp"] = whatsapp
        elif phone:
            contact_info["whatsapp"] = phone
        if email:
            contact_info["email"] = email

        # Parse last visit date
        last_visit_date = self._parse_date(first("last_visit_date"))

        # Recall interval — must be a sensible integer
        raw_interval = first("recall_interval_days")
        try:
            recall_interval = int(raw_interval) if raw_interval else None
            if recall_interval and not (7 <= recall_interval <= 730):
                recall_interval = None
        except (ValueError, TypeError):
            recall_interval = None

        # No-show history — must be a small integer
        raw_noshows = first("no_show_history")
        try:
            no_shows = int(raw_noshows) if raw_noshows else 0
            if no_shows > 20:
                no_shows = 0
        except (ValueError, TypeError):
            no_shows = 0

        treatment_raw = first("treatment_type")
        treatment = self._normalize_treatment_type(treatment_raw)

        if recall_interval is None:
            recall_interval = self._default_recall_interval(treatment)

        # days_overdue
        if isinstance(last_visit_date, date):
            days_since = (date.today() - last_visit_date).days
            days_overdue = max(0, days_since - recall_interval)
        else:
            days_overdue = 0

        standardized = {
            "patient_id": first("patient_id") or self._generate_patient_id(),
            "name": first("name") or "Unknown Patient",
            "contact_info": contact_info,
            "preferred_channel": "sms",
            "last_visit_date": last_visit_date.isoformat() if isinstance(last_visit_date, date) else str(last_visit_date),
            "treatment_type": treatment,
            "recall_interval_days": recall_interval,
            "no_show_history": no_shows,
            "language": first("language") or "en",
            "days_overdue": days_overdue,
        }
        return standardized

    def _rule_based_standardize(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Rule-based fallback: maps fields by trying known synonym key names.
        Used when use_llm=False or when the LLM call fails.
        """
        # Normalize keys (make lowercase and remove special chars)
        normalized = {k.lower().replace(' ', '_').replace('-', '_'): v 
                      for k, v in raw_data.items()}
        
        # Map to standard fields using flexible matching
        standardized = {}
        
        # Patient ID
        standardized['patient_id'] = self._find_field(
            normalized, 
            ['patient_id', 'id', 'patient_number', 'patientid', 'pid', 'mrn', 'medical_record_number']
        ) or self._generate_patient_id()
        
        # Name
        standardized['name'] = self._find_field(
            normalized,
            ['name', 'patient_name', 'full_name', 'patientname', 'patient']
        ) or "Unknown Patient"
        
        # Contact information
        phone = self._find_field(
            normalized,
            ['phone', 'telephone', 'mobile', 'phone_number', 'contact', 'cell']
        )
        email = self._find_field(
            normalized,
            ['email', 'e_mail', 'email_address', 'mail']
        )
        whatsapp = self._find_field(
            normalized,
            ['whatsapp', 'whats_app', 'wa']
        )
        
        contact_info = {}
        if phone:
            contact_info[ContactChannel.SMS.value] = phone
            contact_info[ContactChannel.PHONE_CALL.value] = phone
        if whatsapp:
            contact_info[ContactChannel.WHATSAPP.value] = whatsapp
        if email:
            contact_info[ContactChannel.EMAIL.value] = email
        
        standardized['contact_info'] = contact_info
        standardized['preferred_channel'] = ContactChannel.SMS.value
        
        # Last visit date
        last_visit_str = self._find_field(
            normalized,
            ['last_visit', 'last_visit_date', 'lastvisit', 'visit_date', 'last_appointment']
        )
        last_visit_date = self._parse_date(last_visit_str)
        standardized['last_visit_date'] = last_visit_date.isoformat() if isinstance(last_visit_date, date) else str(last_visit_date)
        
        # Treatment type
        treatment = self._find_field(
            normalized,
            ['treatment', 'treatment_type', 'procedure', 'service', 'care_type']
        )
        standardized['treatment_type'] = self._normalize_treatment_type(treatment)
        
        # Recall interval
        recall_interval = self._find_field(
            normalized,
            ['recall_interval', 'interval', 'follow_up_days', 'recall_days']
        )
        standardized['recall_interval_days'] = int(recall_interval) if recall_interval else self._default_recall_interval(standardized['treatment_type'])
        
        # No-show history
        no_shows = self._find_field(
            normalized,
            ['no_shows', 'no_show_history', 'missed_appointments', 'noshows']
        )
        standardized['no_show_history'] = int(no_shows) if no_shows else 0
        
        # Language
        language = self._find_field(
            normalized,
            ['language', 'lang', 'preferred_language']
        )
        standardized['language'] = language if language else 'en'
        
        # Calculate days overdue
        if isinstance(last_visit_date, date):
            days_since = (date.today() - last_visit_date).days
            standardized['days_overdue'] = max(0, days_since - standardized['recall_interval_days'])
        else:
            standardized['days_overdue'] = 0
        
        return standardized
    
    def _find_field(self, data: Dict[str, Any], possible_keys: List[str]) -> Optional[str]:
        """Find a field value by trying multiple possible key names."""
        for key in possible_keys:
            if key in data and data[key]:
                return str(data[key])
        return None
    
    def _parse_date(self, date_str: Optional[str]) -> date:
        """
        Parse date string in various formats.
        
        Handles: YYYY-MM-DD, MM/DD/YYYY, DD-MM-YYYY, etc.
        """
        if not date_str:
            # Default to 6 months ago for demo
            return date.today() - timedelta(days=180)
        
        date_str = str(date_str).strip()
        
        # Try common formats
        formats = [
            '%Y-%m-%d',
            '%m/%d/%Y',
            '%d/%m/%Y',
            '%Y/%m/%d',
            '%d-%m-%Y',
            '%m-%d-%Y',
            '%Y%m%d'
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        
        # If all fail, use default
        return date.today() - timedelta(days=180)
    
    def _normalize_treatment_type(self, treatment: Optional[str]) -> str:
        """Normalize treatment type to standard categories."""
        if not treatment:
            return 'checkup'
        
        treatment = treatment.lower()
        
        # Map various terms to standard types
        if any(word in treatment for word in ['surgery', 'surgical', 'extraction', 'implant']):
            return 'post_surgery'
        elif any(word in treatment for word in ['root canal', 'endodontic', 'rct']):
            return 'root_canal_followup'
        elif any(word in treatment for word in ['cavity', 'filling', 'restoration']):
            return 'cavity_treatment'
        elif any(word in treatment for word in ['braces', 'orthodontic', 'ortho']):
            return 'orthodontic_adjustment'
        elif any(word in treatment for word in ['gum', 'periodontal', 'perio']):
            return 'periodontal_maintenance'
        elif any(word in treatment for word in ['cleaning', 'prophylaxis', 'hygiene']):
            return 'cleaning'
        else:
            return 'checkup'
    
    def _default_recall_interval(self, treatment_type: str) -> int:
        """Get default recall interval based on treatment type."""
        intervals = {
            'post_surgery': 21,
            'root_canal_followup': 14,
            'cavity_treatment': 30,
            'orthodontic_adjustment': 28,
            'periodontal_maintenance': 90,
            'cleaning': 180,
            'checkup': 180
        }
        return intervals.get(treatment_type, 180)
    
    def _generate_patient_id(self) -> str:
        """Generate a unique patient ID."""
        import random
        import string
        return 'P' + ''.join(random.choices(string.digits, k=6))
    
    def parse_with_llm(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Use actual LLM to parse patient data.
        Calls the Gemini/OpenAI-compatible API to extract correct fields
        even when values are in wrong keys.
        """
        if self.api_key:
            return self._parse_with_llm_api(raw_data)
        return self._rule_based_standardize(raw_data)
