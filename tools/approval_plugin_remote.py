import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from tools.approval_plugin_commands import parse_options


_FORBIDDEN_COMPONENTS = {".ssh", ".gnupg", ".aws", ".iai-mcp", ".config", ".hermes"}
_FORBIDDEN_BASENAMES = (".env*", "*.pem", "*.key", "*.p12", "id_*", "*secret*", "*token*", "*credential*", ".netrc", ".npmrc", ".pypirc")
_GLOB_CHARACTERS = frozenset("*?[]{}")


@dataclass(frozen=True)
class RemoteExemptionPolicy:
    ssh_hosts: frozenset[str]
    scripts: frozenset[str]
    file_roots: tuple[str, ...]
    scratch_roots: tuple[str, ...]


def canonical_path(value: str) -> str | None:
    if not value or value.startswith("!") or any(char in _GLOB_CHARACTERS for char in value):
        return None
    try:
        return os.path.realpath(os.path.expanduser(value))
    except (OSError, ValueError, UnicodeError):
        return None


def absolute_paths(values) -> tuple[str, ...] | None:
    if not isinstance(values, list) or any(not isinstance(value, str) or not os.path.isabs(value) for value in values):
        return None
    resolved = tuple(canonical_path(value) for value in values)
    return resolved if all(value is not None for value in resolved) else None


def is_scratch_root(root: str) -> bool:
    parts = Path(root).parts
    return len(parts) >= 5 and parts[-5:-3] == (".hermes", "profiles") and parts[-2:] == ("cache", "scratch")


def remote_exemption_policy(policy: dict) -> RemoteExemptionPolicy | None:
    hosts = policy.get("ssh_hosts", [])
    if not isinstance(hosts, list) or any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9-]+", host)
                                         or host in {"orion", "nebula"} for host in hosts):
        return None
    oracle = policy.get("oracle", {})
    if not isinstance(oracle, dict):
        return None
    scripts = absolute_paths(oracle.get("scripts", []))
    roots = absolute_paths(oracle.get("file_roots", []))
    if scripts is None or roots is None:
        return None
    return RemoteExemptionPolicy(frozenset(hosts), frozenset(scripts), roots,
                                 tuple(root for source, root in zip(oracle.get("file_roots", []), roots)
                                       if is_scratch_root(source) or is_scratch_root(root)))


def ssh_destination(argv: list[str], hosts: frozenset[str]) -> str | None:
    if argv[0] != "ssh":
        return None
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        index += 1
        if option in {"-T", "-n", "-q"}:
            continue
        if not option.startswith("-o"):
            return None
        value = option[2:]
        if option == "-o":
            if index == len(argv):
                return None
            value = argv[index]
            index += 1
        if value != "BatchMode=yes" and not re.fullmatch(r"ConnectTimeout=[0-9]+", value):
            return None
    if index == len(argv) or argv[index] not in hosts:
        return None
    if any(word.startswith("-") for word in argv[index + 1:]):
        return None
    return argv[index]


def inside_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def oracle_file_path(value: str, policy: RemoteExemptionPolicy, *, log: bool = False) -> bool:
    path = canonical_path(value)
    if path is None:
        return False
    roots = (*policy.scratch_roots, os.path.realpath("/tmp")) if log else policy.file_roots  # no-tmp: ok -- owner-approved Oracle log boundary
    if not any(inside_root(path, root) for root in roots):
        return False
    allowed_hermes_indexes = {
        len(Path(root).parts) - 5 for root in policy.scratch_roots
        if inside_root(path, root) and is_scratch_root(root)
    }
    for index, component in enumerate(Path(path).parts):
        if component.lower() in _FORBIDDEN_COMPONENTS and not (component == ".hermes" and index in allowed_hermes_indexes):
            return False
    return not any(fnmatch.fnmatchcase(Path(name).name.lower(), pattern)
                   for name in (value, path) for pattern in _FORBIDDEN_BASENAMES)


def oracle_command(argv: list[str], policy: RemoteExemptionPolicy) -> bool:
    if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        return False
    script = canonical_path(argv[0])
    if script not in policy.scripts:
        return False
    parsed = parse_options(argv[1:], {"-p", "--prompt", "--file", "--effort", "--model", "-l", "--log"}, {"--dry-run"})
    if parsed is None:
        return False
    operands, options = parsed
    if operands:
        return False
    seen = set()
    aliases = {"-p": "--prompt", "-l": "--log"}
    for option, value in options:
        option = aliases.get(option, option)
        if option != "--file" and option in seen:
            return False
        seen.add(option)
        if not oracle_option(option, value, policy):
            return False
    return True


def oracle_option(option: str, value: str, policy: RemoteExemptionPolicy) -> bool:
    if option in {"--prompt", "--file", "--log"}:
        return oracle_file_path(value, policy, log=option == "--log")
    if option == "--effort":
        return value in {"low", "medium", "high"}
    if option == "--model":
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value))
    return option == "--dry-run"


def remote_command_owner(argv: list[str], policy: RemoteExemptionPolicy) -> str | None:
    if argv[0] == "ssh":
        host = ssh_destination(argv, policy.ssh_hosts)
        return f"ssh:{host}" if host else None
    return "oracle" if policy.scripts and oracle_command(argv, policy) else None
