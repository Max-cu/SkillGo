"""
Patches for flat-mode SVG→PPTX conversion.
Each module handles one template type's known incompatibilities.
"""
from . import common
from . import patch_002_toc
from . import patch_002_toc_3ch
