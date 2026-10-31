# -*- coding: utf-8 -*-
"""WM004 NCM reader: the one place the sealed file formats are written and opened. Byte formats are in CRYPTO.md.
Used by ncm_reader_action.py on the GitHub runner (public key only, it can seal but never open), by mkncm_keys.py and by ncm_auto.py on the laptop.
Needs the cryptography package. Holds no key and no access code."""
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b'WMN1'             # four bytes, format version 1
INFO = b'wm004-ncm-v1'      # HKDF info
HEADER = 4 + 65 + 12        # magic, ephemeral public point, iv
ROUNDS = 1000000            # PBKDF2 rounds, the same as the console gate


def normal_code(code):
    """The console gate strips every whitespace character before it derives a key. Do the same here or the two drift apart."""
    return ''.join(str(code).split())


def _aes_key(shared):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(shared)


def public_from_pem(pem):
    return serialization.load_pem_public_key(pem if isinstance(pem, bytes) else pem.encode('ascii'))


def private_from_pem(pem):
    return serialization.load_pem_private_key(pem if isinstance(pem, bytes) else pem.encode('ascii'), password=None)


def seal(plain, public_key):
    """plain: bytes. Returns magic + ephemeral point + iv + ciphertext with its tag. The first 81 bytes are the additional data, so the header cannot be changed."""
    eph = ec.generate_private_key(ec.SECP256R1())
    point = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    iv = os.urandom(12)
    head = MAGIC + point + iv
    return head + AESGCM(_aes_key(eph.exchange(ec.ECDH(), public_key))).encrypt(iv, plain, head)


def open_sealed(blob, private_key):
    """Raises ValueError on a wrong magic or a short file, and cryptography's InvalidTag on a wrong key or a changed byte."""
    if len(blob) < HEADER + 16 or blob[:4] != MAGIC:
        raise ValueError('not a WMN1 file')
    point, iv = blob[4:69], blob[69:81]
    eph = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    return AESGCM(_aes_key(private_key.exchange(ec.ECDH(), eph))).decrypt(iv, blob[HEADER:], blob[:HEADER])


def frame(blob):
    """One sealed blob as a frame of a .enc file: four byte big endian length, then the blob."""
    return struct.pack('>I', len(blob)) + blob


def frames(data):
    """Split a .enc file into its sealed blobs without opening any of them. A cut or damaged end raises ValueError, because silently losing the tail of a record is not acceptable."""
    out, i = [], 0
    while i < len(data):
        if i + 4 > len(data):
            raise ValueError('cut frame header at byte %d' % i)
        n = struct.unpack('>I', data[i:i + 4])[0]
        if n < HEADER + 16 or i + 4 + n > len(data):
            raise ValueError('bad frame length at byte %d' % i)
        out.append(data[i + 4:i + 4 + n])
        i += 4 + n
    return out


def _code_key(code, salt, rounds=ROUNDS):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=rounds).derive(normal_code(code).encode('utf-8'))


def pkcs8_der(private_key):
    return private_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def wrap_key(private_key, code, rounds=ROUNDS):
    """key.bin: salt 16, iv 12, AES 256 GCM ciphertext of the PKCS8 DER key with its tag. No additional data."""
    salt, iv = os.urandom(16), os.urandom(12)
    return salt + iv + AESGCM(_code_key(code, salt, rounds)).encrypt(iv, pkcs8_der(private_key), None)


def unwrap_key(blob, code, rounds=ROUNDS):
    """Returns the private key, or raises cryptography's InvalidTag on a wrong code."""
    der = AESGCM(_code_key(code, blob[:16], rounds)).decrypt(blob[16:28], blob[28:], None)
    return serialization.load_der_private_key(der, password=None)
