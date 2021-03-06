# -*- coding: utf-8 -*-
"""
The module :mod:`actpy.tests.common` provides unittest test cases and a few
helpers and classes to write tests.

"""
import errno
import glob
import importlib
import json
import logging
import os
import select
import subprocess
import threading
import time
import itertools
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from lxml import etree
from pprint import pformat

import requests

from actpy.tools import pycompat

try:
    from itertools import zip_longest as izip_longest
except ImportError:
    from itertools import izip_longest
try:
    from xmlrpc import client as xmlrpclib
except ImportError:
    # pylint: disable=bad-python3-import
    import xmlrpclib

import actpy
from actpy import api
from actpy.service import security


_logger = logging.getLogger(__name__)

# The actpy library is supposed already configured.
ADDONS_PATH = actpy.tools.config['addons_path']
HOST = '127.0.0.1'
PORT = actpy.tools.config['http_port']
# Useless constant, tests are aware of the content of demo data
ADMIN_USER_ID = actpy.SUPERUSER_ID


def get_db_name():
    db = actpy.tools.config['db_name']
    # If the database name is not provided on the command-line,
    # use the one on the thread (which means if it is provided on
    # the command-line, this will break when installing another
    # database from XML-RPC).
    if not db and hasattr(threading.current_thread(), 'dbname'):
        return threading.current_thread().dbname
    return db


# For backwards-compatibility - get_db_name() should be used instead
DB = get_db_name()


def at_install(flag):
    """ Sets the at-install state of a test, the flag is a boolean specifying
    whether the test should (``True``) or should not (``False``) run during
    module installation.

    By default, tests are run right after installing the module, before
    starting the installation of the next module.
    """
    def decorator(obj):
        obj.at_install = flag
        return obj
    return decorator

def post_install(flag):
    """ Sets the post-install state of a test. The flag is a boolean
    specifying whether the test should or should not run after a set of
    module installations.

    By default, tests are *not* run after installation of all modules in the
    current installation set.
    """
    def decorator(obj):
        obj.post_install = flag
        return obj
    return decorator

class TreeCase(unittest.TestCase):
    def __init__(self, methodName='runTest'):
        super(TreeCase, self).__init__(methodName)
        self.addTypeEqualityFunc(etree._Element, self.assertTreesEqual)

    def assertTreesEqual(self, n1, n2, msg=None):
        self.assertEqual(n1.tag, n2.tag, msg)
        # Because lxml.attrib is an ordereddict for which order is important
        # to equality, even though *we* don't care
        self.assertEqual(dict(n1.attrib), dict(n2.attrib), msg)

        self.assertEqual((n1.text or u'').strip(), (n2.text or u'').strip(), msg)
        self.assertEqual((n1.tail or u'').strip(), (n2.tail or u'').strip(), msg)

        for c1, c2 in izip_longest(n1, n2):
            self.assertEqual(c1, c2, msg)

class BaseCase(TreeCase):
    """
    Subclass of TestCase for common OpenERP-specific code.

    This class is abstract and expects self.registry, self.cr and self.uid to be
    initialized by subclasses.
    """

    longMessage = True      # more verbose error message by default: https://www.actpy.com/r/Vmh

    def cursor(self):
        return self.registry.cursor()

    def ref(self, xid):
        """ Returns database ID for the provided :term:`external identifier`,
        shortcut for ``get_object_reference``

        :param xid: fully-qualified :term:`external identifier`, in the form
                    :samp:`{module}.{identifier}`
        :raise: ValueError if not found
        :returns: registered id
        """
        return self.browse_ref(xid).id

    def browse_ref(self, xid):
        """ Returns a record object for the provided
        :term:`external identifier`

        :param xid: fully-qualified :term:`external identifier`, in the form
                    :samp:`{module}.{identifier}`
        :raise: ValueError if not found
        :returns: :class:`~actpy.models.BaseModel`
        """
        assert "." in xid, "this method requires a fully qualified parameter, in the following form: 'module.identifier'"
        return self.env.ref(xid)

    @contextmanager
    def _assertRaises(self, exception):
        """ Context manager that clears the environment upon failure. """
        with super(BaseCase, self).assertRaises(exception) as cm:
            with self.env.clear_upon_failure():
                yield cm

    def assertRaises(self, exception, func=None, *args, **kwargs):
        if func:
            with self._assertRaises(exception):
                func(*args, **kwargs)
        else:
            return self._assertRaises(exception)

    def shortDescription(self):
        doc = self._testMethodDoc
        return doc and ' '.join(l.strip() for l in doc.splitlines() if not l.isspace()) or None

    if not pycompat.PY2:
        # turns out this thing may not be quite as useful as we thought...
        def assertItemsEqual(self, a, b, msg=None):
            self.assertCountEqual(a, b, msg=None)

