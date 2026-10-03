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
import time
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
        *("me", "can", "could", "you", "first", "priority", "asap"),
    }
)
# Next to a pasted link, any of these puts it ahead of his other pasted links.
PRIORITY_WORDS = frozenset({"now", "first", "next", "priority", "asap"})
# Alone in agent-control, or as a reply to Rove's line about a paste: move the latest paste up.
FIRST_REPLIES = frozenset(
    {"first", "move it up", "do this one first", "do that one first", "do it first"}
)
# How long after a paste a bare `first` still means that paste.
JUST_PASTED = timedelta(minutes=30)
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


def wants_first(text) -> bool:
    """Whether a pasted link came with a word that asks for it first."""
    rest = set(re.findall(r"[a-z0-9']+", LINK.sub(" ", str(text or "")).lower()))
    return bool(rest & PRIORITY_WORDS)


# --- the owner's line of pasted links ---------------------------------------------
#
# His pasted links run before any feed job, oldest first (`worker.next_queued`). One he
# marks `first` goes ahead of the others that have not started; the latest mark wins.


def order_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS owner_link_order(application_id TEXT PRIMARY KEY, "
        "pasted_at TEXT NOT NULL, first_at TEXT)"
    )


def jump_key(conn, application_id: str) -> tuple:
    """Sort key among the owner's own links: marked `first` (latest mark first), then the rest."""
    order_table(conn)
    row = conn.execute(
        "SELECT first_at FROM owner_link_order WHERE application_id=?", (application_id,)
    ).fetchone()
    if row and row[0]:
        return (0, -datetime.fromisoformat(row[0]).timestamp())
    return (1, 0.0)


def mark_pasted(application_ids: list[str], first: bool = False):
    """Remember when he pasted these links, and mark them `first` when he asked."""
    stamp = workflow.now()
    with workflow.db() as conn:
        order_table(conn)
        for application_id in application_ids:
            conn.execute(
                "INSERT INTO owner_link_order VALUES(?,?,?) ON CONFLICT(application_id) DO "
                "UPDATE SET pasted_at=excluded.pasted_at,first_at=COALESCE(excluded.first_at,first_at)",
                (application_id, stamp, stamp if first else None),
            )


def line_of_mine(conn) -> list[str]:
    """The order `worker.next_queued` takes the owner's choices in: his explicit resumes,
    then his links and picks by source rank, `first` marks, and age."""
    resumes = conn.execute(
        "SELECT q.id FROM application_queue q JOIN owner_commands c ON c.application_id=q.id "
        "WHERE q.status='QUEUED' AND c.kind IN ('resume','proceed','account') "
        "AND c.status='applied' ORDER BY c.created_at DESC"
    ).fetchall()
    queued = conn.execute(
        "SELECT id,source FROM application_queue WHERE status='QUEUED' ORDER BY created_at"
    ).fetchall()
    chosen = sorted(
        (row for row in queued if workflow.source_policy(row["source"])["owner_decided"]),
        key=lambda row: (
            -workflow.source_policy(row["source"])["rank"],
            *jump_key(conn, row["id"]),
        ),
    )
    return list(dict.fromkeys([row["id"] for row in resumes] + [row["id"] for row in chosen]))


def where_it_stands(application_id: str, *, many: bool = False, offer: bool = False) -> str:
    """Where one of his queued links stands among his own choices, in one plain sentence."""
    with workflow.db() as conn:
        line = line_of_mine(conn)
    ahead = line.index(application_id) if application_id in line else len(line)
    if not ahead:
        return "The first goes next." if many else "It goes next."
    them = "them" if many else "it"
    text = f"{ahead} of your links {'is' if ahead == 1 else 'are'} ahead of {them}"
    return text + (f"; say `first` to move {them} up." if offer else ".")


def move_up(*, replied: bool) -> str:
    """`first` after a paste: his latest pasted links that have not started go to the front.

    As a reply it means the paste he replied about; alone it counts only for a paste in
    the last half hour.
    """
    with workflow.db() as conn:
        order_table(conn)
        rows = conn.execute(
            "SELECT o.application_id,o.pasted_at,q.status FROM owner_link_order o JOIN "
            "application_queue q ON q.id=o.application_id ORDER BY o.pasted_at DESC"
        ).fetchall()
    if not rows:
        return "Nothing you pasted is waiting. Paste a link with `first` to put it at the front."
    latest = rows[0]["pasted_at"]
    if not replied and datetime.now(UTC) - datetime.fromisoformat(latest) > JUST_PASTED:
        return (
            "Nothing you pasted in the last half hour is waiting. Paste the link again with "
            "`first` to put it at the front."
        )
    batch = [row for row in rows if row["pasted_at"] == latest]
    waiting = [row["application_id"] for row in batch if row["status"] == "QUEUED"]
    if not waiting:
        return "That link already started, so there is nothing to move."
    stamp = workflow.now()
    with workflow.db() as conn:
        for application_id in waiting:
            conn.execute(
                "UPDATE owner_link_order SET first_at=? WHERE application_id=?",
                (stamp, application_id),
            )
    many = len(waiting) > 1
    return ("Moved them up. " if many else "Moved it up. ") + where_it_stands(waiting[0], many=many)


