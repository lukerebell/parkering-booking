"""Tester for prisberegning og avrunding av påbegynte døgn."""

import unittest

from main import (
    billable_days_from_hours,
    calculate_booking_price,
    calculate_price_for_days,
    hours_between,
)


class BillableDaysTests(unittest.TestCase):
    def test_one_hour_rounds_up_to_one_day(self):
        self.assertEqual(billable_days_from_hours(1), 1)

    def test_exactly_24_hours_is_one_day(self):
        self.assertEqual(billable_days_from_hours(24), 1)

    def test_25_hours_rounds_up_to_two_days(self):
        self.assertEqual(billable_days_from_hours(25), 2)

    def test_47_hours_rounds_up_to_two_days(self):
        self.assertEqual(billable_days_from_hours(47), 2)

    def test_48_hours_is_two_days(self):
        self.assertEqual(billable_days_from_hours(48), 2)

    def test_zero_hours_raises(self):
        with self.assertRaises(ValueError):
            billable_days_from_hours(0)


class PriceForDaysTests(unittest.TestCase):
    def test_examples_from_spec(self):
        cases = {
            1: 50,
            5: 250,
            7: 300,
            8: 350,
            12: 550,
            14: 600,
            30: 1000,
            31: 1050,
        }
        for days, expected in cases.items():
            with self.subTest(days=days):
                self.assertEqual(calculate_price_for_days(days), expected)

    def test_six_days_not_more_than_week(self):
        self.assertEqual(calculate_price_for_days(6), 300)

    def test_week_cheaper_than_seven_days_at_day_rate(self):
        self.assertLess(calculate_price_for_days(7), 7 * 50)

    def test_month_cheaper_than_thirty_days_at_day_rate(self):
        self.assertLess(calculate_price_for_days(30), 30 * 50)

    def test_29_days_uses_month_package(self):
        self.assertEqual(calculate_price_for_days(29), 1000)

    def test_13_days_uses_two_weeks_or_week_plus_days(self):
        self.assertEqual(calculate_price_for_days(13), 600)

    def test_never_more_than_day_rate_total(self):
        for days in range(1, 91):
            with self.subTest(days=days):
                self.assertLessEqual(
                    calculate_price_for_days(days),
                    days * 50,
                )


class HoursBetweenAndPriceTests(unittest.TestCase):
    def test_same_day_partial_hours_is_one_day_price(self):
        hours = hours_between("2026-06-01", "08", "2026-06-01", "18")
        self.assertEqual(hours, 10)
        self.assertEqual(calculate_booking_price(hours), 50)

    def test_exactly_24_hours_is_one_day_price(self):
        hours = hours_between("2026-06-01", "08", "2026-06-02", "08")
        self.assertEqual(hours, 24)
        self.assertEqual(calculate_booking_price(hours), 50)

    def test_just_over_24_hours_is_two_days(self):
        hours = hours_between("2026-06-01", "08", "2026-06-02", "09")
        self.assertEqual(hours, 25)
        self.assertEqual(calculate_booking_price(hours), 100)

    def test_seven_full_days_is_week_price(self):
        hours = hours_between("2026-06-01", "00", "2026-06-08", "00")
        self.assertEqual(hours, 7 * 24)
        self.assertEqual(calculate_booking_price(hours), 300)

    def test_eight_days_is_week_plus_day(self):
        hours = hours_between("2026-06-01", "12", "2026-06-09", "12")
        self.assertEqual(hours, 8 * 24)
        self.assertEqual(calculate_booking_price(hours), 350)

    def test_twelve_days_optimal_combo(self):
        hours = hours_between("2026-06-01", "10", "2026-06-13", "10")
        self.assertEqual(hours, 12 * 24)
        self.assertEqual(calculate_booking_price(hours), 550)

    def test_thirty_days_is_month_price(self):
        hours = hours_between("2026-06-01", "00", "2026-07-01", "00")
        self.assertEqual(hours, 30 * 24)
        self.assertEqual(calculate_booking_price(hours), 1000)

    def test_thirty_one_days_is_month_plus_day(self):
        hours = hours_between("2026-06-01", "00", "2026-07-02", "00")
        self.assertEqual(hours, 31 * 24)
        self.assertEqual(calculate_booking_price(hours), 1050)

    def test_end_equal_to_start_rejected(self):
        with self.assertRaises(ValueError):
            hours_between("2026-06-01", "10", "2026-06-01", "10")

    def test_end_before_start_rejected(self):
        with self.assertRaises(ValueError):
            hours_between("2026-06-02", "10", "2026-06-01", "12")


if __name__ == "__main__":
    unittest.main()
