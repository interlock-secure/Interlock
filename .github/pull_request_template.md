## What changed

<!-- One or two sentences. What does this PR do that main does not? -->

## Milestone

<!-- M0-M8, and which requirement IDs from the PRD this satisfies (FR-1 etc.) -->

## Does this touch the schema?

- [ ] No
- [ ] Yes - and I bumped the version and the round-trip test against the
      previous version passes

## The three rules

Confirm none of these were broken (see CLAUDE.md):

- [ ] The hub still only routes and logs. It does not score.
- [ ] No code path returns a low-risk band when the real answer is "unknown".
- [ ] No recall request can close without a recorded disposition.

## How I verified this

<!-- Not "tests pass". What did you actually check, and how would it have
     failed if the change were wrong? -->
