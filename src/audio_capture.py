"""Capture the mix played by one Windows output device via WASAPI loopback."""

from __future__ import annotations

from collections import deque
from math import ceil

import numpy as np
import soundcard as sc

SAMPLE_RATE = 16000
FRAME_SAMPLES = 3200  # 200 ms
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


def output_devices() -> list[tuple[str, str]]:
    return [(speaker.name, speaker.id) for speaker in sc.all_speakers()]


def default_output_id() -> str | None:
    speaker = sc.default_speaker()
    return None if speaker is None else speaker.id


def listen(output_id: str, threshold: float, silence_seconds: float, stop_event, on_level, on_utterance) -> None:
    loopback = sc.get_microphone(id=output_id, include_loopback=True)
    if loopback is None:
        raise RuntimeError("无法打开该播放设备的系统声音回录")

    pre_roll: deque[np.ndarray] = deque(maxlen=2)
    frames: list[np.ndarray] = []
    voiced_frames = 0
    quiet_frames = 0
    quiet_limit = max(1, ceil(silence_seconds / FRAME_SECONDS))
    max_frames = int(60 / FRAME_SECONDS)

    def emit() -> None:
        nonlocal frames, voiced_frames, quiet_frames
        if voiced_frames >= 3 and frames:
            on_utterance(np.concatenate(frames).astype(np.float32, copy=False))
        frames = []
        voiced_frames = 0
        quiet_frames = 0

    try:
        with loopback.recorder(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES) as recorder:
            while not stop_event.is_set():
                block = recorder.record(numframes=FRAME_SAMPLES)
                if block.ndim == 2:
                    mono = block.mean(axis=1).astype(np.float32)
                else:
                    mono = block.astype(np.float32)
                level = float(np.sqrt(np.mean(np.square(mono))))
                on_level(level)
                speaking = level >= threshold

                if not frames:
                    if speaking:
                        frames = [*pre_roll, mono]
                        voiced_frames = 1
                    else:
                        pre_roll.append(mono)
                    continue

                frames.append(mono)
                if speaking:
                    voiced_frames += 1
                    quiet_frames = 0
                else:
                    quiet_frames += 1

                if quiet_frames >= quiet_limit or len(frames) >= max_frames:
                    emit()
                    pre_roll.clear()
    finally:
        # Stop accepting new sound, but keep the spoken fragment already captured.
        emit()
