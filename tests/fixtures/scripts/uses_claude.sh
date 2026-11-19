#!/bin/zsh
# Example nightly job. No PATH is set, so claude and jq are not found under launchd.
source @FIXTURES@/scripts/missing.env
out=$(claude -p "summarize the notes")
echo "$out" | jq -r '.result' > /tmp/launchd-why-example.txt
