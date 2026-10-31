"""Read-only capability admission for the explicit silent H.264/MP4 profile.

This does not encode, verify media, or alter the legacy audiovisual review mux.
Executable overrides are trusted caller configuration, never manifest fields.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

MAX_PROBE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 10.0

def silent_profile():
    """Return a fresh versioned profile; callers cannot mutate admission policy."""
    return {
        "schema_version": 1,
        "container": "mp4",
        "codec": "h264",
        "encoder": "libx264",
        "pixel_format": "yuv420p",
        "alpha": "none",
        "audio": {"policy": "forbidden", "audio_stream_count": 0},
    }


class CapabilityError(ValueError):
    """A requested profile is malformed or cannot be admitted."""


def validate_profile(profile):
    """Admit only the exact versioned profile; no arbitrary FFmpeg arguments.

    The returned value is an independent copy. Unsupported profiles are not
    silently translated into this one. Dimensions, FPS, source verification and
    color conversion are intentionally not part of this capability-only slice.
    """
    if not isinstance(profile, dict) or set(profile) != set(silent_profile()):
        raise CapabilityError("silent profile must contain exactly the supported fields")
    audio = profile.get("audio")
    if (type(profile.get("schema_version")) is not int or
            not isinstance(audio, dict) or set(audio) != set(silent_profile()["audio"]) or
            type(audio.get("audio_stream_count")) is not int or profile != silent_profile()):
        raise CapabilityError("unsupported silent profile; audio policy must forbid every audio stream")
    return silent_profile()


def _run(argv, timeout):
    """Read bounded output without pipe-reader locks or inherited-pipe waits.

    A temporary regular file avoids waiting for descendants to close inherited
    stdout. Accepted output is strictly bounded; on-disk output may exceed the
    threshold between 10 ms observations. Executables remain trusted config.
    """
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                              stderr=subprocess.STDOUT) as process:
            try:
                while True:
                    size = output.seek(0, 2)
                    if size > MAX_PROBE_BYTES:
                        raise CapabilityError("capability probe output exceeded byte limit")
                    if time.monotonic() >= deadline:
                        raise CapabilityError("capability probe timed out")
                    if process.poll() is not None:
                        break
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
            except BaseException:
                process.kill()
                process.wait()
                raise
            if process.returncode:
                raise CapabilityError("capability probe exited nonzero")
        output.seek(0)
        data = output.read(MAX_PROBE_BYTES + 1)
        if len(data) > MAX_PROBE_BYTES:
            raise CapabilityError("capability probe output exceeded byte limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CapabilityError("capability probe output is not UTF-8") from None


def _executable(name, override):
    if override is None:
        resolved = shutil.which(name)
    else:
        # An explicit invalid override never falls back to PATH.
        path = Path(override)
        resolved = str(path.resolve()) if path.is_absolute() and path.is_file() else None
    return resolved


def _table(text, kind):
    patterns = {
        "encoders": ("Encoders:", r"^\s*V[.A-Z]{5}\s+([A-Za-z0-9_]+)\s+\S"),
        "muxers": ("Formats:", r"^\s*[D ]E[d. ]?\s+([A-Za-z0-9_,]+)\s+\S"),
        "pixels": ("Pixel formats:", r"^\s*[I.]O[.A-Z]{3}\s+([A-Za-z0-9_]+)\s+\d+\s+\d+"),
    }
    heading, pattern = patterns[kind]
    if heading not in text.splitlines():
        raise CapabilityError("malformed " + kind + " capability response")
    names = set()
    for line in text.splitlines():
        match = re.match(pattern, line)
        if match:
            names.update(match.group(1).split(","))
    if not names:
        raise CapabilityError("empty or malformed " + kind + " capability table")
    return names


def probe_capabilities(*, ffmpeg=None, ffprobe=None, timeout=DEFAULT_TIMEOUT):
    """Inspect installed tools without media execution or readiness claims.

    status=available means the named encoder/muxer/pixel format is advertised,
    including encoder-specific pixel compatibility. It does NOT prove an encode
    can execute, that an output is silent, or that any media has been verified.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or not 0 < timeout <= 60):
        raise CapabilityError("timeout must be finite, positive and at most 60 seconds")
    report = {"schema_version": 1, "status": "unavailable", "tools": {},
              "profile": silent_profile(), "evidence": "advertised-capabilities-only",
              "media_execution": "not-performed", "reason": ""}
    for name, override in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        executable = _executable(name, override)
        report["tools"][name] = {"path": executable, "version": None}
    if any(not item["path"] for item in report["tools"].values()):
        report["reason"] = "required FFmpeg/ffprobe executable unavailable"
        return report
    try:
        for name, item in report["tools"].items():
            output = _run([item["path"], "-version"], timeout)
            lines = output.splitlines()
            if not lines or not lines[0].startswith(name + " version ") or len(lines[0]) > 4096:
                raise CapabilityError("malformed " + name + " version response")
            item["version"] = lines[0]
        exe = report["tools"]["ffmpeg"]["path"]
        encoders = _table(_run([exe, "-hide_banner", "-encoders"], timeout), "encoders")
        muxers = _table(_run([exe, "-hide_banner", "-muxers"], timeout), "muxers")
        pixels = _table(_run([exe, "-hide_banner", "-pix_fmts"], timeout), "pixels")
        missing = [name for name, present in (("libx264", "libx264" in encoders),
                    ("mp4", "mp4" in muxers), ("yuv420p", "yuv420p" in pixels)) if not present]
        if missing:
            report.update(status="unsupported", reason="not advertised: " + ", ".join(missing))
            return report
        encoder = _run([exe, "-hide_banner", "-h", "encoder=libx264"], timeout)
        if not encoder.startswith("Encoder libx264 "):
            raise CapabilityError("malformed libx264 capability response")
        match = re.search(r"^\s*Supported pixel formats:\s*(.+)$", encoder, re.MULTILINE)
        if not match:
            raise CapabilityError("libx264 did not report pixel-format compatibility")
        if "yuv420p" not in match.group(1).split():
            report.update(status="unsupported", reason="libx264 does not advertise yuv420p")
            return report
        report.update(status="available", reason="exact profile components advertised; encode untested")
    except (OSError, CapabilityError) as exc:
        report.update(status="unavailable", reason=str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="per-command timeout in seconds (0 < timeout <= 60)")
    args = parser.parse_args()
    try:
        report = probe_capabilities(timeout=args.timeout)
    except CapabilityError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
