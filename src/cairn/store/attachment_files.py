"""Files kept for an application, by absolute path."""
import os
from pathlib import Path

from cairn.store import db
from cairn.store.db import connect

_ATTACHMENT = "SELECT id, posting_id, label, path, added_at FROM attachments "


def _under_home(real):
    """Whether an ancestor of real is the home folder. The comparison goes through
    the file system, which on a case-insensitive volume matches /USERS/x to /Users/x."""
    home = os.path.realpath(Path.home())
    for parent in real.parents:
        try:
            if os.path.samefile(parent, home):
                return True
        except OSError:
            continue
    return False


def _home_file(path):
    """path with ~ expanded and symlinks resolved, when that names an existing regular
    file under the user's home directory. Raises ValueError otherwise.

    path is tried as given first, so a file whose name ends in a space is found, and
    stripped of surrounding whitespace only when nothing exists there."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("a file needs a path")
    given = Path(path).expanduser()
    if not given.exists():
        given = Path(path.strip()).expanduser()
    if not given.is_absolute():
        raise ValueError(f"{path} is not an absolute path")
    real = Path(os.path.realpath(given))
    if not _under_home(real):
        raise ValueError(f"{path} is outside your home folder")
    if not real.is_file():
        raise ValueError(f"{path} is not a file")
    return str(real)


def attachments(posting_id):
    rows = connect().execute(_ATTACHMENT + "WHERE posting_id = ? ORDER BY id", (posting_id,))
    return [dict(row) for row in rows]


def attachment(attachment_id):
    row = connect().execute(_ATTACHMENT + "WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def add_attachment(posting_id, label, path):
    """Record a file for a posting by its resolved path; a blank label takes the
    file's name. Raises ValueError unless the path is a file under the home folder."""
    real = _home_file(path)
    label = (label or "").strip() or Path(real).name
    conn = connect()
    with conn:
        cur = conn.execute("INSERT INTO attachments (posting_id, label, path, added_at) "
                           "VALUES (?, ?, ?, ?)", (posting_id, label, real, db.now()))
    return attachment(cur.lastrowid)


def delete_attachment(attachment_id):
    """Forget a file; the file itself stays. Returns its posting id, or None."""
    found = attachment(attachment_id)
    if found:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    return found and found["posting_id"]
