# WandPy

Control Bluetooth magic wands from Python:

- **Kano Coding Wand**
- **Harry Potter Magic Caster Wand**

One `Wand` class drives both. WandPy detects the wand type when you connect,
and lights, vibration, buttons, battery and motion work the same way on each,
so the same script runs on either wand. Features only one wand has (spells,
touch pads, temperature…) are on the same object.

## Contents

- [Installation](#installation)
- [Quickstart](#quickstart)
- [Common tasks](#common-tasks)
- [Comparing the wands](#comparing-the-wands)
- [API cheat sheet](#api-cheat-sheet)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)
- [Good to know](#good-to-know)
- [Upgrading from 1.x](#upgrading-from-1x)

## Installation

You need Python 3.9+ and a computer with Bluetooth Low Energy (Linux, macOS or
Windows; WandPy uses [bleak](https://github.com/hbldh/bleak)).

```bash
pip install git+https://github.com/prototux/wandpy.git
```

Or from a clone, to also get the examples:

```bash
git clone https://github.com/prototux/wandpy.git
cd wandpy
pip install -e .
```

## Quickstart

1. Wake your wand up: press the Kano button, or pick up the Magic Caster wand.
   Close any phone app that could be connected to it.
2. Save this as `hello_wand.py`:

   ```python
   import asyncio
   from wandpy import Wand, Color, ButtonEvent

   async def main():
       async with Wand() as wand:  # Connects to the nearest wand of any type
           print(f"Connected to a {wand.wand_type.value} wand, battery {wand.state.battery}%")
           await wand.set_led(Color.TEAL)
           await wand.vibrate()

           async def on_button(event, state):
               if event == ButtonEvent.PRESSED:
                   await wand.set_led(Color.GREEN)
               elif event == ButtonEvent.RELEASED:
                   await wand.set_led(Color.TEAL)

           wand.on_button_event = on_button
           wand.on_imu_quaternions = lambda q: print(f"yaw={q.yaw:7.1f}  pitch={q.pitch:6.1f}  roll={q.roll:7.1f}")

           await asyncio.sleep(30)  # Play with the wand for 30 seconds

   asyncio.run(main())
   ```

3. Run it with `python hello_wand.py`. The wand lights up and buzzes, the
   console shows its orientation, and the light turns green while you press
   the button. On the Magic Caster, "pressing the button" means gripping all
   four touch pads.

## Common tasks

Everything WandPy does is `async`, so call it from an `async def` function and
start your program with `asyncio.run(main())`.

### Connect

```python
from wandpy import Wand, KanoWand, MagicCasterWand

# The nearest wand, whatever its type. Disconnects automatically at the end
async with Wand() as wand:
    ...

# Without a "with" block
wand = Wand()
await wand.connect()          # Returns False if no wand was found
...
await wand.disconnect()

# Pick from a scan (strongest signal first)
wands = await Wand.scan(timeout=5)
for w in wands:
    print(w.name, w.address, w.wand_type, w.rssi)
await wand.connect(wands[0])

# A known address
await Wand("AA:BB:CC:DD:EE:FF").connect()

# Only one type of wand
async with MagicCasterWand() as wand:
    ...
```

The wand reconnects by itself if the link drops. Set `wand.on_disconnect` and
`wand.on_connect` to be notified, or pass `auto_reconnect=False`.

### Lights

```python
from wandpy import Color, LedPattern, LedGroup

await wand.set_led(Color.PURPLE)                         # Named color
await wand.set_led(Color(255, 128, 0))                   # Any RGB color
await wand.set_led(Color.from_hex("#ff8800"))
await wand.set_led(Color.RED, LedPattern.PULSE_FAST)     # Built-in effects
await wand.set_led(Color.BLUE, duration_ms=1000)         # Light for one second
await wand.led_off()

# Magic Caster only (ignored on the Kano, which has one LED and no fades)
await wand.set_led(Color.WHITE, group=LedGroup.ALL, transition_ms=500)
await wand.set_led(Color.GREEN, group=LedGroup.POMMEL)
```

### Vibration

```python
from wandpy import VibrationPattern

await wand.vibrate()                                     # Regular buzz
await wand.vibrate(VibrationPattern.BURST)
await wand.vibrate(duration_ms=300)                      # Closest pattern on the Kano
```

### Button

```python
from wandpy import ButtonEvent

def on_button(event, state):
    if event == ButtonEvent.PRESSED:
        print("pressed", state.press_count)
    elif event == ButtonEvent.RELEASED:
        print(f"released after {state.last_press_duration:.2f}s")
        if state.was_double_press():
            print("double press!")
    elif event == ButtonEvent.HOLD:
        print("held for", state.press_duration)

wand.on_button_event = on_button

pressed = await wand.wait_for_press(timeout=10)          # Or wait for one press
wand.on_buttons = lambda pads: print(pads)               # (True,) on Kano, 4 pads on Magic Caster
```

### Motion

```python
# Orientation in degrees, ready to use
wand.on_imu_quaternions = lambda q: print(q.yaw, q.pitch, q.roll)

# Raw sensor samples
def on_raw(sample):
    print(sample.accel_xyz, sample.gyro_xyz)             # Same on both wands
    print(sample.accel_g, sample.gyro_dps)               # Physical units (Magic Caster)

wand.on_imu_raw = on_raw

await wand.reset_quaternions()                           # The current position becomes "straight ahead"
await wand.stop_imu()                                    # Save battery when you don't need motion
await wand.start_imu()
```

### Spells, touch pads and macros (Magic Caster)

```python
from wandpy import Color, LedGroup, Macro

# Grip all four pads, draw a spell, release
wand.on_spell = lambda spell: print("Cast", spell.name)   # "Expecto Patronum"

spell = await wand.wait_for_spell(timeout=30)
if spell and spell.matches("lumos"):
    await wand.set_led(Color.WHITE, group=LedGroup.ALL)

# Light/haptic sequences played by the wand itself, with exact timings
await wand.play_macro(
    Macro()
    .buzz(150)
    .repeat(3, Macro().led(Color.RED, LedGroup.TIP).delay(150).led(Color.OFF).delay(150))
    .led(Color.GOLD, LedGroup.ALL, transition_ms=400)
)

# Touch pad sensitivity: one (min, max) pair for all pads, or one per pad
await wand.set_button_thresholds((5, 8))
await wand.calibrate_buttons()                           # Don't touch the pads meanwhile
```

### Battery, device info and other data

```python
print(wand.state.battery)                                # Latest values, always available
wand.on_battery = lambda level: print(f"{level}%")

print(wand.info.model, wand.info.firmware_version)       # e.g. "DEFIANT 0.3" (Magic Caster)

wand.on_temperature = print                              # Kano only
wand.on_imu_fused = print                                # Kano only

history = wand.get_history("battery")                    # (timestamp, value) pairs
```

### Write code for both wands

Methods for a feature the wand doesn't have raise `UnsupportedFeatureError`.
Callbacks for data the wand never sends are simply never called. Check before
you call:

```python
from wandpy import Feature

if wand.supports(Feature.SPELLS):
    wand.on_spell = on_spell
else:
    print("This wand doesn't recognize spells, use the button instead")
```

## Comparing the wands

### Hardware

|                         | Kano Coding Wand                                  | Magic Caster Wand                                             |
| ----------------------- | ------------------------------------------------- | ------------------------------------------------------------- |
| Bluetooth name          | `Kano-Wand-…`                                     | `MCW-…`                                                       |
| Models                  | One                                               | Defiant, Loyal, Heroic, Honourable, Adventurous, Wise         |
| Lights                  | 1 RGB LED, RGB565 color                           | 4 RGB LED groups (tip, mid upper, mid lower, pommel), RGB888  |
| Light effects           | 7 built-in patterns (pulse, blink, RGB cycle…)    | Fades between colors, plus on-wand macros (sequences, loops)  |
| Vibration               | 7 built-in patterns                               | Buzz for a given duration, plus on-wand macros                |
| Input                   | 1 push button                                     | 4 capacitive touch pads (adjustable sensitivity)              |
| Motion sensors          | 9-axis: accelerometer, gyroscope, magnetometer    | 6-axis: accelerometer (±16 g), gyroscope (±2000 °/s)          |
| Orientation             | Computed on the wand (quaternions, fused data)    | Raw samples only (~234 Hz); WandPy computes the orientation   |
| Heading                 | Absolute (magnetometer)                           | Relative to start, slowly drifts (no magnetometer)            |
| Spell recognition       | No                                                | Yes, on the wand                                              |
| Other sensors           | Temperature                                       | None                                                          |
| Battery level           | Yes                                               | Yes (standard BLE battery service)                            |
| Device info             | Maker name, hardware revision                     | Model, firmware, serial number, SKU, paired box address       |
| Connection upkeep       | Keepalive packet every 3 s (handled by WandPy)    | None needed                                                   |

### What WandPy supports on each wand

| Feature                                            | Kano                 | Magic Caster                              |
| -------------------------------------------------- | -------------------- | ----------------------------------------- |
| `set_led(color)`                                   | ✅                   | ✅ (full RGB888)                          |
| `set_led(color, LedPattern.X)`                     | ✅ native            | ✅ emulated with macros                   |
| `set_led(..., group=LedGroup.X)`                   | single LED (ignored) | ✅ 4 groups: tip → pommel, or `ALL`       |
| `set_led(..., transition_ms=)` fades               | applied instantly    | ✅                                        |
| `set_led(..., duration_ms=)`                       | ✅                   | ✅                                        |
| `vibrate(VibrationPattern.X)`                      | ✅ native            | ✅ emulated with macros                   |
| `vibrate(duration_ms=)`                            | closest pattern      | ✅                                        |
| Button events, hold, double press                  | ✅ button            | ✅ full grip (all 4 pads)                 |
| `on_buttons` per-pad state                         | 1 button             | ✅ 4 touch pads                           |
| Touch pad thresholds / calibration                 | ❌                   | ✅                                        |
| Battery                                            | ✅                   | ✅                                        |
| `on_imu_quaternions` orientation                   | ✅ on-device         | ✅ computed by WandPy                     |
| `on_imu_raw` accel/gyro samples                    | ✅ + magnetometer    | ✅ ~234 Hz, with g and rad/s units        |
| `on_imu_fused`                                     | ✅                   | ❌                                        |
| `on_temperature`                                   | ✅                   | ❌                                        |
| `start_imu()` / `stop_imu()`                       | ✅                   | ✅                                        |
| `reset_quaternions()`                              | ✅                   | ✅                                        |
| `calibrate_imu()`                                  | ✅ magnetometer      | ✅                                        |
| `on_spell` / `wait_for_spell()` spell recognition  | ❌                   | ✅ on-device                              |
| `play_macro(Macro)` light/haptic sequences         | ❌                   | ✅                                        |
| Device info (`wand.info`)                          | maker, hardware      | model, firmware, serial, SKU, box address |

## API cheat sheet

### Creating a wand

| Option                    | Default         | What it does                                                         |
| ------------------------- | --------------- | -------------------------------------------------------------------- |
| `mac_address`             | `None`          | Wand to connect to; `None` picks the nearest one                     |
| `wand_type`               | `None`          | Force `WandType.KANO` or `WandType.MAGIC_CASTER` instead of detecting |
| `auto_reconnect`          | `True`          | Reconnect after an unexpected disconnection                          |
| `imu_streaming`           | `True`          | Stream motion data from the start                                    |
| `keepalive_interval`      | wand's need     | Seconds between keepalive packets, `0` to disable                    |
| `button_thresholds`       | `None`          | Magic Caster pad sensitivity applied on connect                      |
| `button_debounce_ms`      | `20`            | Ignore button changes faster than this                               |
| `button_hold_interval_ms` | `50`            | Time between `HOLD` events                                           |
| `connect_timeout`         | `20`            | Seconds to wait for the Bluetooth connection                         |

### Callbacks

Assign a plain or `async` function. Keep plain functions quick; use `async`
ones to send commands to the wand.

| Callback             | Called with                         | When                                          |
| -------------------- | ----------------------------------- | --------------------------------------------- |
| `on_button_event`    | `(ButtonEvent, ButtonState)`        | Press, release, and repeatedly while held     |
| `on_button`          | `bool`                              | Press (`True`) and release (`False`)          |
| `on_buttons`         | `tuple[bool, ...]`                  | Any button/pad change                         |
| `on_imu_quaternions` | `QuaternionData` (`.yaw .pitch .roll`) | New orientation                            |
| `on_imu_raw`         | `RawData`                           | Every raw motion sample                       |
| `on_imu_fused`       | `FusedData`                         | New fused data (Kano)                         |
| `on_spell`           | `Spell` (`.name`, `.matches()`)     | A spell was recognized (Magic Caster)         |
| `on_battery`         | `int` (percent)                     | Battery level update                          |
| `on_temperature`     | `int`                               | Temperature update (Kano)                     |
| `on_connect`         | nothing                             | Connected (also after a reconnection)         |
| `on_disconnect`      | nothing                             | Connection lost unexpectedly                  |

### Methods and properties

| Method / property                                                  | Description                                     |
| ------------------------------------------------------------------ | ----------------------------------------------- |
| `Wand.scan(timeout, wand_type)`                                    | List nearby wands (`WandInfo`)                  |
| `connect(target)` / `disconnect()`                                 | Open / close the connection                     |
| `is_connected`, `wand_type`, `features`, `supports(feature)`       | What is connected and what it can do            |
| `info`, `get_device_info()`                                        | Model, firmware, serial number…                 |
| `state`                                                            | Latest values: battery, buttons, orientation…   |
| `set_led(color, pattern, group=, transition_ms=, duration_ms=)`    | Set the light                                   |
| `led_off()`                                                        | Turn every light off                            |
| `vibrate(pattern, duration_ms=)`                                   | Vibrate                                         |
| `wait_for_press(timeout)`, `wait_for_spell(timeout)`               | Wait for an event                               |
| `is_button_pressed()`, `get_press_duration()`, `was_double_press()` | Button state                                   |
| `start_imu()`, `stop_imu()`                                        | Turn motion data on/off                         |
| `reset_quaternions()`, `calibrate_imu()`, `calibrate_magnetometer()` | Orientation and sensor calibration            |
| `play_macro(macro, replace=)`, `stop_macro()`                      | On-wand sequences (Magic Caster)                |
| `get_button_thresholds()`, `set_button_thresholds()`, `calibrate_buttons()` | Touch pads (Magic Caster)              |
| `get_history(kind)`, `clear_history()`                             | Recent values with timestamps                   |

Every class and method has a docstring with details: try `help(wandpy.Wand)`.

## Examples

The [`examples/`](examples) folder has complete programs:

- [`basic_usage.py`](examples/basic_usage.py): scan, connect, lights, vibration,
  button and motion. Works with both wands.
- [`spells.py`](examples/spells.py): react to Magic Caster spells with on-wand
  light effects.
- [`wandview.py`](examples/wandview.py): live motion visualizer (needs
  `pip install pygame`).

## Troubleshooting

- **No wand found.** Make sure the wand is awake and close to the computer, and
  that no phone or other program is connected to it (a wand accepts a single
  connection). A Magic Caster wand may need to be put in pairing mode the
  first time.
- **Nothing happens on Linux.** Check that Bluetooth is on and the BlueZ
  service is running (`systemctl status bluetooth`).
- **Callbacks freeze the program.** Plain callbacks run inside the Bluetooth
  event handling: don't `time.sleep()` or block in them. Use an `async def`
  callback and `await asyncio.sleep()` instead.
- **See what is going on.** Turn on debug logs to see every Bluetooth packet:

  ```python
  import logging, wandpy
  wandpy.setup_logging(logging.DEBUG)
  ```

## Good to know

- **Magic Caster orientation.** This wand streams only raw gyroscope and
  accelerometer data. WandPy turns it into the same `QuaternionData` as the Kano
  wand with a Mahony filter. Pitch and roll are absolute. There is no
  magnetometer, so yaw is relative to the connection time (or the last
  `reset_quaternions()`) and slowly drifts. Gyroscope bias is learned while the
  wand rests.
- **Kano patterns on the Magic Caster.** `LedPattern` and `VibrationPattern` are
  played as on-wand macros. Continuous patterns loop 255 times, the longest a
  macro can express. Setting a light replaces the current effect.
- **Battery.** Motion data streams from the moment you connect. Pass
  `imu_streaming=False` (or call `stop_imu()`) when you only need buttons or
  spells.
- **Credits.** The Magic Caster protocol is reverse engineered. Thanks to
  [hass-magic-caster-wand](https://github.com/eigger/hass-magic-caster-wand),
  [magic-caster-wand-open-source-controller](https://github.com/oelison/magic-caster-wand-open-source-controller),
  [Magic-Caster-Wand-Open-app-ai](https://github.com/whymaxwhy/Magic-Caster-Wand-Open-app-ai)
  and [OpenMagicCasterWand](https://github.com/Dakewlmancoding/OpenMagicCasterWand).

## Upgrading from 1.x

- `scan()` now returns `WandInfo` named tuples
  `(address, name, service_uuids, wand_type, rssi, device)`. `devices[0][0]` is
  still the address, but unpack them by name rather than as 3-tuples.
- `Color.OFF` and `Color.BLACK` are now real colors (they were `None`).
- `wait_for_press()` no longer replaces your `on_button_event` callback while it
  waits.
