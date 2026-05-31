import urllib.request, json
data = json.dumps({"model": "gemma3:1b",
                   "prompt": "write an extremely long and detailed essay about the history of the universe",
                   "stream": True, "keep_alive": 120,
                   "options": {"num_predict": 6000}}).encode()
req = urllib.request.Request("http://host.docker.internal:11434/api/generate", data=data)
r = urllib.request.urlopen(req, timeout=180)
for _ in r:
    pass
