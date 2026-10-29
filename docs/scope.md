# Community Edition scope

The current tree is the Community Edition (CE). It is released under the MIT
License. The current distribution name is `millionaire-ce`.

## Included

- Backtesting, paper trading, and simulation runtime paths
- Generic broker, market-data, strategy, and `RiskStrategy` contracts
- The generic risk-overlay lifecycle (`RiskOverlay` protocol and
  `CompositeRiskOverlay`) and explicit `RiskProgramAdapter`/registry
  extension point
- Generic custom adapter helpers such as `register_risk_program`,
  `available_risk_programs`, `normalize_risk_programs`,
  `build_backtest_risk_overlay`, and `build_live_risk_strategies`
- Example strategies and local development stubs
- Local web UI, initialization flow, and developer documentation

## Deliberately absent from CE

- Private risk-program implementations or private risk-program IDs
- Built-in triple-barrier, pullback-sell, or cost-stop programs (and their
  tests): the CE default registry is empty and ships no program
- Production/live trading controls and live account UI
- A bundled native installer or OS service registration

Live trading is hidden and unavailable when the active edition is CE,
including when an old local configuration contains gateway or live-account
settings. Paper/simulation mode is the supported CE operational mode. CE does
not silently replace an old risk-program ID: `normalize_risk_programs` rejects
an unknown or private ID, and an adapter must be explicitly registered by the
host before it can be selected.

## Extension boundary

Applications may register their own risk adapters through
`quantide.service.risk_programs`. The CE package supplies the generic
interfaces and lifecycle only; its module-level registry starts empty and it
does not import or discover private risk implementations. See
[Risk program boundary](design/risk_program_selection.md) for the data and
validation contract.

## Local-use status

The repository is still undergoing architectural refactoring. The local setup
and paper-mode flow are the actionable parts of this first release groundwork.
See [Installation](installation.md) and
[Developer acceptance](developer-acceptance.md) for commands.
