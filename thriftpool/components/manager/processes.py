"""Manage process pool."""
from __future__ import absolute_import

import logging
import sys
import os

from gaffer.process import ProcessConfig
from pyuv import Pipe
from six import iteritems

from thriftworker.utils.loop import in_loop
from thriftworker.utils.decorators import cached_property
from thriftworker.utils.mixin import LoopMixin

from thriftpool.exceptions import SystemTerminate
from thriftpool.components.base import StartStopComponent
from thriftpool.rpc.broker import Broker
from thriftpool.utils.serializers import StreamSerializer

logger = logging.getLogger(__name__)


class RedirectStream(object):
    """Try to write to stream asynchronous."""

    def __init__(self, loop, stream):
        self.fd = None
        self.stream = stream
        try:
            fd = self.fd = stream.fileno()
        except AttributeError:
            self.channel = None
        else:
            channel = self.channel = Pipe(loop)
            setattr(channel, 'bypass', True)
            channel.open(fd)

    def write(self, data):
        """Write data in asynchronous way."""
        if self.channel is not None and not self.channel.closed:
            self.channel.write(data)
        else:
            self.stream.write(data)


class ManagerMixin(object):

    app = None

    @property
    def manager(self):
        """Shortcut to gaffer manager."""
        return self.app.gaffer_manager


class ProcessFactory(ManagerMixin, LoopMixin):
    """Encapsulate process creation logic."""

    #: Specify which serializer should we use.
    Serializer = StreamSerializer

    #: Name of session to use.
    session_name = 'thriftpool'

    #: Default process name.
    job_name = 'worker'

    #: specify worker initialization string
    initialize_script = 'from thriftpool.bin.thriftworker import main; main();'

    #: custom string for gevent monkey patching
    gevent_monkey_script = 'from gevent.monkey import patch_all; patch_all();'

    def __init__(self, app, broker, setup_callback=None, teardown_callback=None):
        self.app = app
        self.broker = broker
        self.serializers = self.Serializer()
        self.setup_callback = setup_callback
        self.teardown_callback = teardown_callback
        super(ProcessFactory, self).__init__()

    @property
    def command(self):
        """Python command to start worker."""
        worker_type = self.app.config.WORKER_TYPE
        if worker_type == 'gevent':
            return '{0} {1}'.format(self.gevent_monkey_script,
                                    self.initialize_script)
        elif worker_type == 'sync':
            return self.initialize_script
        else:
            raise NotImplementedError('unknown worker type {0!r}'.format(worker_type))

    @property
    def process_name(self):
        return '{0}.{1}'.format(self.session_name, self.job_name)

    def create_config(self, channels):
        """Create worker's process configuration."""
        config = self.app.config
        return ProcessConfig(
            name=self.job_name,
            cmd=sys.executable,
            args=['-c', '{0}'.format(self.command)],
            env=dict(os.environ, IS_WORKER='1'),
            numprocesses=config.WORKERS,
            redirect_input=True,
            redirect_output=['out', 'err'],
            custom_streams=['handshake', 'incoming', 'outgoing'],
            custom_channels=channels,
            graceful_timeout=config.PROCESS_STOP_TIMEOUT,
        )

    def _handle_exit(self, pid, term_signal, exit_status, **kwargs):
        """Handle exit event here."""
        if exit_status != 0 or term_signal not in (0, 15):
            # We have a problem in this case, notify users!
            logger.critical(
                'Worker %d exited with term signal %d and exit status %d.',
                pid, term_signal, exit_status)
        else:
            logger.info('Worker %d exited normally.', pid)
        self.broker.unregister(pid)
        if self.teardown_callback is not None:
            self.teardown_callback(pid)

    @cached_property
    def _stdout(self):
        """Create wrapper around stdout to support async write."""
        return RedirectStream(self.loop, sys.stdout)

    @cached_property
    def _stderr(self):
        """Create wrapper around stderr to support async write."""
        return RedirectStream(self.loop, sys.stderr)

    def _setup_io_redirect(self, process):
        """Setup redirection for stdout & stderr."""
        def inner_on_io(evtype, msg):
            data = msg['data']
            if evtype == 'err':
                self._stderr.write(data)
            else:
                self._stdout.write(data)
        process.monitor_io('.', inner_on_io)

    def _do_handshake(self, process):
        """Transfer main application to worker."""
        # Pass application to created process.
        stream = process.streams['handshake']
        stream.write(self.serializers.encode_with_length(self.app))

        def handshake_done(*args):
            stream.unsubscribe(handshake_done)
            # Process exited and we will do same.
            if not process.active:
                return
            self.broker.register(process, callback=self.setup_callback)

        # Wait for worker answer.
        stream.subscribe(handshake_done)

    def _handle_spawn(self, pid, os_pid, **kwargs):
        """Handle spawn event here."""
        logger.info('Worker %d spawned with pid %d.', pid, os_pid)
        process = self.manager.get_process(pid)
        self._setup_io_redirect(process)
        self._do_handshake(process)

    def _on_event(self, evtype, msg):
        """Handle process events."""
        if evtype == 'exit':
            self._handle_exit(**msg)
        elif evtype == 'spawn':
            self._handle_spawn(**msg)

    def setup(self, channels):
        """Setup bootstapper. Add processes to manager."""
        manager = self.manager
        manager.load(self.create_config(channels),
                     sessionid=self.session_name,
                     start=False)
        manager \
            .subscribe('JOB:{0}'.format(self.process_name)) \
            .bind_all(self._on_event)
        manager.start_job(self.process_name)

    def teardown(self):
        """Teardown bootstrapper. Remove processes from manager."""
        self.manager.unload(self.job_name, sessionid=self.session_name)


