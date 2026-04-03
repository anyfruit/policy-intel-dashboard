# Policy Intel Dashboard 政策情报仪表盘

自动抓取、分析中国各省市政策文件，提取关键截止日期与标签，并通过 Web 仪表盘呈现。

## 功能

- 多省市政策页面自动抓取（支持搜索引擎回退）
- 中英文政策正文提取与清洗
- 关键截止日期识别
- 政策标签分类
- FastAPI Web 仪表盘 + 认证
- 定时自动更新

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- BeautifulSoup4 / lxml / trafilatura
- SQLite（运行时自动生成）

## 快速开始

```bash
# 1. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动服务
uvicorn auto_scrape:app --reload --port 8000
```

## 项目结构

```
├── auth.py                 # 认证模块
├── auto_scrape.py          # 自动抓取入口
├── cleanup_noise.py        # 噪声清洗
├── cleanup_targeted.py     # 针对性清洗
├── extract_deadlines.py    # 截止日期提取
├── notifier.py             # 通知模块
├── scrape_in_en.py         # 中英文抓取
├── scrape_provinces_via_search.py  # 省级搜索抓取
├── tags.py                 # 标签分类
├── requirements.txt
├── Dockerfile
└── fly.toml                # Fly.io 部署配置
```

## 许可

仅供学习与内部使用。
