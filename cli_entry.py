"""Entry point for the console exe (agents / pipelines).

Kept separate from launcher.py so the CLI build stays console-based
and does not import the web app.
"""

import sys

from minutenest.cli import main

if __name__ == "__main__":
    sys.exit(main())
