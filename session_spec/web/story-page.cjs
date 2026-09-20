const { architectureMarkup } = require('./story-diagram.cjs');
const { translator } = require('./locale.cjs');
const MarkdownIt = require('markdown-it');
const lucide = require('lucide');
const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false });
const escape = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
const icon = name => {
  const nodes = lucide.icons[name];
  if (!nodes) throw new Error('Unknown icon: ' + name);
  return '<svg class="icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + nodes.map(([tag, attributes]) => '<' + tag + ' ' + Object.entries(attributes).map(([key, value]) => key + '="' + escape(value) + '"').join(' ') + ' />').join('') + '</svg>';
};
markdown.renderer.rules.table_open = () => '<div class="table-scroll" role="region" aria-label="技术对照表" tabindex="0"><table>\n';
markdown.renderer.rules.table_close = () => '</table></div>\n';

const styles = `
:root{--paper:#f7f6f2;--surface:#fff;--ink:#242632;--muted:#626472;--line:#dedee5;--accent:#5544a2;--soft:#eeebf7;--green:#28655a;--amber:#865d25}*{box-sizing:border-box}html{scroll-padding-top:28px}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.8 "Segoe UI","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}a{color:var(--accent);text-underline-offset:4px}button,textarea{font:inherit}button{cursor:pointer}.icon{flex-shrink:0;vertical-align:middle}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #ad91ef;outline-offset:4px}header{background:#252633;color:#f9f8fc}.topbar{max-width:1220px;margin:auto;padding:19px 32px;display:flex;justify-content:space-between;align-items:center;gap:20px}.brand{display:flex;align-items:center;gap:11px;font-size:12px;letter-spacing:.13em;color:inherit;text-decoration:none}.actions{display:flex;gap:18px;align-items:center}.actions a{color:#e0dcef;text-decoration:none;font-size:13px}.link-button{border:0;background:none;color:var(--accent);padding:0;display:inline-flex;gap:7px;align-items:center}.topbar .link-button{color:#e0dcef;font-size:13px}.shell{max-width:1220px;margin:auto;padding:0 32px 60px}.hero{padding:54px 0 32px;max-width:960px}.eyebrow{color:var(--accent);font-size:12px;letter-spacing:.08em;margin:0 0 15px;font-weight:600}.hero h1{font-size:clamp(30px,4vw,44px);line-height:1.4;letter-spacing:-.035em;margin:0 0 20px;font-weight:650}.subtitle{font-size:19px;color:var(--muted);line-height:1.85;margin:0;max-width:820px}.route{list-style:none;display:grid;grid-template-columns:repeat(var(--steps),minmax(0,1fr));gap:24px;padding:23px 0 26px;margin:0 0 43px;border-block:1px solid var(--line)}.route li{min-width:0}.route .icon{display:block;color:var(--accent);margin-bottom:10px}.route strong{font-size:14px;display:block}.route small{display:block;color:var(--muted);font-size:12px;margin-top:4px;line-height:1.7}.layout{display:grid;grid-template-columns:minmax(0,810px) 215px;gap:65px}.prose{min-width:0;font-size:17px;line-height:1.95;overflow-wrap:anywhere}.prose p{margin:0 0 21px}.prose h2{font-size:26px;line-height:1.55;margin:0 0 22px;font-weight:650;letter-spacing:-.025em}.prose h3{font-size:19px;line-height:1.6;margin:25px 0 15px}.chapter{padding-top:38px;margin-top:10px}.chapter+.chapter{border-top:1px solid var(--line);margin-top:37px}.chapter-number{font-size:11px;letter-spacing:.15em;color:var(--accent);display:block;margin-bottom:10px}.prose ul,.prose ol{padding-left:25px;margin:14px 0 22px}.prose li{padding:3px 0}.prose blockquote{margin:25px 0;padding:4px 0 4px 22px;border-left:3px solid var(--accent);color:#414250}.prose blockquote p{margin:0}.prose code{background:#ebeaf0;padding:2px 5px;border-radius:3px;font-size:.86em;font-family:Consolas,"Cascadia Code",monospace;overflow-wrap:anywhere}.prose pre{background:#272936;color:#eeeeF4;padding:21px 23px;border-radius:8px;font:13px/1.85 Consolas,monospace;overflow:auto;white-space:pre;margin:24px 0}.prose pre code{background:none;padding:0;color:inherit;font:inherit;overflow-wrap:normal}.table-scroll{overflow-x:auto;margin:22px 0}.prose table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.75;min-width:440px}.prose th{text-align:left;color:var(--ink);border-bottom:2px solid var(--line);font-weight:600}.prose td,.prose th{padding:13px 13px 13px 0;vertical-align:top}.prose td{border-bottom:1px solid var(--line)}.prose details{border-block:1px solid var(--line);margin:24px 0;padding:13px 0;font-size:14px;line-height:1.85;color:var(--muted)}summary{cursor:pointer;color:var(--accent);font-weight:500}details[open] summary{margin-bottom:17px}.sidebar{position:sticky;top:26px;align-self:start;font-size:13px}.sidebar h2{font-size:12px;font-weight:600;letter-spacing:.08em;color:var(--muted);margin:0 0 16px}.sidebar ol{padding:0;list-style:none;margin:0}.sidebar li{margin:0 0 11px}.sidebar a{color:var(--muted);text-decoration:none;line-height:1.7;display:block;padding-left:12px;border-left:2px solid var(--line)}.sidebar a:hover,.sidebar a[aria-current=true]{color:var(--accent);border-color:var(--accent)}.side-note{font-size:12px;margin-top:26px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);line-height:1.8}.outcome{background:var(--soft);padding:20px 23px;margin:28px 0 10px;border-radius:8px;font-size:15px}.outcome strong{display:block;color:var(--accent);font-size:12px;margin-bottom:7px}.outcome p{margin:0}.figure{margin:29px 0 32px;background:#f0eef6;padding:25px;border-radius:10px}.figure figcaption{font-size:14px;font-weight:600;margin:0 0 18px;display:flex;align-items:center;gap:9px}.figure-grid{display:grid;grid-template-columns:repeat(var(--columns),minmax(0,1fr));gap:18px}.figure-step{min-width:0}.figure-step .step-label{font-size:11px;color:var(--accent);display:block;margin-bottom:7px;letter-spacing:.06em}.figure-step strong{display:block;font-size:15px;line-height:1.6;margin-bottom:8px}.figure-step p{font-size:13px;line-height:1.8;color:#545563;margin:0}.figure-step+.figure-step{padding-left:18px;border-left:1px solid #d7d2e7}.figure-note{font-size:12px;line-height:1.8;color:var(--muted);margin:18px 0 0!important}.figure.teal{background:#edf2ef}.figure.teal .step-label{color:var(--green)}.figure.teal .figure-step+.figure-step{border-color:#cbd9d1}.proof{border-top:1px solid var(--line);margin-top:40px;padding-top:23px;font-size:14px}.proof summary{font-size:14px}.footnav{display:flex;justify-content:space-between;gap:20px;align-items:center;border-top:1px solid var(--line);padding-top:26px;margin-top:35px;font-size:14px}.footnote{font-size:12px;color:var(--muted);line-height:1.8;margin:25px 0 0}.agent-dialog{width:min(980px,calc(100vw - 32px));max-height:90vh;border:1px solid var(--line);border-radius:12px;background:var(--paper);color:var(--ink);padding:25px;box-shadow:0 24px 80px #15122240}.agent-dialog::backdrop{background:#18172480}.dialog-top{display:flex;justify-content:space-between;align-items:center;gap:15px}.dialog-top h2{font-size:21px;margin:0}.dialog-note{font-size:13px;color:var(--muted);margin:10px 0 15px}.dialog-controls{display:flex;gap:15px;align-items:center;flex-wrap:wrap;margin-bottom:15px}.button{padding:9px 14px;border-radius:6px;border:1px solid var(--line);color:var(--ink);background:white;display:inline-flex;gap:7px;align-items:center;font-size:14px}.primary{background:var(--accent);border-color:var(--accent);color:white}.agent-source{display:block;width:100%;height:58vh;resize:vertical;border:1px solid var(--line);border-radius:6px;padding:18px;background:white;color:#272936;font:13px/1.7 Consolas,"Microsoft YaHei",monospace;white-space:pre;overflow:auto}.copy-status{font-size:13px;color:var(--green)}.hub{max-width:1040px}.story-cards{display:grid;grid-template-columns:1fr 1fr;gap:30px;margin-top:15px}.story-card{padding:30px;border:1px solid var(--line);border-radius:12px;background:white}.story-card .story-icon{display:block;color:var(--accent);margin-bottom:25px}.story-card h2{font-size:24px;line-height:1.6;margin:0 0 15px}.story-card p{color:var(--muted);font-size:15px;line-height:1.9}.story-card a{display:inline-flex;align-items:center;gap:8px;font-size:14px;margin-top:13px}.story-card .tag{font-size:12px;color:var(--accent);margin-bottom:12px}
@media(max-width:1040px){.layout{grid-template-columns:minmax(0,1fr);max-width:810px;margin:auto}.sidebar{display:none}.hero{padding-top:38px}.shell{max-width:940px}.route{gap:18px}.route small{font-size:12px}}
@media(max-width:600px){.shell{padding:0 20px 35px}.topbar{padding:17px 20px}.actions>a{display:none}.brand{font-size:10px;letter-spacing:.08em}.topbar .link-button{font-size:12px}.hero{padding:30px 0}.hero h1{font-size:30px}.subtitle{font-size:16px}.eyebrow{font-size:11px}.route{grid-template-columns:1fr;padding:18px 0;gap:12px;margin-bottom:27px}.route li{display:grid;grid-template-columns:25px 105px minmax(0,1fr);align-items:center;gap:10px}.route .icon{margin:0;width:18px}.route small{margin:0;font-size:11px}.route strong{font-size:13px}.prose{font-size:16px;line-height:1.95}.prose h2{font-size:23px}.chapter{padding-top:25px}.figure{padding:21px;margin-inline:0}.figure-grid{grid-template-columns:1fr;gap:18px}.figure-step+.figure-step{border-left:0;border-top:1px solid #d7d2e7;padding:17px 0 0}.figure.teal .figure-step+.figure-step{border-top-color:#cbd9d1}.figure-note{font-size:12px}.footnav{flex-wrap:wrap}.agent-dialog{padding:18px}.dialog-top h2{font-size:18px}.agent-source{height:55vh;font-size:12px}.story-cards{grid-template-columns:1fr;gap:20px}.story-card{padding:24px}.story-card h2{font-size:22px}}
@media print{header,.sidebar,.route,.footnav,.agent-dialog{display:none!important}.layout{display:block}.shell{padding:0}.hero{padding-top:0}body{background:white}.prose{font-size:12pt}.chapter,.figure{break-inside:avoid}.prose pre{white-space:pre-wrap}.chapter{break-inside:auto}}
`;

