"""Kazoo Zookeeper Client"""
import inspect
import logging
import os
import re
from collections import defaultdict, deque
from functools import partial
from os.path import split

from kazoo.exceptions import (
    AuthFailedError,
    ConnectionLoss,
    ConnectionClosedError,
    NoNodeError,
    NodeExistsError,
    ConfigurationError,
    SessionExpiredError
)
from kazoo.handlers.threading import SequentialThreadingHandler
from kazoo.hosts import collect_hosts
from kazoo.protocol.connection import ConnectionHandler
from kazoo.protocol.paths import normpath
from kazoo.protocol.paths import _prefix_root
from kazoo.protocol.serialization import (
    Auth,
    CheckVersion,
    Close,
    Create,
    Delete,
    Exists,
    GetChildren,
    GetChildren2,
    GetACL,
    SetACL,
    GetData,
    SetData,
    Sync,
    Transaction
)
from kazoo.protocol.states import KazooState
from kazoo.protocol.states import KeeperState
from kazoo.retry import KazooRetry
from kazoo.security import ACL
from kazoo.security import OPEN_ACL_UNSAFE

try:  # pragma: nocover
    basestring
except NameError:  # pragma: nocover
    basestring = str

LOST_STATES = (KeeperState.EXPIRED_SESSION, KeeperState.AUTH_FAILED,
               KeeperState.CLOSED)
ENVI_VERSION = re.compile('[\w\s:.]*=([\d\.]*).*', re.DOTALL)
log = logging.getLogger(__name__)


