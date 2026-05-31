#!/usr/bin/env python3
"""Prototype: attribute model-server traffic to the calling container.

For every running container we read its OWN socket table (/proc/<pid>/net/tcp[6],
visible because we run as root in the host PID namespace) and count ESTABLISHED
connections whose REMOTE port belongs to a recognised model server. A caller
reaching the server via host.docker.internal hits the published port; a caller on
the same docker network hits the container's private port — we map both, so either
wiring is caught. Remote IP is ignored on purpose (host.docker.internal collapses
every caller onto the gateway IP) — the port is what identifies the server.
"""
import json, subprocess, os

AI_KEYS = ("ollama", "vllm", "whisper", "speaches", "localai", "llama.cpp", "llama-server",
           "koboldcpp", "tabbyapi", "text-generation", "lmstudio", "xinference", "aphrodite",
           "infinity", "comfyui", "automatic1111", "stable-diffusion", "sd-webui", "invokeai")

def sh(*a):
    return subprocess.run(a, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")

def est_remote_ports(pid):
    """Set of remote ports (int) of this pid's ESTABLISHED TCP connections."""
    ports = set()
    for fn in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(fn) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if len(p) > 3 and p[3] == "01":          # 01 = TCP_ESTABLISHED
                        ports.add(int(p[2].split(":")[1], 16))  # rem_address = IP:PORT(hex)
        except Exception:
            pass
    return ports

ids = sh("docker", "ps", "-q").split()
cts = json.loads(sh("docker", "inspect", *ids)) if ids else []

# Build: per container → (name, pid, is_ai, ports it serves). target_port → server name.
meta, targets = [], {}
for c in cts:
    name = c["Name"].lstrip("/")
    pid = c.get("State", {}).get("Pid") or 0
    image = (c.get("Config", {}) or {}).get("Image", "").lower()
    is_ai = any(k in image or k in name.lower() for k in AI_KEYS)
    pubs, privs = set(), set()
    for cport, binds in ((c.get("NetworkSettings", {}) or {}).get("Ports") or {}).items():
        try:
            privs.add(int(cport.split("/")[0]))
        except Exception:
            pass
        for b in (binds or []):
            hp = b.get("HostPort")
            if hp:
                pubs.add(int(hp))
    meta.append({"name": name, "pid": pid, "is_ai": is_ai})
    if is_ai:
        for p in pubs | privs:
            targets.setdefault(p, name)

print(f"recognised servers + their ports: "
      f"{ {targets[p]: [q for q in targets if targets[q]==targets[p]] for p in targets} }\n")

edges = {}
for m in meta:
    if not m["pid"]:
        continue
    hit = est_remote_ports(m["pid"]) & set(targets)
    for port in hit:
        server = targets[port]
        if server == m["name"]:
            continue
        edges[(m["name"], server, port)] = edges.get((m["name"], server, port), 0) + 1

if not edges:
    print("no caller→server edges at this instant")
else:
    print("CALLER".ljust(20), "→  SERVER".ljust(18), "PORT")
    for (caller, server, port), n in sorted(edges.items()):
        print(caller.ljust(20), "→ ", server.ljust(16), port)
