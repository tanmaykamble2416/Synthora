"""Central logging configuration."""

import logging

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)
    # SQLAlchemy is very chatty at INFO; SQL echo is controlled by DB_ECHO instead.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
