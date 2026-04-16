"""
Traffic Monitoring and Statistics Collector - Ryu SDN Controller
Course: COMPUTER NETWORKS - UE24CS252B
Project 3

Description:
    A Ryu OpenFlow 1.3 controller that acts as a learning switch while
    periodically collecting and displaying flow/port statistics from all
    connected datapaths. Generates human-readable traffic reports.

Features:
    - L2 learning switch with explicit match-action flow rules
    - Periodic flow statistics retrieval (every MONITOR_INTERVAL seconds)
    - Periodic port statistics retrieval
    - Packet/byte count display per flow and per port
    - Auto-generated text reports saved to disk
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub

import datetime
import os

# ─── Tunables ────────────────────────────────────────────────────────────────
MONITOR_INTERVAL  = 10   # seconds between each stats poll
REPORT_EVERY      = 3    # generate a report every Nth poll cycle
FLOW_IDLE_TIMEOUT = 30   # idle timeout (s) for installed forwarding rules
FLOW_HARD_TIMEOUT = 120  # hard  timeout (s) for installed forwarding rules
REPORT_DIR        = "./reports"   # directory to save report files
# ─────────────────────────────────────────────────────────────────────────────


class TrafficMonitor(app_manager.RyuApp):
    """
    Ryu application: Learning Switch + Traffic Monitor.

    Packet-in handling installs per-flow (src-MAC, dst-MAC, in_port) rules so
    subsequent frames are forwarded in the data-plane without controller
    involvement.  A background greenlet polls every switch for OpenFlow flow
    and port statistics, prints them to the Ryu log, and periodically writes
    a consolidated report file.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def __init__(self, *args, **kwargs):
        super(TrafficMonitor, self).__init__(*args, **kwargs)

        # {dpid -> {MAC -> port}}  – learned forwarding table
        self.mac_to_port = {}

        # {dpid -> datapath object}
        self.datapaths = {}

        # Latest statistics snapshots
        self.flow_stats = {}   # {dpid -> [OFPFlowStats]}
        self.port_stats = {}   # {dpid -> [OFPPortStats]}

        # Poll / report counters
        self._poll_count = 0

        # Ensure report directory exists
        os.makedirs(REPORT_DIR, exist_ok=True)

        # Start the background monitoring greenlet
        self.monitor_thread = hub.spawn(self._monitor_loop)

        self.logger.info("TrafficMonitor controller started. "
                         "Poll interval: %ds, Reports in: %s/",
                         MONITOR_INTERVAL, REPORT_DIR)

    # ── Datapath lifecycle ────────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        """Track which datapaths are currently connected."""
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("[+] Switch connected  : dpid=%016x", dp.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.logger.info("[-] Switch disconnected: dpid=%016x", dp.id)

    # ── Initial switch configuration ──────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install the table-miss rule so unknown packets reach the controller."""
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # Match everything, send to controller, lowest priority (0)
        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, priority=0, match=match, actions=actions)
        self.logger.info("[*] Table-miss rule installed on dpid=%016x", dp.id)

    # ── Helper: install a flow rule ───────────────────────────────────────────

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0):
        """
        Send an OFPFlowMod to install a flow rule.

        Args:
            datapath    : target switch datapath object
            priority    : OpenFlow priority (higher wins)
            match       : OFPMatch object (fields to match)
            actions     : list of OFPAction objects
            idle_timeout: remove rule if no traffic for this many seconds (0=never)
            hard_timeout: remove rule after this many seconds regardless (0=never)
        """
        ofp    = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(
            datapath     = datapath,
            priority     = priority,
            match        = match,
            instructions = inst,
            idle_timeout = idle_timeout,
            hard_timeout = hard_timeout,
        )
        datapath.send_msg(mod)

    # ── Packet-In: learning switch logic ──────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
        Handle unknown packets:
          1. Learn source MAC → in_port mapping.
          2. Determine output port (unicast or flood).
          3. Install an explicit match-action rule for future frames.
          4. Forward the current packet.
        """
        msg     = ev.msg
        dp      = msg.datapath
        ofp     = dp.ofproto
        parser  = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        # Drop LLDP frames silently
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src  = eth.src
        dst  = eth.dst
        dpid = dp.id

        self.mac_to_port.setdefault(dpid, {})

        # ── Learn ──────────────────────────────────────────────────────────
        if src not in self.mac_to_port[dpid]:
            self.logger.info("[LEARN] dpid=%016x  %s → port %s", dpid, src, in_port)
        self.mac_to_port[dpid][src] = in_port

        # ── Decide output port ────────────────────────────────────────────
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofp.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # ── Install flow rule (skip for flood) ────────────────────────────
        if out_port != ofp.OFPP_FLOOD:
            # Match: incoming port + src MAC + dst MAC
            match = parser.OFPMatch(
                in_port = in_port,
                eth_src = src,
                eth_dst = dst,
            )
            self._add_flow(
                dp,
                priority     = 1,
                match        = match,
                actions      = actions,
                idle_timeout = FLOW_IDLE_TIMEOUT,
                hard_timeout = FLOW_HARD_TIMEOUT,
            )
            self.logger.info("[FLOW] Installed rule: dpid=%016x  %s→%s  port %s→%s",
                             dpid, src, dst, in_port, out_port)

        # ── Forward the buffered packet ───────────────────────────────────
        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath  = dp,
            buffer_id = msg.buffer_id,
            in_port   = in_port,
            actions   = actions,
            data      = data,
        )
        dp.send_msg(out)

    # ── Background monitoring loop ────────────────────────────────────────────

    def _monitor_loop(self):
        """Greenlet: periodically poll all connected switches for stats."""
        while True:
            hub.sleep(MONITOR_INTERVAL)
            self._poll_count += 1
            self.logger.info("\n" + "═" * 60)
            self.logger.info("  STATS POLL #%d  (%s)",
                             self._poll_count,
                             datetime.datetime.now().strftime("%H:%M:%S"))
            self.logger.info("═" * 60)

            for dp in list(self.datapaths.values()):
                self._request_flow_stats(dp)
                self._request_port_stats(dp)

            # Generate a report every Nth poll
            if self._poll_count % REPORT_EVERY == 0:
                hub.sleep(1)   # let stat replies arrive first
                self._generate_report()

    def _request_flow_stats(self, dp):
        """Send OFPFlowStatsRequest to a datapath."""
        parser = dp.ofproto_parser
        req    = parser.OFPFlowStatsRequest(dp)
        dp.send_msg(req)

    def _request_port_stats(self, dp):
        """Send OFPPortStatsRequest (all ports) to a datapath."""
        ofp    = dp.ofproto
        parser = dp.ofproto_parser
        req    = parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY)
        dp.send_msg(req)

    # ── Stats reply handlers ──────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        """Receive and display per-flow packet/byte counters."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        self.flow_stats[dpid] = body

        # Filter out the table-miss rule (priority 0)
        rules = [s for s in body if s.priority > 0]

        self.logger.info("\n--- Flow Stats  dpid=%016x  (%d active rules) ---",
                         dpid, len(rules))
        if not rules:
            self.logger.info("    (no forwarding rules installed yet)")
            return

        header = "  {:<18} {:<18} {:>6}  {:>12}  {:>12}  {:>8}"
        row    = "  {:<18} {:<18} {:>6}  {:>12,}  {:>12,}  {:>8}"
        self.logger.info(header.format(
            "Src MAC", "Dst MAC", "Prio", "Packets", "Bytes", "Port→Port"))
        self.logger.info("  " + "-" * 76)

        for stat in sorted(rules, key=lambda s: (s.packet_count), reverse=True):
            m    = stat.match
            src  = m.get("eth_src",  "—")
            dst  = m.get("eth_dst",  "—")
            ip   = m.get("in_port",  "?")
            # find output port from instructions
            out_port = "?"
            for inst in stat.instructions:
                for act in getattr(inst, "actions", []):
                    if hasattr(act, "port"):
                        out_port = act.port
            self.logger.info(row.format(
                src, dst, stat.priority,
                stat.packet_count, stat.byte_count,
                f"{ip}→{out_port}",
            ))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """Receive and display per-port Rx/Tx counters."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        self.port_stats[dpid] = body

        self.logger.info("\n--- Port Stats  dpid=%016x ---", dpid)
        header = "  {:>6}  {:>12}  {:>14}  {:>12}  {:>14}  {:>8}  {:>8}"
        row    = "  {:>6}  {:>12,}  {:>14,}  {:>12,}  {:>14,}  {:>8,}  {:>8,}"
        self.logger.info(header.format(
            "Port", "Rx Pkts", "Rx Bytes",
            "Tx Pkts", "Tx Bytes",
            "Rx Drop", "Tx Drop"))
        self.logger.info("  " + "-" * 88)

        for stat in sorted(body, key=lambda s: s.port_no):
            self.logger.info(row.format(
                stat.port_no,
                stat.rx_packets, stat.rx_bytes,
                stat.tx_packets, stat.tx_bytes,
                stat.rx_dropped, stat.tx_dropped,
            ))

    # ── Report generation ─────────────────────────────────────────────────────

    def _generate_report(self):
        """Write a consolidated traffic report to disk."""
        now       = datetime.datetime.now()
        ts_human  = now.strftime("%Y-%m-%d %H:%M:%S")
        ts_file   = now.strftime("%Y%m%d_%H%M%S")
        filename  = os.path.join(REPORT_DIR, f"traffic_report_{ts_file}.txt")

        lines = []
        lines.append("=" * 62)
        lines.append(f"  SDN TRAFFIC MONITOR — REPORT #{self._poll_count // REPORT_EVERY}")
        lines.append(f"  Generated : {ts_human}")
        lines.append("=" * 62)

        if not self.datapaths:
            lines.append("  No switches connected.")
        else:
            for dpid in sorted(self.datapaths):
                lines.append(f"\n  Switch dpid = {dpid:016x}")
                lines.append("  " + "─" * 50)

                # ── Port summary ─────────────────────────────────────────
                port_data = self.port_stats.get(dpid, [])
                if port_data:
                    total_rx_pkts  = sum(s.rx_packets for s in port_data)
                    total_tx_pkts  = sum(s.tx_packets for s in port_data)
                    total_rx_bytes = sum(s.rx_bytes   for s in port_data)
                    total_tx_bytes = sum(s.tx_bytes   for s in port_data)
                    lines.append(f"  Port totals:")
                    lines.append(f"    RX : {total_rx_pkts:>10,} packets  |  {total_rx_bytes:>14,} bytes")
                    lines.append(f"    TX : {total_tx_pkts:>10,} packets  |  {total_tx_bytes:>14,} bytes")
                    lines.append(f"\n  Per-port breakdown:")
                    lines.append(f"  {'Port':>5}  {'RxPkts':>10}  {'RxBytes':>12}  "
                                 f"{'TxPkts':>10}  {'TxBytes':>12}")
                    lines.append("  " + "-" * 55)
                    for s in sorted(port_data, key=lambda x: x.port_no):
                        lines.append(f"  {s.port_no:>5}  {s.rx_packets:>10,}  {s.rx_bytes:>12,}  "
                                     f"{s.tx_packets:>10,}  {s.tx_bytes:>12,}")
                else:
                    lines.append("  Port statistics not yet available.")

                # ── Flow summary ─────────────────────────────────────────
                flow_data = self.flow_stats.get(dpid, [])
                active    = [s for s in flow_data if s.priority > 0]
                lines.append(f"\n  Active flow rules : {len(active)}")
                if active:
                    top5 = sorted(active, key=lambda s: s.byte_count, reverse=True)[:5]
                    lines.append(f"  Top flows by byte count:")
                    lines.append(f"  {'Src MAC':<18} {'Dst MAC':<18} {'Packets':>10}  {'Bytes':>12}")
                    lines.append("  " + "-" * 62)
                    for stat in top5:
                        m   = stat.match
                        src = m.get("eth_src", "—")
                        dst = m.get("eth_dst", "—")
                        lines.append(f"  {src:<18} {dst:<18} {stat.packet_count:>10,}  "
                                     f"{stat.byte_count:>12,}")

        lines.append("\n" + "=" * 62)
        report_text = "\n".join(lines)

        # Print to Ryu log
        self.logger.info("\n%s", report_text)

        # Save to file
        with open(filename, "w") as fh:
            fh.write(report_text + "\n")
        self.logger.info("[REPORT] Saved → %s", filename)
