# -*- coding: utf-8 -*-
from aiohttp import web
from aiohttp.web import RequestHandler
from aiohttp_traversal.abc import AbstractResource
from aiohttp_traversal.ext.views import View
from aiohttp_traversal import TraversalRouter
from aiohttp_traversal.traversal import Traverser
from BTrees.Length import Length
from BTrees._OOBTree import OOBTree
from concurrent.futures import ThreadPoolExecutor
from transaction.interfaces import ISavepointDataManager
from zope.interface import implementer
import threading
import asyncio
import inspect
import logging
import transaction
import ZODB
import ZODB.Connection

logger = logging.getLogger('sandbox')


def locked(obj):
    if not hasattr(obj, '_v_lock'):
        obj._v_lock = asyncio.Lock()
    return obj._v_lock


def tm(request):
    assert getattr(request, 'app', None) is not None, \
        'Request has no app'
    assert getattr(request.app, '_p_jar', None) is not None, \
        'App has no ZODB connection'
    return request.app._p_jar.transaction_manager


def sync(request):
    assert getattr(request, 'app', None) is not None, \
        'Request has no app'
    assert getattr(request.app, 'executor', None) is not None, \
        'App has no executor'
    return lambda *args, **kwargs: request.app.loop.run_in_executor(
            request.app.executor, *args, **kwargs)


def get_current_request():
    """Get the nearest request from the current frame stack"""
    frame = inspect.currentframe()
    while frame is not None:
        if isinstance(frame.f_locals.get('self'), View):
            return frame.f_locals.get('self').request
        elif isinstance(frame.f_locals.get('self'), RequestHandler):
            return frame.f_locals['request']
        frame = frame.f_back
    raise RuntimeError('Unable to find the current request')


class RequestAwareTransactionManager(transaction.TransactionManager):

    lock = threading.Lock()

    # ITransactionManager
    def begin(self, request=None):
        if request is None:
            request = get_current_request()

        txn = getattr(request, '_txn', None)
        if txn is not None:
            txn.abort()
        txn = request._txn = transaction.Transaction(self._synchs, self)

        # ISynchronizer
        if self._synchs:
            self._synchs.map(lambda s: s.newTransaction(txn))

        return txn

    def __enter__(self):
        return self.begin(get_current_request())

    async def __aenter__(self):
        return self.begin(get_current_request())

    def __exit__(self, type_, value, traceback):
        request = get_current_request()
        if value is None:
            with self.lock:
                self.get(request).commit()
        else:
            with self.lock:
                self.get(request).abort()

    async def __aexit__(self, type_, value, traceback):
        request = get_current_request()
        if value is None:
            await sync(request)(self.get(request).commit)
        else:
            await sync(request)(self.get(request).abort)

    # ITransactionManager
    def get(self, request=None):
        if request is None:
            request = get_current_request()

        if getattr(request, '_txn', None) is None:
            request._txn = transaction.Transaction(self._synchs, self)
        return request._txn

    # ITransactionManager
    def free(self, txn):
        pass


@implementer(ISavepointDataManager)
class RequestDataManager(object):

    def __init__(self, request, connection):
        self.request = request
        self.connection = connection
        self._registered_objects = []
        self._savepoint_storage = None

    @property
    def txn_manager(self):
        return self.connection.transaction_manager

    def abort(self, txn):
        self.connection._registered_objects = self._registered_objects
        self.connection._savepoint_storage = self._savepoint_storage
        self.connection.abort(txn)
        self._registered_objects = []
        self._savepoint_storage = None
        delattr(self.request, '_txn_dm')

    def tpc_begin(self, txn):
        self.connection._registered_objects = self._registered_objects
        self.connection._savepoint_storage = self._savepoint_storage
        return self.connection.tpc_begin(txn)

    def commit(self, txn):
        self.connection.commit(txn)

    def tpc_vote(self, txn):
        self.connection.tpc_vote(txn)

    def tpc_finish(self, txn):
        self.connection.tpc_finish(txn)
        self._registered_objects = []
        self._savepoint_storage = None

    def tpc_abort(self, txn):
        self.connection.tpc_abort(txn)
        self._registered_objects = []
        self._savepoint_storage = None

    def sortKey(self):
        return self.connection.sortKey()

    def savepoint(self):
        self.connection._registered_objects = self._registered_objects
        self.connection._savepoint_storage = self._savepoint_storage
        savepoint = self.connection.savepoint()
        self._registered_objects = []
        self._savepoint_storage = None
        return savepoint


class RequestAwareConnection(ZODB.Connection.Connection):
    def _register(self, obj=None):
        request = get_current_request()
        if not hasattr(request, '_txn_dm'):
            request._txn_dm = RequestDataManager(request, self)
            self.transaction_manager.get(request).join(request._txn_dm)

        if obj is not None:
            request._txn_dm._registered_objects.append(obj)


class RequestAwareDB(ZODB.DB):
    klass = RequestAwareConnection


class Container(OOBTree):
    @property
    def __parent__(self):
        return getattr(self, '_v_parent', None)

    async def __getchild__(self, name):
        if name not in self:
            self[name] = Container()
            self[name]['__name__'] = name
            self[name]['__visited__'] = Length()
        self[name]._v_parent = self
        return self[name]


class RootFactory(AbstractResource):

    __parent__ = None

    def __init__(self, app):
        self.app = app
        self.root = app._p_jar.root.data

    def __getitem__(self, name):
        return Traverser(self, (name,))

    async def __getchild__(self, name):
        return await self.root.__getchild__(name)


class ContainerView(View):
    async def __call__(self):
        counter = self.resource['__visited__']

        async with locked(counter), tm(self.request):
            counter.change(1)

        parts = [str(counter()),
                 self.resource['__name__']]
        parent = self.resource.__parent__

        while parent is not None and parent.get('__name__') is not None:
            parts.append(parent['__name__'])
            parent = parent.__parent__

        return web.Response(text='/'.join(reversed(parts)))


def make_app():
    app = web.Application(router=TraversalRouter())
    app.executor = ThreadPoolExecutor(max_workers=1)
    app.router.set_root_factory(RootFactory)
    app.router.bind_view(Container, ContainerView)

    # Initialize DB
    db = ZODB.DB('Data.fs')
    conn = db.open()
    if getattr(conn.root, 'data', None) is None:
        with transaction.manager:
            conn.root.data = Container()
            conn.root._p_changed = 1
    conn.close()
    db.close()

    # Set request aware database for app
    db = RequestAwareDB('Data.fs')
    tm_ = RequestAwareTransactionManager()
    # While _p_jar is a funny name, it's consistent with Persistent API
    app._p_jar = db.open(transaction_manager=tm_)
    return app


def main():
    logging.basicConfig(level=logging.DEBUG)
    web.run_app(make_app(), port=8082)


if __name__ == "__main__":
    main()