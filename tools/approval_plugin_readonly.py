import hashlib
import os
import re
import stat
from dataclasses import dataclass

from tools.approval_detection import _scan_shell


_MAX_SCRIPT_BYTES = 1024 * 1024
_DEFAULT_CHARS = 8000


@dataclass(frozen=True)
class GmailGetPin:
    script: str
    sha256: str
    max_chars: int


@dataclass(frozen=True)
class ReadonlyCommandPolicy:
    gh_auth_status: bool
    gmail_get: tuple[GmailGetPin, ...]


def canonical_script_path(value) -> bool:
    if not isinstance(value, str) or not os.path.isabs(value):
        return False
    try:
        os.fsencode(value)
        return "\x00" not in value and os.path.realpath(value) == value
    except (OSError, ValueError, UnicodeError):
        return False


def gmail_get_pin(value) -> GmailGetPin | None:
    if not isinstance(value, dict) or set(value) != {"script", "sha256", "max_chars"}:
        return None
    script, digest, maximum = value["script"], value["sha256"], value["max_chars"]
    if not canonical_script_path(script) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if type(maximum) is not int or not 1 <= maximum <= _DEFAULT_CHARS:
        return None
    return GmailGetPin(script, digest, maximum)


def readonly_command_policy(policy: dict) -> ReadonlyCommandPolicy | None:
    value = policy.get("readonly_commands", {})
    if not isinstance(value, dict) or not set(value) <= {"gh_auth_status", "gmail_get"}:
        return None
    gh = value.get("gh_auth_status", False)
    entries = value.get("gmail_get", [])
    if not isinstance(gh, bool) or not isinstance(entries, list):
        return None
    pins, scripts = [], set()
    for entry in entries:
        pin = gmail_get_pin(entry)
        if pin is None or pin.script in scripts:
            return None
        pins.append(pin)
        scripts.add(pin.script)
    return ReadonlyCommandPolicy(gh, tuple(pins))


def script_matches_pin(pin: GmailGetPin) -> bool:
    if not canonical_script_path(pin.script) or not hasattr(os, "O_NOFOLLOW"):
        return False
    try:
        before = os.lstat(pin.script)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SCRIPT_BYTES:
            return False
        descriptor = os.open(pin.script, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SCRIPT_BYTES:
                return False
            data = stream.read(_MAX_SCRIPT_BYTES + 1)
        return len(data) <= _MAX_SCRIPT_BYTES and hashlib.sha256(data).hexdigest() == pin.sha256
    except (OSError, ValueError, UnicodeError):
        return False


def gmail_get_matches(argv: list[str], pins: tuple[GmailGetPin, ...]) -> bool:
    if len(argv) not in {5, 7} or argv[:2] != ["python3", "-I"] or argv[3] != "gmail-get":
        return False
    if not re.fullmatch(r"[0-9a-f]{16}", argv[4]):
        return False
    pin = next((entry for entry in pins if entry.script == argv[2]), None)
    if pin is None:
        return False
    if len(argv) == 5:
        if pin.max_chars < _DEFAULT_CHARS:
            return False
    elif argv[5] != "--chars" or not bounded_chars(argv[6], pin.max_chars):
        return False
    return script_matches_pin(pin)


def bounded_chars(value: str, maximum: int) -> bool:
    significant = value.lstrip("0")
    return (bool(re.fullmatch(r"[0-9]+", value)) and 0 < len(significant) <= 4
            and int(significant) <= maximum)


def readonly_command_matches(command: str, segments: list[list[str]], policy: ReadonlyCommandPolicy) -> bool:
    from agent.secret_scope import get_secret

    if len(segments) != 1:
        return False
    if any(kind == "char" and quote is None and command[index] in ";&|*?[]~"
           for kind, index, _, quote in _scan_shell(command)):
        return False
    argv = segments[0]
    if argv == ["gh", "auth", "status"]:
        return policy.gh_auth_status and not get_secret("GH_HOST")
    return gmail_get_matches(argv, policy.gmail_get)
