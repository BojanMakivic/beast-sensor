import unittest

from probe_imu_characteristics import calculate_rate_statistics


class ProbeRateTests(unittest.TestCase):
    def test_sequence_rate_and_missing_samples_are_measured_separately(self):
        statistics = calculate_rate_statistics(
            [0.00, 0.02, 0.04, 0.10],
            [10, 11, 12, 15],
        )
        self.assertEqual(statistics.received_packets, 4)
        self.assertEqual(statistics.sequence_steps, 5)
        self.assertEqual(statistics.missing_samples, 2)
        self.assertEqual(statistics.duplicate_packets, 0)
        self.assertAlmostEqual(statistics.sequence_rate_hz, 50.0)
        self.assertAlmostEqual(statistics.notification_rate_hz, 30.0)
        self.assertAlmostEqual(statistics.interval_median_ms, 20.0)

    def test_sequence_counter_wrap_and_duplicate_are_supported(self):
        statistics = calculate_rate_statistics(
            [0.00, 0.02, 0.04, 0.06],
            [65535, 0, 0, 1],
        )
        self.assertEqual(statistics.sequence_steps, 2)
        self.assertEqual(statistics.missing_samples, 0)
        self.assertEqual(statistics.duplicate_packets, 1)

    def test_mismatched_capture_arrays_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rate_statistics([0.0], [])


if __name__ == "__main__":
    unittest.main()
