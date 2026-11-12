#!/usr/bin/env bash
# 批量抓取 IACR ePrint 近5年(2022-2026)全部论文元数据
# 用法：bash run_pull.sh          （DELAY=2 bash run_pull.sh 可放慢速度）
# 特性：支持断点续传（已写入 papers.csv 的ID自动跳过）、失败ID自动重试轮次
set -u
cd "$(dirname "$0")"

PY=python3
OUT=papers.csv
FAIL=failed_ids.txt
DELAY=${DELAY:-1.2}
MAXDELAY=${MAXDELAY:-30}

# 各年度ID上限（依据 eprint.iacr.org/<year>/ 列表页枚举结果）
RANGES=(
  2022/1-2022/1781
  2023/1-2023/1973
  2024/1-2024/2100
  2025/1-2025/2340
  2026/1-2026/2160
)

echo "===== 第 1 轮：全量区间 2022-2026（共 10354 个ID）====="
date '+%F %T'
$PY fetch_iacr.py "${RANGES[@]}" --out "$OUT" --delay "$DELAY" --max-delay "$MAXDELAY" --fail-log "$FAIL"

for round in 2 3 4 5; do
  if [ ! -s "$FAIL" ]; then
    echo "===== 无失败ID，抓取结束 ====="
    break
  fi
  n=$(sort -u "$FAIL" | wc -l)
  echo "===== 第 $round 轮：重试 $n 个失败的ID ====="
  date '+%F %T'
  sort -u "$FAIL" > failed_retry.txt
  : > "$FAIL"
  $PY fetch_iacr.py --file failed_retry.txt --out "$OUT" --delay "$DELAY" \
      --max-delay "$MAXDELAY" --fail-log "$FAIL"
done

echo "===== 全部结束 ====="
date '+%F %T'
echo "CSV 行数（含表头）：$(wc -l < "$OUT")"
