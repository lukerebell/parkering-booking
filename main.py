import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator, model_validator

DATABASE = Path(__file__).parent / "bookings.db"
ALL_HOURS = [f"{h:02d}" for h in range(24)]
MAX_BOOKING_HOURS = 96
LETTER_RE = re.compile(r"[a-zA-ZæøåÆØÅ]", re.UNICODE)
CAR_REG_RE = re.compile(r"^[A-ZÆØÅ]{2}\d{5}$", re.UNICODE)
ADMIN_COOKIE = "admin_session"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def letter_count(value: str) -> int:
    return len(LETTER_RE.findall(value))


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


app = FastAPI(title="Parkering Booking")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    for err in exc.errors():
        if err.get("type") == "value_error":
            msg = err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[13:]
            return JSONResponse(status_code=422, content={"detail": msg})
    return JSONResponse(status_code=422, content={"detail": "Fyll ut alle felter"})


class BookingCreate(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    car_reg: str = Field(min_length=1)
    booking_date_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    booking_date_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    hour_from: str = Field(min_length=1)
    hour_to: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if letter_count(value) < 2:
            raise ValueError("Navn må ha minst to bokstaver.")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        digits = phone_digits(value)
        if len(digits) != 8:
            raise ValueError("Telefon må ha åtte siffer.")
        return digits

    @field_validator("car_reg")
    @classmethod
    def validate_car_reg(cls, value: str) -> str:
        value = value.strip().upper()
        if not CAR_REG_RE.fullmatch(value):
            raise ValueError("Reg.nr må ha to bokstaver og fem siffer.")
        return value

    @field_validator("hour_from", "hour_to")
    @classmethod
    def validate_hour(cls, value: str) -> str:
        hour = parse_hour(value)
        if hour not in range(24):
            raise ValueError("Ugyldig klokkeslett.")
        return f"{hour:02d}"

    @model_validator(mode="after")
    def validate_dates(self):
        end = self.booking_date_to or self.booking_date_from
        if date.fromisoformat(end) < date.fromisoformat(self.booking_date_from):
            raise ValueError("Til-dato kan ikke være før fra-dato.")
        self.booking_date_to = end
        return self


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def parse_hour(value: str) -> int:
    value = value.strip()
    if ":" in value:
        return int(value.split(":")[0])
    return int(value)


def normalize_hour(hour: str) -> str:
    return f"{parse_hour(hour):02d}"


def format_date_norwegian(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return d.strftime("%d.%m.%Y")


def admin_session_token() -> str:
    secret = os.environ.get("ADMIN_SECRET", ADMIN_PASSWORD or "change-me").encode()
    return hmac.new(secret, b"parkering-admin", hashlib.sha256).hexdigest()


def is_admin_authenticated(request: Request) -> bool:
    if not ADMIN_PASSWORD:
        return False
    token = request.cookies.get(ADMIN_COOKIE)
    expected = admin_session_token()
    return token is not None and secrets.compare_digest(token, expected)


def slot_after(date_str: str, hour_str: str) -> tuple[str, str]:
    hour = int(hour_str)
    current = date.fromisoformat(date_str)
    if hour < 23:
        return date_str, f"{hour + 1:02d}"
    return (current + timedelta(days=1)).isoformat(), "00"


def slots_are_consecutive(
    prev: tuple[str, str], nxt: tuple[str, str]
) -> bool:
    return slot_after(prev[0], prev[1]) == nxt


def finalize_booking_group(group: dict) -> dict:
    end_date, end_hour = slot_after(group["last_slot"][0], group["last_slot"][1])
    return {
        "name": group["name"],
        "phone": group["phone"],
        "car_reg": group["car_reg"],
        "ids": ",".join(str(booking_id) for booking_id in group["ids"]),
        "slot_count": group["slot_count"],
        "created_at": group["created_at"],
        "period_label": format_period_label(
            group["first_slot"][0],
            group["first_slot"][1],
            end_date,
            end_hour,
        ),
    }


def list_booking_groups(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, phone, car_reg, booking_date, booking_hour, created_at
        FROM bookings
        ORDER BY created_at DESC, name, booking_date, booking_hour
        """
    ).fetchall()

    groups: list[dict] = []
    current: dict | None = None

    for row in rows:
        identity = (row["name"], row["phone"], row["car_reg"])
        slot = (row["booking_date"], normalize_hour(row["booking_hour"]))

        if (
            current
            and current["identity"] == identity
            and slots_are_consecutive(current["last_slot"], slot)
        ):
            current["ids"].append(row["id"])
            current["last_slot"] = slot
            current["slot_count"] += 1
            continue

        if current:
            groups.append(finalize_booking_group(current))

        current = {
            "identity": identity,
            "name": row["name"],
            "phone": row["phone"],
            "car_reg": row["car_reg"],
            "ids": [row["id"]],
            "first_slot": slot,
            "last_slot": slot,
            "slot_count": 1,
            "created_at": row["created_at"],
        }

    if current:
        groups.append(finalize_booking_group(current))

    return groups


def format_period_label(date_from: str, hour_from: str, date_to: str, hour_to: str) -> str:
    return (
        f"{format_date_norwegian(date_from)} kl. {normalize_hour(hour_from)}:00"
        f" – {format_date_norwegian(date_to)} kl. {normalize_hour(hour_to)}:00"
    )


def slots_in_continuous_range(
    date_from: str,
    hour_from: str,
    date_to: str,
    hour_to: str,
) -> list[tuple[str, str]]:
    """
    Sammenhengende periode [start, slutt).
    Start-time er inkludert. Slutt-time er eksklusiv (plassen er ledig fra slutt-klokkeslett).
    """
    h_start = parse_hour(hour_from)
    h_end = parse_hour(hour_to)
    d_start = date.fromisoformat(date_from)
    d_end = date.fromisoformat(date_to)

    start_key = (d_start, h_start)
    end_key = (d_end, h_end)

    if start_key == end_key:
        return [(date_from, f"{h_start:02d}")]

    if end_key < start_key:
        raise ValueError("Sluttidspunkt må være etter start.")

    slots: list[tuple[str, str]] = []
    current = d_start

    while current <= d_end:
        iso = current.isoformat()
        if current == d_start and current == d_end:
            for hour in range(h_start, h_end):
                slots.append((iso, f"{hour:02d}"))
        elif current == d_start:
            for hour in range(h_start, 24):
                slots.append((iso, f"{hour:02d}"))
        elif current == d_end:
            for hour in range(0, h_end):
                slots.append((iso, f"{hour:02d}"))
        else:
            for hour in range(24):
                slots.append((iso, f"{hour:02d}"))
        current += timedelta(days=1)

    if not slots:
        raise ValueError("Tidsrommet er tomt. Velg et lengre tidsrom.")

    return slots


def assert_max_booking_hours(slots: list[tuple[str, str]]) -> None:
    if len(slots) > MAX_BOOKING_HOURS:
        raise ValueError("Plassen kan bookes maks fire dager (96 timer) om gangen.")


def find_conflicts(conn, slots: list[tuple[str, str]]) -> list[dict[str, str]]:
    conflicts = []
    for booking_date, hour in slots:
        row = conn.execute(
            "SELECT 1 FROM bookings WHERE booking_date = ? AND booking_hour = ?",
            (booking_date, hour),
        ).fetchone()
        if row:
            conflicts.append({"date": booking_date, "hour": hour})
    return conflicts


def booked_slots_in_dates(conn, date_from: str, date_to: str) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT booking_date, booking_hour FROM bookings
        WHERE booking_date >= ? AND booking_date <= ?
        ORDER BY booking_date, booking_hour
        """,
        (date_from, date_to),
    ).fetchall()
    return [
        {"date": row["booking_date"], "hour": normalize_hour(row["booking_hour"])}
        for row in rows
    ]


def conflicts_to_message(conflicts: list[dict[str, str]]) -> str:
    if not conflicts:
        return "Plassen er allerede booket. Velg et annet tidsrom."
    by_date: dict[str, list[str]] = {}
    for c in conflicts:
        by_date.setdefault(c["date"], []).append(c["hour"])
    parts = []
    for d in sorted(by_date):
        hours = ", ".join(f"{h}:00" for h in sorted(by_date[d]))
        parts.append(f"{format_date_norwegian(d)} ({hours})")
    return f"Plassen er allerede booket: {'; '.join(parts)}. Velg et annet tidsrom."


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                car_reg TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                booking_hour TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (booking_date, booking_hour)
            )
            """
        )
        rows = conn.execute("SELECT id, booking_hour FROM bookings").fetchall()
        for row in rows:
            normalized = normalize_hour(row["booking_hour"])
            if normalized != row["booking_hour"]:
                conn.execute(
                    "UPDATE bookings SET booking_hour = ? WHERE id = ?",
                    (normalized, row["id"]),
                )
        conn.commit()


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


def admin_template_context(request: Request, **extra):
    return {
        "request": request,
        "admin_enabled": bool(ADMIN_PASSWORD),
        "logged_in": is_admin_authenticated(request),
        **extra,
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, message: str | None = None):
    if not ADMIN_PASSWORD:
        return templates.TemplateResponse(
            "admin.html",
            admin_template_context(request),
        )

    if not is_admin_authenticated(request):
        return templates.TemplateResponse(
            "admin.html",
            admin_template_context(request, error=None),
        )

    with get_db() as conn:
        groups = list_booking_groups(conn)

    slot_count = sum(group["slot_count"] for group in groups)
    return templates.TemplateResponse(
        "admin.html",
        admin_template_context(
            request,
            groups=groups,
            slot_count=slot_count,
            message=message,
        ),
    )


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD er ikke satt.")

    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        return templates.TemplateResponse(
            "admin.html",
            admin_template_context(request, error="Feil passord."),
            status_code=401,
        )

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        admin_session_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response


@app.post("/admin/delete")
async def admin_delete(request: Request, ids: str = Form(...)):
    if not is_admin_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)

    booking_ids: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if part.isdigit():
            booking_ids.append(int(part))

    if not booking_ids:
        return RedirectResponse(url="/admin?message=Ingen+rader+valgt", status_code=303)

    placeholders = ",".join("?" for _ in booking_ids)
    with get_db() as conn:
        conn.execute(
            f"DELETE FROM bookings WHERE id IN ({placeholders})",
            booking_ids,
        )
        conn.commit()

    return RedirectResponse(url="/admin?message=Booking+slettet", status_code=303)


@app.get("/available-times")
async def available_times(
    date_from: str,
    date_to: str | None = None,
    hour_from: str = "00",
    hour_to: str = "01",
):
    end = date_to or date_from
    try:
        slots = slots_in_continuous_range(date_from, hour_from, end, hour_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with get_db() as conn:
        conflicts = find_conflicts(conn, slots)
        booked_slots = booked_slots_in_dates(conn, date_from, end)

    slot_count = len(slots)
    return {
        "date_from": date_from,
        "date_to": end,
        "hour_from": normalize_hour(hour_from),
        "hour_to": normalize_hour(hour_to),
        "available": len(conflicts) == 0 and slot_count <= MAX_BOOKING_HOURS,
        "exceeds_max_hours": slot_count > MAX_BOOKING_HOURS,
        "slot_count": slot_count,
        "conflicts": conflicts,
        "booked_slots": booked_slots,
        "period_label": format_period_label(date_from, hour_from, end, hour_to),
    }


@app.post("/bookings")
async def create_booking(booking: BookingCreate):
    date_to = booking.booking_date_to or booking.booking_date_from
    hour_from = normalize_hour(booking.hour_from)
    hour_to = normalize_hour(booking.hour_to)

    try:
        slots = slots_in_continuous_range(
            booking.booking_date_from,
            hour_from,
            date_to,
            hour_to,
        )
        assert_max_booking_hours(slots)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = booking.name.strip()
    phone = booking.phone.strip()
    car_reg = booking.car_reg.strip().upper()

    with get_db() as conn:
        conflicts = find_conflicts(conn, slots)
        if conflicts:
            raise HTTPException(status_code=409, detail=conflicts_to_message(conflicts))

        try:
            for booking_date, hour in slots:
                conn.execute(
                    """
                    INSERT INTO bookings (name, phone, car_reg, booking_date, booking_hour)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, phone, car_reg, booking_date, hour),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Plassen er allerede booket i dette tidsrommet. Velg et annet tidsrom.",
            )

    return {
        "message": "Plassen er booket!",
        "booking": {
            "name": name,
            "phone": phone,
            "car_reg": car_reg,
            "booking_date_from": booking.booking_date_from,
            "booking_date_to": date_to,
            "hour_from": hour_from,
            "hour_to": hour_to,
            "period_label": format_period_label(
                booking.booking_date_from, hour_from, date_to, hour_to
            ),
            "slot_count": len(slots),
        },
    }
