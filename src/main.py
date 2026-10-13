import signal
import time
from pathlib import Path

from .config import load_settings, validate_settings
from .keycrm_client import KeyCRMClient
from .logger import setup_logger
from .sync_service import SyncService
from .trello_client import TrelloClient


def main():
    root_dir = Path(__file__).resolve().parent.parent
    settings = load_settings(root_dir)
    validate_settings(settings)

    logger = setup_logger(settings.log_file)
    keycrm_client = KeyCRMClient(
        settings.keycrm_base_url,
        settings.keycrm_api_token,
        settings.keycrm_pipeline_id,
        settings.keycrm_status_map,
    )
    trello_client = TrelloClient(
        settings.trello_base_url,
        settings.trello_api_key,
        settings.trello_token,
        settings.trello_board_id,
    )
    sync_service = SyncService(
        keycrm_client=keycrm_client,
        trello_client=trello_client,
        logger=logger,
        keycrm_status_map=settings.keycrm_status_map,
        require_phone=settings.keycrm_require_phone,
        auto_create_trello_cards=settings.sync_auto_create_trello_cards,
        sync_bidirectional=settings.sync_bidirectional,
        sync_comments=settings.sync_comments,
    )

    running = True

    def stop_handler(signum, frame):
        nonlocal running
        running = False
        logger.info("Received signal %s, stopping sync agent", signum)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    logger.info("KeyCRM -> Trello sync agent started")
    while running:
        try:
            sync_service.run_once()
        except Exception as exc:
            logger.exception("Unhandled sync error: %s", exc)
        time.sleep(settings.poll_interval_seconds)
    logger.info("KeyCRM -> Trello sync agent stopped")


if __name__ == "__main__":
    main()
