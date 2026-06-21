"""
Basic usage example for WandPy.

This example shows how to:
1. Scan for wands
2. Connect to one
3. Set up callbacks for IMU and button data
4. Control the LED and vibration
"""

import asyncio
import sys

from wandpy import ButtonEvent, Color, VibrationPattern, Wand


async def main():
    # Create wand instance
    wand = Wand()

    # --- Scan for devices ---
    print("Scanning for wands...")
    devices = await wand.scan(timeout=5.0)

    if not devices:
        print("No wands found. Make sure your device is on and in range.")
        sys.exit(1)

    print(f"Found {len(devices)} wand(s):")
    for addr, name, uuids in devices:
        print(f"  {addr} - {name}")

    # Connect to the first one
    target = devices[0][0]
    print(f"\nConnecting to {target}...")
    success = await wand.connect(target)

    if not success:
        print("Failed to connect!")
        sys.exit(1)

    print("Connected!")

    # --- Set up callbacks ---

    def on_imu_quats(data):
        """Called when new quaternion data arrives."""
        print(
            f"\rIMU: yaw={data.euler['yaw']:6.1f}° "
            f"pitch={data.euler['pitch']:6.1f}° "
            f"roll={data.euler['roll']:6.1f}°",
            end="",
            flush=True,
        )

    def on_imu_raw(data):
        """Called when raw 9-axis data arrives."""
        if not data.error:
            accel = data.accel
            # Uncomment to see raw sensor data:
            # print(f"Accel: x={accel['x']}, y={accel['y']}, z={accel['z']}")

    def on_button(event, state):
        """Called on button events."""
        if event == ButtonEvent.PRESSED:
            print(f"\n[Button PRESSED] Count: {state.press_count}")
            # Flash LED green on press
            asyncio.create_task(wand.set_led(Color.GREEN))
        elif event == ButtonEvent.RELEASED:
            print(f"\n[Button RELEASED] Duration: {state.press_duration:.3f}s")
            # Return to teal on release
            asyncio.create_task(wand.set_led(Color.BLUE))
            # Check for double press
            if state.was_double_press(window_ms=500):
                print("[DOUBLE PRESS DETECTED!]")
                asyncio.create_task(wand.vibrate(VibrationPattern.BURST))

    wand.on_imu_quaternions = on_imu_quats
    wand.on_imu_raw = on_imu_raw
    wand.on_button_event = on_button

    # --- Initial LED color ---
    await wand.set_led(Color.TEAL, 2)

    # --- Main loop ---
    print("\nPress Ctrl+C to exit...")
    print("Button: press for green LED, double-press for burst vibration")

    try:
        while True:
            await asyncio.sleep(1)
            # Print battery every 10 seconds (roughly)
            if wand.state.battery is not None:
                pass  # Battery updates come via callback
    except KeyboardInterrupt:
        print("\n\nShutting down...")

    # --- Cleanup ---
    await wand.set_led(Color.OFF)
    await wand.disconnect()
    print("Disconnected. Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
