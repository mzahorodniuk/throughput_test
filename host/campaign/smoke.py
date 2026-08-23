import hiload, scis
host = scis.resolve_host_ip()
rig = hiload.Rig(host)
print(f"host {host} -> FPGA RX {scis.FPGA_RX_IP}   granted SO_RCVBUF={rig.granted_rcvbuf/1024:.0f} KiB  ncpu={hiload.NCPU}")
try:
    ok, r = hiload.probe_alive(rig, [9219])
    print("T0 trickle alive=", ok, "sent=", r["sent"], "unique=", r["unique"])
    for rate in (5000, 20000, 60000, 100000, 140000):
        r = hiload.run_step(rig, scis.ALL_PORTS, rate, 2.0, nsend=2, nrecv=2)
        print(f"rate={rate:>7} ach={r['achieved_pps']:>9,.0f} sent={r['sent']:>7} "
              f"uniq={r['unique']:>7} loss={r['loss_pct']:>8.3f}% deliv={r['delivered_pps']:>9,.0f} "
              f"batch={r['batch_mean']:>5.2f}/{r['batch_max']:<2} dg={r['datagrams']:>6} "
              f"hd={r['host_drops']:>6} ilk={r['interlock']} af={r['almostfull']} "
              f"gap={r['gap_kind']}/{r['longest_gap']} valid={r['valid']}")
finally:
    rig.close()
