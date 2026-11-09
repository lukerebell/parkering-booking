import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator, model_validator


def database_path() -> Path:
    configured = os.environ.get("DATABASE_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).parent / "bookings.db"


DATABASE = database_path()
ALL_HOURS = [f"{h:02d}" for h in range(24)]
LETTER_RE = re.compile(r"[a-zA-ZæøåÆØÅ]", re.UNICODE)
CAR_REG_RE = re.compile(r"^[A-ZÆØÅ]{2}\d{5}$", re.UNICODE)
ADMIN_COOKIE = "admin_session"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ASSET_VERSION = os.environ.get("APP_ASSET_VERSION", "12")
TZ_OSLO = ZoneInfo("Europe/Oslo")

PRICE_PER_DAY = 50
PRICE_PER_WEEK = 300
PRICE_PER_MONTH = 1000
DAYS_PER_WEEK = 7
DAYS_PER_MONTH = 30
VIPPS_NUMBER = "908 12 444"


def letter_count(value: str) -> int:
    return len(LETTER_RE.findall(value))


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


app = FastAPI(title="Parkering Booking")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path

    if path in {"/", "/admin"} or path.startswith("/admin/") or path.startswith("/forleng/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif path.startswith("/static"):
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"

    return response


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
        start_key = interval_key(self.booking_date_from, self.hour_from)
        end_key = interval_key(end, self.hour_to)
        if end_key <= start_key:
            raise ValueError("Sluttidspunkt må være etter start.")
        self.booking_date_to = end
        return self


class ExtendRequest(BaseModel):
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_hour: str = Field(min_length=1)

    @field_validator("end_hour")
    @classmethod
    def validate_hour(cls, value: str) -> str:
        hour = parse_hour(value)
        if hour not in range(24):
            raise ValueError("Ugyldig klokkeslett.")
        return f"{hour:02d}"


def billable_days_from_hours(hours: int) -> int:
    """Påbegynt døgn rundes opp til et helt døgn."""
    if hours <= 0:
        raise ValueError("Tidsrommet er tomt.")
    return (hours + 23) // 24


def calculate_price_for_days(days: int) -> int:
    """
    Finn rimeligste kombinasjon av døgn-, uke- og månedspris.
    Uke = 7 døgn, måned = 30 døgn. Pakker kan dekke mer enn nødvendig
    dersom det gir lavere totalpris.
    """
    if days <= 0:
        raise ValueError("Antall døgn må være positivt.")

    best = days * PRICE_PER_DAY
    max_months = days // DAYS_PER_MONTH + 1
    for months in range(0, max_months + 1):
        remaining_after_months = max(0, days - months * DAYS_PER_MONTH)
        max_weeks = remaining_after_months // DAYS_PER_WEEK + 1
        for weeks in range(0, max_weeks + 1):
            remaining_days = max(0, remaining_after_months - weeks * DAYS_PER_WEEK)
            cost = (
                months * PRICE_PER_MONTH
                + weeks * PRICE_PER_WEEK
                + remaining_days * PRICE_PER_DAY
            )
            if cost < best:
                best = cost
    return best


def calculate_booking_price(slot_count: int) -> int:
    return calculate_price_for_days(billable_days_from_hours(slot_count))


def extension_extra_payment(new_total_price: int, already_charged: int) -> int:
    return max(0, new_total_price - already_charged)


class MigrationError(RuntimeError):
    """Migrering avbrutt; kallende kode skal rulle tilbake uten å slette bookings."""


def reservation_is_legacy(reservation) -> bool:
    try:
        return bool(reservation["is_legacy"])
    except (KeyError, IndexError):
        return False


def reservation_original_end(reservation) -> tuple[str, str]:
    try:
        orig_date = reservation["original_end_date"]
        orig_hour = reservation["original_end_hour"]
    except (KeyError, IndexError):
        orig_date = None
        orig_hour = None
    if orig_date and orig_hour is not None and str(orig_hour) != "":
        return orig_date, normalize_hour(str(orig_hour))
    return reservation["end_date"], normalize_hour(reservation["end_hour"])


def extension_extra_for_reservation(reservation, new_total_price: int) -> int:
    """
    Nye bookinger: ny total − allerede belastet.
    Migrerte (historisk gratis): ny total − pris for opprinnelig periode − allerede
    belastede tillegg. Gir ikke tilbakevirkende betaling for den gamle perioden.
    """
    already_charged = int(reservation["charged_total_kr"] or 0)
    if not reservation_is_legacy(reservation):
        return extension_extra_payment(new_total_price, already_charged)

    start_date = reservation["start_date"]
    start_hour = normalize_hour(reservation["start_hour"])
    orig_end_date, orig_end_hour = reservation_original_end(reservation)
    baseline = calculate_booking_price(
        hours_between(start_date, start_hour, orig_end_date, orig_end_hour)
    )
    return max(0, new_total_price - baseline - already_charged)


def now_oslo() -> datetime:
    return datetime.now(TZ_OSLO)


def combine_oslo(date_str: str, hour_str: str) -> datetime:
    return datetime.combine(
        date.fromisoformat(date_str),
        time(parse_hour(hour_str), 0),
        tzinfo=TZ_OSLO,
    )


def is_reservation_active(end_date: str, end_hour: str, at: datetime | None = None) -> bool:
    """Aktiv så lenge gjeldende sluttidspunkt ikke er passert."""
    current = at or now_oslo()
    return current < combine_oslo(end_date, end_hour)


def generate_manage_token() -> str:
    return secrets.token_urlsafe(32)


def manage_path(token: str) -> str:
    return f"/forleng/{token}"


def absolute_public_url(request: Request, path: str) -> str:
    """Bygg offentlig absolutt URL (HTTPS på Render bak proxy)."""
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return f"{configured}{path}"

    base = str(request.base_url).rstrip("/")
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://") :]
    return f"{base}{path}"


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def interval_key(date_str: str, hour_str: str) -> tuple[date, int]:
    return (date.fromisoformat(date_str), parse_hour(hour_str))


def intervals_overlap(
    start_a: tuple[date, int],
    end_a: tuple[date, int],
    start_b: tuple[date, int],
    end_b: tuple[date, int],
) -> bool:
    """Half-open intervals [start, end). Touching endpoints do not overlap."""
    return start_a < end_b and end_a > start_b


def hours_between(
    start_date: str,
    start_hour: str,
    end_date: str,
    end_hour: str,
) -> int:
    start = combine_oslo(start_date, start_hour)
    end = combine_oslo(end_date, end_hour)
    if end <= start:
        raise ValueError("Sluttidspunkt må være etter start.")
    hours = int((end - start).total_seconds() // 3600)
    if hours <= 0:
        raise ValueError("Tidsrommet er tomt. Velg et lengre tidsrom.")
    return hours


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


def format_period_label(date_from: str, hour_from: str, date_to: str, hour_to: str) -> str:
    return (
        f"{format_date_norwegian(date_from)} kl. {normalize_hour(hour_from)}:00"
        f" – {format_date_norwegian(date_to)} kl. {normalize_hour(hour_to)}:00"
    )


def find_overlapping_reservations(
    conn,
    start_date: str,
    start_hour: str,
    end_date: str,
    end_hour: str,
    exclude_reservation_id: int | None = None,
) -> list[sqlite3.Row]:
    start_key = interval_key(start_date, start_hour)
    end_key = interval_key(end_date, end_hour)
    rows = conn.execute(
        """
        SELECT id, name, phone, car_reg, start_date, start_hour, end_date, end_hour
        FROM reservations
        ORDER BY start_date, start_hour, id
        """
    ).fetchall()
    overlaps = []
    for row in rows:
        if exclude_reservation_id is not None and row["id"] == exclude_reservation_id:
            continue
        other_start = interval_key(row["start_date"], row["start_hour"])
        other_end = interval_key(row["end_date"], row["end_hour"])
        if intervals_overlap(start_key, end_key, other_start, other_end):
            overlaps.append(row)
    return overlaps


def occupied_periods_in_range(conn, date_from: str, date_to: str) -> list[dict]:
    """Reservasjoner som berører valgt datoperiod (for UI-varsel)."""
    range_start = interval_key(date_from, "00")
    # inkluder hele til-datoen: [date_to 00:00, date_to+1 00:00)
    day_after = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
    range_end = interval_key(day_after, "00")

    rows = conn.execute(
        """
        SELECT start_date, start_hour, end_date, end_hour
        FROM reservations
        ORDER BY start_date, start_hour, id
        """
    ).fetchall()
    periods = []
    for row in rows:
        start_key = interval_key(row["start_date"], row["start_hour"])
        end_key = interval_key(row["end_date"], row["end_hour"])
        if intervals_overlap(range_start, range_end, start_key, end_key):
            periods.append(
                {
                    "start_date": row["start_date"],
                    "start_hour": normalize_hour(row["start_hour"]),
                    "end_date": row["end_date"],
                    "end_hour": normalize_hour(row["end_hour"]),
                    "period_label": format_period_label(
                        row["start_date"],
                        row["start_hour"],
                        row["end_date"],
                        row["end_hour"],
                    ),
                }
            )
    return periods


def conflicts_to_message(overlaps: list) -> str:
    if not overlaps:
        return "Plassen er allerede booket. Velg et annet tidsrom."
    parts = [
        format_period_label(
            row["start_date"], row["start_hour"], row["end_date"], row["end_hour"]
        )
        for row in overlaps
    ]
    return f"Plassen er allerede booket: {'; '.join(parts)}. Velg et annet tidsrom."


def ensure_column(conn, table: str, column: str, definition: str) -> None:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def reservation_public_dict(row, request: Request | None = None) -> dict:
    active = is_reservation_active(row["end_date"], row["end_hour"])
    path = manage_path(row["manage_token"])
    manage_url = absolute_public_url(request, path) if request else path
    hours = hours_between(
        row["start_date"], row["start_hour"], row["end_date"], row["end_hour"]
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "car_reg": row["car_reg"],
        "start_date": row["start_date"],
        "start_hour": normalize_hour(row["start_hour"]),
        "end_date": row["end_date"],
        "end_hour": normalize_hour(row["end_hour"]),
        "period_label": format_period_label(
            row["start_date"], row["start_hour"], row["end_date"], row["end_hour"]
        ),
        "slot_count": hours,
        "charged_total_kr": row["charged_total_kr"],
        "paid_total_kr": row["paid_total_kr"],
        "active": active,
        "manage_path": path,
        "manage_url": manage_url,
        "vipps_number": VIPPS_NUMBER,
    }


def get_reservation_by_token(conn, token: str):
    return conn.execute(
        "SELECT * FROM reservations WHERE manage_token = ?",
        (token,),
    ).fetchone()


def list_booking_groups(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            id,
            name,
            phone,
            car_reg,
            start_date,
            start_hour,
            end_date,
            end_hour,
            charged_total_kr,
            paid_total_kr,
            created_at
        FROM reservations
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()

    groups = []
    for row in rows:
        hours = hours_between(
            row["start_date"], row["start_hour"], row["end_date"], row["end_hour"]
        )
        groups.append(
            {
                "name": row["name"],
                "phone": row["phone"],
                "car_reg": row["car_reg"],
                "reservation_id": row["id"],
                "slot_count": hours,
                "price_kr": row["charged_total_kr"],
                "paid_total_kr": row["paid_total_kr"],
                "created_at": row["created_at"],
                "period_label": format_period_label(
                    row["start_date"],
                    row["start_hour"],
                    row["end_date"],
                    row["end_hour"],
                ),
            }
        )
    return groups


def ensure_reservation_schema(conn) -> None:
    ensure_column(conn, "reservations", "is_legacy", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "reservations", "original_end_date", "TEXT")
    ensure_column(conn, "reservations", "original_end_hour", "TEXT")
    conn.execute(
        """
        UPDATE reservations
        SET original_end_date = end_date
        WHERE original_end_date IS NULL OR TRIM(original_end_date) = ''
        """
    )
    conn.execute(
        """
        UPDATE reservations
        SET original_end_hour = end_hour
        WHERE original_end_hour IS NULL OR TRIM(original_end_hour) = ''
        """
    )


def validate_migrated_reservations(conn) -> None:
    """Kontroller migrert resultat før bookings slettes. Feiler → MigrationError."""
    booking_count = conn.execute("SELECT COUNT(*) AS n FROM bookings").fetchone()["n"]
    orphan_count = conn.execute(
        "SELECT COUNT(*) AS n FROM bookings WHERE reservation_id IS NULL"
    ).fetchone()["n"]
    if orphan_count:
        raise MigrationError(
            f"Migrering ufullstendig: {orphan_count} timerader mangler reservation_id."
        )

    reservations = conn.execute("SELECT * FROM reservations").fetchall()
    if not reservations and booking_count:
        raise MigrationError("Ingen reservasjoner ble opprettet, men bookings har rader.")

    tokens: set[str] = set()
    intervals: list[tuple[tuple[date, int], tuple[date, int], int]] = []
    covered_hours = 0

    for row in reservations:
        rid = row["id"]
        token = row["manage_token"]
        if not token:
            raise MigrationError(f"Reservasjon {rid} mangler manage_token.")
        if token in tokens:
            raise MigrationError(f"Duplikat manage_token for reservasjon {rid}.")
        tokens.add(token)

        start_date = row["start_date"]
        start_hour = normalize_hour(row["start_hour"])
        end_date = row["end_date"]
        end_hour = normalize_hour(row["end_hour"])
        try:
            hours = hours_between(start_date, start_hour, end_date, end_hour)
        except ValueError as exc:
            raise MigrationError(
                f"Ugyldig intervall for reservasjon {rid}: {exc}"
            ) from exc

        linked = conn.execute(
            """
            SELECT id, name, phone, car_reg, booking_date, booking_hour
            FROM bookings
            WHERE reservation_id = ?
            ORDER BY booking_date, booking_hour, id
            """,
            (rid,),
        ).fetchall()
        if len(linked) != hours:
            raise MigrationError(
                f"Reservasjon {rid}: forventet {hours} timerader, fant {len(linked)}."
            )
        if not linked:
            raise MigrationError(f"Reservasjon {rid} har ingen tilknyttede timerader.")

        first = linked[0]
        last = linked[-1]
        if (
            first["booking_date"] != start_date
            or normalize_hour(first["booking_hour"]) != start_hour
        ):
            raise MigrationError(f"Reservasjon {rid}: starttid stemmer ikke.")
        expected_end = slot_after(
            last["booking_date"], normalize_hour(last["booking_hour"])
        )
        if expected_end != (end_date, end_hour):
            raise MigrationError(f"Reservasjon {rid}: sluttid stemmer ikke.")

        for prev, nxt in zip(linked, linked[1:]):
            prev_slot = (prev["booking_date"], normalize_hour(prev["booking_hour"]))
            next_slot = (nxt["booking_date"], normalize_hour(nxt["booking_hour"]))
            if not slots_are_consecutive(prev_slot, next_slot):
                raise MigrationError(
                    f"Reservasjon {rid}: timeradene er ikke sammenhengende."
                )
            if (
                prev["name"] != row["name"]
                or prev["phone"] != row["phone"]
                or prev["car_reg"] != row["car_reg"]
            ):
                raise MigrationError(
                    f"Reservasjon {rid}: kundeinformasjon stemmer ikke med timerader."
                )

        if (
            first["name"] != row["name"]
            or first["phone"] != row["phone"]
            or first["car_reg"] != row["car_reg"]
        ):
            raise MigrationError(
                f"Reservasjon {rid}: kundeinformasjon er ikke bevart."
            )

        is_legacy = reservation_is_legacy(row)
        charged = int(row["charged_total_kr"] or 0)
        paid = int(row["paid_total_kr"] or 0)
        if is_legacy:
            if charged != 0 or paid != 0:
                raise MigrationError(
                    f"Legacy-reservasjon {rid} skal ha charged=0 og paid=0."
                )
            charge_sum = conn.execute(
                """
                SELECT COALESCE(SUM(amount_kr), 0) AS s
                FROM price_charges WHERE reservation_id = ?
                """,
                (rid,),
            ).fetchone()["s"]
            if int(charge_sum) != 0:
                raise MigrationError(
                    f"Legacy-reservasjon {rid} har fiktive price_charges."
                )
            orig_date, orig_hour = reservation_original_end(row)
            if (orig_date, orig_hour) != (end_date, end_hour):
                # Ved migrering skal original_end == end før noen forlengelse
                raise MigrationError(
                    f"Legacy-reservasjon {rid}: original_end må matche migrert slutt."
                )
        else:
            if paid != 0 and charged == 0:
                raise MigrationError(
                    f"Reservasjon {rid}: betalt uten belastning er ugyldig ved migrering."
                )

        start_key = interval_key(start_date, start_hour)
        end_key = interval_key(end_date, end_hour)
        for other_start, other_end, other_id in intervals:
            if intervals_overlap(start_key, end_key, other_start, other_end):
                raise MigrationError(
                    f"Overlappende reservasjoner: {rid} og {other_id}."
                )
        intervals.append((start_key, end_key, rid))
        covered_hours += hours

    if covered_hours != booking_count:
        raise MigrationError(
            f"Timeantall stemmer ikke: {covered_hours} i reservasjoner vs "
            f"{booking_count} i bookings."
        )


def migrate_hourly_bookings_to_reservations(conn) -> None:
    """
    Trygg migrering fra gammel time-basert bookings-tabell.
    Grupperer på name+phone+car_reg+created_at. Ved feil: MigrationError
    før DROP (kallende kode ruller tilbake).
    """
    if not table_exists(conn, "bookings"):
        return

    ensure_column(conn, "bookings", "reservation_id", "INTEGER")
    ensure_column(conn, "bookings", "price_kr", "INTEGER")
    ensure_reservation_schema(conn)

    orphan_count = conn.execute(
        "SELECT COUNT(*) AS n FROM bookings WHERE reservation_id IS NULL"
    ).fetchone()["n"]

    if orphan_count:
        rows = conn.execute(
            """
            SELECT id, name, phone, car_reg, booking_date, booking_hour, created_at, price_kr
            FROM bookings
            WHERE reservation_id IS NULL
            ORDER BY phone, car_reg, name, created_at, booking_date, booking_hour, id
            """
        ).fetchall()

        groups: list[dict] = []
        current: dict | None = None

        for row in rows:
            identity = (
                row["name"],
                row["phone"],
                row["car_reg"],
                row["created_at"],
            )
            slot = (row["booking_date"], normalize_hour(row["booking_hour"]))

            if (
                current
                and current["identity"] == identity
                and slots_are_consecutive(current["last_slot"], slot)
            ):
                current["ids"].append(row["id"])
                current["last_slot"] = slot
                current["slot_count"] += 1
                if current["price_kr"] is None and row["price_kr"] is not None:
                    current["price_kr"] = row["price_kr"]
                continue

            if current:
                groups.append(current)

            current = {
                "identity": identity,
                "name": row["name"],
                "phone": row["phone"],
                "car_reg": row["car_reg"],
                "ids": [row["id"]],
                "first_slot": slot,
                "last_slot": slot,
                "slot_count": 1,
                "price_kr": row["price_kr"],
                "created_at": row["created_at"],
            }

        if current:
            groups.append(current)

        for group in groups:
            end_date, end_hour = slot_after(group["last_slot"][0], group["last_slot"][1])
            start_date, start_hour = group["first_slot"]
            has_stored_price = group["price_kr"] is not None
            is_legacy = 0 if has_stored_price else 1
            if has_stored_price:
                charged = int(group["price_kr"])
            else:
                charged = 0

            token = generate_manage_token()
            cur = conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr, created_at,
                    is_legacy, original_end_date, original_end_hour
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    token,
                    group["name"],
                    group["phone"],
                    group["car_reg"],
                    start_date,
                    start_hour,
                    end_date,
                    end_hour,
                    charged,
                    group["created_at"],
                    is_legacy,
                    end_date,
                    end_hour,
                ),
            )
            reservation_id = cur.lastrowid
            placeholders = ",".join("?" for _ in group["ids"])
            conn.execute(
                f"""
                UPDATE bookings
                SET reservation_id = ?
                WHERE id IN ({placeholders})
                """,
                [reservation_id, *group["ids"]],
            )

            # Kun opprett charge når det faktisk fantes lagret pris
            if has_stored_price:
                conn.execute(
                    """
                    INSERT INTO price_charges (
                        reservation_id, amount_kr, total_price_kr, charged_total_after_kr,
                        kind, end_date, end_hour, created_at
                    ) VALUES (?, ?, ?, ?, 'initial', ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        charged,
                        charged,
                        charged,
                        end_date,
                        end_hour,
                        group["created_at"],
                    ),
                )

    validate_migrated_reservations(conn)

    remaining_orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM bookings WHERE reservation_id IS NULL"
    ).fetchone()["n"]
    if remaining_orphans:
        raise MigrationError(
            f"Kan ikke fjerne bookings: {remaining_orphans} rader mangler reservation_id."
        )

    conn.execute("DROP TABLE bookings")


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manage_token TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                car_reg TEXT NOT NULL,
                start_date TEXT NOT NULL,
                start_hour TEXT NOT NULL,
                end_date TEXT NOT NULL,
                end_hour TEXT NOT NULL,
                charged_total_kr INTEGER NOT NULL DEFAULT 0,
                paid_total_kr INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_legacy INTEGER NOT NULL DEFAULT 0,
                original_end_date TEXT,
                original_end_hour TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_charges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER NOT NULL,
                amount_kr INTEGER NOT NULL,
                total_price_kr INTEGER NOT NULL,
                charged_total_after_kr INTEGER NOT NULL,
                kind TEXT NOT NULL,
                end_date TEXT NOT NULL,
                end_hour TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (reservation_id, end_date, end_hour),
                FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
            )
            """
        )

        ensure_reservation_schema(conn)

        # Behold gammel tabell midlertidig kun for migrering dersom den finnes
        if not table_exists(conn, "bookings"):
            conn.commit()
            return

        ensure_column(conn, "bookings", "price_kr", "INTEGER")
        ensure_column(conn, "bookings", "reservation_id", "INTEGER")
        rows = conn.execute("SELECT id, booking_hour FROM bookings").fetchall()
        for row in rows:
            normalized = normalize_hour(row["booking_hour"])
            if normalized != row["booking_hour"]:
                conn.execute(
                    "UPDATE bookings SET booking_hour = ? WHERE id = ?",
                    (normalized, row["id"]),
                )

        try:
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()
        except MigrationError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise


def build_extension_quote(reservation, new_end_date: str, new_end_hour: str) -> dict:
    new_end_date = date.fromisoformat(new_end_date).isoformat()
    new_end_hour = normalize_hour(new_end_hour)
    start_date = reservation["start_date"]
    start_hour = normalize_hour(reservation["start_hour"])
    old_end_date = reservation["end_date"]
    old_end_hour = normalize_hour(reservation["end_hour"])

    old_key = interval_key(old_end_date, old_end_hour)
    new_key = interval_key(new_end_date, new_end_hour)
    start_key = interval_key(start_date, start_hour)

    if new_key <= start_key:
        raise ValueError("Sluttidspunkt må være etter start.")
    if new_key < old_key:
        raise ValueError("Forlengelse kan ikke forkorte parkeringen.")

    hours = hours_between(start_date, start_hour, new_end_date, new_end_hour)
    new_total = calculate_booking_price(hours)
    already_charged = int(reservation["charged_total_kr"] or 0)
    extra = extension_extra_for_reservation(reservation, new_total)

    return {
        "original_period_label": format_period_label(
            start_date, start_hour, old_end_date, old_end_hour
        ),
        "new_end_date": new_end_date,
        "new_end_hour": new_end_hour,
        "new_period_label": format_period_label(
            start_date, start_hour, new_end_date, new_end_hour
        ),
        "slot_count": hours,
        "billable_days": billable_days_from_hours(hours),
        "new_total_price_kr": new_total,
        "already_charged_kr": already_charged,
        "paid_total_kr": int(reservation["paid_total_kr"] or 0),
        "extra_to_pay_kr": extra,
        "idempotent": new_key == old_key,
        "is_legacy": reservation_is_legacy(reservation),
    }


def apply_extension(conn, reservation, new_end_date: str, new_end_hour: str) -> dict:
    """Forleng i én transaksjon. Idempotent ved samme sluttidspunkt."""
    quote = build_extension_quote(reservation, new_end_date, new_end_hour)
    reservation_id = reservation["id"]

    if quote["idempotent"]:
        return {
            **quote,
            "extended": False,
            "message": "Parkeringen har allerede dette sluttidspunktet.",
        }

    if not is_reservation_active(reservation["end_date"], reservation["end_hour"]):
        raise ValueError(
            "Parkeringen er utløpt. Opprett en ny booking for å parkere igjen."
        )

    overlaps = find_overlapping_reservations(
        conn,
        reservation["start_date"],
        reservation["start_hour"],
        quote["new_end_date"],
        quote["new_end_hour"],
        exclude_reservation_id=reservation_id,
    )
    if overlaps:
        raise LookupError(conflicts_to_message(overlaps))

    already = quote["already_charged_kr"]
    extra = quote["extra_to_pay_kr"]
    new_charged = already + extra

    try:
        conn.execute(
            """
            UPDATE reservations
            SET end_date = ?, end_hour = ?, charged_total_kr = ?
            WHERE id = ?
            """,
            (
                quote["new_end_date"],
                quote["new_end_hour"],
                new_charged,
                reservation_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO price_charges (
                reservation_id, amount_kr, total_price_kr, charged_total_after_kr,
                kind, end_date, end_hour
            ) VALUES (?, ?, ?, ?, 'extension', ?, ?)
            """,
            (
                reservation_id,
                extra,
                quote["new_total_price_kr"],
                new_charged,
                quote["new_end_date"],
                quote["new_end_hour"],
            ),
        )
    except sqlite3.IntegrityError as exc:
        existing = conn.execute(
            """
            SELECT 1 FROM price_charges
            WHERE reservation_id = ? AND end_date = ? AND end_hour = ?
            """,
            (reservation_id, quote["new_end_date"], quote["new_end_hour"]),
        ).fetchone()
        if existing:
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            quote = build_extension_quote(
                refreshed, quote["new_end_date"], quote["new_end_hour"]
            )
            return {
                **quote,
                "charged_total_after_kr": int(refreshed["charged_total_kr"]),
                "extended": False,
                "message": "Forlengelsen var allerede registrert.",
            }
        raise LookupError(
            "Plassen er allerede booket i dette tidsrommet. Velg et annet tidsrom."
        ) from exc

    return {
        **quote,
        "charged_total_after_kr": new_charged,
        "extended": True,
        "message": "Parkeringen er forlenget!",
    }


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "asset_version": ASSET_VERSION},
    )


@app.get("/forleng/{token}", response_class=HTMLResponse)
async def extend_page(request: Request, token: str):
    with get_db() as conn:
        reservation = get_reservation_by_token(conn, token)
        if not reservation:
            return templates.TemplateResponse(
                "extend.html",
                {
                    "request": request,
                    "asset_version": ASSET_VERSION,
                    "error": "Fant ikke bookingen. Sjekk at du bruker lenken fra bestillingsbekreftelsen.",
                    "reservation": None,
                },
                status_code=404,
            )
        data = reservation_public_dict(reservation, request)

    return templates.TemplateResponse(
        "extend.html",
        {
            "request": request,
            "asset_version": ASSET_VERSION,
            "error": None,
            "reservation": data,
            "token": token,
        },
    )


@app.get("/api/reservations/{token}")
async def api_get_reservation(request: Request, token: str):
    with get_db() as conn:
        reservation = get_reservation_by_token(conn, token)
        if not reservation:
            raise HTTPException(status_code=404, detail="Fant ikke bookingen.")
        return reservation_public_dict(reservation, request)


@app.post("/api/reservations/{token}/preview-extend")
async def api_preview_extend(token: str, body: ExtendRequest):
    with get_db() as conn:
        reservation = get_reservation_by_token(conn, token)
        if not reservation:
            raise HTTPException(status_code=404, detail="Fant ikke bookingen.")
        if not is_reservation_active(reservation["end_date"], reservation["end_hour"]):
            raise HTTPException(
                status_code=400,
                detail="Parkeringen er utløpt. Opprett en ny booking for å parkere igjen.",
            )
        try:
            quote = build_extension_quote(reservation, body.end_date, body.end_hour)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        overlaps = []
        if not quote["idempotent"]:
            overlaps = find_overlapping_reservations(
                conn,
                reservation["start_date"],
                reservation["start_hour"],
                quote["new_end_date"],
                quote["new_end_hour"],
                exclude_reservation_id=reservation["id"],
            )
        available = len(overlaps) == 0
        return {
            "available": available,
            "conflicts": [
                {
                    "period_label": format_period_label(
                        row["start_date"],
                        row["start_hour"],
                        row["end_date"],
                        row["end_hour"],
                    )
                }
                for row in overlaps
            ],
            "conflict_message": conflicts_to_message(overlaps) if overlaps else None,
            "original_period_label": quote["original_period_label"],
            "new_end_date": quote["new_end_date"],
            "new_end_hour": quote["new_end_hour"],
            "new_period_label": quote["new_period_label"],
            "slot_count": quote["slot_count"],
            "billable_days": quote["billable_days"],
            "new_total_price_kr": quote["new_total_price_kr"],
            "already_charged_kr": quote["already_charged_kr"],
            "paid_total_kr": quote["paid_total_kr"],
            "extra_to_pay_kr": quote["extra_to_pay_kr"],
            "vipps_number": VIPPS_NUMBER,
            "idempotent": quote["idempotent"],
        }


@app.post("/api/reservations/{token}/extend")
async def api_extend_reservation(request: Request, token: str, body: ExtendRequest):
    with get_db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            reservation = get_reservation_by_token(conn, token)
            if not reservation:
                conn.rollback()
                raise HTTPException(status_code=404, detail="Fant ikke bookingen.")

            result = apply_extension(conn, reservation, body.end_date, body.end_hour)
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LookupError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise

        refreshed = get_reservation_by_token(conn, token)
        public = reservation_public_dict(refreshed, request)

    return {
        "message": result["message"],
        "extended": result["extended"],
        "reservation": public,
        "quote": {
            "original_period_label": result["original_period_label"],
            "new_period_label": result["new_period_label"],
            "new_total_price_kr": result["new_total_price_kr"],
            "already_charged_kr": result["already_charged_kr"],
            "charged_total_after_kr": result.get(
                "charged_total_after_kr", public["charged_total_kr"]
            ),
            "paid_total_kr": public["paid_total_kr"],
            "extra_to_pay_kr": result["extra_to_pay_kr"] if result["extended"] else 0,
            "vipps_number": VIPPS_NUMBER,
        },
    }


def admin_template_context(request: Request, **extra):
    return {
        "request": request,
        "asset_version": ASSET_VERSION,
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
async def admin_delete(request: Request, reservation_id: str = Form("")):
    if not is_admin_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)

    if not reservation_id.strip().isdigit():
        return RedirectResponse(url="/admin?message=Ingen+rader+valgt", status_code=303)

    rid = int(reservation_id.strip())
    with get_db() as conn:
        conn.execute("DELETE FROM price_charges WHERE reservation_id = ?", (rid,))
        conn.execute("DELETE FROM reservations WHERE id = ?", (rid,))
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
        hours = hours_between(date_from, hour_from, end, hour_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with get_db() as conn:
        overlaps = find_overlapping_reservations(
            conn, date_from, hour_from, end, hour_to
        )
        occupied = occupied_periods_in_range(conn, date_from, end)

    billable_days = billable_days_from_hours(hours)
    price_kr = calculate_booking_price(hours)
    return {
        "date_from": date_from,
        "date_to": end,
        "hour_from": normalize_hour(hour_from),
        "hour_to": normalize_hour(hour_to),
        "available": len(overlaps) == 0,
        "slot_count": hours,
        "billable_days": billable_days,
        "price_kr": price_kr,
        "vipps_number": VIPPS_NUMBER,
        "conflicts": [
            {
                "period_label": format_period_label(
                    row["start_date"],
                    row["start_hour"],
                    row["end_date"],
                    row["end_hour"],
                )
            }
            for row in overlaps
        ],
        "occupied_periods": occupied,
        "period_label": format_period_label(date_from, hour_from, end, hour_to),
    }


@app.post("/bookings")
async def create_booking(request: Request, booking: BookingCreate):
    date_to = booking.booking_date_to or booking.booking_date_from
    hour_from = normalize_hour(booking.hour_from)
    hour_to = normalize_hour(booking.hour_to)

    try:
        hours = hours_between(
            booking.booking_date_from,
            hour_from,
            date_to,
            hour_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = booking.name.strip()
    phone = booking.phone.strip()
    car_reg = booking.car_reg.strip().upper()
    price_kr = calculate_booking_price(hours)
    billable_days = billable_days_from_hours(hours)
    token = generate_manage_token()

    with get_db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            overlaps = find_overlapping_reservations(
                conn,
                booking.booking_date_from,
                hour_from,
                date_to,
                hour_to,
            )
            if overlaps:
                conn.rollback()
                raise HTTPException(
                    status_code=409, detail=conflicts_to_message(overlaps)
                )

            cur = conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr,
                    is_legacy, original_end_date, original_end_hour
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    token,
                    name,
                    phone,
                    car_reg,
                    booking.booking_date_from,
                    hour_from,
                    date_to,
                    hour_to,
                    price_kr,
                    date_to,
                    hour_to,
                ),
            )
            reservation_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO price_charges (
                    reservation_id, amount_kr, total_price_kr, charged_total_after_kr,
                    kind, end_date, end_hour
                ) VALUES (?, ?, ?, ?, 'initial', ?, ?)
                """,
                (reservation_id, price_kr, price_kr, price_kr, date_to, hour_to),
            )
            conn.commit()
        except HTTPException:
            raise
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Plassen er allerede booket i dette tidsrommet. Velg et annet tidsrom.",
            )
        except Exception:
            conn.rollback()
            raise

    manage = manage_path(token)
    manage_url = absolute_public_url(request, manage)

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
            "slot_count": hours,
            "billable_days": billable_days,
            "price_kr": price_kr,
            "charged_total_kr": price_kr,
            "paid_total_kr": 0,
            "vipps_number": VIPPS_NUMBER,
            "manage_token": token,
            "manage_path": manage,
            "manage_url": manage_url,
        },
    }
