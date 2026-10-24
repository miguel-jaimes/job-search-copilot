# Jarvis — the voice (Phase 1: speaking. Listening comes in Phase 2.)
#
# Uses macOS's built-in `say` command, so there's nothing to install and it
# costs nothing. The speech runs in the background: Jarvis starts talking and
# you can start typing right away. Sending your next message stops it, the
# same way you'd cut someone off politely.
#
# Better-sounding voices are free: System Settings -> Accessibility ->
# Spoken Content -> System voice -> Manage Voices, download an "Enhanced" or
# "Premium" voice, then run Jarvis with --voice "<that voice's name>".
# List the voices you have with:  say -v '?'

import re
import shutil
import subprocess

_current = None   # the `say` process that's talking right now, if any


def available():
    """True on a Mac (where the `say` command exists)."""
    return shutil.which("say") is not None


def clean_for_speech(text):
    """Strip things that sound bad read aloud: the [MODE] tag, markdown symbols,
    list numbering, and links."""
    text = re.sub(r"\[(CHAT|INTERVIEW|COACH|DEBRIEF)\]\s*", "", text)   # mode tags like [COACH]
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"[*_#`>|]", "", text)                    # markdown symbols
    text = re.sub(r"^\s*(\d+)[.)]\s+", r"\1, ", text, flags=re.M)   # "1. Fix" -> "1, Fix"
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)     # bullet dashes
    return re.sub(r"\s+", " ", text).strip()


def stop():
    """Stop talking (if we are)."""
    global _current
    if _current and _current.poll() is None:
        _current.terminate()
    _current = None


def speak(text, voice=None, rate=None):
    """Say `text` out loud in the background."""
    global _current
    stop()
    spoken = clean_for_speech(text)
    if not spoken:
        return
    cmd = ["say"]
    if voice:
        cmd += ["-v", voice]
    if rate:
        cmd += ["-r", str(rate)]           # words per minute; macOS default is about 175-200
    _current = subprocess.Popen(cmd + [spoken], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
