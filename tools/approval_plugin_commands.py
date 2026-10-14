import re

from tools.approval_detection import _command_parser_limit_exceeded, _scan_shell, _shell_segment_tokens


_GH_OPERATIONS = {
    "issue": {"create", "comment", "edit", "view", "list", "close", "reopen"},
    "pr": {"create", "view", "list", "comment", "edit", "checks", "diff", "status"},
    "repo": {"view"},
    "run": {"list", "view", "watch"},
}
_GH_VALUES = {
    "--repo", "-R", "--title", "-t", "--body", "-b", "--label", "-l", "--assignee", "-a",
    "--milestone", "-m", "--project", "-p", "--json", "--jq", "-q", "--limit", "-L",
    "--state", "-s", "--author", "--base", "-B", "--head", "-H",
    "--reviewer", "-r", "--add-label", "--remove-label", "--add-assignee", "--remove-assignee",
    "--add-project", "--remove-project", "--add-reviewer", "--remove-reviewer", "--reason",
    "--workflow", "-w", "--branch", "--event", "--commit", "--created", "--interval", "-i",
}
_GH_SWITCHES = {
    "--draft", "-d", "--closed", "--comments", "-c", "--edit-last", "--create-if-none",
    "--delete-last", "--yes", "--fail-fast", "--watch", "--exit-status", "--log", "--log-failed",
    "--verbose", "-v", "--compact", "--patch", "--name-only", "--required", "--disabled",
}
_GIT_OPTIONS = {
    "fetch": ({"--depth", "--deepen", "--shallow-since", "--shallow-exclude"},
              {"--all", "--prune", "--tags", "--no-tags", "--quiet", "-q", "--verbose", "-v", "--dry-run", "--unshallow", "--no-recurse-submodules"}),
    "status": ({"--untracked-files"}, {"--short", "-s", "--branch", "-b", "--porcelain", "--ignored", "--show-stash"}),
    "log": ({"--max-count", "-n", "--since", "--until", "--author", "--grep", "--format", "--pretty"},
            {"--oneline", "--all", "--decorate", "--no-decorate", "--graph", "--stat", "--name-only", "--name-status", "--no-ext-diff", "--no-textconv"}),
    "worktree add": ({"-b", "-B", "--reason"}, {"--detach", "-d", "--force", "-f", "--no-checkout", "--lock"}),
    "worktree list": (set(), {"--porcelain", "--verbose", "-v", "-z"}),
}
_REPO_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_GIT_REF = re.compile(r"[A-Za-z0-9_./+*:~^{}-]+\Z")
_UNSAFE_COMMAND_CONTROLS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def command_segments(command: str, *, forbid_pipes: bool = False) -> list[list[str]] | None:
    if _UNSAFE_COMMAND_CONTROLS.search(command):
        return None
    if not command.strip() or _command_parser_limit_exceeded(command):
        return None
    segments, start, skip, previous = [], 0, -1, ""
    for kind, i, j, quote in _scan_shell(command, subst="uq", stop_unterminated=True, comments=True):
        if i < skip:
            continue
        if kind == "subst" or (kind == "char" and quote != "'" and command[i] in "`$"):
            return None
        if kind == "char" and quote is None and command[i] in "<>{}()":
            return None
        if kind == "comment":
            return None
        if kind != "char" or quote is not None or command[i] not in ";&|":
            continue
        segment = command[start:i].strip()
        if segment:
            argv = _shell_segment_tokens(segment, 0)
            if not argv:
                return None
            segments.append(argv)
        else:
            return None
        operator = command[i]
        if operator in "&|" and command[i:i + 2] == operator * 2:
            operator *= 2
            j += 1
            skip = j
        if operator == "&" or (forbid_pipes and operator == "|"):
            return None
        previous = operator
        start = j
    segment = command[start:].strip()
    if segment:
        argv = _shell_segment_tokens(segment, 0)
        if not argv:
            return None
        segments.append(argv)
    elif previous in {"&&", "||", "|"}:
        return None
    return segments or None


def executable_name(word: str) -> str | None:
    return word if word in {"gh", "git"} else None


