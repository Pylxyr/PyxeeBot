from __future__ import annotations

import types

import nacl.secret
import nacl.utils
from discord.voice_state import VoiceConnectionState  # noqa: F401  (import must keep working)


def test_pynacl_aead_primitives_used_by_discord_roundtrip() -> None:
    """discord.py's voice encryption relies on these PyNaCl APIs; PyNaCl is pinned past the
    `discord.py[voice]` extra's cap, so make sure the surface it uses still works."""
    key = nacl.utils.random(nacl.secret.Aead.KEY_SIZE)
    box = nacl.secret.Aead(key)
    nonce = nacl.utils.random(nacl.secret.Aead.NONCE_SIZE)
    sealed = box.encrypt(b"opus-frame", b"header", nonce)
    assert box.decrypt(sealed.ciphertext, b"header", nonce) == b"opus-frame"
    secret = nacl.secret.SecretBox(nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE))
    assert secret.decrypt(secret.encrypt(b"x")) == b"x"


def test_voice_extras_importable() -> None:
    import davey  # noqa: F401

    assert types.ModuleType("ok")
