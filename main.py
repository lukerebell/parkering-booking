import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

DATABASE = Path(__file__).parent / "bookings.db"
ALL_HOURS = [f"{h:02d}" for h in range(24)]

app = FastAPI(title="Parkering Booking")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class BookingCreate(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    car_reg: str = Field(min_length=1)
    booking_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    booking_hours: list[str] = Field(min_length=1)

    @field_validator("booking_hours")
    @classmethod
    def validate_hours(cls, hours: list[str]) -> list[str]:
        unique = sorted(set(hours))
        for hour in unique:
            if not len(hour) == 2 or hour not in ALL_HOURS:
                raise ValueError(f"Ugyldig time: {hour}")
        return unique


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


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
        conn.commit()


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/available-times")
async def available_times(date: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT booking_hour FROM bookings WHERE booking_date = ?",
            (date,),
        ).fetchall()
    booked = {row["booking_hour"] for row in rows}
    available = [h for h in ALL_HOURS if h not in booked]
    return {"date": date, "available_times": available}


@app.post("/bookings")
async def create_booking(booking: BookingCreate):
    hours = booking.booking_hours
    name = booking.name.strip()
    phone = booking.phone.strip()
    car_reg = booking.car_reg.strip().upper()

    with get_db() as conn:
        placeholders = ",".join("?" * len(hours))
        taken = conn.execute(
            f"""
            SELECT booking_hour FROM bookings
            WHERE booking_date = ? AND booking_hour IN ({placeholders})
            """,
            (booking.booking_date, *hours),
        ).fetchall()
        if taken:
            taken_hours = ", ".join(row["booking_hour"] for row in taken)
            raise HTTPException(
                status_code=409,
                detail=f"Disse timene er allerede booket: {taken_hours}. Velg andre timer.",
            )
        try:
            for hour in hours:
                conn.execute(
                    """
                    INSERT INTO bookings (name, phone, car_reg, booking_date, booking_hour)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, phone, car_reg, booking.booking_date, hour),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Minst én av timene er allerede booket. Velg andre timer.",
            )

    return {
        "message": "Plassen er booket!",
        "booking": {
            "name": name,
            "phone": phone,
            "car_reg": car_reg,
            "booking_date": booking.booking_date,
            "booking_hours": hours,
        },
    }
