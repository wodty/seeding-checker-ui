#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自测脚本：进程内启动服务并验证 API 链路（写 marker 文件便于外部观察）"""
import os
import sys
import time
import threading
import traceback

MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_marker.txt")


def log(msg):
    with open(MARKER, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main():
    try:
        os.remove(MARKER)
    except Exception:
        pass
    log("STEP1: starting")
    sys.argv = ["app.py", "--demo"]
    import app
    log("STEP2: app imported")
    import uvicorn
    import requests

    cfg = uvicorn.Config(app.app, host="127.0.0.1", port=8003, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(15):
        time.sleep(1)
        if server.started:
            break
    log(f"STEP3: server.started={server.started}")
    if not server.started:
        return

    r = requests.post("http://127.0.0.1:8003/api/qb/test", timeout=10)
    log(f"STEP4: qb/test http={r.status_code} body={r.json()}")

    r = requests.post("http://127.0.0.1:8003/api/scan", timeout=60)
    log(f"STEP5: scan http={r.status_code}")
    d = r.json()
    log(f"STEP6: summary={d['summary']}")
    log(f"STEP7: redundant={len(d['redundant'])} missing={len(d['missing'])}")

    if d["redundant"]:
        p = d["redundant"][0]["path"]
        r = requests.post("http://127.0.0.1:8003/api/delete-files",
                          json={"paths": [p], "mode": "trash"}, timeout=20)
        log(f"STEP8: delete-files trash http={r.status_code} body={r.json()}")

    if d["missing"]:
        h = d["missing"][0]["torrent_hash"]
        r = requests.post("http://127.0.0.1:8003/api/delete-torrents",
                          json={"hashes": [h], "delete_torrent_file": True}, timeout=20)
        log(f"STEP9: delete-torrents http={r.status_code} body={r.json()}")

    log("STEP10: DONE")
    server.should_exit = True


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL: " + traceback.format_exc())
