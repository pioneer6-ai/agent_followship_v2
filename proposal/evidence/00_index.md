# Evidence pack index

Generated: 2026-09-25T09:47:42+00:00

Every artifact listed here was produced by a script in
`proposal/scripts/` against the deployment revision recorded in
evidence 3. Hashes are of the artifact as it exists in this pack, so a
reviewer can confirm they are reading the same bytes the proposal was
written from.

## Reports

### `01_agent_decision_audit.md`

*Deployment evidence 1 - Agent decisions and audit trail*

The agent runs its full Perceive -> Decide -> Act -> Observe loop and records a durable, queryable audit trail for every decision and every delivery attempt. The counts below are computed directly from the artifact whose hash is recorded here, so they can be re-derived by a reviewer instead of taken on trust.

- Size: 5,155 bytes
- SHA-256: `1b9c73463a6718c18e0373640ef64cc77a918d383295a1c5b460fbca3f573f41`

### `02_aws_messaging_config.md`

*Deployment evidence 2 - AWS messaging configuration*

The messaging transport is wired to named AWS services in a named region, it sends from a verified identity, and it refuses recipients outside an explicit allow-list. The last section pins every claim to a file and line in the deployed revision.

- Size: 2,302 bytes
- SHA-256: `2dab4ce38689760c3ef5184f86c2b5666abf9173876491f6228c863d7ee43f01`

### `03_runtime_and_repository.md`

*Deployment evidence 3 - Runtime and repository state*

The deployment was validated against a specific, identifiable revision of the code on a specific interpreter with specific library versions. Anyone re-running the project can confirm they are comparing like with like before judging any other evidence in this pack.

- Size: 1,471 bytes
- SHA-256: `d9e43d0f5876f536971a03daa7d6831ca02bde14c3478533c21ec42536aaee9e`

### `04_test_suite.md`

*Deployment evidence 4 - Automated test suite*

The revision identified in evidence 3 passes its full automated test suite on the recorded interpreter. The suite is the executable form of the behaviour contracts: tool schemas, error taxonomy, provider selection, agent loop control flow, delivery gating and the web API. The verbatim pytest output is stored alongside this file.

- Size: 2,911 bytes
- SHA-256: `a58c7fb56a9604730087eb7e0ea15d8db91d147e7eae2c8736f62901b3828b54`

### `05_dashboard_runtime.md`

*Deployment evidence 5 - Running dashboard*

The deployed application starts, serves its staff dashboard and its patient portal, answers its JSON API and accepts operator commands. The screenshots are the rendered UI as a browser receives it, and the JSON files are the unedited API responses the dashboard consumes while rendering. - Server: `http://127.0.0.1:54301` (werkzeug, bound to localhost) - Screenshot: `proposal/evidence/screenshots/dashboard_overview.png` (captured, 475,711 bytes) - Viewport: 1600x1400

- Size: 3,368 bytes
- SHA-256: `9a800d1e377ede06f2f538091a763c46822b3e98a4f1b37b3b8daadfbe3d9a52`

### `06_live_delivery.md`

*Deployment evidence 6 - Live end-to-end delivery*

A message was handed to the real provider through the agent's own tool layer and the provider answered. This is the only artifact in the pack that evidences transmission rather than behaviour, which is why it is run only with explicit approval.

- Size: 2,092 bytes
- SHA-256: `ba29e0695c1cc155a25e605f7cd89d0d6dc77191d4c38604e9d5f108e24e4553`

### `07_delivery_confirmation.md`

*Deployment evidence 7 - Delivery confirmation*

Evidence 6 shows the provider accepted a message. This artifact shows what happened to it afterwards, from two independent vantage points: the provider's own delivery telemetry and the recipient mailbox. These are different claims and are kept separate on purpose.

- Size: 2,342 bytes
- SHA-256: `5fd63e74232ac2e1453f2dcd16272d33a015548bd6ff592d747598ffae82780d`

## Supporting artifacts

| Artifact | Kind | Size | SHA-256 (first 16) |
| --- | --- | ---: | --- |
| `artifacts/audit_log.generation.json` | raw artifact | 4,782 | `42da6d67abc3fdad` |
| `artifacts/audit_log_snapshot.json` | raw artifact | 261,297 | `76551f7389296eda` |
| `artifacts/pytest_output.txt` | raw artifact | 2,216 | `1da74e80729d72a8` |
| `screenshots/dashboard_cases_api.json` | visual artifact | 3,824 | `393bcba9f050eb10` |
| `screenshots/dashboard_overview.png` | visual artifact | 475,711 | `3c4e06f485f6bf9d` |
| `screenshots/dashboard_status_api.json` | visual artifact | 569 | `acb61130deebd2a4` |
| `screenshots/patient_portal.png` | visual artifact | 736,923 | `b919902b944a98d3` |

## Provenance of this index

The index is generated, not maintained by hand: adding a report to this
directory and re-running `capture_evidence_index.py` adds it here. There
is no list to forget to update.
