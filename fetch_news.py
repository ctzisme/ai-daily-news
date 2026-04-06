#!/usr/bin/env python3
"""
AI 晚报生成器 v3
数据源：Hacker News · GitHub Trending · AI 官方博客 RSS
输出：HTML（浏览器阅读）+ Markdown（Obsidian 存档）+ JSON（评分系统）
"""

import os, json, hashlib, webbrowser, httpx, feedparser, threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from openai import OpenAI

BASE_DIR  = Path(__file__).parent
load_dotenv(BASE_DIR / '.env')

API_KEY      = os.getenv('API_KEY')
API_BASE_URL = os.getenv('API_BASE_URL')
API_MODEL    = os.getenv('API_MODEL')
WIKI_PATH    = Path(os.getenv('WIKI_PATH', '~/wiki')).expanduser()
DATA_DIR     = BASE_DIR / 'data'
REPORTS_DIR  = BASE_DIR / 'reports'
WEIGHTS_FILE = BASE_DIR / 'weights.json'
CUTOFF_HOURS = 48

RSS_FEEDS = [
    ('Anthropic',       'https://www.anthropic.com/rss.xml'),
    ('OpenAI Blog',     'https://openai.com/blog/rss.xml'),
    ('Google DeepMind', 'https://deepmind.google/blog/rss/'),
    ('Hugging Face',    'https://huggingface.co/blog/feed.xml'),
    ('MIT Tech Review', 'https://www.technologyreview.com/feed/'),
    ('The Verge AI',    'https://www.theverge.com/ai-artificial-intelligence/rss/index.xml'),
]

# ─── 数据抓取 ────────────────────────────────────────────────────

def load_weights() -> dict:
    if WEIGHTS_FILE.exists():
        return json.loads(WEIGHTS_FILE.read_text())
    return {"sources": {}, "tags": {}}

def fetch_hacker_news() -> list:
    items, seen = [], set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)
    queries = ['AI LLM', 'language model', 'AI agent', 'machine learning', 'artificial intelligence', 'OpenAI', 'Anthropic']
    with httpx.Client(timeout=15) as client:
        for query in queries:
            try:
                r = client.get('https://hn.algolia.com/api/v1/search', params={
                    'query': query, 'tags': 'story', 'hitsPerPage': 20,
                    'numericFilters': f'created_at_i>{int(cutoff.timestamp())}',
                })
                for h in r.json().get('hits', []):
                    title = h.get('title', '')
                    if title.lower() in seen or h.get('points', 0) < 20:
                        continue
                    seen.add(title.lower())
                    items.append({
                        'source': 'Hacker News', 'title': title,
                        'url': h.get('url') or f"https://news.ycombinator.com/item?id={h['objectID']}",
                        'points': h.get('points', 0), 'comments': h.get('num_comments', 0),
                        'date': h.get('created_at', '')[:10],
                    })
            except Exception as e:
                print(f"  HN '{query}' 失败: {e}")
    items.sort(key=lambda x: x['points'] + x['comments'] * 2, reverse=True)
    return items[:25]

def fetch_github_trending() -> list:
    items = []
    ai_kw = {'ai','llm','gpt','neural','ml','deep learning','agent','diffusion',
              'transformer','inference','fine-tun','rag','embedding','claude','gemini','copilot','nlp'}
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        try:
            r = client.get('https://github.com/trending', params={'since': 'daily'},
                           headers={'Accept-Language': 'en-US,en;q=0.9'})
            soup = BeautifulSoup(r.text, 'lxml')
            for repo in soup.select('article.Box-row')[:40]:
                name_tag = repo.select_one('h2 a')
                if not name_tag:
                    continue
                name = name_tag.get('href', '').strip('/')
                desc = (repo.select_one('p') or type('', (), {'get_text': lambda self, **k: ''})()).get_text(strip=True)
                stars_tag = repo.select_one('a[href$="/stargazers"]')
                stars = stars_tag.get_text(strip=True) if stars_tag else '0'
                today_tag = repo.select_one('span.d-inline-block.float-sm-right')
                today_stars = today_tag.get_text(strip=True) if today_tag else ''
                lang_tag = repo.select_one('[itemprop="programmingLanguage"]')
                lang = lang_tag.get_text(strip=True) if lang_tag else ''
                if any(kw in (name + ' ' + desc).lower() for kw in ai_kw):
                    items.append({
                        'source': 'GitHub Trending', 'name': name, 'description': desc,
                        'stars': stars, 'today_stars': today_stars, 'language': lang,
                        'url': f'https://github.com/{name}',
                    })
        except Exception as e:
            print(f"  GitHub Trending 失败: {e}")
    return items[:15]

