"""Optional audit sinks: forward every audit event to syslog and/or an HTTP collector.

The JSON Lines file written by :mod:`audit` stays the source of truth. Sinks are a convenience for
installs without a log shipper (a homelab syslog server, Graylog, Wazuh) or with an HTTPS-only
collector (Splunk HEC and similar). Delivery rules:

* Events are queued and sent from one daemon thread. Emitting never blocks a NetBox operation.
* A sink failure is retried a few times with a short backoff, then the event is dropped for that
  sink and the failure is logged once per sink per few minutes, not per event.
* The queue is bounded; when it is full, events are dropped for the sinks (never for the file).

Standard library only. Configuration lives in ``plugins.entries.netbox.settings.audit_sinks``::

    audit_sinks:
      - type: syslog
        host: 10.0.0.5
        port: 514
        protocol: udp          # udp | tcp | tls
        format: json           # json | cef
        facility: local0
      - type: http
        url: https://splunk.example.com:8088/services/collector/event
        headers: {Authorization: "Splunk ${SPLUNK_HEC_TOKEN}"}   # ${VAR} is read from the environment
        format: hec            # json | hec

Secrets belong in the environment (``~/.hermes/.env``); ``${VAR}`` placeholders in header values are
resolved at send time so the token never sits in ``config.yaml``.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

from .version import __version__

logger = logging.getLogger(__name__)

QUEUE_SIZE = 1000
RETRIES = 3
RETRY_BACKOFF = 0.25
ERROR_LOG_INTERVAL = 300.0

FACILITIES = {
    "kern": 0,
    "user": 1,
    "mail": 2,
    "daemon": 3,
    "auth": 4,
    "syslog": 5,
    "lpr": 6,
    "news": 7,
    "uucp": 8,
    "cron": 9,
    "authpriv": 10,
    "ftp": 11,
    "local0": 16,
    "local1": 17,
    "local2": 18,
    "local3": 19,
    "local4": 20,
    "local5": 21,
    "local6": 22,
    "local7": 23,
}
SEV_WARNING, SEV_NOTICE, SEV_INFO = 4, 5, 6

_WARNING_EVENTS = {
    "plan_rejected",
    "apply_refused",
    "rollback_refused",
    "step_failed",
    "step_conflict",
    "revert_failed",
    "revert_conflict",
}
_NOTICE_EVENTS = {"apply_started", "rollback_started", "plans_pruned"}


def severity_of(record: Dict[str, Any]) -> int:
    """Syslog severity for an audit record: warning for anything that stopped or was refused."""
    event = str(record.get("event", ""))
    if event in _WARNING_EVENTS:
        return SEV_WARNING
    if event == "apply_finished" and record.get("outcome") == "failed":
        return SEV_WARNING
    if event == "rollback_finished" and record.get("status") != "rolled_back":
        return SEV_WARNING
    if event == "changelog_linked" and record.get("linked") is False:
        return SEV_WARNING
    if event in _NOTICE_EVENTS:
        return SEV_NOTICE
    return SEV_INFO


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


# -- CEF ------------------------------------------------------------------------------------------


def _cef_header(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _cef_ext(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text.replace("\\", "\\\\").replace("=", "\\=").replace("\r", " ").replace("\n", "\\n")


def to_cef(record: Dict[str, Any]) -> str:
    """ArcSight Common Event Format line. Standard keys where they fit, ``csN`` pairs for the rest."""
    actor = record.get("actor") or {}
    sev = {SEV_WARNING: 7, SEV_NOTICE: 4, SEV_INFO: 2}[severity_of(record)]
    stamp = record.get("ts")
    try:
        rt = int(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        rt = int(time.time() * 1000)
    ext: List[tuple] = [("rt", rt), ("dvchost", record.get("host")), ("dvcpid", record.get("pid"))]
    if record.get("netbox_url"):
        ext.append(("request", record["netbox_url"]))
    if actor.get("user_name") or actor.get("user_id"):
        ext.append(("suser", actor.get("user_name") or actor.get("user_id")))
    ext += [
        ("cs1Label", "plan_id"),
        ("cs1", record.get("plan_id")),
        ("cs2Label", "actor_kind"),
        ("cs2", actor.get("kind")),
        ("cs3Label", "via"),
        ("cs3", actor.get("via")),
        ("cs4Label", "platform"),
        ("cs4", actor.get("platform") or actor.get("source")),
        ("cs5Label", "chat"),
        ("cs5", actor.get("chat_name") or actor.get("chat_id")),
        ("cs6Label", "endpoint"),
        ("cs6", record.get("endpoint")),
    ]
    if record.get("action"):
        ext.append(("act", record["action"]))
    if record.get("object_id") is not None:
        ext.append(("externalId", record["object_id"]))
    if record.get("http_status") is not None:
        ext += [("cn1Label", "http_status"), ("cn1", record["http_status"])]
    if record.get("outcome") or record.get("status"):
        ext.append(("outcome", record.get("outcome") or record.get("status")))
    msg = record.get("error") or record.get("reason") or record.get("description")
    if msg:
        ext.append(("msg", msg))
    if actor.get("request"):
        ext += [("cs7Label", "request_text"), ("cs7", actor["request"])]
    extension = " ".join(f"{k}={_cef_ext(v)}" for k, v in ext if v not in (None, ""))
    name = str(record.get("event", "")).replace("_", " ")
    return "|".join(
        [
            "CEF:0",
            "andrewvieyra",
            "hermes-plugin-netbox",
            _cef_header(__version__),
            _cef_header(record.get("event", "")),
            _cef_header(name),
            str(sev),
            extension,
        ]
    )


# -- sinks ----------------------------------------------------------------------------------------


class Sink:
    name = "sink"

    def send(self, record: Dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None


class SyslogSink(Sink):
    """RFC 5424 over UDP, TCP (newline or octet-counted framing) or TLS (octet-counted, RFC 5425)."""

    def __init__(
        self,
        host: str,
        port: int = 514,
        *,
        protocol: str = "udp",
        fmt: str = "json",
        facility: str = "local0",
        app_name: str = "hermes-netbox",
        framing: str | None = None,
        ca_file: str | None = None,
        verify_ssl: bool = True,
        timeout: float = 5.0,
    ):
        self.host, self.port = host, int(port)
        self.protocol = protocol.lower()
        if self.protocol not in {"udp", "tcp", "tls"}:
            raise ValueError(f"syslog protocol must be udp, tcp or tls (got {protocol!r})")
        self.fmt = fmt.lower()
        if self.fmt not in {"json", "cef"}:
            raise ValueError(f"syslog format must be json or cef (got {fmt!r})")
        self.facility = FACILITIES[facility.lower()] if isinstance(facility, str) else int(facility)
        self.app_name = app_name
        self.framing = framing or ("octet-counting" if self.protocol == "tls" else "newline")
        self.ca_file, self.verify_ssl, self.timeout = ca_file, verify_ssl, timeout
        self.name = f"syslog {self.protocol}://{self.host}:{self.port}"
        self._sock: socket.socket | None = None
        try:
            self.hostname = socket.gethostname() or "-"
        except Exception:
            self.hostname = "-"

    def message(self, record: Dict[str, Any]) -> str:
        pri = self.facility * 8 + severity_of(record)
        stamp = record.get("ts") or datetime.now(timezone.utc).isoformat()
        body = (
            to_cef(record)
            if self.fmt == "cef"
            else json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
        )
        msgid = str(record.get("event") or "-")[:32] or "-"
        return f"<{pri}>1 {stamp} {self.hostname} {self.app_name} {record.get('pid') or '-'} {msgid} - {body}"

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        if self.protocol == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            if self.protocol == "tls":
                context = ssl.create_default_context(cafile=self.ca_file)
                if not self.verify_ssl:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(sock, server_hostname=self.host)
        self._sock = sock
        return sock

    def send(self, record: Dict[str, Any]) -> None:
        payload = self.message(record).encode("utf-8")
        try:
            sock = self._connect()
            if self.protocol == "udp":
                sock.sendto(payload, (self.host, self.port))
            else:
                frame = f"{len(payload)} ".encode() + payload if self.framing == "octet-counting" else payload + b"\n"
                sock.sendall(frame)
        except Exception:
            self.close()  # reconnect on the next attempt
            raise

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


class HttpSink(Sink):
    """POST each record as JSON (``format: json``) or wrapped for Splunk HEC (``format: hec``)."""

    def __init__(
        self,
        url: str,
        *,
        headers: Dict[str, str] | None = None,
        fmt: str = "json",
        timeout: float = 5.0,
        verify_ssl: bool = True,
        sourcetype: str = "hermes:netbox",
    ):
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"http sink url must start with http:// or https:// (got {url!r})")
        self.url = url
        self.headers = dict(headers or {})
        self.fmt = fmt.lower()
        if self.fmt not in {"json", "hec"}:
            raise ValueError(f"http format must be json or hec (got {fmt!r})")
        self.timeout, self.verify_ssl, self.sourcetype = timeout, verify_ssl, sourcetype
        self.name = f"http {url}"

    def body(self, record: Dict[str, Any]) -> bytes:
        if self.fmt == "hec":
            try:
                epoch = datetime.fromisoformat(str(record.get("ts")).replace("Z", "+00:00")).timestamp()
            except Exception:
                epoch = time.time()
            payload: Any = {"time": epoch, "sourcetype": self.sourcetype, "host": record.get("host"), "event": record}
        else:
            payload = record
        return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    def send(self, record: Dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json", "User-Agent": f"hermes-plugin-netbox/{__version__}"}
        headers.update({k: expand_env(str(v)) for k, v in self.headers.items()})
        req = urllib.request.Request(self.url, data=self.body(record), headers=headers, method="POST")
        context = None
        if self.url.startswith("https://") and not self.verify_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=self.timeout, context=context) as resp:
            if resp.status >= 300:
                raise OSError(f"HTTP {resp.status}")


def build_sink(spec: Dict[str, Any]) -> Sink | None:
    """One sink from its config mapping; ``None`` (with a warning) for an unusable spec."""
    if not isinstance(spec, dict):
        logger.warning("netbox audit_sinks: entry is not a mapping: %r", spec)
        return None
    kind = str(spec.get("type") or "").lower()
    try:
        if kind == "syslog":
            if not spec.get("host"):
                raise ValueError("syslog sink needs a host")
            return SyslogSink(
                str(spec["host"]),
                int(spec.get("port", 514)),
                protocol=str(spec.get("protocol", "udp")),
                fmt=str(spec.get("format", "json")),
                facility=spec.get("facility", "local0"),
                app_name=str(spec.get("app_name", "hermes-netbox")),
                framing=spec.get("framing"),
                ca_file=spec.get("ca_file"),
                verify_ssl=bool(spec.get("verify_ssl", True)),
                timeout=float(spec.get("timeout", 5.0)),
            )
        if kind == "http":
            return HttpSink(
                str(spec.get("url") or ""),
                headers=spec.get("headers") or {},
                fmt=str(spec.get("format", "json")),
                timeout=float(spec.get("timeout", 5.0)),
                verify_ssl=bool(spec.get("verify_ssl", True)),
                sourcetype=str(spec.get("sourcetype", "hermes:netbox")),
            )
        raise ValueError(f"unknown sink type {kind!r} (expected syslog or http)")
    except Exception as exc:
        logger.warning("netbox audit_sinks: ignoring entry %r: %s", spec, exc)
        return None


# -- worker ---------------------------------------------------------------------------------------


class SinkWorker:
    """Single background thread draining a bounded queue into every sink."""

    def __init__(
        self, sinks: List[Sink], *, queue_size: int = QUEUE_SIZE, retries: int = RETRIES, backoff: float = RETRY_BACKOFF
    ):
        self.sinks = list(sinks)
        self.retries, self.backoff = retries, backoff
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.stats = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0}
        self._last_error_log: Dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="netbox-audit-sinks", daemon=True)
        if self.sinks:
            self._thread.start()

    def enqueue(self, record: Dict[str, Any]) -> bool:
        if not self.sinks:
            return False
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.stats["dropped"] += 1
            self._log_once("queue", "audit sink queue is full; dropping events for sinks (file is unaffected)")
            return False
        self.stats["queued"] += 1
        return True

    def _deliver(self, sink: Sink, record: Dict[str, Any]) -> None:
        for attempt in range(1, self.retries + 1):
            try:
                sink.send(record)
                self.stats["sent"] += 1
                return
            except Exception as exc:
                if attempt == self.retries:
                    self.stats["failed"] += 1
                    self._log_once(sink.name, f"audit sink {sink.name} failed after {attempt} attempts: {exc}")
                    return
                time.sleep(self.backoff * attempt)

    def _log_once(self, key: str, message: str) -> None:
        now = time.monotonic()
        if now - self._last_error_log.get(key, -ERROR_LOG_INTERVAL) >= ERROR_LOG_INTERVAL:
            self._last_error_log[key] = now
            logger.warning("netbox %s", message)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                record = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                for sink in self.sinks:
                    self._deliver(sink, record)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued events are delivered (or the timeout passes). Returns True when drained."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        for sink in self.sinks:
            with contextlib.suppress(Exception):
                sink.close()


_worker: SinkWorker | None = None
_worker_lock = threading.Lock()


def configure(specs: Any) -> SinkWorker:
    """Build the process-wide worker from the ``audit_sinks`` setting."""
    sinks = [s for s in (build_sink(spec) for spec in (specs or [])) if s is not None]
    return SinkWorker(sinks)


def get_worker() -> SinkWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            from .settings import get_settings

            _worker = configure(get_settings().audit_sinks)
            atexit.register(_worker.close, 2.0)
        return _worker


def set_worker(worker: SinkWorker | None) -> None:
    """Test seam / settings reload: replace (and close) the process-wide worker."""
    global _worker
    with _worker_lock:
        old, _worker = _worker, worker
    if old is not None and old is not worker:
        old.close(1.0)


def dispatch(record: Dict[str, Any]) -> None:
    """Hand a record to the sinks; never raises."""
    try:
        get_worker().enqueue(record)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("netbox audit sink dispatch failed: %s", exc)
