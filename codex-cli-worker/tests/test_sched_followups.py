"""Timezone refresh must not block worker actions or outlive turn authority."""
import threading
import unittest
from unittest.mock import MagicMock, patch

import test_scheduling as fixtures
from test_server import server

REAL_TIMEZONE = server.schedule_timezone


class FollowupTests(unittest.TestCase):
    setUp = fixtures.SchedulingApiTests.setUp

    def exchange(self):
        task='timezone-fixture'
        server.tasks[task]={'status':'running','current_turn_id':'synthetic-turn'}
        cap=server.ha_data.begin(task); self.addCleanup(lambda:server.ha_data.end(task))
        return task, {'operation':'post_mailbox','arguments':{'body':'Synthetic mailbox message.'},'capability':cap}

    def test_n1_slow_failed_ha_refresh_leaves_worker_lock_available_and_is_cached(self):
        task,payload=self.exchange(); entered=threading.Event(); release=threading.Event(); outcomes=[]
        response=MagicMock(); response.__enter__.return_value.status_code=503
        client=MagicMock()
        def unavailable(*args,**kwargs):
            entered.set()
            if not release.wait(2):raise RuntimeError('Test release timed out')
            return response
        client.get.side_effect=unavailable
        def dispatch():
            try:outcomes.append(self.service.dispatch(payload))
            except Exception as exc:outcomes.append(exc)
        with patch.object(server,'schedule_timezone',side_effect=REAL_TIMEZONE), \
             patch.dict(server._schedule_timezone_cache,{'name':None,'until':0}), \
             patch.object(server,'ha_token',return_value='synthetic-supervisor'), \
             patch.object(server.requests,'Session') as session:
            session.return_value.__enter__.return_value=client
            thread=threading.Thread(target=dispatch)
            thread.start()
            try:
                self.assertTrue(entered.wait(1),'Timezone refresh never started')
                available=server.lock.acquire(timeout=.2)
                if available:server.lock.release()
            finally:
                release.set();thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(available,'A failed HA request held the global worker lock')
            self.assertIsInstance(outcomes[0],dict)
            self.assertTrue(self.service.dispatch(payload)['ok'])
            self.assertEqual(client.get.call_count,1,'Unavailable timezone was not cached')
            self.assertIsNone(self.service.engine().store.timezone)

    def test_n1_expired_exchange_cannot_write_after_slow_timezone_refresh(self):
        task,payload=self.exchange();entered=threading.Event();release=threading.Event();outcomes=[]
        def refresh():
            entered.set()
            if not release.wait(2):raise RuntimeError('Test release timed out')
            return 'UTC'
        def dispatch():
            try:outcomes.append(self.service.dispatch(payload))
            except Exception as exc:outcomes.append(exc)
        with patch.object(server,'schedule_timezone',side_effect=refresh):
            thread=threading.Thread(target=dispatch);thread.start()
            try:
                self.assertTrue(entered.wait(1))
                server.ha_data.end(task)
            finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcomes[0],ValueError)
        self.assertEqual(self.service.engine().store.messages(),[])
