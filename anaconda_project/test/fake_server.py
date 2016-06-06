# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
from __future__ import absolute_import, print_function

import socket
import sys
import threading

from tornado.ioloop import IOLoop
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets
from tornado.web import Application, RequestHandler


class ProjectViewHandler(RequestHandler):
    def __init__(self, application, *args, **kwargs):
        # Note: application is stored as self.application
        super(ProjectViewHandler, self).__init__(application, *args, **kwargs)

    def get(self, *args, **kwargs):
        # print("Received GET %r %r" % (args, kwargs), file=sys.stderr)
        path = args[0]
        if path == 'user':
            if 'auth' in self.application.server.fail_these:
                self.set_status(401)
            else:
                if 'missing_login' in self.application.server.fail_these:
                    self.set_header('Content-Type', 'application/json')
                    self.write('{}')
                else:
                    self.set_header('Content-Type', 'application/json')
                    self.write('{"login":"fake_username"}\n')
        elif path == 'user/foobar':
            self.set_header('Content-Type', 'application/json')
            self.write('{"login":"foobar"}\n')
        else:
            self.set_status(status_code=404)

    def post(self, *args, **kwargs):
        # print("Received POST %r %r" % (args, kwargs), file=sys.stderr)
        path = args[0]
        if path == 'apps/fake_username/projects':
            if 'create' in self.application.server.fail_these:
                self.set_status(501)
            else:
                self.set_header('Content-Type', 'application/json')
                self.write('{}\n')
        elif path.startswith('apps/fake_username/projects/'):
            path = path[len('apps/fake_username/projects/'):]
            [project, operation] = path.split("/", 1)
            # print("project=" + project + " operation=" + operation, file=sys.stderr)
            if operation == 'stage':
                if 'stage' in self.application.server.fail_these:
                    self.set_status(501)
                else:
                    post_url = self.application.server.url + "fake_s3"
                    self.set_header('Content-Type', 'application/json')
                    self.write(('{"post_url":"%s", ' + '"form_data":{"x-should-be-passed-back-to-us":"12345"},' +
                                '"dist_id":"rev42"}\n') % (post_url))
            elif operation == 'commit/rev42':
                if 'commit' in self.application.server.fail_these:
                    self.set_status(501)
                else:
                    self.set_header('Content-Type', 'application/json')
                    self.write('{"url":"http://example.com/whatevs"}')
            else:
                self.set_status(status_code=404)
        elif path == 'fake_s3':
            if 's3' in self.application.server.fail_these:
                self.set_status(501)
            else:
                if self.get_body_argument('x-should-be-passed-back-to-us') != '12345':
                    print("form_data for s3 wasn't sent", file=sys.stderr)
                    self.set_status(status_code=500)
                else:
                    assert 'file' in self.request.files
                    assert len(self.request.files['file']) == 1
                    fileinfo = self.request.files['file'][0]
                    assert fileinfo['filename'] is not None
                    assert len(fileinfo['body']) > 100  # shouldn't be some tiny or empty thing
        else:
            self.set_status(status_code=404)


class FakeAnacondaApplication(Application):
    def __init__(self, server, io_loop, **kwargs):
        self.server = server
        self.io_loop = io_loop

        patterns = [(r'/(.*)', ProjectViewHandler)]
        super(FakeAnacondaApplication, self).__init__(patterns, **kwargs)


class FakeAnacondaServer(object):
    def __init__(self, io_loop, fail_these):
        assert io_loop is not None

        self.fail_these = fail_these
        self._application = FakeAnacondaApplication(server=self, io_loop=io_loop)
        self._http = HTTPServer(self._application, io_loop=io_loop)

        # these would throw OSError on failure
        sockets = bind_sockets(port=None, address='127.0.0.1')

        self._port = None
        for s in sockets:
            # we have to find the ipv4 one
            if s.family == socket.AF_INET:
                self._port = s.getsockname()[1]
        assert self._port is not None

        self._http.add_sockets(sockets)
        self._http.start(1)

    @property
    def port(self):
        return self._port

    @property
    def url(self):
        return "http://localhost:%d/" % self.port

    def unlisten(self):
        """Permanently close down the HTTP server, no longer listen on any sockets."""
        self._http.close_all_connections()
        self._http.stop()


def _monkeypatch_client_config(monkeypatch, url):
    def _mock_get_config(user=True, site=True, remote_site=None):
        return {'url': url}

    monkeypatch.setattr('binstar_client.utils.get_config', _mock_get_config)


class FakeServerContext(object):
    def __init__(self, monkeypatch, fail_these):
        self._monkeypatch = monkeypatch
        self._fail_these = fail_these
        self._url = None
        self._loop = None
        self._started = threading.Condition()
        self._thread = threading.Thread(target=self._run)

    def __exit__(self, type, value, traceback):
        if self._loop is not None:
            # we can ONLY use add_callback here, since the loop is
            # running in a different thread.
            self._loop.add_callback(self._stop)
        self._thread.join()

    def __enter__(self):
        self._started.acquire()
        self._thread.start()
        self._started.wait()
        self._started.release()
        _monkeypatch_client_config(self._monkeypatch, self._url)
        return self._url

    def _run(self):
        self._loop = IOLoop()
        self._server = FakeAnacondaServer(io_loop=self._loop, fail_these=self._fail_these)
        self._url = self._server.url

        def notify_started():
            self._started.acquire()
            self._started.notify()
            self._started.release()

        self._loop.add_callback(notify_started)
        self._loop.start()
        # done
        self._server.unlisten()

    def _stop(self):
        def really_stop():
            if self._loop is not None:
                self._loop.stop()
                self._loop = None
        # the delay allows pending next-tick things to go ahead
        # and happen, which may avoid some problems with trying to
        # output to stdout after pytest closes it
        if self._loop is not None:
            self._loop.call_later(delay=0.05, callback=really_stop)


def fake_server(monkeypatch, fail_these=()):
    return FakeServerContext(monkeypatch, fail_these)
