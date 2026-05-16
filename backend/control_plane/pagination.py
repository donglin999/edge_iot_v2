"""Project-wide DRF pagination.

H10: list endpoints are paginated globally so they never dump an unbounded
result set. We use limit/offset pagination rather than page-number pagination
so the contract is consistent with the custom ``data-points`` action (which
already takes ``?limit`` & ``?offset``) and with the frontend API client.

Response shape for every paginated list endpoint::

    {"count": <int>, "next": <url|null>, "previous": <url|null>, "results": [...]}

Clients page via ``?limit=N&offset=M``. When ``?limit`` is omitted,
``default_limit`` (settings.PAGE_SIZE) applies; ``max_limit`` caps abusive
requests so a single call can never pull an unbounded page.
"""
from rest_framework.pagination import LimitOffsetPagination


class StandardLimitOffsetPagination(LimitOffsetPagination):
    """LimitOffsetPagination with project defaults.

    ``default_limit`` is inherited from ``api_settings.PAGE_SIZE`` (DRF default
    behaviour); ``max_limit`` is set so a client cannot request an unbounded
    page even if it passes a huge ``?limit``.
    """

    max_limit = 1000
