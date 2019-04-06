# -*- coding: utf-8 -*-
"""
wsproto/connection
~~~~~~~~~~~~~~~~~~

An implementation of a WebSocket connection.
"""

from collections import deque
from enum import Enum

from .events import BytesMessage, CloseConnection, Message, Ping, Pong, TextMessage
from .frame_protocol import CloseReason, FrameProtocol, Opcode, ParseFailed
from .utilities import LocalProtocolError


class ConnectionState(Enum):
    """
    RFC 6455, Section 4 - Opening Handshake
    """

    CONNECTING = 0
    OPEN = 1
    REMOTE_CLOSING = 2
    LOCAL_CLOSING = 3
    CLOSED = 4
    REJECTING = 5


class ConnectionType(Enum):
    #: This connection will act as client and talk to a remote server
    CLIENT = 1

    #: This connection will as as server and waits for client connections
    SERVER = 2


CLIENT = ConnectionType.CLIENT
SERVER = ConnectionType.SERVER


class Connection:
    """
    A low-level WebSocket connection object.

    This wraps two other protocol objects, an HTTP/1.1 protocol object used
    to do the initial HTTP upgrade handshake and a WebSocket frame protocol
    object used to exchange messages and other control frames.

    :param conn_type: Whether this object is on the client- or server-side of
        a connection. To initialise as a client pass ``CLIENT`` otherwise
        pass ``SERVER``.
    :type conn_type: ``ConnectionType``
    """

    def __init__(self, connection_type, extensions=None, trailing_data=b""):
        # type: (bool, Optional[List[Extension]], bytes) -> None
        self.client = connection_type is ConnectionType.CLIENT
        self._events = deque()
        self._proto = FrameProtocol(self.client, extensions or [])
        self._state = ConnectionState.OPEN
        self.receive_data(trailing_data)

    @property
    def state(self):
        # type: () -> ConnectionState
        return self._state

    def send(self, event):
        # type: (wsproto.events.Event) -> bytes
        data = b""
        if isinstance(event, Message):
            data += self._proto.send_data(event.data, event.message_finished)
        elif isinstance(event, Ping):
            data += self._proto.ping(event.payload)
        elif isinstance(event, Pong):
            data += self._proto.pong(event.payload)
        elif isinstance(event, CloseConnection):
            if self.state not in {ConnectionState.OPEN, ConnectionState.REMOTE_CLOSING}:
                raise LocalProtocolError(
                    "Connection cannot be closed in state %s" % self.state
                )
            data += self._proto.close(event.code, event.reason)
            if self.state == ConnectionState.REMOTE_CLOSING:
                self._state = ConnectionState.CLOSED
            else:
                self._state = ConnectionState.LOCAL_CLOSING
        else:
            raise LocalProtocolError("Event {} cannot be sent.".format(event))
        return data

    def receive_data(self, data):
        # type: (bytes) -> None
        """
        Pass some received data to the connection for handling.

        A list of events that the remote peer triggered by sending this data can
        be retrieved with :meth:`~wsproto.connection.Connection.events`.

        :param data: The data received from the remote peer on the network.
        :type data: ``bytes``
        """

        if data is None:
            # "If _The WebSocket Connection is Closed_ and no Close control
            # frame was received by the endpoint (such as could occur if the
            # underlying transport connection is lost), _The WebSocket
            # Connection Close Code_ is considered to be 1006."
            self._events.append(CloseConnection(code=CloseReason.ABNORMAL_CLOSURE))
            self._state = ConnectionState.CLOSED
            return

        if self.state in (ConnectionState.OPEN, ConnectionState.LOCAL_CLOSING):
            self._proto.receive_bytes(data)
        elif self.state is ConnectionState.CLOSED:
            raise LocalProtocolError("Connection already closed.")

    def events(self):
        # type: () -> Generator[Event, None, None]
        """
        Return a generator that provides any events that have been generated
        by protocol activity.

        :returns: generator of :class:`Event <wsproto.events.Event>` subclasses
        """
        while self._events:
            yield self._events.popleft()

        try:
            for frame in self._proto.received_frames():
                if frame.opcode is Opcode.PING:
                    assert frame.frame_finished and frame.message_finished
                    yield Ping(payload=frame.payload)

                elif frame.opcode is Opcode.PONG:
                    assert frame.frame_finished and frame.message_finished
                    yield Pong(payload=frame.payload)

                elif frame.opcode is Opcode.CLOSE:
                    code, reason = frame.payload
                    if self.state is ConnectionState.LOCAL_CLOSING:
                        self._state = ConnectionState.CLOSED
                    else:
                        self._state = ConnectionState.REMOTE_CLOSING
                    yield CloseConnection(code=code, reason=reason)

                elif frame.opcode is Opcode.TEXT:
                    yield TextMessage(
                        data=frame.payload,
                        frame_finished=frame.frame_finished,
                        message_finished=frame.message_finished,
                    )

                elif frame.opcode is Opcode.BINARY:
                    yield BytesMessage(
                        data=frame.payload,
                        frame_finished=frame.frame_finished,
                        message_finished=frame.message_finished,
                    )
        except ParseFailed as exc:
            yield CloseConnection(code=exc.code, reason=str(exc))
