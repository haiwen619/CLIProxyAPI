"""
shadowsocks_local.py — 纯 Python Shadowsocks SOCKS5 本地隧道
=====================================================================

公开接口：
    is_ss_config(raw: str) -> bool
    normalize_ss_url(raw: str) -> str | None
    get_ss_manager() -> SsManager

SsManager.resolve(normalized_url) -> str | None
    确保对应 SS 服务器的本地 SOCKS5 隧道已启动，返回 socks5://127.0.0.1:PORT

支持的加密方式：
    AEAD（推荐）: aes-128-gcm, aes-256-gcm, chacha20-ietf-poly1305
    流式（兼容旧服务端）: aes-128-cfb, aes-192-cfb, aes-256-cfb, rc4-md5

依赖：
    pip install cryptography
    pip install httpx[socks]   # 让 httpx 支持 socks5:// 代理（使用方需要）

典型用法：
    from tools.shadowsocks_local import is_ss_config, normalize_ss_url, get_ss_manager

    async def resolve_proxy_url(raw_proxy: str | None) -> str | None:
        if not raw_proxy:
            return None
        raw_proxy = raw_proxy.strip()
        if not raw_proxy:
            return None
        if is_ss_config(raw_proxy):
            normalized = normalize_ss_url(raw_proxy)
            if not normalized:
                raise ValueError("invalid ss proxy")
            local_socks5 = await get_ss_manager().resolve(normalized)
            if not local_socks5:
                raise RuntimeError("failed to start local shadowsocks tunnel")
            return local_socks5
        return raw_proxy

支持的 ss:// URL 格式：
    ss://cipher:password@host:port              # SIP002 明文
    ss://BASE64(cipher:password)@host:port      # SIP002 编码 userinfo
    ss://BASE64(cipher:password@host:port)      # 旧格式全量 base64
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["is_ss_config", "normalize_ss_url", "get_ss_manager", "SsManager"]

# ─────────────────────────────────────────────────────────────
# Cipher registry  name → (key_len, salt/iv_len, is_aead)
# ─────────────────────────────────────────────────────────────

_CIPHER_INFO: dict[str, tuple[int, int, bool]] = {
    # AEAD
    "aes-128-gcm":            (16, 16, True),
    "aes-256-gcm":            (32, 32, True),
    "chacha20-ietf-poly1305": (32, 32, True),
    # Stream (legacy)
    "aes-128-cfb":            (16, 16, False),
    "aes-192-cfb":            (24, 16, False),
    "aes-256-cfb":            (32, 16, False),
    "rc4-md5":                (16, 16, False),
}

_KNOWN_CIPHERS = frozenset(_CIPHER_INFO)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def is_ss_config(raw: str) -> bool:
    """Return True if *raw* looks like a shadowsocks proxy string."""
    return bool(raw) and raw.strip().lower().startswith("ss://")


def normalize_ss_url(raw: str) -> Optional[str]:
    """
    Parse any supported ss:// variant and return the canonical form
    ``ss://cipher:password@host:port``, or *None* on parse failure.
    """
    cfg = _parse_ss_url(raw)
    if cfg is None:
        return None
    url = f"ss://{cfg.cipher}:{cfg.password}@{cfg.host}:{cfg.port}"
    if cfg.obfs:
        from urllib.parse import quote
        plugin = f"obfs-local;obfs={cfg.obfs}"
        if cfg.obfs_host:
            plugin += f";obfs-host={cfg.obfs_host}"
        if cfg.obfs_path and cfg.obfs_path != "/":
            plugin += f";obfs-path={cfg.obfs_path}"
        url += f"?plugin={quote(plugin, safe='')}"
    return url


def get_ss_manager() -> "SsManager":
    """Return the process-wide :class:`SsManager` singleton."""
    return _manager


# ─────────────────────────────────────────────────────────────
# SsManager
# ─────────────────────────────────────────────────────────────

class SsManager:
    """
    Manages local SOCKS5 tunnels that forward traffic through Shadowsocks servers.

    Each unique ``(cipher, password, host, port)`` tuple gets exactly one
    persistent asyncio TCP server, bound to a random loopback port.  The server
    stays alive for the lifetime of the event loop.
    """

    def __init__(self) -> None:
        self._tunnels: dict[tuple, int] = {}
        self._servers: dict[tuple, asyncio.Server] = {}
        self._lock: Optional[asyncio.Lock] = None

    # asyncio.Lock must be created inside a running loop; create lazily.
    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def resolve(self, normalized_url: str) -> Optional[str]:
        """
        Ensure a local SOCKS5 server is running for the given SS config.

        *normalized_url* should be a canonical ``ss://cipher:password@host:port``
        string (as returned by :func:`normalize_ss_url`).

        Returns ``socks5://127.0.0.1:<port>`` on success, ``None`` on failure.
        """
        cfg = _parse_ss_url(normalized_url)
        if cfg is None:
            logger.error("SsManager.resolve: cannot parse SS URL: %.80r", normalized_url)
            return None

        key = (cfg.cipher, cfg.password, cfg.host, cfg.port, cfg.obfs, cfg.obfs_host, cfg.obfs_path)

        async with self._get_lock():
            if key in self._tunnels:
                port = self._tunnels[key]
                logger.debug(
                    "SS tunnel reuse: %s:%d → 127.0.0.1:%d", cfg.host, cfg.port, port
                )
                return f"socks5://127.0.0.1:{port}"

            # capture cfg in closure explicitly to avoid late-binding bugs
            _cfg = cfg
            try:
                server = await asyncio.start_server(
                    lambda r, w, c=_cfg: _handle_socks5(r, w, c),
                    host="127.0.0.1",
                    port=0,
                )
            except OSError as exc:
                logger.error("Failed to start local SOCKS5 server: %s", exc)
                return None

            port = server.sockets[0].getsockname()[1]
            self._tunnels[key] = port
            self._servers[key] = server  # keep reference alive

            logger.info(
                "SS tunnel started: %s:%d → 127.0.0.1:%d (cipher=%s)",
                cfg.host, cfg.port, port, cfg.cipher,
            )
            return f"socks5://127.0.0.1:{port}"

    async def close_all(self) -> None:
        """Close all managed tunnels (optional clean shutdown)."""
        async with self._get_lock():
            for server in self._servers.values():
                server.close()
                await server.wait_closed()
            self._servers.clear()
            self._tunnels.clear()


_manager = SsManager()


# ─────────────────────────────────────────────────────────────
# URL parsing
# ─────────────────────────────────────────────────────────────

@dataclass
class _SsConfig:
    cipher: str
    password: str
    host: str
    port: int
    obfs: str = ""        # "http" | "" (tls not yet implemented)
    obfs_host: str = ""
    obfs_path: str = "/"


def _b64_decode(s: str) -> Optional[bytes]:
    """Try URL-safe then standard base64, auto-adding missing padding."""
    for decode_fn in (base64.urlsafe_b64decode, base64.b64decode):
        for pad in ("", "=", "=="):
            try:
                return decode_fn(s + pad)
            except Exception:
                pass
    return None


def _parse_obfs_plugin(plugin_str: str) -> tuple[str, str, str]:
    """
    Parse obfs plugin string, e.g.
    ``obfs-local;obfs=http;obfs-host=example.com;obfs-path=/``

    Returns ``(obfs_mode, obfs_host, obfs_path)`` on success, or
    ``("", "", "/")`` for unknown/unsupported plugins (caller should treat as error).
    """
    parts = plugin_str.split(";")
    plugin_name = parts[0].strip()
    if plugin_name not in ("obfs-local", "simple-obfs"):
        logger.warning("SS plugin %r is not supported; ignoring proxy", plugin_name)
        return "", "", "/"

    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip()] = v.strip()

    obfs_mode = params.get("obfs", "http")
    if obfs_mode not in ("http",):
        # tls obfuscation is not yet implemented
        logger.warning("SS obfs mode %r is not yet supported; ignoring proxy", obfs_mode)
        return "", "", "/"

    return obfs_mode, params.get("obfs-host", ""), params.get("obfs-path", "/")


def _parse_userinfo(userinfo: str) -> Optional[Tuple[str, str]]:
    """
    Parse SS userinfo (the part before ``@``) into *(cipher, password)*.
    Accepts plain text, base64url, and standard base64.
    """
    # Plain: cipher:password
    if ":" in userinfo:
        cipher, _, pw = userinfo.partition(":")
        if cipher.lower() in _KNOWN_CIPHERS:
            return cipher.lower(), pw

    # Encoded
    raw = _b64_decode(userinfo)
    if raw:
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        if decoded and ":" in decoded:
            cipher, _, pw = decoded.partition(":")
            if cipher.lower() in _KNOWN_CIPHERS:
                return cipher.lower(), pw

    return None


def _parse_hostinfo(hostinfo: str) -> Optional[Tuple[str, int]]:
    """Parse ``host:port`` (IPv4/domain) or ``[::1]:port`` (IPv6)."""
    hostinfo = hostinfo.strip().rstrip("/")
    if hostinfo.startswith("["):
        try:
            end = hostinfo.index("]")
        except ValueError:
            return None
        host = hostinfo[1:end]
        rest = hostinfo[end + 1:]
        if not rest.startswith(":"):
            return None
        port_str = rest[1:]
    else:
        idx = hostinfo.rfind(":")
        if idx < 0:
            return None
        host, port_str = hostinfo[:idx], hostinfo[idx + 1:]

    try:
        port = int(port_str)
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    return host or None, port  # type: ignore[return-value]


def _parse_ss_url(raw: str) -> Optional[_SsConfig]:
    """Parse any supported ss:// URL variant."""
    raw = raw.strip()
    if not raw.lower().startswith("ss://"):
        return None

    body = raw[5:]

    # Strip fragment
    if "#" in body:
        body = body[: body.index("#")]

    # Plugin check — obfs-local/simple-obfs HTTP is supported natively
    obfs, obfs_host, obfs_path = "", "", "/"
    if "?" in body:
        body, qs = body.split("?", 1)
        if "plugin=" in qs:
            from urllib.parse import unquote
            # extract plugin= value from query string
            plugin_val = ""
            for part in qs.split("&"):
                if part.startswith("plugin="):
                    plugin_val = unquote(part[len("plugin="):])
                    break
            obfs, obfs_host, obfs_path = _parse_obfs_plugin(plugin_val)
            if not obfs and plugin_val:
                return None  # unsupported plugin

    if "@" not in body:
        # Old format: base64("cipher:password@host:port")
        decoded_bytes = _b64_decode(body)
        if not decoded_bytes:
            logger.debug("SS old-format base64 decode failed")
            return None
        try:
            decoded = decoded_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if "@" not in decoded:
            return None
        at = decoded.rfind("@")
        userinfo, hostinfo = decoded[:at], decoded[at + 1:]
    else:
        at = body.rfind("@")
        userinfo, hostinfo = body[:at], body[at + 1:]

    cipher_pw = _parse_userinfo(userinfo)
    if cipher_pw is None:
        logger.debug("SS cannot parse userinfo: %.40r", userinfo)
        return None

    host_port = _parse_hostinfo(hostinfo)
    if host_port is None or host_port[0] is None:
        logger.debug("SS cannot parse hostinfo: %.40r", hostinfo)
        return None

    cipher, password = cipher_pw
    host, port = host_port
    return _SsConfig(
        cipher=cipher, password=password, host=host, port=port,
        obfs=obfs, obfs_host=obfs_host, obfs_path=obfs_path,
    )


