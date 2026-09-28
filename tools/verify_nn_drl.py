"""Compare frozen native PPO actor with the complete CPU dashboard adapter."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import numpy as np


def main():
    import torch
    from stable_baselines3 import PPO
    from adaptive_crypto.drl_models import DRLModel
    from adaptive_crypto.neural_models import candidate_bundle_path
    from adaptive_crypto.candidate_features import FEATURE_NAMES
    from tools.candidate_train_pipeline import read_data, asset_samples, softmax, sha256_path
    from tools.train_nn_drl import observations, save
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--data', default='.qa/candidate_data/kraken_2026q2_plus_rest')
    args = parser.parse_args()
    manifest = json.loads((args.run / 'bundle/manifest.json').read_text())
    model_id = manifest['id'] + '@' + manifest['artifact_id'][:12]
    model = DRLModel(model_id, args.run / 'bundle')
    segments, audit, _ = read_data(args.data, tuple(model.members))
    result = {'model_id': model_id, 'manifest_sha256': model.identity, 'tolerance': 1e-5, 'assets': {}}
    for asset, member in model.members.items():
        if audit[asset]['dataset_sha256'] != member['dataset_sha256']:
            raise ValueError('Changed parity dataset')
        native = PPO.load(args.run / f"{asset}.{member['selected_seed']}.zip", device='cpu')
        windows = asset_samples(segments[asset])
        eligible = np.flatnonzero((windows.signal_ends >= manifest['splits']['test_start_ms']) &
                                  (windows.signal_ends < manifest['splits']['test_end_ms'] - 2 * 14400000))
        selected = eligible[np.linspace(0, len(eligible) - 1, 12, dtype=int)]
        differences, latency = [], []
        for i in selected:
            end = int(windows.signal_ends[i])
            history = next([c for c in segment if c.end <= end][-319:] for segment in segments[asset]
                           if segment[0].end <= end <= segment[-1].end)
            base_member = model.base.members[asset]
            x = ((windows.x[i:i+1] - np.asarray(base_member['mean'], np.float32)) /
                 np.asarray(base_member['scale'], np.float32)).astype(np.float32)
            base_probs = softmax(model.base.sessions[asset].run(['logits'], {'features': x})[0])
            obs = observations(windows.x[i:i+1, -1, :], base_probs, member['mean'], member['scale'])
            with torch.inference_mode():
                probs = native.policy.get_distribution(torch.from_numpy(obs)).distribution.probs.numpy()[0]
            start = time.perf_counter()
            live = model.predict(history, asset)
            latency.append((time.perf_counter() - start) * 1000)
            actual = np.asarray([live['probabilities'][k] for k in ('BUY', 'HOLD', 'SELL')])
            differences.append(float(np.max(np.abs(probs - actual))))
            if live['signal_end'] != end or live['label'] != ('BUY', 'HOLD', 'SELL')[int(probs.argmax())]:
                raise ValueError('Native/full adapter candle or action mismatch')
        if max(differences) > result['tolerance']:
            raise ValueError('Native/full runtime parity failed')
        result['assets'][asset] = {'samples': len(selected), 'max_abs_probability_difference': max(differences),
                                   'full_adapter_p50_ms': float(np.median(latency)),
                                   'full_adapter_p95_ms': float(np.percentile(latency, 95))}
    result['status'] = 'complete'
    save(args.run / 'full_runtime_parity.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