const insightStyles = `
.architecture{margin:30px 0;background:#eff3f1;border:1px solid #d7e1db;border-radius:12px;padding:23px}.architecture figcaption{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;line-height:1.7}.architecture figcaption small{font-size:11px;color:var(--green);margin-left:auto;white-space:nowrap;font-weight:400}.arch-scope{font-size:13px;color:var(--muted);margin:12px 0!important}.arch-scroll{overflow-x:auto}.arch-svg{display:block;width:100%;height:auto}.arch-node rect{fill:#fff;stroke:#c7d5cd;stroke-width:1.2}.arch-role{font-size:12px;fill:#537063}.arch-node-title{font-size:16px;fill:#252633;font-weight:600}.arch-node-detail{font-size:14px;fill:#555e59}.arch-edge>path{fill:none;stroke:currentColor;stroke-width:1.8}.implemented{color:#67559a;fill:#67559a}.observed{color:#227963;fill:#227963}.arch-edge.feedback>path{stroke-dasharray:5 4}.arch-edge rect{fill:#eff3f1;stroke:#eff3f1;stroke-width:4}.arch-edge-label{fill:currentColor;font-size:14px}.arch-legend{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:11px;margin:10px 0 16px}.arch-evidence,.arch-limit{font-size:13px;line-height:1.8;margin:10px 0!important}.arch-limit{color:var(--muted);border-left:2px solid #a4b6aa;padding-left:12px}.architecture details{margin:20px 0 0;padding-bottom:0;border-bottom:0}.takeaway{background:linear-gradient(135deg,#efecf7,#f7f6f2);padding:30px!important;border-radius:12px;border:0!important}.takeaway .chapter-number{display:flex;align-items:center;gap:8px}.takeaway p:last-child{margin-bottom:0}.takeaway h2{font-size:24px}.arch-scroll:focus-visible{outline:3px solid #ad91ef;outline-offset:3px}@media(max-width:600px){.architecture{padding:17px}.architecture figcaption{flex-wrap:wrap}.takeaway{padding:23px!important}}@media print{.arch-scroll{overflow:visible}.arch-svg{min-width:0!important}}
`;

