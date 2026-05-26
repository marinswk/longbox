<!--
Thanks for the PR! A few small things to confirm before merging.
Delete sections that don't apply.
-->

## What

<!-- One or two sentences. What user-facing change does this PR make? -->

## Why

<!-- The underlying motivation. Link an issue if there is one. -->

## How

<!-- Bullet points listing the actual code changes. Useful for reviewers
to load the architectural picture before reading the diff. -->

-
-

## Checklist

- [ ] `docker build --target test -t longbox-test . && docker run --rm longbox-test` is green locally.
- [ ] `app/version.py` bumped (patch / minor / major per
      [CONTRIBUTING.md](../CONTRIBUTING.md#project-conventions)).
- [ ] Docs updated if the user-facing surface changed (README,
      `docs/*.md`, screenshots if relevant).
- [ ] No new dependency added without a one-line justification
      somewhere in the PR description.
- [ ] Not reintroducing any of the explicitly-dropped features
      (wishlist, loan tracking, pull list, cost/value KPIs, file
      reading — see [ROADMAP.md](../ROADMAP.md)).

## Out of scope

<!-- Anything you noticed but deliberately didn't touch. Helps the
reviewer not ask "what about X?" in the review thread. -->
