"""Submitter/approver notifications (guide Phase 4).

`Notifier` is the interface; `LocalNotifier` records messages in-memory for tests,
`LogicAppNotifier` posts to a Logic App HTTP trigger (Teams/email) in production.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class Notification(BaseModel):
    to: str
    subject: str
    body: str
    kind: str = "info"      # info | approved | rejected | approval_request


class Notifier(Protocol):
    async def notify(self, notification: Notification) -> None: ...


class LocalNotifier:
    """Collects notifications so tests can assert on them; prints in dev."""

    def __init__(self, echo: bool = False) -> None:
        self.sent: list[Notification] = []
        self._echo = echo

    async def notify(self, notification: Notification) -> None:
        self.sent.append(notification)
        if self._echo:
            print(f"[notify:{notification.kind}] to={notification.to} :: {notification.subject}")


class LogicAppNotifier:
    """POSTs the notification to a Logic App HTTP trigger."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def notify(self, notification: Notification) -> None:
        import urllib.request

        req = urllib.request.Request(
            self._url,
            data=notification.model_dump_json().encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).close()  # noqa: S310
