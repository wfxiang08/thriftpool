"""Request handlers that provide information about workers."""
from __future__ import absolute_import

import json

from .base import BaseHandler


class ClientsHandler(BaseHandler):

    def get(self):
        self.preflight()
        self.set_status(200)
        self.write(json.dumps(self.processes.broker.keys()))


class SpecificClientHandler(BaseHandler):
    """Abstract client handler."""

    def get_data(self, proxy):
        raise NotImplementedError('subclass responsibility')

    def get(self, *args):
        self.preflight()

        try:
            pid = int(args[0])
        except ValueError:
            self.set_status(400)
            self.write({"error": "bad_value"})
            return

        if pid in self.processes.broker:
            self.set_status(200)
        else:
            self.set_status(404)
            return

        client = self.processes.broker[pid]
        data = client.spawn(self.get_data).get()
        self.write(json.dumps(data))


class CounterHandler(SpecificClientHandler):
    """Provide information about counters."""

    def get_data(self, proxy):
        return {'{0}.{1}'.format(service, method): value
                for (service, method), value in proxy.get_counters().items()}


class TimerHandler(SpecificClientHandler):
    """Provide information about timers."""

    def get_data(self, proxy):
        return {'{0}.{1}'.format(service, method): value
                for (service, method), value in proxy.get_timers().items()}


class StackHandler(SpecificClientHandler):
    """Provide information about currently running tasks."""

    def get_data(self, proxy):
        return {ident: [{'method': '{0}.{1}'.format(service, method),
                         'args': repr(args), 'kwargs': repr(kwargs)}
                        for (service, method, args, kwargs) in l]
                for ident, l in proxy.get_stack().items()}
