# -*- coding: utf-8 -*-

from __future__ import absolute_import, division

import ssl
import asyncio
import errno
import os
import socket
import struct
import sys

from thriftpy2.transport import TTransportException
from thriftpy2.transport._ssl import (
    create_thriftpy_context,
    RESTRICTED_SERVER_CIPHERS,
    DEFAULT_CIPHERS
)

MAC_OR_BSD = sys.platform == 'darwin' or sys.platform.startswith('freebsd')


class TAsyncSocket(object):
    """Socket implementation for client side."""

    def __init__(self, host=None, port=None, unix_socket=None,
                 sock=None, socket_family=socket.AF_INET,
                 socket_timeout=3000, connect_timeout=None,
                 ssl_context=None, validate=True,
                 cafile=None, capath=None, certfile=None, keyfile=None,
                 ciphers=DEFAULT_CIPHERS):
        """Initialize a TSocket

        TSocket can be initialized in 3 ways:
        * host + port. can configure to use AF_INET/AF_INET6
        * unix_socket
        * socket. should pass already opened socket here.

        @param host(str)    The host to connect to.
        @param port(int)    The (TCP) port to connect to.
        @param unix_socket(str) The filename of a unix socket to connect to.
        @param sock(socket)     Initialize with opened socket directly.
            If this param used, the host, port and unix_socket params will
            be ignored.
        @param socket_family(str) socket.AF_INET or socket.AF_INET6. only
            take effect when using host/port
        @param socket_timeout   socket timeout in ms
        @param connect_timeout  connect timeout in ms, only used in
            connection, will be set to socket_timeout if not set.
        @param validate(bool)       Set to False to disable SSL certificate
            validation and hostname validation. Default enabled.
        @param cafile(str)          Path to a file of concatenated CA
            certificates in PEM format.
        @param capath(str)           path to a directory containing several CA
            certificates in PEM format, following an OpenSSL specific layout.
        @param certfile(str)        The certfile string must be the path to a
            single file in PEM format containing the certificate as well as
            any number of CA certificates needed to establish the
            certificate’s authenticity.
        @param keyfile(str)         The keyfile string, if not present,
            the private key will be taken from certfile as well.
        @param ciphers(list<str>)   The cipher suites to allow
        @param ssl_context(SSLContext)  Customize the SSLContext, can be used
            to persist SSLContext object. Caution it's easy to get wrong, only
            use if you know what you're doing.
        """
        self.read_sock = None
        self.write_sock = None
        if sock:
            self.raw_sock = sock
        elif unix_socket:
            self.unix_socket = unix_socket
            self.host = None
            self.port = None
            self.raw_sock = None
            self.sock_factory = asyncio.open_unix_connection
        else:
            self.unix_socket = None
            self.host = host
            self.port = port
            self.raw_sock = None
            self.sock_factory = asyncio.open_connection

        self.socket_family = socket_family
        self.socket_timeout = socket_timeout / 1000 if socket_timeout else None
        self.connect_timeout = connect_timeout / 1000 if connect_timeout \
            else self.socket_timeout

        if ssl_context:
            self.ssl_context = ssl_context
            self.server_hostname = host
        elif certfile or keyfile:
            self.server_hostname = host
            self.ssl_context = create_thriftpy_context(server_side=False,
                                                       ciphers=ciphers)

            if cafile or capath:
                self.ssl_context.load_verify_locations(cafile=cafile, capath=capath)

            if certfile:
                self.ssl_context.load_cert_chain(certfile, keyfile=keyfile)

            if not validate:
                self.ssl_context.check_hostname = False
                self.ssl_context.verify_mode = ssl.CERT_NONE
        else:
            self.ssl_context = None
            self.server_hostname = None

    async def open(self):
        addr = self.unix_socket or (self.host, self.port)
        try:
            if self.unix_socket:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(addr), self.connect_timeout
                )
            else:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=self.ssl_context),
                    self.connect_timeout,
                )

            write_sock = self.writer.get_extra_info("socket")
            read_sock = self.reader._transport.get_extra_info("socket")
            self.write_sock = write_sock
            self.read_sock = read_sock
            linger = struct.pack("ii", 0, 0)
            for sock in [self.read_sock, self.write_sock]:
                # socket options
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        except (socket.error, OSError):
            raise TTransportException(
                type=TTransportException.NOT_OPEN,
                message="Could not connect to %s" % str(addr),
            )

    def set_handle(self, sock):
        self.raw_sock = sock

    def set_timeout(self, ms):
        """Backward compat api, will bind the timeout to both connect_timeout
        and socket_timeout.
        """
        self.socket_timeout = ms / 1000 if (ms and ms > 0) else None
        self.connect_timeout = self.socket_timeout
        if self.read_sock:
            self.read_sock.settimeout(self.socket_timeout)
        if self.write_sock:
            self.write_sock.settimeout(self.socket_timeout)


    async def read(self, sz):
        try:
            buff = await asyncio.wait_for(self.reader.read(sz), self.connect_timeout)
        except asyncio.TimeoutError:
            raise TTransportException(
                type=TTransportException.TIMED_OUT, message="TSocket read timed out"
            )
        except asyncio.IncompleteReadError as e:
            raise TTransportException(
                type=TTransportException.END_OF_FILE, message="TSocket read 0 bytes"
            )
        except socket.error as e:
            if e.errno == errno.ECONNRESET and MAC_OR_BSD:
                # freebsd and Mach don't follow POSIX semantic of recv
                # and fail with ECONNRESET if peer performed shutdown.
                # See corresponding comment and code in TSocket::read()
                # in lib/cpp/src/transport/TSocket.cpp.
                self.close()
                # Trigger the check to raise the END_OF_FILE exception below.
                buff = ''
            else:
                raise

        if len(buff) == 0:
            raise TTransportException(type=TTransportException.END_OF_FILE,
                                      message='TSocket read 0 bytes')
        return buff

    def write(self, buff):
        self.writer.write(buff)

    async def flush(self):
        await asyncio.wait_for(self.writer.drain(), self.connect_timeout)

    def close(self):
        if self.writer:
            self.writer.close()
            # await self.writer.wait_closed()
        
        if self.reader:
            self.reader._transport.close()

        if self.read_sock:
            self.read_sock.close()

        if self.write_sock:
            self.write_sock.close()

