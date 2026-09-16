"""KiCad library symbols for the connector packages the engine draws itself.

A connector's land pattern here is the engine's own (``footprints.py``), but
its schematic glyph need not be: KiCad ships the symbols, and their pin
numbers are the same names these land patterns use -- ``A9``/``B9`` VBUS and
``A12``/``B12`` GND on ``Connector:USB_C_Receptacle_PowerOnly_6P``, ``1..N``
on ``Connector_Generic:Conn_01xNN``. KiCad's USB-C symbol stacks its four GND
contacts as one pin, which is exactly the tie ``board.tie_package_pins`` makes.

One table, read by the schematic (which symbol to draw) and the board (which
pin names KiCad's ``unconnected-(...)`` nets are built from), so the two
cannot disagree. Left out on purpose: the barrel jack (KiCad's
``Barrel_Jack_Switch`` has unnamed pins whose order has not been checked
against this land pattern) and the battery holders and switch, whose
engine glyphs are already the conventional ones.
"""

from __future__ import annotations

__all__ = ["PACKAGE_SYMBOLS", "package_pads"]

PACKAGE_SYMBOLS: dict[str, str] = {
    "USB_C_Receptacle_Power": "Connector:USB_C_Receptacle_PowerOnly_6P",
    "USB_C_Receptacle_USB2.0_16P": "Connector:USB_C_Receptacle_USB2.0_16P",
    "TerminalBlock_2P_5.08mm": "Connector:Screw_Terminal_01x02",
    "TestPoint_Pad_1.5x1.5mm": "Connector:TestPoint",
    **{f"JST_PH_{n}P": f"Connector_Generic:Conn_01x{n:02d}" for n in (2, 3, 4)},
    **{
        f"PinHeader_1x{n:02d}_P2.54mm": f"Connector_Generic:Conn_01x{n:02d}"
        for n in (2, 3, 4, 5, 6, 8, 10)
    },
}


def package_pads(package: str) -> frozenset[str] | None:
    """The pad numbers the engine's land pattern for ``package`` has."""
    from ..footprints import (
        _BATTERY_BUILDERS,
        _CONNECTOR_BUILDERS,
        _SWITCH_BUILDERS,
        _TESTPOINT_BUILDERS,
    )

    for table in (
        _CONNECTOR_BUILDERS,
        _BATTERY_BUILDERS,
        _SWITCH_BUILDERS,
        _TESTPOINT_BUILDERS,
    ):
        if package in table:
            return frozenset(p.number for p in table[package](None).pads)
    return None
