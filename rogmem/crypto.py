import base64
import hashlib
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode())


def new_keypair() -> tuple[bytes, bytes]:
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    secret = private.private_bytes_raw()
    return secret, public


def code_for(pubkey: bytes) -> str:
    return hashlib.sha256(pubkey).hexdigest()[:8]


def seal(pubkey: bytes, plaintext: bytes) -> bytes:
    ephemeral = x25519.X25519PrivateKey.generate()
    shared = ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(pubkey))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rogmem-enroll").derive(shared)
    nonce = os.urandom(12)
    token = AESGCM(key).encrypt(nonce, plaintext, None)
    return ephemeral.public_key().public_bytes_raw() + nonce + token


def open_seal(private: bytes, wrapped: bytes) -> bytes:
    eph_pub, nonce, token = wrapped[:32], wrapped[32:44], wrapped[44:]
    shared = x25519.X25519PrivateKey.from_private_bytes(private).exchange(
        x25519.X25519PublicKey.from_public_bytes(eph_pub)
    )
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rogmem-enroll").derive(shared)
    return AESGCM(key).decrypt(nonce, token, None)
