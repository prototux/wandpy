"""
Spells recognized by the wand itself.

The Magic Caster wand recognizes spell gestures on-device (hold the wand with
all four pads pressed, draw the spell, release) and reports the spell name.

    >>> def on_spell(spell):
    ...     print(f"Cast {spell.name}!")
    ...     if spell.matches("lumos"):
    ...         ...
    >>> wand.on_spell = on_spell
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional


def normalize_spell_name(name: str) -> str:
    """Return a comparison key: lowercase words joined by underscores."""
    return "_".join(re.split(r"[\s_\-]+", name.strip().lower())).strip("_")


@dataclass(frozen=True)
class Spell:
    """
    A spell cast with the wand.

    Attributes:
        name: Human readable name, e.g. "Expecto Patronum"
        raw_name: Name exactly as sent by the wand, e.g. "Expecto_Patronum"
        timestamp: When the spell was received (time.time())
        raw_bytes: Original packet for advanced use/debugging
    """

    name: str
    raw_name: str
    timestamp: float = field(default_factory=time.time)
    raw_bytes: bytes = field(default=b"", repr=False)

    @property
    def key(self) -> str:
        """Normalized name for comparisons, e.g. 'expecto_patronum'."""
        return normalize_spell_name(self.raw_name)

    def matches(self, name: str) -> bool:
        """Case/space/underscore-insensitive comparison: spell.matches('Lumos Maxima')."""
        return self.key == normalize_spell_name(name)

    @classmethod
    def parse(cls, data: bytes) -> Optional["Spell"]:
        """
        Parse a Magic Caster spell packet.

        Format: [0x24][2 unknown bytes][name length][name (ASCII)...]
        Returns None for packets without a usable name.
        """
        if len(data) < 5:
            return None
        length = data[3]
        raw = bytes(data[4 : 4 + length]).decode("utf-8", errors="ignore").replace("\x00", "").strip()
        if not raw or not any(c.isalpha() for c in raw):
            return None
        name = " ".join(raw.replace("_", " ").split())
        return cls(name=name, raw_name=raw, raw_bytes=bytes(data))

    def __str__(self) -> str:
        return self.name
