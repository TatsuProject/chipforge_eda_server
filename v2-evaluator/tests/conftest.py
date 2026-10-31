import sys
from pathlib import Path
runtime = Path('/opt/cf')
sys.path.insert(0, str(runtime if (runtime / 'loader').is_dir() else Path(__file__).resolve().parents[1]))
