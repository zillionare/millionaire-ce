# Edition feature boundaries

The source-tree package is the Community Edition (CE), distributed as
`millionaire-ce` and identified at runtime as the `millionaire-ce` edition.
Shared adapter boundaries may also be used by a separately maintained private
edition; that edition is outside this package and can still enable live
trading through the edition gate.

## CE behavior

- Live trading is unavailable. The live header/sidebar entries, live account
  widgets, live page, gateway settings page, and live deployment controls are
  omitted from the UI.
- Direct requests to live routes, live deployment endpoints, live account
  endpoints, and gateway settings return HTTP 503 with a clear CE-unavailable
  response. This boundary applies even when an old configuration database
  contains gateway or QMT settings.
- CE initialization keeps the shared wizard state machine but replaces the
  gateway-settings step with a paper/simulation information step. Submitted
  gateway fields cannot enable a live gateway in CE.
- Paper/simulation accounts and strategy runtimes remain available after
  initialization. Their quotes continue to come from `MarketDataHub`,
  including the existing Tencent quote publisher path.
- A persisted `live` runtime mode is not resumed: persisted live strategy
  runtimes are marked `status=blocked`, with `block_scope=edition`, and the
  gateway broker is never started. Generic gateway and live adapter modules
  remain in the tree as an adapter boundary; CE does not expose or register
  them.
- The risk-program registry starts empty. CE ships no built-in triple-barrier,
  pullback-sell, or cost-stop program; hosts must register a custom
  `RiskProgramAdapter` explicitly. Generic `RISK_OVERLAY` host declarations
  resolve only through that registry.
- A direct `deploy_to_live` call fails with
  `FeatureUnavailableError("CE edition 暂不支持实盘交易")` before resolving a
  backtest or touching a gateway broker.

The edition gate is implemented in `quantide.config.features`; use
`supports_live_trading()` or `require_live_trading()` at application/runtime
boundaries instead of deleting shared gateway internals.

## Private-edition contract tests

Gateway adapter and private live-runtime internals remain testable in the
shared source tree, but those tests must explicitly enable the development
edition override:

```bash
QUANTIDE_ENABLE_EDITION_OVERRIDE=1 \
QUANTIDE_EDITION=zillionaire \
pytest tests/e2e -m private_only
```

The normal CE test run excludes tests marked `private_only`. CE tests must
assert the unavailable exception or edition block and must not assert that a
live broker starts.