def queue_pasted(links: list[str], first: bool = False) -> str:
    """Queue the owner's links as his own and say in one plain line where they stand.

    With `first` (a priority word next to the links) they go ahead of his other pasted
    links that have not started.
    """
    results = [workflow.enqueue(link, source="owner_link") for link in links]
    mark_pasted([r["application_id"] for r in results], first=first)
    fresh = [r for r in results if not r["already_exists"]]
    waiting = [r for r in results if r["status"] == "QUEUED"]
    if not waiting:
        states = {workflow.STATE_WORDS.get(r["status"], "tracked").lower() for r in results}
        return "Already tracked: " + ", ".join(sorted(states)) + "."
    many = len(waiting) > 1
    if fresh:
        lead = f"Queued {len(fresh)} of your links." if len(fresh) > 1 else "Queued."
    else:
        lead = "Already queued."
    if len(fresh) not in (0, len(results)):
        lead += f" {len(results) - len(fresh)} {'was' if len(results) - len(fresh) == 1 else 'were'} already tracked."
    return lead + " " + where_it_stands(waiting[0]["application_id"], many=many, offer=True)


def owner_message(message: dict, channel: str, settings: dict, threads: dict) -> str | None:
    """Handle one message the owner wrote, when it is one of the cases above.

    Returns the plain line to post in the same channel, or None when the message is
    something else and the command parser should read it. Raises ValueError with a
    plain line when the message was understood and cannot apply. The caller has already
    checked `from_owner`.
    """
    content = message.get("content") or ""
    if channel == settings.get("control_channel_id"):
        return control_line(message)
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


# --- one answer in agent-control --------------------------------------------------
#
# The Hermes agent answers every message in agent-control and cannot stay silent, so a
# line from the worker would be a second answer. For a pasted link or `first` the agent
# calls a tool that applies the owner's own newest messages (read back from Discord,
# owner id checked, exactly as the worker would) and relays the line code wrote. Each
# message is applied once, by whoever claims it first. A line the agent never picked up
# (the gateway or the model is down) is posted by the worker after HAND_OVER.

HAND_OVER = timedelta(seconds=45)
CLAIM_WAIT = 5.0  # seconds the agent waits for a line the worker is writing that moment


def control_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS control_replies(message_id TEXT PRIMARY KEY, "
        "line TEXT NOT NULL, created_at TEXT NOT NULL, delivered TEXT)"
    )


def is_control_request(content) -> bool:
    """A pasted link, or `first` on its own: the two things code answers in agent-control."""
    return bool(pasted_links(content)) or words(content) in FIRST_REPLIES


def apply_control(message: dict) -> str:
    content = message.get("content") or ""
    links = pasted_links(content)
    try:
        if links:
            return queue_pasted(links, first=wants_first(content))
        replied = bool((message.get("message_reference") or {}).get("message_id"))
        return move_up(replied=replied)
    except (ValueError, PermissionError) as error:
        return str(error)


def control_line(message: dict, *, for_agent: bool = False) -> str | None:
    """Apply one owner message in agent-control once and return its line.

    None when the message is not a paste or `first`. The worker gets "" for a message
    the agent already took; the agent gets the line, also when the worker applied it.
    """
    if not is_control_request(message.get("content")):
        return None
    message_id = str(message.get("id") or "")
    if not message_id:
        return apply_control(message)
    with workflow.db() as conn:
        control_table(conn)
        claimed = conn.execute(
            "INSERT OR IGNORE INTO control_replies VALUES(?,?,?,NULL)",
            (message_id, "", workflow.now()),
        ).rowcount
    if claimed:
        line = apply_control(message)
        with workflow.db() as conn:
            conn.execute(
                "UPDATE control_replies SET line=?,delivered=? WHERE message_id=?",
                (line, workflow.now() if for_agent else None, message_id),
            )
        return line
    if not for_agent:
        return ""  # the agent took it and has answered
    deadline = time.monotonic() + CLAIM_WAIT
    while True:
        with workflow.db() as conn:
            row = conn.execute(
                "SELECT line,delivered FROM control_replies WHERE message_id=?", (message_id,)
            ).fetchone()
        if row["line"] or time.monotonic() > deadline:
            break
        time.sleep(0.25)
    if row["delivered"] or not row["line"]:
        return ""  # already answered, or still being written: nothing to say twice
    with workflow.db() as conn:
        taken = conn.execute(
            "UPDATE control_replies SET delivered=? WHERE message_id=? AND delivered IS NULL",
            (workflow.now(), message_id),
        ).rowcount
    return row["line"] if taken else ""


def held_for_agent(message: dict) -> bool:
    """Whether the worker keeps this line back for the agent to say (agent-control only)."""
    with workflow.db() as conn:
        control_table(conn)
        row = conn.execute(
            "SELECT delivered FROM control_replies WHERE message_id=?",
            (str(message.get("id") or ""),),
        ).fetchone()
    return bool(row) and row["delivered"] is None


def flush_control_lines(channel: str | None):
    """Post the lines the agent did not pick up within HAND_OVER; the worker calls this."""
    if not channel:
        return
    cutoff = (datetime.now(UTC) - HAND_OVER).isoformat()
    with workflow.db() as conn:
        control_table(conn)
        rows = conn.execute(
            "SELECT message_id,line FROM control_replies WHERE delivered IS NULL AND line!='' "
            "AND created_at<? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
    for row in rows:
        with workflow.db() as conn:
            taken = conn.execute(
                "UPDATE control_replies SET delivered=? WHERE message_id=? AND delivered IS NULL",
                (workflow.now(), row["message_id"]),
            ).rowcount
        if taken:
            workflow.discord(
                "POST",
                f"/channels/{channel}/messages",
                {"content": row["line"], "allowed_mentions": {"parse": []}},
            )


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
