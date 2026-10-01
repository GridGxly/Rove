"""Job boards Rove knows by their own pages, one module each.

A board is recognised by its exact hosts, never by a look-alike. Each module says how
the board names a job, where the form lives, and what a confirmed or refused send looks
like; its adapter is only used for final submission when the owner lists it in
`submit_adapters`.
"""

from . import bamboohr, jazzhr, paylocity, workable
from .base import markers_js

BOARDS = (paylocity, workable, jazzhr, bamboohr)
ADAPTERS = {board.Adapter.name: board.Adapter for board in BOARDS}
# Added to every observation's `ats_markers`.
MARKERS_JS = markers_js(BOARDS)


def board_for(url: str):
    """The board whose host serves this URL, if any."""
    return next((board for board in BOARDS if board.owns(url)), None)


def approved(url: str) -> bool:
    return board_for(url) is not None


def scope(url: str) -> tuple | None:
    """The job a posting, form or confirmation URL belongs to; None off a job page."""
    board = board_for(url)
    return board.scope(url) if board else None


def apply_url(url: str) -> str | None:
    """Where the application form for a posting lives."""
    board = board_for(url)
    return board.apply_url(url) if board else None


def fillable(url: str, fields: list[dict]) -> list[dict]:
    """The observed fields without a board's honeypot: it is never a question to answer."""
    trap = getattr(board_for(url), "is_trap", None)
    return [f for f in fields if not trap(f)] if trap else fields
