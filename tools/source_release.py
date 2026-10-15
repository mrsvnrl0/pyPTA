"""Create a deterministic, allowlisted source archive without runtime data.

The maintainable archive contains the application and original Parente 5/2
artifacts. The working tree is authoritative; Git's index is neither read nor
changed. ZIP_STORED avoids compression-library drift.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXED_FILES = (
    ".gitignore", ".gitattributes", "README.md", "adaptive_crypto_dashboard.py",
    "adaptive_crypto_settings.example.json", "requirements.txt",
    "requirements-neural.txt", "requirements-runtime-lock.txt", "requirements-linux.txt",
    "build_windows_installers.ps1", "start_dashboard.ps1", "start_dashboard_background.ps1",
    "adaptive_crypto/models/NOTICE.md", "adaptive_crypto/models/parente_5_2.json",
    "adaptive_crypto/models/parente_5_2.npz",
    "docs/NEURAL_STRATEGY.md", "docs/SQLITE_PERSISTENCE.md", "docs/RELEASE_BASELINE.md",
    "docs/SMC_REACHABILITY.md", "docs/SMC_PRICE_GUIDE.md", "docs/perky_smc_port.md", "docs/DERIBIT_DATA.md",
    "scripts/pine/perky_smc_ultimate_v5.pine",
    "tests/__init__.py", "tests/neural_fixtures.py", "tests/ui_helpers.py", "tests/currency_fixtures.py",
    "tests/data/neural_author_btc.json", "restart_dashboard.ps1", "deploy/pypta.service",
    "packaging/windows/dashboard.ico", "packaging/windows/desktop.py",
    "packaging/windows/installer.nsi", "packaging/windows/pyPTA.spec",
    "packaging/windows/requirements-lock.txt", "packaging/windows/self_test_btc.json",
    "packaging/windows/version.txt", "packaging/windows/windows_launcher.py",
    "tools/source_release.py", "tools/prepare_windows_build.py", 
)
# Positive module/resource lists prevent extra source, private notes or removed
# subsystems from returning through broad package-directory globbing.
PACKAGE_MODULES = (
    "__init__", "__main__", "administration", "ai_prompts", "ai_providers", "backups",
    "binance_market_data", "binance_spot_data", "market_feed", "buy_watches", "cli", 
    "connection_credentials", "connection_settings", "connections", "core", "currency_conversion", "deribit",
    "deribit_settings", "deribit_market_data", "deribit_options", "dvol", "dvol_settings", "project_keys", "feed_diagnostics", "gex", "gex_currency", 
    "gex_targets", "holding_targets", "linux_credentials",
    "manual_paper", "market_brief", "market_brief_provider", "market_data", "neural",
    "neural_engine", "neural_input_contract", "neural_ledger", "neural_models", "neural_tools",
    "notifications", "paper_deletion", "persistence", "pine_alerts", "pine_guidance",
    "smc_position_context", "smc_runtime", "position_alerts", "position_guidance",
    "position_neural", "position_options", "positions", "restore", "runtime", "settings_editor",
    "spot_correction", "state_codec", "state_lock", "state_tools", "state_paths", "storage", "perky_smc",
    "take_profit_alerts", "trade_records", "web", "smc_reachability",
)
TEMPLATES = (
    "reachability", "buy_watches", "connection_settings", "dashboard_title", "deribit_settings", "feed_status",
    "dvol", "dvol_settings", "gex", "live_paper", "manual_paper_entry", "market_brief", "market_dashboard",
    "market_price_guide", "navigation", "neural_dashboard", "page_footer", "page_header",
    "paper_statistics", "paper_trade_table", "pine_chart", "pine_quality", "position_signals",
    "positions_page", "restore_controls", "settings", "signal_lights", "strategy_controls",
    "target_method", "target_selection", "trade_records",
)
STATIC_FILES = (
    "reachability.js", "connection_settings.js", "dashboard.css", "dashboard.js", "deribit_settings.js",
    "dvol.js", "dvol_settings.js", "dvol.css", "feed_status.css", "feed_status.js", "gex.js", "live_refresh.js", "market_brief.css",
    "market_brief.js", "pine_chart.css", "pine_chart.js", "positions.js", "restore.js", "settings.js",
)
SOURCE_GLOBS = ("tests/test_*.py", "packaging/windows/licenses/*.txt")
MANIFEST_NAME = "SOURCE_MANIFEST.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
PARENTE_SHA256 = "8907c6f395dc8cf385619f1cace6e27c3b23a04a820fe980cde65898cad56c87"


def _checked_file(root: Path, path: Path) -> Path:
    """Reject links/junctions rather than inadvertently package their targets."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError(f"Source links/reparse points are not allowed: {relative.as_posix()}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError(f"Source is not a regular file inside the root: {relative.as_posix()}")
    return path


