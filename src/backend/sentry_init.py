"""
Optional error tracking. Only imported (see app.py) when SENTRY_DSN is set,
so a missing `sentry-sdk` package or unset DSN never affects local dev -
this is purely additive for a real deployment.

To use: set SENTRY_DSN in your environment and `pip install sentry-sdk`
(it's in requirements.txt as an optional extra - see the comment there).
"""
import logging

import config

logger = logging.getLogger(__name__)

try:
    import sentry_sdk
    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        environment=config.ENVIRONMENT,
        traces_sample_rate=0.1,
    )
    logger.info("Sentry error tracking initialized.")
except ImportError:
    logger.warning("SENTRY_DSN is set but the 'sentry-sdk' package isn't installed - skipping. "
                    "Run: pip install sentry-sdk")
