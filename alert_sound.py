"""
alert_sound.py
==============
Cross-device audio alerts using pygame — works through your actual sound card,
unlike winsound.Beep() which routes to the PC speaker (silent on most modern PCs).

Four escalation levels with distinct tone patterns:
  Level 0 — NEW ALERT     : 1 soft beep  (440 Hz) — something to watch
  Level 1 — MOVING >2%    : 2 beeps      (660 Hz) — it's moving
  Level 2 — HOT >5%       : 3 sharp beeps(880 Hz) — get on it
  Level 3 — ROCKET >10%   : 4 rapid high (1100 Hz)— act NOW

Usage:
    from alert_sound import play_alert, init_audio
    init_audio()           # call once at startup
    play_alert(level=0)    # non-blocking, plays in background thread
"""

import threading
import time
import os
import logging

logger = logging.getLogger(__name__)

# ── pygame init ───────────────────────────────────────────────────────────────
try:
    import pygame
    import numpy as np
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

_audio_ready = False


def init_audio() -> bool:
    """Call once at startup. Returns True if audio is available."""
    global _audio_ready
    if not PYGAME_AVAILABLE:
        logger.warning("alert_sound: pygame not installed — run: pip install pygame numpy")
        return False
    try:
        pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=512)
        pygame.mixer.init()
        _audio_ready = True
        logger.info("alert_sound: pygame audio ready")
        return True
    except Exception as e:
        logger.warning(f"alert_sound: pygame init failed — {e}")
        return False


def _generate_tone(frequency: int, duration_ms: int, volume: float = 0.6) -> "pygame.mixer.Sound":
    """Generate a sine-wave tone as a pygame Sound object."""
    sample_rate = 44100
    n_samples   = int(sample_rate * duration_ms / 1000)
    t           = np.linspace(0, duration_ms / 1000, n_samples, endpoint=False)
    # Sine wave with short attack/decay envelope to avoid clicks
    wave        = np.sin(2 * np.pi * frequency * t)
    envelope    = np.ones(n_samples)
    attack      = min(int(sample_rate * 0.01), n_samples // 4)
    decay       = min(int(sample_rate * 0.05), n_samples // 4)
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[-decay:] = np.linspace(1, 0, decay)
    wave        = (wave * envelope * volume * 32767).astype(np.int16)
    sound       = pygame.sndarray.make_sound(wave)
    return sound


# Tone patterns per escalation level: list of (frequency_hz, duration_ms, gap_ms)
_PATTERNS = {
    0: [(440,  300, 0)],                                          # 1 soft beep
    1: [(660,  250, 80), (660,  250, 0)],                         # 2 medium beeps
    2: [(880,  220, 60), (880,  220, 60), (880,  220, 0)],        # 3 sharp beeps
    3: [(1100, 160, 50), (1100, 160, 50),
        (1100, 160, 50), (1100, 160, 0)],                         # 4 rapid high
}

_VOLUME_MAP = {0: 0.45, 1: 0.60, 2: 0.75, 3: 0.90}


def _play_pattern(level: int, volume_multiplier: float = 1.0):
    """Blocking — meant to be called from a thread."""
    if not _audio_ready:
        return
    pattern = _PATTERNS.get(level, _PATTERNS[0])
    base_vol = _VOLUME_MAP.get(level, 0.6) * volume_multiplier
    vol      = min(base_vol, 1.0)
    for freq, dur_ms, gap_ms in pattern:
        try:
            tone = _generate_tone(freq, dur_ms, vol)
            tone.play()
            time.sleep((dur_ms + gap_ms) / 1000)
        except Exception as e:
            logger.debug(f"alert_sound tone error: {e}")
            break


def play_alert(level: int = 0, volume: float = 1.0):
    """
    Non-blocking alert tone. Fires in a background thread.
    level  : 0-3 (escalation level)
    volume : 0.0-1.0 multiplier on top of the level's base volume
    """
    if not _audio_ready:
        return
    t = threading.Thread(target=_play_pattern, args=(level, volume), daemon=True)
    t.start()


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("alert_sound.py — standalone test")
    if not init_audio():
        print("  Audio not available. Install: pip install pygame numpy")
    else:
        labels = {0:"NEW ALERT (440Hz x1)", 1:"MOVING >2% (660Hz x2)",
                  2:"HOT >5% (880Hz x3)",   3:"ROCKET >10% (1100Hz x4)"}
        for lvl in range(4):
            print(f"  Level {lvl}: {labels[lvl]}")
            _play_pattern(lvl, 1.0)
            time.sleep(1.2)
        print("  Done.")
