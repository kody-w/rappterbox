"""
local_storage.py — kernel-sibling shim.

The canonical kernel (`brainstem.py`) imports this module by its bare
name during shim registration:

    from local_storage import AzureFileStorageManager  # brainstem.py:689

The actual implementation lives at `utils/local_storage.py` so the
brainstem root stays minimal (Article XVI). This file is the
kernel-adjacent shim that lets the bare import resolve on any layout
without requiring a kernel-side `sys.path.insert(...)`.

Why a kernel sibling and not a kernel edit:
- The kernel is the digital organism's DNA (Constitution Article XXXIII).
  It must drop in onto any locally-mutated organism without modification.
- Anything the kernel imports at module-load or shim-registration time
  must exist at the kernel's own directory level. The kernel ships its
  own siblings; it does not reach into the mutation surface.
- This file is the first such sibling, recorded as Fixture 01 in the
  vault: `pages/vault/Fixtures/Fixture 01 — Canonical Kernel local_storage Drop-In.md`.
"""

from utils.local_storage import AzureFileStorageManager  # noqa: F401

__all__ = ["AzureFileStorageManager"]
