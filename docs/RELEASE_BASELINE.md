# Current release verification

Use `.venv/Scripts/python.exe -B -m unittest discover -s tests -p "test_*.py"` on Windows or `.venv/bin/python -B -m unittest discover -s tests -p "test_*.py"` on Linux. Tests are grouped in `tests/` and cover Parente features, shared Deribit inputs, Pine structure, normalized options, combined paper chronology and current storage/UI workflows.

`python tools/source_release.py --label parente-smc` produces an archive from an explicit source allowlist. It includes one immutable Parente artifact, current application modules, the original Pine script, current tests and documentation. It excludes state, credentials, caches, virtual environments and generated archives.

The Windows packaging sources use the same current module set. An installer requires a separate Windows build and release validation; source checks alone do not certify an installer. Ubuntu uses the same source with a service-owned virtual environment, deployment credential key and HTTPS configuration.
