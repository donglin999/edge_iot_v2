#!/bin/bash
# Mock Modbus TCP Server entrypoint.
#
# Runs the realistic generator in /mock/modbus_realistic.py (mounted from
# the host). If the file is missing falls back to a minimal inline server
# so the container still answers Modbus.

set -e

if [ -f /mock/modbus_realistic.py ]; then
    exec python -u /mock/modbus_realistic.py
fi

exec python -u -c "
import random, struct, logging
from socketserver import TCPServer, BaseRequestHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('mock-fallback')

class H(BaseRequestHandler):
    def handle(self):
        log.info('conn from %s', self.client_address)
        while True:
            data = self.request.recv(1024)
            if not data or len(data) < 8:
                return
            tid, _, _, uid = struct.unpack('>HHHB', data[:7])
            fc = data[7]
            if fc in (3, 4):
                start, qty = struct.unpack('>HH', data[8:12])
                vals = [(start+i)*10 + random.randint(0,100) for i in range(qty)]
                pdu = struct.pack('BB', fc, qty*2) + b''.join(struct.pack('>H', v) for v in vals)
                self.request.sendall(struct.pack('>HHH', tid, 0, len(pdu)+1) + bytes([uid]) + pdu)

TCPServer.allow_reuse_address = True
TCPServer(('0.0.0.0', 5020), H).serve_forever()
"
