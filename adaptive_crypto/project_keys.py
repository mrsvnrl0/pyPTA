"""Read explicitly named project key files without exposing their contents."""
from pathlib import Path
from .core import DataError


def read_project_key(directory, filename):
    path = Path(directory) / "keys" / filename
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = stream.read(4097).strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        raise DataError("A project key file could not be read") from None
    if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise DataError("A project key file must contain only its key value on one line")
    return value
