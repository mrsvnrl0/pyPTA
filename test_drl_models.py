"""Real bundled DRL artifacts: independent validation and regression failures."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.drl_models import validate_manifest, FAMILY
from adaptive_crypto.neural_models import CANDIDATE_ROOT, _manifest, model_catalog, load_selected_model


def reidentify(m):
    operational = {k: v for k, v in m.items() if k not in ('artifact_id', 'report', 'report_sha256')}
    m['artifact_id'] = hashlib.sha256(json.dumps(operational, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return FAMILY + '@' + m['artifact_id'][:12]


class DRLArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = sorted((CANDIDATE_ROOT / FAMILY).glob('*/manifest.json'))
        if not paths:
            raise unittest.SkipTest('Trained DRL bundle is not installed')
        cls.source = paths[0].parent
        cls.manifest = json.loads(paths[0].read_text())
        cls.model_id = FAMILY + '@' + cls.manifest['artifact_id'][:12]

    def mutated(self, mutate):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'bundle'
            shutil.copytree(self.source, bundle)
            m = copy.deepcopy(self.manifest)
            mutate(m, bundle)
            model_id = reidentify(m)
            (bundle / 'manifest.json').write_text(json.dumps(m))
            with self.assertRaises(DataError):
                validate_manifest(bundle, model_id)

    def test_real_bundle_and_old_ensemble_contract_remain_valid(self):
        m, _ = validate_manifest(self.source, self.model_id)
        self.assertEqual(set(m['members']), {'BTC', 'ETH', 'SOL', 'SUI'})
        self.assertIsNone(m['target_horizon_bars'])
        for path in (CANDIDATE_ROOT / 'probability_ensemble_v1').glob('*/manifest.json'):
            ensemble, _ = _manifest(path.parent, 'probability_ensemble_v1@' + path.parent.name)
            self.assertEqual(len(ensemble['components']), 4)

    def test_tampered_weights_are_rejected(self):
        def change(m, p):
            with (p / m['members']['BTC']['onnx']).open('ab') as out:
                out.write(b'changed')
        self.mutated(change)

    def test_future_base_selection_and_unearned_recommendation_rejected(self):
        self.mutated(lambda m, p: m.update(base_selection_end_ms=m['splits']['fit_start_ms']))
        self.mutated(lambda m, p: m.update(recommendation='recommended'))
        self.mutated(lambda m, p: m.update(classes=['SELL', 'HOLD', 'BUY']))
        self.mutated(lambda m, p: m['base_model'].update(manifest_sha256='f' * 64))

    def test_rehashed_report_cannot_disguise_untrained_policy(self):
        def change(m, p):
            r = json.loads((p / m['report']).read_text())
            r['per_asset']['BTC']['seed_runs'][0]['timesteps'] = 0
            raw = json.dumps(r).encode()
            (p / m['report']).write_bytes(raw)
            m['report_sha256'] = hashlib.sha256(raw).hexdigest()
        self.mutated(change)

    @unittest.skipUnless(importlib.util.find_spec('onnxruntime'), 'CPU runtime unavailable')
    def test_rules_catalog_and_selection_preserve_independent_limits(self):
        rules = Rules(nn_model_id=self.model_id, nn_limitations=False)
        rules.validate()
        model = load_selected_model(rules)
        self.assertFalse(rules.nn_limitations)
        self.assertEqual(model.model_id, self.model_id)
        entry = next(row for row in model_catalog() if row['id'] == self.model_id)
        self.assertTrue(entry['available'])
        self.assertIn('confirmation pending', entry['status'])
        with self.assertRaises(DataError):
            model.trained_through_ms_for('UNSUPPORTED')
