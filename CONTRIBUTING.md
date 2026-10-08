# Contributing

Issues and pull requests are welcome. This is a small project, so open an issue
before a large change and we can agree on the shape first.

## Setup

```bash
uv sync
uv run pytest
```

Everything runs against the built-in OCC mock (`src/mcp_commerce/mock_occ/`),
so no SAP Commerce instance is needed.

## What a change should include

- **Tests.** Tool behaviour is tested through `mcp.call_tool` against the mock;
  backend failures through a scripted httpx transport (`tests/test_failures.py`).
- **Mock parity.** If you rely on an OCC behaviour, make the mock reproduce it,
  including the awkward parts. The mock is only useful while it honours the
  OCC contract.
- **Token budget.** A tool's largest legal result must stay under
  `RESULT_BUDGET_CHARS`; `tests/test_shaping.py` checks it. Run
  `uv run scripts/measure_tokens.py` (needs `ANTHROPIC_API_KEY`) if you change
  a response shape.
- **Design notes.** New tools follow [`docs/tool-surface.md`](docs/tool-surface.md):
  a description that says when to call the tool and which neighbour it's not,
  identity never as an argument, errors worded for the model.

## Reports from real instances

The server has been verified against the mock only. If you run it against a
real SAP Commerce, an issue describing what differed (version, B2C or B2B, the
request and the response shape) is one of the most useful contributions there is.
