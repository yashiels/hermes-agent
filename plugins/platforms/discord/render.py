import re

from gateway.platforms.helpers import convert_table_to_bullets

_HORIZONTAL_RULE_RE = re.compile(r'^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$')
_DEEP_HEADER_RE = re.compile(r'^(\s{0,3})#{4,}(\s+.*)$')


def render_for_discord(text: str) -> str:
    if not text:
        return text
    out: list[str] = []
    in_fence = False
    for line in text.split('\n'):
        is_fence_line = line.lstrip().startswith('```')
        in_fence ^= is_fence_line
        if in_fence or is_fence_line:
            out.append(line)
            continue
        if _HORIZONTAL_RULE_RE.match(line):
            continue
        header_match = _DEEP_HEADER_RE.match(line)
        out.append(f"{header_match.group(1)}###{header_match.group(2)}" if header_match else line)
    return '\n'.join(out)


def format_discord_message(content: str) -> str:
    if not content:
        return content
    return render_for_discord(convert_table_to_bullets(content))
