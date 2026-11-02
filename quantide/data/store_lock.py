"""Serialize a store's read/merge/publish operations across writers."""

from functools import wraps

from filelock import FileLock


def locked_store(method):
    """Keep a stable sibling lock file while data files are atomically replaced."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        path = self._store_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock", timeout=30):
            if method.__name__ == "append_data":
                self._collect_dates()
            return method(self, *args, **kwargs)

    return locked
