"""Helpers shared by test modules.

Test modules import shared doubles and utilities from here, never from each other: importing a
test module re-runs its module-level code (fixtures, platform skips) inside the importer.
"""

from pathlib import Path

SUPPORT = Path(__file__).resolve().parent
REPO_ROOT = SUPPORT.parents[1]
