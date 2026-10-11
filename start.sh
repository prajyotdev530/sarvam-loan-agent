#!/bin/bash
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt 2>/dev/null || pip3 install -q -r requirements.txt
(sleep 2; open http://localhost:8000 2>/dev/null) &
python3 server.py
