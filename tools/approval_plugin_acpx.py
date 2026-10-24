import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from tools.approval_plugin_commands import _REPO_COMPONENT
from tools.approval_plugin_remote import absolute_paths


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ROUND_PROMPT = re.compile(r"Read (AGENT-BRIEF-R[1-9][0-9]?\.md) in this directory and execute it\Z")
_BRIEF_NAME = re.compile(r"\bAGENT-BRIEF(?:-R[1-9][0-9]?)?\.md\b")


@dataclass(frozen=True)
class AcpxExemptionPolicy:
    agents: frozenset[str]
    repo_roots: tuple[str, ...]
    prompts: frozenset[str]
    prompt_round_files: bool
    cursor_models: frozenset[str]


def acpx_exemption_policy(policy: dict) -> AcpxExemptionPolicy | None:
    if "acpx" not in policy:
        return AcpxExemptionPolicy(frozenset(), (), frozenset(), False, frozenset())
    value = policy["acpx"]
    if not isinstance(value, dict):
        return None
    lists = [value.get(key) for key in ("agents", "prompts", "cursor_models")]
    if any(not isinstance(items, list) or any(not isinstance(item, str) for item in items) for items in lists):
        return None
    agents, prompts, models = lists
    roots = absolute_paths(value.get("repo_roots"))
    rounds = value.get("prompt_round_files")
    if roots is None or not isinstance(rounds, bool) or not set(agents) <= {"codex", "cursor"}:
        return None
    return AcpxExemptionPolicy(frozenset(agents), roots, frozenset(prompts), rounds, frozenset(models))


def regular_file(path: Path, limit: int) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_size <= limit


def metadata_line(path: Path) -> str | None:
    if not regular_file(path, 4096):
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return None
        data = stream.read(4097)
    if len(data) > 4096:
        return None
    lines = data.decode("utf-8").splitlines()
    return lines[0] if len(lines) == 1 and lines[0] and "\x00" not in lines[0] else None


def worktree_metadata_matches(cwd: Path, repo: Path) -> bool:
    marker = metadata_line(cwd / ".git")
    if marker is None or not marker.startswith("gitdir: "):
        return False
    metadata = Path(os.path.realpath(cwd / marker[8:]))
    if metadata.parent != repo / ".git/worktrees" or not metadata.is_dir():
        return False
    backref = metadata_line(metadata / "gitdir")
    if backref is None or os.path.realpath(metadata / backref) != str(cwd / ".git"):
        return False
    common = metadata / "commondir"
    if os.path.lexists(common):
        common_line = metadata_line(common)
        if common_line is None or os.path.realpath(metadata / common_line) != str(repo / ".git"):
            return False
    return True


def acpx_cwd_owner(value: str, policy: AcpxExemptionPolicy, owners: set[str]) -> str | None:
    if not os.path.isabs(value) or ".." in Path(value).parts or any(char in value for char in "*?[]{}"):
        return None
    try:
        normalized = os.path.normpath(value)
        if os.path.realpath(value) != normalized:
            return None
        cwd = Path(normalized)
        for root in policy.repo_roots:
            if not cwd.is_relative_to(root):
                continue
            parts = cwd.relative_to(root).parts
            if len(parts) != 4:
                continue
            owner, repo, worktrees, task = parts
            if owner.lower() not in owners or worktrees != ".worktrees":
                continue
            if not all(_REPO_COMPONENT.fullmatch(part) for part in (repo, task)):
                continue
            if worktree_metadata_matches(cwd, Path(root) / owner / repo):
                return f"acpx:{owner.lower()}/{repo}"
    except (OSError, ValueError, UnicodeError):
        return None
    return None


def bounded_decimal(value: str, maximum: int) -> bool:
    significant = value.lstrip("0")
    return (bool(re.fullmatch(r"[0-9]+", value)) and 0 < len(significant) <= len(str(maximum))
            and int(significant) <= maximum)


def acpx_globals(args: list[str]) -> tuple[dict[str, str], list[str]] | None:
    options = {}
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        index += 1
        if option in options:
            return None
        if option == "--approve-all":
            options[option] = ""
            continue
        if option not in {"--cwd", "--format", "--timeout", "--model"} or index == len(args):
            return None
        value = args[index]
        index += 1
        if not value or value.startswith(("-", "@")):
            return None
        options[option] = value
    if "--cwd" not in options:
        return None
    if "--format" in options and options["--format"] not in {"json", "text"}:
        return None
    if "--timeout" in options and not bounded_decimal(options["--timeout"], 86400):
        return None
    return options, args[index:]


def acpx_prompt(prompt: str, cwd: Path, policy: AcpxExemptionPolicy) -> bool:
    round_match = _ROUND_PROMPT.fullmatch(prompt) if policy.prompt_round_files else None
    if prompt not in policy.prompts and round_match is None:
        return False
    names = _BRIEF_NAME.findall(prompt)
    if len(names) != 1:
        return False
    try:
        return regular_file(cwd / names[0], 256 * 1024)
    except OSError:
        return False


def acpx_subcommand(args: list[str], cwd: Path, policy: AcpxExemptionPolicy) -> bool:
    if len(args) == 3 and args[0] == "-s":
        return bool(_NAME.fullmatch(args[1])) and acpx_prompt(args[2], cwd, policy)
    if args[:3] == ["sessions", "ensure", "--name"] and len(args) == 4:
        return bool(_NAME.fullmatch(args[3]))
    if len(args) < 3 or args[0] != "sessions" or not _NAME.fullmatch(args[2]):
        return False
    if args[1] == "close":
        return len(args) == 3
    if args[1] == "history":
        return len(args) == 3 or (len(args) == 5 and args[3] == "--limit" and bounded_decimal(args[4], 1000))
    return False


def acpx_command_owner(argv: list[str], policy: AcpxExemptionPolicy, owners: set[str]) -> str | None:
    if not argv or argv[0] != "acpx" or any(token == "--" or token.startswith("@") for token in argv):
        return None
    parsed = acpx_globals(argv[1:])
    if parsed is None:
        return None
    options, args = parsed
    if not args or args[0] not in policy.agents:
        return None
    if "--model" in options and (args[0] != "cursor" or options["--model"] not in policy.cursor_models):
        return None
    owner = acpx_cwd_owner(options["--cwd"], policy, owners)
    if owner and acpx_subcommand(args[1:], Path(options["--cwd"]), policy):
        return owner
    return None