class TransactionCase(BaseCase):
    """ TestCase in which each test method is run in its own transaction,
    and with its own cursor. The transaction is rolled back and the cursor
    is closed after each test.
    """

    def setUp(self):
        self.registry = actpy.registry(get_db_name())
        #: current transaction's cursor
        self.cr = self.cursor()
        self.uid = actpy.SUPERUSER_ID
        #: :class:`~actpy.api.Environment` for the current test case
        self.env = api.Environment(self.cr, self.uid, {})

        @self.addCleanup
        def reset():
            # rollback and close the cursor, and reset the environments
            self.registry.clear_caches()
            self.registry.reset_changes()
            self.env.reset()
            self.cr.rollback()
            self.cr.close()

        self.patch(type(self.env['res.partner']), '_get_gravatar_image', lambda *a: False)

    def patch(self, obj, key, val):
        """ Do the patch ``setattr(obj, key, val)``, and prepare cleanup. """
        old = getattr(obj, key)
        setattr(obj, key, val)
        self.addCleanup(setattr, obj, key, old)

    def patch_order(self, model, order):
        """ Patch the order of the given model (name), and prepare cleanup. """
        self.patch(type(self.env[model]), '_order', order)


class SingleTransactionCase(BaseCase):
    """ TestCase in which all test methods are run in the same transaction,
    the transaction is started with the first test method and rolled back at
    the end of the last.
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = actpy.registry(get_db_name())
        cls.cr = cls.registry.cursor()
        cls.uid = actpy.SUPERUSER_ID
        cls.env = api.Environment(cls.cr, cls.uid, {})

    @classmethod
    def tearDownClass(cls):
        # rollback and close the cursor, and reset the environments
        cls.registry.clear_caches()
        cls.env.reset()
        cls.cr.rollback()
        cls.cr.close()


savepoint_seq = itertools.count()
class SavepointCase(SingleTransactionCase):
    """ Similar to :class:`SingleTransactionCase` in that all test methods
    are run in a single transaction *but* each test case is run inside a
    rollbacked savepoint (sub-transaction).

    Useful for test cases containing fast tests but with significant database
    setup common to all cases (complex in-db test data): :meth:`~.setUpClass`
    can be used to generate db test data once, then all test cases use the
    same data without influencing one another but without having to recreate
    the test data either.
    """
    def setUp(self):
        self._savepoint_id = next(savepoint_seq)
        self.cr.execute('SAVEPOINT test_%d' % self._savepoint_id)
    def tearDown(self):
        self.cr.execute('ROLLBACK TO SAVEPOINT test_%d' % self._savepoint_id)
        self.env.clear()
        self.registry.clear_caches()


class HttpCase(TransactionCase):
    """ Transactional HTTP TestCase with url_open and phantomjs helpers.
    """
    registry_test_mode = True

    def __init__(self, methodName='runTest'):
        super(HttpCase, self).__init__(methodName)
        # v8 api with correct xmlrpc exception handling.
        self.xmlrpc_url = url_8 = 'http://%s:%d/xmlrpc/2/' % (HOST, PORT)
        self.xmlrpc_common = xmlrpclib.ServerProxy(url_8 + 'common')
        self.xmlrpc_db = xmlrpclib.ServerProxy(url_8 + 'db')
        self.xmlrpc_object = xmlrpclib.ServerProxy(url_8 + 'object')

    def setUp(self):
        super(HttpCase, self).setUp()
        if self.registry_test_mode:
            self.registry.enter_test_mode()
            self.addCleanup(self.registry.leave_test_mode)
        # setup a magic session_id that will be rollbacked
        self.session = actpy.http.root.session_store.new()
        self.session_id = self.session.sid
        self.session.db = get_db_name()
        actpy.http.root.session_store.save(self.session)
        # setup an url opener helper
        self.opener = requests.Session()
        self.opener.cookies['session_id'] = self.session_id

    def url_open(self, url, data=None, timeout=10):
        if url.startswith('/'):
            url = "http://%s:%s%s" % (HOST, PORT, url)
        if data:
            return self.opener.post(url, data=data, timeout=timeout)
        return self.opener.get(url, timeout=timeout)

    def authenticate(self, user, password):
        # stay non-authenticated
        if user is None:
            return

        db = get_db_name()
        uid = self.registry['res.users'].authenticate(db, user, password, None)
        env = api.Environment(self.cr, uid, {})

        # self.session.authenticate(db, user, password, uid=uid)
        # OpenERPSession.authenticate accesses the current request, which we
        # don't have, so reimplement it manually...
        session = self.session

        session.db = db
        session.uid = uid
        session.login = user
        session.session_token = uid and security.compute_session_token(session)
        session.context = env['res.users'].context_get() or {}
        session.context['uid'] = uid
        session._fix_lang(session.context)

        actpy.http.root.session_store.save(session)

    def phantom_poll(self, phantom, timeout):
        """ Phantomjs Test protocol.

        Use console.log in phantomjs to output test results:

        - for a success: console.log("ok")
        - for an error:  console.log("error")

        Other lines are relayed to the test log.

        """
        logger = _logger.getChild('phantomjs')
        t0 = datetime.now()
        td = timedelta(seconds=timeout)
        buf = bytearray()
        pid = phantom.stdout.fileno()
        while True:
            # timeout
            self.assertLess(datetime.now() - t0, td,
                "PhantomJS tests should take less than %s seconds" % timeout)

            # read a byte
            try:
                ready, _, _ = select.select([pid], [], [], 0.5)
            except select.error as e:
                # In Python 2, select.error has no relation to IOError or
                # OSError, and no errno/strerror/filename, only a pair of
                # unnamed arguments (matching errno and strerror)
                err, _ = e.args
                if err == errno.EINTR:
                    continue
                raise

            if not ready:
                continue

            s = os.read(pid, 4096)
            if not s:
                self.fail("Ran out of data to read")
            buf.extend(s)

            # process lines
            while b'\n' in buf and (not buf.startswith(b'<phantomLog>') or b'</phantomLog>' in buf):

                if buf.startswith(b'<phantomLog>'):
                    line, buf = buf[12:].split(b'</phantomLog>\n', 1)
                else:
                    line, buf = buf.split(b'\n', 1)
                line = line.decode('utf-8')

                lline = line.lower()
                if lline.startswith(("error", "server application error")):
                    try:
                        # when errors occur the execution stack may be sent as a JSON
                        prefix = lline.index('error') + 6
                        self.fail(pformat(json.loads(line[prefix:])))
                    except ValueError:
                        self.fail(lline)
                elif lline.startswith("warning"):
                    logger.warn(line)
                else:
                    logger.info(line)

                if line == "ok":
                    return True

    def phantom_run(self, cmd, timeout):
        _logger.info('phantom_run executing %s', ' '.join(cmd))

        ls_glob = os.path.expanduser('~/.qws/share/data/Ofi Labs/PhantomJS/http_%s_%s.*' % (HOST, PORT))
        ls_glob2 = os.path.expanduser('~/.local/share/Ofi Labs/PhantomJS/http_%s_%s.*' % (HOST, PORT))
        for i in (glob.glob(ls_glob) + glob.glob(ls_glob2)):
            _logger.info('phantomjs unlink localstorage %s', i)
            os.unlink(i)
        try:
            phantom = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None, close_fds=True)
        except OSError:
            raise unittest.SkipTest("PhantomJS not found")
        try:
            result = self.phantom_poll(phantom, timeout)
            self.assertTrue(
                result,
                "PhantomJS test completed without reporting success; "
                "the log may contain errors or hints.")
        finally:
            # kill phantomjs if phantom.exit() wasn't called in the test
            if phantom.poll() is None:
                _logger.info("Terminating phantomjs")
                phantom.terminate()
                phantom.wait()
            else:
                # if we had to terminate phantomjs its return code is
                # always -15 so we don't care
                # check PhantomJS health
                from signal import SIGSEGV
                _logger.info("Phantom JS return code: %d" % phantom.returncode)
                if phantom.returncode == -SIGSEGV:
                    _logger.error("Phantom JS has crashed (segmentation fault) during testing; log may not be relevant")

            self._wait_remaining_requests()

    def _wait_remaining_requests(self):
        t0 = int(time.time())
        for thread in threading.enumerate():
            if thread.name.startswith('actpy.service.http.request.'):
                join_retry_count = 10
                while thread.isAlive():
                    # Need a busyloop here as thread.join() masks signals
                    # and would prevent the forced shutdown.
                    thread.join(0.05)
                    join_retry_count -= 1
                    if join_retry_count < 0:
                        _logger.warning("Stop waiting for thread %s handling request for url %s",
                                        thread.name, thread.url)
                        break
                    time.sleep(0.5)
                    t1 = int(time.time())
                    if t0 != t1:
                        _logger.info('remaining requests')
                        actpy.tools.misc.dumpstacks()
                        t0 = t1

    def phantom_js(self, url_path, code, ready="window", login=None, timeout=60, **kw):
        """ Test js code running in the browser
        - optionnally log as 'login'
        - load page given by url_path
        - wait for ready object to be available
        - eval(code) inside the page

        To signal success test do:
        console.log('ok')

        To signal failure do:
        console.log('error')

        If neither are done before timeout test fails.
        """
        options = {
            'port': PORT,
            'db': get_db_name(),
            'url_path': url_path,
            'code': code,
            'ready': ready,
            'timeout' : timeout,
            'session_id': self.session_id,
        }
        options.update(kw)

        self.authenticate(login, login)

        phantomtest = os.path.join(os.path.dirname(__file__), 'phantomtest.js')
        cmd = ['phantomjs', phantomtest, json.dumps(options)]
        self.phantom_run(cmd, timeout)

def can_import(module):
    """ Checks if <module> can be imported, returns ``True`` if it can be,
    ``False`` otherwise.

    To use with ``unittest.skipUnless`` for tests conditional on *optional*
    dependencies, which may or may be present but must still be tested if
    possible.
    """
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    else:
        return True
