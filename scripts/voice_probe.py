"""Probe Ada's voice path line by line: record, transcribe, show what she would do.

    ./.venv/bin/python scripts/voice_probe.py        # engine must be up on :8081

A tkinter window with a script of lines to read. Press Space, say the
highlighted line (pausing the way you naturally would), press Space again. The
clip goes to the engine's real ``POST /transcribe`` -- the same route and model
the overlay uses after "hey Ada" -- and the window shows:

* the transcript, exactly as the engine returned it;
* what follows the wake word, the way ``matchWakeWord`` in
  ``app/src/lib/wake-word.ts`` splits it;
* what the overlay would do with it while the chosen stage is waiting: a step
  command (approve / restart / later) or chat with Ada;
* optionally, Ada's actual chat reply (``/chat/stream`` with
  ``confirm_before_build``, so nothing is ever built from here).

Every take lands in ``data/voice_probe/<session>/NNNN.wav`` with one JSON line
per take in ``results.jsonl`` beside it (expected line, transcript, decision),
so a bad transcript can be replayed and diffed rather than remembered.

The recorder, WAV format and file numbering come from ``scripts/voice_samples.py``
(itself after Mycroft Precise's ``collect.py``); this adds start/stop capture
instead of a fixed clip length, because a command clip is as long as the
sentence. The command vocabulary below mirrors ``STEP_WORDS``, ``GO_WORDS`` and
``RESTART_PHRASES`` in ``app/src/lib/silkscreen/steps.ts``; if the two ever
disagree, ``steps.ts`` is what the app runs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_samples import (  # noqa: E402
    CHANNELS,
    INSTALL_HINT,
    REPO_ROOT,
    SAMPLE_RATE,
    Recorder,
    next_path,
    peak_dbfs,
    write_wav,
)

WINDOW_TITLE = "Ada voice probe"
MAX_TAKE_S = 10.0
BASE_URL = os.getenv("SILKSCREEN_URL", "http://127.0.0.1:8081").rstrip("/")
PROBE_ROOT = REPO_ROOT / "data" / "voice_probe"

#: The script. "…" marks where to pause, which is the case the overlay got
#: wrong in the 2026-09-14 recording ("hey ada … can you place it").
SCRIPT: list[str] = [
    "Hey Ada, can you hear me?",
    "Hey Ada … can you place it?",
    "Hey Ada, place it.",
    "Hey Ada … looks good, go ahead.",
    "Hey Ada, route it.",
    "Hey Ada, run the review.",
    "Hey Ada, source the parts.",
    "Hey Ada … start over.",
    "Hey Ada, make me a 3.3 volt LDO board powered from USB-C.",
    "Hey Ada, an ESP32 board with an AMS1117 regulator and a USB-C connector.",
    "Hey Ada, what does a decoupling capacitor do?",
    "Hey Ada, never mind.",
]

STAGES = ["(nothing waiting)", "place", "route", "review", "sourcing", "order", "case"]

# --- mirrors of app/src/lib (keep in step with the TypeScript) --------------

STEP_ORDER = ["propose", "place", "route", "review", "sourcing", "order", "case"]
STEP_WORDS = {
    "propose": ["propose", "schematic", "circuit"],
    "place": ["place", "placement", "placed", "layout"],
    "route": ["route", "routing", "copper", "traces", "tracks"],
    "review": ["review", "critique", "check", "findings"],
    "sourcing": [
        "source",
        "sourcing",
        "bom",
        "parts",
        "mpn",
        "datasheets",
        "datasheet",
    ],
    "order": ["order", "fab", "fabricate", "manufacture", "pcbway", "jlc", "jlcpcb"],
    "case": ["case", "enclosure", "housing", "box", "cad", "stp"],
}
GO_WORDS = {
    "go",
    "next",
    "continue",
    "proceed",
    "approve",
    "approved",
    "ok",
    "okay",
    "yes",
    "yep",
    "yeah",
    "sg",
    "lgtm",
    "ship",
    "do",
    "it",
    "sure",
}
RESTART_PHRASES = [
    "restart",
    "start over",
    "start again",
    "start fresh",
    "from scratch",
    "new board",
    "new run",
    "run it again",
    "do it again",
    "over again",
    "scrap",
]
WAKE_ALIASES = {
    "ada",
    "aida",
    "ayda",
    "adah",
    "adda",
    "eda",
    "ida",
    "oda",
    "odda",
    "otto",
    "auto",
    "aider",
    "heyada",
    "heyaida",
    "heyadda",
    "heyotto",
}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _said(words: list[str], phrase: str) -> bool:
    p = phrase.split()
    return any(words[i : i + len(p)] == p for i in range(len(words) - len(p) + 1))


def after_wake(transcript: str) -> str | None:
    """What follows the wake word, or None when no wake word was heard."""
    parts = transcript.split()
    for i, raw in enumerate(parts):
        token = re.sub(r"[^a-z]", "", raw.lower())
        if token in WAKE_ALIASES or (3 <= len(token) <= 4 and _near_ada(token)):
            return " ".join(parts[i + 1 :]).lstrip(" ,.;:!?-").strip()
    return None


def _near_ada(token: str) -> bool:
    if token in {"adam"}:
        return False
    # one edit from "ada"
    a, b = token, "ada"
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(
                prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1])
            )
        prev = cur
    return prev[-1] <= 1


def overlay_decision(utterance: str, waiting: str | None) -> str:
    """``interpretCommand`` plus the page's routing, as one sentence."""
    words = _words(utterance)
    available = [waiting] if waiting else []
    if not words:
        return "nothing said: the overlay lists what you could say"
    if any(_said(words, p) for p in RESTART_PHRASES):
        return "STEP COMMAND: restart (asks you to confirm)"
    for step in STEP_ORDER:
        if step in available and any(_said(words, p) for p in STEP_WORDS[step]):
            return f"STEP COMMAND: approve {step} (arms it, asks you to confirm)"
    for step in STEP_ORDER:
        if any(_said(words, p) for p in STEP_WORDS[step]):
            return f"STEP COMMAND: later — '{step}' is not the stage waiting"
    if all(w in GO_WORDS or w in {"lets", "let", "s"} for w in words):
        return (
            f"STEP COMMAND: approve {available[0]}"
            if available
            else "nothing to agree to"
        )
    return "CHAT: goes to Ada's chat agent (she answers, or proposes a board)"


