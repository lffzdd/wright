import logging
from logging import Formatter, StreamHandler, getLogger


def get_logger(name: str) -> logging.Logger:
    logger = getLogger(name)

    if not logger.handlers:
        handler = StreamHandler()
        handler.setFormatter(
            Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger