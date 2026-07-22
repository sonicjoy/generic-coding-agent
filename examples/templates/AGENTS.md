# Project agent guidance

Keep changes focused on the task; prefer small, reviewable diffs.

## Working style

- Prefer targeted edits via `apply_patch` over rewriting whole files.
- Do not change unrelated files, formatting, or dependencies unless required.
- Match existing project structure, naming, and patterns before inventing new ones.
- When deleting temporary files you created, use `delete_file` (not shell `rm`).
- Do not attempt destructive shell commands (`rm`, `sudo`, `git push --force`,
  `git reset --hard`, etc.) — the harness blocks them.

## Before finishing

- Run the project's usual tests / linters / typechecks when they exist.
- Summarize what changed and how to verify it.
- Leave the tree in a runnable state; do not leave half-applied refactors.

## Customize for this repo

Edit this file with project-specific conventions (build/test commands, package
layout, coding standards, and areas that must not be touched). Routing and tool
policy live in `.gca/config.yaml`.