# ─────────────────────────────────────────────────────────────
# Key derivation
# ─────────────────────────────────────────────────────────────

def _evp_bytes_to_key(password: bytes, key_len: int) -> bytes:
    """OpenSSL EVP_BytesToKey with MD5, no salt, count=1."""
    d, d_i = b"", b""
    while len(d) < key_len:
        d_i = hashlib.md5(d_i + password).digest()
        d += d_i
    return d[:key_len]


def _hkdf_sha1(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA1 sub-key derivation used by SS AEAD ciphers."""
    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.backends import default_backend

    return HKDF(
        algorithm=SHA1(),
        length=length,
        salt=salt,
        info=info,
        backend=default_backend(),
    ).derive(ikm)


# ─────────────────────────────────────────────────────────────
# AEAD helpers
# ─────────────────────────────────────────────────────────────

_AEAD_TAG_LEN = 16
_AEAD_CHUNK_MAX = 0x3FFF  # 16383 bytes per chunk


def _make_aead(cipher_name: str, key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
    if cipher_name == "chacha20-ietf-poly1305":
        return ChaCha20Poly1305(key)
    return AESGCM(key)


def _nonce(counter: int) -> bytes:
    """12-byte little-endian nonce from a counter."""
    return counter.to_bytes(12, "little")


class _AeadEncoder:
    """Stateful AEAD encryptor for one direction of a SS stream."""

    def __init__(self, cipher_name: str, master_key: bytes, key_len: int) -> None:
        self._salt = os.urandom(key_len)
        subkey = _hkdf_sha1(master_key, self._salt, b"ss-subkey", key_len)
        self._aead = _make_aead(cipher_name, subkey)
        self._counter = 0

    @property
    def prefix(self) -> bytes:
        """Random salt that must be sent before any encrypted data."""
        return self._salt

    def _enc(self, data: bytes) -> bytes:
        ct = self._aead.encrypt(_nonce(self._counter), data, None)
        self._counter += 1
        return ct

    def pack(self, data: bytes) -> bytes:
        """Encrypt *data* into one or more AEAD-framed chunks."""
        if not data:
            return b""
        out: list[bytes] = []
        for i in range(0, len(data), _AEAD_CHUNK_MAX):
            chunk = data[i : i + _AEAD_CHUNK_MAX]
            out.append(self._enc(struct.pack("!H", len(chunk))))  # encrypted length
            out.append(self._enc(chunk))                          # encrypted payload
        return b"".join(out)


class _AeadDecoder:
    """Stateful AEAD decryptor for one direction of a SS stream."""

    def __init__(
        self, cipher_name: str, master_key: bytes, key_len: int, salt: bytes
    ) -> None:
        subkey = _hkdf_sha1(master_key, salt, b"ss-subkey", key_len)
        self._aead = _make_aead(cipher_name, subkey)
        self._counter = 0
        self._buf = b""
        self._dead = False

    def feed(self, data: bytes) -> bytes:
        """Append raw ciphertext and return all newly decrypted plaintext."""
        if self._dead:
            return b""
        self._buf += data
        out: list[bytes] = []
        while True:
            need_len = 2 + _AEAD_TAG_LEN
            if len(self._buf) < need_len:
                break
            try:
                length_plain = self._aead.decrypt(
                    _nonce(self._counter), self._buf[:need_len], None
                )
            except Exception:
                logger.debug("AEAD length decrypt failed — closing stream")
                self._dead = True
                self._buf = b""
                break

            payload_len = struct.unpack("!H", length_plain)[0]
            need_total = need_len + payload_len + _AEAD_TAG_LEN
            if len(self._buf) < need_total:
                break

            try:
                payload = self._aead.decrypt(
                    _nonce(self._counter + 1),
                    self._buf[need_len:need_total],
                    None,
                )
            except Exception:
                logger.debug("AEAD payload decrypt failed — closing stream")
                self._dead = True
                self._buf = b""
                break

            self._counter += 2
            self._buf = self._buf[need_total:]
            out.append(payload)

        return b"".join(out)


# ─────────────────────────────────────────────────────────────
# Stream cipher helpers
# ─────────────────────────────────────────────────────────────

def _stream_encryptor(cipher_name: str, master_key: bytes, iv_len: int):
    """Return *(iv_bytes, encryptor)* for the given stream cipher."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    iv = os.urandom(iv_len)
    backend = default_backend()
    if cipher_name in ("aes-128-cfb", "aes-192-cfb", "aes-256-cfb"):
        cipher = Cipher(algorithms.AES(master_key), modes.CFB(iv), backend=backend)
    elif cipher_name == "rc4-md5":
        rc4_key = hashlib.md5(master_key + iv).digest()
        cipher = Cipher(algorithms.ARC4(rc4_key), mode=None, backend=backend)
    else:
        raise ValueError(f"Unsupported stream cipher: {cipher_name}")
    return iv, cipher.encryptor()


def _stream_decryptor(cipher_name: str, master_key: bytes, iv: bytes):
    """Return a decryptor for the given stream cipher and IV."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    backend = default_backend()
    if cipher_name in ("aes-128-cfb", "aes-192-cfb", "aes-256-cfb"):
        cipher = Cipher(algorithms.AES(master_key), modes.CFB(iv), backend=backend)
    elif cipher_name == "rc4-md5":
        rc4_key = hashlib.md5(master_key + iv).digest()
        cipher = Cipher(algorithms.ARC4(rc4_key), mode=None, backend=backend)
    else:
        raise ValueError(f"Unsupported stream cipher: {cipher_name}")
    return cipher.decryptor()


# ─────────────────────────────────────────────────────────────
# SOCKS5 server
# ─────────────────────────────────────────────────────────────

_SOCKS5_VER = 5
_SOCKS5_CMD_CONNECT = 1
_SOCKS5_ATYP_IPV4 = 1
_SOCKS5_ATYP_DOMAIN = 3
_SOCKS5_ATYP_IPV6 = 4

_SOCKS5_SUCCESS = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
_SOCKS5_REFUSED = b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00"
_SOCKS5_ADDR_ERR = b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00"


async def _handle_socks5(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: _SsConfig,
) -> None:
    """Handle one inbound SOCKS5 client connection."""
    peer = writer.get_extra_info("peername", ("?", 0))
    try:
        # ── Greeting ────────────────────────────────────────
        hdr = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        if hdr[0] != _SOCKS5_VER:
            return
        await reader.readexactly(hdr[1])        # consume method list
        writer.write(b"\x05\x00")               # no-auth selected
        await writer.drain()

        # ── Request ─────────────────────────────────────────
        req = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        if req[0] != _SOCKS5_VER or req[1] != _SOCKS5_CMD_CONNECT:
            writer.write(_SOCKS5_REFUSED)
            await writer.drain()
            return

        atyp = req[3]
        if atyp == _SOCKS5_ATYP_IPV4:
            raw_addr = await reader.readexactly(4)
            ss_addr = bytes([atyp]) + raw_addr
        elif atyp == _SOCKS5_ATYP_DOMAIN:
            n = (await reader.readexactly(1))[0]
            domain = await reader.readexactly(n)
            ss_addr = bytes([atyp, n]) + domain
        elif atyp == _SOCKS5_ATYP_IPV6:
            raw_addr = await reader.readexactly(16)
            ss_addr = bytes([atyp]) + raw_addr
        else:
            writer.write(_SOCKS5_ADDR_ERR)
            await writer.drain()
            return

        port_bytes = await reader.readexactly(2)
        ss_header = ss_addr + port_bytes         # SS request header = ATYP+ADDR+PORT

        writer.write(_SOCKS5_SUCCESS)
        await writer.drain()

        logger.debug(
            "SOCKS5 CONNECT from %s:%d via SS %s:%d",
            peer[0], peer[1], cfg.host, cfg.port,
        )
        await _relay(reader, writer, cfg, ss_header)

    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as exc:
        logger.debug("SOCKS5 %s:%d: %s", peer[0], peer[1], exc)
    except Exception as exc:
        logger.debug("SOCKS5 %s:%d unexpected: %s", peer[0], peer[1], exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Relay: SOCKS5 client ↔ SS server
# ─────────────────────────────────────────────────────────────

async def _do_http_obfs_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    obfs_host: str,
    obfs_path: str = "/",
) -> None:
    """
    Perform the simple-obfs HTTP handshake.

    Sends a fake WebSocket-upgrade GET request and reads the server's
    HTTP 101 response.  After this call, *reader*/*writer* carry raw SS
    traffic with no further HTTP framing.
    """
    import secrets as _secrets
    ws_key = base64.b64encode(_secrets.token_bytes(16)).decode()
    host_header = obfs_host or "www.bing.com"

    request = (
        f"GET {obfs_path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"
        "Accept: */*\r\n"
        "Accept-Encoding: gzip, deflate\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Content-Length: 8388608\r\n"
        "\r\n"
    ).encode()

    writer.write(request)
    await writer.drain()

    # Read exactly up to and including \r\n\r\n; remaining bytes are SS data
    try:
        headers_raw = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=10,
        )
    except asyncio.LimitOverrunError:
        raise ConnectionError("obfs HTTP response headers too large")

    first_line = headers_raw.split(b"\r\n", 1)[0].decode(errors="replace")
    # accept 101 Switching Protocols or 200 OK (some servers differ)
    if "101" not in first_line and "200" not in first_line:
        raise ConnectionError(f"unexpected obfs HTTP response: {first_line!r}")

    logger.debug("obfs HTTP handshake OK: %s", first_line.strip())


