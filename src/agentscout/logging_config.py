from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Plain, parseable single-line logs: ts level logger message."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    logging.Formatter.converter = __import__("time").gmtime
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
