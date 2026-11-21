"""Authentication tokens, session IDs, and TeamViewer pairing configuration."""

from __future__ import annotations

import hmac
import json
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any

VALID_PAIRING_MODES: frozenset[str] = frozenset({"direct", "relay"})
PAIRING_URI_SCHEME: str = "maxpair"
_SESSION_ID_ALPHABET: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_REQUIRED_URI_KEYS: frozenset[str] = frozenset({"server_url", "session_id", "token"})


def generate_session_id(length: int = 4) -> str:
    """Generate a human-readable session ID like 'S-4K9P'."""
    if isinstance(length, bool) or not isinstance(length, int) or length < 4:
        raise ValueError("length must be an int >= 4")
    suffix = "".join(secrets.choice(_SESSION_ID_ALPHABET) for _ in range(length))
    return f"S-{suffix}"


def generate_token(nbytes: int = 32) -> str:
    """Generate a URL-safe high-entropy bearer token."""
    if isinstance(nbytes, bool) or not isinstance(nbytes, int) or nbytes < 16:
        raise ValueError("nbytes must be an int >= 16")
    return secrets.token_urlsafe(nbytes)


def verify_token(expected: str, provided: str | None) -> bool:
    """Constant-time bearer token verification safe for None, non-str, and non-ASCII."""
    if not isinstance(expected, str) or not isinstance(provided, str):
        return False
    if not expected or not provided or not expected.strip() or not provided.strip():
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def extract_bearer_token(auth_header: str | None) -> str | None:
    """Extract bearer token from an HTTP Authorization header, or return None."""
    if not isinstance(auth_header, str):
        return None
    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token or any(ch.isspace() for ch in token):
        return None
    return token


def _validate_server_url(server_url: Any) -> str:
    if not isinstance(server_url, str):
        raise ValueError("server_url must be a str")
    if not server_url or server_url != server_url.strip() or any(
        ch.isspace() for ch in server_url
    ):
        raise ValueError("server_url must be non-empty and contain no whitespace")
    if not server_url.startswith(("http://", "https://")):
        raise ValueError("server_url must start with 'http://' or 'https://'")

    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported server_url scheme: {parsed.scheme!r}")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("server_url must include a valid hostname")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in server_url: {server_url!r}") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("server_url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("server_url must not contain query parameters or fragments")

    return server_url.rstrip("/")


def _validate_no_ws_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise ValueError(f"{field_name} must be a non-empty string without whitespace")
    return value


@dataclass
class PairingConfig:
    """Client/Operator pairing configuration exchangeable via connect.json or maxpair:// URI."""

    mode: str
    server_url: str
    session_id: str
    token: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in VALID_PAIRING_MODES:
            raise ValueError(
                f"mode must be one of {sorted(VALID_PAIRING_MODES)}, got {self.mode!r}"
            )
        self.server_url = _validate_server_url(self.server_url)
        self.session_id = _validate_no_ws_str(self.session_id, "session_id")
        self.token = _validate_no_ws_str(self.token, "token")

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "server_url": self.server_url,
            "session_id": self.session_id,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PairingConfig:
        if not isinstance(data, dict):
            raise ValueError(
                f"PairingConfig.from_dict expects a dict, got {type(data).__name__}"
            )
        required = ("mode", "server_url", "session_id", "token")
        for key in required:
            if key not in data:
                raise ValueError(f"PairingConfig missing required field: {key!r}")
            if not isinstance(data[key], str):
                raise ValueError(
                    f"PairingConfig field {key!r} must be str, got {type(data[key]).__name__}"
                )
        return cls(
            mode=data["mode"],
            server_url=data["server_url"],
            session_id=data["session_id"],
            token=data["token"],
        )

    def to_connect_json(self) -> str:
        """Serialize pairing configuration to formatted connect.json string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_connect_json(cls, raw: str) -> PairingConfig:
        """Parse and validate pairing configuration from connect.json string."""
        if not isinstance(raw, str):
            raise ValueError("connect.json content must be a str")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid connect.json: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("connect.json root must be a JSON object")
        return cls.from_dict(parsed)

    def to_pairing_uri(self) -> str:
        """Encode pairing configuration into a compact maxpair:// URI."""
        query = urllib.parse.urlencode(
            [
                ("server_url", self.server_url),
                ("session_id", self.session_id),
                ("token", self.token),
            ],
            quote_via=urllib.parse.quote,
        )
        return f"{PAIRING_URI_SCHEME}://{self.mode}?{query}"

    @classmethod
    def from_pairing_uri(cls, uri: str) -> PairingConfig:
        """Parse and strictly validate a maxpair:// pairing URI."""
        if not isinstance(uri, str) or not uri or uri != uri.strip() or any(
            ch.isspace() for ch in uri
        ):
            raise ValueError("Pairing URI must be a non-empty string without whitespace")
        if not uri.startswith(f"{PAIRING_URI_SCHEME}://"):
            raise ValueError(f"Pairing URI must start with '{PAIRING_URI_SCHEME}://'")

        parsed = urllib.parse.urlsplit(uri)
        if parsed.scheme != PAIRING_URI_SCHEME:
            raise ValueError(f"Invalid pairing URI scheme: {parsed.scheme!r}")
        if parsed.netloc not in VALID_PAIRING_MODES:
            raise ValueError(f"Invalid pairing mode in URI: {parsed.netloc!r}")
        if parsed.path:
            raise ValueError(f"Unexpected path in pairing URI: {parsed.path!r}")
        if parsed.fragment:
            raise ValueError("Pairing URI must not contain a fragment")
        if not parsed.query:
            raise ValueError("Pairing URI is missing required query parameters")

        try:
            pairs = urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True
            )
        except ValueError as exc:
            raise ValueError(f"Malformed query string in pairing URI: {exc}") from exc

        params: dict[str, str] = {}
        for key, value in pairs:
            if key in params:
                raise ValueError(f"Duplicate query parameter in pairing URI: {key!r}")
            params[key] = value

        keys = set(params.keys())
        missing = _REQUIRED_URI_KEYS - keys
        if missing:
            raise ValueError(
                f"Pairing URI missing required parameter(s): {sorted(missing)}"
            )
        extra = keys - _REQUIRED_URI_KEYS
        if extra:
            raise ValueError(
                f"Pairing URI contains unknown parameter(s): {sorted(extra)}"
            )

        return cls(
            mode=parsed.netloc,
            server_url=params["server_url"],
            session_id=params["session_id"],
            token=params["token"],
        )


__all__ = [
    "VALID_PAIRING_MODES",
    "PAIRING_URI_SCHEME",
    "generate_session_id",
    "generate_token",
    "verify_token",
    "extract_bearer_token",
    "PairingConfig",
]
