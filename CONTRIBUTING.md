# Contributing to Anansi

Thanks for your interest. Contributions are welcome, but this project has opinions. Read this before opening a PR.

---

## Getting started

```bash
git clone https://github.com/mdowis/anansi.git
cd anansi
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite before you change anything to make sure your environment is clean:

```bash
pytest
```

---

## What to work on

Check the [issues](../../issues) tab first. Anything labeled `good first issue` is scoped and ready. Anything labeled `help wanted` is fair game.

If you want to work on something that isn't tracked, open an issue before writing code. A quick description of what you're solving and why is enough. This avoids wasted effort if the direction doesn't fit.

Feature requests without a clear use case or that add complexity without fixing a real problem are likely to be declined.

---

## Submitting a pull request

- Keep PRs focused. One fix or feature per PR.
- Write a clear description of what changed and why. Not what the code does, what problem it solves.
- Include tests for new behavior. PRs that reduce test coverage won't be merged.
- If your change affects the self-healing logic, selector scoring, or TLS configuration, explain your reasoning carefully. These are load-bearing parts.

Target the `main` branch unless you've been told otherwise.

---

## Code style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Run it before committing:

```bash
ruff check . --fix
ruff format .
```

Type annotations are expected on all public functions. Pydantic models are the source of truth for data shapes. Don't work around them.

---

## Commits

Write commit messages that describe intent, not mechanics.

```
# Good
fix: recover gracefully when selector confidence drops below threshold

# Not useful
fix: update scraper.py
```

Use the conventional commits format (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`). This feeds the changelog.

---

## Reporting bugs

Open an issue. Include:

- What you were scraping (domain is fine, full URL if it helps)
- What you expected to happen
- What actually happened
- Relevant logs or stack traces

The more specific you are, the faster it gets addressed.

---

## What won't be accepted

- Changes that weaken the self-healing behavior as a tradeoff for simplicity
- Dependencies that bring in significant weight without clear justification
- PRs that rewrite working code in a different style without a functional reason
- Anything that requires an external service to function without a local fallback

---

If you're unsure whether something is in scope, ask. An issue is cheaper than a PR that doesn't land.