async def _relay(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    cfg: _SsConfig,
    ss_header: bytes,
) -> None:
    """Open a TCP connection to the SS server and bidirectionally relay traffic."""
    key_len, salt_len, is_aead = _CIPHER_INFO[cfg.cipher]
    master_key = _evp_bytes_to_key(cfg.password.encode(), key_len)

    try:
        ss_r, ss_w = await asyncio.wait_for(
            asyncio.open_connection(cfg.host, cfg.port),
            timeout=15,
        )
    except Exception as exc:
        logger.warning("Cannot connect to SS server %s:%d — %s", cfg.host, cfg.port, exc)
        return

    try:
        # obfs-local HTTP handshake (if configured)
        if cfg.obfs == "http":
            await _do_http_obfs_handshake(ss_r, ss_w, cfg.obfs_host, cfg.obfs_path)

        if is_aead:
            enc = _AeadEncoder(cfg.cipher, master_key, key_len)
            # Send: [salt][encrypted(ss_header)]
            ss_w.write(enc.prefix + enc.pack(ss_header))
            await ss_w.drain()
            await asyncio.gather(
                _upload_aead(client_r, ss_w, enc),
                _download_aead(ss_r, client_w, cfg.cipher, master_key, key_len),
                return_exceptions=True,
            )
        else:
            iv, encryptor = _stream_encryptor(cfg.cipher, master_key, salt_len)
            # Send: [IV][encrypted(ss_header)]
            ss_w.write(iv + encryptor.update(ss_header))
            await ss_w.drain()
            await asyncio.gather(
                _upload_stream(client_r, ss_w, encryptor),
                _download_stream(ss_r, client_w, cfg.cipher, master_key, salt_len),
                return_exceptions=True,
            )
    finally:
        try:
            ss_w.close()
        except Exception:
            pass


