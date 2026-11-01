"""联系人澄清的短期状态；按发起者和原会话隔离，不持久化候选选择。"""
from __future__ import annotations

import time


class NeedsTarget(ValueError):
    def __init__(self, name, candidates=()):
        self.name = str(name)
        self.candidates = list(candidates)
        super().__init__("联系人需要确认")


class PendingTargets:
    def __init__(self, ttl=300, limit=1000):
        self.ttl = ttl
        self.limit = limit
        self.items = {}

    def prune(self):
        now = time.time()
        self.items = {key: value for key, value in self.items.items() if value['expires'] > now}

    def put(self, owner, origin, **request):
        self.prune()
        key = (owner, origin)
        self.items.pop(key, None)
        if len(self.items) >= self.limit:
            self.items.pop(next(iter(self.items)))
        self.items[key] = dict(request, expires=time.time() + self.ttl)

    def get(self, owner, origin):
        self.prune()
        return self.items.get((owner, origin))

    def pop(self, owner, origin):
        self.prune()
        return self.items.pop((owner, origin), None)
