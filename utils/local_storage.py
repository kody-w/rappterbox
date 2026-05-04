"""
LocalStorageManager — drop-in replacement for AzureFileStorageManager.
Mirrors the CommunityRAPP storage layout:
  shared_memories/memory.json   — shared memories
  memory/{guid}/user_memory.json — per-user memories
Data lives in .brainstem_data/ next to this file.

Concurrency: writes are atomic and serialized.

The brainstem runs Flask in threaded mode by default, so two agent
calls that both modify the same JSON file can race. This module
defends against torn writes (partial JSON on disk after a crash or
interleaved write) and against last-writer-wins corruption (two
threads writing simultaneously stomp on each other) using:

  1. **Atomic write-then-rename**: writes go to `<path>.tmp.<pid>`
     first, then `os.replace(tmp, final)`. Replace is atomic on
     POSIX and Windows — readers always see either the old full
     contents or the new full contents, never a partial file.
  2. **Per-path threading.Lock**: in-process serialization so two
     concurrent writes to the same file can't interleave their
     read-modify steps.
  3. **OS file lock (best-effort)**: `fcntl.flock` on Unix to
     coordinate across multiple brainstem processes pointing at the
     same data dir. Skipped silently on platforms where fcntl is
     unavailable (Windows) — threading.Lock still covers the common
     single-process case.

Lost-update across separate read-modify-write boundaries (agent reads,
agent thinks, agent writes — another agent slips in between) is a
distinct concern that requires explicit transaction boundaries; see
`update_json()` below for the locked read-modify-write helper.
"""

import os
import json
import logging
import threading

try:
    import fcntl as _fcntl  # POSIX
except ImportError:
    _fcntl = None

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".brainstem_data")

# In-process lock registry. Acquire _LOCK_REGISTRY_LOCK to add a key,
# then use the per-path lock for actual write serialization. Locks are
# never removed (memory cost is one threading.Lock per file path ever
# written to in this process — bounded by user files).
_LOCK_REGISTRY: dict[str, threading.Lock] = {}
_LOCK_REGISTRY_LOCK = threading.Lock()


def _path_lock(path: str) -> threading.Lock:
    """Return the threading.Lock for `path` (creating it on first ask)."""
    abspath = os.path.abspath(path)
    lock = _LOCK_REGISTRY.get(abspath)
    if lock is None:
        with _LOCK_REGISTRY_LOCK:
            lock = _LOCK_REGISTRY.get(abspath)
            if lock is None:
                lock = threading.Lock()
                _LOCK_REGISTRY[abspath] = lock
    return lock


def _atomic_write(path: str, render) -> None:
    """Write a file atomically: render bytes/text into a tempfile next
    to the target, fsync, then os.replace into place. Holds the per-path
    threading.Lock for the duration; tries to also hold a cross-process
    fcntl lock when the platform supports it.

    `render` is a callable that takes an open file handle (text mode,
    utf-8) and writes the desired content. The handle is the tempfile;
    the caller never sees the final path being touched mid-write.
    """
    final_path = os.path.abspath(path)
    folder = os.path.dirname(final_path)
    os.makedirs(folder, exist_ok=True)

    # Tempfile colocated with target so os.replace is same-filesystem
    # (cross-fs replace is not atomic).
    tmp_path = f"{final_path}.tmp.{os.getpid()}.{threading.get_ident()}"

    with _path_lock(final_path):
        # Cross-process best-effort: lock a sibling .lock file. This
        # works only on POSIX; Windows users get the in-process lock
        # alone, which is fine for the common single-brainstem case.
        lock_handle = None
        if _fcntl is not None:
            try:
                lock_handle = open(final_path + ".lock", "w")
                _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
            except (OSError, ValueError):
                lock_handle = None
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                render(fh)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync on some FS / platforms isn't supported;
                    # the rename is still atomic.
                    pass
            os.replace(tmp_path, final_path)
        finally:
            if lock_handle is not None:
                try:
                    _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_UN)
                except (OSError, ValueError):
                    pass
                try:
                    lock_handle.close()
                except OSError:
                    pass
            # Tempfile cleanup if rename failed
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


