---
name: code-review
description: Review code changes for bugs, security issues and missing tests. Use when the user asks for a review of a diff, branch or file.
---

# Code review

1. Get the change: `git diff` (or `git diff main...HEAD` for a branch). Read surrounding code for context.
2. Look for, in order: correctness bugs, security problems, error handling, concurrency issues, missing tests.
3. For every finding give `path:line`, what is wrong, why it matters, and a concrete fix.
4. Separate **must fix** from **nice to have**. Do not pad the review with style nits.
5. If you are unsure whether something is a bug, say so and explain how to verify it.

See `checklist.md` in this folder for language-specific checks.
