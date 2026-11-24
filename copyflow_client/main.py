from __future__ import annotations

import logging

from copyflow_client.config import get_settings, load_subscriptions
from copyflow_client.receiver import create_app

log = logging.getLogger("copyflow-client")

settings = get_settings()
subs = load_subscriptions(settings.config_file)

app = create_app(settings, subs)
