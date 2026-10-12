import datetime as dt
import json
import logging
import os
import re
import threading
from calendar import monthrange
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.concurrency import run_in_threadpool

load_dotenv()  

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000          
MAX_MS = 4_102_444_800_000        

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]     
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60                      
OVERTIME_MIN = 30                           
HALF_DAY_LIMIT = 4.5                         
MAX_SHIFT = dt.timedelta(hours=24)
MAX_TREND_DAYS = 92


def to_ms(value: Optional[dt.datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // dt.timedelta(milliseconds=1)


def from_ms(ms: int) -> dt.datetime:
    return EPOCH + dt.timedelta(milliseconds=ms)


def utc_now() -> dt.datetime:
    now = dt.datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)  


def trunc_seconds(value: dt.datetime) -> dt.datetime:
    return value.replace(microsecond=0)


def hm_to_minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm_to_minutes(shift_end) <= hm_to_minutes(shift_start)


def attendance_date(punch_in: dt.datetime, shift_start: str, shift_end: str) -> dt.date:
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_into_day < hm_to_minutes(shift_end) * 60:
            day -= dt.timedelta(days=1)
    return day


def _ist_at(day: dt.date, hm: str) -> dt.datetime:
    h, m = hm.split(":")
    return dt.datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


def shift_end_at(day: dt.date, shift_start: str, shift_end: str) -> dt.datetime:
    end = _ist_at(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += dt.timedelta(days=1)
    return end


def compute_late_minutes(punch_in: dt.datetime, day: dt.date, shift_start: str) -> int:
    delta = int((punch_in - _ist_at(day, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime(punch_out: dt.datetime, day: dt.date, shift_start: str, shift_end: str) -> int:
    delta = int((punch_out - shift_end_at(day, shift_start, shift_end)).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= OVERTIME_MIN else 0


def compute_work_hours(punch_in: dt.datetime, punch_out: dt.datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < HALF_DAY_LIMIT


def half_up(value: Any, places: int) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return Decimal(repr(value) if isinstance(value, float) else str(value)).quantize(
        quantum, rounding=ROUND_HALF_UP
    )


def clean_float_sum(value: Any) -> Decimal:
    return Decimal(repr(round(float(value), 6)))


def weekdays_between(start: dt.date, end: dt.date) -> int:
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + dt.timedelta(days=i)).weekday() < 5:
            count += 1
    return count


def month_bounds(month: str) -> tuple:
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, 1), dt.date(year, mon, monthrange(year, mon)[1])


def _weekday_expr(path: str) -> dict:
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": path}}}, [2, 3, 4, 5, 6]]}


def _root(field: str) -> str:
    return f"${field}"


def _item(field: str) -> str:
    return f"$$l.{field}"


def presence_value(p, weekdays_only: bool) -> dict:
    cond: dict = {"$in": [p("status"), PRESENCE]}
    if weekdays_only:
        cond = {"$and": [cond, _weekday_expr(p("date"))]}
    half = {"$eq": [{"$ifNull": [p("half_day"), False]}, True]}
    return {"$cond": [cond, {"$cond": [half, 0.5, 1]}, 0]}


def _late_flag(p) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [p("late_minutes"), 0]}, 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": presence_value(_root, True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag(_root)},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str] = None) -> list:
    match: dict = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department

    def size_where(cond):
        return {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": cond}}}

    has_hours = {"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$isNumber": "$$l.work_hours"}]}
    hours_logs = {"$filter": {"input": "$logs", "as": "l", "cond": has_hours}}

    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                                  "late_minutes": 1, "work_hours": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l",
                                                   "in": presence_value(_item, True)}}},
                "wh_sum": {"$sum": {"$map": {"input": hours_logs, "as": "l", "in": "$$l.work_hours"}}},
                "wh_cnt": {"$size": hours_logs},
                "late_count": size_where({"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}),
                "late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": size_where({"$eq": ["$$l.status", "LEAVE"]}),
                "on_duty_count": size_where({"$eq": ["$$l.status", "ON_DUTY"]}),
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "wh_sum": {"$sum": "$wh_sum"},
                "wh_cnt": {"$sum": "$wh_cnt"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str] = None) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},   
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, d_from: dt.date, d_to: dt.date) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    n_days = (d_to - d_from).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$facet": {
                "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
                "days": [
                    {"$lookup": {
                        "from": "attendance_logs",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "pipeline": [
                            {"$match": {"date": {"$gte": f, "$lte": t}}},
                            {"$project": {"_id": 0, "date": 1, "status": 1,
                                          "half_day": 1, "late_minutes": 1}},
                        ],
                        "as": "logs",
                    }},
                    {"$unwind": "$logs"},
                    {"$group": {
                        "_id": "$logs.date",
                        "present": {"$sum": presence_value(lambda x: f"$logs.{x}", False)},
                        "late": {"$sum": _late_flag(lambda x: f"$logs.{x}")},
                    }},
                ],
            }
        },
        {"$project": {"joins": 1, "days": 1, "idx": {"$range": [0, n_days]}}},
        {"$unwind": "$idx"},
        {"$addFields": {"date": {"$dateToString": {
            "format": "%Y-%m-%d",
            "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": f}},
                                  "unit": "day", "amount": "$idx"}},
        }}}},
        {"$addFields": {
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "headcount": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$joins", "as": "jf", "cond": {"$lte": ["$$jf._id", "$date"]}}},
                "as": "jm", "in": "$$jm.n"}}},
            "stat": {"$arrayElemAt": [
                {"$filter": {"input": "$days", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]}}}, 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "present_count": {"$ifNull": ["$stat.present", 0]},
            "late_count": {"$ifNull": ["$stat.late", 0]},
        }},
        {"$addFields": {
            "rate_units": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$floor": {"$divide": [
                    {"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                    {"$multiply": [2, "$headcount"]}]}},
                None]},
        }},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"avg_units": {"$avg": "$rate_units", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "rate_units": 1, "avg_units": 1}},
    ]


