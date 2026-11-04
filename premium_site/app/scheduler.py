"""Opt-in renewal runner: python -m app.scheduler."""
import logging
import time
from . import identity
from .billing import schema, tick

if __name__=='__main__':
    schema(); identity.schema()
    while True:
        try: tick()
        except Exception: logging.error('Billing tick failed; durable attempts retained')
        time.sleep(60)
