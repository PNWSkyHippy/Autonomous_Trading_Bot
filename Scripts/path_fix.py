"""
path_fix.py
============
Import this at the top of any script in the Scripts/ folder to ensure
it can find the project's modules regardless of where it's run from.

Usage (add these two lines to the top of any script in Scripts/):
    import Scripts.path_fix  # noqa
    # OR if running directly:
    exec(open('Scripts/path_fix.py').read())

Or just add this boilerplate directly (preferred):
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)
