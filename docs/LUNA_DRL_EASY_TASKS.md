# Luna: bounded DRL documentation and UI verification

Workspace: `C:\Users\CxN\Documents\ChatGPT\pyPTA`.

This is a handoff for routine work requested by the user. It has not been sent to a Luna agent: this session's sub-agent interface does not expose Luna. The main implementation is being completed in the current task. Read the current source and results before doing these checks; do not assume the training run has finished merely because this file exists.

1. Read `docs/DRL_NN_ASSISTED.md`, `adaptive_crypto/drl_models.py` and the installed DRL bundle report. Check that documentation states which frozen supervised NN supplies the policy's inputs, its coverage, actual seed/timestep counts, and measured after-cost results. Compare numbers directly with `.qa/drl_runs/nn_assisted_v1/evaluation.json` when that local experiment is present. Correct transcription errors only.
2. Check that the UI calls DRL outputs policy action scores, distinguishes them from supervised class probabilities, and displays the research/confirmation-pending status. This policy is not a probability-of-profit estimator. Report wording issues or make small text-only corrections, preserving element hooks and behavior.
3. Run the existing disposable model-selector preview with temporary settings and ledgers, no scanner or credentials. Verify desktop/mobile layout, report links, Save -> Apply, all three consumer pages' applied model identity, and the unchanged limitations value. Do not use the production dashboard for mutation tests.
4. Report exact files/checks/results, with screenshot paths when created. Do not claim tests ran from historical counts. If the bundle, browser dependencies or result files are absent, name the missing prerequisite; do not fabricate it.

Do not change the algorithm, rewards, costs, data, splits, weights, model hashes, live settings or running processes. Do not retrain, select a live model, send Telegram messages, call paid AI services, or implement the separate paper-improvements handoff. Keep this assignment to documentation and disposable UI verification.
