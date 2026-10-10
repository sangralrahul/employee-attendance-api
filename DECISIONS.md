# DECISIONS.md

1. **Indexes.** `employees`: `{emp_code:1}` unique (identity + the create race); `{department:1, emp_code:1}` (filtered list, sorted for free); `{department:1, joined_on:1}` and `{joined_on:1}` (summary and trend start from employees by join date). `attendance_logs`: `{emp_code:1, date:1}` unique (the punch-in race, per-employee month, the `$lookup` from employees); `{date:-1, emp_code:1}` (default listing sort, month ranges for the leaderboard); `{status:1, date:-1, emp_code:1}` (listing filtered by status). Rejected: `{emp_code:1, punch_in:-1}` for punch-out — the `emp_code` prefix already narrows to a few hundred documents, not worth a second index per write.

2. **Punch-in race.** Both requests load the employee, compute the same `date` and call `insert_one` — there is no read before the write. The unique `(emp_code, date)` index lets exactly one insert commit; the other raises `DuplicateKeyError`, mapped to 409.

3. **Ties.** `$rank` gives competition ranking and `limit` filters on `rank <= limit` *after* ranking, so both tied employees are returned (ranks 1, 2, 2 for `limit=2`), ordered by `emp_code`; the next rank would be 4.

4. **Headcount.** The summary starts from `employees` (`joined_on <= month end`) and `$lookup`s each person's logs; an empty lookup still adds 1 to `headcount` and 0 to every sum.

5. **100x data.** Maintain pre-aggregated `(emp_code, month)` and `(department, date)` summaries (on write, or nightly with `$merge`) and serve analytics from them instead of scanning raw logs.