def packaged_model_paths(root: Path) -> set[Path]:
    """Only the published Parente weights, metadata and license notice ship."""
    root = Path(root).resolve(strict=True)
    base = root / "adaptive_crypto" / "models"
    return {base / name for name in ("NOTICE.md", "parente_5_2.json", "parente_5_2.npz")}


def collect_sources(root: Path) -> dict[str, bytes]:
    """Read permitted sources and verify the unchanged published Parente model."""
    root = Path(root).resolve(strict=True)
    paths = {root / name for name in FIXED_FILES}
    optional = [*("adaptive_crypto/"+name+".py" for name in PACKAGE_MODULES),
                *("adaptive_crypto/templates/"+name+".html" for name in TEMPLATES),
                *("adaptive_crypto/static/"+name for name in STATIC_FILES)]
    paths.update(root / name for name in optional if (root / name).exists() or (root / name).is_symlink())
    for pattern in SOURCE_GLOBS:
        paths.update(root.glob(pattern))
    paths.update(packaged_model_paths(root))
    sources = {}
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        sources[name] = _checked_file(root, path).read_bytes()
    if hashlib.sha256(sources["adaptive_crypto/models/parente_5_2.npz"]).hexdigest() != PARENTE_SHA256:
        raise ValueError("Published Parente 5/2 model hash mismatch")
    if not any(name.startswith("tests/test_") and name.endswith(".py") for name in sources):
        raise ValueError("Source baseline requires its Python regression tests")
    return sources


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def release_bytes(sources: dict[str, bytes], label: str) -> tuple[bytes, bytes]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label):
        raise ValueError("Label must be 1-80 filename-safe letters, numbers, dots, underscores or hyphens")
    files = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for name, data in sorted(sources.items())]
    manifest = _json_bytes({
        "schema": 1, "product": "pyPTA", "label": label,
        "scope": "allowlisted working-tree source; no runtime data or Git index",
        "content_sha256": hashlib.sha256(_json_bytes(files)).hexdigest(),
        "files": files,
    })
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted({**sources, MANIFEST_NAME: manifest}.items()):
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    return output.getvalue(), manifest


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Output must be a regular file: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".source-release-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_release(sources: dict[str, bytes], output_dir: Path, label: str = "baseline") -> dict[str, str | int]:
    archive, manifest = release_bytes(sources, label)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"pyPTA-Source-{label}"
    archive_path = output_dir / (stem + ".zip")
    manifest_path = output_dir / (stem + ".manifest.json")
    checksum_path = output_dir / (stem + ".sha256")
    checksum = hashlib.sha256(archive).hexdigest()
    hashes = (f"{checksum}  {archive_path.name}\n"
              f"{hashlib.sha256(manifest).hexdigest()}  {manifest_path.name}\n").encode("ascii")
    # The checksums file is written last and acts as the completed-output marker.
    for path, data in ((archive_path, archive), (manifest_path, manifest), (checksum_path, hashes)):
        _atomic_write(path, data)
    return {"archive": str(archive_path), "manifest": str(manifest_path),
            "checksums": str(checksum_path), "sha256": checksum,
            "source_files": len(sources), "bytes": len(archive)}


def build_release(root: Path, output_dir: Path, label: str = "baseline") -> dict[str, str | int]:
    return write_release(collect_sources(root), output_dir, label)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release" / "source")
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    print(json.dumps(build_release(args.root, args.output_dir, args.label), indent=2))


if __name__ == "__main__":
    main()
