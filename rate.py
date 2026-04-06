#!/usr/bin/env python3
"""
AI 晚报评分工具
对今日每条资讯评分（1-5），系统自动更新信息源权重
"""

import json, sys
from datetime import datetime
from pathlib import Path

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / 'data'
WEIGHTS_FILE  = BASE_DIR / 'weights.json'
RATINGS_FILE  = BASE_DIR / 'ratings_store.json'

STARS = {1: '★☆☆☆☆', 2: '★★☆☆☆', 3: '★★★☆☆', 4: '★★★★☆', 5: '★★★★★'}
COLORS = {
    'reset': '\033[0m', 'bold': '\033[1m', 'dim': '\033[2m',
    'yellow': '\033[93m', 'green': '\033[92m', 'cyan': '\033[96m',
    'gray': '\033[90m', 'red': '\033[91m', 'blue': '\033[94m',
}

def c(color, text): return f"{COLORS.get(color,'')}{text}{COLORS['reset']}"

def load_ratings() -> dict:
    if RATINGS_FILE.exists():
        return json.loads(RATINGS_FILE.read_text())
    return {}

def save_ratings(ratings: dict):
    RATINGS_FILE.write_text(json.dumps(ratings, ensure_ascii=False, indent=2))

def load_weights() -> dict:
    if WEIGHTS_FILE.exists():
        return json.loads(WEIGHTS_FILE.read_text())
    return {"sources": {}, "tags": {}}

def save_weights(weights: dict):
    weights['updated_at'] = datetime.now().strftime('%Y-%m-%d')
    WEIGHTS_FILE.write_text(json.dumps(weights, ensure_ascii=False, indent=2))

def update_weights(weights: dict, ratings: dict) -> dict:
    """根据历史评分动态调整 source 权重"""
    # 统计每个 source 的平均分
    source_scores: dict = {}
    for item_id, record in ratings.items():
        source = record.get('source')
        score  = record.get('score')
        if source and score:
            if source not in source_scores:
                source_scores[source] = []
            source_scores[source].append(score)

    for source, scores in source_scores.items():
        avg = sum(scores) / len(scores)
        # 平均分 3 → 权重不变；5 → +0.3；1 → -0.3；线性插值
        adjustment = (avg - 3) * 0.15
        current = weights['sources'].get(source, 1.0)
        new_weight = round(max(0.3, min(2.0, current + adjustment)), 2)
        weights['sources'][source] = new_weight

    # 同样对 tags 做统计
    tag_scores: dict = {}
    for item_id, record in ratings.items():
        for tag in record.get('tags', []):
            if tag not in tag_scores:
                tag_scores[tag] = []
            tag_scores[tag].append(record.get('score', 3))

    for tag, scores in tag_scores.items():
        avg = sum(scores) / len(scores)
        adjustment = (avg - 3) * 0.1
        current = weights['tags'].get(tag, 1.0)
        weights['tags'][tag] = round(max(0.3, min(2.0, current + adjustment)), 2)

    return weights

def rate_today(date_str: str = None):
    date_str = date_str or datetime.now().strftime('%Y-%m-%d')
    json_path = DATA_DIR / f'{date_str}.json'

    if not json_path.exists():
        print(c('red', f'找不到 {date_str} 的数据文件：{json_path}'))
        print('请先运行 fetch_news.py 生成今日晚报')
        sys.exit(1)

    report  = json.loads(json_path.read_text())
    ratings = load_ratings()
    weights = load_weights()

    # 收集所有 items
    all_items = []
    for item in report.get('major_companies', []):
        all_items.append({**item, 'module': 'major_companies', 'module_label': '大厂动态'})
    for item in report.get('open_source', []):
        all_items.append({**item, 'module': 'open_source', 'module_label': '开源项目'})
    for item in report.get('new_tech', []):
        all_items.append({**item, 'module': 'new_tech', 'module_label': '新技术'})

    if not all_items:
        print(c('yellow', '今日报告暂无内容'))
        return

    print(f"\n{c('bold', '='*50)}")
    print(f"  {c('cyan', 'AI 晚报评分')}  {date_str}  共 {len(all_items)} 条")
    print(f"{c('bold', '='*50)}")
    print(c('gray', "  输入 1-5 评分，回车跳过，q 退出并保存\n"))

    new_ratings = 0
    for i, item in enumerate(all_items, 1):
        item_id = item.get('id', item['title'][:8])
        already = ratings.get(item_id, {}).get('score')

        # 模块标题分隔
        if i == 1 or all_items[i-2].get('module') != item.get('module'):
            module_colors = {'major_companies': 'yellow', 'open_source': 'green', 'new_tech': 'blue'}
            mc = module_colors.get(item['module'], 'cyan')
            print(f"\n  {c(mc, '── ' + item['module_label'] + ' ──')}\n")

        # 显示条目
        already_str = f" {c('gray', f'(已评:{STARS[already]})')}" if already else ''
        print(f"  {c('bold', str(i)+'.')} {item['title']}{already_str}")
        print(f"     {c('gray', item.get('interpretation','')[:160] + '...' if len(item.get('interpretation','')) > 160 else item.get('interpretation',''))}")
        print(f"     {c('gray', item.get('source',''))}  {c('cyan', item.get('url','')[:60])}")

        try:
            raw = input(f"     评分 [{c('yellow','1-5')}，回车跳过]：").strip()
        except (KeyboardInterrupt, EOFError):
            print('\n\n已中断，保存已有评分')
            break

        if raw.lower() == 'q':
            break
        if raw in ('1','2','3','4','5'):
            score = int(raw)
            ratings[item_id] = {
                'date': date_str, 'title': item['title'], 'source': item.get('source',''),
                'module': item['module'], 'score': score,
                'tags': item.get('tags', []), 'url': item.get('url',''),
            }
            print(f"     {c('green', '✓')} {STARS[score]}\n")
            new_ratings += 1
        else:
            print()

    # 保存评分 + 更新权重
    save_ratings(ratings)
    new_weights = update_weights(weights, ratings)
    save_weights(new_weights)

    print(f"\n{c('bold', '─'*50)}")
    print(f"  {c('green', f'✓ 保存 {new_ratings} 条新评分')}")
    print(f"\n  权重更新（Top 来源）：")
    top_sources = sorted(new_weights['sources'].items(), key=lambda x: -x[1])[:6]
    for src, w in top_sources:
        bar = '█' * int(w * 5)
        print(f"    {src:<20} {bar:<10} {w}")
    print()

if __name__ == '__main__':
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    rate_today(date_arg)
