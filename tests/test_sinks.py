import http.server
import json
import os
import socket
import socketserver
import threading
import time
import unittest

from .base import PluginTestCase
from .helpers import submodule

sinks = submodule("sinks")
version = submodule("version")


def _record(event="apply_started", **extra):
    rec = {
        "ts": "2026-09-08T19:35:50Z",
        "schema": 1,
        "plugin": "netbox",
        "event": event,
        "plan_id": "nbp-20260908T193512Z-4f1a",
        "netbox_url": "https://netbox.example.com",
        "actor": {
            "kind": "model",
            "via": "tool",
            "platform": "signal",
            "user_name": "Andrew",
            "chat_type": "dm",
            "chat_name": "Andrew",
            "request": "Move sw1 | to SFO",
        },
        "host": "hermes-01",
        "pid": 4242,
    }
    rec.update(extra)
    return rec


class _UDP(socketserver.UDPServer):
    allow_reuse_address = True


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.received.append(self.request[0])


class _TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TCPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            self.server.received.append(line.rstrip(b"\n"))


class _HTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"text":"Success","code":0}')

    def log_message(self, *args):  # silence
        return None


def _serve(server):
    server.received = []
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    return server


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Formats(unittest.TestCase):
    def test_severity_mapping(self):
        self.assertEqual(sinks.severity_of(_record("step_done")), sinks.SEV_INFO)
        self.assertEqual(sinks.severity_of(_record("apply_started")), sinks.SEV_NOTICE)
        self.assertEqual(sinks.severity_of(_record("apply_refused")), sinks.SEV_WARNING)
        self.assertEqual(sinks.severity_of(_record("apply_finished", outcome="failed")), sinks.SEV_WARNING)
        self.assertEqual(sinks.severity_of(_record("apply_finished", outcome="applied")), sinks.SEV_INFO)
        self.assertEqual(
            sinks.severity_of(_record("rollback_finished", status="partially_rolled_back")), sinks.SEV_WARNING
        )
        self.assertEqual(sinks.severity_of(_record("changelog_linked", linked=False)), sinks.SEV_WARNING)

    def test_rfc5424_message_shape(self):
        sink = sinks.SyslogSink("127.0.0.1", 514, facility="local0", app_name="hermes-netbox")
        msg = sink.message(_record("step_done"))
        self.assertTrue(msg.startswith("<134>1 2026-09-08T19:35:50Z "), msg)  # local0(16)*8 + info(6)
        head, body = msg.split(" - ", 1)
        self.assertEqual(head.split(" ")[3:6], ["hermes-netbox", "4242", "step_done"])
        self.assertEqual(json.loads(body)["event"], "step_done")
        self.assertTrue(sink.message(_record("apply_refused")).startswith("<132>1 "))

    def test_cef_line(self):
        line = sinks.to_cef(
            _record(
                "apply_refused",
                reason="write_mode is read_only",
                endpoint="dcim/devices",
                object_id=10,
                http_status=403,
                action="update",
            )
        )
        self.assertTrue(
            line.startswith(
                f"CEF:0|andrewvieyra|hermes-plugin-netbox|{version.__version__}|apply_refused|apply refused|7|"
            )
        )
        ext = line.split("|", 7)[7]
        for needle in (
            "rt=1788896150000",
            "dvchost=hermes-01",
            "suser=Andrew",
            "cs1Label=plan_id cs1=nbp-20260908T193512Z-4f1a",
            "cs2Label=actor_kind cs2=model",
            "cs4Label=platform cs4=signal",
            "cs6Label=endpoint cs6=dcim/devices",
            "act=update",
            "externalId=10",
            "cn1Label=http_status cn1=403",
            "msg=write_mode is read_only",
            "cs7Label=request_text cs7=Move sw1 | to SFO",
        ):
            self.assertIn(needle, ext, needle)
        pipe = sinks.to_cef(_record("x|y"))
        self.assertIn("|x\\|y|", pipe)
        self.assertEqual(sinks._cef_ext("a=b\\c\nd"), "a\\=b\\\\c\\nd")

    def test_env_expansion_and_validation(self):
        os.environ["NB_TEST_TOKEN"] = "s3cret"
        try:
            self.assertEqual(sinks.expand_env("Splunk ${NB_TEST_TOKEN}"), "Splunk s3cret")
            self.assertEqual(sinks.expand_env("${NB_MISSING_VAR}"), "")
        finally:
            del os.environ["NB_TEST_TOKEN"]
        self.assertIsNone(sinks.build_sink({"type": "syslog"}))  # no host
        self.assertIsNone(sinks.build_sink({"type": "carrier-pigeon", "host": "x"}))
        self.assertIsNone(sinks.build_sink({"type": "http", "url": "ftp://x"}))
        self.assertIsNone(sinks.build_sink("not a mapping"))
        self.assertIsNone(sinks.build_sink({"type": "syslog", "host": "x", "protocol": "smoke"}))
        good = sinks.build_sink(
            {"type": "syslog", "host": "10.0.0.5", "protocol": "tcp", "format": "cef", "facility": "local3"}
        )
        self.assertEqual((good.protocol, good.fmt, good.facility, good.framing), ("tcp", "cef", 19, "newline"))
        tls = sinks.build_sink({"type": "syslog", "host": "10.0.0.5", "protocol": "tls"})
        self.assertEqual(tls.framing, "octet-counting")
        worker = sinks.configure([{"type": "syslog", "host": "10.0.0.5"}, {"type": "bogus"}])
        self.assertEqual(len(worker.sinks), 1)
        worker.close(0.5)


