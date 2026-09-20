# One-off scripts

These are not part of the production codebase. They are kept here
(git history preserved) because they document design decisions and
historical debugging:

- `analyze-*.py` / `analyze-data.json`: one-time usage analysis
  scripts and the JSON they produced
- `backfill-*.py`: backfill scripts used during the cache-tracking
  rollout
- `backfill-upstream-credit.py`: 把 `logs.credit` 从 token 估算改成上游真值
  （Qoder CN 的 `usage.credits`/`billable`；免费档曾按 token 记出假消耗，
  见 `docs/credit-and-token-tracking.md` §11）

Do not import from these. Do not run them in production. The files
are intentionally git-tracked (rather than deleted) so that the
rationale behind subsequent schema decisions can be reconstructed.
