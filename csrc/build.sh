#!/bin/bash
# Copyright (c) 2024 Huawei Technologies Co., Ltd.
# This file is a part of the CANN Open Software.
# Licensed under CANN Open Software License Agreement Version 1.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# ======================================================================================================================

set -e

CURRENT_DIR=$(dirname $(readlink -f ${BASH_SOURCE[0]}))
BUILD_DIR=${CURRENT_DIR}/build
OUTPUT_DIR=${CURRENT_DIR}/output
USER_ID=$(id -u)
PARENT_JOB="false"
CHECK_COMPATIBLE="true"
ASAN="false"
COV="false"
VERBOSE="false"

if [ "${USER_ID}" != "0" ]; then
    DEFAULT_TOOLKIT_INSTALL_DIR="${HOME}/Ascend/ascend-toolkit/latest"
    DEFAULT_INSTALL_DIR="${HOME}/Ascend/latest"
else
    DEFAULT_TOOLKIT_INSTALL_DIR="/usr/local/Ascend/ascend-toolkit/latest"
    DEFAULT_INSTALL_DIR="/usr/local/Ascend/latest"
fi

CUSTOM_OPTION="-DBUILD_OPEN_PROJECT=ON"

function help_info() {
    echo "Usage: $0 [options]"
    echo "Options:"
    echo
    echo "-h|--help            Displays help message."
    echo
    echo "-n|--op-name         Specifies the compiled operator. If there are multiple values, separate them with semicolons and use quotation marks. The default is all."
    echo "                     For example: -n \"flash_attention_score\" or -n \"flash_attention_score;flash_attention_score_grad\""
    echo
    echo "-c|--compute-unit    Specifies the chip type. If there are multiple values, separate them with semicolons and use quotation marks. The default is ascend910b."
    echo "                     For example: -c \"ascend910b\" or -c \"ascend910b;ascend310p\""
    echo
    echo "--cov                Compiles with cov."
    echo
    echo "--verbose            Displays more compilation information."
    echo
}

function log() {
    local current_time=`date +"%Y-%m-%d %H:%M:%S"`
    echo "[$current_time] "$1
}

function set_env()
{
    source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash || echo "0"

    export BISHENG_REAL_PATH=$(which bisheng || true)

    if [ -z "${BISHENG_REAL_PATH}" ];then
        log "Error: bisheng compilation tool not found, Please check whether the cann package or environment variables are set."
        exit 1
    fi
}

function clean()
{
    if [ -n "${BUILD_DIR}" ];then
        rm -rf ${BUILD_DIR}
    fi
    mkdir -p ${BUILD_DIR} ${OUTPUT_DIR}
}

function cmake_config()
{
    local extra_option="$1"
    log "Info: cmake config ${CUSTOM_OPTION} ${extra_option} ."
    cmake ..  ${CUSTOM_OPTION} ${extra_option}
}

function build()
{
    local target="$1"
    if [ "${VERBOSE}" == "true" ];then
        local option="--verbose"
    fi
    cmake --build . --target ${target} ${JOB_NUM} ${option}
}

function gen_bisheng(){
    local ccache_program=$1
    local gen_bisheng_dir=${BUILD_DIR}/gen_bisheng_dir

    if [ ! -d "${gen_bisheng_dir}" ];then
        mkdir -p ${gen_bisheng_dir}
    fi

    pushd ${gen_bisheng_dir}
    # 使用cat和heredoc创建包装脚本
    # 关键：让sccache包装真实的bisheng编译器
    cat > bisheng << EOF
#!/bin/bash
# Bisheng wrapper script for sccache/ccache
# This script wraps the bisheng compiler to work with caching tools

if [ "${VERBOSE}" == "true" ]; then
    echo "Bisheng wrapper: ${ccache_program} ${BISHENG_REAL_PATH} \$@" >&2
fi

# 使用exec让sccache包装真实的bisheng编译器
# 这样sccache可以正确识别和缓存编译结果
exec "${ccache_program}" "${BISHENG_REAL_PATH}" "\$@"
EOF
    chmod +x bisheng

    export PATH=${gen_bisheng_dir}:$PATH
    popd
}

function build_package(){
    build package
}

function build_host(){
    build_package
}

function build_kernel(){
    build ops_kernel
}

