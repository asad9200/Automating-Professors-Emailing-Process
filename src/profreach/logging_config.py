import logging

from profreach.config import Settings, get_settings


def configure_logging(settings: Settings | None = None) -> None:
    configured = settings or get_settings()
    level = getattr(logging, configured.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