# --- engine calls -------------------------------------------------------------


def _token() -> str:
    token = os.getenv("SILKSCREEN_ACCESS_TOKEN", "")
    env = REPO_ROOT / ".env"
    if not token and env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("SILKSCREEN_ACCESS_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("'\"")
    return token


def _post(path: str, body: dict, timeout: float = 60.0) -> bytes:
    headers = {"Content-Type": "application/json"}
    if token := _token():
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"engine not reachable at {BASE_URL}: {exc.reason}"
        ) from None


def transcribe(wav: Path, peak_128: float) -> dict:
    body = {
        "audio_b64": base64.b64encode(wav.read_bytes()).decode(),
        "mime_type": "audio/wav",
        "peak": round(min(128.0, peak_128), 2),
    }
    return json.loads(_post("/transcribe", body))


def ask_ada(text: str) -> dict:
    raw = _post("/chat/stream", {"intent": text, "confirm_before_build": True}, 120.0)
    for line in raw.decode(errors="replace").splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("event") == "chat.done":
            return {
                "assistant": frame.get("assistant"),
                "proposal": frame.get("proposal"),
            }
        if frame.get("event") == "chat.error":
            return {"error": frame.get("error")}
    return {"error": "stream ended without an answer"}


# --- GUI ----------------------------------------------------------------------