const briefStyles = `
.brief{margin:0 0 30px;padding:27px 30px 25px;background:var(--surface);border:1px solid var(--line);border-radius:12px;font-size:15px;line-height:1.85}.brief-label{display:flex;align-items:center;gap:8px;color:var(--accent);font-size:12px;letter-spacing:.08em;margin-bottom:12px}.brief h2{font-size:24px;margin-bottom:16px}.brief h3{display:flex;align-items:center;gap:8px;font-size:15px;margin:0 0 10px}.brief p{margin:0 0 13px}.brief-background{color:#505360}.brief-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:27px;padding-top:21px;margin-top:19px;border-top:1px solid var(--line)}.brief ul{margin:8px 0 0;padding-left:20px}.brief li{padding:2px 0}.brief-subheading{display:block;font-size:13px;color:var(--accent);margin-top:15px}.brief-boundaries{font-size:14px}.brief-boundaries h3{font-size:14px}.brief-boundaries p:last-child{margin-bottom:0}.brief-unknown{color:var(--muted);font-size:13px}.brief-kind{font-size:11px;color:var(--green);margin-right:7px}.brief .outcome{margin-top:22px;font-size:14px}.brief+.route{margin-bottom:0}.brief+.route+.chapter{margin-top:0}@media(max-width:600px){.brief{padding:22px 20px;font-size:14px}.brief h2{font-size:21px}.brief-grid{grid-template-columns:minmax(0,1fr);gap:21px}.brief+.route{margin-bottom:5px}}@media print{.brief{border:0;padding:0}.brief-grid{gap:20px}}
`;

