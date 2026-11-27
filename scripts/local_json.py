# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic replacement for generated JSON artifacts."""
import json
import os
import pathlib
import stat
import tempfile


def destination_path(value):
    # Resolve the parent, not the final name, so lstat sees final symlinks.
    requested = pathlib.Path(value)
    return requested.parent.resolve() / requested.name


def existing_output_mode(output, label):
    try:
        previous = output.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(previous.st_mode):
        raise ValueError(f"{label} output must be a regular file")
    return previous.st_mode & 0o777


def publish_json(output, value, *, label, prefix):
    """Publish only a fully written JSON file, retaining prior mode bits."""
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = existing_output_mode(output, label)
    # A private stage on the same filesystem keeps replacement atomic and
    # leaves the previous destination untouched if writing or syncing fails.
    with tempfile.TemporaryDirectory(prefix=prefix, dir=output.parent) as directory:
        staged = pathlib.Path(directory) / output.name
        with staged.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            if mode is not None:
                staged.chmod(mode)
            os.fsync(stream.fileno())
        os.replace(staged, output)
