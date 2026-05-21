# Phase 0 — Complete

Date: 2026-05-21

## What was built

| Artifact | Purpose |
|---|---|
| `pyproject.toml` | Python 3.12+, deps pinned (Pydantic 2.13, SQLGlot 25, NetworkX 3, PyGithub 2.9, Typer 0.25, psycopg 3, httpx, Jinja2). Ruff + Pyright + pytest configured. |
| `docker-compose.yml` + `infra/postgres-init.sql` | Single Postgres 16 with three schemas (`source_raw`, `analytics`, `schema_drift`) + agent metadata tables auto-created. |
| `Makefile` | `help`, `install`, `up`, `down`, `reset`, `demo`, `bench`, `fmt`, `lint`, `typecheck`, `test`, `test-fast`, `cov`, `clean`. |
| `src/schema_drift/models.py` | 16 Pydantic v2 models: enums, snapshots, RawChange, DriftEvent (with destructive-op gate validator + severity floor), ImpactSet, MigrationBundle, AuditRecord. 158 statements, 100% covered. |
| `src/schema_drift/cli.py` | Typer entry point with `version`, `demo`, `watch` stubs. Installs as `drift` console script. |
| `tests/test_models.py` | 41 contract tests across enums, validators, serialization, and module exports. |
| `tests/test_cli.py` | 3 CLI smoke tests using `CliRunner`. |
| `.github/workflows/ci.yml` | GitHub Actions matrix (3.12, 3.13): ruff check, ruff format check, pyright, pytest with coverage upload. |
| `.pre-commit-config.yaml` | trailing-whitespace, EOF, yaml/toml validation, ruff + ruff-format. |
| `.env.example`, `.gitignore`, `LICENSE`, `README.md` | Standard repo furniture. README is recruiter-scannable, with hero-metric placeholders. |

## Quality gate (all green)

```
=== ruff lint ===
All checks passed!
=== ruff format ===
7 files already formatted
=== pyright ===
0 errors, 0 warnings, 0 informations
=== pytest ===
44 passed, 1 warning in 0.31s
TOTAL  181 stmts, 18 branches, 98% coverage
```

`models.py` itself is **100% covered**. The 3 missed statements in `cli.py` are inside the `watch` stub that gets wired up Day 3.

## Smoke verified

- `drift version` → `schema-drift-detective 0.1.0`
- `drift --help` → lists `version`, `demo`, `watch`
- `docker compose up -d` → Postgres healthy, schemas `analytics` / `source_raw` / `schema_drift` present, tables `audit_log` / `drift_events` / `schema_snapshots` auto-created from `postgres-init.sql`.

## Key design properties baked in

1. **Pydantic models are frozen + extra-forbid** — typos surface as `ValidationError`, never as silent attribute drift.
2. **Destructive-op gate is a model validator** — any code path that tries to build `DriftEvent(change_type=COLUMN_DROPPED, auto_mergeable=True)` raises at construction time. There is no way to bypass it.
3. **Severity floor enforced** — you can upgrade severity per context, but never downgrade below the per-ChangeType default. (E.g., a `COLUMN_DROPPED` cannot be marked LOW even if impact is empty.)
4. **Single Postgres for source + warehouse + agent metadata** — radical simplicity for the demo while still being three logically-separate environments (different schemas). Documented in `docs/02_revised_plan.md`.

## What's NOT in Phase 0 (and why)

- No PostgresWatcher implementation yet → Week 1 Day 3.
- No classifier rules yet → Week 1 Days 4–6.
- No lineage code yet → Week 3.
- No LLM integration yet → Week 4.
- No benchmark generator → Week 2.
- Marquez container commented out in `docker-compose.yml` → Week 6.

Everything has a placeholder file or a TODO with a target week so nothing is forgotten.

## Recommended first git commit

```bash
git init
git add .
git commit -m "phase-0: scaffold with Pydantic contracts, CI, docker-compose, and 44 passing tests"
```

Then create the GitHub repo and push:

```bash
gh repo create antarang/schema-drift-detective --public --source=. --push --remote=origin
# or via the web UI: github.com/new
```

## Next step — Week 1, Day 3

Build the thin-slice MVP:
1. `PostgresWatcher` that takes two `SchemaSnapshot`s and emits `RawChange`s.
2. `Classifier` that handles `COLUMN_ADDED_NULLABLE` only.
3. `LineageGraph.from_manifest()` that reads `target/manifest.json` and does forward BFS.
4. `MigrationDrafter` for nullable add (just patches `schema.yml`).
5. `GitHubPRGateway.open_pr()` against a real fork.
6. Wire it all together in `drift demo` and `drift watch --once`.

Definition of done: `make demo` injects a `nullable column add` into Postgres and (in `--dry-run` mode) prints the PR body to stdout. End-to-end in < 30s.
