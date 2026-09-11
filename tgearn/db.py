"""Small SQLite boundary shared as code, never as a database between bots."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3


class DomainError(ValueError):
    """A safe, human-readable error that does not include user input or secrets."""


class Database:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self, schema):
        with self.read() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(schema)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def read(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.read() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


class UpdateCursor:
    def __init__(self, database):
        self.database = database
        database.initialize("CREATE TABLE IF NOT EXISTS runtime_cursor (id INTEGER PRIMARY KEY CHECK(id=1), next_update INTEGER NOT NULL)")

    def read(self):
        with self.database.read() as connection:
            row = connection.execute("SELECT next_update FROM runtime_cursor WHERE id=1").fetchone()
            return row[0] if row else 0

    def advance(self, update_id):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_cursor(id,next_update) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET next_update=MAX(next_update,excluded.next_update)",
                (update_id + 1,),
            )
