"""H10 pagination contract — list endpoints use limit/offset.

Verifies StandardLimitOffsetPagination so the frontend (XIU-7) can rely on
``?limit=N&offset=M`` and the ``{count, next, previous, results}`` response
shape. The paginator slices any sequence, so these tests need no database.
"""
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from control_plane.pagination import StandardLimitOffsetPagination

DATASET = list(range(5000))


def _paginate(query):
    request = Request(APIRequestFactory().get("/api/acquisition/sessions/", query))
    paginator = StandardLimitOffsetPagination()
    page = paginator.paginate_queryset(DATASET, request)
    return paginator, page


def test_limit_and_offset_params_are_honored():
    """?limit & ?offset slice the result set as the frontend expects."""
    paginator, page = _paginate({"limit": "10", "offset": "20"})
    assert page == list(range(20, 30))


def test_response_shape_is_count_next_previous_results():
    """Paginated list response matches the contract handed to the frontend."""
    paginator, page = _paginate({"limit": "10", "offset": "20"})
    body = paginator.get_paginated_response(page).data
    assert set(body) == {"count", "next", "previous", "results"}
    assert body["count"] == len(DATASET)
    assert body["results"] == list(range(20, 30))


def test_default_limit_falls_back_to_page_size():
    """Omitting ?limit applies default_limit (settings.PAGE_SIZE = 50)."""
    paginator, page = _paginate({})
    assert len(page) == 50


def test_max_limit_caps_abusive_requests():
    """A huge ?limit is capped at max_limit so one call can't pull everything."""
    paginator, page = _paginate({"limit": "999999"})
    assert len(page) == StandardLimitOffsetPagination.max_limit == 1000
