"""Live traffic, kept apart from the planner.

Everything here pulls OUTSIDE data (Google traffic tiles, CHP incidents, ...)
and its only output is Valhalla's traffic.tar. Turn it off and the planner is
fully local again: routes use typical speeds and say so. See README.md here.
"""

import os


def _load_local_env(path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")):
    """API keys (FT_TOMTOM_API_KEY, FT_SF511_API_KEY, ...) from backend/.env.local,
    the same gitignored file the backend reads. Real environment variables win."""
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_local_env()
