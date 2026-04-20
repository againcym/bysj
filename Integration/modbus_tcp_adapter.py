from __future__ import annotations

import socket
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class ModbusAdapterError(RuntimeError):
    pass


@dataclass
class SignalAddressMapping:
    signal_name: str
    signal_type: str
    device: str
    raw_address: str
    modbus_area: str
    bit_address: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_name": self.signal_name,
            "signal_type": self.signal_type,
            "device": self.device,
            "raw_address": self.raw_address,
            "modbus_area": self.modbus_area,
            "bit_address": self.bit_address,
        }


def _parse_bit_address(raw_address: str) -> int:
    address = (raw_address or "").strip().upper()
    if len(address) < 4 or "." not in address:
        raise ModbusAdapterError(f"暂不支持的地址格式: {raw_address}")

    prefix = address[0]
    if prefix not in {"I", "Q"}:
        raise ModbusAdapterError(f"仅支持 I/Q 数字量地址，当前为: {raw_address}")

    byte_text, bit_text = address[1:].split(".", 1)
    try:
        byte_index = int(byte_text)
        bit_index = int(bit_text)
    except ValueError as exc:
        raise ModbusAdapterError(f"无法解析地址: {raw_address}") from exc

    if bit_index < 0 or bit_index > 7:
        raise ModbusAdapterError(f"位地址超出范围(0-7): {raw_address}")

    return byte_index * 8 + bit_index


