from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Dict, List, Optional, Tuple


@dataclass
class Session:
    session_id: str
    csrf_token: str
    authenticated: bool = False
    expires_at: float = 0
    flashes: List[Tuple[str, str]] = field(default_factory=list)


class SessionManager:
    COOKIE_NAME = "ebay_sandbox_session"

    def __init__(self, secure_cookie: bool, now=time.time, ttl_seconds: int = 3600) -> None:
        self.secure_cookie = secure_cookie
        self.now = now
        self.ttl_seconds = ttl_seconds
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, cookie_header: str) -> Tuple[Session, bool]:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
        except Exception:
            cookie = SimpleCookie()
        session_id = cookie.get(self.COOKIE_NAME).value if cookie.get(self.COOKIE_NAME) else None
        with self._lock:
            self._purge()
            session = self._sessions.get(session_id or "")
            if session is None:
                session = self._new_session()
                self._sessions[session.session_id] = session
                return session, True
            session.expires_at = self.now() + self.ttl_seconds
            return session, False

    def authenticate(self, old_session: Session) -> Session:
        with self._lock:
            self._sessions.pop(old_session.session_id, None)
            session = self._new_session()
            session.authenticated = True
            self._sessions[session.session_id] = session
            return session

    def destroy(self, session: Session) -> None:
        with self._lock:
            self._sessions.pop(session.session_id, None)

    def cookie_header(self, session: Session, clear: bool = False) -> str:
        cookie = SimpleCookie()
        cookie[self.COOKIE_NAME] = "" if clear else session.session_id
        morsel = cookie[self.COOKIE_NAME]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if self.secure_cookie:
            morsel["secure"] = True
        if clear:
            morsel["max-age"] = 0
        else:
            morsel["max-age"] = self.ttl_seconds
        return morsel.OutputString()

    def _new_session(self) -> Session:
        return Session(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self.now() + self.ttl_seconds,
        )

    def _purge(self) -> None:
        now = self.now()
        expired = [key for key, session in self._sessions.items() if session.expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)
        while len(self._sessions) >= 1000:
            oldest = min(self._sessions.values(), key=lambda session: session.expires_at)
            self._sessions.pop(oldest.session_id, None)
