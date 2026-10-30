import asyncio
from email import policy
from email.parser import BytesParser
import smtplib
import socketserver
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.domains.alerts import notify
from app.domains.notifications import mail as digest_mail
from app.domains.notifications.service import retryable


_SMTP_SSL = smtplib.SMTP_SSL
_SMTP = smtplib.SMTP


class _SinkHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost test sink\r\n")
        recipient = ""
        while command := self.rfile.readline():
            verb = command.split(b" ", 1)[0].strip().upper()
            if verb == b"EHLO":
                self.wfile.write(b"250-localhost\r\n250 AUTH PLAIN\r\n")
            elif verb == b"AUTH":
                self.wfile.write(b"235 authenticated\r\n")
            elif verb == b"MAIL":
                self.wfile.write(b"250 sender accepted\r\n")
            elif verb == b"RCPT":
                recipient = command.decode().strip()
                self.wfile.write(b"451 temporary rejection\r\n" if self.server.mode == "rcpt451"
                                 else b"250 recipient accepted\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 send message\r\n")
                lines = []
                while (line := self.rfile.readline()) and line != b".\r\n":
                    lines.append(line)
                self.server.receipts.append({"recipient": recipient, "body": b"".join(lines)})
                self.server.received.set()
                if self.server.mode == "lost_reply":
                    return
                if self.server.mode == "delay":
                    time.sleep(0.5)
                if self.server.mode == "data451":
                    self.wfile.write(b"451 local failure reference 535\r\n")
                elif self.server.mode == "data550":
                    self.wfile.write(b"550 permanent failure reference 451\r\n")
                else:
                    self.wfile.write(b"250 accepted\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"421 closing failure\r\n" if self.server.mode == "quit421"
                                 else b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


class _TLSSink(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        sock, address = super().get_request()
        return self.tls.wrap_socket(sock, server_side=True), address


@pytest.fixture
def smtp_sink(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "sink.pem", tmp_path / "sink.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    sink = _TLSSink(("127.0.0.1", 0), _SinkHandler)
    sink.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sink.tls.load_cert_chain(cert_path, key_path)
    sink.mode, sink.receipts, sink.received = "normal", [], threading.Event()
    worker = threading.Thread(target=sink.serve_forever, daemon=True)
    worker.start()

    async def config():
        return dict(host="127.0.0.1", port=sink.server_address[1],
                    user="sender@example.invalid", password="local-only",
                    to_addr="old@example.invalid", use_ssl=True)

    monkeypatch.setattr(notify, "smtp_config", config)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _SMTP_SSL)
    monkeypatch.setattr(notify.smtplib, "SMTP", _SMTP)
    try:
        yield sink
    finally:
        sink.shutdown()
        sink.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, success, unknown", [
    ("normal", True, False), ("quit421", True, False), ("lost_reply", False, True),
    ("rcpt451", False, False), ("data451", False, False), ("data550", False, False),
])
async def test_actual_tls_receipt_boundary(smtp_sink, mode, success, unknown):
    smtp_sink.mode = mode
    ok, error = await notify.send_mail_ex("local test", "<p>local test</p>",
                                         recipient="frozen@example.invalid")
    assert ok is success
    assert notify.is_delivery_unknown(error) is unknown
    if mode == "rcpt451":
        assert smtp_sink.receipts == []
        assert retryable(error)
    else:
        assert len(smtp_sink.receipts) == 1
        assert smtp_sink.receipts[0]["recipient"] == "rcpt to:<frozen@example.invalid>"
        message = BytesParser(policy=policy.default).parsebytes(smtp_sink.receipts[0]["body"])
        assert message.get_content_type() == "multipart/alternative"
        assert message.get_body(preferencelist=("plain",)).get_content().strip() == "local test"
        assert message.get_body(preferencelist=("html",)).get_content().strip() == "<p>local test</p>"
        assert retryable(error) is (mode == "data451")


@pytest.mark.asyncio
async def test_delayed_tls_response_does_not_block_other_async_work(smtp_sink):
    smtp_sink.mode = "delay"
    started = time.monotonic()
    task = asyncio.create_task(notify.send_mail_ex("local delay", "<p>delay</p>"))
    await asyncio.sleep(0.02)
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 0.25
    finally:
        assert (await task)[0] is True


@pytest.mark.asyncio
async def test_actual_tls_mail_preserves_frozen_price_and_reason_with_failed_quit(smtp_sink):
    smtp_sink.mode = "quit421"
    html = digest_mail.digest_mail_html(
        counts=[("价格变化", 1)],
        details=["指定价格 · 美国 · A & B <C> · USD 12.34 · 价格低于 USD 15.00"],
        total=1, games=1, deferred=0, when=datetime.now(),
    )
    assert await notify.send_mail_ex("local rendered mail", html) == (True, None)
    message = BytesParser(policy=policy.default).parsebytes(smtp_sink.receipts[0]["body"])
    plain = message.get_body(preferencelist=("plain",)).get_content()
    body = message.get_body(preferencelist=("html",)).get_content()
    assert body == html
    for value in ("A & B <C>", "美国", "USD 12.34", "价格低于 USD 15.00"):
        assert value in plain
    assert "A &amp; B &lt;C&gt;" in body


@pytest.mark.asyncio
async def test_close_failure_cannot_overwrite_accepted_delivery(monkeypatch):
    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def sendmail(self, *_args):
            return {}

        def quit(self):
            return 221, b"bye"

        def close(self):
            raise OSError("injected cleanup failure")

    async def config():
        return dict(host="localhost", port=1, user="local", password="local",
                    to_addr="test@example.invalid", use_ssl=True)

    monkeypatch.setattr(notify, "smtp_config", config)
    monkeypatch.setattr(notify, "_smtp_login", lambda *_args: None)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", SMTP)
    assert await notify.send_mail_ex("local", "local") == (True, None)