def build_app(tk, ttk, sd, np):
    session = PROBE_ROOT / time.strftime("%Y%m%d-%H%M%S")
    results = session / "results.jsonl"

    root = tk.Tk()
    root.title(WINDOW_TITLE)
    frame = ttk.Frame(root, padding=12)
    frame.grid(sticky="nsew")

    ttk.Label(frame, text="Script — say the highlighted line").grid(
        row=0, column=0, sticky="w"
    )
    script_box = tk.Listbox(frame, height=len(SCRIPT), width=64, exportselection=False)
    for line in SCRIPT:
        script_box.insert("end", line)
    script_box.selection_set(0)
    script_box.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)

    stage_var = tk.StringVar(value="place")
    ask_var = tk.BooleanVar(value=True)
    ttk.Label(frame, text="Stage waiting in the overlay").grid(
        row=2, column=0, sticky="w"
    )
    ttk.Combobox(
        frame, textvariable=stage_var, values=STAGES, state="readonly", width=18
    ).grid(row=2, column=1, sticky="w")
    ttk.Checkbutton(frame, text="Also ask Ada when it's chat", variable=ask_var).grid(
        row=3, column=0, columnspan=3, sticky="w", pady=2
    )

    level_var = tk.DoubleVar(value=0.0)
    ttk.Progressbar(frame, variable=level_var, maximum=60.0, length=420).grid(
        row=4, column=0, columnspan=3, sticky="ew", pady=4
    )
    record_btn = ttk.Button(frame, text="Record  [Space]")
    record_btn.grid(row=5, column=0, sticky="w")

    out = tk.Text(frame, width=72, height=12, wrap="word")
    out.grid(row=6, column=0, columnspan=3, pady=(8, 0))
    status_var = tk.StringVar(
        value=f"Engine: {BASE_URL}. Takes save to {session.relative_to(REPO_ROOT)}/"
    )
    ttk.Label(frame, textvariable=status_var, wraplength=520).grid(
        row=7, column=0, columnspan=3, sticky="w", pady=(6, 0)
    )

    recorder = Recorder(sd, np)
    state = {"busy": False}

    def show(text: str) -> None:
        out.delete("1.0", "end")
        out.insert("end", text)

    def current_line() -> tuple[int, str]:
        sel = script_box.curselection()
        i = sel[0] if sel else 0
        return i, SCRIPT[i]

    def poll() -> None:
        if not recorder.active:
            return
        level_var.set(max(0.0, 60.0 + recorder.level_dbfs))
        if recorder.done:
            stop()
        else:
            root.after(50, poll)

    def toggle(*_):
        if state["busy"]:
            return "break"
        if recorder.active:
            stop()
        else:
            try:
                recorder.start(MAX_TAKE_S)
            except Exception as exc:
                status_var.set(f"Could not open the microphone: {exc}")
                return "break"
            record_btn.configure(text="Stop  [Space]")
            status_var.set(
                f"Recording (up to {MAX_TAKE_S:.0f} s). Space again to stop."
            )
            root.after(50, poll)
        return "break"

    def stop() -> None:
        samples = recorder.take()
        record_btn.configure(text="Record  [Space]")
        level_var.set(0.0)
        if samples is None or samples.size == 0:
            status_var.set("Nothing captured.")
            return
        index, expected = current_line()
        wav = write_wav(next_path(session), samples)
        seconds = samples.size / SAMPLE_RATE
        peak_128 = float(np.max(np.abs(samples.astype(np.int32)))) / 32768.0 * 128.0
        state["busy"] = True
        status_var.set(f"Transcribing {wav.name} ({seconds:.1f} s)…")
        waiting = None if stage_var.get().startswith("(") else stage_var.get()
        threading.Thread(
            target=work,
            args=(
                wav,
                expected,
                seconds,
                peak_128,
                peak_dbfs(samples),
                waiting,
                ask_var.get(),
                index,
            ),
            daemon=True,
        ).start()

    def work(wav, expected, seconds, peak_128, dbfs, waiting, ask, index) -> None:
        record = {
            "clip": str(wav.relative_to(REPO_ROOT)),
            "expected": expected,
            "seconds": round(seconds, 2),
            "peak_dbfs": round(dbfs, 1),
            "stage_waiting": waiting,
        }
        try:
            got = transcribe(wav, peak_128)
            transcript = str(got.get("text", ""))
            record.update(transcript=transcript, model=got.get("model"))
            tail = after_wake(transcript)
            record["after_wake"] = tail
            if tail is None:
                decision = (
                    "NO WAKE WORD in the transcript: the overlay would ignore this"
                )
            else:
                decision = overlay_decision(tail, waiting)
            record["decision"] = decision
            if ask and tail is not None and decision.startswith("CHAT"):
                record["ada"] = ask_ada(tail or transcript)
        except Exception as exc:
            record["error"] = str(exc)
        session.mkdir(parents=True, exist_ok=True)
        with results.open("a") as f:
            f.write(json.dumps(record) + "\n")
        root.after(0, lambda: finish(record, index))

    def finish(record: dict, index: int) -> None:
        state["busy"] = False
        lines = [
            f"expected:    {record['expected']}",
            f"clip:        {record['clip']}  "
            f"({record['seconds']} s, peak {record['peak_dbfs']} dBFS)",
        ]
        if "error" in record:
            lines.append(f"ERROR:       {record['error']}")
        else:
            lines += [
                f"transcript:  {record.get('transcript')!r}   [{record.get('model')}]",
                f"after wake:  {record.get('after_wake')!r}",
                f"overlay:     {record.get('decision')}",
            ]
            if "ada" in record:
                ada = record["ada"]
                lines.append(f"Ada says:    {ada.get('assistant') or ada.get('error')}")
                if ada.get("proposal"):
                    lines.append(f"proposal:    {ada['proposal']}  (nothing built)")
        show("\n".join(lines))
        status_var.set("Saved. Space records the next line.")
        if index + 1 < len(SCRIPT):
            script_box.selection_clear(0, "end")
            script_box.selection_set(index + 1)
            script_box.see(index + 1)

    record_btn.configure(command=toggle)
    root.bind_all("<space>", toggle)
    root.bind("<Escape>", lambda e: root.destroy())
    root.protocol("WM_DELETE_WINDOW", lambda: (recorder.take(), root.destroy()))
    record_btn.focus_set()
    return root


def main() -> int:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        print(
            f"voice_probe: missing audio dependency ({exc.name}).\n    {INSTALL_HINT}",
            file=sys.stderr,
        )
        return 2
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("voice_probe: this Python has no tkinter.", file=sys.stderr)
        return 2
    try:
        sd.check_input_settings(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16"
        )
    except Exception as exc:
        print(f"voice_probe: no usable microphone ({exc}).", file=sys.stderr)
        return 1
    build_app(tk, ttk, sd, np).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
