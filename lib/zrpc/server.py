from __future__ import with_statement

from contextlib import closing, nested
from itertools import repeat
import sys
import traceback

import logbook
from bson import BSON, InvalidDocument
import zmq

from zrpc.concurrency import DummyCallback
from zrpc.registry import Registry


logger = logbook.Logger('zrpc.server')
run_logger = logbook.Logger('zrpc.server.run')
pm_logger = logbook.Logger('zrpc.server.process_message')


class Server(object):

    """
    A ZRPC server.

    A :class:`Server` listens on a ``zmq.REP`` socket for incoming requests,
    performs the requested methods (using a :class:`Registry`) and returns the
    result. All communication is BSON-encoded.

    Usage is pretty simple:

        >>> server = Server('tcp://127.0.0.1:7341', registry)
        >>> server.run()

    You could even start a new thread/greenlet/process, using the server's
    `run()` method as the target. This library does not enforce or encourage
    any single concurrency model.

    .. py:attribute:: addr
        The address to bind or connect to as a ZeroMQ-style address. Examples
        include ``'tcp://*:7341'`` and ``'inproc://tasks'``.

    .. py:attribute:: registry
        A :class:`Registry` object holding the method definitions for this
        server.

    .. py:attribute:: connect
        A boolean indicating whether the server should bind or connect to its
        address. Default is ``False``, so the server will bind. Set to ``True``
        if you're using a broker-model :class:`LoadBalancer`, and specify the
        output address of that load balancer as this server's `addr`.

    .. py:attribute:: context
        A ``zmq.Context`` to use when creating sockets. Can be left unspecified
        and a new context will be created.
    """

    def __init__(self, addr, registry, connect=False, context=None):
        self.context = context or zmq.Context.instance()
        self.addr = addr
        self.connect = connect
        self.registry = registry

    @staticmethod
    def capture_exception():
        """Capture the current exception as a BSON-serializable dictionary."""

        pm_logger.exception()
        exc_type, exc_value, exc_tb = sys.exc_info()
        exc_type_string = "%s.%s" % (exc_type.__module__, exc_type.__name__)
        exc_message = traceback.format_exception_only(exc_type, exc_value)[-1].strip()
        error = {"type": exc_type_string,
                 "message": exc_message}
        try:
            BSON.encode({'args': exc_value.args})
        except InvalidDocument:
            pass
        else:
            error["args"] = exc_value.args
        return error

    def get_response(self, message_id, func, *args, **kwargs):

        """
        Run a Python function, returning the result in BSON-serializable form.

        The behaviour of this function is to capture either a successful return
        value or exception in a BSON-serialized form (a dictionary with `id`,
        `result` and `error` keys).
        """

        result, error = None, None
        try:
            result = func(*args, **kwargs)
        except Exception, exc:
            error = self.capture_exception()

        response = {'result': result, 'error': error}
        if message_id is not None:
            response['id'] = message_id

        try:
            return BSON.encode(response)
        except InvalidDocument, exc:
            response['error'] = self.capture_exception()
            response['result'] = None
            return BSON.encode(response)


    def process_message(self, message):

        """
        Process a single message.

        At the moment this just does some logging and dispatches to
        :meth:`get_response`, using the :attr:`registry`. You can override this
        in a subclass to customize the way messages are interpreted or methods
        are called.
        """

        if 'id' in message:
            logger.debug("Processing message {0}: {1!r}",
                         message['id'], message['method'])
        else:
            logger.debug("Processing method {0!r}", message['method'])

        response = self.get_response(message.get('id', None),
                                     self.registry,
                                     message['method'],
                                     *message['params'])
        return response

    def run(self, die_after=None, callback=DummyCallback()):

        """
        Run the worker, optionally dying after a number of requests.

        :param int die_after:
            Die after processing a set number of messages (default: continue
            forever).

        :param callback:
            A :class:`~zrpc.concurrency.Callback` which will be called with the
            socket once it has been successfully connected or bound.
        """

        with callback.catch_exceptions():
            socket = self.context.socket(zmq.REP)
            if self.connect:
                run_logger.debug("Replying to requests from {0!r}", self.addr)
                socket.connect(self.addr)
            else:
                run_logger.debug("Listening for requests on {0!r}", self.addr)
                socket.bind(self.addr)
        callback.send(socket)

        iterator = die_after and repeat(None, die_after) or repeat(None)
        with nested(run_logger.catch_exceptions(), closing(socket)):
            try:
                for _ in iterator:
                    message = BSON(socket.recv()).decode()
                    socket.send(self.process_message(message))
            except zmq.ZMQError, exc:
                if exc.errno == zmq.ETERM:
                    run_logger.info("Context was terminated, shutting down")
                else:
                    raise
            except KeyboardInterrupt:
                run_logger.info("SIGINT received, shutting down")
