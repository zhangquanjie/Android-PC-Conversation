import sys
import threading
import unittest
import tempfile
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
    def test_file_jobs_accept_unicode_spaces_and_uppercase_extension(self):
        engine = Intercom(lambda _: None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '测试 audio.MP3'
            path.write_bytes(b'audio')
            engine.request_file(path)
            kind, name, voice = engine.text_jobs.get_nowait()
            self.assertEqual(kind, 'file')
            self.assertEqual(Path(name), path.resolve())

    def test_stop_clears_pending_job_and_new_voice_job_can_start(self):
        engine = Intercom(lambda _: None)
        engine.request_text('Hello', 'Microsoft Zira Desktop')
        engine.stop_playback()
        self.assertTrue(engine.text_jobs.empty())
        self.assertTrue(engine.cancel_token.is_set())
        engine.request_text('你好', 'Microsoft Huihui Desktop')
        self.assertFalse(engine.cancel_token.is_set())
        self.assertEqual(engine.text_jobs.get_nowait(), ('text', '你好', 'Microsoft Huihui Desktop'))

    def test_invalid_file_is_rejected(self):
        engine = Intercom(lambda _: None)
        with self.assertRaises(RuntimeError):
            engine.request_file('does-not-exist.mp3')

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