const interaction = `
(() => {
  const dialog = document.getElementById('agent-dialog');
  const source = document.getElementById('agent-source');
  const status = document.getElementById('copy-status');
  for (const button of document.querySelectorAll('[data-open-agent]')) {
    button.addEventListener('click', () => { status.textContent = ''; dialog.showModal(); });
  }
  document.getElementById('close-agent').addEventListener('click', () => dialog.close());
  document.getElementById('copy-agent').addEventListener('click', () => {
    source.focus();
    source.select();
    try {
      if (!document.execCommand('copy')) throw new Error('Copy unavailable');
      status.textContent = '已复制完整 Markdown，可直接粘贴给 Agent。';
    } catch {
      status.textContent = '已选中全文，请按 Ctrl+C（Mac 为 ⌘C）复制。';
    }
  });
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        for (const link of document.querySelectorAll('.sidebar a')) {
          link.setAttribute('aria-current', String(link.getAttribute('href') === '#' + entry.target.closest('section').id));
        }
      }
    }, { rootMargin: '-5% 0px -65% 0px' });
    for (const heading of document.querySelectorAll('.chapter h2, .brief h2')) observer.observe(heading);
  }
})();`;

function figureMarkup(figure) {
  if (!figure) return '';
  return `<figure class="figure ${figure.tone === 'teal' ? 'teal' : ''}" aria-label="${escape(figure.title)}"><figcaption>${icon(figure.icon || 'Workflow')}${escape(figure.title)}</figcaption><div class="figure-grid" style="--columns:${figure.steps.length}">${figure.steps.map(step => `<div class="figure-step"><span class="step-label">${escape(step.label)}</span><strong>${escape(step.title)}</strong><p>${escape(step.body)}</p></div>`).join('')}</div><p class="figure-note">${escape(figure.note || '')}</p></figure>`;
}

