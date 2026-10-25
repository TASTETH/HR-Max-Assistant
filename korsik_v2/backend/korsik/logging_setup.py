import logging
import os
import sys


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(stream=sys.stdout, level=level,
                        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log URLs with tokens/bodies