async def _upload_aead(
    src: asyncio.StreamReader,
    dst_w: asyncio.StreamWriter,
    enc: _AeadEncoder,
) -> None:
    try:
        while True:
            data = await src.read(16384)
            if not data:
                break
            dst_w.write(enc.pack(data))
            await dst_w.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass


async def _download_aead(
    src: asyncio.StreamReader,
    dst_w: asyncio.StreamWriter,
    cipher_name: str,
    master_key: bytes,
    key_len: int,
) -> None:
    try:
        # First key_len bytes from server are the response salt
        salt = await asyncio.wait_for(src.readexactly(key_len), timeout=15)
        dec = _AeadDecoder(cipher_name, master_key, key_len, salt)
        while True:
            data = await src.read(16384)
            if not data:
                break
            plain = dec.feed(data)
            if plain:
                dst_w.write(plain)
                await dst_w.drain()
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
        pass


async def _upload_stream(
    src: asyncio.StreamReader,
    dst_w: asyncio.StreamWriter,
    encryptor,
) -> None:
    try:
        while True:
            data = await src.read(16384)
            if not data:
                break
            dst_w.write(encryptor.update(data))
            await dst_w.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass


async def _download_stream(
    src: asyncio.StreamReader,
    dst_w: asyncio.StreamWriter,
    cipher_name: str,
    master_key: bytes,
    iv_len: int,
) -> None:
    try:
        # First iv_len bytes from server are the response IV
        iv = await asyncio.wait_for(src.readexactly(iv_len), timeout=15)
        decryptor = _stream_decryptor(cipher_name, master_key, iv)
        while True:
            data = await src.read(16384)
            if not data:
                break
            dst_w.write(decryptor.update(data))
            await dst_w.drain()
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
        pass
