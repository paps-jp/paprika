"""paprika learner -- the out-of-process learning system.

Lives OUTSIDE ``server/`` and ``core/`` on purpose: those two trees are
what the deploy watcher hashes and what workers self-update from, so code
here can change without restarting a hub or churning the fleet.

The hub's only contribution is a stream of per-job FACTS
(server/hub/_outcome_stream.py). Everything that judges, counts, or
learns is in this package. Design:
internal/design/autonomous-learning-loop-2026-11-12.html (§5, §6, §15).
"""