class KazooClient(object):
    """An Apache Zookeeper Python client supporting alternate callback
    handlers and high-level functionality.

    Watch functions registered with this class will not get session
    events, unlike the default Zookeeper watches. They will also be
    called with a single argument, a
    :class:`~kazoo.protocol.states.WatchedEvent` instance.

    """
    def __init__(self, hosts='127.0.0.1:2181',
                 timeout=10.0, client_id=None, max_retries=None,
                 retry_delay=0.1, retry_backoff=2, retry_jitter=0.8,
                 retry_max_delay=3600, handler=None, default_acl=None,
                 auth_data=None, read_only=None, randomize_hosts=True):
        """Create a :class:`KazooClient` instance. All time arguments
        are in seconds.

        :param hosts: Comma-separated list of hosts to connect to
                      (e.g. 127.0.0.1:2181,127.0.0.1:2182).
        :param timeout: The longest to wait for a Zookeeper connection.
        :param client_id: A Zookeeper client id, used when
                          re-establishing a prior session connection.
        :param max_retries: Maximum retries when using the
                            :meth:`KazooClient.retry` method.
        :param retry_delay: Initial delay when retrying a call.
        :param retry_backoff:
            Backoff multiplier between retry attempts. Defaults to 2
            for exponential back-off.
        :param retry_jitter:
            How much jitter delay to introduce per call. An amount of
            time up to this will be added per retry call to avoid
            hammering the server.
        :param retry_max_delay:
            Maximum delay in seconds, regardless of other backoff
            settings. Defaults to one hour.
        :param handler: An instance of a class implementing the
                        :class:`~kazoo.interfaces.IHandler` interface
                        for callback handling.
        :param default_acl: A default ACL used on node creation.
        :param auth_data:
            A list of authentication credentials to use for the
            connection. Should be a list of (scheme, credential)
            tuples as :meth:`add_auth` takes.
        :param read_only: Allow connections to read only servers.
        :param randomize_hosts: By default randomize host selection.

        Retry parameters will be used for connection establishment
        attempts and reconnects.

        Basic Example:

        .. code-block:: python

            zk = KazooClient()
            zk.start()
            children = zk.get_children('/')
            zk.stop()

        As a convenience all recipe classes are available as attributes
        and get automatically bound to the client. For example::

            zk = KazooClient()
            zk.start()
            lock = zk.Lock('/lock_path')

        .. versionadded:: 0.6
            The read_only option. Requires Zookeeper 3.4+

        .. versionadded:: 0.6
            The retry_max_delay option.

        .. versionadded:: 0.6
            The randomize_hosts option.

        .. versionchanged:: 0.8
            Removed the unused watcher argument (was second argument).

        """
        self.log_debug = logging.DEBUG >= log.getEffectiveLevel()

        # Record the handler strategy used
        self.handler = handler if handler else SequentialThreadingHandler()
        if inspect.isclass(self.handler):
            raise ConfigurationError("Handler must be an instance of a class, "
                                     "not the class: %s" % self.handler)

        self.auth_data = auth_data if auth_data else set([])
        self.default_acl = default_acl
        self.randomize_hosts = randomize_hosts
        self.hosts, chroot = collect_hosts(hosts, randomize_hosts)
        if chroot:
            self.chroot = normpath(chroot)
        else:
            self.chroot = ''

        # Curator like simplified state tracking, and listeners for
        # state transitions
        self._state = KeeperState.CLOSED
        self.state = KazooState.LOST
        self.state_listeners = set()

        self._reset()
        self.read_only = read_only

        if client_id:
            self._session_id = client_id[0]
            self._session_passwd = client_id[1]
        else:
            self._reset_session()

        # ZK uses milliseconds
        self._session_timeout = int(timeout * 1000)

        # We use events like twitter's client to track current and
        # desired state (connected, and whether to shutdown)
        self._live = self.handler.event_object()
        self._writer_stopped = self.handler.event_object()
        self._stopped = self.handler.event_object()
        self._stopped.set()
        self._writer_stopped.set()

        self.retry = KazooRetry(
            max_tries=max_retries,
            delay=retry_delay,
            backoff=retry_backoff,
            max_jitter=retry_jitter,
            max_delay=retry_max_delay,
            sleep_func=self.handler.sleep_func
        )
        self.retry_sleeper = self.retry.retry_sleeper.copy()

        self._connection = ConnectionHandler(
            self, self.retry.retry_sleeper.copy(), log_debug=self.log_debug)

        # convenience API
        from kazoo.recipe.barrier import Barrier
        from kazoo.recipe.barrier import DoubleBarrier
        from kazoo.recipe.counter import Counter
        from kazoo.recipe.election import Election
        from kazoo.recipe.lock import Lock
        from kazoo.recipe.lock import Semaphore
        from kazoo.recipe.partitioner import SetPartitioner
        from kazoo.recipe.party import Party
        from kazoo.recipe.party import ShallowParty
        from kazoo.recipe.queue import Queue
        from kazoo.recipe.watchers import ChildrenWatch
        from kazoo.recipe.watchers import DataWatch

        self.Barrier = partial(Barrier, self)
        self.Counter = partial(Counter, self)
        self.DoubleBarrier = partial(DoubleBarrier, self)
        self.ChildrenWatch = partial(ChildrenWatch, self)
        self.DataWatch = partial(DataWatch, self)
        self.Election = partial(Election, self)
        self.Lock = partial(Lock, self)
        self.Party = partial(Party, self)
        self.Queue = partial(Queue, self)
        self.SetPartitioner = partial(SetPartitioner, self)
        self.Semaphore = partial(Semaphore, self)
        self.ShallowParty = partial(ShallowParty, self)

    def _reset(self):
        """Resets a variety of client states for a new connection."""
        self._queue = deque()
        self._pending = deque()
        self._child_watchers = defaultdict(list)
        self._data_watchers = defaultdict(list)

        self._reset_session()
        self.last_zxid = 0

    def _reset_session(self):
        self._session_id = None
        self._session_passwd = b'\x00' * 16

    @property
    def client_state(self):
        """Returns the last Zookeeper client state

        This is the non-simplified state information and is generally
        not as useful as the simplified KazooState information.

        """
        return self._state

    @property
    def client_id(self):
        """Returns the client id for this Zookeeper session if
        connected.

        :returns: client id which consists of the session id and
                  password.
        :rtype: tuple
        """
        if self._live.is_set():
            return (self._session_id, self._session_passwd)
        return None

    @property
    def connected(self):
        """Returns whether the Zookeeper connection has been
        established."""
        return self._live.is_set()

    def add_listener(self, listener):
        """Add a function to be called for connection state changes.

        This function will be called with a
        :class:`~kazoo.protocol.states.KazooState` instance indicating
        the new connection state on state transitions.

        .. warning::

            This function must not block. If its at all likely that it
            might need data or a value that could result in blocking
            than the :meth:`~kazoo.interfaces.IHandler.spawn` method
            should be used so that the listener can return immediately.

        """
        if not (listener and callable(listener)):
            raise ConfigurationError("listener must be callable")
        self.state_listeners.add(listener)

    def remove_listener(self, listener):
        """Remove a listener function"""
        self.state_listeners.discard(listener)

    def _make_state_change(self, state):
        # skip if state is current
        if self.state == state:
            return

        self.state = state

        # Create copy of listeners for iteration in case one needs to
        # remove itself
        for listener in list(self.state_listeners):
            try:
                remove = listener(state)
                if remove is True:
                    self.remove_listener(listener)
            except Exception:
                log.exception("Error in connection state listener")

    def _session_callback(self, state):
        if state == self._state:
            return

        # Note that we don't check self.state == LOST since thats also
        # the clients initial state
        dead_state = self._state in LOST_STATES
        self._state = state

        # If we were previously closed or had an expired session, and
        # are now connecting, don't bother with the rest of the
        # transitions since they only apply after
        # we've established a connection
        if dead_state and state == KeeperState.CONNECTING:
            log.info("Skipping state change")
            return

        if state in (KeeperState.CONNECTED, KeeperState.CONNECTED_RO):
            log.info("Zookeeper connection established, state: %s", state)
            self._live.set()
            self._make_state_change(KazooState.CONNECTED)
        elif state in LOST_STATES:
            log.info("Zookeeper session lost, state: %s", state)
            self._live.clear()
            self._make_state_change(KazooState.LOST)
            self._notify_pending(state)
            self._reset()
        else:
            log.info("Zookeeper connection lost")
            # Connection lost
            self._live.clear()
            self._notify_pending(state)
            self._make_state_change(KazooState.SUSPENDED)

    def _notify_pending(self, state):
        """Used to clear a pending response queue and request queue
        during connection drops."""
        if state == KeeperState.AUTH_FAILED:
            exc = AuthFailedError()
        elif state == KeeperState.EXPIRED_SESSION:
            exc = SessionExpiredError()
        else:
            exc = ConnectionLoss()

        while True:
            try:
                request, async_object, xid = self._pending.popleft()
                if async_object:
                    async_object.set_exception(exc)
            except IndexError:
                break

        while True:
            try:
                request, async_object = self._queue.popleft()
                if async_object:
                    async_object.set_exception(exc)
            except IndexError:
                break

    def _safe_close(self):
        self.handler.stop()
        if not self._connection.stop(10):
            raise Exception("Writer still open from prior connection"
                            " and wouldn't close after 10 seconds")

    def _call(self, request, async_object):
        """Ensure there's an active connection and put the request in
        the queue if there is."""
        if self._state == KeeperState.AUTH_FAILED:
            raise AuthFailedError()
        elif self._state == KeeperState.CLOSED:
            raise ConnectionClosedError("Connection has been closed")
        elif self._state in (KeeperState.EXPIRED_SESSION,
                             KeeperState.CONNECTING):
            raise SessionExpiredError()
        self._queue.append((request, async_object))
        os.write(self._connection._write_pipe, b'\0')

    def start(self, timeout=15):
        """Initiate connection to ZK.

        :param timeout: Time in seconds to wait for connection to
                        succeed.
        :raises: :attr:`~kazoo.interfaces.IHandler.timeout_exception`
                 if the connection wasn't established within `timeout`
                 seconds.

        """
        event = self.start_async()
        event.wait(timeout=timeout)
        if not self.connected:
            # We time-out, ensure we are disconnected
            self.stop()
            raise self.handler.timeout_exception("Connection time-out")

    def start_async(self):
        """Asynchronously initiate connection to ZK.

        :returns: An event object that can be checked to see if the
                  connection is alive.
        :rtype: :class:`~threading.Event` compatible object.

        """
        # If we're already connected, ignore
        if self._live.is_set():
            return self._live

        # Make sure we're safely closed
        self._safe_close()

        # We've been asked to connect, clear the stop and our writer
        # thread indicator
        self._stopped.clear()
        self._writer_stopped.clear()

        # Start the handler
        self.handler.start()

        # Start the connection
        self._connection.start()
        return self._live

    def stop(self):
        """Gracefully stop this Zookeeper session.

        This method can be called while a reconnection attempt is in
        progress, which will then be halted.

        Once the connection is closed, its session becomes invalid. All
        the ephemeral nodes in the ZooKeeper server associated with the
        session will be removed. The watches left on those nodes (and
        on their parents) will be triggered.

        """
        if self._stopped.is_set():
            return

        self._stopped.set()
        self._queue.append((Close(), None))
        os.write(self._connection._write_pipe, b'\0')
        self._safe_close()

    def restart(self):
        """Stop and restart the Zookeeper session."""
        self.stop()
        self.start()

    def command(self, cmd=b'ruok'):
        """Sent a management command to the current ZK server.

        Examples are `ruok`, `envi` or `stat`.

        :returns: An unstructured textual response.
        :rtype: str

        :raises:
            :exc:`ConnectionLoss` if there is no connection open, or
            possibly a :exc:`socket.error` if there's a problem with
            the connection used just for this command.

        .. versionadded:: 0.5

        """
        if not self._live.is_set():
            raise ConnectionLoss("No connection to server")

        sock = self.handler.socket()
        sock.settimeout(self._session_timeout)
        peer = self._connection._socket.getpeername()
        sock.connect(peer)
        sock.sendall(cmd)
        result = sock.recv(8192)
        sock.close()
        return result.decode('utf-8', 'replace')

    def server_version(self):
        """Get the version of the currently connected ZK server.

        :returns: The server version, for example (3, 4, 3).
        :rtype: tuple

        .. versionadded:: 0.5

        """
        data = self.command(b'envi')
        string = ENVI_VERSION.match(data).group(1)
        return tuple([int(i) for i in string.split('.')])

    def add_auth(self, scheme, credential):
        """Send credentials to server.

        :param scheme: authentication scheme (default supported:
                       "digest").
        :param credential: the credential -- value depends on scheme.
        """
        return self.add_auth_async(scheme, credential)

    def add_auth_async(self, scheme, credential):
        """Asynchronously send credentials to server. Takes the same
        arguments as :meth:`add_auth`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(scheme, basestring):
            raise TypeError("Invalid type for scheme")
        if not isinstance(credential, basestring):
            raise TypeError("Invalid type for credential")
        self._call(Auth(0, scheme, credential), None)
        return True

    def unchroot(self, path):
        """Strip the chroot if applicable from the path."""
        if not self.chroot:
            return path

        if path.startswith(self.chroot):
            return path[len(self.chroot):]
        else:
            return path

    def sync_async(self, path):
        """Asynchronous sync.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self.handler.async_result()
        self._call(Sync(_prefix_root(self.chroot, path)), async_result)
        return async_result

    def sync(self, path):
        """Sync, blocks until response is acknowledged.

        Flushes channel between process and leader.

        :param path: path of node.
        :returns: The node path that was synced.
        :raises:
            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        .. versionadded:: 0.5

        """
        return self.sync_async(path).get()

    def create(self, path, value=b"", acl=None, ephemeral=False,
               sequence=False, makepath=False):
        """Create a node with the given value as its data. Optionally
        set an ACL on the node.

        The ephemeral and sequence arguments determine the type of the
        node.

        An ephemeral node will be automatically removed by ZooKeeper
        when the session associated with the creation of the node
        expires.

        A sequential node will be given the specified path plus a
        suffix `i` where i is the current sequential number of the
        node. The sequence number is always fixed length of 10 digits,
        0 padded. Once such a node is created, the sequential number
        will be incremented by one.

        If a node with the same actual path already exists in
        ZooKeeper, a NodeExistsError will be raised. Note that since a
        different actual path is used for each invocation of creating
        sequential nodes with the same path argument, the call will
        never raise NodeExistsError.

        If the parent node does not exist in ZooKeeper, a NoNodeError
        will be raised. Setting the optional `makepath` argument to
        `True` will create all missing parent nodes instead.

        An ephemeral node cannot have children. If the parent node of
        the given path is ephemeral, a NoChildrenForEphemeralsError
        will be raised.

        This operation, if successful, will trigger all the watches
        left on the node of the given path by :meth:`exists` and
        :meth:`get` API calls, and the watches left on the parent node
        by :meth:`get_children` API calls.

        The maximum allowable size of the node value is 1 MB. Values
        larger than this will cause a ZookeeperError to be raised.

        :param path: Path of node.
        :param value: Initial bytes value of node.
        :param acl: :class:`~kazoo.security.ACL` list.
        :param ephemeral: Boolean indicating whether node is ephemeral
                          (tied to this session).
        :param sequence: Boolean indicating whether path is suffixed
                         with a unique index.
        :param makepath: Whether the path should be created if it
                         doesn't exist.
        :returns: Real path of the new node.
        :rtype: str

        :raises:
            :exc:`~kazoo.exceptions.NodeExistsError` if the node
            already exists.

            :exc:`~kazoo.exceptions.NoNodeError` if parent nodes are
            missing.

            :exc:`~kazoo.exceptions.NoChildrenForEphemeralsError` if
            the parent node is an ephemeral node.

            :exc:`~kazoo.exceptions.ZookeeperError` if the provided
            value is too large.

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        """
        try:
            realpath = self.create_async(path, value, acl=acl,
                ephemeral=ephemeral, sequence=sequence).get()

        except NoNodeError:
            # some or all of the parent path doesn't exist. if makepath is set
            # we will create it and retry. If it fails again, someone must be
            # actively deleting ZNodes and we'd best bail out.
            if not makepath:
                raise

            parent, _ = split(path)

            # using the inner call directly because path is already namespaced
            self._inner_ensure_path(parent, acl)

            # now retry
            realpath = self.create_async(path, value, acl=acl,
                ephemeral=ephemeral, sequence=sequence).get()

        return self.unchroot(realpath)

    def create_async(self, path, value=b"", acl=None, ephemeral=False,
                     sequence=False):
        """Asynchronously create a ZNode. Takes the same arguments as
        :meth:`create`, with the exception of `makepath`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if acl is None and self.default_acl:
            acl = self.default_acl

        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if acl and (isinstance(acl, ACL) or
                    not isinstance(acl, (tuple, list))):
            raise TypeError("acl must be a tuple/list of ACL's")
        if not isinstance(value, bytes):
            raise TypeError("value must be a byte string")
        if not isinstance(ephemeral, bool):
            raise TypeError("ephemeral must be a bool")
        if not isinstance(sequence, bool):
            raise TypeError("sequence must be a bool")

        flags = 0
        if ephemeral:
            flags |= 1
        if sequence:
            flags |= 2
        if acl is None:
            acl = OPEN_ACL_UNSAFE

        async_result = self.handler.async_result()
        self._call(Create(_prefix_root(self.chroot, path), value, acl, flags),
                   async_result)
        return async_result

    def ensure_path(self, path, acl=None):
        """Recursively create a path if it doesn't exist.

        :param path: Path of node.
        :param acl: Permissions for node.

        """
        self._inner_ensure_path(path, acl)

    def _inner_ensure_path(self, path, acl):
        if self.exists(path):
            return

        if acl is None and self.default_acl:
            acl = self.default_acl

        parent, node = split(path)

        if node:
            self._inner_ensure_path(parent, acl)
        try:
            self.create_async(path, acl=acl).get()
        except NodeExistsError:
            # someone else created the node. how sweet!
            pass

    def exists(self, path, watch=None):
        """Check if a node exists.

        If a watch is provided, it will be left on the node with the
        given path. The watch will be triggered by a successful
        operation that creates/deletes the node or sets the data on the
        node.

        :param path: Path of node.
        :param watch: Optional watch callback to set for future changes
                      to this path.
        :returns: ZnodeStat of the node if it exists, else None if the
                  node does not exist.
        :rtype: :class:`~kazoo.protocol.states.ZnodeStat` or `None`.

        :raises:
            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        """
        return self.exists_async(path, watch).get()

    def exists_async(self, path, watch=None):
        """Asynchronously check if a node exists. Takes the same
        arguments as :meth:`exists`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if watch and not callable(watch):
            raise TypeError("watch must be a callable")

        async_result = self.handler.async_result()
        self._call(Exists(_prefix_root(self.chroot, path), watch),
                   async_result)
        return async_result

    def get(self, path, watch=None):
        """Get the value of a node.

        If a watch is provided, it will be left on the node with the
        given path. The watch will be triggered by a successful
        operation that sets data on the node, or deletes the node.

        :param path: Path of node.
        :param watch: Optional watch callback to set for future changes
                      to this path.
        :returns:
            Tuple (value, :class:`~kazoo.protocol.states.ZnodeStat`) of
            node.
        :rtype: tuple

        :raises:
            :exc:`~kazoo.exceptions.NoNodeError` if the node doesn't
            exist

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code

        """
        return self.get_async(path, watch).get()

    def get_async(self, path, watch=None):
        """Asynchronously get the value of a node. Takes the same
        arguments as :meth:`get`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if watch and not callable(watch):
            raise TypeError("watch must be a callable")

        async_result = self.handler.async_result()
        self._call(GetData(_prefix_root(self.chroot, path), watch),
                   async_result)
        return async_result

    def get_children(self, path, watch=None, include_data=False):
        """Get a list of child nodes of a path.

        If a watch is provided it will be left on the node with the
        given path. The watch will be triggered by a successful
        operation that deletes the node of the given path or
        creates/deletes a child under the node.

        The list of children returned is not sorted and no guarantee is
        provided as to its natural or lexical order.

        :param path: Path of node to list.
        :param watch: Optional watch callback to set for future changes
                      to this path.
        :param include_data:
            Include the :class:`~kazoo.protocol.states.ZnodeStat` of
            the node in addition to the children. This option changes
            the return value to be a tuple of (children, stat).

        :returns: List of child node names, or tuple if `include_data`
                  is `True`.
        :rtype: list

        :raises:
            :exc:`~kazoo.exceptions.NoNodeError` if the node doesn't
            exist.

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        .. versionadded:: 0.5
            The `include_data` option.

        """
        return self.get_children_async(path, watch, include_data).get()

    def get_children_async(self, path, watch=None, include_data=False):
        """Asynchronously get a list of child nodes of a path. Takes
        the same arguments as :meth:`get_children`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if watch and not callable(watch):
            raise TypeError("watch must be a callable")
        if not isinstance(include_data, bool):
            raise TypeError("include_data must be a bool")

        async_result = self.handler.async_result()
        if include_data:
            req = GetChildren2(_prefix_root(self.chroot, path), watch)
        else:
            req = GetChildren(_prefix_root(self.chroot, path), watch)
        self._call(req, async_result)
        return async_result

    def get_acls(self, path):
        """Return the ACL and stat of the node of the given path.

        :param path: Path of the node.
        :returns: The ACL array of the given node and its
            :class:`~kazoo.protocol.states.ZnodeStat`.
        :rtype: tuple of (:class:`~kazoo.security.ACL` list,
                :class:`~kazoo.protocol.states.ZnodeStat`)
        :raises:
            :exc:`~kazoo.exceptions.NoNodeError` if the node doesn't
            exist.

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code

        .. versionadded:: 0.5

        """
        return self.get_acls_async(path).get()

    def get_acls_async(self, path):
        """Return the ACL and stat of the node of the given path. Takes
        the same arguments as :meth:`get_acls`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")

        async_result = self.handler.async_result()
        self._call(GetACL(_prefix_root(self.chroot, path)), async_result)
        return async_result

    def set_acls(self, path, acls, version=-1):
        """Set the ACL for the node of the given path.

        Set the ACL for the node of the given path if such a node
        exists and the given version matches the version of the node.

        :param path: Path for the node.
        :param acls: List of :class:`~kazoo.security.ACL` objects to
                     set.
        :param version: The expected node version that must match.
        :returns: The stat of the node.
        :raises:
            :exc:`~kazoo.exceptions.BadVersionError` if version doesn't
            match.

            :exc:`~kazoo.exceptions.NoNodeError` if the node doesn't
            exist.

            :exc:`~kazoo.exceptions.InvalidACLError` if the ACL is
            invalid.

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        .. versionadded:: 0.5

        """
        return self.set_acls_async(path, acls, version).get()

    def set_acls_async(self, path, acls, version=-1):
        """Set the ACL for the node of the given path. Takes the same
        arguments as :meth:`set_acls`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if isinstance(acls, ACL) or not isinstance(acls, (tuple, list)):
            raise TypeError("acl must be a tuple/list of ACL's")
        if not isinstance(version, int):
            raise TypeError("version must be an int")

        async_result = self.handler.async_result()
        self._call(SetACL(_prefix_root(self.chroot, path), acls, version),
                   async_result)
        return async_result

    def set(self, path, value, version=-1):
        """Set the value of a node.

        If the version of the node being updated is newer than the
        supplied version (and the supplied version is not -1), a
        BadVersionError will be raised.

        This operation, if successful, will trigger all the watches on
        the node of the given path left by :meth:`get` API calls.

        The maximum allowable size of the value is 1 MB. Values larger
        than this will cause a ZookeeperError to be raised.

        :param path: Path of node.
        :param value: New data value.
        :param version: Version of node being updated, or -1.
        :returns: Updated :class:`~kazoo.protocol.states.ZnodeStat` of
                  the node.

        :raises:
            :exc:`~kazoo.exceptions.BadVersionError` if version doesn't
            match.

            :exc:`~kazoo.exceptions.NoNodeError` if the node doesn't
            exist.

            :exc:`~kazoo.exceptions.ZookeeperError` if the provided
            value is too large.

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        """
        return self.set_async(path, value, version).get()

    def set_async(self, path, value, version=-1):
        """Set the value of a node. Takes the same arguments as
        :meth:`set`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if not isinstance(value, bytes):
            raise TypeError("value must be a byte string")
        if not isinstance(version, int):
            raise TypeError("version must be an int")

        async_result = self.handler.async_result()
        self._call(SetData(_prefix_root(self.chroot, path), value, version),
                   async_result)
        return async_result

    def transaction(self):
        """Create and return a :class:`TransactionRequest` object

        Creates a :class:`TransactionRequest` object. A Transaction can
        consist of multiple operations which can be committed as a
        single atomic unit. Either all of the operations will succeed
        or none of them.

        :returns: A TransactionRequest.
        :rtype: :class:`TransactionRequest`

        .. versionadded:: 0.6
            Requires Zookeeper 3.4+

        """
        return TransactionRequest(self)

    def delete(self, path, version=-1, recursive=False):
        """Delete a node.

        The call will succeed if such a node exists, and the given
        version matches the node's version (if the given version is -1,
        the default, it matches any node's versions).

        This operation, if successful, will trigger all the watches on
        the node of the given path left by `exists` API calls, and the
        watches on the parent node left by `get_children` API calls.

        :param path: Path of node to delete.
        :param version: Version of node to delete, or -1 for any.
        :param recursive: Recursively delete node and all its children,
                          defaults to False.
        :type recursive: bool

        :raises:
            :exc:`~kazoo.exceptions.BadVersionError` if version doesn't
            match.

            :exc:`~kazoo.exceptions.NoNodeError` if the node doesn't
            exist.

            :exc:`~kazoo.exceptions.NotEmptyError` if the node has
            children.

            :exc:`~kazoo.exceptions.ZookeeperError` if the server
            returns a non-zero error code.

        """
        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a bool")

        if recursive:
            return self._delete_recursive(path)
        else:
            return self.delete_async(path, version).get()

    def delete_async(self, path, version=-1):
        """Asynchronously delete a node. Takes the same arguments as
        :meth:`delete`, with the exception of `recursive`.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if not isinstance(version, int):
            raise TypeError("version must be an int")
        async_result = self.handler.async_result()
        self._call(Delete(_prefix_root(self.chroot, path), version),
                   async_result)
        return async_result

    def _delete_recursive(self, path):
        try:
            children = self.get_children(path)
        except NoNodeError:
            return True

        if children:
            for child in children:
                if path == "/":
                    child_path = path + child
                else:
                    child_path = path + "/" + child

                self._delete_recursive(child_path)
        try:
            self.delete(path)
        except NoNodeError:  # pragma: nocover
            pass


