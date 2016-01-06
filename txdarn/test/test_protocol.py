import json

from twisted.trial import unittest
from twisted.internet.defer import Deferred
from twisted.internet.protocol import Protocol, Factory
from twisted.protocols.policies import WrappingFactory
from twisted.test.proto_helpers import StringTransport
from twisted.internet.task import Clock
from twisted.internet.address import IPv4Address
from twisted.internet.error import ConnectionDone
# TODO: don't use twisted's private test APIs
from twisted.web.test.requesthelper import DummyRequest
from .. import protocol as P


class sockJSJSONTestCase(unittest.SynchronousTestCase):

    def test_sockJSJSON(self):
        self.assertEqual(P.sockJSJSON([3000, 'Go away!']),
                         b'[3000,"Go away!"]')


class HeartbeatClockTestCase(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.period = 25.0
        self.heartbeats = 0

        def fakeHeartbeat():
            self.heartbeats += 1

        self.heartbeater = P.HeartbeatClock(fakeHeartbeat,
                                            period=self.period,
                                            clock=self.clock)

    def test_neverScheduled(self):
        '''Heartbeats are not scheduled before their first schedule(), and are
        not scheduled if we immediately stop() the HeartbeatClock.  A
        stopped HeartbeatClock can never schedule any other
        heartbeats.

        '''

        self.assertFalse(self.clock.getDelayedCalls())
        self.heartbeater.stop()
        self.assertFalse(self.clock.getDelayedCalls())

        with self.assertRaises(KeyError):
            self.heartbeater.schedule()

        self.assertFalse(self.clock.getDelayedCalls())

    def test_schedule(self):
        '''Heartbeats are scheduled and recur if not interrupted.'''

        self.heartbeater.schedule()
        pendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(), [pendingBeat])

        self.clock.advance(self.period * 2)
        self.assertEqual(self.heartbeats, 1)

        rescheduledPendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(),
                         [rescheduledPendingBeat])

    def test_schedule_interrupts(self):
        '''A schedule() call will remove the pending heartbeat and reschedule
        it for later.

        '''
        self.heartbeater.schedule()
        pendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(), [pendingBeat])

        self.heartbeater.schedule()

        self.assertFalse(self.heartbeats)

        rescheduledPendingBeat = self.heartbeater.pendingHeartbeat
        self.assertIsNot(pendingBeat, rescheduledPendingBeat)
        self.assertEqual(self.clock.getDelayedCalls(),
                         [rescheduledPendingBeat])

    def test_schedule_stop(self):
        '''A stop() call removes any pending heartbeats.'''
        self.heartbeater.schedule()
        pendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(), [pendingBeat])

        self.heartbeater.stop()
        self.assertFalse(self.heartbeats)
        self.assertFalse(self.clock.getDelayedCalls())


class RecordsHeartbeat(object):

    def __init__(self):
        self.scheduleCalls = 0
        self.stopCalls = 0

    def scheduleCalled(self):
        self.scheduleCalls += 1

    def stopCalled(self):
        self.stopCalls += 1


class FakeHeartbeatClock(object):

    def __init__(self, recorder):
        self.writeHeartbeat = None
        self._recorder = recorder

    def schedule(self):
        self._recorder.scheduleCalled()

    def stop(self):
        self._recorder.stopCalled()


