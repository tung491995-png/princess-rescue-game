#!/usr/bin/env python3
from pathlib import Path
root=Path(__file__).resolve().parent
src=''.join((root/'chunks'/f'c{i:02d}.txt').read_text() for i in range(6))
code=compile(src,str(root/'stage8_runner_embedded.py'),'exec')
exec(code,{'__name__':'__main__','__file__':str(root/'stage8_runner_embedded.py')})
