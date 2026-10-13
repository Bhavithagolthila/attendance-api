# DECISIONS.md

1. **Indexes.** Logs: unique (emp_code, date) enforces one record per day and serves per-employee queries; (date desc, emp_code) serves the default list and month-wide ranges; (status, date desc, emp_code) serves the status filter; (emp_code, punch_in desc) finds the record to punch out. Employees: unique emp_code, (department, emp_code), and joined_on for headcount. Rejected: an index on late_minutes, because the date range already narrows the data and each index slows writes.

2. **Punch-in race.** Both requests work out the same date and call insert_one. The unique index lets one insert succeed; the other gets DuplicateKeyError, which I turn into 409. I never check first.

3. **Ties.** The pipeline ranks with $rank (1, 2, 2, 4) and only then keeps rank <= limit, so everyone tied at the cutoff is returned, even if that is more than limit. Rows are ordered by minutes, then emp_code.

4. **Headcount.** The summary starts from employees (joined_on <= month end) and $lookups each person's logs. No logs means an empty array, counted as zero, yet the person is still counted in headcount.

5. **100x data.** I would stop aggregating raw logs for trends and summaries, and keep a small daily rollup per department, updated on every punch.
