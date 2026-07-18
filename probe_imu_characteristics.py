import asyncio
import struct

from bleak import BleakClient


DEVICE_ADDRESS = "BE:A5:7F:30:78:68"
CHARACTERISTICS = [
    f"bea5760d-503d-4920-b000-101e7306b00{index}"
    for index in range(1, 4)
]


async def main() -> None:
    async with BleakClient(DEVICE_ADDRESS) as client:
        print(f"Connected: {client.is_connected}")

        for uuid in CHARACTERISTICS:
            received = asyncio.Event()

            def handler(characteristic, data: bytearray) -> None:
                if received.is_set():
                    return

                values = struct.unpack("<hhh", data[2:8]) if len(data) >= 8 else None
                print(f"{characteristic.uuid}: len={len(data)} raw={data.hex()} values={values}")
                received.set()

            await client.start_notify(uuid, handler)
            try:
                await asyncio.wait_for(received.wait(), timeout=3.0)
            except TimeoutError:
                print(f"{uuid}: no notification received")
            finally:
                await client.stop_notify(uuid)


if __name__ == "__main__":
    asyncio.run(main())
