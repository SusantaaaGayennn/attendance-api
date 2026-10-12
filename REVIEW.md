# REVIEW.md

These are the problem areas that I found in starter `app/main.py`.

# Where, What is wrong, How you would notice it, How I fixed it.

(1)
- `compute_late_minutes`
- The grace check was on the whole minutes, so 10:00 to 10:59 would be counted as on time. The rule is that after 10:00, you're considered late.
- Shift is 09:30, punch 09:40:01: 0: returns 0, should be 10.
- I compare in seconds (rude if more than 600) and then I take the whole minutes.

(2)
- `compute_late_minutes`
- The shift start was constructed in whatever the time zone that the datetime was, (server or UTC) not in IST. The Microseconds weren't truncated either.
- A punch on a UTC server, a 09:30 IST punch seems hours late.
- The shift start is drawn in IST on the attendance date, and the punch time is rounded down to seconds first.

(3)
- `compute_late_minutes`
- The overnight shift was disregarded. It was always the punch-in's personal day.
- 22:00-06:00 (punch at 01:00) needs to be 180 minutes behind same day's 22:00.
- Attendance_date() determines the correct day initially, and the time of late arrival is calculated from that particular day's shift start.

(4)
- `compute_work_hours`
- `round()` on a float rounds halves to even and has float error. The rule wants half-up.
- 8.125 hours (29,250 seconds) 8.12 should be 8.13.
- It need to be `Decimal` and round with `ROUND_HALF_UP` on whole seconds.

(5)
- `compute_overtime`
- There was no 30-minute minimum.
- Shift ends 18:30, punch-out 18:35: it comes back 5, it should be 0.
- It returns 0 when the whole minutes are less than 30.

(6)
- `compute_overtime`
- The shift end was put on the attendance date, so overnight shifts ended on the wrong day. Additionally, this forced a timezone on one side and compared that with a naive datetime, thus causing an error.
- An overnight record's a 500 or a bad number.
- `shift_end_at()` does time zone conversion for day shifts but adds a day for overnights.

(7)
- `EmployeeIn`
- No validation at all (code format, email, `HH:MM`, equal shift times, a real `joined_on`).
- `shift_start: "abc"` is stored, then the first punch-in for that individual hits 500.
- Pydantic patterns and length, plus validator for the shift times and date.

(8)
- `PunchInIn`
- `status`- any text can be stored and `punched_at`- a string, a float, seconds or very large numbers can be stored.
- `{"status": "LEAVE"}` is returned with 201. Any seconds value of 1783312500 is valid.
- `Literal` for the status, and a strict integer that is within a sensible time-jection range.

(9)
- `/health`
- It always said ok and the database was never modified.
- Stop mongo, and it still gives you a 200.
- It pings MongoDB and returns 503 if that fails.

(10)
- `create_employee`
- It "test and then insert" was used and not one-of-a-kind index, so both requests could pass.
- Many parallel instances of one code produce many 201s.
- Unique index on `emp_code`, and a duplicate key error becomes 409.

(11)
- `create_employee`
- `created_at` was naive local time and came back as a datetime, not epoch milliseconds.
- Compare the response with the contract.
- It is stored in UTC and returned as an integer in milliseconds.

(12)
- `list_employees`
- `skip = page * page_size` skipped the whole first page.
- `page=1` returns employees 21 to 40.
- `skip = (page - 1) * page_size`.

(13) 
- `list_employees`
- `total` counted every employee, not the filtered ones. No sorting. `page` and `page_size` had no limits.
- `department=Sales` reports the total of all employees.
- Count with the filter, sort by `emp_code`, and limit `page` and `page_size`.

(14)
- `punch_in`
- An unknown employee made `emp["shift_start"]` fail, giving a 500 instead of a 404.
- Punch in with `EMP9999`.
- I return 404.

(15)
- `punch_in`
- `fromtimestamp` used the server's local time, so the date was wrong (not the IST date, no overnight rule, no truncation). Also `if body.punched_at` treats 0 as missing.
- A server in UTC gets the wrong date for a punch at 01:30 IST.
- A UTC time cut to seconds, the IST date from `attendance_date()`, and an `is not None` check.

(16)
- `punch_in`
- Check-then-insert race.
- Ten identical punch-ins at once give several 201s.
- Unique `(emp_code, date)` index, and a duplicate key error becomes 409.

(17)
- `punch_in`
- The response included an `id` (from `_id`) and raw datetimes.
- Look at the response body.
- One serializer that hides `_id` and returns times in milliseconds.

(18)
- `list_attendance`
- It loaded every record into memory, sorted in Python and used `len()` for the total.
- It gets slow with 100,000 records.
- The database does the filter, sort, skip and limit, with a count, backed by indexes.

(19) 
- `list_attendance` | 
- It sorted by date only, with no tie-break on `emp_code`, and had no validation for dates, status or paging.
- `date_from` after `date_to` returns 200.
- Validation helpers, and a sort on date descending then `emp_code` ascending.

(20)
- `list_attendance`
- It returned `_id` as `id` and raw datetimes, and old records without `half_day` or `history` came out without those keys.
- Look at an older record such as EMP0002 in the sample data.
- The shared serializer adds the defaults.

(21)
- module level
- `MongoClient` was not time-zone aware, so datetimes came back naive. No indexes were created, and `IST` was defined but never used.
- Any date conversion or comparison.
- `tz_aware=True`, and indexes are created at startup.

# Things that were fine

- `load_dotenv()` does not overwrite variables that are already set in the environment, so real environment variables already win.
- The endpoints are plain `def` functions, so FastAPI runs them in a thread pool and the blocking PyMongo client does not block the event loop.
- `doc.pop("_id")` after `insert_one` is harmless. The real problem was the extra `id` field (#17).
- The default `MONGO_URI` and `MONGO_DB` are only fallbacks, and environment values take priority.