class SockJSProtocolStateMachineTestCase(unittest.TestCase):

    def setUp(self):
        self.heartbeatRecorder = RecordsHeartbeat()
        self.heartbeater = FakeHeartbeatClock(self.heartbeatRecorder)
        self.sockJSMachine = P.SockJSProtocolMachine(self.heartbeater)
        self.transport = StringTransport()

    def test_disconnectBeforeConnect(self):
        '''Disconnecting before connecting permanently disconnects
        a SockJSProtocolMachine.

        '''
        self.sockJSMachine.disconnect()
        self.assertFalse(self.transport.value())

        with self.assertRaises(KeyError):
            self.sockJSMachine.connect(self.transport)

    def test_connect(self):
        '''SockJSProtocolMachine.connect writes an opening frame and schedules
        a heartbeat.

        '''
        self.sockJSMachine.connect(self.transport)
        self.assertEqual(self.transport.value(), b'o')
        self.assertTrue(self.heartbeatRecorder.scheduleCalled)

    def test_write(self):
        '''SockJSProtocolMachine.write writes the requested data and
        (re)schedules a heartbeat.

        '''
        self.sockJSMachine.connect(self.transport)
        self.sockJSMachine.write([1, 'something'])

        expectedProtocolTrace = b''.join([b'o', b'a[1,"something"]'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)

        self.assertEqual(self.heartbeatRecorder.scheduleCalls, 2)

    def test_heartbeat(self):
        '''SockJSProtocolMachine.heartbeat writes a heartbeat!'''
        self.sockJSMachine.connect(self.transport)
        self.sockJSMachine.heartbeat()

        expectedProtocolTrace = b''.join([b'o', b'h'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)

        self.assertTrue(self.heartbeatRecorder.scheduleCalls)

    def test_withHeartBeater(self):
        '''SockJSProtocolMachine.withHeartbeater should associate a new
        instance's heartbeat method with the heartbeater.

        '''
        instance = P.SockJSProtocolMachine.withHeartbeater(
            self.heartbeater)
        instance.connect(self.transport)
        self.heartbeater.writeHeartbeat()

        expectedProtocolTrace = b''.join([b'o', b'h'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)

    def test_receive(self):
        '''SockJSProtocolMachine.receive decodes JSON.'''
        self.sockJSMachine.connect(self.transport)
        self.assertEqual(self.sockJSMachine.receive(b'[1,"something"]'),
                         [1, "something"])

    def test_disconnect(self):
        '''SockJSProtocolMachine.disconnect writes a close frame, disconnects
        the transport and cancels any pending heartbeats.

        '''
        self.sockJSMachine.connect(self.transport)
        self.sockJSMachine.disconnect(reason=P.DISCONNECT.GO_AWAY)

        expectedProtocolTrace = b''.join([b'o', b'c[3000,"Go away!"]'])
        self.assertTrue(self.transport.value(), expectedProtocolTrace)
        self.assertTrue(self.heartbeatRecorder.stopCalls)

    def test_jsonEncoder(self):
        '''SockJSProtocolMachine can use a json.JSONEncoder subclass for
        writes.

        '''
        class ComplexEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, complex):
                    return [obj.real, obj.imag]
                # Let the base class default method raise the TypeError
                return json.JSONEncoder.default(self, obj)

        encodingSockJSMachine = P.SockJSProtocolMachine(
            self.heartbeater,
            jsonEncoder=ComplexEncoder)

        encodingSockJSMachine.connect(self.transport)
        encodingSockJSMachine.write([2 + 1j])

        expectedProtocolTrace = b''.join([b'o', b'a[[2.0,1.0]]'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)

    def test_jsonDecoder(self):
        '''SockJSProtocolMachine can use a json.JSONDecoder subclass for
        receives.

        '''
        class SetDecoder(json.JSONDecoder):
            def __init__(self, *args, **kwargs):
                kwargs['object_hook'] = self.set_object_hook
                super(SetDecoder, self).__init__(*args, **kwargs)

            def set_object_hook(self, obj):
                if isinstance(obj, dict) and obj.get('!set'):
                    return set(obj['!set'])
                return obj

        decodingSockJSMachine = P.SockJSProtocolMachine(
            self.heartbeater,
            jsonDecoder=SetDecoder)

        decodingSockJSMachine.connect(self.transport)
        result = decodingSockJSMachine.receive(b'{"!set": [1, 2, 3]}')
        self.assertEqual(result, {1, 2, 3})


class EchoProtocol(Protocol):
    connectionMadeCalls = 0

    def connectionMade(self):
        self.connectionMadeCalls += 1
        if self.connectionMadeCalls > 1:
            assert False, "connectionMade must only be called once"

    def dataReceived(self, data):
        if isinstance(data, list):
            self.transport.writeSequence(data)
        else:
            self.transport.write(data)


class SockJSProtocolTestCase(unittest.TestCase):

    def setUp(self):
        self.transport = StringTransport()
        self.clock = Clock()

        wrappedFactory = Factory.forProtocol(EchoProtocol)
        self.factory = P.SockJSProtocolFactory(wrappedFactory,
                                               clock=self.clock)
        self.address = IPv4Address('TCP', '127.0.0.1', 80)
        self.protocol = self.factory.buildProtocol(self.address)
        self.protocol.makeConnection(self.transport)

    def test_dataReceived_write(self):
        self.protocol.dataReceived(b'"something"')
        expectedProtocolTrace = b''.join([b'o', b'a["something"]'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)

    def test_dataReceived_writeSequence(self):
        self.protocol.dataReceived(b'["something", "else"]')
        expectedProtocolTrace = b''.join([b'o', b'a["something","else"]'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)

    def test_loseConnection(self):
        self.protocol.loseConnection()
        expectedProtocolTrace = b''.join([b'o', b'c[3000,"Go away!"]'])
        self.assertEqual(self.transport.value(), expectedProtocolTrace)


class TestFactoryForRequestSessionProtocol(WrappingFactory):
    '''
    A factory for testing RequestSessionProtocol
    '''
    protocol = P._RequestSessionProtocol


class RequestSessionProtocolTestCase(unittest.TestCase):

    def setUp(self):
        self.transport = StringTransport()
        self.request = DummyRequest([b'ignored'])
        self.request.transport = self.transport

        wrappedFactory = Factory.forProtocol(EchoProtocol)
        self.factory = TestFactoryForRequestSessionProtocol(wrappedFactory)

        self.protocol = self.factory.buildProtocol(self.request.getHost())
        self.protocol.makeConnectionFromRequest(self.request)

    def test_makeConnection_forbidden(self):
        '''
        RequestSessionProtocol.makeConnection raises a RuntimeError.
        '''
        protocol = self.factory.buildProtocol(self.request.getHost())

        with self.assertRaises(RuntimeError):
            protocol.makeConnection(self.transport)

    def test_write(self):
        ''' RequestSessionProtocol.write writes data to the request, not the
        transport.

        '''
        self.protocol.dataReceived(b'something')
        self.assertEqual(self.request.written, [b'something'])
        self.assertFalse(self.transport.value())

    def test_writeSequence(self):
        '''RequestSessionProtocol.writeSequence also writes data to the
        request, not the transport.

        '''
        self.protocol.dataReceived([b'something', b'else'])
        self.assertEqual(self.request.written, [b'something', b'else'])
        self.assertFalse(self.transport.value())

    def test_loseConnection(self):
        '''RequestSessionProtocol.loseConnection finishes the request.'''

        finishedDeferred = self.request.notifyFinish()

        def assertFinished(ignored):
            self.assertEqual(self.request.finished, 1)
            # DummyRequest doesn't call its transport's loseConnection
            self.assertFalse(self.transport.disconnecting)

        finishedDeferred.addCallback(assertFinished)

        self.protocol.dataReceived([b'about to close!'])
        self.protocol.loseConnection()

        self.assertEqual(self.request.written, [b'about to close!'])

        return finishedDeferred

    def test_connectionLost(self):
        '''RequestSessionProtocol.connectionLost calls through to the wrapped
        protocol with Connection Done when there's no pending data.

        '''

        ensureCalled = Deferred()

        def trapConnectionDone(failure):
            failure.trap(ConnectionDone)

        ensureCalled.addErrback(trapConnectionDone)

        def connectionLost(reason):
            ensureCalled.errback(reason)

        self.protocol.wrappedProtocol.connectionLost = connectionLost
        self.protocol.connectionLost()

        return ensureCalled

    def test_detachAndReattach(self):
        '''RequestSessionProtocol.detachFromRequest buffers subsequent writes,
        which are flushed as soon as makeConnectionFromRequest is
        called again.

        '''
        self.protocol.detachFromRequest()
        self.protocol.dataReceived(b'pending')
        self.protocol.dataReceived(b'more pending')
        self.assertFalse(self.request.written)

        self.protocol.makeConnectionFromRequest(self.request)
        self.assertEqual(self.request.written, [b'pending', b'more pending'])

        self.protocol.dataReceived(b'written!')
        self.assertEqual(self.request.written, [b'pending', b'more pending',
                                                b'written!'])

    def test_doubleMakeConnectionFromRequestClosesDuplicate(self):
        '''A RequestSessionProtocol that's attached to a request will close a
        second request with an error message.

        '''
        secondRequest = DummyRequest([b'another'])
        secondTransport = StringTransport()
        secondRequest.transport = secondTransport

        self.protocol.makeConnectionFromRequest(secondRequest)
        self.assertEqual(secondRequest.written,
                         [b'c[2010,"Another connection still open"]'])
        # but we can still write to our first request
        self.protocol.dataReceived(b'hello')
        self.assertEqual(self.request.written, [b'hello'])

    def test_loseConnection_whenDetached(self):
        '''A RequestSessionProtocol with pending data has loseConnection
        called, its connectionLost method receives a SessionTimeout
        failure.
        '''
        self.protocol.detachFromRequest()
        self.protocol.dataReceived(b'pending')
        self.protocol.loseConnection()

        ensureCalled = Deferred()

        def trapSessionTimeout(failure):
            failure.trap(P.SessionTimeout)

        ensureCalled.addErrback(trapSessionTimeout)

        def connectionLost(reason):
            ensureCalled.errback(reason)

        self.protocol.wrappedProtocol.connectionLost = connectionLost
        self.protocol.connectionLost()

        return ensureCalled

    def test_connectionLost_whenDetached(self):
        '''A RequestSessionProtocol with pending data has loseConnection
        called, its connectionLost method receives a SessionTimeout
        failure.
        '''
        self.protocol.detachFromRequest()
        self.protocol.dataReceived(b'pending')

        ensureCalled = Deferred()

        def trapSessionTimeout(failure):
            failure.trap(P.SessionTimeout)

        ensureCalled.addErrback(trapSessionTimeout)

        def connectionLost(reason):
            ensureCalled.errback(reason)

        self.protocol.wrappedProtocol.connectionLost = connectionLost
        self.protocol.connectionLost()

        return ensureCalled


class TimeoutClockTestCase(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.length = 5.0
        self.timedOut = False

        def fakeTerminateSession():
            self.timedOut = True

        self.timeout = P.TimeoutClock(fakeTerminateSession,
                                      length=self.length,
                                      clock=self.clock)

    def test_neverScheduled(self):
        '''Timeouts are not scheduled before their first start(), and are not
        scheduled if we immediately expire() the TimeoutClock.  A stopped
        TimeoutClock can never schedule another timeout.

        '''
        self.assertFalse(self.clock.getDelayedCalls())
        self.timeout.expire()
        self.assertFalse(self.clock.getDelayedCalls())

        with self.assertRaises(KeyError):
            self.timeout.reset()

        self.assertFalse(self.clock.getDelayedCalls())

    def test_start(self):
        '''A timeout expires a connection if not interrupted and then
        stops.

        '''

        self.timeout.start()

        pendingExpiration = self.timeout.timeout
        self.assertFalse(self.timedOut)
        self.assertEqual(self.clock.getDelayedCalls(), [pendingExpiration])

        self.clock.advance(self.length * 2)
        self.assertTrue(self.timedOut)

        self.assertIsNone(self.timeout.timeout)

        with self.assertRaises(KeyError):
            self.timeout.start()

        with self.assertRaises(KeyError):
            self.timeout.reset()

    def test_reset_interrupts(self):
        '''A reset() call will remove the pending timeout and reschedule it
        for later.

        '''
        self.timeout.start()

        pendingExpiration = self.timeout.timeout
        self.assertEqual(self.clock.getDelayedCalls(), [pendingExpiration])

        self.timeout.reset()

        resetPendingExecution = self.timeout.timeout
        self.assertIsNot(pendingExpiration, resetPendingExecution)
        self.assertEqual(self.clock.getDelayedCalls(), [])