def units_to_rate(units: Optional[float]) -> Optional[float]:
    """Integer 1e-4 units -> rate with 4 decimals."""
    if units is None:
        return None
    return float(Decimal(int(units)).scaleb(-4))


def avg_units_to_rate(avg_units: Optional[float]) -> Optional[float]:
    """Mean of rate units (a double) -> 4 decimals, half-up."""
    if avg_units is None:
        return None
    whole = Decimal(repr(float(avg_units))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(whole.scaleb(-4))


DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MONTH_RE = re.compile(MONTH_PATTERN)

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]


def invalid(loc: str, msg: str, where: str = "query") -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": (where, loc), "msg": msg, "input": None}])


def parse_date(value: str, loc: str, where: str = "query") -> dt.date:
    if not DATE_RE.fullmatch(value):          
        raise invalid(loc, "must be a date in YYYY-MM-DD format", where)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise invalid(loc, "not a valid calendar date", where)


def parse_month(value: str, loc: str = "month") -> tuple:
    if not MONTH_RE.fullmatch(value):
        raise invalid(loc, "must be YYYY-MM")
    try:
        start, end = month_bounds(value)
    except ValueError:
        raise invalid(loc, "not a valid month")
    return start, end


def parse_trend_range(date_from: str, date_to: str) -> tuple:
    d_from = parse_date(date_from, "from")
    d_to = parse_date(date_to, "to")
    if d_to < d_from:
        raise invalid("to", "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise invalid("to", f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        if not DATE_RE.fullmatch(self.joined_on):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            dt.date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null; omit it to leave it unchanged")
        return self


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def serialize_history(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):       
            changes[key] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[key] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"),
            "reason": entry.get("reason"), "changes": changes}


def serialize_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


INDEXES = {
    "employees": [
        ([("emp_code", ASCENDING)], {"unique": True}),                       
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),          
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {}),         
        ([("joined_on", ASCENDING)], {}),                                    
    ],
    "attendance_logs": [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),  
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),               
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),  
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),           
    ],
}

_indexes_ready = False
_index_lock = threading.Lock()


