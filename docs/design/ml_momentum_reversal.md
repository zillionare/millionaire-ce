# MLMomentumReversalStrategy decision contract

`MLMomentumReversalStrategy` is a daily cross-sectional selector.  On each
decision date its target portfolio is exactly the current `top_k` selection:

1. a holding that is still in `top_k` is kept;
2. an available holding absent from `top_k` is fully sold and realized; and
3. every selected asset that is not held is bought.

The strategy does not use fixed holding days, price barriers, or any
built-in risk program. Generic `RiskStrategy` and the backtest risk-overlay
framework remain available for strategies that explicitly opt into a
host-registered adapter.

## Training target

The model is supervised with the explicitly named
`future_n_trading_day_close_return` target:

```text
close[t + N trading days] / close[t] - 1
```

The target is created only inside model training.  Training input is cut off
strictly before the signal date, so the signal-day row cannot use a future
close.  `future_return_horizon` controls `N` for this label; it is a modeling
parameter and does not define a sell or holding policy.

## CE packaging note

CE retains the generic `RiskStrategy` interface and backtest risk-overlay
framework, but contains no private risk-program implementation. Private
learner packages register their own adapters through the generic risk-program
registry when installed. With no registered adapters, risk selection is empty;
persisted IDs that cannot be resolved are unavailable/blocked rather than
silently replaced.
