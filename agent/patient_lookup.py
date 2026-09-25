"""
Patient and case lookup services for email conversation routing.

Provides mappings from email addresses to patients and patient IDs to active cases.
"""

from typing import Dict, Optional
from core.models import PatientRecord, FollowUpCase
from core.data_access import PatientDataStore


class PatientLookupService:
    """
    Service for looking up patients and their active follow-up cases.
    
    Used by email conversation manager to route inbound emails to the
    correct patient context.
    """
    
    def __init__(self, data_store: PatientDataStore):
        """
        Initialize lookup service.
        
        Args:
            data_store: Patient data store for patient and case queries
        """
        self.data_store = data_store
    
    def get_patient_by_email(self, email_address: str) -> Optional[PatientRecord]:
        """
        Look up patient by email address.
        
        Args:
            email_address: Patient's email address (case-insensitive)
            
        Returns:
            PatientRecord if found, None otherwise
        """
        email_lower = email_address.lower().strip()
        
        # Get all patients and find matching email
        patients = self.data_store.get_all_active_patients()
        
        for patient in patients:
            from core.models import ContactChannel
            patient_email = patient.contact_info.get(ContactChannel.EMAIL, "")
            if patient_email and patient_email.lower().strip() == email_lower:
                return patient
        
        return None
    
    def get_patient_by_email_lookup(self) -> Dict[str, PatientRecord]:
        """
        Build dict mapping email addresses to patients.
        
        Returns:
            Dict with lowercase email addresses as keys, PatientRecords as values
        """
        lookup = {}
        patients = self.data_store.get_all_active_patients()
        
        from core.models import ContactChannel
        for patient in patients:
            email = patient.contact_info.get(ContactChannel.EMAIL, "")
            if email:
                lookup[email.lower().strip()] = patient
        
        return lookup
    
    def get_active_case(self, patient_id: str) -> Optional[FollowUpCase]:
        """
        Get active follow-up case for a patient.
        
        Args:
            patient_id: Patient identifier
            
        Returns:
            FollowUpCase if patient has active case, None otherwise
        """
        # Get patient record first
        patient = self.data_store.get_patient_by_id(patient_id)
        if not patient:
            return None
        
        # For testing/simple cases, assume any patient could have an active case
        # In production, this would query the case management system
        from datetime import date
        from core.models import UrgencyLevel
        
        days_overdue = (date.today() - patient.last_visit_date).days - patient.recall_interval_days
        
        # Only return case if actually overdue
        if days_overdue > 0:
            return FollowUpCase(
                patient=patient,
                days_overdue=days_overdue,
                urgency=UrgencyLevel.MEDIUM,
                reason=f"Follow-up due for {patient.treatment_type}"
            )
        
        return None
    
    def get_active_cases_lookup(self) -> Dict[str, FollowUpCase]:
        """
        Build dict mapping patient_ids to active follow-up cases.
        
        Returns:
            Dict with patient_ids as keys, FollowUpCases as values
        """
        lookup = {}
        patients = self.data_store.get_all_active_patients()
        
        for patient in patients:
            case = self.get_active_case(patient.patient_id)
            if case:
                lookup[patient.patient_id] = case
        
        return lookup
