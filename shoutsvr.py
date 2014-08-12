#!/usr/bin/env python

from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
import sys
import os
import json
import optparse
import urlparse
import logging
import time
import subprocess
import threading
import select

__author__ = 'Manuel Huber'
__copyright__ = "Copyright (c) 2014 Manuel Huber."
__version__ = '0.2'
__docformat__ = "restructuredtext en"

_DEFAULT_LOG_FORMAT = "%(name)s : %(threadName)s : %(levelname)s \
: %(message)s"

PORT = 8000

class WebRequestHandler (BaseHTTPRequestHandler):
    def _split_url (self):
        url = urlparse.urlparse(self.path)
        self._path = url.path
        self._param = urlparse.parse_qs(url.query)

    def do_GET (self):
        self._split_url()
        if self._path == "/":
            self._get_main()
        elif (self._path == "/stdout") and self._param.has_key("line"):
            self._get_line(int(self._param['line'][0]))
        elif self._path == "/start":
            self.server.proc.start()
            self._get_main()
        else:
            self.send_error(404)

    def _get_line (self, line):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        obj = self.server.proc.get(line)
        json.dump(obj, self.wfile)

    def _get_main (self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(\
"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
	<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
	<title>Console Output</title>
	<script src="http://code.jquery.com/jquery-1.9.1.js"></script>
</head>
<body>
<pre id="console">
</pre>
<hr>
<a id="console_restart" href="/start">Restart task</a>
<script type="text/javascript">

var co = {
	install: function () {
		co['timeout'] = 100;
		co['line'] = 0;
		setTimeout('co.update()', co.timeout);
	},
	update: function () {
		$.get('stdout', {
			line: co.line
		}, function (d) {
			console.log(d);
			if (d.hasOwnProperty('text') &&
			    d.hasOwnProperty('line')) {
				co['line'] = d.line;
				$('#console').append(d.text);
				co.scroll();
			}
			if (d.hasOwnProperty('fin') && (!d.fin)) {
				setTimeout('co.update()', co.timeout);
			}
		}, 'json');
	},
        scroll: function () {
		var c = $('#console');
		var pos = c.height() + c.position().top
		$('html, body').animate({
			scrollTop: pos
		}, 200);
	}
};

$(document).ready(function () {
	co.install();
});
</script>
</body>
</html>
""")


class Process (object):
    S_FIN_STDOUT = 1
    S_FIN_STDERR = 2
    S_FINISHED = 3
    S_PENDING = 0
    S_TERM_BIT = 4
    S_STOPPED = 7

    def __init__ (self, args):
        self._p = None
        self._args = args
        self._lb_lock = threading.RLock()
        self._start_lock = threading.RLock()
        self._buffer = list()
        self._max_line = 0
        self._state = self.S_STOPPED

    def _start_locked (self):
        with self._lb_lock:
            if self._state != self.S_STOPPED:
                logging.debug("Can't restart; process still running (state=%d)" %
                              self._state)
                return False
            self._p = subprocess.Popen(self._args, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            self._thread_out = threading.Thread(target=self._capture,
                                                args=(self._p.stdout, self.S_FIN_STDOUT, 'O'))
            self._thread_err = threading.Thread(target=self._capture,
                                                args=(self._p.stderr, self.S_FIN_STDERR, 'E'))
            self._buffer = list()
            self._max_line = 0
            self._state = self.S_PENDING
            self._thread_out.start()
            self._thread_err.start()
            return True

    def start (self):
        with self._start_lock:
            self._cleanup_locked()
            return self._start_locked()

    def _capture (self, fobj, fin_flag, id_):
        poller = select.poll()
        poller.register(fobj, select.POLLIN | select.POLLERR | select.POLLHUP)
        process = self._p
        running = True
        while running:
            for fd, flags in poller.poll(-1):
                if (flags & select.POLLIN):
                    line = fobj.readline()
                    with self._lb_lock:
                        self._buffer.append("%s: %s" % (id_, line))
                        self._max_line += 1
                elif (flags & select.POLLHUP):
                    with self._lb_lock:
                        self._state |= fin_flag
                    running = False
                    break
        process.wait()
        with self._lb_lock:
            # TRICKY: Will be set twice but doesn't matter as we join()
            self._state |= self.S_TERM_BIT
        # Exit function

    def get (self, start_line=0):
        obj = {'fin' : False}
        with self._lb_lock:
            if start_line < self._max_line:
                obj['line'] = self._max_line
                obj['text'] = self._buffer[start_line:self._max_line]
            else:
                obj['line'] = self._max_line
            if self._state == self.S_FINISHED:
                obj['fin'] = True
            if self._p is not None:
                ret = self._p.poll()
                if ret is not None:
                    obj['rc'] = ret
        return obj

    def _cleanup_locked (self):
        if self._p is not None:
            self._thread_out.join()
            self._thread_err.join()
            self._p.wait()
            self._p = None

    def cleanup (self):
        with self._start_lock:
            self._cleanup_locked()


class ConsoleService (object):
    def __init__ (self, args, options):
        self._log = logging.getLogger('bash-service')
        self._args = args
        self._port = options.port
        self._autostart = options.autostart

    def start (self):
        self._proc = Process(self._args)
        self._srv = HTTPServer(('', self._port), WebRequestHandler)
        self._srv.proc = self._proc
        if self._autostart:
            self._srv.proc.start()

        self._log.info("Start webserver")
        try:
            self._srv.serve_forever()
        except KeyboardInterrupt:
            self._srv.socket.close()
            self._log.info("Shutdown webserver")
        finally:
            self._proc.cleanup()


def main (argv):
    parser = optparse.OptionParser(
        usage="usage: %prog [options] -- command [arguments]",
        version=("%prog " + __version__)
    )
    parser.add_option("--verbose", action="store_const", const=logging.DEBUG,
        dest="verb_level", help="Verbose output (DEBUG)"
    )
    parser.add_option("-p", "--port",
        type=int,
        dest="port",
        default=PORT,
        help="Port to listen on"
    )
    parser.add_option("--autostart",
        action="store_true",
        dest="autostart",
        default=False,
        help="Run command once immediately"
    )
    parser.add_option("--quiet",
        action="store_const",
        const=logging.ERROR,
        dest="verb_level",
        help="Non verbose output: only output errors"
    )
    parser.set_defaults(version=False, verb_level=logging.INFO)

    options, args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, format=_DEFAULT_LOG_FORMAT,
                        level=options.verb_level)
    logging.debug("Starting up '%s' (%s)" % (
        os.path.basename(sys.argv[0]),
        datetime.now().isoformat())
    )

    if len(args) == 0:
        parser.error("Missing command!")

    csrv = ConsoleService(args, options)
    csrv.start()


if __name__ == '__main__':
    main(sys.argv[1:])
