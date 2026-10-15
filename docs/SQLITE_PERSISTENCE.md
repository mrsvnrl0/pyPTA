# Current state storage

The default state path is `adaptive_crypto_state.json`; SQLite is stored at `adaptive_crypto_state.sqlite3`. The database contains only `neural` (SMC + Parente paper simulation) and `positions` (recorded holdings, buy watches and alerts). Fresh initialization publishes both namespaces atomically. Unknown namespaces and incompatible document versions are rejected.

Settings contain only the current Rules fields. A clean installation creates default BTC/USDC, ETH/USDC and SOL/USDC pairs and a 1,000 USDC paper account. Runtime Settings apply to subsequent decisions; already recorded trade levels and costs stay fixed.

Document transactions, single-instance state locks, SQLite WAL and verified current-state backups protect current ledgers. Restore validates a complete current backup before replacing state. JSON import/export supports this current format only. The application does not upgrade earlier strategy formats.

Composite spot-candle continuity and combined options research observations use separate current databases. Market cache reconstruction never imports trades or settings. Credentials are stored separately and excluded from backups.
