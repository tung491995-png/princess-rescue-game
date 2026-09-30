#!/usr/bin/env python3
from pathlib import Path
import base64, gzip, importlib.util, subprocess, sys

root=Path(__file__).resolve().parent

# Harness-only dependency bootstrap. GitHub runners may install pip packages
# into a user-site directory that is not added to sys.path in an already-running
# interpreter. Install into a deterministic local target and prepend it.
deps=root/'.stage8_deps'
if importlib.util.find_spec('selenium') is None:
    deps.mkdir(exist_ok=True)
    subprocess.run([
        sys.executable, '-m', 'pip', 'install',
        '--disable-pip-version-check', '--quiet',
        '--target', str(deps), 'selenium>=4.20,<5'
    ], check=True)
    sys.path.insert(0, str(deps))

projection=''.join(p.read_text('ascii') for p in sorted(root.glob('projection.part.*')))
(root/'projection.tar.gz.b64').write_text(projection, encoding='ascii')

runner_b64=''.join(p.read_text('ascii') for p in sorted(root.glob('runner.part.*')))
code=gzip.decompress(base64.b64decode(runner_b64, validate=True))
ns={'__name__':'__main__','__file__':str(root/'stage8_runner_full.py')}
exec(compile(code, ns['__file__'], 'exec'), ns, ns)
