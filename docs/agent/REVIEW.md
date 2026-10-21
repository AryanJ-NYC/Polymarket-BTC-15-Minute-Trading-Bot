# US migration review packet

- Migration base SHA: `69985e574bec8330829a2e73280a62d3057e9eb3`. This packet
  records the scoped review performed on the migration before its first commit.
- Spec: current conversation. User asks to connect this repo with `.env.us`,
  implement US data/order/position/paper support, then explicitly requests a
  complete US-only migration with simplicity and no international dual track.
- Standards: supplied global AGENTS.md (newspaper pattern, minimal clear APIs,
  boundary fakes, surgical scope); `docs/agent/MEMORY.md`; no repo AGENTS/ADRs.
- Modules: `bot.py`, `execution/polymarket_client.py`,
  `execution/trade_journal.py`, `core/strategy_brain/strategies/btc_15min_strategy.py`.
  Existing signal processors/fusion remain; dead international phase scaffolding,
  sample scripts, tracked bytecode, and redundant order patches removed.
- Main owns verification commands: `pytest -q`, `ruff check .`,
  `ruff format --check .`, `compileall`, `pip check`, `bot.py --check`, finite
  default-paper smoke. Reviewers must not run these or touch real credentials,
  connect to the account, submit/cancel orders, or edit files.
- Independent lanes: code health plus code-pruning; design integrity; spec and
  financial lifecycle. Remote exchange is the external seam, HTTP MockTransport
  the test substitute; SQLite is local state. No frontend/browser surface;
  Impeccable/React Doctor/Fallow are inapplicable to this Python CLI migration.
- Change-amplification scenario: a US order-envelope or fee/quantity change
  should be localized in the exchange client and tests; signals consume a
  normalized snapshot. Restart/ownership state belongs to one journal.
