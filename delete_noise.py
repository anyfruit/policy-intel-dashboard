#!/usr/bin/env python3
"""一次性：从 seed.db 删除激进模式识别的 24 条噪音，并备份"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

IDS = [5520,5521,5528,5565,5572,5597,5606,5622,5627,5634,5640,5641,
       5646,5647,5653,5657,5661,5662,5665,5670,5687,5711,5715,5716]

qmarks = ",".join(["?"] * len(IDS))
conn = sqlite3.connect("seed.db")
conn.row_factory = sqlite3.Row

# backup
rows = conn.execute(f"SELECT * FROM items WHERE id IN ({qmarks})", IDS).fetchall()
bk_dir = Path("data/cleanup_backups")
bk_dir.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
bk_path = bk_dir / f"noise_backup_aggressive_{ts}.jsonl"
with bk_path.open("w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")

conn.execute(f"DELETE FROM items WHERE id IN ({qmarks})", IDS)
conn.commit()
after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
conn.close()
print(f"已删除 {len(IDS)} 条噪音，备份: {bk_path}")
print(f"剩余 items: {after}")
