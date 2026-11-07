# Contributing to Cortex OS

Thanks for helping! Cortex is an autonomous media-production factory, and there is a lot of room to improve it.

## Get running in 5 minutes

```bash
git clone https://github.com/gawadeditor/cortex-os.git && cd cortex-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # provider keys are optional for tests and dry runs
python3 -m pytest -q tests/test_pipeline.py
python3 run_dashboard.py        # http://127.0.0.1:5001
```

You do **not** need paid provider keys to work on most of the code: tests are isolated from real providers and databases, and `MUAPI_DRY_RUN=true` returns mock video responses.

## Ground rules (the project's integration contracts)

- Read configuration only through `hypogeum.config.Config` — never `os.getenv()` directly.
- Log through `hypogeum.logger.get_logger()` — never `print()` or stdlib `logging`.
- Every LLM call goes through `hypogeum.agent.run_agent()`.
- Every agent output is validated with `hypogeum.schemas.validate()`.
- Database access goes through `hypogeum.db.get_connection(db_path)`. **Tests must never touch the production database** — use `tmp_path` and pass `db_path=` explicitly.
- Never commit secrets, `.env` files, key files or personal data.

More detail lives in [`CLAUDE.md`](CLAUDE.md) (architecture and conventions) and [`docs/architecture-index.md`](docs/architecture-index.md).

## Good places to start

- Look for issues labelled **good first issue** or **help wanted**.
- Improve documentation, add tests, or tighten error messages.
- Add a new provider adapter (video, voice, music, publishing).
- Add a channel template for a new niche (see [`CHANNEL_PACKAGE_SPEC.md`](CHANNEL_PACKAGE_SPEC.md)).
- Improve the dashboard UI (see `design-system/`).

## Pull requests

1. Open an issue first for anything large, so we can agree on the approach.
2. Keep each PR focused on one change, with tests where it makes sense.
3. Run the tests for the files you touched before pushing.
4. Describe *why* the change is needed, not just what it does.
