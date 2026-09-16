# Ada voice samples

Records wake-phrase clips for retraining `app/src-tauri/resources/wake/hey_ada.onnx`.

    ./.venv/bin/pip install sounddevice numpy   # once; the script names this line itself if missing
    ./.venv/bin/python scripts/voice_samples.py

Space (or Record) captures a 2.0 s clip — 16 kHz mono int16 WAV, openWakeWord's input format — into
`data/voice_samples/<phrase-slug>/NNNN.wav`; tick Negative to save background/other speech into `<slug>/negative/`.
Backspace deletes the last clip (it says which), Play last replays it, and the status line flags a take whose peak
is under -40 dBFS as probable silence. Esc quits. Prior art and format citations are in the module docstring.
