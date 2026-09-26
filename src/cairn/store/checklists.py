"""The to-do list each application carries."""
from cairn.store import db
from cairn.store.db import connect

_ITEM = "SELECT id, posting_id, label, done, position FROM checklist "


def _item(row):
    return {**dict(row), "done": bool(row["done"])}


def _label(label):
    label = (label or "").strip() if isinstance(label, str) else ""
    if not label:
        raise ValueError("a checklist item needs a label")
    return label


def checklist(posting_id):
    rows = connect().execute(_ITEM + "WHERE posting_id = ? ORDER BY position, id",
                             (posting_id,))
    return [_item(row) for row in rows]


def checklist_item(item_id):
    row = connect().execute(_ITEM + "WHERE id = ?", (item_id,)).fetchone()
    return _item(row) if row else None


def add_item(posting_id, label):
    """Append an item to a posting's checklist. Raises ValueError for a blank label."""
    label = _label(label)
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO checklist (posting_id, label, done, position, created_at) "
            "SELECT ?, ?, 0, coalesce(max(position) + 1, 0), ? FROM checklist "
            "WHERE posting_id = ?", (posting_id, label, db.now(), posting_id))
    return checklist_item(cur.lastrowid)


def set_item(item_id, label=None, done=None):
    """Change what is given of an item. Returns it, or None for no such item.

    Raises ValueError for a blank label.
    """
    changes = {}
    if label is not None:
        changes["label"] = _label(label)
    if done is not None:
        changes["done"] = int(bool(done))
    if changes:
        conn = connect()
        with conn:
            conn.execute(f"UPDATE checklist SET "
                         f"{', '.join(f'{name} = ?' for name in changes)} WHERE id = ?",
                         [*changes.values(), item_id])
    return checklist_item(item_id)


def delete_item(item_id):
    """Delete an item. Returns its posting id, or None for no such item."""
    found = checklist_item(item_id)
    if found:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM checklist WHERE id = ?", (item_id,))
    return found and found["posting_id"]


def reorder(posting_id, ids):
    """Put a posting's checklist in the order of ids and return it.

    Raises ValueError unless ids holds each of its item ids exactly once.
    """
    ids = list(ids)
    conn = connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        held = [row[0] for row in conn.execute("SELECT id FROM checklist WHERE posting_id = ?",
                                               (posting_id,))]
        if len(ids) != len(held) or set(ids) != set(held):
            raise ValueError("the order must list every item of the checklist once")
        conn.executemany("UPDATE checklist SET position = ? WHERE id = ?",
                         [(position, item_id) for position, item_id in enumerate(ids)])
    return checklist(posting_id)
