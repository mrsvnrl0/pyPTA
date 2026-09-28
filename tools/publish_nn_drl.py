"""Install a completely validated DRL research bundle without applying Settings."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from adaptive_crypto.drl_models import DRLModel, FAMILY
from adaptive_crypto.neural_models import CANDIDATE_ROOT


def publish(run):
    run = Path(run).resolve(strict=True)
    source = run / 'bundle'
    m = json.loads((source / 'manifest.json').read_text())
    model_id = FAMILY + '@' + m['artifact_id'][:12]
    model = DRLModel(model_id, source)
    parity = json.loads((run / 'full_runtime_parity.json').read_text())
    if (parity.get('status') != 'complete' or parity.get('manifest_sha256') != model.identity
            or parity.get('model_id') != model_id or set(parity.get('assets', {})) != set(model.members)
            or any(v.get('samples', 0) < 12 or not 0 <= v.get('max_abs_probability_difference', float('inf')) <= 1e-5
                   for v in parity['assets'].values())):
        raise ValueError('Publish requires complete native/full-adapter parity for this manifest')
    names = ['manifest.json', m['report'], *(member['onnx'] for member in m['members'].values())]
    parent = CANDIDATE_ROOT / FAMILY
    parent.mkdir(exist_ok=True)
    target = parent / m['artifact_id'][:12]
    if target.exists():
        if any((target / n).read_bytes() != (source / n).read_bytes() for n in names):
            raise ValueError('Existing immutable DRL version differs; refuse overwrite')
        DRLModel(model_id, target)
        return target
    with tempfile.TemporaryDirectory(prefix='.drl-stage-', dir=parent) as temporary:
        staging = Path(temporary) / 'bundle'
        staging.mkdir()
        for name in names:
            shutil.copyfile(source / name, staging / name)
        DRLModel(model_id, staging)
        staging.rename(target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({'installed_bundle': str(publish(args.run)), 'settings_changed': False}))
