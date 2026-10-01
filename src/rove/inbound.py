"""What the owner's own Discord messages can do before they are parsed as a command.

`worker.poll_commands` reads the channels; this module decides who wrote a message and
handles the things that are not a plain reply to one application's hold:

- a link the owner pastes in agent-control is queued as the owner's own link. Apart
  from the owner's own terminal, this is the only place that source is given: the
  model's MCP tool cannot give it, and neither can a page, a mail or anyone else.
- the owner's Discord reply on a mail card in the recruiting channel confirms or drops a
  mail whose sender could not be verified.
- `not rejected` in an application's thread takes back the last step a mail made.
- in action-needed and shortlist, a bare word with several cards live gets a "Which
  one?" line; the owner's next message naming the company applies the word to it.
"""

import re
from datetime import UTC, datetime, timedelta

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
    cards = {settings.get(key): name for name, key in workflow.NOTICE_CHANNELS.items()}
    if channel in cards and not (message.get("message_reference") or {}).get("message_id"):
        return answer_which_one(message, channel, cards[channel])
    return None


# --- "Which one?" in action-needed and shortlist ----------------------------------

# How long a bare word waits for the owner to name the application it was meant for.
WAITING_WORD = timedelta(minutes=60)
LISTED_CARDS = 6


def waiting_db():
    conn = workflow.db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS owner_waiting_words(channel_id TEXT PRIMARY KEY, "
        "content TEXT NOT NULL, message_id TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    return conn


def live_cards(channel_name: str) -> list[dict]:
    """The applications with a live card in one owner channel, oldest card first."""
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT q.* FROM owner_notices n JOIN application_queue q ON q.id=n.application_id "
            "WHERE n.channel=? AND n.delivery='sent' GROUP BY q.id ORDER BY MIN(n.id)",
            (channel_name,),
        ).fetchall()
    return [dict(row) for row in rows]


def shown_title(item: dict) -> str:
    """Company and role as plain words: no markup a title could carry, never an id."""
    title = re.sub(r"[`*_~|<>\[\]\\@#]", " ", workflow.display_title(item))
    return workflow.clip(" ".join(title.split()), 70)


def listed(cards: list[dict]) -> str:
    names = [shown_title(item) for item in cards[:LISTED_CARDS]]
    more = len(cards) - len(names)
    return " · ".join(names) + (f" · and {more} more" if more > 0 else "")


def plain_key(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def could_answer(content: str, cards: list[dict]) -> bool:
    """Whether a plain message could be the answer to a card's one open question."""
    if not content.strip() or len(content) > 200 or re.search(r"https?://", content):
        return False
    for item in cards:
        hold = workflow.latest_hold(item["id"]) or {}
        opened = [q for q in hold.get("questions") or [] if q.get("state", "open") == "open"]
        if item["status"] == "NEEDS_USER" and len(opened) == 1:
            return True
    return False


def which_one(channel: str, message: dict, cards: list[dict]) -> str:
    """Keep the owner's word and return the line that asks which card it was for."""
    with waiting_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO owner_waiting_words VALUES(?,?,?,?)",
            (
                channel,
                str(message.get("content") or "").strip(),
                str(message.get("id") or ""),
                workflow.now(),
            ),
        )
    return (
        f"Which one? {listed(cards)}. Answer with the company name, or use Discord's "
        "reply on its card."
    )


def named_cards(content: str, cards: list[dict]) -> list[dict]:
    """The live cards a short answer names: by company, by role, or by words of the title."""
    from .mail import split_title

    wanted = plain_key(content)
    if len(wanted) < 2 or len(wanted) > 80:
        return []
    exact, partial = [], []
    for item in cards:
        company, role = (plain_key(part) for part in split_title(item))
        title = plain_key(workflow.display_title(item))
        if wanted in {company, title}:
            exact.append(item)
        elif len(wanted) >= 3 and f" {wanted} " in f" {company} {role} {title} ":
            partial.append(item)
    return exact or partial


def answer_which_one(message: dict, channel: str, channel_name: str) -> str | None:
    """The owner's message after a "Which one?": when it names one live card, the word
    that was waiting is applied to that application, exactly as if typed in its thread."""
    with waiting_db() as conn:
        row = conn.execute(
            "SELECT * FROM owner_waiting_words WHERE channel_id=?", (channel,)
        ).fetchone()
    if not row:
        return None
    waiting = dict(row)

    def forget():
        with waiting_db() as conn:
            conn.execute("DELETE FROM owner_waiting_words WHERE channel_id=?", (channel,))

    if datetime.now(UTC) - datetime.fromisoformat(waiting["created_at"]) > WAITING_WORD:
        forget()
        return None
    cards = live_cards(channel_name)
    named = named_cards(message.get("content") or "", cards)
    if not named:
        return None
    if len(named) > 1:
        raise ValueError(
            f"That fits more than one: {listed(named)}. Add the role, or use Discord's "
            "reply on its card."
        )
    from . import worker

    forget()
    item = named[0]
    command = worker.thread_command(waiting["content"], item["id"])
    if command is None:
        raise ValueError(
            f"“{workflow.clip(waiting['content'], 60)}” does not fit {shown_title(item)}. "
            "Reply on its card or in its thread."
        )
    worker.apply_command(command, waiting["message_id"] or str(message.get("id") or ""))
    return f"Got it: {shown_title(item)}."
