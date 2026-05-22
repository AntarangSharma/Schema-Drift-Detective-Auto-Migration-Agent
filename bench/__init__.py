"""Schema-drift benchmark.

Generator + runner + baselines for the 300-scenario eval. Built out in
Week 2 (see ``docs/02_revised_plan.md``). Each scenario is a deterministic,
seeded ``(pre_snapshot, raw_changes, expected_event)`` triple — see
``bench.generate.Scenario``.
"""
