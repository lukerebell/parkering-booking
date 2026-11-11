"""Tester for forlengelse, overlapp, migrering og forenklet datamodell."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import main
from main import (
    TZ_OSLO,
    MigrationError,
    apply_extension,
    build_extension_quote,
    calculate_booking_price,
    extension_extra_for_reservation,
    extension_extra_payment,
    find_overlapping_reservations,
    get_db,
    hours_between,
    init_db,
    intervals_overlap,
    interval_key,
    is_reservation_active,
    list_booking_groups,
    migrate_hourly_bookings_to_reservations,
    reservation_is_fully_paid,
    set_reservation_paid_status,
    table_exists,
    validate_migrated_reservations,
)


def make_reservation(
    *,
    start_date="2026-06-01",
    start_hour="08",
    end_date="2026-06-02",
    end_hour="02",
    charged_total_kr=50,
    paid_total_kr=0,
    is_legacy=0,
    original_end_date=None,
    original_end_hour=None,
):
    return {
        "id": 1,
        "manage_token": "test-token",
        "name": "Test Testesen",
        "phone": "90812444",
        "car_reg": "AB12345",
        "start_date": start_date,
        "start_hour": start_hour,
        "end_date": end_date,
        "end_hour": end_hour,
        "charged_total_kr": charged_total_kr,
        "paid_total_kr": paid_total_kr,
        "is_legacy": is_legacy,
        "original_end_date": original_end_date or end_date,
        "original_end_hour": original_end_hour or end_hour,
    }


class IntervalOverlapTests(unittest.TestCase):
    def test_overlapping_middle(self):
        self.assertTrue(
            intervals_overlap(
                interval_key("2026-06-01", "08"),
                interval_key("2026-06-01", "18"),
                interval_key("2026-06-01", "12"),
                interval_key("2026-06-01", "20"),
            )
        )

    def test_touching_endpoints_allowed(self):
        self.assertFalse(
            intervals_overlap(
                interval_key("2026-06-01", "08"),
                interval_key("2026-06-01", "12"),
                interval_key("2026-06-01", "12"),
                interval_key("2026-06-01", "18"),
            )
        )

    def test_identical_intervals_overlap(self):
        self.assertTrue(
            intervals_overlap(
                interval_key("2026-06-01", "08"),
                interval_key("2026-06-01", "18"),
                interval_key("2026-06-01", "08"),
                interval_key("2026-06-01", "18"),
            )
        )


class ExtensionPricingTests(unittest.TestCase):
    def test_examples_from_spec(self):
        self.assertEqual(
            extension_extra_payment(
                calculate_booking_price(20),
                calculate_booking_price(18),
            ),
            0,
        )
        self.assertEqual(
            extension_extra_payment(
                calculate_booking_price(26),
                calculate_booking_price(18),
            ),
            50,
        )
        self.assertEqual(
            extension_extra_payment(
                calculate_booking_price(12 * 24),
                calculate_booking_price(7 * 24),
            ),
            250,
        )
        self.assertEqual(
            extension_extra_payment(
                calculate_booking_price(30 * 24),
                calculate_booking_price(28 * 24),
            ),
            0,
        )

    def test_quote_18_to_20_hours_zero_extra(self):
        reservation = make_reservation(
            start_date="2026-06-01",
            start_hour="08",
            end_date="2026-06-02",
            end_hour="02",
            charged_total_kr=50,
        )
        quote = build_extension_quote(reservation, "2026-06-02", "04")
        self.assertEqual(quote["slot_count"], 20)
        self.assertEqual(quote["new_total_price_kr"], 50)
        self.assertEqual(quote["extra_to_pay_kr"], 0)

    def test_quote_18_to_26_hours_fifty_extra(self):
        reservation = make_reservation(
            start_date="2026-06-01",
            start_hour="08",
            end_date="2026-06-02",
            end_hour="02",
            charged_total_kr=50,
        )
        quote = build_extension_quote(reservation, "2026-06-02", "10")
        self.assertEqual(quote["slot_count"], 26)
        self.assertEqual(quote["new_total_price_kr"], 100)
        self.assertEqual(quote["extra_to_pay_kr"], 50)

    def test_quote_7_to_12_days(self):
        reservation = make_reservation(
            start_date="2026-06-01",
            start_hour="00",
            end_date="2026-06-08",
            end_hour="00",
            charged_total_kr=300,
        )
        quote = build_extension_quote(reservation, "2026-06-13", "00")
        self.assertEqual(quote["slot_count"], 12 * 24)
        self.assertEqual(quote["new_total_price_kr"], 550)
        self.assertEqual(quote["extra_to_pay_kr"], 250)

    def test_quote_28_to_30_days_zero_extra(self):
        reservation = make_reservation(
            start_date="2026-06-01",
            start_hour="00",
            end_date="2026-06-29",
            end_hour="00",
            charged_total_kr=1000,
        )
        quote = build_extension_quote(reservation, "2026-07-01", "00")
        self.assertEqual(quote["slot_count"], 30 * 24)
        self.assertEqual(quote["new_total_price_kr"], 1000)
        self.assertEqual(quote["extra_to_pay_kr"], 0)

    def test_cannot_shorten(self):
        reservation = make_reservation()
        with self.assertRaises(ValueError):
            build_extension_quote(reservation, "2026-06-01", "12")

    def test_legacy_18_to_20_zero_extra(self):
        reservation = make_reservation(
            start_date="2026-06-01",
            start_hour="08",
            end_date="2026-06-02",
            end_hour="02",
            charged_total_kr=0,
            is_legacy=1,
            original_end_date="2026-06-02",
            original_end_hour="02",
        )
        quote = build_extension_quote(reservation, "2026-06-02", "04")
        self.assertEqual(quote["extra_to_pay_kr"], 0)

    def test_legacy_18_to_26_fifty_extra(self):
        reservation = make_reservation(
            start_date="2026-06-01",
            start_hour="08",
            end_date="2026-06-02",
            end_hour="02",
            charged_total_kr=0,
            is_legacy=1,
            original_end_date="2026-06-02",
            original_end_hour="02",
        )
        quote = build_extension_quote(reservation, "2026-06-02", "10")
        self.assertEqual(quote["extra_to_pay_kr"], 50)

    def test_legacy_no_extension_no_payment_demand(self):
        reservation = make_reservation(
            charged_total_kr=0,
            is_legacy=1,
        )
        # Samme slutt → idempotent, 0 ekstra
        quote = build_extension_quote(reservation, reservation["end_date"], reservation["end_hour"])
        self.assertTrue(quote["idempotent"])
        self.assertEqual(quote["extra_to_pay_kr"], 0)
        self.assertEqual(
            extension_extra_for_reservation(reservation, quote["new_total_price_kr"]),
            0,
        )


class ActiveReservationTests(unittest.TestCase):
    def test_active_before_end(self):
        at = datetime(2026, 6, 2, 1, 0, tzinfo=TZ_OSLO)
        self.assertTrue(is_reservation_active("2026-06-02", "02", at=at))

    def test_inactive_at_end(self):
        at = datetime(2026, 6, 2, 2, 0, tzinfo=TZ_OSLO)
        self.assertFalse(is_reservation_active("2026-06-02", "02", at=at))

    def test_inactive_after_end(self):
        at = datetime(2026, 6, 2, 3, 0, tzinfo=TZ_OSLO)
        self.assertFalse(is_reservation_active("2026-06-02", "02", at=at))


class ExtensionDbTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"
        self._old_db = main.DATABASE
        main.DATABASE = self.db_path
        init_db()

    def tearDown(self):
        main.DATABASE = self._old_db
        self._tmpdir.cleanup()

    def _future_range_18h(self):
        start_day = (datetime.now(TZ_OSLO) + timedelta(days=1)).date()
        end_day = start_day + timedelta(days=1)
        return start_day.isoformat(), "08", end_day.isoformat(), "02"

    def _insert_reservation(
        self,
        start,
        start_h,
        end,
        end_h,
        price,
        token=None,
        *,
        is_legacy=0,
        charged=None,
    ):
        token = token or f"token-{start}-{end_h}-{price}-{is_legacy}"
        charged_total = price if charged is None else charged
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr,
                    is_legacy, original_end_date, original_end_hour
                ) VALUES (?, 'Ada', '12345678', 'KJ12345', ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    token,
                    start,
                    start_h,
                    end,
                    end_h,
                    charged_total,
                    is_legacy,
                    end,
                    end_h,
                ),
            )
            rid = cur.lastrowid
            if not is_legacy:
                conn.execute(
                    """
                    INSERT INTO price_charges (
                        reservation_id, amount_kr, total_price_kr, charged_total_after_kr,
                        kind, end_date, end_hour
                    ) VALUES (?, ?, ?, ?, 'initial', ?, ?)
                    """,
                    (rid, charged_total, charged_total, charged_total, end, end_h),
                )
            conn.commit()
            return conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (rid,)
            ).fetchone()

    def test_create_uses_only_reservations_and_charges(self):
        start, start_h, end, end_h = self._future_range_18h()
        self._insert_reservation(start, start_h, end, end_h, 50)
        with get_db() as conn:
            self.assertFalse(table_exists(conn, "bookings"))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM reservations").fetchone()["n"], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM price_charges").fetchone()["n"], 1
            )

    def test_touching_reservations_allowed(self):
        start, start_h, end, end_h = self._future_range_18h()
        self._insert_reservation(start, start_h, end, end_h, 50, token="a")
        with get_db() as conn:
            overlaps = find_overlapping_reservations(
                conn, end, end_h, end, "08"
            )
            self.assertEqual(overlaps, [])

    def test_overlapping_reservations_detected(self):
        start, start_h, end, end_h = self._future_range_18h()
        self._insert_reservation(start, start_h, end, end_h, 50, token="a")
        with get_db() as conn:
            overlaps = find_overlapping_reservations(
                conn, start, "10", end, "12"
            )
            self.assertEqual(len(overlaps), 1)

    def test_apply_extension_updates_end_and_charge(self):
        start, start_h, end, end_h = self._future_range_18h()
        reservation = self._insert_reservation(start, start_h, end, end_h, 50)
        with get_db() as conn:
            result = apply_extension(conn, reservation, end, "10")
            conn.commit()
            self.assertTrue(result["extended"])
            self.assertEqual(result["extra_to_pay_kr"], 50)
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(refreshed["end_hour"], "10")
            self.assertEqual(refreshed["charged_total_kr"], 100)
            charges = conn.execute(
                "SELECT amount_kr, kind FROM price_charges WHERE reservation_id = ? ORDER BY id",
                (reservation["id"],),
            ).fetchall()
            self.assertEqual(len(charges), 2)
            self.assertEqual(charges[1]["amount_kr"], 50)
            self.assertEqual(charges[1]["kind"], "extension")
            self.assertEqual(
                hours_between(
                    refreshed["start_date"],
                    refreshed["start_hour"],
                    refreshed["end_date"],
                    refreshed["end_hour"],
                ),
                26,
            )

    def test_idempotent_same_extension_twice(self):
        start, start_h, end, end_h = self._future_range_18h()
        reservation = self._insert_reservation(start, start_h, end, end_h, 50)
        with get_db() as conn:
            first = apply_extension(conn, reservation, end, "10")
            conn.commit()
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            second = apply_extension(conn, refreshed, end, "10")
            conn.commit()
            self.assertTrue(first["extended"])
            self.assertFalse(second["extended"])
            charges = conn.execute(
                "SELECT COUNT(*) AS n FROM price_charges WHERE reservation_id = ?",
                (reservation["id"],),
            ).fetchone()["n"]
            self.assertEqual(charges, 2)
            self.assertEqual(refreshed["charged_total_kr"], 100)

    def test_extension_blocked_by_other_customer(self):
        start, start_h, end, end_h = self._future_range_18h()
        reservation = self._insert_reservation(start, start_h, end, end_h, 50, token="mine")
        self._insert_reservation(end, end_h, end, "12", 50, token="other")
        with get_db() as conn:
            with self.assertRaises(LookupError):
                apply_extension(conn, reservation, end, "10")

    def test_expired_reservation_cannot_extend(self):
        past_start = (datetime.now(TZ_OSLO) - timedelta(days=2)).date().isoformat()
        past_end = (datetime.now(TZ_OSLO) - timedelta(days=1)).date().isoformat()
        reservation = self._insert_reservation(
            past_start, "08", past_end, "08", 50
        )
        future_end = (datetime.now(TZ_OSLO) + timedelta(days=1)).date().isoformat()
        with get_db() as conn:
            with self.assertRaises(ValueError):
                apply_extension(conn, reservation, future_end, "08")

    def test_extension_without_extra_payment(self):
        start, start_h, end, end_h = self._future_range_18h()
        reservation = self._insert_reservation(start, start_h, end, end_h, 50)
        with get_db() as conn:
            result = apply_extension(conn, reservation, end, "04")
            conn.commit()
            self.assertTrue(result["extended"])
            self.assertEqual(result["extra_to_pay_kr"], 0)

    def test_legacy_extension_preserves_original_end_and_charges_delta(self):
        start, start_h, end, end_h = self._future_range_18h()
        reservation = self._insert_reservation(
            start, start_h, end, end_h, 0, is_legacy=1, charged=0
        )
        with get_db() as conn:
            result = apply_extension(conn, reservation, end, "10")
            conn.commit()
            self.assertTrue(result["extended"])
            self.assertEqual(result["extra_to_pay_kr"], 50)
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(refreshed["is_legacy"], 1)
            self.assertEqual(refreshed["original_end_date"], end)
            self.assertEqual(refreshed["original_end_hour"], end_h)
            self.assertEqual(refreshed["end_hour"], "10")
            self.assertEqual(refreshed["charged_total_kr"], 50)
            self.assertEqual(refreshed["paid_total_kr"], 0)
            # Videre forlengelse trekker fra allerede belastet tillegg
            result2 = apply_extension(conn, refreshed, end, "12")
            conn.commit()
            # 08→12 next day = 28 timer → 100 kr total; baseline 50; charged 50 → ekstra 0
            self.assertEqual(result2["extra_to_pay_kr"], 0)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"
        self._old_db = main.DATABASE
        main.DATABASE = self.db_path
        init_db()

    def tearDown(self):
        main.DATABASE = self._old_db
        self._tmpdir.cleanup()

    def _create_legacy_bookings_table(self, conn):
        conn.execute("DELETE FROM price_charges")
        conn.execute("DELETE FROM reservations")
        conn.execute(
            """
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                car_reg TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                booking_hour TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                price_kr INTEGER,
                reservation_id INTEGER,
                UNIQUE (booking_date, booking_hour)
            )
            """
        )

    def _insert_hours(self, conn, name, phone, car_reg, day, hours, created_at, price_kr=None):
        for hour in hours:
            conn.execute(
                """
                INSERT INTO bookings
                    (name, phone, car_reg, booking_date, booking_hour, created_at, price_kr, reservation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (name, phone, car_reg, day, f"{hour:02d}", created_at, price_kr),
            )

    def test_migrate_legacy_without_price(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            self._insert_hours(
                conn,
                "Legacy",
                "11112222",
                "LE12345",
                "2026-05-01",
                range(8, 18),
                "2026-04-01 10:00:00",
            )
            conn.commit()
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()

            self.assertFalse(table_exists(conn, "bookings"))
            reservation = conn.execute("SELECT * FROM reservations").fetchone()
            self.assertEqual(reservation["start_hour"], "08")
            self.assertEqual(reservation["end_hour"], "18")
            self.assertEqual(reservation["charged_total_kr"], 0)
            self.assertEqual(reservation["paid_total_kr"], 0)
            self.assertEqual(reservation["is_legacy"], 1)
            self.assertEqual(reservation["original_end_hour"], "18")
            self.assertTrue(reservation["manage_token"])
            charges = conn.execute(
                "SELECT COUNT(*) n FROM price_charges WHERE reservation_id = ?",
                (reservation["id"],),
            ).fetchone()["n"]
            self.assertEqual(charges, 0)

    def test_created_at_keeps_abutting_bookings_separate(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            # Samme kunde, timer rett etter hverandre, men ulik created_at
            self._insert_hours(
                conn,
                "Ada",
                "12345678",
                "KJ12345",
                "2026-05-01",
                range(8, 12),
                "2026-04-01 10:00:00",
            )
            self._insert_hours(
                conn,
                "Ada",
                "12345678",
                "KJ12345",
                "2026-05-01",
                range(12, 16),
                "2026-04-02 11:00:00",
            )
            conn.commit()
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()

            rows = conn.execute(
                "SELECT start_hour, end_hour, created_at FROM reservations ORDER BY created_at"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["start_hour"], "08")
            self.assertEqual(rows[0]["end_hour"], "12")
            self.assertEqual(rows[1]["start_hour"], "12")
            self.assertEqual(rows[1]["end_hour"], "16")

    def test_gap_creates_separate_reservations(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            self._insert_hours(
                conn,
                "Ada",
                "12345678",
                "KJ12345",
                "2026-05-01",
                range(8, 10),
                "2026-04-01 10:00:00",
            )
            self._insert_hours(
                conn,
                "Ada",
                "12345678",
                "KJ12345",
                "2026-05-01",
                range(14, 16),
                "2026-04-01 10:00:00",
            )
            conn.commit()
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM reservations").fetchone()["n"], 2
            )

    def test_no_hour_rows_lost(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            total = 0
            self._insert_hours(
                conn, "A", "11111111", "AA11111", "2026-05-01", range(0, 5), "t1"
            )
            total += 5
            self._insert_hours(
                conn, "B", "22222222", "BB22222", "2026-05-02", range(10, 20), "t2"
            )
            total += 10
            conn.commit()
            before = conn.execute("SELECT COUNT(*) n FROM bookings").fetchone()["n"]
            self.assertEqual(before, total)
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()
            hours = 0
            for row in conn.execute(
                "SELECT start_date, start_hour, end_date, end_hour FROM reservations"
            ):
                hours += hours_between(
                    row["start_date"],
                    row["start_hour"],
                    row["end_date"],
                    row["end_hour"],
                )
            self.assertEqual(hours, total)

    def test_migration_aborts_without_dropping_on_validation_failure(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            self._insert_hours(
                conn,
                "Legacy",
                "11112222",
                "LE12345",
                "2026-05-01",
                range(8, 12),
                "2026-04-01 10:00:00",
            )
            conn.commit()
            with patch(
                "main.validate_migrated_reservations",
                side_effect=MigrationError("simulert feil"),
            ):
                with self.assertRaises(MigrationError):
                    migrate_hourly_bookings_to_reservations(conn)
            conn.rollback()
            self.assertTrue(table_exists(conn, "bookings"))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM bookings").fetchone()["n"], 4
            )

    def test_migration_idempotent_when_rerun(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            self._insert_hours(
                conn,
                "Legacy",
                "11112222",
                "LE12345",
                "2026-05-01",
                range(8, 12),
                "2026-04-01 10:00:00",
            )
            conn.commit()
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()
            self.assertFalse(table_exists(conn, "bookings"))
            count = conn.execute("SELECT COUNT(*) n FROM reservations").fetchone()["n"]
            migrate_hourly_bookings_to_reservations(conn)
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM reservations").fetchone()["n"],
                count,
            )

    def test_validate_detects_overlap(self):
        with get_db() as conn:
            self._create_legacy_bookings_table(conn)
            self._insert_hours(
                conn, "A", "11111111", "AA11111", "2026-05-01", range(8, 12), "t1"
            )
            conn.commit()
            migrate_hourly_bookings_to_reservations(conn)
            # Simuler overlap ved å endre en reservasjon før drop allerede er gjort
            # (re-opprett bookings for validate-test)
            conn.execute(
                """
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    car_reg TEXT NOT NULL,
                    booking_date TEXT NOT NULL,
                    booking_hour TEXT NOT NULL,
                    created_at TEXT,
                    price_kr INTEGER,
                    reservation_id INTEGER
                )
                """
            )
            rows = conn.execute("SELECT * FROM reservations").fetchall()
            self.assertEqual(len(rows), 1)
            r = rows[0]
            # Sett inn timerader som matcher, deretter lag overlappende reservasjon
            for h in range(8, 12):
                conn.execute(
                    """
                    INSERT INTO bookings
                        (name, phone, car_reg, booking_date, booking_hour, reservation_id)
                    VALUES (?, ?, ?, '2026-05-01', ?, ?)
                    """,
                    (r["name"], r["phone"], r["car_reg"], f"{h:02d}", r["id"]),
                )
            conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr, is_legacy,
                    original_end_date, original_end_hour
                ) VALUES ('other-token', 'B', '22222222', 'BB22222',
                          '2026-05-01', '10', '2026-05-01', '14', 0, 0, 1,
                          '2026-05-01', '14')
                """
            )
            other_id = conn.execute("SELECT id FROM reservations WHERE manage_token='other-token'").fetchone()["id"]
            for h in range(10, 14):
                conn.execute(
                    """
                    INSERT INTO bookings
                        (name, phone, car_reg, booking_date, booking_hour, reservation_id)
                    VALUES ('B', '22222222', 'BB22222', '2026-05-01', ?, ?)
                    """,
                    (f"{h:02d}", other_id),
                )
            with self.assertRaises(MigrationError):
                validate_migrated_reservations(conn)


class AdminPaidStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"
        self._old_db = main.DATABASE
        self._old_password = main.ADMIN_PASSWORD
        main.DATABASE = self.db_path
        main.ADMIN_PASSWORD = "test-admin-secret"
        init_db()

    def tearDown(self):
        main.DATABASE = self._old_db
        main.ADMIN_PASSWORD = self._old_password
        self._tmpdir.cleanup()

    def _future_range_18h(self):
        start_day = (datetime.now(TZ_OSLO) + timedelta(days=1)).date()
        end_day = start_day + timedelta(days=1)
        return start_day.isoformat(), "08", end_day.isoformat(), "02"

    def _insert(self, price=50, paid=0):
        start, start_h, end, end_h = self._future_range_18h()
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr,
                    is_legacy, original_end_date, original_end_hour
                ) VALUES ('paid-token', 'Ada', '12345678', 'KJ12345', ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (start, start_h, end, end_h, price, paid, end, end_h),
            )
            rid = cur.lastrowid
            conn.execute(
                """
                INSERT INTO price_charges (
                    reservation_id, amount_kr, total_price_kr, charged_total_after_kr,
                    kind, end_date, end_hour
                ) VALUES (?, ?, ?, ?, 'initial', ?, ?)
                """,
                (rid, price, price, price, end, end_h),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (rid,)
            ).fetchone()
            return row, end

    def test_mark_paid_sets_paid_equal_to_charged(self):
        reservation, _ = self._insert(50, 0)
        with get_db() as conn:
            ok = set_reservation_paid_status(conn, reservation["id"], True)
            conn.commit()
            self.assertTrue(ok)
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(refreshed["paid_total_kr"], 50)
            self.assertTrue(reservation_is_fully_paid(refreshed))
            groups = list_booking_groups(conn)
            self.assertTrue(groups[0]["is_paid"])
            self.assertEqual(groups[0]["price_kr"], 50)

    def test_unmark_paid_resets_to_zero(self):
        reservation, _ = self._insert(50, 50)
        with get_db() as conn:
            set_reservation_paid_status(conn, reservation["id"], False)
            conn.commit()
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(refreshed["paid_total_kr"], 0)
            self.assertFalse(reservation_is_fully_paid(refreshed))

    def test_paid_status_persists_after_reload(self):
        reservation, _ = self._insert(50, 0)
        with get_db() as conn:
            set_reservation_paid_status(conn, reservation["id"], True)
            conn.commit()
        with get_db() as conn:
            groups = list_booking_groups(conn)
            self.assertEqual(len(groups), 1)
            self.assertTrue(groups[0]["is_paid"])
            self.assertEqual(groups[0]["paid_total_kr"], 50)

    def test_extension_keeps_paid_and_unchecks_when_price_rises(self):
        reservation, end = self._insert(50, 0)
        with get_db() as conn:
            set_reservation_paid_status(conn, reservation["id"], True)
            conn.commit()
            paid_row = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(paid_row["paid_total_kr"], 50)
            self.assertTrue(reservation_is_fully_paid(paid_row))

            result = apply_extension(conn, paid_row, end, "10")
            conn.commit()
            self.assertTrue(result["extended"])
            refreshed = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(refreshed["charged_total_kr"], 100)
            self.assertEqual(refreshed["paid_total_kr"], 50)
            self.assertFalse(reservation_is_fully_paid(refreshed))

            set_reservation_paid_status(conn, reservation["id"], True)
            conn.commit()
            final = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
            self.assertEqual(final["paid_total_kr"], 100)
            self.assertTrue(reservation_is_fully_paid(final))

    def test_only_admin_can_change_paid_status_via_endpoint(self):
        from fastapi.testclient import TestClient

        reservation, _ = self._insert(50, 0)
        client = TestClient(main.app)

        denied = client.post(
            "/admin/paid",
            data={"reservation_id": str(reservation["id"]), "paid": "1"},
            follow_redirects=False,
        )
        self.assertEqual(denied.status_code, 303)
        self.assertEqual(denied.headers["location"], "/admin")

        with get_db() as conn:
            still = conn.execute(
                "SELECT paid_total_kr FROM reservations WHERE id = ?",
                (reservation["id"],),
            ).fetchone()
            self.assertEqual(still["paid_total_kr"], 0)

        login = client.post(
            "/admin/login",
            data={"password": "test-admin-secret"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)
        self.assertIn("admin_session", login.cookies)

        allowed = client.post(
            "/admin/paid",
            data={"reservation_id": str(reservation["id"]), "paid": "1"},
            cookies=login.cookies,
            follow_redirects=False,
        )
        self.assertEqual(allowed.status_code, 303)
        self.assertIn("Betaling+registrert", allowed.headers["location"])

        with get_db() as conn:
            refreshed = conn.execute(
                "SELECT paid_total_kr FROM reservations WHERE id = ?",
                (reservation["id"],),
            ).fetchone()
            self.assertEqual(refreshed["paid_total_kr"], 50)

        unchecked = client.post(
            "/admin/paid",
            data={"reservation_id": str(reservation["id"]), "paid": "0"},
            cookies=login.cookies,
            follow_redirects=False,
        )
        self.assertEqual(unchecked.status_code, 303)
        with get_db() as conn:
            cleared = conn.execute(
                "SELECT paid_total_kr FROM reservations WHERE id = ?",
                (reservation["id"],),
            ).fetchone()
            self.assertEqual(cleared["paid_total_kr"], 0)

    def test_admin_page_shows_paid_status_labels(self):
        from fastapi.testclient import TestClient

        unpaid, _ = self._insert(50, 0)
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr,
                    is_legacy, original_end_date, original_end_hour
                ) VALUES (
                    'paid-label-token', 'Bo', '87654321', 'XY98765',
                    ?, '10', ?, '18', 100, 100, 0, ?, '18'
                )
                """,
                (unpaid["start_date"], unpaid["end_date"], unpaid["end_date"]),
            )
            conn.commit()

        client = TestClient(main.app)
        login = client.post(
            "/admin/login",
            data={"password": "test-admin-secret"},
            follow_redirects=False,
        )
        page = client.get("/admin", cookies=login.cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Ikke betalt", page.text)
        self.assertIn(">Betalt<", page.text)
        self.assertIn("admin-paid-toggle is-paid", page.text)
        self.assertIn("admin-paid-toggle is-unpaid", page.text)

    def test_admin_copy_links_use_existing_tokens_and_are_unique(self):
        from fastapi.testclient import TestClient
        from starlette.requests import Request

        first, _ = self._insert(50, 0)
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO reservations (
                    manage_token, name, phone, car_reg,
                    start_date, start_hour, end_date, end_hour,
                    charged_total_kr, paid_total_kr,
                    is_legacy, original_end_date, original_end_hour
                ) VALUES (
                    'second-manage-token', 'Bo', '87654321', 'XY98765',
                    ?, '12', ?, '16', 50, 0, 0, ?, '16'
                )
                """,
                (first["start_date"], first["end_date"], first["end_date"]),
            )
            conn.commit()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/admin",
                "raw_path": b"/admin",
                "query_string": b"",
                "headers": [
                    (b"host", b"parkering-booking.onrender.com"),
                    (b"x-forwarded-proto", b"https"),
                ],
                "client": ("1.1.1.1", 123),
                "server": ("parkering-booking.onrender.com", 443),
            }
            groups = list_booking_groups(conn, Request(scope))

        urls = [g["manage_url"] for g in groups]
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(set(urls)), 2)
        self.assertTrue(
            all(u.startswith("https://parkering-booking.onrender.com/forleng/") for u in urls)
        )
        self.assertTrue(any(u.endswith("/forleng/paid-token") for u in urls))
        self.assertTrue(any(u.endswith("/forleng/second-manage-token") for u in urls))

        client = TestClient(main.app)
        anonymous = client.get("/admin")
        self.assertEqual(anonymous.status_code, 200)
        self.assertNotIn("/forleng/", anonymous.text)
        self.assertNotIn("paid-token", anonymous.text)
        self.assertNotIn("Kopier administrasjonslenke", anonymous.text)

        login = client.post(
            "/admin/login",
            data={"password": "test-admin-secret"},
            follow_redirects=False,
        )
        page = client.get("/admin", cookies=login.cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Kopier administrasjonslenke", page.text)
        self.assertIn('data-url="http://testserver/forleng/paid-token"', page.text)
        self.assertIn(
            'data-url="http://testserver/forleng/second-manage-token"', page.text
        )
        self.assertIn("/static/admin.js?v=", page.text)

        opened = client.get("/forleng/paid-token")
        self.assertEqual(opened.status_code, 200)
        self.assertIn("Ada", opened.text)
        self.assertIn("KJ12345", opened.text)


if __name__ == "__main__":
    unittest.main()
