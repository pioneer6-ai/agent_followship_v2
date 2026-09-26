# Deployment evidence 3 - Runtime and repository state

Captured: 2026-09-25T09:47:34+00:00

## What this proves

The deployment was validated against a specific, identifiable revision of the code on a specific interpreter with specific library versions. Anyone re-running the project can confirm they are comparing like with like before judging any other evidence in this pack.

## Interpreter

- Python: 3.14.5 (CPython)
- Executable: `/Users/martinchen/agent_followship_v2/.venv/bin/python`
- Platform: macOS-27.0-arm64-arm-64bit-Mach-O

## Repository

- Branch: `master`
- Commit: `fbcc6c1ca09a7624fd5f01016e674cacdc4a5287`
- Commit subject: Merge pull request #3 from pioneer6-ai/feature/patient-portal
- Commit date: 2026-09-25T13:41:07+08:00
- Working tree clean: no

Files changed relative to that commit (this analysis work only):

```
M .gitignore
 M QUICKSTART.md
 M README.md
 M tools/demo_tool_use.py
?? proposal/
```

## Installed dependencies

| Package | Version |
| --- | --- |
| boto3 | 1.35.36 |
| botocore | 1.35.99 |
| anthropic | not installed |
| flask | 3.0.0 |
| openpyxl | 3.1.2 |
| certifi | 2026.7.22 |

## Codebase size

| Module | Python files | Lines |
| --- | ---: | ---: |
| `agent/` | 9 | 4,635 |
| `core/` | 11 | 1,978 |
| `tools/` | 13 | 6,118 |
| `utils/` | 2 | 1,022 |
| `web/` | 5 | 3,477 |
| `scheduling/` | 4 | 1,321 |
| `scripts/` | 2 | 288 |
| `tests/` | 30 | 12,119 |
| `(root)/` | 2 | 1,115 |

Total: 78 files, 32,073 lines.

