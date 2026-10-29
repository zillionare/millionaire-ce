# Risk program selection boundary

CE retains the generic `RiskStrategy` contract and the broker-level
backtest-risk-overlay lifecycle. It ships no built-in triple-barrier,
pullback-sell, or cost-stop program, and the default registry is empty.
Host-owned learner programs are maintained outside the CE source tree and are
excluded from CE discovery, runner fallbacks, and selection UI.

Backtest configuration stores an ordered `risk_programs` array. Each item has
an explicitly registered adapter ID, normalized parameters, and a
deterministic priority. An empty array is valid and is the normal result when
the host has installed no adapters. `allow_same_bar_repurchase` is stored
alongside it and is passed through the generic overlay/composite boundary;
the broker still enforces A-share T+1 availability.

Host packages register a `RiskProgramAdapter` with
`quantide.service.risk_programs.register_risk_program` (or the exported
`risk_program_registry`). The adapter supplies compatibility checks, default
parameters, a backtest overlay factory, and/or a live `RiskStrategy` factory.
The CE runtime never imports a private class by ID and never treats a
discovered `RiskStrategy` as backtestable without an adapter.

Registered overlays execute in normalized priority order through
`CompositeRiskOverlay`. Paper/live construction uses the same selected IDs
and parameters. An old persisted ID that is absent from the current registry
is displayed as unavailable/blocked and causes startup validation to fail
closed; it is never silently replaced.
