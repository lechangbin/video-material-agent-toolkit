# Contributing

Use Windows x64 and CPython 3.14.6. Keep changes scoped to one package or one
cross-package contract, and update the corresponding tests and documentation.

Before opening a pull request, run:

```powershell
Set-Location packages\material-collector
uv sync
uv run pytest
uv run ruff check .
uv run mypy .

Set-Location ..\semvideo
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Never commit API Keys, Cookies, browser profiles, downloaded media, task state,
SQLite databases, `.env` files, virtual environments or build artifacts.

New or changed Skills must keep `SKILL.md` at the Skill root, use only relative
resource references, and validate against the Agent Skills specification.
