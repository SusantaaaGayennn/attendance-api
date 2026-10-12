# DECISIONS.md

1. Indexes: 
Unique employees.empcode (identity, 409 on duplicates); (department, empcode) and (department, joinedon) for list and head count. On logs: unique (empcode, date) (one record per day, monthly scans, punch-in guaranteed); (date desc, empcode) for list sort and month scans; (status, date desc, empcode) for status filters; (empcode, punchin desc) for punch-out. I didn't create an index on late_minutes: the date index will be narrowed to one month.

2. Punch-in race: 
Both requests send the same IST date and perform insertone. An index of (empcode, date) as unique will only allow a single record; any others inserted will trigger a 409 Duplicate Key Error. No "check first" race here.

3. Ties:
The $rank operator ranks each row. The limit filters after ranking so the last group of tied employees is returned, which may mean more rows than the limit.

4. Headcount:
The initial part of the department summary begins with employees (joined on or before month end) and joins their logs with $lookup. An empty array in the logs for someone without logs will still make it into the department $group where it counts.

5. 100x data:
Retain pre-aggregated monthly totals per employee, factored on punch and correction. Analytics operate on small summary documents rather than re-scanning logs (and distribute logs by shard to emp_code).