class Aborted(Exception):
    """Waiting was aborted."""


class Waiter(object):
    """Waiter primitive."""

    def __init__(self, app, timeout=None):
        self.app = app
        self.timeout = timeout or 30
        self._aborted = False
        super(Waiter, self).__init__()

    @cached_property
    def _event(self):
        return self.app.env.RealEvent()

    def reset(self):
        """Reset waiter state."""
        self._aborted = False
        self._event.clear()

    def abort(self):
        """Abort initialization."""
        self._aborted = True
        self._event.set()

    def done(self):
        """Notify all that initialization done."""
        self._event.set()

    def wait(self):
        """Wait for initialization."""
        event = self._event
        try:
            event.wait(self.timeout)
            if self._aborted:
                raise Aborted('Waiter was aborted!')
            return event.is_set()
        finally:
            self.reset()

    def wait_or_terminate(self, msg=None):
        """Generate `SystemTerminate` in case of timeout or aborting."""
        try:
            if not self.wait():
                logger.error(msg or 'Timeout in waiter happened.')
                raise SystemTerminate()
        except Aborted:
            logger.info('Waiter aborted.')
            raise SystemTerminate()


class ProcessManager(ManagerMixin, LoopMixin):
    """Start and manage workers."""

    #: Which class should be used to bootstrap process.
    Factory = ProcessFactory

    #: Which class should be used to pass commands to processes.
    Broker = Broker

    #: How worker process should be named.
    name_template = '[thriftworker-{0}] -c {1.CONCURRENCY} -k {1.WORKER_TYPE}'

    def __init__(self, app, listeners, controller):
        self.app = app
        self.listeners = listeners
        self.controller = controller

        broker = self.broker = self.Broker(self.app)
        self.factory = self.Factory(self.app, broker,
            setup_callback=self.setup_cb, teardown_callback=self.teardown_cb)

        self._bootstrapped = {}
        self._aborted = False

        self._start_waiter = Waiter(self.app,
            timeout=self.app.config.PROCESS_START_TIMEOUT)
        self._stop_waiter = Waiter(self.app,
            timeout=self.app.config.PROCESS_STOP_TIMEOUT * 2)

        super(ProcessManager, self).__init__()

    def __iter__(self):
        return iter(self._bootstrapped)

    def get_start_time(self, process_id):
        """When process was registered?"""
        return self._bootstrapped.get(process_id)

    def is_ready(self):
        """Are all workers started or not?"""
        return len(self._bootstrapped) >= self.app.config.WORKERS

    def setup_cb(self, proxy, process):
        # Change name of process.
        name = self.name_template.format(process.pid, self.app.config)
        proxy.change_title(name)

        # Register acceptors in remote process.
        proxy.register_acceptors({i: listener.name
            for i, listener in iteritems(self.listeners.enumerated)})

        for listener in self.listeners:
            if listener.started:
                proxy.start_acceptor(listener.name)

        # Notify about process initialization.
        self._bootstrapped[process.pid] = self.loop.now()
        logger.info('Worker %d initialized.', process.pid)
        if self.is_ready():
            self.ready_cb()

    def teardown_cb(self, pid):
        self._bootstrapped.pop(pid)

    def ready_cb(self, *args):
        logger.info('Workers initialization done.')
        self._start_waiter.done()

    def stop_cb(self, *args):
        logger.info('Workers stopped.')
        self._stop_waiter.done()

    @in_loop
    def setup(self):
        self.app.gaffer_manager.start()
        self.factory.setup(self.listeners.channels)

    @in_loop
    def teardown(self):
        self.factory.teardown()
        self.app.gaffer_manager.stop(callback=self.stop_cb)

    def start(self):
        self.setup()
        self._start_waiter.wait_or_terminate(
            'Timeout happened when starting processes.')

    def stop(self):
        self.teardown()
        self._stop_waiter.wait_or_terminate(
            'Timeout happened when starting processes.')

    def abort(self):
        self._start_waiter.abort()
        self._stop_waiter.abort()


class ProcessManagerComponent(StartStopComponent):

    name = 'manager.processes'
    requires = ('loop', 'listeners')

    def create(self, parent):
        processes = parent.processes = \
            ProcessManager(parent.app, parent.listeners, parent)
        return processes
