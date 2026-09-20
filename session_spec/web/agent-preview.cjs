const MarkdownIt = require('markdown-it');

function enhanceAgent(html, source, language) {
  const chinese = language.startsWith('zh');
  const label = (english, translated) => chinese ? translated : english;
  const parser = new MarkdownIt({ html: false, linkify: false, typographer: false });
  parser.renderer.rules.heading_open = (tokens, index, options, environment, renderer) => {
    const title = tokens[index + 1]?.content || '';
    if (/^E\d{6}$/.test(title)) tokens[index].attrSet('id', title.toLowerCase());
    return renderer.renderToken(tokens, index, options);
  };
  const preview = `<div class="prose agent-preview" id="agent-preview">${parser.render(source)}</div>`;
  const switcher = `<button type="button" class="button" id="agent-readable">${label('Readable view', '阅读视图')}</button><button type="button" class="button" id="agent-raw">${label('Markdown source', 'Markdown 原文')}</button>`;
  const style = '<style>.agent-preview{font-size:14px;line-height:1.75}.agent-preview h2{margin-top:30px;font-size:22px}.agent-preview h3{font-size:17px}.agent-preview a{overflow-wrap:anywhere}.agent-preview pre{white-space:pre-wrap}.agent-source[hidden],.agent-preview[hidden]{display:none}.agent-dialog{overflow:auto}</style>';
  const script = `<script>(()=>{const dialog=document.getElementById('agent-dialog');const source=document.getElementById('agent-source');const preview=document.getElementById('agent-preview');const show=raw=>{source.hidden=!raw;preview.hidden=raw;};document.querySelectorAll('[data-open-agent]').forEach(button=>button.addEventListener('click',()=>{show(false);dialog.showModal();}));document.getElementById('close-agent').addEventListener('click',()=>dialog.close());document.getElementById('agent-readable').addEventListener('click',()=>show(false));document.getElementById('agent-raw').addEventListener('click',()=>show(true));preview.addEventListener('click',event=>{const link=event.target.closest('a');if(!link)return;const href=link.getAttribute('href');if(/^#e[0-9]{6}$/.test(href)){event.preventDefault();document.getElementById(href.slice(1))?.scrollIntoView({block:'start'});}});document.getElementById('copy-agent').addEventListener('click',async()=>{const status=document.getElementById('copy-status');try{await navigator.clipboard.writeText(source.value);status.textContent=${JSON.stringify(label('Copied the complete Markdown, including source notes.', '已复制完整 Markdown，包含来源说明。'))};}catch{show(true);source.focus();source.select();status.textContent=${JSON.stringify(label('Selected the full Markdown. Press Ctrl+C to copy.', '已选中完整 Markdown，请按 Ctrl+C 复制。'))};}});})();</script>`;
  return html.replace('</head>', style + '</head>')
    .replace('<div class="dialog-controls">', '<div class="dialog-controls">' + switcher)
    .replace('<textarea readonly', preview + '<textarea hidden readonly')
    .replace(/<script>[\s\S]*?<\/script><\/body><\/html>$/, script + '</body></html>');
}

module.exports = { enhanceAgent };
