"""Per-answer quality ratings, appended to a flat CSV.

The chat shows five faces under every answer; each maps to a score of 1 (worst) to 5
(best), and only the number is stored — the emoji is presentation, and writing it would
cost 4 bytes a row to say what one digit already says.

CSV rather than JSONL or a database:

* JSONL repeats every field NAME on every line. For rows this small that roughly doubles
  the file for no added information — CSV writes the header once.
* A database would need a schema, a migration path and a connection for what is a
  single append per rating. The warehouse is for BayOne's business data, not the app's
  own telemetry.
* CSV opens directly in Excel and loads in one line of pandas, which is what this data is
  for. A rating is ~90 bytes, so a year of heavy use is a few megabytes.

Writes are append-only under a mutex: never rewritten, so a crash mid-write can cost the
last row but cannot corrupt earlier ones. Re-rating an answer appends a second row rather
than editing the first; readers take the LAST row for a (session_id, turn) pair, which
keeps the audit trail of what someone changed their mind about.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sf_agent.feedback")

# 1 = deepest frown … 5 = smile with teeth. Kept here so the API, the store and the tests
# agree on one definition of a valid score.
MIN_SCORE = 1
MAX_SCORE = 5

# The question is kept because a score alone cannot be acted on — "follow-ups rate badly"
# is a finding, "THIS follow-up rated badly" is a fix. Truncated so one pathological
# paste cannot dominate the file. (Questions already reach the application log via the
# router, so this adds no new class of exposure.)
_MAX_QUESTION_CHARS = 200

COLUMNS = (
    "rated_at",    # ISO-8601 UTC, so rows sort lexicographically
    "score",       # 1..5
    "session_id",  # groups ratings from one conversation
    "turn",        # which answer within that session; (session_id, turn) identifies it
    "route",       # database / followup / web / general — which lane scores badly
    "elapsed_ms",  # is a low score explained by a slow answer?
    "cost_usd",    # ...or an expensive one?
    "rated_by",    # caller identity when ownership enforcement is on
    "question",
)

_LOCK = threading.Lock()

# Spreadsheets treat a leading =, +, - or @ as the start of a formula, so free text from
# a browser must not be written raw into a file whose whole point is being opened in
# Excel. Prefixing with an apostrophe keeps the text visible and inert.
_FORMULA_LEADERS = ("=", "+", "-", "@")


def _sanitize(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return "'" + text if text[:1] in _FORMULA_LEADERS else text


def default_path() -> Path:
    """Where ratings are written, unless FEEDBACK_PATH overrides it.

    On Azure App Service only /home is persistent — the deployment is extracted to a temp
    directory that is replaced on every restart and redeploy, so a file next to the code
    would silently vanish along with every rating in it. WEBSITE_SITE_NAME is set by App
    Service and by nothing else, which makes it a reliable way to tell the two apart.
    """
    override = os.environ.get("FEEDBACK_PATH", "").strip()
    if override:
        return Path(override)
    if os.environ.get("WEBSITE_SITE_NAME"):
        return Path("/home/data/bayi/feedback.csv")
    return Path(__file__).resolve().parents[2] / ".feedback" / "ratings.csv"


def record(
    score: int,
    session_id: str,
    turn: int,
    route: str | None = None,
    elapsed_ms: float | None = None,
    cost_usd: float | None = None,
    rated_by: str | None = None,
    question: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one rating. Returns the row written.

    Raises ValueError when the score is not an integer in 1..5, so a malformed client
    cannot put junk in a file meant to be read as numbers.
    """
    # Parse via the string form so a fractional score is REJECTED rather than truncated:
    # int(2.7) is 2, which would file a rating the user never gave. This also rejects
    # True, which is an int subclass and would otherwise sail through as 1.
    try:
        value = int(str(score).strip())
    except (TypeError, ValueError):
        raise ValueError(f"score must be a whole number {MIN_SCORE}-{MAX_SCORE}") from None
    if not MIN_SCORE <= value <= MAX_SCORE:
        raise ValueError(f"score must be between {MIN_SCORE} and {MAX_SCORE}, got {value}")

    row = {
        "rated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "score": value,
        "session_id": _sanitize(session_id),
        "turn": int(turn),
        "route": _sanitize(route),
        "elapsed_ms": "" if elapsed_ms is None else round(float(elapsed_ms)),
        "cost_usd": "" if cost_usd is None else f"{float(cost_usd):.6f}",
        "rated_by": _sanitize(rated_by),
        "question": _sanitize(question)[:_MAX_QUESTION_CHARS],
    }

    target = path or default_path()
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        new_file = not target.exists() or target.stat().st_size == 0
        # Build the line first, then write it in a single call: a partial line from an
        # interrupted write would break every later row's column alignment.
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        with target.open("a", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
    logger.info("rating %d recorded for session=%s turn=%s route=%s", value, session_id, turn, route)
    return row


def read_ratings(path: Path | None = None) -> list[dict[str, str]]:
    """Every rating on file, oldest first. Empty when nothing has been rated yet."""
    target = path or default_path()
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def summarize(path: Path | None = None) -> dict[str, Any]:
    """Current standing: how many answers were rated, the average, and the split by route.

    Superseded ratings are dropped first — re-rating an answer appends a row rather than
    editing one, so counting every row would let a single answer vote as many times as the
    user changed their mind.
    """
    latest: dict[tuple[str, str], dict[str, str]] = {}
    for row in read_ratings(path):
        latest[(row.get("session_id", ""), row.get("turn", ""))] = row

    scores = [int(r["score"]) for r in latest.values() if str(r.get("score", "")).isdigit()]
    by_route: dict[str, list[int]] = {}
    for r in latest.values():
        if str(r.get("score", "")).isdigit():
            by_route.setdefault(r.get("route") or "unknown", []).append(int(r["score"]))

    return {
        "rated_answers": len(scores),
        "average": round(sum(scores) / len(scores), 2) if scores else None,
        "distribution": {n: scores.count(n) for n in range(MIN_SCORE, MAX_SCORE + 1)},
        "by_route": {
            route: {"count": len(v), "average": round(sum(v) / len(v), 2)}
            for route, v in sorted(by_route.items())
        },
    }
