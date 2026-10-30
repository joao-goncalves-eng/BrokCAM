"""Camada de transporte de bytes entre o controlador e o GRBL.

O ``GrblController`` não fala diretamente com o pyserial: ele usa qualquer objeto
que siga o protocolo ``Transport``. Isso permite trocar a porta serial real pelo
``GrblSimulator`` (máquina virtual) ou por uma conexão TCP/Telnet (WiFi da MKS DLC32,
``TcpTransport``) sem mudar nada no controlador.
"""
from __future__ import annotations

import select
import socket
import threading
from typing import Protocol, runtime_checkable

__all__ = ["Transport", "SerialTransport", "TcpTransport", "is_tcp_url", "list_serial_ports"]


@runtime_checkable
class Transport(Protocol):
    """Interface mínima de um canal de bytes full-duplex.

    * ``write(data)`` envia bytes (pode levantar ``OSError`` se desconectado).
    * ``read()`` devolve os bytes disponíveis, bloqueando no máximo um intervalo curto
      (~20–50 ms); retorna ``b""`` se nada chegou. Levanta ``OSError`` se o
      dispositivo sumiu (cabo desconectado).
    * ``close()`` libera o recurso; chamadas repetidas são seguras.
    * ``in_waiting`` quantos bytes já estão disponíveis para leitura.
    """

    def write(self, data: bytes) -> None: ...

    def read(self) -> bytes: ...

    def close(self) -> None: ...

    @property
    def in_waiting(self) -> int: ...


class SerialTransport:
    """``Transport`` sobre uma porta serial real (pyserial)."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.03) -> None:
        import serial  # import tardio: o resto do módulo funciona sem pyserial

        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.timeout = timeout
        s.write_timeout = 2.0
        # DTR/RTS desligados ANTES de abrir: em placas com ESP32 (MKS DLC32, Grbl_Esp32) esses
        # sinais estão ligados ao EN/IO0 e, ligados, seguram o ESP32 em reset ou no modo de
        # gravação — o GRBL nunca responde. Num Arduino com GRBL isso só evita um reset extra.
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException as exc:
            raise OSError(explain_serial_error(port, exc)) from exc
        self._serial = s
        self.port = port

    def write(self, data: bytes) -> None:
        try:
            self._serial.write(data)
        except Exception as exc:  # serial.SerialException herda de IOError
            raise OSError(str(exc)) from exc

    def read(self) -> bytes:
        try:
            n = self._serial.in_waiting
            return self._serial.read(n if n > 0 else 1)
        except Exception as exc:
            raise OSError(str(exc)) from exc

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass

    @property
    def in_waiting(self) -> int:
        try:
            return int(self._serial.in_waiting)
        except Exception as exc:
            raise OSError(str(exc)) from exc


class TcpTransport:
    """``Transport`` sobre TCP (Telnet "cru"), ex.: WiFi da MKS DLC32/Grbl_Esp32.

    O Grbl_Esp32 abre um servidor Telnet na porta 23 (``Telnet/Port``) que repassa
    bytes diretamente ao GRBL e envia o banner ``Grbl ...`` ao conectar. No modo AP da
    DLC32 o endereço padrão é ``192.168.4.1``: ``TcpTransport("192.168.4.1", 23)`` ou
    ``TcpTransport.from_url("tcp://192.168.4.1:23")``.

    Sequências de negociação Telnet (``IAC ...``) que algum servidor envie são
    descartadas; o GRBL só fala ASCII, então isso não afeta os dados.
    """

    IAC = 0xFF

    def __init__(self, host: str, port: int = 23, connect_timeout: float = 5.0,
                 read_timeout: float = 0.03, write_timeout: float = 3.0) -> None:
        self.host, self.port = host, int(port)
        self.read_timeout = read_timeout
        try:
            sock = socket.create_connection((host, self.port), timeout=connect_timeout)
        except OSError as exc:
            raise OSError(f"Não foi possível conectar a {host}:{self.port} ({exc}). Confira se o "
                          f"computador está na mesma rede WiFi da placa e se o Telnet está ligado.") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:  # pragma: no cover - depende do SO
            pass
        sock.settimeout(write_timeout)
        self._sock = sock
        self._closed = False
        self._iac_state = 0          # 0 normal, 1 após IAC, 2 aguardando opção, 3 subnegociação
        self._pending = bytearray()
        self._wlock = threading.Lock()

    @classmethod
    def from_url(cls, url: str, **kw) -> "TcpTransport":  # type: ignore[no-untyped-def]
        from brokcam.machine.firmware import split_tcp_url
        host, port = split_tcp_url(url)
        return cls(host, port, **kw)

    @property
    def url(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def write(self, data: bytes) -> None:
        if self._closed:
            raise OSError("conexão TCP fechada")
        try:
            with self._wlock:
                self._sock.sendall(data)
        except OSError as exc:
            raise OSError(f"falha ao enviar para {self.host}:{self.port}: {exc}") from exc

    def _filter(self, data: bytes) -> bytes:
        if self.IAC not in data and self._iac_state == 0:
            return data
        out = bytearray()
        for b in data:
            st = self._iac_state
            if st == 0:
                if b == self.IAC:
                    self._iac_state = 1
                else:
                    out.append(b)
            elif st == 1:
                if b == self.IAC:            # IAC IAC = byte 255 literal
                    out.append(b)
                    self._iac_state = 0
                elif b in (251, 252, 253, 254):   # WILL/WONT/DO/DONT + opção
                    self._iac_state = 2
                elif b == 250:               # SB ... IAC SE
                    self._iac_state = 3
                else:
                    self._iac_state = 0
            elif st == 2:
                self._iac_state = 0
            elif st == 3:
                if b == self.IAC:
                    self._iac_state = 4
            else:                            # 4: dentro de SB após IAC
                self._iac_state = 0 if b == 240 else 3
        return bytes(out)

    def _recv(self, timeout: float) -> bytes:
        if self._closed:
            raise OSError("conexão TCP fechada")
        try:
            r, _, _ = select.select([self._sock], [], [], timeout)
        except (OSError, ValueError) as exc:
            raise OSError(str(exc)) from exc
        if not r:
            return b""
        try:
            data = self._sock.recv(4096)
        except OSError as exc:
            raise OSError(f"conexão com {self.host}:{self.port} perdida: {exc}") from exc
        if not data:
            raise OSError(f"a placa {self.host}:{self.port} fechou a conexão")
        return self._filter(data)

    def read(self) -> bytes:
        if self._pending:
            data = bytes(self._pending)
            self._pending.clear()
            return data
        return self._recv(self.read_timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    @property
    def in_waiting(self) -> int:
        if not self._pending:
            self._pending += self._recv(0.0)
        return len(self._pending)


def is_tcp_url(port: str) -> bool:
    """``True`` para ``tcp://...`` ou ``telnet://...``."""
    low = (port or "").strip().lower()
    return low.startswith("tcp://") or low.startswith("telnet://")


