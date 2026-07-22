"""Hard safety filters for shell commands issued by the agent.

These are core guardrails, not advisory skills: blocked commands never reach
the shell. Prefer built-in tools (``delete_file``, ``apply_patch``, etc.) for
intentional workspace edits.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "`"})


@dataclass(frozen=True)
class BlockedCommand:
    """A command rejected by the safety filter."""

    reason: str
    rule: str


def check_command(command: str) -> BlockedCommand | None:
    """Return a :class:`BlockedCommand` when ``command`` violates a safety rule."""

    text = command.strip()
    if not text:
        return BlockedCommand(reason="empty command", rule="empty")
    if re.search(r":\(\)\s*\{\s*:\|:&\s*\}\s*;?\s*:", text):
        return BlockedCommand(reason="fork bombs are blocked", rule="fork-bomb")
    if "$(" in text or "`" in text:
        return BlockedCommand(
            reason="command substitution is blocked; run the inner command directly",
            rule="command-substitution",
        )

    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return BlockedCommand(
            reason="could not parse command safely; refusing to run",
            rule="unparseable",
        )

    for argv in _command_argv_groups(tokens):
        blocked = _check_argv(argv)
        if blocked is not None:
            return blocked
    return None


def _command_argv_groups(tokens: list[str]) -> list[list[str]]:
    """Split tokenized input on shell operators while preserving quoted text."""

    groups: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if current:
                groups.append(current)
                current = []
            continue
        current.append(token)
    if current:
        groups.append(current)
    return groups


# Wrappers whose argument is itself a command to inspect.
_TRANSPARENT_WRAPPERS = frozenset({"env", "nohup", "nice", "ionice", "setsid", "stdbuf", "xargs"})
_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh"})


def _check_argv(tokens: list[str]) -> BlockedCommand | None:
    tokens = [token for token in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token)]
    if not tokens:
        return None

    name = PurePosixPath(tokens[0]).name

    if name == "sudo":
        return BlockedCommand(
            reason="sudo is blocked inside the agent sandbox",
            rule="sudo",
        )

    if name in _SHELL_NAMES:
        # Recurse into `bash -c '<payload>'` so wrappers cannot launder commands.
        for index, arg in enumerate(tokens[1:], start=1):
            if arg == "-c" and index + 1 < len(tokens):
                return check_command(tokens[index + 1])
        return None

    if name in _TRANSPARENT_WRAPPERS:
        rest = [arg for arg in tokens[1:] if not arg.startswith("-")]
        if rest:
            return _check_argv(rest)
        return None

    if name == "timeout":
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        if rest:
            # Skip the duration argument.
            return _check_argv(rest[1:]) if len(rest) > 1 else None
        return None

    if name == "find" and any(arg == "-delete" for arg in tokens[1:]):
        return BlockedCommand(
            reason="find -delete is blocked; use the delete_file tool for intentional deletes",
            rule="find-delete",
        )
    if name == "find" and any(arg in {"-exec", "-execdir", "-ok", "-okdir"} for arg in tokens[1:]):
        for index, arg in enumerate(tokens[1:], start=1):
            if arg in {"-exec", "-execdir", "-ok", "-okdir"} and index + 1 < len(tokens):
                blocked = _check_argv(tokens[index + 1 :])
                if blocked is not None:
                    return blocked
        return None

    if name in {"rm", "rmdir"}:
        return BlockedCommand(
            reason="rm/rmdir is blocked; use the delete_file tool for intentional deletes",
            rule="rm",
        )
    if name == "unlink":
        return BlockedCommand(
            reason="unlink is blocked; use the delete_file tool for intentional deletes",
            rule="unlink",
        )
    if name == "dd":
        return BlockedCommand(
            reason="dd is blocked to prevent raw disk writes",
            rule="dd",
        )
    if name == "mkfs" or name.startswith("mkfs."):
        return BlockedCommand(
            reason="filesystem formatting commands are blocked",
            rule="mkfs",
        )
    if name == "git":
        return _check_git(tokens[1:])
    return None


def _check_git(args: list[str]) -> BlockedCommand | None:
    """Inspect git argv after global options."""

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-C", "-c"} and index + 1 < len(args):
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return None

    subcommand = args[index]
    rest = args[index + 1 :]
    if subcommand == "push" and _has_flag(rest, {"-f", "--force", "--force-with-lease"}):
        return BlockedCommand(
            reason="git push --force is blocked to protect shared history",
            rule="git-push-force",
        )
    if subcommand == "reset" and _has_flag(rest, {"--hard"}):
        return BlockedCommand(
            reason="git reset --hard is blocked to prevent destructive history rewrites",
            rule="git-reset-hard",
        )
    if subcommand == "clean" and (_has_flag(rest, {"--force"}) or _has_short_flag_with(rest, "f")):
        return BlockedCommand(
            reason="git clean -f is blocked to prevent deleting untracked files",
            rule="git-clean-force",
        )
    if subcommand == "checkout" and _has_flag(rest, {"-f", "--force"}):
        return BlockedCommand(
            reason="git checkout --force is blocked to prevent discarding local changes",
            rule="git-checkout-force",
        )
    return None


def _has_flag(args: list[str], flags: set[str]) -> bool:
    for arg in args:
        if arg in flags:
            return True
        if any(arg.startswith(f"{flag}=") for flag in flags if flag.startswith("--")):
            return True
    return False


def _has_short_flag_with(args: list[str], letter: str) -> bool:
    for arg in args:
        if re.fullmatch(r"-[a-zA-Z]*" + re.escape(letter) + r"[a-zA-Z]*", arg):
            return True
    return False
