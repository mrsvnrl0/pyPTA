"""Strict, immutable NN-assisted PPO adapter; no training dependencies at runtime."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .core import DataError, H4
from .neural import CLASSES

FAMILY = 'drl_ppo_classifier_v1'
ENSEMBLE_FAMILY = 'drl_ensemble_ppo_v1'
FAMILIES = (FAMILY, ENSEMBLE_FAMILY)


def _ensemble_base_contract(base, *, check_weights):
    """Resolve the immutable component locks and their common source hashes."""
    from .neural_models import _manifest, candidate_bundle_path
    components = []
    for reference in base['components']:
        component, raw = _manifest(candidate_bundle_path(reference['model_id']),
                                   reference['model_id'], check_weights=check_weights)
        if (hashlib.sha256(raw).hexdigest() != reference['manifest_sha256']
                or component['artifact_id'] != reference['artifact_id']):
            raise DataError('DRL ensemble component changed')
        components.append(component)
    datasets = {}
    for asset in base['coverage']:
        hashes = {component['members'][asset]['dataset_sha256'] for component in components}
        if len(hashes) != 1:
            raise DataError('DRL ensemble components do not share the original source history')
        datasets[asset] = hashes.pop()
    return max(component['splits']['test_start_ms'] for component in components) - 1, datasets


def validate_manifest(path, expected_id, *, check_weights=True):
    from .neural_models import (_read_small, _hex_sha, _local_filename, _vector,
                                _manifest, candidate_bundle_path, FEATURE_SCHEMA,
                                MAX_MANIFEST_BYTES, MAX_MODEL_BYTES, MAX_REPORT_BYTES)
    from .candidate_features import FEATURE_NAMES
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        raise DataError('DRL bundle directory is unavailable')
    raw = _read_small(path / 'manifest.json', MAX_MANIFEST_BYTES)
    try:
        m = json.loads(raw)
        family = m.get('id')
        ensemble_assisted = family == ENSEMBLE_FAMILY
        schema_version = 'v2' if ensemble_assisted else 'v1'
        if (family not in FAMILIES or m.get('schema') != 'nn-assisted-drl-' + schema_version
                or not _hex_sha(m.get('artifact_id'))
                or expected_id != family + '@' + m['artifact_id'][:12]):
            raise DataError('Invalid DRL identity/schema')
        operational = {k: v for k, v in m.items() if k not in ('artifact_id', 'report', 'report_sha256')}
        digest = hashlib.sha256(json.dumps(operational, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if digest != m['artifact_id']:
            raise DataError('DRL identity does not cover its operational contract')
        if (m.get('classes') != list(CLASSES) or m.get('input_shape') != [1, 19]
                or m.get('output_shape') != [1, 3] or m.get('timeframe_ms') != H4
                or m.get('history_bars') != 319 or m.get('feature_schema') != FEATURE_SCHEMA
                or m.get('feature_names') != list(FEATURE_NAMES) or m.get('observation_clip') != 10.
                or m.get('target_horizon_bars') is not None
                or m.get('output_semantics') != 'policy_action_probabilities'
                or m.get('evidence_kind') != 'retrospective_research'
                or m.get('recommendation') != 'no demonstrated winner'):
            raise DataError('Incompatible DRL observation/action/evidence contract')
        if not _hex_sha(m.get('experiment_sha256')) or not _hex_sha(m.get('dataset_provenance_sha256')):
            raise DataError('DRL provenance hashes missing')
        splits = m['splits']
        keys = ('fit_start_ms', 'validation_start_ms', 'test_start_ms', 'test_end_ms')
        if (any(type(splits.get(k)) is not int for k in keys)
                or not 0 < splits[keys[0]] < splits[keys[1]] < splits[keys[2]] < splits[keys[3]]
                or type(m.get('base_selection_end_ms')) is not int
                or m['base_selection_end_ms'] >= splits['fit_start_ms']
                or type(m.get('latest_model_selection_through_ms')) is not int
                or m['latest_model_selection_through_ms'] < splits['test_end_ms'] - 1):
            raise DataError('DRL chronology includes future base-model selection or invalid splits')
        base_ref = m['base_model']
        base_families = {'probability_ensemble_v1'} if ensemble_assisted else {
            'lstm_classifier_v1', 'gru_classifier_v1', 'cnn_lstm_classifier_v1', 'grouped_attention_lstm_v1'}
        if not isinstance(base_ref, dict) or base_ref.get('model_id', '').partition('@')[0] not in base_families:
            raise DataError('DRL requires a supported immutable supervised base')
        base, base_raw = _manifest(candidate_bundle_path(base_ref['model_id']), base_ref['model_id'], check_weights=check_weights)
        if ensemble_assisted:
            base_selection_end, base_datasets = _ensemble_base_contract(base, check_weights=check_weights)
            base_coverage = set(base['coverage'])
        else:
            base_selection_end = base['splits']['test_start_ms'] - 1
            base_datasets = {asset: member['dataset_sha256'] for asset, member in base['members'].items()}
            base_coverage = set(base['members'])
        if (hashlib.sha256(base_raw).hexdigest() != base_ref.get('manifest_sha256')
                or base_selection_end != m['base_selection_end_ms']
                or m['latest_model_selection_through_ms'] < base['latest_model_selection_through_ms']):
            raise DataError('DRL base model changed or its selection cutoff is inconsistent')
        runtime = m['inference_runtime']
        if (runtime.get('name') != 'onnxruntime' or runtime.get('execution_provider') != 'CPUExecutionProvider'
                or runtime.get('opset') != 17 or not isinstance(runtime.get('version_validated'), str)
                or not runtime['version_validated']):
            raise DataError('Invalid DRL CPU runtime contract')
        members = m['members']
        if (not isinstance(members, dict) or not members or not set(members).issubset(base_coverage)
                or ensemble_assisted and set(members) != base_coverage):
            raise DataError('DRL trained coverage differs from base')
        names = set()
        for asset, member in members.items():
            filename = _local_filename(member['onnx'])
            if (not filename.endswith('.onnx') or filename in names or not _hex_sha(member.get('sha256'))
                    or (not _hex_sha(member.get('dataset_sha256')) if ensemble_assisted else
                        member.get('dataset_sha256') != base_datasets[asset])
                    or member.get('selected_seed') not in (11, 23, 37)
                    or type(member.get('trained_through_ms')) is not int
                    or member['trained_through_ms'] != splits['validation_start_ms'] - 1):
                raise DataError('Invalid DRL member provenance')
            names.add(filename)
            _vector(member['mean'], 'DRL mean')
            _vector(member['scale'], 'DRL scale', positive=True)
            if check_weights and hashlib.sha256(_read_small(path / filename, MAX_MODEL_BYTES)).hexdigest() != member['sha256']:
                raise DataError('DRL weights do not match manifest')
        report_name = _local_filename(m['report'])
        if not report_name.endswith('.json') or not _hex_sha(m.get('report_sha256')):
            raise DataError('Invalid DRL report reference')
        # V2 source extensions are part of the report-bound protocol, so even
        # metadata-only validation must check that evidence and its hashes.
        if check_weights or ensemble_assisted:
            report_raw = _read_small(path / report_name, MAX_REPORT_BYTES)
            if hashlib.sha256(report_raw).hexdigest() != m['report_sha256']:
                raise DataError('DRL report hash mismatch')
            r = json.loads(report_raw)
            if (r.get('schema') != 'nn-assisted-drl-report-' + schema_version or r.get('artifact_id') != m['artifact_id']
                    or r.get('architecture_id') != family or r.get('protocol_sha256') != m['experiment_sha256']
                    or hashlib.sha256(json.dumps(r['protocol'], indent=2, sort_keys=True, allow_nan=False).encode() + b'\n').hexdigest() != m['experiment_sha256']):
                raise DataError('DRL report identity mismatch')
            protocol = r['protocol']
            if (protocol.get('base_model_id') != base_ref['model_id']
                    or protocol.get('base_manifest_sha256') != base_ref['manifest_sha256']
                    or protocol.get('evidence_kind') != 'retrospective_research'
                    or protocol.get('prior_results_observed') is not True
                    or any(protocol.get(k) != splits[k] for k in keys)):
                raise DataError('DRL protocol differs from manifest')
            if ensemble_assisted:
                snapshot_hashes = protocol.get('data_sha256')
                if (protocol.get('schema') != 'nn-assisted-drl-protocol-v2'
                        or protocol.get('source_extension_verified') is not True
                        or protocol.get('base_dataset_sha256') != base_datasets
                        or protocol.get('base_selection_end_ms') != base_selection_end
                        or protocol.get('source_provenance_sha256') != m['dataset_provenance_sha256']
                        or not isinstance(snapshot_hashes, dict) or set(snapshot_hashes) != base_coverage
                        or any(not _hex_sha(value) for value in snapshot_hashes.values())
                        or any(member['dataset_sha256'] != snapshot_hashes[asset]
                               for asset, member in members.items())):
                    raise DataError('DRL ensemble source-extension provenance is inconsistent')
            if (r.get('technical_status') != 'trained_export_parity_passed'
                    or r.get('evidence_kind') != 'retrospective_research'
                    or r.get('recommendation') != 'no demonstrated winner'
                    or set(r['per_asset']) != set(members) or not _hex_sha(r.get('evaluation_sha256'))):
                raise DataError('DRL trained/evaluation evidence incomplete')
            for asset, evidence in r['per_asset'].items():
                runs = evidence['seed_runs']
                export = evidence['export']
                if (len(runs) != 3 or {run['seed'] for run in runs} != {11, 23, 37}
                        or any(type(run['timesteps']) is not int or run['timesteps'] < 16384
                               or not math.isfinite(run['parameter_delta_l2']) or run['parameter_delta_l2'] <= 0
                               or run['protocol_sha256'] != m['experiment_sha256'] for run in runs)
                        or any(type(evidence['sample_counts'].get(k)) is not int or evidence['sample_counts'][k] < 100
                               for k in ('fit', 'validation', 'test'))
                        or evidence.get('selected_seed') != members[asset]['selected_seed']
                        or export.get('onnx_sha256') != members[asset]['sha256']
                        or any(type(export.get(k)) not in (int, float) or not math.isfinite(export[k])
                               or not 0 <= export[k] <= tol for k, tol in
                               (('max_abs_logits_difference', 1e-4), ('max_abs_probability_difference', 1e-5)))):
                    raise DataError('DRL fit/export evidence invalid')
            for name in ('drl', 'base_nn'):
                for case in ('limited_base', 'limited_stress', 'unlimited_base', 'unlimited_stress'):
                    ev = r['evaluation'][name][case]
                    if ev.get('status') != 'complete' or not math.isfinite(ev['net_return']):
                        raise DataError('DRL shared-cash comparison incomplete')
        return m, raw
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise DataError(f'Invalid DRL bundle: {exc}') from exc


class DRLModel:
    def __init__(self, model_id, path):
        import onnxruntime as ort
        from .neural_models import (CandidateModel, EnsembleModel, candidate_bundle_path,
                                    _read_small, MAX_MODEL_BYTES, FAMILIES as MODEL_NAMES)
        m, raw = validate_manifest(path, model_id)
        if ort.__version__ != m['inference_runtime']['version_validated']:
            raise DataError('DRL requires its validated ONNX Runtime CPU version')
        self.path, self.members, self.manifest = Path(path), m['members'], m
        self.model_id, self.architecture_id, self.artifact_id = model_id, m['id'], m['artifact_id']
        self.identity = hashlib.sha256(raw).hexdigest()
        self.trained_through_ms = m['latest_model_selection_through_ms']
        self.required_history_bars = 319
        base_class = EnsembleModel if self.architecture_id == ENSEMBLE_FAMILY else CandidateModel
        self.base = base_class(m['base_model']['model_id'], candidate_bundle_path(m['base_model']['model_id']))
        if self.base.identity != m['base_model']['manifest_sha256']:
            raise DataError('DRL base weights changed while loading')
        self.metadata = {'display_name': MODEL_NAMES[self.architecture_id], 'architecture_id': self.architecture_id,
                         'artifact_id': self.artifact_id, 'feature_count': 19, 'target_horizon': None,
                         'trained_through_ms': max(v['trained_through_ms'] for v in self.members.values()),
                         'live_eligibility_after_ms': self.trained_through_ms, 'coverage': sorted(self.members),
                         'report_url': f'/api/models/{model_id}/report',
                         'probability_semantics': 'policy_action', 'base_model_id': self.base.model_id}
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.sessions = {}
        for asset, member in self.members.items():
            data = _read_small(self.path / member['onnx'], MAX_MODEL_BYTES)
            if hashlib.sha256(data).hexdigest() != member['sha256']:
                raise DataError('DRL weights changed while loading')
            session = ort.InferenceSession(data, options, providers=['CPUExecutionProvider'])
            ins, outs = session.get_inputs(), session.get_outputs()
            if (len(ins) != 1 or ins[0].name != 'observation' or ins[0].shape != [1, 19]
                    or ins[0].type != 'tensor(float)' or len(outs) != 1 or outs[0].name != 'logits'
                    or outs[0].shape != [1, 3] or outs[0].type != 'tensor(float)'):
                raise DataError('Invalid DRL tensor contract')
            self.sessions[asset] = session

    def trained_through_ms_for(self, asset):
        if asset not in self.members:
            raise DataError(f'Selected DRL model has no trained artifact for {asset}')
        return self.trained_through_ms

    def predict(self, candles, asset):
        import numpy as np
        from .candidate_features import FEATURE_NAMES
        self.trained_through_ms_for(asset)
        base = self.base.predict(candles, asset)
        member = self.members[asset]
        raw = np.asarray([base['features'][k] for k in FEATURE_NAMES], dtype=np.float64)
        obs = np.concatenate((np.clip((raw - member['mean']) / member['scale'], -10, 10),
                              [base['probabilities'][k] for k in CLASSES])).astype(np.float32)[None]
        if not np.isfinite(obs).all():
            raise DataError('Nonfinite DRL observation')
        logits = np.asarray(self.sessions[asset].run(['logits'], {'observation': obs})[0], dtype=np.float64)
        if logits.shape != (1, 3) or not np.isfinite(logits).all():
            raise DataError('Invalid DRL policy logits')
        weights = np.exp(logits[0] - logits[0].max())
        probabilities = weights / weights.sum()
        return {'label': CLASSES[int(probabilities.argmax())], 'probabilities': dict(zip(CLASSES, map(float, probabilities))),
                'features': base['features'], 'signal_end': base['signal_end'], 'model_id': self.identity,
                'probability_semantics': 'policy_action', 'base_model_id': self.base.model_id,
                'base_label': base['label'], 'base_probabilities': base['probabilities']}
