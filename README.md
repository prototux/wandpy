# WandPy

A Python library for controlling the Kano Coding Wand with IMU sensor, RGB LED, vibration motor, and button input.

## Quick Start

```python
import asyncio
from wandpy import Wand, Color

async def main():
    # Scan for wands
    wand = Wand()
    devices = await wand.scan()
    print(f"Found: {devices}")

    # Connect to the first wand found
    await wand.connect(devices[0][0])

    # Set LED to teal
    await wand.set_led(Color.TEAL)

    # Vibrate
    await wand.vibrate()

    # Print IMU data for 10 seconds
    def on_imu(data):
        print(f"Euler: {data.to_euler_angles()}")
        print(f"Raw quaternions: {data.raw_components}")

    wand.on_imu_quaternions = on_imu

    await asyncio.sleep(10)
    await wand.disconnect()

asyncio.run(main())
