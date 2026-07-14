#!/bin/sh
# test-case-007: CA 信任机制矩阵测试
#
# 设计原则（按 review 意见重做）：
#   - 一个 case 只设置一种 CA 信任机制（一个环境变量，或系统级信任重建），不叠加。
#   - 但每个 case 内部要测**全部 5 种客户端**（curl / git / python3 urllib / pip /
#     裸 python requests），不是挑一部分测——同一个变量下有的客户端成功、有的失败，
#     这本身就是要证明的结果，不是遗漏。
#   - 每个 case 开头先跑一次金丝雀检测，独立于本 case 测的变量，证明 squid 这次
#     确实拦截了目标域名（历史上 Node 场景踩过"squid 根本没拦截，误判为通过"的坑，
#     其他工具也要防同一类误判）。
#
# 用法：
#   sh run_case.sh <case_id>
#   case_id: 00 01 02 03 04 05 06 08
#     07（NODE_EXTRA_CA_CERTS）未纳入：Node 内置 https 模块不读 HTTP_PROXY 环境变量，
#     一旦 runner 网络出口没有强制走 squid（只是约定，没有 NetworkPolicy 兜底），
#     Node 就会绕过 squid 直连公网，导致测试结果和 CA 信任机制完全无关。
#
# 环境变量（可选覆盖默认值）：
#   SQUID_CA      默认 /etc/squid-ca/squid-ca.pem（runner 上由 infra 自动挂载，见
#                 .github/workflows/hello-buildkit.yaml 里的 "Check Squid proxy" 步骤）
#   TARGET_URL    默认 https://pypi.org（squid.conf 里 ssl_bump bump all 覆盖，会被拦截）
#   GIT_TARGET    默认 https://github.com/octocat/Hello-World（squid.conf 里
#                 github.com 不在 splice registry 白名单里，同样会被拦截）

set -u

SQUID_CA="${SQUID_CA:-/etc/squid-ca/squid-ca.pem}"
TARGET_URL="${TARGET_URL:-https://pypi.org}"
GIT_TARGET="${GIT_TARGET:-https://github.com/octocat/Hello-World}"
CASE_ID="${1:-}"
WORKDIR="$(mktemp -d /tmp/env-ca-case.XXXXXX)"
FAIL=0

if [ -z "$CASE_ID" ]; then
  echo "usage: run_case.sh <case_id>" >&2
  exit 2
fi

if [ ! -f "$SQUID_CA" ]; then
  echo "[case ${CASE_ID}] FATAL: $SQUID_CA not found on this runner" >&2
  exit 2
fi

log() { echo "[case ${CASE_ID}] $*"; }

# pip 在 Ubuntu 24.04 / 较新 openEuler 上默认启用 PEP 668
# (externally-managed-environment)，测试场景下需要显式绕过。
PIP_EXTRA_FLAGS=""
if python3 -m pip install --help 2>/dev/null | grep -q "break-system-packages"; then
  PIP_EXTRA_FLAGS="--break-system-packages"
fi

# ---- 金丝雀检测：不管本 case 测的是哪个变量，独立确认 squid 这次真的在拦截 ----
canary_check() {
  issuer=$(curl -s --max-time 10 --cacert "$SQUID_CA" -v -o /dev/null "$TARGET_URL" 2>&1 | grep -i "issuer:")
  case "$issuer" in
    *SquidCacheCA*) log "canary: CANARY_BUMPED ($issuer)" ;;
    *)
      log "canary: CANARY_NOT_BUMPED ($issuer)"
      FAIL=1
      ;;
  esac
}

# ---- 5 种客户端测试，统一返回 OK / FAIL，互不影响、不 abort 整个脚本 ----
test_curl() {
  if curl -fsS --max-time 10 -o /dev/null "$TARGET_URL" 2>"$WORKDIR/curl.err"; then
    echo OK
  else
    echo FAIL
  fi
}

test_git() {
  rm -rf "$WORKDIR/git-clone"
  if git clone --depth 1 "$GIT_TARGET" "$WORKDIR/git-clone" >"$WORKDIR/git.log" 2>&1; then
    echo OK
  else
    echo FAIL
  fi
}

test_python_urllib() {
  if python3 -c "import urllib.request; urllib.request.urlopen('${TARGET_URL}', timeout=10)" >"$WORKDIR/urllib.log" 2>&1; then
    echo OK
  else
    echo FAIL
  fi
}

test_pip() {
  # 用 --force-reinstall --no-deps 强制真实网络往返，避免因为镜像里预装了
  # requests 而 pip 直接打印 "already satisfied" 不发起任何请求就误判通过。
  out=$(python3 -m pip install --no-cache-dir --force-reinstall --no-deps $PIP_EXTRA_FLAGS requests 2>&1)
  echo "$out" >"$WORKDIR/pip.log"
  if echo "$out" | grep -qE "Collecting|Downloading" && echo "$out" | grep -q "Successfully installed"; then
    echo OK
  else
    echo FAIL
  fi
}

test_requests() {
  if python3 -c "import requests; requests.get('${TARGET_URL}', timeout=10)" >"$WORKDIR/requests.log" 2>&1; then
    echo OK
  else
    echo FAIL
  fi
}

run_all_tools() {
  R_CURL=$(test_curl)
  R_GIT=$(test_git)
  R_URLLIB=$(test_python_urllib)
  R_PIP=$(test_pip)
  R_REQ=$(test_requests)
  log "curl=$R_CURL git=$R_GIT python-urllib=$R_URLLIB pip=$R_PIP requests=$R_REQ"
}

# check <label> <actual> <expected>
check() {
  if [ "$2" != "$3" ]; then
    log "MISMATCH $1: expected=$3 actual=$2"
    FAIL=1
  fi
}

