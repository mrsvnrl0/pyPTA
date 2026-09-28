# NN-assisted deep reinforcement learning

Implemented 28 September 2026 as an **experimental selectable model**, not an automatically applied improvement. Trained model: `drl_ppo_classifier_v1@c86350f7f752`. The first experiment did **not** beat its frozen supervised base. The recommended action is to retain the existing active model.

## What it does

A separate PPO neural policy receives the final causal OHLCV16 feature row and the BUY/HOLD/SELL scores of the frozen attention-LSTM `grouped_attention_lstm_v1@2e29d734b5b6`. It learns BUY/HOLD/SELL actions from simulated changes in net-liquidation equity after fees, spread and slippage. This adds a reward-trained decision layer; it does not change the original attention-LSTM weights or claim to reinforce every installed model at once. The training command can target another supported single-network base in a separate immutable run.

The actor deliberately sees market information only. Cash and holdings affect simulated rewards but are hidden from the actor. This is a reactive, partially observed policy, preserving one deterministic signal per completed candle across Home, Paper Trading and Positions. It is not a fully observed portfolio optimizer. Training is per asset with limitations OFF; evaluation also runs the existing shared-cash, ordered-asset portfolio replay in both limitations modes. The difference is explicit in the report.

PPO alternates simulated rollouts with actor/critic updates. Its clipped objective is described in the [original PPO paper](https://arxiv.org/abs/1707.06347); the implementation uses the maintained [Stable-Baselines3 PPO implementation](https://stable-baselines3.readthedocs.io/en/v2.9.0/modules/ppo.html). [FinRL](https://arxiv.org/abs/2011.09607) provides relevant financial-RL context. None of those sources establishes profitability for this application. Trading actions need an explicit simulator, cost model and evaluation; DRL is not a switch that makes a supervised predictor more accurate.

## Actual training and chronology

- Genuine Kraken/USD historical H4 data, the same verified source files used by the existing candidates. BTC, ETH, SOL and SUI each have a separately fitted policy.
- Base NN seed/epoch selection ends before 22 January 2026 04:00 UTC. PPO never observes base scores before that boundary; it does not run a later-trained base over its earlier training period.
- PPO fit observations begin 22 January 2026 04:00 UTC; validation begins 20 June 2026 04:00 UTC; retrospective test begins 8 August 2026 20:00 UTC and ends 27 September 2026 16:00 UTC (exclusive).
- Every observation executes at the next bar's open and receives that bar's close reward. Transitions crossing split boundaries are excluded. Per asset there are 893 fit, 297 validation and 298 test observations. Feature scalers use fit observations only; standardized features are clipped to [-10,10]. There is no interpolation across missing bars.
- Three seeds (11, 23, 37) per asset, 16,384 PPO steps each: **12 actual fits, 196,608 simulated training steps**. Repeated historical episodes are not 196,608 independent market observations.
- Actor and critic each have two 64-unit Tanh layers. PPO uses learning rate 0.0003, discount 0.99, GAE 0.95, clipping 0.2, entropy coefficient 0.01, rollout 256, minibatch 64, 10 optimization epochs, CPU/two threads. Each seed's parameter change and update count are recorded. Validation net return selects the seed, with smaller seed breaking ties.
- Training reward is `100 * log(next net-liquidation equity / prior equity)`. Episodes have up to 128 transitions and explicit terminal liquidation. Every mark includes liquidation costs; closing the terminal position does not charge them a second time. BUY uses available simulated cash, HOLD retains the portfolio, SELL closes the long position. Invalid repeated BUY/empty SELL has no extra transaction cost.
- Training/export/evaluation execution recorded 227.47 seconds, excluding initial feature preparation and later full-adapter verification. The protocol caps training work at one hour and rejects partial fits for publication.

This entire historical period was already examined during the prior candidate study. It is **retrospective research**, despite chronological fit/validation/test separation within the new experiment. There is no untouched confirmation and no demonstrated winner. A new frozen forward experiment would be required before a performance recommendation; no background retraining or confirmation automation was created.

## Measured portfolio results

Same four assets, dates, fixed execution ordering, initial equity and costs for both models. Costs are 0.4% fee and 0.05% slippage per side plus 10 bps synthetic spread; stress doubles all three. Final positions are marked and liquidated, separately from natural exits.

| Model / limitations | Net return | Stress return | Max sampled drawdown | Natural exits | Terminal liquidations |
| --- | ---: | ---: | ---: | ---: | ---: |
| Base attention-LSTM / OFF | +33.95% | +32.62% | 25.98% | 0 | 1 |
| NN-assisted PPO / OFF | -15.29% | -24.12% | 18.49% | 11 | 0 |
| Base attention-LSTM / ON | -5.00% | -5.00% | 5.00% | 1 | 0 |
| NN-assisted PPO / ON | -7.32% | -11.14% | 8.96% | 11 | 0 |

The base's positive OFF result depends on a terminal liquidation and does not establish a generally better strategy. These numbers also differ from the older three-asset comparison because this replay includes SUI and uses a different, shorter period. The DRL policy's loss is evidence against claiming reinforcement improved this model. Keep the failure visible; do not tune against these test results until they turn positive and call that confirmation.

The simulator uses next-H4-open midpoint execution proxies and synthetic bid/ask. Stops in limited-mode evaluation are confirmed from completed bar ranges. Actual intrabar fills and missed earlier exits cannot be reconstructed from this H4 archive. The limitations checkbox, existing stops/costs, fresh quotes, execution window and candle deduplication remain controlled by the original execution code.

## Runtime and use

The bundle has four real CPU ONNX actors, fit-only scalers, frozen base-model hash, protocol/data hashes and a compact report. It contains no training pickle/checkpoint. The DRL loader has its own strict schema and tensor contract; the existing NPZ and supervised-candidate validators remain separate. The original four-member ensemble has an explicit component order so adding DRL cannot change its meaning.

Export parity checks 24 observations per asset. An independent full-adapter check compared 12 historical windows per asset (48 total) against native PPO action probabilities: maximum absolute error **1.02e-7**, below `1e-5`, with identical selected actions and candle timestamps. Full-adapter p50 per-asset latency was 8.14-8.73 ms and p95 8.60-10.46 ms on this Windows host, including frozen base inference and feature construction. These measurements exclude market fetching/scheduling.

Settings lists **NN-assisted DRL (PPO research)** after this source is loaded by the dashboard. Its status is “Trained research policy; confirmation pending.” Save -> Apply remains explicit and the current model is not changed by training or installing a bundle. Policy action scores are not supervised class probabilities or probabilities of profit. The policy is fixed at runtime: no live exploration, learning, automatic model selection or change to the limitations checkbox.

The main dashboard process has not been restarted as part of this research implementation. Use the existing backup/activation workflow before loading the new software into that process. Keep whichever model and limitations setting are actually applied during software activation; selecting the experimental DRL model is a separate user choice. Final read-only verification found the user had selected `probability_ensemble_v1@3801b1dabe20`, with limitations OFF. Both saved and applied state agree; neither was changed by this implementation. The prototype's frozen training input remains the explicitly named attention-LSTM above, not a dynamically swapped live model.

Verification completed: 805 Python tests, five skips (the three optional Gymnasium tests run separately and pass in the isolated DRL environment, plus two existing optional/platform skips); 53 browser tests passed. The successful disposable preview is `.qa/candidate-preview/2026-09-28T21-05-53-654Z-report.json`. It applies all six candidate versions in temporary state, confirms one applied model across Home/Paper Trading/Positions, preserves limitations OFF, checks report links and both 1280/390px layouts, and exits cleanly. The first preview passed UI assertions but failed Windows file cleanup; graceful server/store shutdown fixed the next run. The earlier temporary directory was left in place after automatic approval review rejected its deletion.

## Reproduce and verify

Run from `C:\Users\CxN\Documents\ChatGPT\pyPTA`. Training uses the isolated environment with `requirements-drl-training.txt`: Stable-Baselines3 2.9.0, Gymnasium 1.2.3, Torch 2.14.0, ONNX Runtime 1.30.0, plus the pinned candidate-training dependencies. The dashboard runtime needs only its existing ONNX dependency.

```powershell
.\.qa\candidate_venv\Scripts\python.exe -B -m tools.train_nn_drl --output .qa/drl_runs/nn_assisted_v1
.\.qa\candidate_venv\Scripts\python.exe -B -m tools.verify_nn_drl --run .qa/drl_runs/nn_assisted_v1
.\.venv\Scripts\python.exe -B -m tools.publish_nn_drl --run .qa/drl_runs/nn_assisted_v1
.\.qa\candidate_venv\Scripts\python.exe -B -m unittest test_drl_environment -q
.\.venv\Scripts\python.exe -B -m unittest test_drl_models -q
```

The training command rejects mismatching protocol/data/code hashes on resume. Use a fresh output directory for intentional recipe/code changes; do not overwrite old evidence. Publication checks the exact full-adapter parity report, validates staging and installs one immutable version without changing settings.

Local evidence: `.qa/drl_runs/nn_assisted_v1/protocol.json`, per-asset seed JSON/ZIP training checkpoints, `evaluation.json`, `full_runtime_parity.json`, and `bundle/`. The source release includes only allowed source/docs and the verified ONNX/JSON runtime bundle. Raw data, checkpoints, credentials, settings and trading records are excluded.

Routine follow-up instructions for Luna are in `docs/LUNA_DRL_EASY_TASKS.md`. That file is prepared, not dispatched: Luna was not exposed by the session's sub-agent tool.
