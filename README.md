# SDN Mininet Simulation Project

## Objective

This project demonstrates Software Defined Networking (SDN) using Mininet and POX controller. It shows controller-switch interaction, flow rule installation, and network performance.

---

## Topology

```
        s1 -------- s2
       /  \        /  \
     h1   h2    h3   h4
```

---

## Setup

### Run Controller

```
cd ~/pox
./pox.py openflow.of_01 --port=6633 forwarding.l2_learning
```

### Run Topology

```
cd ~/cn_project
sudo python3 topology.py
```

---

## Testing

### Connectivity

```
pingall
```

### Performance

```
h1 iperf -s &
h3 iperf -c 10.0.0.1 -t 10
```

### Flow Table

```
sh ovs-ofctl -O OpenFlow10 dump-flows s1
```

---

## Results

### Topology

![Topology](screenshots/topology.png)

### Controller

![Controller](screenshots/controller.png)

### Connectivity

![Ping](screenshots/pingall.png)

### Performance

![iperf](screenshots/iperf.png)

### Flow Table

![Flows](screenshots/flows.png)

---

## Conclusion

The controller dynamically installs flow rules based on traffic, demonstrating SDN principles.

---

## References

* Mininet Documentation
* POX Controller
