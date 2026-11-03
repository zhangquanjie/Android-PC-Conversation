import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from intercom import Intercom


class Channel:
    def __init__(self):
        self.polls = 0
        self.closed = False

    def exec_command(self, script):
        self.script = script

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        self.polls += 1
        return self.polls > 1

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


class Client:
    def __init__(self, channel):
        self.channel = channel

    def get_transport(self):
        return self

    def open_session(self, timeout):
        return self.channel


class LifecycleTests(unittest.TestCase):
    def test_cleanup_can_finish_after_stop_requested(self):
        channel = Channel()
        engine = Intercom(lambda _: None)
        engine.client = Client(channel)
        engine.stop.set()
        self.assertEqual(engine.remote('cleanup', cancel=False), '')
        self.assertTrue(channel.closed)

    def test_normal_work_is_cancelled_and_channel_closed(self):
        channel = Channel()
        engine = Intercom(lambda _: None)
        engine.client = Client(channel)
        engine.stop.set()
        with self.assertRaisesRegex(RuntimeError, '取消'):
            engine.remote('work', cancel=True)
        self.assertTrue(channel.closed)


if __name__ == '__main__':
    unittest.main()
