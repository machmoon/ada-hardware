"""Two people editing one KiCad board at the same time.

Each engineer runs a seat beside their own KiCad (``python -m kicadcollab
join``); both seats talk to one relay (``python -m kicadcollab relay``). A seat
polls its open board through KiCad's IPC API, sends what changed, and applies
what the other seat changed, one KiCad commit (one undo step) per batch. Which
copy wins is Excalidraw's per-element rule (``merge.py``). See ``docs/collab.md``.
"""
