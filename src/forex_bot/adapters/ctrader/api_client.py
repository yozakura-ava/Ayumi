"""cTraderAPIClient — concrete runtime type satisfying OrderManager's forward refs.

The codebase has multiple ``Optional["cTraderAPIClient"]`` annotations on
``PaperTrader.__init__``, ``OrderManager.__init__``, ``OrderManager.set_api_client``
and the dead-code ``MultiStrategyOrchestrator.__init__``. Until now those were
``TYPE_CHECKING``-only forward references — the class did not exist at runtime.

This module provides the concrete class so that:

* ``PaperTrader(api_client=instance)`` works at runtime (the engine now
  passes the live spot feed in via Task T4 wiring).
* ``OrderManager._wire_live_callbacks`` can register ``on_order_filled`` /
  ``on_order_rejected`` callbacks that fire when late execution events arrive
  (used by Task T1's race-condition handling).
* Tests can ``from adapters.ctrader.api_client import cTraderAPIClient``
  without relying on ``TYPE_CHECKING``-only semantics.

Design note
-----------
We subclass :class:`OpenApiSpotFeed` rather than aliasing it. The subclass
relationship is preserved so any existing code that does
``isinstance(feed, OpenApiSpotFeed)`` keeps working.  All behavior
(``is_paper_mode``, ``is_connected``, ``new_order``, ``start``, …) is
inherited unchanged from the parent.

We expose ``is_paper_mode`` as a class attribute (``False``) on the subclass.
This matches the parent class's own :py:attr:`OpenApiSpotFeed.is_paper_mode`
property which also returns ``False`` for the OpenAPI live feed.  Calling
code that does ``not api_client.is_paper_mode`` will see the inherited
property and work correctly.
"""

from __future__ import annotations

from typing import Any

from .open_api_spot_feed import OpenApiSpotFeed

__all__ = ["cTraderAPIClient"]


class cTraderAPIClient(OpenApiSpotFeed):
    """Concrete live-API client for cTrader OpenAPI.

    Subclasses :class:`OpenApiSpotFeed` and re-affirms ``is_paper_mode=False``
    as a class attribute so duck-typed attribute access (``getattr(client,
    'is_paper_mode', False)``) is reliable.  No new behavior is added.

    Constructor signature matches the parent; ``**kwargs`` is forwarded
    unchanged so all live-credential wiring (``ctid_account_id``,
    ``client_id``, ``client_secret``, ``access_token``, ``refresh_token``,
    ``host``, ``port``) flows through to the live spot feed.
    """

    # Mirror the parent's runtime promise as a class attribute so duck-typed
    # checks like ``getattr(client, "is_paper_mode", False)`` succeed even
    # before any instance attributes are bound.  The parent class defines
    # this as a ``@property`` returning False; this attribute is a
    # belt-and-suspenders fallback for the same value.
    is_paper_mode: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