function briefMarkup(brief, translate = translator()) {
  const inline = value => markdown.renderInline(value);
  const list = items => `<ul>${items.map(item => `<li>${inline(item.text)}</li>`).join('')}</ul>`;
  const nonGoals = brief.non_goals.length ? `<div${brief.constraints.length ? '' : ' style="grid-column:1/-1"'}><h3>${icon('Fence')} ${translate('明确不做 · Non-goals')}</h3>${list(brief.non_goals)}</div>` : '';
  const scope = brief.scope.length ? `<strong class="brief-subheading">${translate('本次涉及的范围')}</strong>${list(brief.scope)}` : '';
  const constraints = brief.constraints.length ? `<div${nonGoals ? '' : ' style="grid-column:1/-1"'}><h3>${icon('SlidersHorizontal')} ${translate('限制与前提')}</h3><ul>${brief.constraints.map(item => `<li><span class="brief-kind">${translate(item.kind === 'requirement' ? '用户约束' : '环境前提')}</span>${inline(item.text)}</li>`).join('')}</ul></div>` : '';
  const boundaries = nonGoals || constraints ? `<div class="brief-grid brief-boundaries">${nonGoals}${constraints}</div>` : '';
  return `<section class="brief" id="story-brief" aria-labelledby="brief-title"><span class="brief-label">${icon('Compass')} ${translate('阅读起点')}</span><h2 id="brief-title">${translate('先把这件事说清楚')}</h2><p class="brief-background">${inline(brief.background.text)}</p>
<div class="brief-grid"><div><h3>${icon('CircleHelp')} ${translate('要解决什么')}</h3><p>${inline(brief.problem.text)}</p><strong class="brief-subheading">${translate('希望达到的状态 · Goal')}</strong>${list(brief.goals)}</div><div><h3>${icon('Route')} ${translate('解决思路')}</h3><p>${inline(brief.approach.text)}</p>${scope}</div></div>
${boundaries}
<aside class="outcome"><strong>${translate('这次走到了哪里')}</strong><p>${inline(brief.status.text)}</p></aside></section>`;
}

const readingInteraction = `
(() => {
  if (!('IntersectionObserver' in window)) return;
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      for (const link of document.querySelectorAll('.sidebar a')) {
        link.setAttribute('aria-current', String(link.getAttribute('href') === '#' + entry.target.closest('section').id));
      }
    }
  }, { rootMargin: '-5% 0px -65% 0px' });
  for (const heading of document.querySelectorAll('.chapter h2, .brief h2')) observer.observe(heading);
})();`;

