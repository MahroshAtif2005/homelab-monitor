import app, sys
pid = sys.argv[1] if len(sys.argv) > 1 else None
if pid:
    print("ports for pid", pid, ":", sorted(set(app._est_remote_ports(pid))))
conts = app.containers()
ai = {c["name"] for c in conts if app._match_probe(c)}
print("ai servers:", ai)
nm = {c["id"]: c["name"] for c in conts}
pm = app.container_pids(nm)
print("pid map size:", len(pm))
print("edges:", app.sample_callers(conts, ai))
