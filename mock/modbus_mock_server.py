#!/usr/bin/env python3
"""
Mock Modbus TCP Server
模拟 Modbus TCP 设备，返回随机数据用于测试
"""
import random
import time
import logging
from socketserver import TCPServer, BaseRequestHandler
import struct

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ModbusMockServer')


class ModbusTCPHandler(BaseRequestHandler):
    """处理 Modbus TCP 请求"""

    def handle(self):
        """处理客户端连接"""
        client_addr = self.client_address
        logger.info(f"New connection from {client_addr}")

        while True:
            try:
                # 接收 Modbus TCP 请求
                # MBAP Header (7 bytes) + PDU
                data = self.request.recv(1024)
                if not data:
                    break

                if len(data) < 8:
                    logger.warning(f"Invalid request length: {len(data)}")
                    continue

                # 解析 MBAP Header
                transaction_id = struct.unpack('>H', data[0:2])[0]
                protocol_id = struct.unpack('>H', data[2:4])[0]
                length = struct.unpack('>H', data[4:6])[0]
                unit_id = data[6]

                # 解析 PDU
                function_code = data[7]

                logger.debug(f"Request - Transaction: {transaction_id}, Function: {function_code}, Unit: {unit_id}")

                # 处理读保持寄存器 (0x03)
                if function_code == 0x03:
                    start_addr = struct.unpack('>H', data[8:10])[0]
                    quantity = struct.unpack('>H', data[10:12])[0]

                    logger.info(f"Read Holding Registers - Start: {start_addr}, Quantity: {quantity}")

                    # 生成随机数据
                    response_data = self._generate_read_response(
                        transaction_id, unit_id, function_code, start_addr, quantity
                    )
                    self.request.sendall(response_data)

                # 处理读输入寄存器 (0x04)
                elif function_code == 0x04:
                    start_addr = struct.unpack('>H', data[8:10])[0]
                    quantity = struct.unpack('>H', data[10:12])[0]

                    logger.info(f"Read Input Registers - Start: {start_addr}, Quantity: {quantity}")

                    response_data = self._generate_read_response(
                        transaction_id, unit_id, function_code, start_addr, quantity
                    )
                    self.request.sendall(response_data)

                # 处理读线圈 (0x01)
                elif function_code == 0x01:
                    start_addr = struct.unpack('>H', data[8:10])[0]
                    quantity = struct.unpack('>H', data[10:12])[0]

                    logger.info(f"Read Coils - Start: {start_addr}, Quantity: {quantity}")

                    response_data = self._generate_coil_response(
                        transaction_id, unit_id, function_code, start_addr, quantity
                    )
                    self.request.sendall(response_data)

                # 处理读离散输入 (0x02)
                elif function_code == 0x02:
                    start_addr = struct.unpack('>H', data[8:10])[0]
                    quantity = struct.unpack('>H', data[10:12])[0]

                    logger.info(f"Read Discrete Inputs - Start: {start_addr}, Quantity: {quantity}")

                    response_data = self._generate_coil_response(
                        transaction_id, unit_id, function_code, start_addr, quantity
                    )
                    self.request.sendall(response_data)

                else:
                    logger.warning(f"Unsupported function code: {function_code}")
                    # 返回异常响应
                    response_data = self._generate_exception_response(
                        transaction_id, unit_id, function_code, 0x01  # Illegal function
                    )
                    self.request.sendall(response_data)

            except Exception as e:
                logger.error(f"Error handling request: {e}")
                break

        logger.info(f"Connection closed from {client_addr}")

    def _generate_read_response(self, transaction_id, unit_id, function_code, start_addr, quantity):
        """生成读寄存器响应（随机数据）"""
        # 生成随机寄存器值
        register_values = []
        for i in range(quantity):
            # 基于地址生成不同的随机值范围
            addr = start_addr + i
            base_value = (addr % 100) * 100
            random_value = base_value + random.randint(0, 1000)
            register_values.append(random_value)

        # 构建响应
        byte_count = quantity * 2
        pdu = struct.pack('B', function_code) + struct.pack('B', byte_count)

        for value in register_values:
            pdu += struct.pack('>H', value)

        # MBAP Header
        mbap_length = len(pdu) + 1  # PDU + unit_id
        mbap = struct.pack('>H', transaction_id)  # Transaction ID
        mbap += struct.pack('>H', 0)  # Protocol ID
        mbap += struct.pack('>H', mbap_length)  # Length
        mbap += struct.pack('B', unit_id)  # Unit ID

        response = mbap + pdu

        logger.debug(f"Response - Registers: {register_values}")
        return response

    def _generate_coil_response(self, transaction_id, unit_id, function_code, start_addr, quantity):
        """生成读线圈/离散输入响应（随机布尔值）"""
        # 生成随机布尔值
        byte_count = (quantity + 7) // 8
        coil_bytes = []

        for i in range(byte_count):
            byte_value = random.randint(0, 255)
            coil_bytes.append(byte_value)

        # 构建响应
        pdu = struct.pack('B', function_code) + struct.pack('B', byte_count)
        for byte_val in coil_bytes:
            pdu += struct.pack('B', byte_val)

        # MBAP Header
        mbap_length = len(pdu) + 1
        mbap = struct.pack('>H', transaction_id)
        mbap += struct.pack('>H', 0)
        mbap += struct.pack('>H', mbap_length)
        mbap += struct.pack('B', unit_id)

        return mbap + pdu

    def _generate_exception_response(self, transaction_id, unit_id, function_code, exception_code):
        """生成异常响应"""
        pdu = struct.pack('B', function_code + 0x80) + struct.pack('B', exception_code)

        mbap_length = len(pdu) + 1
        mbap = struct.pack('>H', transaction_id)
        mbap += struct.pack('>H', 0)
        mbap += struct.pack('>H', mbap_length)
        mbap += struct.pack('B', unit_id)

        return mbap + pdu


class ModbusMockServer:
    """Mock Modbus TCP 服务器"""

    def __init__(self, host='127.0.0.1', port=502):
        self.host = host
        self.port = port
        self.server = None

    def start(self):
        """启动服务器"""
        try:
            self.server = TCPServer((self.host, self.port), ModbusTCPHandler)
            logger.info(f"Mock Modbus TCP Server started on {self.host}:{self.port}")
            logger.info("Waiting for connections...")
            self.server.serve_forever()
        except PermissionError:
            logger.error(f"Permission denied on port {self.port}. Try using port >= 1024 or run with sudo.")
            raise
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
            self.stop()
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise

    def stop(self):
        """停止服务器"""
        if self.server:
            logger.info("Shutting down server...")
            self.server.shutdown()
            self.server.server_close()
            logger.info("Server stopped")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Mock Modbus TCP Server')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=502, help='Port to bind (default: 502)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args()

    if args.debug:
        logging.getLogger('ModbusMockServer').setLevel(logging.DEBUG)

    server = ModbusMockServer(host=args.host, port=args.port)
    server.start()
