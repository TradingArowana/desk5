"""Simple structured logger for desk5."""
import logging
import sys
from datetime import datetime

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(_FMT))

def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        log.addHandler(_handler)
    log.setLevel(logging.INFO)
    return log
