"""Local proxy helpers for kernel integration tests."""
import socket
import threading


def pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            chunk = a.recv(65536)
            if not chunk:
                break
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class RedirectProxy:
    """本地 CONNECT 代理：忽略目标，一律隧道到同一回显。"""

    def __init__(self, redirect_to: int) -> None:
        self._redirect_to = redirect_to
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        upstream = None
        try:
            head = conn.recv(8192)
            if not head:
                return
            upstream = socket.create_connection(("127.0.0.1", self._redirect_to),
                                                timeout=8)
            if head.split(b" ", 1)[0].upper() == b"CONNECT":
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.sendall(head)
            threading.Thread(target=pipe, args=(conn, upstream), daemon=True).start()
            pipe(upstream, conn)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for s in (conn, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def close(self) -> None:
        self._sock.close()
