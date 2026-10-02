"""Shared Twisted reactor manager for cTrader Open API clients.

Twisted's reactor is a singleton that can only start once per process.
When multiple clients (e.g. CTraderOpenApiClient and OpenApiSpotFeed)
manage the reactor independently, stopping it in one client causes
ReactorNotRestartable in the other.

This module provides a process-level singleton that starts the reactor
exactly once in a daemon thread.  No stop() method is exposed — the
reactor lives for the entire process lifetime.
"""

import logging
import threading

from twisted.internet import reactor

logger = logging.getLogger(__name__)


class ReactorManager:
    """Singleton that ensures the Twisted reactor runs exactly once."""

    _instance: "ReactorManager | None" = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "ReactorManager":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._start_lock = threading.Lock()
                    cls._instance._started = False
                    cls._instance._thread: threading.Thread | None = None
        return cls._instance

    def ensure_running(self) -> None:
        """Start the reactor in a daemon thread if not already running.

        Thread-safe: a threading.Lock guards the check-and-start sequence
        so that concurrent callers cannot race to start the reactor twice.
        """
        # Fast path — already started by us or already running externally
        if self._started and reactor.running:
            return

        with self._start_lock:
            if self._started and reactor.running:
                return

            if reactor.running:
                # Reactor was started externally (e.g. another library).
                self._started = True
                logger.info("Reactor already running externally — adopting")
                return

            self._thread = threading.Thread(
                target=reactor.run,
                kwargs={"installSignalHandlers": False},
                name="twisted-reactor",
                daemon=True,
            )
            self._thread.start()
            self._started = True
            logger.info("Twisted reactor started in daemon thread (process-lifetime)")

    @property
    def is_running(self) -> bool:
        return reactor.running
