"""
Script for syncing transactions from Akahu to YNAB and Actual Budget.
Also provides webhook endpoints for real-time transaction syncing.
"""

from contextlib import contextmanager
import os
import logging
import sys
from actual import Actual

# Import from our modules package
from modules.sync_handler import sync_to_ab, sync_to_ynab
from modules.webhook_handler import create_flask_app
from modules.account_mapper import load_existing_mapping
from modules.config import AKAHU_ENDPOINT, AKAHU_HEADERS
from modules.config import RUN_SYNC_TO_AB, RUN_SYNC_TO_YNAB
from modules.config import ENVs

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)


@contextmanager
def get_actual_client(run_sync):
    """Context manager that yields an Actual client if run_sync is True.
    This is needed because actualpy only works with contextmanager
    """
    if run_sync:
        with Actual(
            base_url=ENVs['ACTUAL_SERVER_URL'],
            password=ENVs['ACTUAL_PASSWORD'],
            file=ENVs['ACTUAL_SYNC_ID'],
            encryption_password=ENVs['ACTUAL_ENCRYPTION_KEY']
        ) as client:
            yield client
    else:
        yield None


def main():
    """Main entry point for the sync script."""
    try:
        # Load the existing mapping
        _, _, _, mapping_list = load_existing_mapping()
        with get_actual_client(RUN_SYNC_TO_AB) as actual:
            # Initialize Actual if syncing to AB
            # Create Flask app with Actual client
            app = create_flask_app(actual, mapping_list, {
                'AKAHU_PUBLIC_KEY': ENVs['AKAHU_PUBLIC_KEY'],
                'akahu_endpoint': AKAHU_ENDPOINT,
                'akahu_headers': AKAHU_HEADERS
            })

            # Run initial syncs if configured
            if RUN_SYNC_TO_AB and actual is not None:
                sync_to_ab(actual, mapping_list)
            if RUN_SYNC_TO_YNAB:
                sync_to_ynab(mapping_list)

            development_mode = os.getenv('FLASK_ENV') == 'development'
            app.run(host="0.0.0.0", port=5000, debug=development_mode)
    except Exception as e:
        logging.exception(f"An unexpected error occurred: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
