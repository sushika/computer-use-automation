# Legacy CU back office

Flask stand-in for a 1990s credit-union intranet (`SPEC.md` §1). Deliberately hostile markup (iframe, nested tables, ASP.NET-style IDs, `input type="button"` + `onclick`) with real `<label>` associations so the perception layer can read the accessibility tree.

## Run

From the repo root, with the project venv active:

```
python apps/legacy_cu/app.py
```

Open [http://127.0.0.1:5001/](http://127.0.0.1:5001/)

- **Teller ID:** any non-empty value (e.g. `jdoe`)
- **Password:** `demo123`

## Seed members

| Member ID | Name | Primary savings |
|-----------|------|-----------------|
| 100241 | Eleanor Vasquez | $12,847.55 |
| 100387 | Thomas Okonkwo | $4,102.00 |
| 100512 | Priya Nandakumar | $89,330.18 |

Search by member ID or last name, open the record, then **Open new subaccount** → confirmation.

## Failure injection

Append `?inject=<flag>` to the shell URL or any content URL (links and forms keep the flag). Each flag is deterministic.

| Flag | What happens |
|------|----------------|
| `not_found` | Search and member lookup always return MEM-404 |
| `validation` | Search POST and new-subaccount POST fail with VAL-* errors even when input is valid |
| `permission` | Member detail / new subaccount / confirm return AUTH-4013 |
| `dialog` | Host System Message overlay (`role="dialog"`) on content pages; OK dismisses, Cancel returns to search |
| `timeout` | Authenticated pages drop the session and show ERR-440 logon |
| `slow` | Content responses sleep 6 seconds (shell and rates frame do not) |

Examples:

```
http://127.0.0.1:5001/?inject=slow
http://127.0.0.1:5001/MemberSearch.aspx?inject=not_found
http://127.0.0.1:5001/MemberDetail.aspx?member_id=100241&inject=permission
```
