#!/usr/bin/env python3
"""Send V01 packets (like test_eth_ceiling) at variable pps and track packet loss.

This script uses the same packet format and pacing as test_eth_ceiling.py,
but allows configurable rates and simpler output.

Usage:
    python3 test_sensor_loss.py --pps 10000 --duration 10
    python3 test_sensor_loss.py --pps 20000 --duration 5 --port 9217
    python3 test_sensor_loss.py --pps 160000 --duration 5  # spread across 8 ports
"""

import argparse
import sys
import time
import socket
import threading

import scis


def test_single_port(port, target_pps, duration):
    """Test a single port with loss tracking."""
    host_ip = scis.resolve_host_ip()
    
    # Setup receiver
    sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock_recv.bind((host_ip, port))
    sock_recv.settimeout(0.1)

    # Setup sender
    sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_send.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)

    received_packets = {}
    running = True
    recv_count = [0]

    def receiver_loop():
        while running:
            try:
                data, _ = sock_recv.recvfrom(4096)
                # Parse received packets
                for offset in range(0, len(data) - 8, 9):
                    record = scis.unpack(data[offset:offset + 9])
                    if record:
                        received_packets[record] = True
                        recv_count[0] += 1
            except socket.timeout:
                continue
            except OSError:
                break

    # Start receiver thread
    recv_thread = threading.Thread(target=receiver_loop, daemon=True)
    recv_thread.start()
    time.sleep(0.1)

    # Send packets
    pacer = scis.Pacer(target_pps)
    pacer.start()
    
    device_id = scis.WIDE_BAND_IDS[0]
    seq = 0
    start_time = time.perf_counter()

    print(f"Sending {target_pps} pps on port {port}...")
    while time.perf_counter() - start_time < duration and seq < scis.SEQ_SPAN:
        sock_send.sendto(scis.pack(device_id, seq), (scis.FPGA_RX_IP, port))
        seq += 1
        pacer.wait()

    sent_count = seq
    time.sleep(0.5)
    running = False
    recv_thread.join(timeout=1.0)

    # Calculate loss
    unique_received = len(received_packets)
    loss_pct = 100.0 * (sent_count - unique_received) / sent_count if sent_count > 0 else 0

    sock_send.close()
    sock_recv.close()

    return sent_count, unique_received, loss_pct


def test_multi_port(ports, total_pps, duration):
    """Test multiple ports with loss tracking."""
    host_ip = scis.resolve_host_ip()
    pps_per_port = total_pps // len(ports)

    # Setup sockets
    sockets_recv = {}
    sockets_send = {}
    received_packets = {}
    running = True
    recv_count = [0]

    for port in ports:
        sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        sock_recv.bind((host_ip, port))
        sock_recv.settimeout(0.1)
        sockets_recv[port] = sock_recv

        sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_send.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        sockets_send[port] = sock_send

    def receiver_loop():
        while running:
            for port, sock in sockets_recv.items():
                try:
                    data, _ = sock.recvfrom(4096)
                    for offset in range(0, len(data) - 8, 9):
                        record = scis.unpack(data[offset:offset + 9])
                        if record:
                            received_packets[record] = True
                            recv_count[0] += 1
                except socket.timeout:
                    continue
                except OSError:
                    break

    # Start receiver threads
    recv_thread = threading.Thread(target=receiver_loop, daemon=True)
    recv_thread.start()
    time.sleep(0.1)

    # Send packets from each port
    pacers = {port: scis.Pacer(pps_per_port) for port in ports}
    for pacer in pacers.values():
        pacer.start()

    seq_counter = [0]
    sent_count = [0]

    def sender_loop(port_idx, port):
        device_id = scis.WIDE_BAND_IDS[port_idx % len(scis.WIDE_BAND_IDS)]
        pacer = pacers[port]
        start_time = time.perf_counter()

        while time.perf_counter() - start_time < duration and seq_counter[0] < scis.SEQ_SPAN:
            sock_send = sockets_send[port]
            seq = seq_counter[0]
            seq_counter[0] += 1
            sent_count[0] += 1
            
            sock_send.sendto(scis.pack(device_id, seq), (scis.FPGA_RX_IP, port))
            pacer.wait()

    send_threads = [
        threading.Thread(target=sender_loop, args=(i, port), daemon=True)
        for i, port in enumerate(ports)
    ]
    
    print(f"Sending {total_pps} pps across {len(ports)} ports ({pps_per_port} pps each)...")
    for t in send_threads:
        t.start()
    for t in send_threads:
        t.join()

    time.sleep(0.5)
    running = False
    recv_thread.join(timeout=1.0)

    # Calculate loss
    unique_received = len(received_packets)
    loss_pct = 100.0 * (sent_count[0] - unique_received) / sent_count[0] if sent_count[0] > 0 else 0

    for sock in sockets_send.values():
        sock.close()
    for sock in sockets_recv.values():
        sock.close()

    return sent_count[0], unique_received, loss_pct


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pps", type=int, default=10000,
                    help="target packets per second (default 10000)")
    ap.add_argument("--duration", type=float, default=5.0,
                    help="test duration in seconds (default 5)")
    ap.add_argument("--port", type=int, default=None,
                    help="single port to test (default: spread across all 8)")
    args = ap.parse_args()

    if args.pps <= 0:
        print("Error: --pps must be positive")
        return 1
    if args.duration <= 0:
        print("Error: --duration must be positive")
        return 1

    if args.port:
        # Single port test
        ports = [args.port]
        print(f"Host: {scis.resolve_host_ip()}  ->  FPGA RX: {scis.FPGA_RX_IP}:{args.port}")
        print(f"Target rate: {args.pps} pps for {args.duration}s")
        print()

        sent, received, loss = test_single_port(args.port, args.pps, args.duration)
    else:
        # Multi-port test
        ports = scis.ALL_PORTS
        print(f"Host: {scis.resolve_host_ip()}  ->  FPGA RX: {scis.FPGA_RX_IP}:{ports}")
        print(f"Total target rate: {args.pps} pps across {len(ports)} ports")
        print(f"Rate per port: {args.pps // len(ports)} pps")
        print()

        sent, received, loss = test_multi_port(ports, args.pps, args.duration)

    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Packets sent:     {sent:>10,}")
    print(f"Packets received: {received:>10,}")
    print(f"Packet loss:      {loss:>10.2f}%")
    print(f"Loss count:       {sent - received:>10,}")
    print("=" * 60)

    if loss > 0.1:
        print(f"\nWARNING: Loss detected ({loss:.2f}%)")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

