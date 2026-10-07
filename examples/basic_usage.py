"""
Basic usage example for WandPy.

Works unchanged with a Kano Coding Wand or a Magic Caster Wand.

This example shows how to:
1. Scan for wands
2. Connect to one
3. Set up callbacks for IMU and button data
4. Control the LED and vibration
5. Use wand-specific features when they are available
"""

import asyncio
import sys

from wandpy import ButtonEvent, Color, Feature, LedGroup, LedPattern, VibrationPattern, Wand


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
    for device in devices:
        print(f"  {device}")

    # Connect to the first one (strongest signal)
    print(f"\nConnecting to {devices[0].name}...")
    if not await wand.connect(devices[0]):
        print("Failed to connect!")
        sys.exit(1)

    info = wand.info
    print(f"Connected to a {wand.wand_type.value} wand ({info.model or info.manufacturer or 'unknown model'})")
    print(f"Battery: {wand.state.battery}%")

    # --- Set up callbacks (plain functions or async functions) ---

    def on_imu_quats(data):
        """Called when new orientation data arrives."""
        print(
            f"\rIMU: yaw={data.yaw:6.1f}° pitch={data.pitch:6.1f}° roll={data.roll:6.1f}°",
            end="",
            flush=True,
        )

    def on_imu_raw(data):
        """Called for every raw IMU sample."""
        if data.valid:
            pass  # Uncomment to see raw sensor data:
            # print(f"Accel: {data.accel_xyz} Gyro: {data.gyro_xyz}")

    async def on_button(event, state):
        """Called on button events (Kano button, or full grip on the Magic Caster)."""
        if event == ButtonEvent.PRESSED:
            print(f"\n[Button PRESSED] Count: {state.press_count}")
            await wand.set_led(Color.GREEN)
        elif event == ButtonEvent.RELEASED:
            print(f"\n[Button RELEASED] Duration: {state.last_press_duration:.3f}s")
            await wand.set_led(Color.BLUE)
            if state.was_double_press(window_ms=500):
                print("[DOUBLE PRESS DETECTED!]")
                await wand.vibrate(VibrationPattern.BURST)

    async def on_spell(spell):
        """Magic Caster only: the wand recognized a spell."""
        print(f"\n[SPELL] {spell.name}")
        if spell.matches("lumos"):
            await wand.set_led(Color.WHITE, group=LedGroup.ALL)  # Every LED (one LED on Kano)
        elif spell.matches("nox"):
            await wand.led_off()

    wand.on_imu_quaternions = on_imu_quats
    wand.on_imu_raw = on_imu_raw
    wand.on_button_event = on_button
    wand.on_buttons = lambda pads: print(f"\nButtons/pads: {pads}") if len(pads) > 1 else None
    wand.on_spell = on_spell

    # --- Initial LED color and pattern (emulated on the Magic Caster) ---
    await wand.set_led(Color.TEAL, LedPattern.PULSE_SLOW)

    # --- Main loop ---
    print("\nPress Ctrl+C to exit...")
    print("Button: press for green LED, double-press for burst vibration")
    if wand.supports(Feature.SPELLS):
        print("Grip all four pads and draw a spell (try Lumos and Nox)")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\nShutting down...")

    # --- Cleanup ---
    await wand.led_off()
    await wand.disconnect()
    print("Disconnected. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
