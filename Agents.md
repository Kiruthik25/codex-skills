# Agent Instructions

## Project context

- Read `docs/CODE_MAP.md` when component discovery is necessary.
- Read `docs/ARCHITECTURE.md` for architectural or cross-component changes.
- Read `docs/TASK_STATE.md` when continuing unfinished work.
- Consult `docs/DECISIONS.md` when modifying architectural decisions.

## Context efficiency

- Start with targeted symbol and file searches.
- Avoid scanning the entire repository unless necessary.
- Reuse previously inspected context when it remains current.
- Inspect the current implementation before editing.
- Read relevant functions, imports, interfaces, callers and tests.
- Exclude generated files, dependencies, caches and build artifacts from routine exploration.
- Retrieve additional context only when required for correctness.

## Implementation

- Prefer minimal, localized changes.
- Preserve existing architecture and public interfaces unless the task requires changing them.
- Reuse existing utilities and patterns.
- Avoid unrelated refactoring.
- Do not introduce unnecessary dependencies.

## Verification

- Run the smallest relevant test set first.
- Run broader tests when changes affect shared components.
- Limit successful command output.
- Preserve useful error messages and stack traces.
- Never claim tests passed unless they were executed successfully.

## Project memory

- Update CODE_MAP.md when component responsibilities change.
- Update ARCHITECTURE.md when system design changes.
- Update DECISIONS.md when architectural decisions change.
- Update TASK_STATE.md at meaningful checkpoints.

## Response format

- Summarize changes concisely.
- Report modified files, test results and unresolved issues.
- Do not reproduce entire source files in the final response unless requested.