class Delivery(unittest.TestCase):
    def test_udp_syslog(self):
        server = _serve(_UDP(("127.0.0.1", 0), _UDPHandler))
        try:
            worker = sinks.SinkWorker([sinks.SyslogSink("127.0.0.1", server.server_address[1])])
            worker.enqueue(_record("plan_created"))
            worker.enqueue(_record("apply_refused"))
            self.assertTrue(worker.flush(5))
            self.assertTrue(_wait(lambda: len(server.received) == 2))
            first = server.received[0].decode()
            self.assertTrue(first.startswith("<134>1 "))
            self.assertEqual(json.loads(first.split(" - ", 1)[1])["event"], "plan_created")
            self.assertTrue(server.received[1].decode().startswith("<132>1 "))
            self.assertEqual(worker.stats["sent"], 2)
            worker.close(1)
        finally:
            server.shutdown()
            server.server_close()

    def test_tcp_syslog_newline_and_cef(self):
        server = _serve(_TCP(("127.0.0.1", 0), _TCPHandler))
        try:
            sink = sinks.SyslogSink("127.0.0.1", server.server_address[1], protocol="tcp", fmt="cef")
            worker = sinks.SinkWorker([sink])
            worker.enqueue(
                _record("step_done", endpoint="dcim/devices", object_id=10, action="update", http_status=200)
            )
            self.assertTrue(worker.flush(5))
            self.assertTrue(_wait(lambda: len(server.received) == 1))
            line = server.received[0].decode()
            self.assertTrue(line.startswith("<134>1 "))
            self.assertIn(" step_done - CEF:0|andrewvieyra|hermes-plugin-netbox|", line)
            worker.close(1)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_sink_json_and_hec_with_env_header(self):
        server = _serve(http.server.HTTPServer(("127.0.0.1", 0), _HTTPHandler))
        os.environ["NB_TEST_HEC"] = "tok-123"
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/services/collector/event"
            plain = sinks.HttpSink(url, headers={"Authorization": "Splunk ${NB_TEST_HEC}"})
            hec = sinks.HttpSink(url, headers={"Authorization": "Splunk ${NB_TEST_HEC}"}, fmt="hec")
            worker = sinks.SinkWorker([plain, hec])
            worker.enqueue(_record("apply_finished", outcome="applied"))
            self.assertTrue(worker.flush(5))
            self.assertTrue(_wait(lambda: len(server.received) == 2))
            a, b = server.received
            self.assertEqual(a["headers"]["Authorization"], "Splunk tok-123")
            self.assertEqual(json.loads(a["body"])["event"], "apply_finished")
            wrapped = json.loads(b["body"])
            self.assertEqual(
                (wrapped["sourcetype"], wrapped["event"]["event"], wrapped["host"]),
                ("hermes:netbox", "apply_finished", "hermes-01"),
            )
            self.assertAlmostEqual(wrapped["time"], 1788896150.0, places=0)
            self.assertIn("hermes-plugin-netbox/", a["headers"]["User-Agent"])
            worker.close(1)
        finally:
            del os.environ["NB_TEST_HEC"]
            server.shutdown()
            server.server_close()

    def test_unreachable_sink_is_retried_then_dropped_quickly(self):
        dead = sinks.HttpSink(f"http://127.0.0.1:{_free_port()}/collector", timeout=0.5)
        worker = sinks.SinkWorker([dead], retries=2, backoff=0.01)
        started = time.monotonic()
        worker.enqueue(_record())
        self.assertTrue(worker.flush(10))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual((worker.stats["sent"], worker.stats["failed"]), (0, 1))
        worker.close(1)

    def test_queue_overflow_drops_for_sinks_only(self):
        class Slow(sinks.Sink):
            name = "slow"

            def send(self, record):
                time.sleep(0.2)

        worker = sinks.SinkWorker([Slow()], queue_size=2)
        results = [worker.enqueue(_record()) for _ in range(6)]
        self.assertIn(False, results)
        self.assertGreaterEqual(worker.stats["dropped"], 1)
        worker.close(3)

    def test_no_sinks_means_no_thread(self):
        worker = sinks.SinkWorker([])
        self.assertFalse(worker.enqueue(_record()))
        self.assertFalse(worker._thread.is_alive())


class ThroughAuditLog(PluginTestCase):
    def test_events_from_a_real_apply_reach_syslog_and_the_file(self):
        server = _serve(_UDP(("127.0.0.1", 0), _UDPHandler))
        try:
            self.sinks.set_worker(self.sinks.SinkWorker([self.sinks.SyslogSink("127.0.0.1", server.server_address[1])]))
            plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
            self.executor.apply_plan(self.client, plan, self.store, self.settings)
            self.assertTrue(self.sinks.get_worker().flush(5))
            file_events = [e["event"] for e in self.events()]
            _wait(lambda: len(server.received) >= len(file_events))
            wire_events = [json.loads(d.decode().split(" - ", 1)[1])["event"] for d in server.received]
            self.assertEqual(wire_events, file_events, (wire_events, file_events, self.sinks.get_worker().stats))
            self.assertIn("apply_finished", wire_events)
        finally:
            server.shutdown()
            server.server_close()

    def test_worker_built_from_settings(self):
        self.sinks.set_worker(None)
        self.settings_mod.set_settings(
            self.settings_mod.Settings(audit_sinks=[{"type": "syslog", "host": "127.0.0.1", "port": 1}])
        )
        worker = self.sinks.get_worker()
        self.assertEqual(len(worker.sinks), 1)
        self.assertEqual(worker.sinks[0].name, "syslog udp://127.0.0.1:1")

    def test_settings_coercion_keeps_only_mappings(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"audit_sinks": [{"type": "http", "url": "https://x"}, "junk", 3]}.get(key, default)

        self.assertEqual(self.settings_mod.Settings.from_ctx(Ctx()).audit_sinks, [{"type": "http", "url": "https://x"}])


if __name__ == "__main__":
    unittest.main()
