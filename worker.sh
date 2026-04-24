#!/bin/bash
# worker.sh

ip=$1
REMOTE_USER="root"
KEY_PATH="xxx.pem"
COMMAND='curl -so /tmp/collector.py http://<YOUR_SERVER_IP>:8080/tools/collector.py && PY=$(command -v python3 || command -v python || command -v python2) && $PY /tmp/collector.py --server http://<YOUR_SERVER_IP>:8080/api/v1/upload_hwinfo'
TIMEOUT=3

# SSH 参数：注意 -n 和 LogLevel
SSH_OPTS="-i $KEY_PATH -n -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=$TIMEOUT -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o LogLevel=ERROR"

# 尝试 22 端口
OUT22=$(ssh $SSH_OPTS -p 22 ${REMOTE_USER}@$ip "$COMMAND" 2>&1)
STATUS="$?"

if [ $STATUS -eq 0 ]; then
    echo "[SUCCESS] $ip (Port 22)"
    # 将多行输出压成单行，保证多线程并行写日志时不会错乱交叉
    OUT_FLAT=$(echo "$OUT22" | tr '\n' ' ')
    echo "$(date '+%Y-%m-%d %H:%M:%S') [SUCCESS] $ip (Port 22) - $OUT_FLAT" >> scan_success.log
else
    # 尝试 22022 端口
    OUT22022=$(ssh $SSH_OPTS -p 22022 ${REMOTE_USER}@$ip "$COMMAND" 2>&1)
    if [ $? -eq 0 ]; then
        echo "[SUCCESS] $ip (Port 22022)"
        OUT_FLAT=$(echo "$OUT22022" | tr '\n' ' ')
        echo "$(date '+%Y-%m-%d %H:%M:%S') [SUCCESS] $ip (Port 22022) - $OUT_FLAT" >> scan_success.log
    else
        echo "[FAILED] $ip"
        OUT22_FLAT=$(echo "$OUT22" | tr '\n' ' ')
        OUT22022_FLAT=$(echo "$OUT22022" | tr '\n' ' ')
        # 单次写入保证 xargs -P 10 多线程写入时不会穿插
        echo "$(date '+%Y-%m-%d %H:%M:%S') [FAILED] $ip | ERR22: $OUT22_FLAT | ERR22022: $OUT22022_FLAT" >> scan_failed.log
    fi
fi