def ensure_indexes() -> bool:
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return True
        ok = True
        for collection, specs in INDEXES.items():
            for keys, options in specs:
                try:
                    db[collection].create_index(keys, **options)
                except PyMongoError as exc:
                    ok = False
                    log.warning("could not create index %s on %s: %s", keys, collection, exc)
        _indexes_ready = ok
        return ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await run_in_threadpool(ensure_indexes)
    except Exception as exc:  
        log.warning("index creation at startup failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_unavailable(_, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:           
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        employees_col.insert_one(doc)          
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = employees_col.count_documents(query)
    cursor = (employees_col.find(query).sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


def _require_employee(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = _require_employee(body.emp_code)
    moment = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    moment = trunc_seconds(moment)
    day = attendance_date(moment, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": moment,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(moment, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs_col.insert_one(doc)               
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = _require_employee(body.emp_code)
    moment = trunc_seconds(from_ms(body.punched_at) if body.punched_at is not None else utc_now())

    rec = logs_col.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": moment}},
                            sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punched_in = trunc_seconds(rec["punch_in"])
    if moment <= punched_in:
        raise invalid("punched_at", "must be after the punch-in", "body")
    if moment - punched_in > MAX_SHIFT:
        raise invalid("punched_at", "more than 24 hours after the punch-in", "body")

    day = dt.date.fromisoformat(rec["date"])
    hours = compute_work_hours(punched_in, moment)
    updated = logs_col.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},     
        {"$set": {
            "punch_out": moment,
            "work_hours": hours,
            "overtime_minutes": compute_overtime(moment, day, emp["shift_start"], emp["shift_end"]),
            "half_day": compute_half_day(hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    d_from = parse_date(date_from, "date_from") if date_from else None
    d_to = parse_date(date_to, "date_to") if date_to else None
    if d_from and d_to and d_from > d_to:
        raise invalid("date_from", "date_from must not be after date_to")
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if d_from or d_to:
        query["date"] = {}
        if d_from:
            query["date"]["$gte"] = d_from.isoformat()
        if d_to:
            query["date"]["$lte"] = d_to.isoformat()
    if status:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = logs_col.count_documents(query)
    cursor = (logs_col.find(query, {"_id": 0}).sort(list(ATTENDANCE_SORT.items()))
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_record(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple:
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]
    day = dt.date.fromisoformat(rec["date"])

    new_status = body.status or rec["status"]
    old_in = trunc_seconds(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = trunc_seconds(rec["punch_out"]) if rec.get("punch_out") else None

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid("punch_in", "ABSENT/LEAVE clear the punch times; do not send them", "body")
        new_in = new_out = None
    else:
        new_in = trunc_seconds(from_ms(body.punch_in)) if body.punch_in is not None else old_in
        new_out = trunc_seconds(from_ms(body.punch_out)) if body.punch_out is not None else old_out
        if new_in is None:
            raise invalid("punch_in", "a presence status requires a punch_in", "body")
        if attendance_date(new_in, shift_start, shift_end) != day:
            raise invalid("punch_in", "punch_in must stay on the record's attendance date", "body")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            raise invalid("punch_out", "must be after punch_in and within 24 hours", "body")

    if new_in is not None:
        late = compute_late_minutes(new_in, day, shift_start)
    else:
        late = 0
    if new_in is not None and new_out is not None:
        hours = compute_work_hours(new_in, new_out)
        overtime = compute_overtime(new_out, day, shift_start, shift_end)
        half = compute_half_day(hours)
    else:
        hours, overtime, half = None, 0, False

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    old = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day"))}

    if all(new[k] == old[k] for k in ("status", "punch_in", "punch_out")):
        raise invalid("body", "the request changes nothing", "body")

    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date", "path")
    emp = _require_employee(emp_code)
    rec = logs_col.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    fields, changes = plan_regularization(rec, emp, body)
    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    updated = logs_col.find_one_and_update(
        guard, {"$set": fields, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return serialize_record(updated)




@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    start, end = parse_month(month)
    emp = _require_employee(emp_code)
    joined = dt.date.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(start, joined), end)        

    rows = list(logs_col.aggregate(monthly_pipeline(emp_code, start.isoformat(), end.isoformat())))
    agg = rows[0] if rows else {}
    present = agg.get("present_days", 0)
    pct = None
    if working_days > 0:
        pct = float(half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(half_up(present, 2)),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = department_summary_pipeline(start.isoformat(), end.isoformat(), department)
    items = []
    for row in employees_col.aggregate(pipeline):
        avg = None
        if row["wh_cnt"] > 0:
            avg = float(half_up(clean_float_sum(row["wh_sum"]) / Decimal(row["wh_cnt"]), 2))
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": float(half_up(row["present_days"], 2)),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = parse_month(month)
    pipeline = leaderboard_pipeline(start.isoformat(), end.isoformat(), limit, department)
    return {"month": month, "items": list(logs_col.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    d_from, d_to = parse_trend_range(date_from, date_to)
    if employees_col.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")

    items = []
    for row in employees_col.aggregate(trend_pipeline(department, d_from, d_to)):
        working = row["is_working_day"]
        items.append({
            "date": row["date"],
            "is_working_day": working,
            "headcount": int(row["headcount"]),
            "present_count": float(half_up(row["present_count"], 2)),
            "late_count": int(row["late_count"]),
            "attendance_rate": units_to_rate(row.get("rate_units")),
            "moving_avg_7d": avg_units_to_rate(row.get("avg_units")),
        })
    return {"department": department, "items": items}


def _json_safe(value: Any) -> Any:
    return json.loads(json_util.dumps(value, json_options=json_util.RELAXED_JSON_OPTIONS))


def _need(value, name: str):
    if value is None or value == "":
        raise invalid(name, "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        spec = {"find": collection,
                "filter": attendance_filter(emp_code, date_from, date_to, status),
                "sort": ATTENDANCE_SORT,
                "skip": (page - 1) * page_size,
                "limit": page_size,
                "projection": {"_id": 0}}
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            start, end = parse_month(_need(month, "month"))
            pipeline = monthly_pipeline(_need(emp_code, "emp_code"), start.isoformat(), end.isoformat())
        elif endpoint == "department_summary":
            collection = "employees"
            start, end = parse_month(_need(month, "month"))
            pipeline = department_summary_pipeline(start.isoformat(), end.isoformat(), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            start, end = parse_month(_need(month, "month"))
            pipeline = leaderboard_pipeline(start.isoformat(), end.isoformat(), limit, department)
        else:  
            collection = "employees"
            d_from, d_to = parse_trend_range(_need(from_, "from"), _need(to, "to"))
            pipeline = trend_pipeline(_need(department, "department"), d_from, d_to)
        spec = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": spec, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _json_safe(result)}
