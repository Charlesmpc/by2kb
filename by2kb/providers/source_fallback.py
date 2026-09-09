"""One explicit, bounded fallback, including failures while expanding short URLs."""
from __future__ import annotations

from dataclasses import replace

from by2kb.errors import NeedsAuth, RateLimited, TransientProviderError, TerminalProviderError


RECOVERABLE = (NeedsAuth, RateLimited, TransientProviderError)


class FallbackSourceProvider:
    name = "fallback"

    def __init__(self, primary, fallback, source: str):
        self.primary = primary
        self.fallback = fallback
        self.source = source
        self.active = primary
        self.attempts = []

    def _record(self, provider, error):
        # Never persist exception text: third-party errors can contain signed URLs.
        self.attempts.append({"provider": provider.name, "category": type(error).__name__})

    def _raise_final(self, error):
        error.detail = {"attempts": list(self.attempts)}
        raise error

    async def resolve(self, source, client):
        try:
            return await self.primary.resolve(source, client)
        except RECOVERABLE as error:
            self._record(self.primary, error)
            self.active = self.fallback
            try:
                return await self.fallback.resolve(source, client)
            except (NeedsAuth, RateLimited, TransientProviderError, TerminalProviderError) as error:
                self._record(self.fallback, error)
                self._raise_final(error)

    async def prepare(self, identity, client, work_dir, options, *, set_stage, cancel_check):
        kwargs = dict(set_stage=set_stage, cancel_check=cancel_check)
        try:
            result = await self.active.prepare(identity, client, work_dir, options, **kwargs)
        except RECOVERABLE as error:
            self._record(self.active, error)
            if self.active is self.fallback:
                self._raise_final(error)
            cancel_check()
            self.active = self.fallback
            try:
                # Use the resolved canonical identity: never switch videos during fallback.
                result = await self.fallback.prepare(identity, client, work_dir, options, **kwargs)
            except (NeedsAuth, RateLimited, TransientProviderError, TerminalProviderError) as error:
                self._record(self.fallback, error)
                self._raise_final(error)
        payload = dict(result.source_payload)
        if self.attempts:
            payload["fallback_attempts"] = list(self.attempts)
        return replace(result, source_payload=payload)
