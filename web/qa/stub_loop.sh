#!/bin/bash
cd /home/user/dl/ext/Unknown-Agent
while true; do
  python3 web/qa/stub_server.py
  echo "[loop] stub exited rc=$?, restarting in 1s"
  sleep 1
done