while [[ $# -gt 0 ]]; do
    case $1 in
    -h|--help)
        help_info
        exit
        ;;
    -n|--op-name)
        ascend_op_name="$2"
        shift 2
        ;;
    -c|--compute-unit)
        ascend_compute_unit="$2"
        shift 2
        ;;
    *)
        help_info
        exit 1
        ;;
    esac
done

if [ -n "${ascend_compute_unit}" ];then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DASCEND_COMPUTE_UNIT=${ascend_compute_unit}"
fi

if [ -n "${ascend_op_name}" ];then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DASCEND_OP_NAME=${ascend_op_name}"
fi

if [ -n "${ASCEND_HOME_PATH}" ];then
    ASCEND_CANN_PACKAGE_PATH=${ASCEND_HOME_PATH}
elif [ -n "${ASCEND_OPP_PATH}" ];then
    ASCEND_CANN_PACKAGE_PATH=$(dirname ${ASCEND_OPP_PATH})
elif [ -d "${DEFAULT_TOOLKIT_INSTALL_DIR}" ];then
    ASCEND_CANN_PACKAGE_PATH=${DEFAULT_TOOLKIT_INSTALL_DIR}
elif [ -d "${DEFAULT_INSTALL_DIR}" ];then
    ASCEND_CANN_PACKAGE_PATH=${DEFAULT_INSTALL_DIR}
else
    log "Error: Please set the toolkit package installation directory through parameter -p|--package-path."
    exit 1
fi

if [ "${PARENT_JOB}" == "false" ];then
    CPU_NUM=$(($(cat /proc/cpuinfo | grep "^processor" | wc -l)*2))
    JOB_NUM="-j${CPU_NUM}"
fi

CUSTOM_OPTION="${CUSTOM_OPTION} -DCUSTOM_ASCEND_CANN_PACKAGE_PATH=${ASCEND_CANN_PACKAGE_PATH} -DCHECK_COMPATIBLE=${CHECK_COMPATIBLE}"

set_env
clean

ccache_system=$(which sccache 2>/dev/null || which ccache 2>/dev/null || true)
if [ -n "${ccache_system}" ];then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DENABLE_CCACHE=ON -DCUSTOM_CCACHE=${ccache_system}"

    # 为sccache配置专用环境变量
    if [[ "${ccache_system}" == *"sccache"* ]]; then
        log "Info: Using sccache for compilation caching"

        # 配置sccache环境变量
        export SCCACHE_DIRECT=${SCCACHE_DIRECT:-true}                    # 启用直接模式
        export SCCACHE_IGNORE_SERVER_IO_ERROR=${SCCACHE_IGNORE_SERVER_IO_ERROR:-1}  # 忽略服务器IO错误
        export SCCACHE_CACHE_SIZE=${SCCACHE_CACHE_SIZE:-10G}             # 缓存大小
        export SCCACHE_COMPILER_WRAPPER=${SCCACHE_COMPILER_WRAPPER:-1}   # 编译器包装器检测

        log "Info: sccache configuration:"
        log "  SCCACHE_DIRECT=${SCCACHE_DIRECT}"
        log "  SCCACHE_CACHE_SIZE=${SCCACHE_CACHE_SIZE}"
        log "  SCCACHE_COMPILER_WRAPPER=${SCCACHE_COMPILER_WRAPPER}"
        log "  SCCACHE_IGNORE_SERVER_IO_ERROR=${SCCACHE_IGNORE_SERVER_IO_ERROR}"

        # 显示当前统计信息
        ${ccache_system} --show-stats 2>/dev/null || true
    else
        log "Info: Using ccache for compilation caching"
    fi

    gen_bisheng ${ccache_system}
fi

cd ${BUILD_DIR}
cmake_config
build_package

# 显示缓存统计信息
if [ -n "${ccache_system}" ];then
    log "Info: Build completed. Cache statistics:"
    if [[ "${ccache_system}" == *"sccache"* ]]; then
        ${ccache_system} --show-stats
        log "Info: To see detailed cache analysis, run: sccache --show-stats"
        log "Info: To analyze non-cacheable calls, enable debug logging:"
        log "      export SCCACHE_LOG=trace"
        log "      export SCCACHE_ERROR_LOG=/tmp/sccache.log"
    else
        ${ccache_system} -s
        log "Info: To see detailed cache analysis, run: ccache -s"
    fi
fi
