#!/usr/bin/env python3
from pathlib import Path
import base64, gzip

root=Path(__file__).resolve().parent

projection=''.join(p.read_text('ascii') for p in sorted(root.glob('projection.part.*')))
(root/'projection.tar.gz.b64').write_text(projection, encoding='ascii')

runner_b64=''.join(p.read_text('ascii') for p in sorted(root.glob('runner.part.*')))
code=gzip.decompress(base64.b64decode(runner_b64, validate=True))
ns={'__name__':'__main__','__file__':str(root/'stage8_runner_full.py')}
exec(compile(code, ns['__file__'], 'exec'), ns, ns)