function renderArticle(article, presentation, hubHref, nextHref, nextTitle, insights, brief, markdownFiles = false, hasEvidence = false) {
  const language = presentation.language || 'zh-CN';
  const translate = translator(language);
  markdown.renderer.rules.table_open = () => `<div class="table-scroll" role="region" aria-label="${translate('技术对照表')}" tabindex="0"><table>\n`;
  const localizedInteraction = interaction.replace('已复制完整 Markdown，可直接粘贴给 Agent。', translate('已复制完整 Markdown，可直接粘贴给 Agent。')).replace('已选中全文，请按 Ctrl+C（Mac 为 ⌘C）复制。', translate('已选中全文，请按 Ctrl+C（Mac 为 ⌘C）复制。'));
  const body = article.chapters.map((chapter, index) => {
    const details = (chapter.details || []).map(item => `<details><summary>${escape(item.title)}</summary>${markdown.render(item.markdown)}</details>`).join('');
    const architecture = insights?.architecture;
    const visual = architecture?.decision === 'include' && architecture.after_chapter === chapter.id ? architectureMarkup(architecture, icon, text => markdown.renderInline(text), language) : figureMarkup(presentation.figures?.[chapter.id]);
    return `<section class="chapter" id="${escape(chapter.id)}"><span class="chapter-number">${String(index + 1).padStart(2, '0')}</span><h2>${escape(chapter.title)}</h2>${markdown.render(chapter.markdown)}${visual}${details}</section>`;
  }).join('');
  const closing = insights?.closing;
  const ending = closing ? `<section class="chapter takeaway" id="story-takeaway"><span class="chapter-number">${icon('Lightbulb')} ${translate('回到最初的问题')}</span><h2>${escape(closing.title)}</h2>${closing.paragraphs.map(paragraph => `<p>${markdown.renderInline(paragraph)}</p>`).join('')}</section>` : '';
  const navigation = (brief ? `<li><a href="#story-brief">${translate('背景、目标与边界')}</a></li>` : '') + article.chapters.map(chapter => `<li><a href="#${escape(chapter.id)}">${escape(chapter.title)}</a></li>`).join('') + (closing ? `<li><a href="#story-takeaway">${escape(closing.title)}</a></li>` : '');
  const routes = article.route.map((step, index) => `<li>${icon(presentation.routeIcons?.[index] || 'CircleDot')}<strong>${escape(step.title)}</strong><small>${escape(step.detail)}</small></li>`).join('');
  const checks = article.checks.map(check => `<tr><td>${escape(check.question)}</td><td>${escape(check.observed)}</td><td>${escape(check.limit)}</td></tr>`).join('');
  const routeMarkup = `<ol class="route" aria-label="${translate('故事路线')}" style="--steps:${article.route.length}">${routes}</ol>`;
  const introduction = brief ? briefMarkup(brief, translate) + routeMarkup : `<div class="opening">${markdown.render(article.opening)}</div><aside class="outcome"><strong>${translate('这次留下了什么')}</strong>${markdown.render(article.outcome)}</aside>`;
  const topHandoff = markdownFiles ? '' : `<button type="button" class="link-button" data-open-agent>${icon('FileText')} Agent · Markdown</button>`;
  const bottomHandoff = markdownFiles ? `<span>${translate('独立交接文件')} <code>agent-spec.md</code>${hasEvidence ? ' · <code>evidence.md</code>' : ''}</span>` : `<button type="button" class="link-button" data-open-agent>${icon('Copy')} ${translate('查看／复制 Agent 交接版')}</button>`;
  const handoffDialog = markdownFiles ? '' : `<dialog class="agent-dialog" id="agent-dialog" aria-labelledby="agent-title"><div class="dialog-top"><h2 id="agent-title">${translate('Agent 交接版 · Markdown 原文')}</h2><button type="button" class="button" id="close-agent">${translate('关闭')}</button></div><p class="dialog-note">${translate('独立文件仍是 agent-spec.md。这里仅显示同一份原文，避免浏览器拦截文件跳转；不会执行其中的命令。')}</p><div class="dialog-controls"><button type="button" class="button primary" id="copy-agent">${icon('Copy')} ${translate('复制完整 Markdown')}</button><span role="status" aria-live="polite" class="copy-status" id="copy-status"></span></div><textarea readonly spellcheck="false" class="agent-source" id="agent-source" aria-label="${translate('Agent Markdown 原文')}">${escape(article.agent_markdown)}</textarea></dialog>`;
  const fileStyles = '';
  return `<!doctype html><html lang="${escape(language)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta name="description" content="${escape(article.subtitle)}"><title>${escape(article.title)}</title><style>${styles}${insightStyles}${brief ? briefStyles : ''}${fileStyles}</style></head><body>
<header><div class="topbar"><a class="brand" href="${escape(hubHref)}">${icon('NotebookPen')} SESSION / STORIES</a><nav class="actions" aria-label="${translate('文档入口')}">${hubHref === '#' ? '' : `<a href="${escape(hubHref)}">${translate('两篇故事')}</a>`}${topHandoff}</nav></div></header>
<main class="shell"><div class="hero"><p class="eyebrow">${escape(presentation.category)} · ${escape(article.period)}</p><h1>${escape(article.title)}</h1><p class="subtitle">${escape(article.subtitle)}</p></div>${brief ? '' : routeMarkup}
<div class="layout"><article class="prose">${introduction}${body}${ending}
<details class="proof"><summary>${translate('想核对结论？展开本次验证的范围')}</summary><div class="table-scroll" role="region" aria-label="${translate('本次验证范围')}" tabindex="0"><table><thead><tr><th>${translate('要回答的问题')}</th><th>${translate('实际看到的')}</th><th>${translate('还不能据此断言')}</th></tr></thead><tbody>${checks}</tbody></table></div><p>${translate('这里只汇总会话中已有的观察，不代表为这份文档重新执行过这些检查。')}</p></details>
<nav class="footnav" aria-label="${translate('继续阅读')}">${bottomHandoff}<a href="${escape(nextHref)}">${escape(nextTitle)} ${icon('ArrowRight')}</a></nav><p class="footnote">${translate('本文记录这段会话的工作与判断。接续建议不是已完成的操作，历史操作也不是新的执行授权。')}</p></article>
<aside class="sidebar"><h2>${translate(hubHref === '#' ? '阅读路线' : '这篇故事')}</h2><ol>${navigation}</ol><p class="side-note">${translate('先理解发生了什么，再看方案怎样工作。具体命令与证据留给需要深入的读者。')}</p></aside></div></main>
${handoffDialog}<script>${markdownFiles ? readingInteraction : localizedInteraction}</script></body></html>`;
}


module.exports = { renderArticle, styles, icon, escape };