def fetch_rss_feeds() -> list:
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            count = 0
            for entry in feed.entries[:8]:
                pub = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if pub and pub < cutoff:
                    continue
                summary = ''
                if hasattr(entry, 'summary'):
                    summary = BeautifulSoup(entry.summary, 'lxml').get_text()[:300].strip()
                items.append({
                    'source': source, 'title': entry.get('title', ''),
                    'url': entry.get('link', ''), 'summary': summary,
                    'date': str(pub.date()) if pub else '',
                })
                count += 1
            if count:
                print(f"  {source}: {count} 篇")
        except Exception as e:
            print(f"  {source} 失败: {e}")
    return items

# ─── LLM 生成报告 ────────────────────────────────────────────────

def generate_report_json(hn: list, gh: list, rss: list, weights: dict) -> dict:
    llm = OpenAI(api_key=API_KEY, base_url=API_BASE_URL)
    today = datetime.now().strftime('%Y-%m-%d')

    hn_text = '\n'.join([f"[HN {i['points']}分/{i['comments']}评] {i['title']} | {i['url']}" for i in hn[:20]])
    gh_text = '\n'.join([f"[⭐{i['stars']} +{i['today_stars']}] {i['name']}: {i['description']} | {i['url']}" for i in gh])
    rss_text = '\n\n'.join([f"[{i['source']}] {i['title']}\n{i['summary']}\n链接: {i['url']}" for i in rss])

    # 根据权重提示 LLM
    source_weights = weights.get('sources', {})
    priority_hint = '，'.join([f"{k}(权重{v})" for k, v in sorted(source_weights.items(), key=lambda x: -x[1])[:5]])

    prompt = f"""你是 AI 晚报编辑。根据以下原始数据生成今日晚报，输出严格的 JSON 格式，不要加任何其他文字。

今日日期：{today}
信息源优先级：{priority_hint}

【Hacker News AI 热帖】
{hn_text}

【GitHub 今日热门 AI 项目】
{gh_text}

【AI 公司官方博客】
{rss_text}

输出 JSON 格式如下（严格遵守，不加注释）：
{{
  "date": "{today}",
  "overview": "2-3句话概览今日最重要动态",
  "major_companies": [
    {{
      "id": "取标题前20字的md5前6位",
      "source": "来源公司名",
      "title": "简洁中文标题",
      "url": "原始链接",
      "interpretation": "150-250字深度解读，包含：①这是什么（一句话定性）②具体发布/更新了什么内容（核心功能或变化点）③对开发者/用户/行业意味着什么（实际影响）④你的判断：值得深入关注还是观望",
      "tags": ["标签1", "标签2"],
      "priority": "S或A或B"
    }}
  ],
  "open_source": [
    {{
      "id": "取标题前20字的md5前6位",
      "source": "GitHub Trending",
      "title": "项目名",
      "url": "GitHub链接",
      "stars": "star数",
      "today_stars": "今日新增",
      "interpretation": "120-200字深度解读，包含：①这个项目解决什么核心问题②技术亮点或差异化是什么③适合哪类开发者/场景使用④为什么现在值得关注（时机、趋势、背书等）",
      "tags": ["标签1", "标签2"]
    }}
  ],
  "new_tech": [
    {{
      "id": "取标题前20字的md5前6位",
      "source": "来源",
      "title": "技术/概念名称",
      "url": "链接",
      "interpretation": "150-250字深度解读，包含：①这项技术/概念是什么②它解决了什么现有方案解决不好的问题③核心原理或机制简述④目前成熟度如何（实验室/工程可用/生产级）⑤对 AI 工程实践的潜在影响",
      "tags": ["标签1", "标签2"]
    }}
  ]
}}

写作规范：
- Anthropic 内容优先级最高，必须排在 major_companies 第一位（如有）
- 解读是提炼不是翻译，帮读者快速判断这条信息是否值得深入
- 每模块最多8条，宁缺毋滥
- 全部中文，专有名词保留英文
- id 用 python hashlib.md5(title.encode()).hexdigest()[:6] 的方式生成"""

    resp = llm.chat.completions.create(
        model=API_MODEL,
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 尝试提取 JSON 块
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise

# ─── 渲染 HTML ───────────────────────────────────────────────────

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 晚报 · {date}</title>
<style>
  :root {{
    --bg: #0e0e10; --card: #18181b; --border: #27272a;
    --text: #e4e4e7; --muted: #71717a; --link: #60a5fa;
    --s-badge: #f59e0b; --a-badge: #34d399; --b-badge: #94a3b8;
    --tag-bg: #27272a; --tag-text: #a1a1aa;
    --module1: #f59e0b; --module2: #34d399; --module3: #818cf8;
    --overview-bg: #1c1917;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, "PingFang SC", sans-serif; line-height: 1.7; }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  header {{ border-bottom: 1px solid var(--border); padding: 28px 0 20px; text-align: center; }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -0.5px; }}
  header .meta {{ color: var(--muted); font-size: 0.85rem; margin-top: 4px; }}

  .container {{ max-width: 780px; margin: 0 auto; padding: 0 20px 60px; }}

  .overview {{ background: var(--overview-bg); border: 1px solid var(--border); border-radius: 10px;
               padding: 18px 22px; margin: 28px 0; font-size: 0.95rem; color: #d4d4d8; }}
  .overview strong {{ color: var(--text); }}

  .module {{ margin-top: 36px; }}
  .module-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }}
  .module-header h2 {{ font-size: 1.05rem; font-weight: 600; }}
  .module-line {{ flex: 1; height: 1px; background: var(--border); }}
  .m1 h2 {{ color: var(--module1); }} .m1 .module-line {{ background: #78350f; }}
  .m2 h2 {{ color: var(--module2); }} .m2 .module-line {{ background: #064e3b; }}
  .m3 h2 {{ color: var(--module3); }} .m3 .module-line {{ background: #312e81; }}

  .news-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px;
                padding: 18px 20px; margin-bottom: 12px; transition: border-color 0.2s; }}
  .news-card:hover {{ border-color: #3f3f46; }}

  .card-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }}
  .card-title {{ font-size: 0.97rem; font-weight: 600; flex: 1; }}
  .card-title a {{ color: var(--text); }}
  .card-title a:hover {{ color: var(--link); text-decoration: none; }}

  .badge {{ font-size: 0.7rem; font-weight: 700; padding: 2px 8px; border-radius: 4px;
            white-space: nowrap; letter-spacing: 0.3px; flex-shrink: 0; }}
  .badge-S {{ background: #78350f; color: var(--s-badge); }}
  .badge-A {{ background: #064e3b; color: var(--a-badge); }}
  .badge-B {{ background: #1e293b; color: var(--b-badge); }}
  .badge-default {{ background: var(--tag-bg); color: var(--tag-text); }}

  .interpretation {{ font-size: 0.875rem; color: #a1a1aa; margin-bottom: 12px; line-height: 1.65; }}
  .interpretation-label {{ color: #52525b; font-size: 0.78rem; margin-bottom: 4px; }}

  .card-footer {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
  .tags {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .tag {{ background: var(--tag-bg); color: var(--tag-text); font-size: 0.72rem; padding: 2px 8px; border-radius: 4px; }}
  .card-meta {{ display: flex; align-items: center; gap: 12px; }}
  .source-label {{ color: var(--muted); font-size: 0.78rem; }}
  .orig-link {{ font-size: 0.78rem; color: var(--muted); }}
  .orig-link:hover {{ color: var(--link); }}
  .stars {{ font-size: 0.78rem; color: var(--muted); }}

  .rating-row {{ display: flex; align-items: center; gap: 10px; margin-top: 14px;
                 padding-top: 12px; border-top: 1px solid var(--border); }}
  .rating-label {{ font-size: 0.75rem; color: var(--muted); white-space: nowrap; }}
  .star-group {{ display: flex; gap: 1px; }}
  .star-btn {{ background: none; border: none; cursor: pointer; font-size: 1.25rem;
               color: #3f3f46; padding: 0 3px; line-height: 1; transition: color 0.1s, transform 0.1s; }}
  .star-btn:hover {{ transform: scale(1.2); }}
  .star-btn.lit {{ color: #f59e0b; }}
  .rating-saved {{ font-size: 0.75rem; color: #34d399; margin-left: 4px; opacity: 0;
                   transition: opacity 0.4s; }}
  .rating-saved.show {{ opacity: 1; }}

  footer {{ text-align: center; color: var(--muted); font-size: 0.8rem; padding: 24px 0; border-top: 1px solid var(--border); margin-top: 40px; }}
</style>
</head>
<body>
<header>
  <div class="container">
    <h1>AI 晚报</h1>
    <div class="meta">{date} &nbsp;·&nbsp; {total} 条资讯</div>
  </div>
</header>

<div class="container">
  <div class="overview"><strong>今日概览</strong><br>{overview}</div>

  <!-- 大厂最新动态 -->
  <div class="module m1">
    <div class="module-header">
      <h2>🏢 大厂最新动态</h2>
      <div class="module-line"></div>
    </div>
    {major_companies_html}
  </div>

  <!-- 热点开源项目 -->
  <div class="module m2">
    <div class="module-header">
      <h2>🛠️ 热点开源项目</h2>
      <div class="module-line"></div>
    </div>
    {open_source_html}
  </div>

  <!-- 新技术发布 -->
  <div class="module m3">
    <div class="module-header">
      <h2>🔬 新技术发布</h2>
      <div class="module-line"></div>
    </div>
    {new_tech_html}
  </div>

</div>

<footer>
  <div class="container">数据来源：Hacker News · GitHub Trending · AI 官方博客 &nbsp;|&nbsp; 生成时间：{generated_at}</div>
</footer>

<script>
const DATE = "{date}";
document.querySelectorAll('.star-group').forEach(group => {{
  const btns = group.querySelectorAll('.star-btn');
  const saved = group.nextElementSibling;

  // 恢复已有评分
  const existing = parseInt(group.dataset.score || '0');
  if (existing) btns.forEach((b, i) => {{ if (i < existing) b.classList.add('lit'); }});

  btns.forEach((btn, idx) => {{
    btn.addEventListener('mouseenter', () => {{
      btns.forEach((b, i) => b.classList.toggle('lit', i <= idx));
    }});
    btn.addEventListener('mouseleave', () => {{
      const cur = parseInt(group.dataset.score || '0');
      btns.forEach((b, i) => b.classList.toggle('lit', i < cur));
    }});
    btn.addEventListener('click', async () => {{
      const score = idx + 1;
      group.dataset.score = score;
      btns.forEach((b, i) => b.classList.toggle('lit', i < score));
      try {{
        await fetch('/rate', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            id: group.dataset.id,
            score,
            title: group.dataset.title,
            source: group.dataset.source,
            module: group.dataset.module,
            tags: group.dataset.tags ? group.dataset.tags.split(',') : [],
            url: group.dataset.url,
            date: DATE,
          }})
        }});
        saved.classList.add('show');
        setTimeout(() => saved.classList.remove('show'), 2000);
      }} catch(e) {{ console.error(e); }}
    }});
  }});
}});
</script>
</body>
</html>'''

def _priority_badge(priority: str) -> str:
    cls = {'S': 'badge-S', 'A': 'badge-A', 'B': 'badge-B'}.get(priority, 'badge-default')
    return f'<span class="badge {cls}">{priority}</span>'

def _tags_html(tags: list) -> str:
    return ''.join(f'<span class="tag">{t}</span>' for t in tags)

def _star_row(item: dict, module: str) -> str:
    iid   = item.get('id', hashlib.md5(item['title'].encode()).hexdigest()[:6])
    tags  = ','.join(item.get('tags', []))
    title = item['title'].replace('"', '&quot;')
    return (f'<div class="rating-row">'
            f'<span class="rating-label">评分</span>'
            f'<div class="star-group" data-id="{iid}" data-source="{item.get("source","")}" '
            f'data-module="{module}" data-tags="{tags}" data-title="{title}" data-url="{item.get("url","")}">'
            f'<button class="star-btn" title="1分">★</button>'
            f'<button class="star-btn" title="2分">★</button>'
            f'<button class="star-btn" title="3分">★</button>'
            f'<button class="star-btn" title="4分">★</button>'
            f'<button class="star-btn" title="5分">★</button>'
            f'</div>'
            f'<span class="rating-saved">已保存 ✓</span>'
            f'</div>')

def render_card_company(item: dict) -> str:
    badge = _priority_badge(item.get('priority', 'B'))
    tags  = _tags_html(item.get('tags', []))
    stars = _star_row(item, 'major_companies')
    return f'''<div class="news-card">
  <div class="card-top">
    <div class="card-title"><a href="{item['url']}" target="_blank">{item['title']}</a></div>
    {badge}
  </div>
  <div class="interpretation-label">📖 解读</div>
  <div class="interpretation">{item.get('interpretation', '')}</div>
  <div class="card-footer">
    <div class="tags">{tags}</div>
    <div class="card-meta">
      <span class="source-label">{item['source']}</span>
      <a class="orig-link" href="{item['url']}" target="_blank">原文 →</a>
    </div>
  </div>
  {stars}
</div>'''

def render_card_opensource(item: dict) -> str:
    tags       = _tags_html(item.get('tags', []))
    stars_info = f"⭐ {item.get('stars','?')}  +{item.get('today_stars','?')} 今日" if item.get('stars') else ''
    stars      = _star_row(item, 'open_source')
    return f'''<div class="news-card">
  <div class="card-top">
    <div class="card-title"><a href="{item['url']}" target="_blank">{item['title']}</a></div>
    <span class="badge badge-default">开源</span>
  </div>
  <div class="interpretation-label">📖 解读</div>
  <div class="interpretation">{item.get('interpretation', '')}</div>
  <div class="card-footer">
    <div class="tags">{tags}</div>
    <div class="card-meta">
      <span class="stars-info">{stars_info}</span>
      <a class="orig-link" href="{item['url']}" target="_blank">GitHub →</a>
    </div>
  </div>
  {stars}
</div>'''

def render_card_tech(item: dict) -> str:
    tags  = _tags_html(item.get('tags', []))
    stars = _star_row(item, 'new_tech')
    return f'''<div class="news-card">
  <div class="card-top">
    <div class="card-title"><a href="{item['url']}" target="_blank">{item['title']}</a></div>
    <span class="badge badge-default">技术</span>
  </div>
  <div class="interpretation-label">📖 解读</div>
  <div class="interpretation">{item.get('interpretation', '')}</div>
  <div class="card-footer">
    <div class="tags">{tags}</div>
    <div class="card-meta">
      <span class="source-label">{item['source']}</span>
      <a class="orig-link" href="{item['url']}" target="_blank">原文 →</a>
    </div>
  </div>
  {stars}
</div>'''

def render_html(report: dict) -> str:
    mc_html  = '\n'.join(render_card_company(i)    for i in report.get('major_companies', []))
    os_html  = '\n'.join(render_card_opensource(i) for i in report.get('open_source', []))
    nt_html  = '\n'.join(render_card_tech(i)        for i in report.get('new_tech', []))
    total    = len(report.get('major_companies',[])) + len(report.get('open_source',[])) + len(report.get('new_tech',[]))
    return HTML_TEMPLATE.format(
        date=report['date'], total=total,
        overview=report.get('overview', ''),
        major_companies_html=mc_html or '<p style="color:#52525b;font-size:.875rem">暂无内容</p>',
        open_source_html=os_html     or '<p style="color:#52525b;font-size:.875rem">暂无内容</p>',
        new_tech_html=nt_html        or '<p style="color:#52525b;font-size:.875rem">暂无内容</p>',
        generated_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
    )

# ─── 渲染 Markdown ────────────────────────────────────────────────

def render_markdown(report: dict) -> str:
    lines = [f"# {report['date']} AI 晚报\n", f"> {report.get('overview', '')}\n"]

    def section(title, items, card_fn):
        if not items:
            return
        lines.append(f"\n## {title}\n")
        for item in items:
            card_fn(item)

    def mc_card(i):
        lines.append(f"### [{i['title']}]({i['url']})")
        lines.append(f"**来源：** {i['source']}  |  **优先级：** {i.get('priority','B')}")
        lines.append(f"\n📖 {i.get('interpretation','')}\n")

    def os_card(i):
        lines.append(f"### [{i['title']}]({i['url']})")
        lines.append(f"**Stars：** {i.get('stars','?')}  |  **今日新增：** {i.get('today_stars','?')}")
        lines.append(f"\n📖 {i.get('interpretation','')}\n")

    def nt_card(i):
        lines.append(f"### [{i['title']}]({i['url']})")
        lines.append(f"**来源：** {i['source']}")
        lines.append(f"\n📖 {i.get('interpretation','')}\n")

    section('🏢 大厂最新动态', report.get('major_companies', []), mc_card)
    section('🛠️ 热点开源项目', report.get('open_source', []),      os_card)
    section('🔬 新技术发布',   report.get('new_tech', []),          nt_card)

    lines.append(f"\n---\n*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    return '\n'.join(lines)

# ─── 本地服务器（支持页面内评分）────────────────────────────────

RATINGS_FILE = BASE_DIR / 'ratings_store.json'
PORT = 8765

def _load_ratings() -> dict:
    return json.loads(RATINGS_FILE.read_text()) if RATINGS_FILE.exists() else {}

def _save_ratings(r: dict):
    RATINGS_FILE.write_text(json.dumps(r, ensure_ascii=False, indent=2))

def make_handler(html_content: str, date_str: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ('/', f'/{date_str}'):
                body = html_content.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            if self.path == '/rate':
                length = int(self.headers.get('Content-Length', 0))
                data   = json.loads(self.rfile.read(length))
                ratings = _load_ratings()
                ratings[data['id']] = {
                    'date': data.get('date'), 'title': data.get('title'),
                    'source': data.get('source'), 'module': data.get('module'),
                    'score': data['score'], 'tags': data.get('tags', []),
                    'url': data.get('url'),
                }
                _save_ratings(ratings)
                # 更新权重
                weights = load_weights()
                save_weights(update_weights(weights, ratings))
                resp = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(resp))
                self.end_headers()
                self.wfile.write(resp)
            else:
                self.send_response(404); self.end_headers()

        def log_message(self, *_): pass  # 静默日志
    return Handler

def update_weights(weights: dict, ratings: dict) -> dict:
    source_scores: dict = {}
    for r in ratings.values():
        s = r.get('source'); sc = r.get('score')
        if s and sc:
            source_scores.setdefault(s, []).append(sc)
    for src, scores in source_scores.items():
        avg = sum(scores) / len(scores)
        cur = weights['sources'].get(src, 1.0)
        weights['sources'][src] = round(max(0.3, min(2.0, cur + (avg - 3) * 0.15)), 2)
    tag_scores: dict = {}
    for r in ratings.values():
        for t in r.get('tags', []):
            tag_scores.setdefault(t, []).append(r.get('score', 3))
    for tag, scores in tag_scores.items():
        avg = sum(scores) / len(scores)
        cur = weights['tags'].get(tag, 1.0)
        weights['tags'][tag] = round(max(0.3, min(2.0, cur + (avg - 3) * 0.1)), 2)
    return weights

# ─── 主流程 ───────────────────────────────────────────────────────

def main():
    date_str = datetime.now().strftime('%Y-%m-%d')
    print(f"{'='*44}")
    print(f"  AI 晚报生成器  {date_str}")
    print(f"{'='*44}\n")

    weights = load_weights()

    print("[1/3] 抓取 Hacker News...")
    hn = fetch_hacker_news()
    print(f"  {len(hn)} 条热帖\n")

    print("[2/3] 抓取 GitHub Trending...")
    gh = fetch_github_trending()
    print(f"  {len(gh)} 个 AI 项目\n")

    print("[3/3] 抓取 RSS 博客...")
    rss = fetch_rss_feeds()
    print(f"  {len(rss)} 篇文章\n")

    print("生成报告中（调用 LLM）...")
    report = generate_report_json(hn, gh, rss, weights)

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f'{date_str}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    html = render_html(report)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / f'{date_str}.html').write_text(html)

    md_dir = WIKI_PATH / 'daily-news'
    md_dir.mkdir(exist_ok=True)
    (md_dir / f'{date_str}.md').write_text(render_markdown(report))

    total = sum(len(report.get(k, [])) for k in ('major_companies','open_source','new_tech'))
    print(f"\n✓ 共 {total} 条资讯，启动本地服务器...")

    port = PORT
    for attempt in range(10):
        try:
            server = HTTPServer(('127.0.0.1', port), make_handler(html, date_str))
            break
        except OSError:
            port += 1
    url = f'http://127.0.0.1:{port}'
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"  浏览器已打开 {url}")
    print(f"  评分直接点页面星星即可，Ctrl+C 退出\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务器已停止，评分已自动保存')

if __name__ == '__main__':
    main()
