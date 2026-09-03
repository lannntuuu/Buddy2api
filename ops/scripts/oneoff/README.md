# One-off scripts

These are not part of the production codebase. They are kept here
(git history preserved) because they document design decisions and
historical debugging:

- `analyze-*.py` / `analyze-data.json`: one-time usage analysis
  scripts and the JSON they produced
- `backfill-*.py`: backfill scripts used during the cache-tracking
  rollout

Do not import from these. Do not run them in production. The files
are intentionally git-tracked (rather than deleted) so that the
rationale behind subsequent schema decisions can be reconstructed.