# chips USB-serial usados em placas de CNC/laser (VID USB)
_USB_SERIAL_VIDS = {
    0x1A86: "CH340",            # MKS DLC32, clones de Arduino
    0x10C4: "CP210x",           # ESP32 DevKit, várias placas laser
    0x0403: "FTDI",
    0x2341: "Arduino",
    0x2A03: "Arduino",
    0x303A: "ESP32 USB",
}


def _port_sort_key(dev: str) -> tuple[str, int]:
    digits = "".join(ch for ch in dev if ch.isdigit())
    return (dev.rstrip("0123456789"), int(digits) if digits else 0)


def list_serial_ports() -> list[tuple[str, str]]:
    """Lista ``(porta, descrição)``: primeiro as que parecem CNC (USB-serial), depois as demais.

    Portas Bluetooth (comuns no Windows) vão para o fim e são identificadas como tal, para
    não serem confundidas com a máquina.
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    likely, other = [], []
    for p in sorted(list_ports.comports(), key=lambda p: _port_sort_key(p.device)):
        desc = p.description or p.device
        hwid = (p.hwid or "").upper()
        if p.vid in _USB_SERIAL_VIDS:
            likely.append((p.device, f"{desc} — provável CNC ({_USB_SERIAL_VIDS[p.vid]})"))
        elif "BTHENUM" in hwid or "BLUETOOTH" in desc.upper():
            other.append((p.device, f"{desc} — Bluetooth (não é a CNC)"))
        else:
            other.append((p.device, desc))
    return likely + other


def explain_serial_error(port: str, exc: BaseException) -> str:
    """Mensagem clara (PT-BR) para falhas comuns ao abrir a porta serial."""
    msg = str(exc)
    low = msg.lower()
    if "acesso negado" in low or "access is denied" in low or "permissionerror" in low or "errno 13" in low:
        return (f"A porta {port} está em uso por outro programa (ex.: Candle, LaserGRBL, LightBurn, UGS, "
                "Arduino IDE/monitor serial ou outra janela do BrokCAM). Feche ou desconecte esse programa e "
                "tente de novo — no Windows só um programa por vez pode usar a porta.")
    if "filenotfound" in low or "could not find" in low or "não pode encontrar" in low or "errno 2" in low:
        return (f"A porta {port} não existe agora. Confira o cabo USB, ligue a placa e clique em atualizar a "
                "lista de portas.")
    if "semaphore" in low or "sem\u00e1foro" in low or "tempo limite" in low:
        return (f"A porta {port} não respondeu (o driver travou). Desconecte e reconecte o cabo USB.")
    return f"Não foi possível abrir {port}: {msg}"
