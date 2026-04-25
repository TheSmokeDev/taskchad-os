"""Spec.md:84 compatibility shim - TestPreserveRaw lives in test_entity_extractor.py."""
import sys
from pathlib import Path

# Ensure tests directory is importable so we can re-export TestPreserveRaw
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_entity_extractor import TestPreserveRaw  # noqa: E402, F401
