# Two people, one board (`kicadcollab/`)

Two engineers edit the same KiCad board at the same time, each in their own
KiCad: a part one of them moves, a track one of them lays, a via one of them
deletes appears in the other's open board within a quarter of a second, as one
undo step. Nothing is saved for either of them; saving stays their decision.

**Unverified live.** The merge, the relay and the sync loop are tested with two
seats and a real relay over loopback, but the KiCad side (`kipy_board.py`) was
written against kicad-python 0.8.0's source and has not yet run against a KiCad.
Its protobuf round trip was checked in `.venv-kicad` without one.

## Run it

Both people open **the same board file** in KiCad 9 or newer, with
*Preferences > Plugins > Enable API server* on. One machine runs the relay:

```bash
python -m kicadcollab relay                                  # loopback only, no token
KICADCOLLAB_TOKEN=... python -m kicadcollab relay --host 0.0.0.0   # reachable; a token is required
```

Each person runs a seat beside their KiCad, in the bridge venv (kicad-python
pins `protobuf<6`, so it cannot share the engine's venv; see `desktop/README.md`):

```bash
KICADCOLLAB_TOKEN=... .venv-kicad/bin/python -m kicadcollab join \
    --room demo --seat ana --relay relay-host:8797
```

The seat prints who joined and left and how many changes it applied from whom.

## What travels, and what does not

| Synced | How |
|---|---|
| Footprints | the whole `FootprintInstance` protobuf: position, rotation, side, fields |
| Tracks, arc tracks, vias | the whole protobuf: geometry, width, layer, net |

Not yet: zones (a refill rewrites a zone's payload after every nearby edit and
would flood the room; it needs its own rule), graphics, text, dimensions, the
schematic, board setup and design rules. Cursors and "who has what selected"
are not shown in KiCad.

## How it decides

- **Changes are found by polling.** KiCad's IPC API has no change notification
  (kicad-python 0.8.0: request/response only), so each seat snapshots the board
  every `--poll` seconds (0.25 by default) and compares.
- **Which copy wins** is Excalidraw's rule (`packages/excalidraw/data/reconcile.ts`
  at `6c90855`, MIT): higher version wins; a tie goes to the lower random nonce,
  so both seats pick the same winner without talking; a deletion is a tombstone
  ordered by the same rule.
- **What you have selected is not overwritten.** Excalidraw drops a remote edit
  to an element being edited; here it is parked and applied when you let go of
  the part, if it still wins.
- **Opening the same file sends nothing.** Every item starts at version 0 on
  both sides, so only real edits travel.
- **A late joiner is caught up** from the relay's copy of the room.
- **The relay refuses in words** a seat that opened a different board file, a
  wrong token, or a seat name already in the room, and will not start on a
  non-loopback address without a token.

The relay protocol is one JSON object per line (`kicadcollab/wire.py`), shaped
on excalidraw-room's room broadcast (`src/index.ts` at `03ff435`, MIT). Unlike
Excalidraw there is no end-to-end encryption: run the relay on a machine one of
you controls.

## Prior art checked

No open-source real-time KiCad or PCB co-editor was found (GitHub searches for
KiCad collaborative, multiplayer, realtime, CRDT and Yjs on 2026-09-27); KiCad
itself has none, and PCBHub offers review comments rather than live editing.
The design therefore takes its merge and relay from Excalidraw, which does ship
live co-editing, and its KiCad access from `desktop/kicad_live.py`.