class TAsyncServerSocket(object):
    """Socket implementation for server side."""

    def __init__(self, host=None, port=None, unix_socket=None,
                 socket_family=socket.AF_INET, client_timeout=3000,
                 backlog=128, ssl_context=None, certfile=None, keyfile=None,
                 ciphers=RESTRICTED_SERVER_CIPHERS):
        """Initialize a TServerSocket

        TSocket can be initialized in 2 ways:
        * host + port. can configure to use AF_INET/AF_INET6
        * unix_socket

        @param host(str)    The host to connect to
        @param port(int)    The (TCP) port to connect to
        @param unix_socket(str) The filename of a unix socket to connect to
        @param socket_family(str) socket.AF_INET or socket.AF_INET6. only
            take effect when using host/port
        @param client_timeout   client socket timeout
        @param backlog          backlog for server socket
        @param certfile(str)        The server cert pem filename
        @param keyfile(str)         The server cert key filename
        @param ciphers(list<str>)   The cipher suites to allow
        @param ssl_context(SSLContext)  Customize the SSLContext, can be used
            to persist SSLContext object. Caution it's easy to get wrong, only
            use if you know what you're doing.
        """
        if unix_socket:
            self.unix_socket = unix_socket
            self.host = None
            self.port = None
            self.sock_factory = asyncio.start_unix_server
        else:
            self.unix_socket = None
            self.host = host
            self.port = port
            self.sock_factory = asyncio.start_server

        self.socket_family = socket_family
        self.client_timeout = client_timeout / 1000 if client_timeout else None
        self.backlog = backlog

        if ssl_context:
            self.ssl_context = ssl_context
        elif certfile:
            if not os.access(certfile, os.R_OK):
                raise IOError("No such certfile found: %s" % certfile)

            self.ssl_context = create_thriftpy_context(server_side=True,
                                                       ciphers=ciphers)
            self.ssl_context.load_cert_chain(certfile, keyfile=keyfile)
        else:
            self.ssl_context = None

    def _init_sock(self):
        if self.unix_socket:
            # try remove the sock file it already exists
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                _sock.connect(self.unix_socket)
            except (socket.error, OSError) as err:
                if err.errno == errno.ECONNREFUSED:
                    os.unlink(self.unix_socket)
        else:
            _sock = socket.socket(self.socket_family, socket.SOCK_STREAM)

        _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except socket.error as err:
                if err[0] in (errno.ENOPROTOOPT, errno.EINVAL):
                    pass
                else:
                    raise
        _sock.settimeout(None)
        self.raw_sock = _sock

    def listen(self):
        self._init_sock()

        addr = self.unix_socket or (self.host, self.port)
        self.raw_sock.bind(addr)
        self.raw_sock.listen(self.backlog)

    async def accept(self, callback):
        server = await self.sock_factory(
            self._create_client_connected_cb(callback),
            sock=self.raw_sock,
            ssl=self.ssl_context
        )
        return server

    def _create_client_connected_cb(self, callback):

        async def client_connected_cb(reader, writer):
            try:
                await asyncio.wait_for(
                    callback(StreamHandler(reader, writer)),
                    self.client_timeout
                )
            except asyncio.exceptions.TimeoutError:
                writer.close()

        return client_connected_cb

    def close(self):
        if not self.raw_sock:
            return

        try:
            self.raw_sock.shutdown(socket.SHUT_RDWR)
            self.raw_sock.close()
        except (socket.error, OSError):
            pass


class StreamHandler(object):
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def read(self, sz):
        try:
            buff = await self.reader.read(sz)
        except socket.error as e:
            if e.errno == errno.ECONNRESET and MAC_OR_BSD:
                # freebsd and Mach don't follow POSIX semantic of recv
                # and fail with ECONNRESET if peer performed shutdown.
                # See corresponding comment and code in TSocket::read()
                # in lib/cpp/src/transport/TSocket.cpp.
                self.close()
                # Trigger the check to raise the END_OF_FILE exception below.
                buff = ''
            else:
                raise

        if len(buff) == 0:
            raise TTransportException(type=TTransportException.END_OF_FILE,
                                      message='TSocket read 0 bytes')
        return buff

    def write(self, buff):
        self.writer.write(buff)

    async def flush(self):
        await self.writer.drain()

    def close(self):
        try:
            self.writer.close()
        except (socket.error, OSError):
            pass

    async def open(self):
        pass
