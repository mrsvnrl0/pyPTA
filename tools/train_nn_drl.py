"""Bounded offline PPO over frozen NN scores; never edits live settings/state.

Run with requirements-drl-training.txt in an isolated environment. This study
reuses an observed historical period and cannot establish a fresh winner.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from adaptive_crypto.core import H4
from adaptive_crypto.neural_models import CandidateModel, candidate_bundle_path, _manifest
from adaptive_crypto.candidate_features import FEATURE_NAMES, FEATURE_SCHEMA
from tools.candidate_train_pipeline import read_data, asset_samples, sha256_path, softmax
from tools.evaluate_nn_candidates import read_bars, read_experiment, replay, _buy_and_hold

FAMILY = 'drl_ppo_classifier_v1'
SEEDS = (11, 23, 37)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def save(path, value):
    Path(path).write_bytes(json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode() + b'\n')


def observations(features, probabilities, mean, scale):
    return np.concatenate((np.clip((features.astype(np.float64) - mean) / scale, -10, 10),
                           probabilities), axis=1).astype(np.float32)


def prepare(args):
    base_path = candidate_bundle_path(args.base_model)
    base_manifest, base_raw = _manifest(base_path, args.base_model)
    if base_manifest['id'] == 'probability_ensemble_v1':
        raise ValueError('First DRL protocol requires one frozen single-network base')
    base = CandidateModel(args.base_model, base_path)
    segments, audit, source = read_data(args.data, tuple(sorted(base.members)))
    original = read_experiment(Path(args.base_run) / 'experiment.json')
    if any(audit[a]['dataset_sha256'] != base.members[a]['dataset_sha256'] for a in base.members):
        raise ValueError('DRL source differs from base NN verified training history')
    # The base NN selected its seed/epoch using validation up to this boundary.
    # Its later evaluation/promotion cutoff is distinct; all this reuse is
    # explicitly retrospective, not fresh out-of-sample performance evidence.
    first = base_manifest['splits']['test_start_ms']
    end = base_manifest['splits']['test_end_ms'] - H4
    count = (end - first) // H4
    val = first + int(count * .6) * H4
    test = first + int(count * .8) * H4
    code_files = ('tools/train_nn_drl.py', 'tools/drl_environment.py',
                  'tools/candidate_train_pipeline.py', 'adaptive_crypto/candidate_features.py',
                  'tools/evaluate_nn_candidates.py')
    protocol = {
        'schema': 'nn-assisted-drl-protocol-v1', 'base_model_id': args.base_model,
        'base_manifest_sha256': hashlib.sha256(base_raw).hexdigest(),
        'base_selection_end_ms': first - 1,
        'data_sha256': {a: audit[a]['dataset_sha256'] for a in base.members},
        'source_provenance_sha256': sha256_path(Path(args.data) / 'provenance.json'),
        'code_sha256': {p: sha256_path(p) for p in code_files},
        'asset_order': list(original['asset_order']), 'classes': ['BUY', 'HOLD', 'SELL'],
        'fit_start_ms': first, 'validation_start_ms': val, 'test_start_ms': test,
        'test_end_ms': end, 'evidence_kind': 'retrospective_research',
        'prior_results_observed': True, 'recommendation': 'no demonstrated winner',
        'observation': 'fit-scaled clipped[-10,10] final causal OHLCV16 row + frozen base NN BUY/HOLD/SELL scores',
        'actor_information': 'market-only reactive policy; holdings hidden; partially observed',
        'training_environment': 'independent per-asset long-only cash; limitations OFF surrogate',
        'evaluation_environment': 'existing ordered shared-cash portfolio replay, limitations ON/OFF, base/stress',
        'reward': '100 * log(net-liquidation-equity_after / before); includes spread, fees, slippage',
        'episode_bars': 128, 'policy': 'PPO MLP actor/critic each 64x64 Tanh',
        'seeds': list(SEEDS), 'timesteps_per_seed': args.timesteps,
        'learning_rate': .0003, 'gamma': .99, 'gae_lambda': .95, 'clip_range': .2,
        'n_steps': 256, 'batch_size': 64, 'n_epochs': 10, 'ent_coef': .01,
        'max_cpu_threads': 2, 'seed_selection': 'highest validation net return; smaller seed tie',
        'costs': {k: original[k] for k in ('fee_rate', 'slippage_rate', 'spread_bps')},
        'primary_limitations_enabled': original['primary_limitations_enabled'],
        'max_training_seconds': 3600,
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'protocol.json'
    if path.exists() and json.loads(path.read_text()) != protocol:
        raise ValueError('Protocol/data/code changed; use a new run directory')
    if not path.exists():
        save(path, protocol)
    all_data = {}
    for asset in protocol['asset_order']:
        samples = asset_samples(segments[asset])
        # Each observation at t uses execution bar t+1 entirely inside its own
        # partition, including its reward close; no transition crosses a split.
        times = samples.signal_ends - H4 + 1
        keep = (times >= first) & (times < end - H4)
        times, windows = times[keep], samples.x[keep]
        bars = {c.t: c for segment in segments[asset] for c in segment}
        if len(times) != (end - H4 - first) // H4 or np.any(np.diff(times) != H4):
            raise ValueError(f'{asset} policy period has a gap; no interpolation')
        member = base.members[asset]
        base_x = ((windows - np.asarray(member['mean'], np.float32)) /
                  np.asarray(member['scale'], np.float32)).astype(np.float32)
        logits = np.concatenate([base.sessions[asset].run(['logits'], {'features': row[None]})[0]
                                 for row in base_x])
        probs = softmax(logits)
        masks = {
            'fit': (times < val - H4),
            'validation': (times >= val) & (times < test - H4),
            'test': (times >= test) & (times < end - H4),
        }
        raw = windows[:, -1, :]
        mean = raw[masks['fit']].astype(np.float64).mean(axis=0)
        scale = raw[masks['fit']].astype(np.float64).std(axis=0)
        scale = np.where(scale > 1e-8, scale, 1.)
        x = observations(raw, probs, mean, scale)
        all_data[asset] = {'x': x, 'base': probs, 'times': times, 'masks': masks,
                           'mean': mean.tolist(), 'scale': scale.tolist(),
                           'open': np.asarray([bars[int(t) + H4].o for t in times]),
                           'close': np.asarray([bars[int(t) + H4].c for t in times])}
        print(f'Prepared {asset}: ' + str({k: int(v.sum()) for k, v in masks.items()}), flush=True)
    return protocol, all_data, base_manifest, original


def environment(data, split, costs, random_start):
    from tools.drl_environment import MarketSignalEnv
    m = data['masks'][split]
    return MarketSignalEnv(data['x'][m], data['open'][m], data['close'][m],
                           fee=costs['fee_rate'], slippage=costs['slippage_rate'],
                           spread_bps=costs['spread_bps'], random_start=random_start,
                           episode_bars=128 if random_start else int(m.sum()))


def validate_policy(model, env):
    obs, _ = env.reset(seed=11)
    while True:
        action, _ = model.predict(obs, deterministic=True)
        obs, _, done, truncated, info = env.step(int(action))
        if done or truncated:
            return info


def run(args):
    import torch
    import onnx
    import onnxruntime as ort
    import stable_baselines3 as sb3
    import gymnasium
    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback
    torch.set_num_threads(2)
    protocol, data, base_manifest, original = prepare(args)
    output = Path(args.output)
    protocol_hash = sha256_path(output / 'protocol.json')
    budget_file = output / 'budget.json'
    consumed = json.loads(budget_file.read_text())['seconds'] if budget_file.exists() else 0.
    started = time.monotonic()

    class Budget(BaseCallback):
        def _on_step(self):
            elapsed = consumed + time.monotonic() - started
            if self.num_timesteps % 256 == 0:
                save(budget_file, {'seconds': elapsed})
            return elapsed < protocol['max_training_seconds']

    class Actor(torch.nn.Module):
        def __init__(self, policy):
            super().__init__()
            self.body = policy.mlp_extractor.policy_net
            self.head = policy.action_net

        def forward(self, observation):
            return self.head(self.body(observation))

    bundle = output / 'bundle'
    bundle.mkdir(exist_ok=True)
    members, per_asset, policy_probs = {}, {}, {}
    for asset, values in data.items():
        runs = []
        for seed in SEEDS:
            checkpoint = output / f'{asset}.{seed}.zip'
            evidence_path = output / f'{asset}.{seed}.json'
            if checkpoint.exists() and evidence_path.exists():
                record = json.loads(evidence_path.read_text())
                if record['protocol_sha256'] != protocol_hash or record['checkpoint_sha256'] != sha256_path(checkpoint):
                    raise ValueError('Changed DRL seed checkpoint/evidence')
            else:
                env = environment(values, 'fit', protocol['costs'], True)
                model = PPO('MlpPolicy', env, seed=seed, device='cpu', verbose=0,
                            learning_rate=.0003, n_steps=256, batch_size=64, n_epochs=10,
                            gamma=.99, gae_lambda=.95, clip_range=.2, ent_coef=.01,
                            policy_kwargs={'net_arch': {'pi': [64, 64], 'vf': [64, 64]}})
                before = torch.cat([p.detach().flatten() for p in model.policy.parameters()]).clone()
                model.learn(total_timesteps=protocol['timesteps_per_seed'], callback=Budget())
                if model.num_timesteps < protocol['timesteps_per_seed']:
                    raise RuntimeError('DRL training budget exhausted; no partial candidate published')
                delta = float(torch.linalg.vector_norm(torch.cat([p.detach().flatten() for p in model.policy.parameters()]) - before))
                if not np.isfinite(delta) or delta <= 0:
                    raise RuntimeError('PPO weights did not learn')
                validation = validate_policy(model, environment(values, 'validation', protocol['costs'], False))
                model.save(checkpoint)
                record = {'seed': seed, 'timesteps': model.num_timesteps, 'updates': model._n_updates,
                          'parameter_delta_l2': delta, 'validation': validation,
                          'protocol_sha256': protocol_hash, 'checkpoint_sha256': sha256_path(checkpoint)}
                save(evidence_path, record)
            runs.append(record)
            print(f"{asset} seed={seed} validation return={record['validation']['equity'] - 1:.4f}", flush=True)
        best = min(runs, key=lambda r: (-r['validation']['equity'], r['seed']))
        model = PPO.load(output / f"{asset}.{best['seed']}.zip", device='cpu')
        actor = Actor(model.policy).eval()
        model_path = bundle / f'{asset}.onnx'
        with torch.inference_mode():
            torch.onnx.export(actor, (torch.from_numpy(values['x'][:1]),), str(model_path),
                              input_names=['observation'], output_names=['logits'], opset_version=17,
                              dynamo=False, do_constant_folding=True)
        onnx.checker.check_model(str(model_path))
        session = ort.InferenceSession(model_path.read_bytes(), providers=['CPUExecutionProvider'])
        indices = np.linspace(0, len(values['x']) - 1, 24, dtype=int)
        with torch.inference_mode():
            native = actor(torch.from_numpy(values['x'][indices])).numpy()
        actual = np.concatenate([session.run(['logits'], {'observation': values['x'][i:i+1]})[0] for i in indices])
        difference = float(np.max(np.abs(native - actual)))
        pdifference = float(np.max(np.abs(softmax(native) - softmax(actual))))
        if difference > 1e-4 or pdifference > 1e-5:
            raise RuntimeError('DRL export parity failed')
        logits = np.concatenate([session.run(['logits'], {'observation': row[None]})[0] for row in values['x']])
        policy_probs[asset] = softmax(logits)
        members[asset] = {'onnx': model_path.name, 'sha256': sha256_path(model_path),
                          'mean': values['mean'], 'scale': values['scale'],
                          'trained_through_ms': protocol['validation_start_ms'] - 1,
                          'dataset_sha256': protocol['data_sha256'][asset], 'selected_seed': best['seed']}
        per_asset[asset] = {'seed_runs': runs, 'selected_seed': best['seed'],
                            'sample_counts': {k: int(v.sum()) for k, v in values['masks'].items()},
                            'export': {'onnx_sha256': sha256_path(model_path), 'max_abs_logits_difference': difference,
                                       'max_abs_probability_difference': pdifference}}
    bars = read_bars(Path(args.base_run) / 'combined_candles_4h.csv', H4)
    spec = {**original, 'test_start_ms': protocol['test_start_ms'], 'test_end_ms': protocol['test_end_ms'],
            'development_cutoff_ms': protocol['test_start_ms'] - 1}
    evaluation = {}
    for name in ('drl', 'base_nn'):
        predictions = {}
        for asset, values in data.items():
            probs = policy_probs[asset] if name == 'drl' else values['base']
            for t, p in zip(values['times'], probs):
                if protocol['test_start_ms'] <= t < protocol['test_end_ms'] - H4:
                    predictions[asset, int(t) + H4 - 1] = {'signal_end_ms': int(t) + H4 - 1,
                                                        'label_index': int(p.argmax())}
        save(output / f'{name}.test_predictions.json', [dict(asset=a, **v) for (a, _), v in predictions.items()])
        evaluation[name] = {f"{'limited' if limited else 'unlimited'}_{'stress' if stress else 'base'}":
                            replay(bars, predictions, spec, limitations=limited, stress=stress, interval=H4)
                            for limited in (False, True) for stress in (False, True)}
    evaluation['buy_and_hold'] = _buy_and_hold(bars, spec, interval=H4)
    evaluation['cash_no_trade'] = {'net_return': 0.}
    save(output / 'evaluation.json', evaluation)
    manifest = {
        'schema': 'nn-assisted-drl-v1', 'id': FAMILY, 'classes': ['BUY', 'HOLD', 'SELL'],
        'feature_schema': FEATURE_SCHEMA, 'feature_names': list(FEATURE_NAMES),
        'input_shape': [1, 19], 'output_shape': [1, 3], 'history_bars': 319, 'timeframe_ms': H4,
        'target_horizon_bars': None, 'output_semantics': 'policy_action_probabilities',
        'base_model': {'model_id': args.base_model, 'manifest_sha256': protocol['base_manifest_sha256']},
        'base_selection_end_ms': protocol['base_selection_end_ms'],
        'splits': {k: protocol[k] for k in ('fit_start_ms', 'validation_start_ms', 'test_start_ms', 'test_end_ms')},
        'latest_model_selection_through_ms': max(base_manifest['latest_model_selection_through_ms'], protocol['test_end_ms'] - 1),
        'members': members, 'experiment_sha256': protocol_hash,
        'dataset_provenance_sha256': protocol['source_provenance_sha256'],
        'observation_clip': 10., 'policy': protocol['policy'], 'reward': protocol['reward'],
        'evidence_kind': 'retrospective_research', 'recommendation': 'no demonstrated winner',
        'inference_runtime': {'name': 'onnxruntime', 'version_validated': ort.__version__,
                              'execution_provider': 'CPUExecutionProvider', 'opset': 17},
    }
    manifest['artifact_id'] = hashlib.sha256(canonical(manifest)).hexdigest()
    report = {'schema': 'nn-assisted-drl-report-v1', 'architecture_id': FAMILY,
              'artifact_id': manifest['artifact_id'], 'technical_status': 'trained_export_parity_passed',
              'recommendation': 'no demonstrated winner', 'evidence_kind': 'retrospective_research',
              'prospective_confirmation': 'pending; new frozen forward evidence required',
              'protocol': protocol, 'protocol_sha256': protocol_hash, 'per_asset': per_asset,
              'evaluation_sha256': sha256_path(output / 'evaluation.json'),
              'evaluation': {n: {k: {a: b for a, b in v.items() if a not in ('trades', 'paired_equity_4h')}
                                for k, v in cases.items()} for n, cases in evaluation.items() if n in ('drl', 'base_nn')},
              'limitations': ['Reactive policy with no holdings input; partially observed.',
                              'Per-asset OFF training surrogate differs from shared-cash portfolio execution.',
                              'ON mode is evaluated but not the training objective.',
                              'Historical data already examined; no untouched confirmation.',
                              '4H open/close synthetic bid/ask proxy; no verified intrabar fill times.'],
              'versions': {'torch': torch.__version__, 'stable_baselines3': sb3.__version__,
                           'gymnasium': gymnasium.__version__, 'onnxruntime': ort.__version__}}
    save(bundle / 'report.json', report)
    manifest.update(report='report.json', report_sha256=sha256_path(bundle / 'report.json'))
    save(bundle / 'manifest.json', manifest)
    save(budget_file, {'seconds': consumed + time.monotonic() - started})
    print(json.dumps({'bundle': str(bundle), 'model_id': FAMILY + '@' + manifest['artifact_id'][:12],
                      'recommendation': report['recommendation']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default='.qa/candidate_data/kraken_2026q2_plus_rest')
    parser.add_argument('--base-run', default='.qa/candidate_runs/final_v1')
    parser.add_argument('--base-model', default='grouped_attention_lstm_v1@2e29d734b5b6')
    parser.add_argument('--output', required=True)
    parser.add_argument('--timesteps', type=int, default=16384)
    args = parser.parse_args()
    if args.timesteps < 16384 or args.timesteps > 65536 or args.timesteps % 256:
        parser.error('Use 16384..65536 steps per seed, a multiple of 256')
    run(args)


if __name__ == '__main__':
    main()
