#!/usr/bin/env bash
# Fake agent for tests: reports a usage limit on the first $FAKE_LIMIT_CALLS calls, then "translates".
state="${FAKE_STATE:-/tmp/tomeshift-fake-count}"
n=$(( $(cat "$state" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$state"
input=$(cat)
if [ "$n" -le "${FAKE_LIMIT_CALLS:-2}" ]; then
  echo "You've hit your usage limit. Try again in 1m." >&2; exit 1
fi
# "Translate": one Bangla word per English word, paragraph breaks kept.
printf '%s\n' "$input" | sed -n '/^Translate this passage:/,$p' | tail -n +3 | awk 'NF{s="";for(i=1;i<=NF;i++)s=s "অনুবাদ ";print s;next}{print}'