class TransactionRequest(object):
    """A Zookeeper Transaction Request

    A Transaction provides a builder object that can be used to
    construct and commit an atomic set of operations. The transaction
    must be committed before its sent.

    Transactions are not thread-safe and should not be accessed from
    multiple threads at once.

    .. versionadded:: 0.6
        Requires Zookeeper 3.4+

    """
    def __init__(self, client):
        self.client = client
        self.operations = []
        self.committed = False

    def create(self, path, value=b"", acl=None, ephemeral=False,
               sequence=False):
        """Add a create ZNode to the transaction. Takes the same
        arguments as :meth:`KazooClient.create`, with the exception
        of `makepath`.

        :returns: None

        """
        if acl is None and self.client.default_acl:
            acl = self.client.default_acl

        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if acl and not isinstance(acl, (tuple, list)):
            raise TypeError("acl must be a tuple/list of ACL's")
        if not isinstance(value, bytes):
            raise TypeError("value must be a byte string")
        if not isinstance(ephemeral, bool):
            raise TypeError("ephemeral must be a bool")
        if not isinstance(sequence, bool):
            raise TypeError("sequence must be a bool")

        flags = 0
        if ephemeral:
            flags |= 1
        if sequence:
            flags |= 2
        if acl is None:
            acl = OPEN_ACL_UNSAFE

        self._add(Create(_prefix_root(self.client.chroot, path), value, acl,
                         flags), None)

    def delete(self, path, version=-1):
        """Add a delete ZNode to the transaction. Takes the same
        arguments as :meth:`KazooClient.delete`, with the exception of
        `recursive`.

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if not isinstance(version, int):
            raise TypeError("version must be an int")
        self._add(Delete(_prefix_root(self.client.chroot, path), version))

    def set_data(self, path, value, version=-1):
        """Add a set ZNode value to the transaction. Takes the same
        arguments as :meth:`KazooClient.set`.

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if not isinstance(value, bytes):
            raise TypeError("value must be a byte string")
        if not isinstance(version, int):
            raise TypeError("version must be an int")
        self._add(SetData(_prefix_root(self.client.chroot, path), value,
                  version))

    def check(self, path, version):
        """Add a Check Version to the transaction.

        This command will fail and abort a transaction if the path
        does not match the specified version.

        """
        if not isinstance(path, basestring):
            raise TypeError("path must be a string")
        if not isinstance(version, int):
            raise TypeError("version must be an int")
        self._add(CheckVersion(_prefix_root(self.client.chroot, path),
                  version))

    def commit_async(self):
        """Commit the transaction asynchronously.

        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        self._check_tx_state()
        self.committed = True
        async_object = self.client.handler.async_result()
        self.client._call(Transaction(self.operations), async_object)
        return async_object

    def commit(self):
        """Commit the transaction.

        :returns: A list of the results for each operation in the
                  transaction.

        """
        return self.commit_async().get()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """Commit and cleanup accumulated transaction data."""
        if not exc_type:
            self.commit()

    def _check_tx_state(self):
        if self.committed:
            raise ValueError('Transaction already committed')

    def _add(self, request, post_processor=None):
        self._check_tx_state()
        if self.client.log_debug:
            log.debug('Added %r to %r', request, self)
        self.operations.append(request)
