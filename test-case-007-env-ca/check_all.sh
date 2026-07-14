#!/bin/sh
# test-case-007: 跑 5 种客户端的信任检测 + 1 次金丝雀检测，把结果打印成
# KEY=VALUE 的形式（每行一个），不对任何结果做期望值判断。
#
# 期望值判断故意不放在这个脚本里——放在 workflow yaml 里每个 case 步骤旁边，
# 这样"这个 case 设置了哪个变量、期望哪个客户端成功"一眼就能看到，不用跳到
# 另一个文件里找 case 分支。这个脚本只负责把 5 个测试机械地跑一遍。
#
# 用法：sh check_all.sh
# 输出（每行一个）：
#   CANARY=BUMPED|NOT_BUMPED(...)
#   CURL=OK|FAIL
#   GIT=OK|FAIL
#   PYTHON_URLLIB=OK|FAIL
#   PIP=OK|FAIL
#   REQUESTS=OK|FAIL

set -u

SQUID_CA="${SQUID_CA:-/etc/squid-ca/squid-ca.pem}"
TARGET_URL="${TARGET_URL:-https://pypi.org}"
GIT_TARGET="${GIT_TARGET:-https://github.com/octocat/Hello-World}"
WORKDIR="$(mktemp -d /tmp/env-ca-check.XXXXXX)"

PIP_EXTRA_FLAGS=""
if python3 -m pip install --help 2>/dev/null | grep -q "break-system-packages"; then
  PIP_EXTRA_FLAGS="--break-system-packages"
fi

# ---- 金丝雀：不管本 case 测的是哪个变量，独立确认 squid 这次真的在拦截 ----
issuer=$(curl -s --max-time 10 --cacert "$SQUID_CA" -v -o /dev/null "$TARGET_URL" 2>&1 | grep -i "issuer:")
case "$issuer" in
  *SquidCacheCA*) echo "CANARY=BUMPED" ;;
  *) echo "CANARY=NOT_BUMPED ($issuer)" ;;
esac

# ---- curl ----
if curl -fsS --max-time 10 -o /dev/null "$TARGET_URL" 2>"$WORKDIR/curl.err"; then
  echo "CURL=OK"
else
  echo "CURL=FAIL"
fi

# ---- git（偶发瞬时网络错误重试最多 3 次，SSL 相关失败不重试）----
tries=0
R_GIT=FAIL
while [ "$tries" -lt 3 ]; do
  rm -rf "$WORKDIR/git-clone"
  if git clone --depth 1 "$GIT_TARGET" "$WORKDIR/git-clone" >"$WORKDIR/git.log" 2>&1; then
    R_GIT=OK
    break
  fi
  grep -qi "ssl\|certificate" "$WORKDIR/git.log" && break
  tries=$((tries + 1))
  sleep 3
done
echo "GIT=$R_GIT"

# ---- python3 urllib ----
if python3 -c "import urllib.request; urllib.request.urlopen('${TARGET_URL}', timeout=10)" >"$WORKDIR/urllib.log" 2>&1; then
  echo "PYTHON_URLLIB=OK"
else
  echo "PYTHON_URLLIB=FAIL"
fi

# ---- pip（强制真实网络往返，避免因预装包 no-op 而误判）----
out=$(python3 -m pip install --no-cache-dir --force-reinstall --no-deps $PIP_EXTRA_FLAGS requests 2>&1)
echo "$out" >"$WORKDIR/pip.log"
if echo "$out" | grep -qE "Collecting|Downloading" && echo "$out" | grep -q "Successfully installed"; then
  echo "PIP=OK"
else
  echo "PIP=FAIL"
fi

# ---- 裸 python requests ----
if python3 -c "import requests; requests.get('${TARGET_URL}', timeout=10)" >"$WORKDIR/requests.log" 2>&1; then
  echo "REQUESTS=OK"
else
  echo "REQUESTS=FAIL"
fi

rm -rf "$WORKDIR"
