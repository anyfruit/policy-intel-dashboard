/* 储能电力政策库 — 前端逻辑 */

/** AI 智能检索（首页） */
async function runAiSearch() {
  const input = document.getElementById('ai-query');
  const btn   = document.getElementById('ai-search-btn');
  const box   = document.getElementById('ai-result');
  if (!input || !box) return;

  const query = input.value.trim();
  if (!query) { input.focus(); return; }

  btn.disabled  = true;
  btn.textContent = '检索中...';
  box.classList.remove('hidden');
  box.innerHTML = `
    <div class="flex items-center gap-3 text-white text-sm py-2">
      <div class="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin shrink-0"></div>
      <span>AI 正在理解您的查询并检索相关政策...</span>
    </div>`;

  try {
    const resp = await fetch('/api/ai/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });

    if (resp.status === 401) { location.href = '/login'; return; }
    if (resp.status === 402) {
      box.innerHTML = `<p class="text-yellow-200 text-sm py-2">⚠️ AI 检索为付费功能，请联系管理员升级账户。</p>`;
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `请求失败 (${resp.status})`);
    }

    const data = await resp.json();
    renderAiResults(data, box);
  } catch (e) {
    box.innerHTML = `<p class="text-red-300 text-sm py-2">❌ 检索失败：${e.message}</p>`;
  } finally {
    btn.disabled  = false;
    btn.textContent = 'AI 搜索';
  }
}

function renderAiResults(data, box) {
  if (!data.items || data.items.length === 0) {
    box.innerHTML = `<p class="text-blue-200 text-sm py-2">未找到相关政策，请尝试其他关键词。</p>`;
    return;
  }

  const relevanceClass = r => {
    if (r === '高') return 'ai-relevance-high';
    if (r === '中') return 'ai-relevance-mid';
    return 'ai-relevance-low';
  };

  const cards = data.items.map(item => `
    <a href="/items/${item.id}" class="ai-result-card block bg-white bg-opacity-10 hover:bg-opacity-20 rounded-lg p-3 transition-all">
      <div class="flex items-start justify-between gap-2 mb-1">
        <span class="text-white text-sm font-medium line-clamp-2 flex-1">${escHtml(item.title)}</span>
        <span class="text-xs shrink-0 ${relevanceClass(item.ai_relevance)} bg-white bg-opacity-20 px-2 py-0.5 rounded-full">${item.ai_relevance || ''}</span>
      </div>
      ${item.ai_key_point ? `<p class="text-blue-200 text-xs mt-1">${escHtml(item.ai_key_point)}</p>` : ''}
      <div class="flex gap-2 mt-1.5 text-xs text-blue-300">
        ${item.region ? `<span>📍 ${escHtml(item.region)}</span>` : ''}
        ${item.date   ? `<span>📅 ${escHtml(item.date)}</span>` : ''}
        ${item.level  ? `<span>${escHtml(item.level)}级</span>` : ''}
      </div>
    </a>`).join('');

  box.innerHTML = `
    ${data.summary ? `<div class="bg-white bg-opacity-10 rounded-lg p-3 mb-3 text-blue-100 text-sm">${escHtml(data.summary)}</div>` : ''}
    <p class="text-blue-200 text-xs mb-2">找到 ${data.total} 条相关政策：</p>
    <div class="space-y-2">${cards}</div>`;
}

/** Enter 键触发 AI 搜索 */
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('ai-query');
  if (input) {
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') runAiSearch();
    });
  }
});

/** HTML 转义，防止 XSS */
function escHtml(str) {
  return (str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
