#!/bin/bash
# 安装依赖并设置定时任务

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 安装 Python 依赖 ==="
pip3 install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "=== 设置每天 20:00 定时任务 ==="
CRON_JOB="0 20 * * * cd $SCRIPT_DIR && /usr/bin/python3 fetch_news.py >> $SCRIPT_DIR/logs/fetch.log 2>&1"

# 检查是否已存在
if crontab -l 2>/dev/null | grep -q "fetch_news.py"; then
    echo "定时任务已存在，跳过"
else
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "定时任务已添加：每天 20:00 运行"
fi

echo ""
echo "=== 验证定时任务 ==="
crontab -l | grep fetch_news.py

echo ""
echo "✓ 安装完成！"
echo ""
echo "手动运行测试："
echo "  cd $SCRIPT_DIR && python3 fetch_news.py"
