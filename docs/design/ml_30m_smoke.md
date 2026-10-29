# 30m LightGBM smoke strategy

`LightGBM30mSmokeStrategy` is a lifecycle smoke target for the supported 30m
path. LightGBM is a hard runtime dependency: strategy construction fails with
an explicit error when the `lightgbm` package is unavailable.

The strategy must not substitute a deterministic score, complete a backtest,
or produce a shadow/paper result when that dependency is missing. Runtime and
backtest failures are persisted and exposed through the runtime status and
system risk-event UI.

Install the optional dependency with the `ml30m` extra before using the
strategy:

```text
poetry install --extras ml30m
```
