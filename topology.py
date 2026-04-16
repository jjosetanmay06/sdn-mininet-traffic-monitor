"""
Custom Mininet Topology for Traffic Monitor Project
Course: COMPUTER NETWORKS - UE24CS252B  |  Project 3

Topology:
                     s1 ──────── s2
                    / \          / \
                  h1   h2      h3   h4
                10Mbps       10Mbps
              inter-switch: 100 Mbps

Run:
    sudo python3 topology.py
"""

from mininet.net     import Mininet
from mininet.node    import RemoteController, OVSSwitch
from mininet.topo    import Topo
from mininet.log     import setLogLevel, info
from mininet.cli     import CLI
from mininet.link    import TCLink


class MonitorTopo(Topo):
    """
    Two switches, four hosts.
    h1, h2 hang off s1.
    h3, h4 hang off s2.
    s1 and s2 are connected with a high-bandwidth inter-switch link.
    """

    def build(self):
        # ── Switches ──────────────────────────────────────────────────────
        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")

        # ── Hosts (auto MAC assignment via autoSetMacs=True in Mininet()) ──
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")

        # ── Host-to-switch links (10 Mbps access links) ───────────────────
        self.addLink(h1, s1, bw=10, delay="2ms")
        self.addLink(h2, s1, bw=10, delay="2ms")
        self.addLink(h3, s2, bw=10, delay="2ms")
        self.addLink(h4, s2, bw=10, delay="2ms")

        # ── Inter-switch link (100 Mbps backbone) ─────────────────────────
        self.addLink(s1, s2, bw=100, delay="1ms")


def run():
    setLogLevel("info")
    topo = MonitorTopo()

    net = Mininet(
        topo        = topo,
        controller  = lambda name: RemoteController(name, ip="127.0.0.1", port=6633),
        switch      = OVSSwitch,
        link        = TCLink,
        autoSetMacs = True,   # deterministic MACs (00:00:00:00:00:01, etc.)
    )

    net.start()
    info("\n" + "═" * 60 + "\n")
    info("  Topology started. Ryu controller expected at 127.0.0.1:6633\n")
    info("  Hosts: h1=10.0.0.1  h2=10.0.0.2  h3=10.0.0.3  h4=10.0.0.4\n")
    info("\n  Quick tests to try in the Mininet CLI:\n")
    info("    pingall                  — test full connectivity\n")
    info("    h1 ping -c5 h3           — ICMP latency\n")
    info("    iperf h1 h3              — TCP throughput\n")
    info("    h1 iperf -s &            — start iperf server on h1\n")
    info("    h3 iperf -c 10.0.0.1 -t 20  — 20-second iperf run\n")
    info("    sh ovs-ofctl dump-flows s1   — view flow table on s1\n")
    info("    sh ovs-ofctl dump-flows s2   — view flow table on s2\n")
    info("═" * 60 + "\n")

    CLI(net)
    net.stop()


if __name__ == "__main__":
    run()
