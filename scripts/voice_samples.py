"""Record wake-phrase voice samples for retraining the on-device wake model.

    ./.venv/bin/python scripts/voice_samples.py

A small tkinter window: type the phrase (default "Hey Ada"), press Record or
Space, say the phrase, and a fixed-length clip lands in
``data/voice_samples/<slug>/<NNNN>.wav`` (or ``<slug>/negative/<NNNN>.wav``
with the Negative toggle on, for background noise and other speech). Clips are
numbered sequentially and never overwritten.

Prior art, read rather than remembered:

* Mycroft Precise, ``precise/scripts/collect.py`` (MycroftAI/mycroft-precise,
  ``dev`` branch): the reference wake-word sample collector. Copied from it:
  the audio parameters (16000 Hz, 1 channel, 2-byte samples), Space as the
  record key, zero-padded sequential numbering, and ``next_name()`` -- the
  next filename is found by probing ``isfile`` and incrementing until a free
  slot exists, so a collector restarted into a half-filled directory never
  overwrites. Deviations, stated: Precise records until Space is pressed
  again; this records a fixed length instead, because openWakeWord's training
  stacks positives into equal-length rows and a fixed clip length keeps every
  take usable without trimming. Precise is terminal-only (PyAudio + termios);
  this is a tkinter window with a level meter, since the point of the meter
  is to make a silent take visible before it is counted.
* openWakeWord, ``openwakeword/data.py`` (dscripka/openWakeWord, ``main``):
  the training-input contract. ``filter_audio_paths`` states "Assumes that
  all wav files are sampled at 16khz, are single channel, and have 16-bit PCM
  data", ``convert_clips`` speaks of "single-channel, 16 khz clips", and
  ``load_audio_clips`` casts with ``(X*32767).astype(np.int16)``. Positives
  and negatives are separate directories, which is what the Negative toggle
  writes. Nothing in openWakeWord records from a microphone, so there was no
  collector there to copy.
* The sibling training project (``../ada-wake-training/corpus.py``,
  ``write``) that produced the shipped ``hey_ada.onnx`` writes exactly this
  format with the stdlib ``wave`` module; ``write_wav`` below is the same
  three ``set*`` calls so the recorded corpus and the synthetic one agree
  byte-for-byte in header.

Capture goes through ``sounddevice`` (PortAudio) rather than PyAudio because
it ships wheels for Apple Silicon and its callback hands over NumPy arrays,
which is what the level meter wants. The dependency is checked in ``main``,
in words, and never at import time, so the WAV writer can be tested without
a microphone or an audio library.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import wave
from pathlib import Path

SAMPLE_RATE = 16_000  # openwakeword/data.py: "sampled at 16khz"
CHANNELS = 1  # "single channel"
SAMPLE_WIDTH = 2  # "16-bit PCM"; precise/scripts/collect.py: width=2
DEFAULT_PHRASE = "Hey Ada"
DEFAULT_SECONDS = 2.0
MIN_SECONDS, MAX_SECONDS = 0.5, 10.0
NUMBER_WIDTH = 4  # NNNN.wav
SILENT_PEAK_DBFS = -40.0  # a take whose loudest sample is below this is called out
WINDOW_TITLE = "Ada voice samples"

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_ROOT = REPO_ROOT / "data" / "voice_samples"

INSTALL_HINT = "./.venv/bin/pip install sounddevice numpy"

_NAME_RE = re.compile(rf"^(\d{{{NUMBER_WIDTH}}})\.wav$")


# --- pure helpers (no audio library, testable without a microphone) ---------


def slugify(phrase: str) -> str:
    """"Hey Ada" -> "hey_ada". Empty input falls back to "phrase"."""
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.strip().lower()).strip("_")
    return slug or "phrase"


def clip_dir(phrase: str, negative: bool) -> Path:
    d = SAMPLES_ROOT / slugify(phrase)
    return d / "negative" if negative else d


def existing_numbers(directory: Path) -> list[int]:
    if not directory.is_dir():
        return []
    out = []
    for name in os.listdir(directory):
        m = _NAME_RE.match(name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def next_path(directory: Path) -> Path:
    """Next free NNNN.wav -- precise/scripts/collect.py ``next_name``: probe
    ``isfile`` and increment until a slot is free, so nothing is overwritten."""
    nums = existing_numbers(directory)
    n = (nums[-1] + 1) if nums else 0
    while True:
        candidate = directory / f"{n:0{NUMBER_WIDTH}d}.wav"
        if not candidate.is_file():
            return candidate
        n += 1


def last_path(directory: Path) -> Path | None:
    nums = existing_numbers(directory)
    if not nums:
        return None
    return directory / f"{nums[-1]:0{NUMBER_WIDTH}d}.wav"


def write_wav(path: Path, samples) -> Path:
    """Write int16 mono samples at 16 kHz. Same header as
    ada-wake-training/corpus.py ``write``; refuses any other dtype rather
    than silently rescaling, because a float clip written as int16 is noise."""
    import numpy as np

    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        raise TypeError(f"write_wav wants int16 samples, got {arr.dtype}")
    if arr.ndim == 2 and arr.shape[1] == 1:
        arr = arr[:, 0]
    if arr.ndim != 1:
        raise ValueError(f"write_wav wants mono samples, got shape {arr.shape}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(arr.tobytes())
    return path


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (
            CHANNELS,
            SAMPLE_WIDTH,
            SAMPLE_RATE,
        ):
            raise ValueError(f"{path} is not 16 kHz mono int16")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def peak_dbfs(samples) -> float:
    import numpy as np

    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        return -120.0
    peak = float(np.max(np.abs(arr))) / 32768.0
    return 20.0 * np.log10(peak) if peak > 0 else -120.0


def rms_dbfs(samples) -> float:
    import numpy as np

    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(arr * arr))) / 32768.0
    return 20.0 * np.log10(rms) if rms > 0 else -120.0


# --- capture ----------------------------------------------------------------


class Recorder:
    """Fixed-length capture on a sounddevice InputStream. The callback runs
    on PortAudio's thread and only appends; the GUI polls ``level`` and
    ``done``. ``take()`` returns the int16 samples once, then resets."""

    def __init__(self, sd, np):
        self._sd = sd
        self._np = np
        self._lock = threading.Lock()
        self._chunks: list = []
        self._want = 0
        self._have = 0
        self._stream = None
        self.level_dbfs = -120.0
        self.done = False
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self, seconds: float) -> None:
        if self._stream is not None:
            return
        self._chunks = []
        self._want = int(seconds * SAMPLE_RATE)
        self._have = 0
        self.done = False
        self.error = None
        self._stream = self._sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=1024,  # precise/scripts/collect.py chunk_size
            callback=self._on_audio,
        )
        self._stream.start()

    def _on_audio(self, indata, frames, _time, status) -> None:
        if status:
            self.error = str(status)
        block = indata[:, 0].copy()
        with self._lock:
            need = self._want - self._have
            if need <= 0:
                return
            block = block[:need]
            self._chunks.append(block)
            self._have += len(block)
            self.level_dbfs = rms_dbfs(block)
            if self._have >= self._want:
                self.done = True

    def take(self):
        """Stop the stream and return the samples (None if nothing ran)."""
        if self._stream is None:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        with self._lock:
            chunks, self._chunks = self._chunks, []
        self.level_dbfs = -120.0
        if not chunks:
            return self._np.zeros(0, dtype=self._np.int16)
        return self._np.concatenate(chunks)

    def play(self, samples) -> None:
        self._sd.play(samples, SAMPLE_RATE)


# --- GUI --------------------------------------------------------------------


def build_app(tk, ttk, sd, np):
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.resizable(False, False)

    phrase_var = tk.StringVar(value=DEFAULT_PHRASE)
    seconds_var = tk.StringVar(value=f"{DEFAULT_SECONDS:.1f}")
    negative_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="Ready. Space records, Backspace deletes last.")
    count_var = tk.StringVar()
    level_var = tk.DoubleVar(value=0.0)
    level_text = tk.StringVar(value="level: silent")

    recorder = Recorder(sd, np)
    last_clip: dict = {"samples": None}

    frame = ttk.Frame(root, padding=12)
    frame.grid(sticky="nsew")

    ttk.Label(frame, text="Phrase").grid(row=0, column=0, sticky="w")
    phrase_entry = ttk.Entry(frame, textvariable=phrase_var, width=28)
    phrase_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)

    ttk.Label(frame, text="Clip length (s)").grid(row=1, column=0, sticky="w")
    seconds_spin = ttk.Spinbox(
        frame,
        from_=MIN_SECONDS,
        to=MAX_SECONDS,
        increment=0.5,
        textvariable=seconds_var,
        width=6,
    )
    seconds_spin.grid(row=1, column=1, sticky="w", pady=2)

    negative_check = ttk.Checkbutton(
        frame, text="Negative (background / other speech)", variable=negative_var
    )
    negative_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)

    ttk.Label(frame, textvariable=count_var).grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(6, 2)
    )

    meter = ttk.Progressbar(frame, variable=level_var, maximum=60.0, length=300)
    meter.grid(row=4, column=0, columnspan=3, sticky="ew", pady=2)
    ttk.Label(frame, textvariable=level_text).grid(
        row=5, column=0, columnspan=3, sticky="w"
    )

    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=3, pady=(8, 2))
    record_btn = ttk.Button(buttons, text="Record  [Space]")
    play_btn = ttk.Button(buttons, text="Play last", state="disabled")
    delete_btn = ttk.Button(buttons, text="Delete last  [Backspace]")
    record_btn.grid(row=0, column=0, padx=4)
    play_btn.grid(row=0, column=1, padx=4)
    delete_btn.grid(row=0, column=2, padx=4)

    status = ttk.Label(frame, textvariable=status_var, wraplength=340, justify="left")
    status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def current_dir() -> Path:
        return clip_dir(phrase_var.get(), negative_var.get())

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)

    def refresh_count(*_):
        d = current_dir()
        n = len(existing_numbers(d))
        kind = "negative clips" if negative_var.get() else "clips"
        phrase = phrase_var.get().strip() or DEFAULT_PHRASE
        count_var.set(f"{n} {kind} for '{phrase}'  ->  {rel(d)}/")
        playable = last_clip["samples"] is not None or n
        play_btn.configure(state="normal" if playable else "disabled")

    def seconds() -> float:
        try:
            s = float(seconds_var.get())
        except ValueError:
            s = DEFAULT_SECONDS
        s = min(MAX_SECONDS, max(MIN_SECONDS, s))
        seconds_var.set(f"{s:.1f}")
        return s

    def set_recording_ui(on: bool):
        state = "disabled" if on else "normal"
        for w in (record_btn, delete_btn, phrase_entry, seconds_spin, negative_check):
            w.configure(state=state)
        play_btn.configure(state="disabled" if on else play_btn.cget("state"))
        record_btn.configure(text="Recording…" if on else "Record  [Space]")

    def start_record(*_):
        if recorder.active:
            return
        s = seconds()
        try:
            recorder.start(s)
        except Exception as exc:  # PortAudio errors are strings, not a taxonomy
            status_var.set(f"Could not open the microphone: {exc}")
            return
        set_recording_ui(True)
        status_var.set(f"Recording {s:.1f} s — say \"{phrase_var.get().strip()}\" now.")
        root.after(50, poll)

    def poll():
        if not recorder.active:
            return
        db = recorder.level_dbfs
        level_var.set(max(0.0, 60.0 + db))
        level_text.set(f"level: {db:6.1f} dBFS" if db > -119 else "level: silent")
        if recorder.done:
            finish_record()
        else:
            root.after(50, poll)

    def finish_record():
        samples = recorder.take()
        set_recording_ui(False)
        level_var.set(0.0)
        level_text.set("level: silent")
        if recorder.error:
            status_var.set(f"Stream reported: {recorder.error}. Take discarded.")
            refresh_count()
            return
        if samples is None or samples.size == 0:
            status_var.set("Nothing captured. Take discarded.")
            refresh_count()
            return
        path = write_wav(next_path(current_dir()), samples)
        last_clip["samples"] = samples
        peak = peak_dbfs(samples)
        msg = f"Saved {rel(path)} (peak {peak:.1f} dBFS)."
        if peak < SILENT_PEAK_DBFS:
            msg += " That sounds like silence — Backspace deletes it."
        status_var.set(msg)
        refresh_count()

    def play_last(*_):
        if recorder.active:
            return
        samples = last_clip["samples"]
        src = "last take"
        if samples is None:
            p = last_path(current_dir())
            if p is None:
                status_var.set("Nothing to play.")
                return
            samples = read_wav(p)
            src = rel(p)
        try:
            recorder.play(samples)
        except Exception as exc:
            status_var.set(f"Could not play: {exc}")
            return
        status_var.set(f"Playing {src}.")

    def delete_last(*_):
        if recorder.active:
            return
        p = last_path(current_dir())
        if p is None:
            status_var.set(f"Nothing to delete in {rel(current_dir())}/.")
            return
        p.unlink()
        last_clip["samples"] = None
        status_var.set(f"Deleted {rel(p)}.")
        refresh_count()

    record_btn.configure(command=start_record)
    play_btn.configure(command=play_last)
    delete_btn.configure(command=delete_last)

    def typing(event) -> bool:
        return isinstance(event.widget, (ttk.Entry, ttk.Spinbox, tk.Entry, tk.Spinbox))

    def on_space(event):
        if typing(event):
            return None
        start_record()
        return "break"

    def on_backspace(event):
        if typing(event):
            return None
        delete_last()
        return "break"

    root.bind_all("<space>", on_space)
    root.bind_all("<BackSpace>", on_backspace)
    root.bind("<Escape>", lambda e: root.destroy())
    phrase_var.trace_add("write", refresh_count)
    negative_var.trace_add("write", refresh_count)

    def on_close():
        if recorder.active:
            recorder.take()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_count()
    record_btn.focus_set()  # so Space records rather than typing into the phrase
    return root


def main() -> int:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        print(f"voice_samples: missing audio dependency ({exc.name}).", file=sys.stderr)
        print(f"Install it with:\n    {INSTALL_HINT}", file=sys.stderr)
        return 2
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print(
            "voice_samples: this Python has no tkinter; use a python.org or Homebrew "
            "python built with Tk.",
            file=sys.stderr,
        )
        return 2
    try:
        sd.check_input_settings(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16"
        )
    except Exception as exc:
        print(
            f"voice_samples: no input device accepts 16 kHz mono int16 ({exc}). "
            "Check System Settings > Sound > Input, and the microphone permission "
            "for your terminal.",
            file=sys.stderr,
        )
        return 1
    root = build_app(tk, ttk, sd, np)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
