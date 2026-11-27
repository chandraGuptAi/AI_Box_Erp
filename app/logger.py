import logging

from app.config import get_settings


def setup_logging() -> None:
    logging.basicConfig(level=get_settings().LOG_LEVEL,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ai_box_erp.{name}")
