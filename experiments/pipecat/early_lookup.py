"""Read-only synthetic prefetch; transcript data never authorizes other actions."""

import asyncio
import re


class EarlyMuseumLookup:
    def __init__(self, lookup, publish, mark):
        self.lookup, self.publish, self.mark = lookup, publish, mark
        self.revision = 0
        self.text = ""
        self.result = None
        self.published = False
        self.speaking = False
        self.blocked = False
        self.tasks = set()
        self.lookup_task = None
        self.publish_task = None

    def begin_turn(self):
        self.revision += 1
        self.text, self.result = "", None
        self.published, self.blocked, self.speaking = False, False, True
        self.lookup_task, self.publish_task = None, None

    def quiet(self):
        self.speaking = False
        self._maybe_publish()

    def current(self, revision):
        return revision == self.revision and not self.blocked

    def _schedule(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def observe(self, delta):
        self.text = (self.text + delta)[-1500:]
        text = self.text.lower()
        if re.search(r"\b(actually|instead|cancel|forget|library|aquarium)\b|\bdo not\b|don't", text):
            self.blocked = True
            self.result = None
            self.mark("prefetch_invalidated", revision=self.revision)
            return
        if (
            not self.blocked
            and self.lookup_task is None
            and re.search(r"\bmuseum\b", text)
            and re.search(r"\bclosing\s+time\b|\bcloses\b|\bclosing\s+hours\b", text)
        ):
            self.mark("prefetch_started", revision=self.revision)
            self.lookup_task = self._schedule(self._lookup(self.revision))

    async def _lookup(self, revision):
        result = await self.lookup()
        if not self.current(revision):
            self.mark("prefetch_discarded", revision=revision)
            return
        self.result = result
        self.mark("prefetch_ready", revision=revision)
        self._maybe_publish()

    def _maybe_publish(self):
        if (
            self.result is not None
            and not self.speaking
            and not self.blocked
            and not self.published
            and self.publish_task is None
        ):
            self.publish_task = self._schedule(self._publish(self.revision, self.result))

    async def _publish(self, revision, result):
        if self.current(revision):
            # Reserve delivery before yielding so a concurrent backend callback
            # cannot announce the same completed result a second time.
            self.published = True
            event_id = await self.publish(result, revision, self.current)
            if not event_id and self.current(revision):
                self.published = False

    async def close(self):
        self.blocked = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
