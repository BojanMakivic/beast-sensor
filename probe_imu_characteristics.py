"""Read-only Agile VBT BLE inspection and notification-rate measurement."""

from __future__ import annotations

import argparse
import asyncio
import struct
import time
from dataclasses import dataclass

from bleak import BleakClient

from agile_vbt_motion import SAMPLE_RATE_HZ


DEVICE_ADDRESS = "BE:A5:7F:30:78:68"
IMU_CHARACTERISTIC_UUID = "bea5760d-503d-4920-b000-101e7306b003"


@dataclass(frozen=True)
class RateStatistics:
    received_packets: int
    sequence_steps: int
    missing_samples: int
    duplicate_packets: int
    elapsed_s: float
    sequence_rate_hz: float
    notification_rate_hz: float
    interval_p10_ms: float
    interval_median_ms: float
    interval_p90_ms: float


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def calculate_rate_statistics(
    timestamps: list[float],
    sequences: list[int],
) -> RateStatistics:
    """Summarize a notification capture using packet sequence numbers."""
    if len(timestamps) != len(sequences):
        raise ValueError("timestamps and sequences must have equal lengths")
    if len(timestamps) < 2:
        return RateStatistics(
            len(timestamps),
            0,
            0,
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )

    sequence_steps = 0
    missing_samples = 0
    duplicate_packets = 0
    for previous, current in zip(sequences, sequences[1:]):
        delta = (current - previous) & 0xFFFF
        if delta == 0:
            duplicate_packets += 1
            continue
        sequence_steps += delta
        missing_samples += max(0, delta - 1)

    elapsed_s = max(0.0, timestamps[-1] - timestamps[0])
    intervals_ms = [
        (current - previous) * 1000.0
        for previous, current in zip(timestamps, timestamps[1:])
    ]
    return RateStatistics(
        received_packets=len(timestamps),
        sequence_steps=sequence_steps,
        missing_samples=missing_samples,
        duplicate_packets=duplicate_packets,
        elapsed_s=elapsed_s,
        sequence_rate_hz=(
            sequence_steps / elapsed_s if elapsed_s > 0.0 else 0.0
        ),
        notification_rate_hz=(
            (len(timestamps) - 1) / elapsed_s if elapsed_s > 0.0 else 0.0
        ),
        interval_p10_ms=_percentile(intervals_ms, 0.10),
        interval_median_ms=_percentile(intervals_ms, 0.50),
        interval_p90_ms=_percentile(intervals_ms, 0.90),
    )


async def _read_services(client: BleakClient) -> None:
    print("\nGATT services and characteristics (read-only)")
    for service in client.services:
        print(f"Service {service.uuid} | {service.description}")
        for characteristic in service.characteristics:
            properties = ",".join(characteristic.properties) or "none"
            line = (
                f"  Characteristic {characteristic.uuid} | "
                f"{characteristic.description} | {properties}"
            )
            if "read" in characteristic.properties:
                try:
                    value = await asyncio.wait_for(
                        client.read_gatt_char(characteristic),
                        timeout=2.0,
                    )
                    line += f" | len={len(value)} raw={bytes(value).hex()}"
                except Exception as exc:
                    line += f" | read failed: {exc}"
            print(line)


async def _measure_rate(
    client: BleakClient,
    characteristic_uuid: str,
    duration_s: float,
) -> RateStatistics:
    timestamps: list[float] = []
    sequences: list[int] = []

    def handler(_characteristic, data: bytearray) -> None:
        if len(data) < 2:
            return
        timestamps.append(time.perf_counter())
        sequences.append(struct.unpack_from("<H", data)[0])

    print(f"\nMeasuring IMU notifications for {duration_s:.1f} seconds...")
    await client.start_notify(characteristic_uuid, handler)
    try:
        await asyncio.sleep(duration_s)
    finally:
        await client.stop_notify(characteristic_uuid)
    return calculate_rate_statistics(timestamps, sequences)


async def inspect_sensor(
    address: str,
    characteristic_uuid: str,
    duration_s: float,
) -> None:
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}")
        print(f"Address: {address}")
        await _read_services(client)
        statistics = await _measure_rate(
            client,
            characteristic_uuid,
            duration_s,
        )

    print("\nNotification-rate result")
    print(f"  Received packets: {statistics.received_packets}")
    print(f"  Sequence steps: {statistics.sequence_steps}")
    print(f"  Missing samples: {statistics.missing_samples}")
    print(f"  Duplicate packets: {statistics.duplicate_packets}")
    print(f"  Measured sequence rate: {statistics.sequence_rate_hz:.3f} Hz")
    print(f"  Tracker processing rate: {SAMPLE_RATE_HZ:.3f} Hz")
    if statistics.sequence_rate_hz > 0.0:
        deviation_percent = (
            (statistics.sequence_rate_hz - SAMPLE_RATE_HZ)
            / SAMPLE_RATE_HZ
            * 100.0
        )
        print(f"  Processing-rate difference: {deviation_percent:+.2f}%")
        if abs(deviation_percent) > 2.0:
            print(
                "  WARNING: measured delivery differs by more than 2%; "
                "velocity and distance can inherit timing error."
            )
    print(
        "  Host notification rate: "
        f"{statistics.notification_rate_hz:.3f} Hz"
    )
    print(
        "  Host intervals p10 / median / p90: "
        f"{statistics.interval_p10_ms:.3f} / "
        f"{statistics.interval_median_ms:.3f} / "
        f"{statistics.interval_p90_ms:.3f} ms"
    )
    print("\nReadable settings")
    print(
        "  Sample-rate setting: no standard readable GATT "
        "characteristic is exposed."
    )
    print(
        "  Pairing PIN: not exposed through GATT. A PIN/passkey, if the "
        "firmware uses one, cannot be read as a characteristic."
    )
    print(
        "  No writes were attempted. Unknown writable characteristics "
        "were intentionally left unchanged."
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the Agile VBT GATT database and measure IMU packet rate "
            "without writing to the sensor."
        )
    )
    parser.add_argument("--address", default=DEVICE_ADDRESS)
    parser.add_argument(
        "--characteristic",
        default=IMU_CHARACTERISTIC_UUID,
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=12.0,
        help="Notification measurement duration in seconds (default: 12).",
    )
    args = parser.parse_args()
    if args.duration < 1.0:
        parser.error("--duration must be at least 1 second")
    return args


def main() -> None:
    args = parse_arguments()
    asyncio.run(
        inspect_sensor(
            args.address,
            args.characteristic,
            args.duration,
        )
    )


if __name__ == "__main__":
    main()