case "$CASE_ID" in
  00)
    log "negative control -- no CA trust set up at all"
    canary_check
    run_all_tools
    check curl "$R_CURL" FAIL
    check git "$R_GIT" FAIL
    check python-urllib "$R_URLLIB" FAIL
    check pip "$R_PIP" FAIL
    check requests "$R_REQ" FAIL
    ;;
  01)
    log "SSL_CERT_FILE"
    SSL_CERT_FILE="$SQUID_CA"; export SSL_CERT_FILE
    canary_check
    run_all_tools
    check curl "$R_CURL" OK
    check git "$R_GIT" FAIL       # git 不认 SSL_CERT_FILE，只认 GIT_SSL_CAINFO / 系统信任
    check python-urllib "$R_URLLIB" OK
    check pip "$R_PIP" OK         # pip 自己的 SSL 上下文会叠加读取 OpenSSL 默认路径
    check requests "$R_REQ" FAIL  # 裸 requests 只认 certifi，不看这个变量
    ;;
  02)
    log "SSL_CERT_DIR"
    mkdir -p "$WORKDIR/capath"
    cp "$SQUID_CA" "$WORKDIR/capath/"
    if command -v openssl >/dev/null 2>&1; then
      openssl rehash "$WORKDIR/capath" >/dev/null 2>&1
    fi
    SSL_CERT_DIR="$WORKDIR/capath"; export SSL_CERT_DIR
    canary_check
    run_all_tools
    check curl "$R_CURL" OK
    check git "$R_GIT" FAIL
    check python-urllib "$R_URLLIB" OK
    check pip "$R_PIP" OK
    check requests "$R_REQ" FAIL
    ;;
  03)
    log "CURL_CA_BUNDLE"
    CURL_CA_BUNDLE="$SQUID_CA"; export CURL_CA_BUNDLE
    canary_check
    run_all_tools
    check curl "$R_CURL" OK        # curl-CLI 专属变量
    check git "$R_GIT" FAIL
    check python-urllib "$R_URLLIB" FAIL
    check pip "$R_PIP" FAIL
    check requests "$R_REQ" FAIL
    ;;
  04)
    log "GIT_SSL_CAINFO"
    GIT_SSL_CAINFO="$SQUID_CA"; export GIT_SSL_CAINFO
    canary_check
    # git clone 偶发瞬时（非 SSL 相关）网络错误，最多重试 3 次；SSL 相关失败不重试
    tries=0
    R_GIT=FAIL
    while [ "$tries" -lt 3 ]; do
      rm -rf "$WORKDIR/git-clone"
      if git clone --depth 1 "$GIT_TARGET" "$WORKDIR/git-clone" >"$WORKDIR/git.log" 2>&1; then
        R_GIT=OK
        break
      fi
      if grep -qi "ssl\|certificate" "$WORKDIR/git.log"; then
        break
      fi
      tries=$((tries + 1))
      sleep 3
    done
    R_CURL=$(test_curl)
    R_URLLIB=$(test_python_urllib)
    R_PIP=$(test_pip)
    R_REQ=$(test_requests)
    log "curl=$R_CURL git=$R_GIT python-urllib=$R_URLLIB pip=$R_PIP requests=$R_REQ"
    check curl "$R_CURL" FAIL
    check git "$R_GIT" OK          # git-CLI 专属变量
    check python-urllib "$R_URLLIB" FAIL
    check pip "$R_PIP" FAIL
    check requests "$R_REQ" FAIL
    ;;
  05)
    log "REQUESTS_CA_BUNDLE"
    REQUESTS_CA_BUNDLE="$SQUID_CA"; export REQUESTS_CA_BUNDLE
    canary_check
    run_all_tools
    check curl "$R_CURL" FAIL
    check git "$R_GIT" FAIL
    check python-urllib "$R_URLLIB" FAIL
    check pip "$R_PIP" OK          # pip 内部用 requests 库读取该变量
    check requests "$R_REQ" OK     # requests 库自己读取的变量，覆盖裸调用的盲区
    ;;
  06)
    log "PIP_CERT"
    PIP_CERT="$SQUID_CA"; export PIP_CERT
    canary_check
    run_all_tools
    check curl "$R_CURL" FAIL
    check git "$R_GIT" FAIL
    check python-urllib "$R_URLLIB" FAIL
    check pip "$R_PIP" OK          # pip 专属变量
    check requests "$R_REQ" FAIL
    ;;
  08)
    log "positive control -- system-wide CA trust rebuild"
    if command -v update-ca-certificates >/dev/null 2>&1; then
      cp "$SQUID_CA" /usr/local/share/ca-certificates/squid-ca.crt
      update-ca-certificates
    elif command -v update-ca-trust >/dev/null 2>&1; then
      mkdir -p /etc/pki/ca-trust/source/anchors
      cp "$SQUID_CA" /etc/pki/ca-trust/source/anchors/squid-ca.pem
      update-ca-trust extract
    else
      log "no known system CA trust update command found (neither update-ca-certificates nor update-ca-trust)"
      FAIL=1
    fi
    canary_check
    run_all_tools
    check curl "$R_CURL" OK
    check git "$R_GIT" OK
    check python-urllib "$R_URLLIB" OK
    check pip "$R_PIP" OK
    check requests "$R_REQ" FAIL   # 裸 requests 只认 certifi，系统信任重建也救不了它
    ;;
  *)
    log "unknown case id: $CASE_ID (valid: 00 01 02 03 04 05 06 08)"
    FAIL=1
    ;;
esac

rm -rf "$WORKDIR"

if [ "$FAIL" -ne 0 ]; then
  log "RESULT: FAIL"
  exit 1
fi
log "RESULT: PASS"
