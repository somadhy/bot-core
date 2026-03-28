"""Admin checks by full public key hex."""

from __future__ import annotations


def _norm(s: str) -> str:
    return s.strip().lower().replace(" ", "")


def is_admin(public_key_hex: str | None, admin_keys: list[str]) -> bool:
    if not public_key_hex or not admin_keys:
        return False
    pk = _norm(public_key_hex)
    return any(pk == _norm(a) for a in admin_keys)
