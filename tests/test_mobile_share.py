"""LAN answer sharing: pairing, read-only access and missed-answer replay."""

from __future__ import annotations

import http.client
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app as app_module
from mobile_share import MobileShare
from settings import AppParameters


def answer(number: int, record_id: int, replaced: list[int] | None = None) -> dict:
    return {
        "id": number,
        "record_id": record_id,
        "time": "10:00:00",
        "question": f"问题 {number}",
        "answer": f"回答 {number}",
        "superseded": False,
        "replaced_record_ids": replaced or [],
    }


class MobileShareServerTests(unittest.TestCase):
    def test_pairing_history_live_answer_and_reconnect_replay(self) -> None:
        share = MobileShare()
        port = share.start([answer(1, 1)])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/api/answers")
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/pair?key=wrong")
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", f"/pair?key={share.token}")
            paired = connection.getresponse()
            self.assertEqual(paired.status, 303)
            cookie = paired.getheader("Set-Cookie").split(";", 1)[0]
            self.assertIn("HttpOnly", paired.getheader("Set-Cookie"))
            paired.read()
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/answers", headers={"Cookie": cookie})
            snapshot = json.loads(connection.getresponse().read())
            self.assertEqual(snapshot["answers"], [answer(1, 1)])
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/events?after=1", headers={"Cookie": cookie})
            stream = connection.getresponse()
            self.assertEqual(stream.status, 200)
            share.publish(answer(2, 2, [1]))
            self.assertEqual(stream.readline().decode().strip(), "id: 2")
            message = stream.readline().decode()
            self.assertEqual(json.loads(message[6:]), answer(2, 2, [1]))
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/answers", headers={"Cookie": cookie})
            corrected = json.loads(connection.getresponse().read())["answers"]
            self.assertTrue(corrected[0]["superseded"])
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/events?after=1", headers={"Cookie": cookie})
            replay = connection.getresponse()
            self.assertEqual(replay.readline().decode().strip(), "id: 2")
            connection.close()
        finally:
            share.stop()

    def test_restart_invalidates_old_pairing(self) -> None:
        share = MobileShare()
        first_port = share.start([])
        old_token = share.token
        share.stop()
        second_port = share.start([])
        try:
            self.assertNotEqual(old_token, share.token)
            connection = http.client.HTTPConnection("127.0.0.1", second_port, timeout=3)
            connection.request("GET", "/api/answers", headers={"Cookie": f"mobile_share={old_token}"})
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()
        finally:
            share.stop()


class MobileShareAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_answer_event_is_published_and_old_answer_marked(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module, "load_key", return_value=None),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
        ):
            window = app_module.ClassroomApp()
        try:
            settings = AppParameters()
            for record_id, parent_id in ((1, None), (2, 1)):
                window.events.put(("record_new", (record_id, datetime.now(), time.monotonic(), "手动", "问题", None, settings, None, parent_id)))
                window.events.put(("answer", (record_id, "问题", f"回答{record_id}", 0.01)))
            window._poll_events()
            self.assertEqual(len(window.mobile_answer_entries), 2)
            self.assertTrue(window.mobile_answer_entries[0]["superseded"])
            self.assertEqual(window.mobile_answer_entries[1]["replaced_record_ids"], [1])
            window.mobile_share.start(window.mobile_answer_entries)
            self.assertEqual(window.mobile_share.snapshot()[1]["answer"], "回答2")
        finally:
            window.close()

    def test_share_button_shows_qr_and_stops_service(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module, "load_key", return_value=None),
            patch.object(app_module, "lan_ipv4_addresses", return_value=["127.0.0.1"]),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
        ):
            window = app_module.ClassroomApp()
            try:
                window.share_button.click()
                self.assertTrue(window.mobile_share.active)
                self.assertIsNotNone(window.share_dialog)
                self.assertEqual(window.share_button.text(), "停止手机共享")
                window.share_button.click()
                self.assertFalse(window.mobile_share.active)
                self.assertEqual(window.share_button.text(), "开启手机共享")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
