"""Regression tests for SQLiteDB connections crossing app/test threads."""

from __future__ import annotations

import threading

from quantide.data.sqlite import db


def test_thread_connection_is_rebound_after_database_reinit(tmp_path):
    """A worker must not reuse the main thread's connection after rebind."""
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    db.init(first_path)
    main_connection = db.db.conn

    worker_connections = []
    worker_errors = []
    worker_ready = threading.Event()
    rebind = threading.Event()

    def read_connection_and_rebind() -> None:
        try:
            worker_connections.append(db.db.conn)
            assert db.table_names()
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)
        finally:
            worker_ready.set()

        if worker_errors:
            return

        try:
            assert rebind.wait(timeout=5)
            worker_connections.append(db.db.conn)
            assert db.db_path == str(second_path)
            assert db.table_names()
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    worker = threading.Thread(target=read_connection_and_rebind)
    worker.start()
    assert worker_ready.wait(timeout=5)

    db.init(second_path)
    rebind.set()
    worker.join()

    assert not worker.is_alive()
    assert not worker_errors
    assert worker_connections[0] is not main_connection
    assert worker_connections[1] is not worker_connections[0]


def test_closed_thread_connection_is_rebound_on_thread_reuse(tmp_path):
    """A reused thread must replace a connection closed outside SQLiteDB."""
    db.init(tmp_path / "closed.db")

    worker_connections = []
    worker_errors = []
    connection_closed = threading.Event()
    check_connection = threading.Event()

    def use_reused_thread() -> None:
        try:
            first_connection = db.db.conn
            worker_connections.append(first_connection)
            first_connection.close()
            connection_closed.set()

            assert check_connection.wait(timeout=5)
            second_connection = db.db.conn
            worker_connections.append(second_connection)
            db.conn.commit()
            assert db.table_names()
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)
            connection_closed.set()

    worker = threading.Thread(target=use_reused_thread)
    worker.start()
    assert connection_closed.wait(timeout=5)
    check_connection.set()
    worker.join()

    assert not worker.is_alive()
    assert not worker_errors
    assert len(worker_connections) == 2
    assert worker_connections[1] is not worker_connections[0]
