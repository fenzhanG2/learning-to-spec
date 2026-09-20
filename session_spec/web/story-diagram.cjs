const dagre = require('@dagrejs/dagre');
const { translator } = require('./locale.cjs');
const MarkdownIt = require('markdown-it');
const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false });
const escape = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
const plainText = value => (markdown.parseInline(String(value), {})[0]?.children || []).map(token => token.type === 'softbreak' ? '\n' : token.content).join('');

function wrap(text, capacity) {
  const lines = [];
  let line = '';
  let width = 0;
  for (const token of String(text).match(/[A-Za-z0-9_.\/-]+|[\s\S]/g) || []) {
    for (const piece of token.length > capacity ? Array.from(token) : [token]) {
      if (!line && /^\s+$/.test(piece)) continue;
      const size = Array.from(piece).reduce((total, character) => total + (/[^\x00-\x7f]/.test(character) ? 2 : 1), 0);
      const punctuation = /^[，。；：！？、）\]】』」,.!?;:)]+$/.test(piece);
      if ((width + size > capacity && !punctuation) || piece === '\n') {
        lines.push(line);
        line = '';
        width = 0;
      }
      if (piece !== '\n' && (line || !/^\s+$/.test(piece))) { line += piece; width += size; }
    }
  }
  if (line) lines.push(line);
  return lines;
}

function architectureMarkup(architecture, icon, inline = escape, language = 'zh-CN') {
  if (architecture.decision === 'omit') return '';
  const translate = translator(language);
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setGraph({ rankdir: 'TB', nodesep: 42, edgesep: 30, ranksep: 56, marginx: 25, marginy: 25 });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of architecture.nodes) {
    const title = wrap(plainText(node.title), 26);
    const detail = wrap(plainText(node.detail), 32);
    graph.setNode(node.id, { width: 258, height: 62 + title.length * 24 + detail.length * 21, title, detail });
  }
  architecture.edges.forEach((edge, index) => {
    const label = wrap(plainText(edge.label), 24);
    const width = Math.max(...label.map(line => Array.from(line).reduce((size, character) => size + (/[^\x00-\x7f]/.test(character) ? 14 : 7), 0))) + 16;
    graph.setEdge(edge.from, edge.to, { width, height: label.length * 20 + 8, labelpos: 'c', label }, String(index));
  });
  dagre.layout(graph);
  const size = graph.graph();
  const textLines = (lines, horizontal, vertical, className, lineHeight, anchor = 'start') => `<text class="${className}" text-anchor="${anchor}">${lines.map((line, index) => `<tspan x="${horizontal}" y="${vertical + index * lineHeight}">${escape(line)}</tspan>`).join('')}</text>`;
  const edges = graph.edges().map(key => {
    const layout = graph.edge(key);
    const edge = architecture.edges[Number(key.name)];
    const color = edge.basis === 'observation' ? 'observed' : 'implemented';
    const route = layout.points.map((point, index) => `${index ? 'L' : 'M'}${point.x},${point.y}`).join(' ');
    return `<g class="arch-edge ${color} ${edge.kind === 'feedback' ? 'feedback' : ''}"><path d="${route}" marker-end="url(#arrow-${color})"><title>${escape(edge.label)}</title></path><rect x="${layout.x - layout.width / 2}" y="${layout.y - layout.height / 2}" width="${layout.width}" height="${layout.height}" rx="5"/>${textLines(layout.label, layout.x, layout.y - (layout.label.length - 1) * 10 + 5, 'arch-edge-label', 20, 'middle')}</g>`;
  }).join('');
  const icons = { '入口': 'LogIn', '控制': 'SlidersHorizontal', '处理': 'Cpu', '存储': 'Database', '产物': 'FileOutput', '外部': 'Globe' };
  const nodes = architecture.nodes.map(node => {
    const layout = graph.node(node.id);
    const left = layout.x - layout.width / 2;
    const top = layout.y - layout.height / 2;
    return `<g class="arch-node"><rect x="${left}" y="${top}" width="${layout.width}" height="${layout.height}" rx="10"/><g transform="translate(${left + 18},${top + 16})">${icon(icons[node.role])}</g><text class="arch-role" x="${left + 47}" y="${top + 31}">${escape(translate(node.role))}</text>${textLines(layout.title, left + 18, top + 60, 'arch-node-title', 24)}${textLines(layout.detail, left + 18, top + 60 + layout.title.length * 24, 'arch-node-detail', 21)}</g>`;
  }).join('');
  const names = new Map(architecture.nodes.map(node => [node.id, node.title]));
  const equivalent = architecture.edges.map(edge => `<li><strong>${inline(names.get(edge.from))}</strong> → ${inline(names.get(edge.to))}：${inline(edge.label)}（${translate(edge.basis === 'observation' ? '当次观察' : '实现约定')}${edge.kind === 'feedback' ? '，' + translate('返回/反馈') : ''}）</li>`).join('');
  const responsibilities = architecture.nodes.map(node => `<li><strong>${inline(node.title)}</strong>：${inline(node.detail)}</li>`).join('');
  return `<figure class="architecture"><figcaption>${icon('Workflow')}<span>${escape(architecture.title)}</span><small>${translate(architecture.kind === 'final_artifact' ? '产物架构' : '实现流程')}</small></figcaption><p class="arch-scope">${inline(architecture.scope)}</p><div class="arch-scroll" role="region" tabindex="0" aria-label="${translate('架构图，可横向滚动')}"><svg class="arch-svg" style="min-width:${Math.min(size.width, 850)}px" viewBox="0 0 ${size.width} ${size.height}" role="img" aria-labelledby="arch-title arch-description"><title id="arch-title">${escape(architecture.title)}</title><desc id="arch-description">${escape(architecture.scope)} ${translate('节点职责与连线的文字等价说明在图后。')}</desc><defs>${['implemented', 'observed'].map(tone => `<marker id="arrow-${tone}" class="${tone}" markerWidth="8" markerHeight="8" refX="7" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z"/></marker>`).join('')}</defs>${edges}${nodes}</svg></div><div class="arch-legend"><span class="implemented">— ${translate('实现／配置关系')}</span>${architecture.edges.some(edge => edge.basis === 'observation') ? `<span class="observed">— ${translate('当次观察关系')}</span>` : ''}${architecture.edges.some(edge => edge.kind === 'feedback') ? `<span>┄ ${translate('返回／反馈方向')}</span>` : ''}</div><p class="arch-evidence">${inline(architecture.evidence_summary)}</p><p class="arch-limit">${inline(architecture.limits)}</p><details><summary>${translate('用文字读这张图')}</summary><ul>${responsibilities}</ul><ul>${equivalent}</ul></details></figure>`;
}

module.exports = { architectureMarkup, wrap, plainText };
