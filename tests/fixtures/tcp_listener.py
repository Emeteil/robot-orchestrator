import socket
import sys
import time

port = int(sys.argv[1])
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

print(f"starting, will listen after {delay}s", flush=True)
time.sleep(delay)

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", port))
sock.listen(1)
print("listening", flush=True)

while True:
    time.sleep(1.0)
