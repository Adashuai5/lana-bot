#!/bin/bash
# Automated decision cycle runner for lana-bot
cd /Users/ada/lana-bot
/usr/local/bin/claude -p "@CLAUDE.md run one decision cycle" >> logs/cycle.log 2>&1
