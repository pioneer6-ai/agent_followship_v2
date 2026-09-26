# Deployment evidence 5 - Running dashboard

Captured: 2026-09-25T09:06:57+00:00

## What this proves

The deployed application starts, serves its staff dashboard and its
patient portal, answers its JSON API and accepts operator commands. The
screenshots are the rendered UI as a browser receives it, and the JSON
files are the unedited API responses the dashboard consumes while
rendering.

- Server: `http://127.0.0.1:54301` (werkzeug, bound to localhost)
- Screenshot: `proposal/evidence/screenshots/dashboard_overview.png` (captured, 475,711 bytes)
- Viewport: 1600x1400

## Patient portal (added in this revision)

The same process also serves the patient-facing portal. A staff-side endpoint mints a link token, and that URL was then rendered, so the page shown is produced by the running application rather than mocked up.

- Portal URL path: `/patient/QITt-c7mJE6WhJtNDUIjdVGqpQPPrnJs` (token is scoped to one patient)
- Screenshot: `proposal/evidence/screenshots/patient_portal.png` (captured, 736,923 bytes)

`GET /api/patient-portal/<token>/status`:

```json
{
  "booking_window_days": 7,
  "next_followup_at": null,
  "patient_name": "Sarah Johnson",
  "status": "awaiting_reply",
  "success": true,
  "treatment_type": "post_surgery"
}
```

## Operator commands exercised

The page was not captured idle. Before the screenshot the same endpoints
the dashboard's buttons call were invoked, so the rendered page shows
cases this process produced rather than an empty board:

- `POST /api/run-cycle` -> `success=True`, `cases_processed=11`
- `POST /api/simulate-reply` for `P001` (`"Yes, I'd like to schedule an appointment"`) -> status `message_sent` to `awaiting_reply`
- `POST /api/simulate-reply` for `P007` (`"No thanks, not needed right now"`) -> status `message_sent` to `declined`
- `POST /api/simulate-reply` for `P004` (`"How much will this cost with my insurance?"`) -> status `message_sent` to `escalated`

> The dashboard is populated with the project's built-in sample data,
> not real patients. It demonstrates the operator experience and the
> operating surface; it is not a record of live patient activity.

> This run's audit log was written to a temporary directory (`followup-dashboard-*/audit_log.json`) and
> discarded, so exercising the agent here cannot alter the audit trail
> deployment evidence 1 is computed from.

## Live API responses

`GET /api/status`:

```json
{
  "audit_summary": {
    "successful_bookings": 0,
    "total_communications": 17,
    "total_decisions": 12,
    "total_log_entries": 29
  },
  "cases_by_status": {
    "awaiting_reply": 1,
    "booked": 0,
    "declined": 1,
    "escalated": 1,
    "message_sent": 8,
    "opted_out": 0,
    "pending": 0,
    "pending_future_availability": 0
  },
  "cases_by_urgency": {
    "critical": 3,
    "high": 2,
    "low": 3,
    "medium": 3
  },
  "escalated_cases": 1,
  "last_updated": "2026-09-25T17:06:55.280205",
  "total_active_cases": 11,
  "total_patients": 12
}
```

`GET /api/cases` returned 11 cases (first entry shown):

```json
{
  "conversation_length": 6,
  "days_overdue": 69,
  "last_contacted": "2026-09-25",
  "last_visit": "2026-06-27",
  "patient_id": "P001",
  "patient_name": "Sarah Johnson",
  "preferred_channel": "sms",
  "reminder_count": 1,
  "status": "awaiting_reply",
  "treatment_type": "post_surgery",
  "urgency": "critical"
}
```