class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._transaction_id = 0

    def ping(self) -> Dict[str, Any]:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return {
                    "ok": True,
                    "host": self.host,
                    "port": self.port,
                    "unit_id": self.unit_id,
                    "timeout": self.timeout,
                }
        except OSError as exc:
            raise ModbusAdapterError(f"无法连接到 Modbus TCP 服务器 {self.host}:{self.port}: {exc}") from exc

    def read_coils(self, start_address: int, count: int = 1) -> List[bool]:
        payload = struct.pack(">HH", start_address, count)
        response = self._request(0x01, payload)
        return self._decode_bit_response(response, count)

    def read_discrete_inputs(self, start_address: int, count: int = 1) -> List[bool]:
        payload = struct.pack(">HH", start_address, count)
        response = self._request(0x02, payload)
        return self._decode_bit_response(response, count)

    def write_single_coil(self, address: int, value: bool) -> Dict[str, Any]:
        payload = struct.pack(">HH", address, 0xFF00 if value else 0x0000)
        response = self._request(0x05, payload)
        if len(response) != 4:
            raise ModbusAdapterError("写单个线圈返回长度异常。")
        written_address, written_value = struct.unpack(">HH", response)
        return {
            "address": written_address,
            "value": written_value == 0xFF00,
        }

    def _request(self, function_code: int, payload: bytes) -> bytes:
        self._transaction_id = (self._transaction_id + 1) % 0xFFFF
        mbap = struct.pack(">HHHB", self._transaction_id, 0, len(payload) + 2, self.unit_id)
        frame = mbap + bytes([function_code]) + payload

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(frame)
                header = self._recv_exact(sock, 7)
                transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", header)
                if transaction_id != self._transaction_id or protocol_id != 0:
                    raise ModbusAdapterError("收到非法的 Modbus TCP 响应头。")
                body = self._recv_exact(sock, length - 1)
        except OSError as exc:
            raise ModbusAdapterError(f"Modbus TCP 请求失败: {exc}") from exc

        resp_function = body[0]
        if resp_function == (function_code | 0x80):
            exception_code = body[1] if len(body) > 1 else -1
            raise ModbusAdapterError(f"Modbus 异常响应，功能码={function_code}，异常码={exception_code}")
        if resp_function != function_code:
            raise ModbusAdapterError("Modbus 功能码不匹配。")
        return body[1:]

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        chunks: List[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ModbusAdapterError("Modbus TCP 连接被远端关闭。")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_bit_response(payload: bytes, expected_count: int) -> List[bool]:
        if not payload:
            raise ModbusAdapterError("空的 Modbus 位读响应。")
        byte_count = payload[0]
        data = payload[1 : 1 + byte_count]
        result: List[bool] = []
        for bit_index in range(expected_count):
            byte_value = data[bit_index // 8]
            result.append(bool((byte_value >> (bit_index % 8)) & 0x01))
        return result


class FactoryIOModbusService:
    def __init__(
        self,
        signal_definition_path: Path,
        host: str = "127.0.0.1",
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 2.0,
    ):
        self.signal_definition_path = Path(signal_definition_path)
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.client = ModbusTcpClient(host=host, port=port, unit_id=unit_id, timeout=timeout)
        self.signal_map = self._load_signal_map()

    def describe(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "unit_id": self.unit_id,
            "timeout": self.timeout,
            "signal_definition_path": str(self.signal_definition_path),
            "signal_count": len(self.signal_map),
        }

    def test_connection(self) -> Dict[str, Any]:
        result = self.client.ping()
        result["signal_count"] = len(self.signal_map)
        return result

    def list_signals(self, signal_type: Optional[str] = None) -> List[Dict[str, Any]]:
        items = list(self.signal_map.values())
        if signal_type:
            normalized = signal_type.strip().lower()
            items = [item for item in items if item.signal_type.lower() == normalized]
        return [item.to_dict() for item in items]

    def read_signal(self, signal_name: str) -> Dict[str, Any]:
        mapping = self._require_signal(signal_name)
        if mapping.modbus_area == "coil":
            value = self.client.read_coils(mapping.bit_address, 1)[0]
        elif mapping.modbus_area == "discrete_input":
            value = self.client.read_discrete_inputs(mapping.bit_address, 1)[0]
        else:
            raise ModbusAdapterError(f"当前骨架暂不支持读取区域: {mapping.modbus_area}")
        return {
            **mapping.to_dict(),
            "value": value,
        }

    def read_signals(
        self,
        signal_names: Optional[Iterable[str]] = None,
        signal_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if signal_names:
            names = list(signal_names)
        else:
            names = [item.signal_name for item in self.signal_map.values()]
            if signal_type:
                names = [name for name in names if self.signal_map[name].signal_type.lower() == signal_type.lower()]
        return [self.read_signal(name) for name in names]

    def write_output_signal(self, signal_name: str, value: bool) -> Dict[str, Any]:
        mapping = self._require_signal(signal_name)
        if mapping.modbus_area != "coil":
            raise ModbusAdapterError(f"信号 {signal_name} 不是可写输出点。")
        result = self.client.write_single_coil(mapping.bit_address, value)
        return {
            **mapping.to_dict(),
            "value": result["value"],
        }

    def apply_output_command(self, action_signal: str, value: bool = True) -> Dict[str, Any]:
        return self.write_output_signal(action_signal, value)

    def _require_signal(self, signal_name: str) -> SignalAddressMapping:
        mapping = self.signal_map.get(signal_name)
        if mapping is None:
            raise ModbusAdapterError(f"未在 Signal_Definition.xml 中找到信号: {signal_name}")
        return mapping

    def _load_signal_map(self) -> Dict[str, SignalAddressMapping]:
        if not self.signal_definition_path.exists():
            raise ModbusAdapterError(f"未找到信号定义文件: {self.signal_definition_path}")

        root = ET.parse(self.signal_definition_path).getroot()
        signal_map: Dict[str, SignalAddressMapping] = {}
        for device_elem in root.findall("Device"):
            device_name = device_elem.get("name") or ""
            for signal_elem in device_elem.findall("Signal"):
                signal_name = signal_elem.get("Name") or ""
                signal_type = signal_elem.get("Type") or ""
                raw_address = signal_elem.get("Address") or ""
                if not signal_name or not raw_address:
                    continue

                prefix = raw_address.strip().upper()[:1]
                if prefix == "Q":
                    modbus_area = "coil"
                elif prefix == "I":
                    modbus_area = "discrete_input"
                else:
                    continue

                signal_map[signal_name] = SignalAddressMapping(
                    signal_name=signal_name,
                    signal_type=signal_type or ("Output" if modbus_area == "coil" else "Input"),
                    device=device_name,
                    raw_address=raw_address,
                    modbus_area=modbus_area,
                    bit_address=_parse_bit_address(raw_address),
                )

        if not signal_map:
            raise ModbusAdapterError(f"在 {self.signal_definition_path} 中未找到可用的 I/Q 数字量信号。")
        return signal_map
