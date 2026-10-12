# Employee Attendance & Analytics API

The project is a FastAPI + MongoDB for punching in and out, fixing records with an audit trail, and some analytics endpoints using aggregation pipeline. The code is in app/main.py.

# How to operate

Install dependencies list
Export MONGO_URI="mongodb://localhost:27017" or use "connection string".
export MONGODB="attendancedb"
uvicorn app.main:app --port 8000

In Windows PowerShell, set it with $env:MONGO_URI="connection string" rather than export.
The indexes are created when the app starts, and GET /health will return 200 if MongoDB does. You’ll need MongoDB 6.0 for this.

# What is done

Most of what's in the contract is already up and running: employees (create, list), punch-in and punch-out, manual corrections (PATCH) with a history, the attendance list, the four analytics endpoints (monthly, department summary, late leaderboard, department trend), and the explain endpoint.
I went through the starter code and documented the problems in REVIEW.md.

Fully explained decisions I took when the contract was unclear.

- Dept trend also rejects a range of more than 92 days, inclusively both end dates.
- The mean of the (4-decimal) # attendance_rate values I return.
- When both corrections come for the same record at the same time, the one that is dropped, receives a 409 and will not be retried.
- Salaries, averages and hours worked are rounded half-up to the nearest value with exact rounding. I did not use MongoDB's $round` with the reported figures because it rounds halves to the nearest even number.

# How I tested it

I devised a small script (not committed to this repo) that imported known data into a test database, called the API, and checked the responses against hand-calculated answers. It tested the late and overtime rules, an overnight shift, number of ties on the leaderboard, the trend and the moving average, all the error codes, and many parallel requests. I also imported around 100K attendance records onto a free cluster on MongoDB Atlas, verified the explain output shows index scans and no collection scans, and observed the requests complete in around half a second.

# How I used AI

I used an AI assistant (Claude) as a reference while building this: for reviewing the starter code, for inspiration on aggregation pipelines, and to troubleshoot set-up issues. I walked through the code and would be glad to explain any part of it.

# What I am less sure about

- The department trend pipeline is the trickiest part. I tested the pipeline with some of my own test data, but I did not try to handle every odd case (for example a department where everybody joins at the mid-range).
- None have automated unit-test suite in the repo, only the manual script above.