def parse_options(args: list[str], values: set[str], switches: set[str]) -> tuple[list[str], list[tuple[str, str]]] | None:
    operands, options, index = [], [], 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--" or token.startswith("@"):
            return None
        if not token.startswith("-"):
            operands.append(token)
            continue
        option, equals, value = token.partition("=")
        if option in switches and not equals:
            options.append((option, ""))
            continue
        if option not in values:
            return None
        if not equals:
            if index == len(args):
                return None
            value = args[index]
            index += 1
        if not value or value.startswith(("@", "-")):
            return None
        options.append((option, value))
    return operands, options


def repository_owner(selector: str, owners: set[str]) -> str | None:
    pieces = selector.split("/")
    if len(pieces) != 2 or not _REPO_COMPONENT.fullmatch(pieces[0]):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", pieces[1]) or pieces[1] in {".", ".."}:
        return None
    owner = pieces[0].lower()
    return owner if owner in owners else None


def gh_owner(argv: list[str], owners: set[str]) -> str | None:
    from agent.secret_scope import get_secret

    if get_secret("GH_HOST") or get_secret("GH_REPO"):
        return None
    if len(argv) < 2 or any(token.startswith("@") for token in argv):
        return None
    if argv[1] == "api":
        return gh_api_owner(argv[2:], owners)
    if len(argv) < 3 or argv[2] not in _GH_OPERATIONS.get(argv[1], set()):
        return None
    parsed = parse_options(argv[3:], _GH_VALUES, _GH_SWITCHES)
    if parsed is None:
        return None
    operands, options = parsed
    selectors = [value for option, value in options if option in {"-R", "--repo"}]
    if len(selectors) != 1 or not gh_operands(argv[1], argv[2], operands):
        return None
    return repository_owner(selectors[0], owners)


def gh_operands(command: str, operation: str, operands: list[str]) -> bool:
    if command == "repo" or operation in {"create", "list", "status"}:
        return not operands
    if len(operands) > 1:
        return False
    if not operands:
        return operation in {"view", "checks", "diff"}
    operand = operands[0]
    if command in {"issue", "run"}:
        return operand.isascii() and operand.isdecimal()
    return bool(re.fullmatch(r"[A-Za-z0-9_./-]+", operand)) and ".." not in operand


def gh_api_owner(args: list[str], owners: set[str]) -> str | None:
    parsed = parse_options(args, {"-X", "--method", "--jq", "-q", "--cache"},
                           {"--paginate", "--slurp", "--include", "-i", "--silent"})
    if parsed is None:
        return None
    operands, options = parsed
    methods = [value for option, value in options if option in {"-X", "--method"}]
    if len(operands) != 1 or len(methods) > 1 or any(method != "GET" for method in methods):
        return None
    pieces = operands[0].split("/")
    if len(pieces) < 3 or pieces[0] != "repos" or any(not _REPO_COMPONENT.fullmatch(piece) for piece in pieces[3:]):
        return None
    return repository_owner("/".join(pieces[1:3]), owners)


def git_local_command(argv: list[str]) -> bool:
    args = argv[1:]
    if args[:1] == ["-C"]:
        if len(args) < 3 or not args[1] or args[1].startswith(("-", "@")):
            return False
        args = args[2:]
    if not args:
        return False
    operation, args = args[0], args[1:]
    if operation == "worktree" and args:
        operation, args = f"worktree {args[0]}", args[1:]
    allowed = _GIT_OPTIONS.get(operation)
    parsed = parse_options(args, *allowed) if allowed else None
    if parsed is None:
        return False
    operands, _ = parsed
    if operation in {"status", "worktree list"}:
        return not operands
    if operation == "worktree add":
        return 1 <= len(operands) <= 2 and (len(operands) == 1 or bool(_GIT_REF.fullmatch(operands[1])))
    if operation == "fetch" and operands:
        return bool(_REPO_COMPONENT.fullmatch(operands[0])) and all(_GIT_REF.fullmatch(ref) for ref in operands[1:])
    return all(_GIT_REF.fullmatch(ref) for ref in operands)
