"""AES-256 ECB decryption used by GTA IV IMG archives.

Uses pycryptodome when available (fast), otherwise a small pure-Python
implementation so the add-on works inside Blender with no extra installs.
GTA IV decrypts every 16-byte block 16 times with the same key.
"""

import hashlib
import os

# SHA1 of the 32-byte AES key embedded in GTAIV.exe / EFLC.exe.
# Only the hash lives here; the key itself is read from the user's own game.
KEY_SHA1 = bytes.fromhex("dea375ef1e6ef2223a1221c2c575c47bf17efa5e")

try:
    from Crypto.Cipher import AES as _CryptoAES  # type: ignore
except Exception:  # pragma: no cover
    _CryptoAES = None


def find_key(exe_path, cache_path=None):
    """Scan the game executable for the AES key (verified via SHA1)."""
    if cache_path and os.path.isfile(cache_path):
        key = open(cache_path, "rb").read()
        if len(key) == 32 and hashlib.sha1(key).digest() == KEY_SHA1:
            return key
    data = open(exe_path, "rb").read()
    sha1 = hashlib.sha1
    for off in range(0, len(data) - 32):
        if sha1(data[off:off + 32]).digest() == KEY_SHA1:
            key = data[off:off + 32]
            if cache_path:
                try:
                    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
                    with open(cache_path, "wb") as f:
                        f.write(key)
                except OSError:
                    pass
            return key
    raise RuntimeError("Could not find the AES key in %s" % exe_path)


# ---------------------------------------------------------------- pure python
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i


def _xt(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


_M9 = [_mul(i, 9) for i in range(256)]
_M11 = [_mul(i, 11) for i in range(256)]
_M13 = [_mul(i, 13) for i in range(256)]
_M14 = [_mul(i, 14) for i in range(256)]


def _expand_key(key):
    nk, nr = 8, 14
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xt(rcon)
        elif i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(nr + 1)]


class _PyAES:
    def __init__(self, key):
        self.rk = _expand_key(key)

    def decrypt_block(self, blk):
        rk = self.rk
        s = [blk[i] ^ rk[14][i] for i in range(16)]
        for rnd in range(13, -1, -1):
            # inv shift rows
            s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
                 s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
            s = [_INV_SBOX[b] for b in s]
            k = rk[rnd]
            s = [s[i] ^ k[i] for i in range(16)]
            if rnd:
                o = []
                for c in range(4):
                    a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                    o += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3],
                          _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                          _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3],
                          _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
                s = o
        return s


def decrypt(data, key):
    """Decrypt whole 16-byte blocks of `data` (trailing bytes left as-is)."""
    n = len(data) & ~15
    if n == 0:
        return bytes(data)
    if _CryptoAES is not None:
        c = _CryptoAES.new(key, _CryptoAES.MODE_ECB)
        out = bytes(data[:n])
        for _ in range(16):
            out = c.decrypt(out)
        return out + bytes(data[n:])
    aes = _PyAES(key)
    out = bytearray(data)
    for off in range(0, n, 16):
        blk = list(out[off:off + 16])
        for _ in range(16):
            blk = aes.decrypt_block(blk)
        out[off:off + 16] = bytes(blk)
    return bytes(out)
