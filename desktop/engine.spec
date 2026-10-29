# Run with python -m PyInstaller desktop/engine.spec from the repository root.
from pathlib import Path
root = Path(SPECPATH).parent
analysis = Analysis([str(root / 'desktop' / 'engine.py')], pathex=[str(root / 'src')],
    binaries=[], datas=[(str(root / 'src' / 'local_coder' / 'webui' / 'dist'), 'local_coder/webui/dist'),
                        (str(root / 'src' / 'local_coder' / 'webui' / 'public'), 'local_coder/webui/public'),
                        (str(root / 'src' / 'local_coder' / 'release_spec.json'), 'local_coder'),
                        (str(root / 'src' / 'local_coder' / 'models' / 'intelligence_index.json'), 'local_coder/models')],
    hiddenimports=[], hookspath=[], runtime_hooks=[],
    # GGUF inference uses the selected native runtime. The optional tokenizer
    # needs text assets only; do not bundle unrelated ML execution frameworks.
    excludes=['pytest', 'torch', 'tensorflow', 'jax', 'jaxlib', 'flax',
              'torchvision', 'torchaudio', 'pandas', 'sklearn', 'scipy',
              'librosa', 'soundfile', 'soxr', 'av', 'cv2', 'PIL',
              'matplotlib', 'sympy', 'IPython', 'timm'])
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [], name='local-coder-engine',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
