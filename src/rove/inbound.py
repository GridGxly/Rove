"""What the owner's own Discord messages can do before they are parsed as a command.

`worker.poll_commands` reads the channels; this module decides who wrote a message and
handles the three things that are not replies to an application's hold:

- a link the owner pastes in agent-control is queued as the owner's own link. This is
  the only place that source is given: the model's MCP tool cannot give it, and neither
  can a page, a mail or anyone else in the server.
- the owner's Discord reply on a mail card in the recruiting channel confirms or drops a
  mail whose sender could not be verified.
- `not rejected` in an application's thread takes back the last step a mail made.
"""

import re

from . import workflow

LINK = re.compile(r"<?(https?://[^\s<>]+)>?", re.IGNORECASE)
# Words that may stand next to a pasted link and still mean "apply to this". Anything
# else (a question, "do not", a comment about the link) leaves the message to the agent,
# whose own queueing carries no owner authority.
PASTE_WORDS = frozenset(
    {
        *("apply", "queue", "add", "start", "prepare", "do", "submit", "send", "go", "ahead"),
        *("to", "for", "in", "the", "this", "that", "these", "one", "it", "here", "now", "next"),
        *("job", "role", "posting", "link", "please", "pls", "and", "also", "too"),
        *("me", "can", "could", "you"),
    }
)
MAX_LINKS = 5
UNDO_WORDS = frozenset(
    {"not rejected", "undo", "undo that", "wrong mail", "that mail was wrong", "that was wrong"}
)


def from_owner(message: dict, owner: str) -> bool:
    """Only the configured owner's own messages are ever read: the numeric author id,
    never a name, and never a bot, a webhook or a system message carrying that id."""
    author = message.get("author")
    if not owner or not isinstance(author, dict):
        return False
    return (
        str(author.get("id") or "") == str(owner)
        and not author.get("bot")
        and not author.get("system")
        and not message.get("webhook_id")
    )


def words(text) -> str:
    return " ".join(str(text or "").strip().strip("`").rstrip(".!?").split()).lower()


def pasted_links(text) -> list[str]:
    """The links of a message that is a pasted link: nothing but links, or links with a
    few words that ask to apply. A message that talks about a link is not one."""
    text = str(text or "")
    links = [match.rstrip(".,;:!?)") for match in LINK.findall(text)]
    if not links or len(links) > MAX_LINKS:
        return []
    rest = re.findall(r"[a-z0-9']+", LINK.sub(" ", text).lower())
    if len(rest) > 8 or any(word not in PASTE_WORDS for word in rest):
        return []
    return list(dict.fromkeys(links))


def queue_pasted(links: list[str]) -> str:
    """Queue the owner's links as his own and say so in one plain line."""
    results = [workflow.enqueue(link, source="owner_link") for link in links]
    fresh = [r for r in results if not r["already_exists"]]
    if fresh and len(results) == 1:
        return "Queued your link. It goes next."
    if fresh:
        return f"Queued {len(fresh)} of your links. They go next."
    states = {workflow.STATE_WORDS.get(r["status"], "tracked").lower() for r in results}
    return "Already tracked: " + ", ".join(sorted(states)) + "."


def owner_message(message: dict, channel: str, settings: dict, threads: dict) -> str | None:
    """Handle one message the owner wrote, when it is one of the cases above.

    Returns the plain line to post in the same channel, or None when the message is
    something else and the command parser should read it. Raises ValueError with a
    plain line when the message was understood and cannot apply. The caller has already
    checked `from_owner`.
    """
    content = message.get("content") or ""
    if channel == settings.get("control_channel_id"):
        links = pasted_links(content)
        return queue_pasted(links) if links else None
    if channel == settings.get("recruiting_channel_id"):
        from . import mail

        return mail.owner_reply(message)
    if channel in threads and words(content) in UNDO_WORDS:
        from . import mail

        return mail.undo_last_step(threads[channel], str(message.get("id") or "owner"))
    return None
