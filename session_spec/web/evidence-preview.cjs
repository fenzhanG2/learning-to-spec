const MarkdownIt = require('markdown-it');

function enhanceSplitAgent(html, source, evidence, language, literal = false) {
  const label = (english, chinese) => language.startsWith('zh') ? chinese : english;
  const escape = value => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const parser = new MarkdownIt({ html: false, linkify: false, typographer: false });
  parser.renderer.rules.heading_open = (tokens, index, options, environment, renderer) => {
    const title = tokens[index + 1]?.content || '';
    if (/^E\d{6}$/.test(title)) tokens[index].attrSet('id', title.toLowerCase());
    return renderer.renderToken(tokens, index, options);
  };
  const button = (id, title) => `<button type="button" class="button" id="${id}">${title}</button>`;
  const controls = button('agent-readable', label('Readable view', '阅读视图'))
    + button('agent-raw', label('Markdown source', 'Markdown 原文'))
    + button('show-evidence', label('Evidence (optional)', '证据（按需）'))
    + button('back-to-agent', label('Back to handoff', '返回交接正文'))
    + button('download-agent', label('Save handoff .md', '保存交接 .md'))
    + button('download-evidence', label('Save evidence .md', '保存证据 .md'));
  const preview = `<div class="prose agent-preview" id="agent-preview">${parser.render(source)}</div>`
    + `<div class="prose agent-preview" id="evidence-preview" hidden>${parser.render(evidence)}</div>`
    + `<textarea hidden readonly spellcheck="false" class="agent-source" id="evidence-source" aria-label="Evidence Markdown">${escape(evidence)}</textarea>`;
  const style = '<style>.agent-preview{font-size:14px;line-height:1.75}.agent-preview h2{margin-top:30px;font-size:22px}.agent-preview h3{font-size:17px}.agent-preview a{overflow-wrap:anywhere}.agent-preview pre{white-space:pre-wrap}.agent-source[hidden],.agent-preview[hidden],#back-to-agent[hidden]{display:none}.agent-dialog{overflow:auto}</style>';
  const script = `<script>(()=>{
    const dialog=document.getElementById('agent-dialog');
    const source=document.getElementById('agent-source');
    const evidence=document.getElementById('evidence-source');
    const preview=document.getElementById('agent-preview');
    const notes=document.getElementById('evidence-preview');
    const back=document.getElementById('back-to-agent');
    const copy=document.getElementById('copy-agent');
    const status=document.getElementById('copy-status');
    let evidenceMode=false, rawMode=false, handoffScroll=0, lastLink=null;
    const show=()=>{
      source.hidden=evidenceMode||!rawMode; preview.hidden=evidenceMode||rawMode;
      evidence.hidden=!evidenceMode||!rawMode; notes.hidden=!evidenceMode||rawMode;
      back.hidden=!evidenceMode;
      copy.textContent=evidenceMode?${JSON.stringify(label('Copy evidence', '复制证据'))}:${JSON.stringify(label('Copy handoff', '复制交接正文'))};
      status.textContent='';
    };
    const openEvidence=anchor=>{
      if(!evidenceMode)handoffScroll=dialog.scrollTop;
      evidenceMode=true;rawMode=false;show();
      if(anchor)document.getElementById(anchor)?.scrollIntoView({block:'start'});else dialog.scrollTop=0;
    };
    const returnToAgent=()=>{evidenceMode=false;rawMode=false;show();dialog.scrollTop=handoffScroll;lastLink?.focus({preventScroll:true});};
    document.querySelectorAll('[data-open-agent]').forEach(button=>button.addEventListener('click',()=>{evidenceMode=false;rawMode=false;show();dialog.showModal();dialog.scrollTop=0;}));
    document.getElementById('close-agent').addEventListener('click',()=>dialog.close());
    document.getElementById('agent-readable').addEventListener('click',()=>{rawMode=false;show();});
    document.getElementById('agent-raw').addEventListener('click',()=>{rawMode=true;show();});
    document.getElementById('show-evidence').addEventListener('click',()=>openEvidence());
    back.addEventListener('click',returnToAgent);
    preview.addEventListener('click',event=>{
      const link=event.target.closest('a');if(!link)return;
      const match=/^evidence\\.md(?:#(e[0-9]{6}))?$/.exec(link.getAttribute('href'));
      if(match){event.preventDefault();lastLink=link;openEvidence(match[1]);}
    });
    notes.addEventListener('click',event=>{const link=event.target.closest('a');if(link?.getAttribute('href')==='agent-spec.md'){event.preventDefault();returnToAgent();}});
    copy.addEventListener('click',async()=>{
      const current=evidenceMode?evidence:source;
      try{await navigator.clipboard.writeText(current.value);status.textContent=evidenceMode?${JSON.stringify(label('Evidence copied separately.', '证据已单独复制。'))}:${JSON.stringify(label('Handoff copied without the evidence appendix.', '已复制交接正文，不含证据附件。'))};}
      catch{rawMode=true;show();current.focus();current.select();status.textContent=${JSON.stringify(label('Selected. Press Ctrl+C to copy.', '已选中，请按 Ctrl+C 复制。'))};}
    });
    const save=(name,content)=>{const url=URL.createObjectURL(new Blob([content],{type:'text/markdown;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);};
    document.getElementById('download-agent').addEventListener('click',()=>save('agent-spec.md',source.value));
    document.getElementById('download-evidence').addEventListener('click',()=>save('evidence.md',evidence.value));
    show();
  })();</script>`;
  const note = label('The handoff and optional evidence are separate Markdown files. Evidence links open here without leaving the story; save both files together for transfer. Historical commands are never executed.', '交接正文与可选证据是两个独立 Markdown 文件。证据链接在此打开，不离开故事；交付时一起保存。不会执行历史命令。');
  if (literal) {
    return html.replace('</head>', () => style + '</head>')
      .replace(/<p class="dialog-note">[\s\S]*?<\/p>/, () => `<p class="dialog-note">${note}</p>`)
      .replace('<div class="dialog-controls">', () => '<div class="dialog-controls">' + controls)
      .replace('<textarea readonly', () => preview + '<textarea hidden readonly')
      .replace(/<script>[\s\S]*?<\/script><\/body><\/html>$/, () => script + '</body></html>');
  }
  return html.replace('</head>', style + '</head>')
    .replace(/<p class="dialog-note">[\s\S]*?<\/p>/, `<p class="dialog-note">${note}</p>`)
    .replace('<div class="dialog-controls">', '<div class="dialog-controls">' + controls)
    .replace('<textarea readonly', preview + '<textarea hidden readonly')
    .replace(/<script>[\s\S]*?<\/script><\/body><\/html>$/, script + '</body></html>');
}

module.exports = { enhanceSplitAgent };
