const fs = require('node:fs');
const path = require('node:path');
const { renderArticle } = require('./story-page.cjs');
const { translator } = require('./locale.cjs');
const input = path.resolve(process.argv[2]);
const output = path.resolve(process.argv[3]);
const read = filename => JSON.parse(fs.readFileSync(path.join(input, filename), 'utf8'));
const article = read('article.json');
const language = fs.existsSync(path.join(input, 'language.json')) ? read('language.json').language : 'zh-CN';
const translate = translator(language);
const agentPresentation = fs.existsSync(path.join(input, 'agent-presentation.json')) ? read('agent-presentation.json') : {};
const markdownFiles = agentPresentation.schema === 'agent-presentation/v2' && agentPresentation.mode === 'markdown-files';
if (!markdownFiles && fs.existsSync(path.join(input, 'agent-rendered.md'))) article.agent_markdown = fs.readFileSync(path.join(input, 'agent-rendered.md'), 'utf8');
const presentationPath = path.join(input, 'presentation.json');
const presentation = fs.existsSync(presentationPath) ? read('presentation.json') : { category: translate('一段真实的技术工作'), routeIcons: ['Compass', 'Route', 'Workflow', 'PackageCheck', 'Flag'] };
presentation.language = language;
const brief = fs.existsSync(path.join(input, 'brief.json')) ? read('brief.json') : undefined;
const evidencePath = path.join(input, 'evidence-rendered.md');
const html = renderArticle(article, presentation, '#', '#story-takeaway', translate('回看这次留下的认识'), read('insights.json'), brief, markdownFiles, fs.existsSync(evidencePath))
  .replace('<a href="#">两篇故事</a>', '')
  .replace('>这篇故事</h2>', '>阅读路线</h2>');
if (markdownFiles) {
  fs.writeFileSync(output, html);
} else {
  const { enhanceAgent } = require('./agent-preview.cjs');
  const { enhanceSplitAgent } = require('./evidence-preview.cjs');
  const literalEvidence = agentPresentation.evidence_substitution === 'literal';
  const split = article.agent_detail?.schema === 'agent-detail/v3' && article.agent_markdown.includes('](evidence.md') && fs.existsSync(evidencePath);
  fs.writeFileSync(output, split ? enhanceSplitAgent(html, article.agent_markdown, fs.readFileSync(evidencePath, 'utf8'), language, literalEvidence)
    : article.agent_detail?.schema === 'agent-detail/v3' ? enhanceAgent(html, article.agent_markdown, language) : html);
}
