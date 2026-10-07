"""
Magic Caster Wand example: react to spells with on-wand light/haptic macros.

Grip all four touch pads, draw a spell, and release.
"""

import asyncio

from wandpy import Color, LedGroup, Macro, MagicCasterWand

EFFECTS = {
    "lumos": Macro().led(Color.WHITE, LedGroup.ALL, transition_ms=300),
    "nox": Macro().led(Color.OFF, LedGroup.ALL, transition_ms=500),
    "incendio": Macro()
    .buzz(200)
    .repeat(3, Macro().led(Color.ORANGE, LedGroup.TIP, 100).delay(100).led(Color.RED, LedGroup.TIP, 100).delay(100))
    .clear(),
}


async def main():
    async with MagicCasterWand() as wand:  # Nearest Magic Caster wand
        print(f"Connected to {wand.info.model} wand, battery {wand.state.battery}%")

        while True:
            spell = await wand.wait_for_spell()
            print(f"Cast {spell.name}")
            effect = EFFECTS.get(spell.key)
            if effect is not None:
                await wand.play_macro(effect, replace=True)
            else:
                await wand.vibrate(duration_ms=150)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