class AzureFileStorageManager:
    """
    Local-first shim that mirrors the AzureFileStorageManager API from
    CommunityRAPP.  Agents import this transparently via the shim in brainstem.py.
    """

    DEFAULT_MARKER_GUID = "c0p110t0-aaaa-bbbb-cccc-123456789abc"

    def __init__(self, share_name=None, **kwargs):
        self.current_guid = None
        # Matches CommunityRAPP paths
        self.shared_memory_path = "shared_memories"
        self.default_file_name = "memory.json"
        self.current_memory_path = self.shared_memory_path
        os.makedirs(_DATA_DIR, exist_ok=True)

    # ── Context ───────────────────────────────────────────────────────────

    def set_memory_context(self, user_guid=None):
        """Set the memory context — matches CommunityRAPP's set_memory_context."""
        if not user_guid or user_guid == self.DEFAULT_MARKER_GUID:
            self.current_guid = None
            self.current_memory_path = self.shared_memory_path
            return True

        # Valid GUID — set up user-specific path (memory/{guid})
        self.current_guid = user_guid
        self.current_memory_path = f"memory/{user_guid}"
        return True

    # ── Core I/O ──────────────────────────────────────────────────────────

    def _file_path(self):
        """Return the absolute path for the current memory file.
        Shared:  .brainstem_data/shared_memories/memory.json
        User:    .brainstem_data/memory/{guid}/user_memory.json
        """
        if self.current_guid:
            folder = os.path.join(_DATA_DIR, self.current_memory_path)
            fname = "user_memory.json"
        else:
            folder = os.path.join(_DATA_DIR, self.shared_memory_path)
            fname = self.default_file_name
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, fname)

    def read_json(self, file_path=None):
        """Read JSON data from local storage."""
        path = file_path or self._file_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def write_json(self, data, file_path=None):
        """Write JSON data to local storage atomically.

        Two concurrent calls for the same path serialize on a per-path
        threading.Lock; the bytes hit disk via os.replace so readers
        never see a torn file. See module docstring for the full
        concurrency story.
        """
        path = file_path or self._file_path()
        _atomic_write(path, lambda fh: json.dump(data, fh, indent=2, default=str))
        return True

    def update_json(self, mutator, file_path=None):
        """Locked read-modify-write transaction.

        Reads the current JSON, calls `mutator(data)` (which mutates
        in place or returns a new dict), and writes the result —
        all under the same per-path lock so no other write can slip
        in between read and write. Returns the final data.

        Example:
            mgr.update_json(lambda d: d.setdefault("memories", []).append(entry))
        """
        path = os.path.abspath(file_path or self._file_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _path_lock(path):
            # Read current state
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, IOError):
                    data = {}
            else:
                data = {}
            # Mutate
            result = mutator(data)
            if result is not None:
                data = result
            # Write atomically (re-enters _path_lock — RLock-free since
            # threading.Lock isn't reentrant; we already hold it, so
            # call _atomic_write which would deadlock. Inline the
            # rename instead.)
            tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
        return data

    # ── Convenience methods used by some agents ───────────────────────────

    def read_file(self, file_path):
        full = os.path.join(_DATA_DIR, file_path)
        if not os.path.exists(full):
            return None
        with open(full, "r", encoding="utf-8") as f:
            return f.read()

    def write_file(self, file_path, content):
        full = os.path.join(_DATA_DIR, file_path)
        _atomic_write(full, lambda fh: fh.write(content))
        return True

    def list_files(self, directory=""):
        full = os.path.join(_DATA_DIR, directory)
        if not os.path.exists(full):
            return []
        return os.listdir(full)

    def delete_file(self, file_path):
        full = os.path.join(_DATA_DIR, file_path)
        if os.path.exists(full):
            os.remove(full)
            return True
        return False

    def file_exists(self, file_path):
        return os.path.exists(os.path.join(_DATA_DIR, file_path))
