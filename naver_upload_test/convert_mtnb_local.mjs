import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { convert } from '../../md-to-naver-blog/packages/core/dist/index.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const inputArg = process.argv[2];
const outputDirArg = process.argv[3];

if (!inputArg) {
  console.error('Usage: node convert_mtnb_local.mjs <post.md> [outputDir]');
  process.exit(1);
}

const inputPath = path.resolve(process.cwd(), inputArg);
const outputDir = outputDirArg
  ? path.resolve(process.cwd(), outputDirArg)
  : path.resolve(__dirname, '..', 'docs', 'naver_mtnb_outputs_20260606');
fs.mkdirSync(outputDir, { recursive: true });

const baseName = path.basename(inputPath, path.extname(inputPath));
const htmlPath = path.join(outputDir, `${baseName}.naver.html`);
const previewPath = path.join(outputDir, `${baseName}.preview.html`);
const metaPath = path.join(outputDir, `${baseName}.result.json`);

const markdown = fs.readFileSync(inputPath, 'utf8');
const result = convert(markdown);
const tags = Array.isArray(result.frontmatter?.tags)
  ? result.frontmatter.tags.map(String)
  : [];
const tagText = tags.map((tag) => `#${tag}`).join(' ');

fs.writeFileSync(htmlPath, result.html, 'utf8');
fs.writeFileSync(
  metaPath,
  JSON.stringify(
    {
      title: result.title,
      source: inputPath,
      htmlFile: htmlPath,
      previewFile: previewPath,
      frontmatter: result.frontmatter,
      errors: result.errors || [],
      blocks: result.blocks?.map((block) => ({ id: block.id, type: block.type })) || [],
    },
    null,
    2,
  ),
  'utf8',
);

const esc = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const previewHtml = `<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${esc(result.title)} - 네이버 복사용 미리보기</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f8fa; color: #222; }
    main { max-width: 860px; margin: 32px auto; padding: 0 20px 48px; }
    section { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 2px 10px rgba(0,0,0,.03); }
    .page-title { font-size: 22px; margin: 0 0 20px; }
    .label { font-size: 13px; color: #667085; font-weight: 700; margin-bottom: 10px; }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    button { border: 1px solid #d0d5dd; background: #fff; border-radius: 8px; padding: 8px 12px; cursor: pointer; font-weight: 600; white-space: nowrap; }
    button:hover { background: #f9fafb; }
    #body-content { border: 1px dashed #d0d5dd; padding: 18px; border-radius: 8px; background: #fff; }
    .hint { color: #667085; line-height: 1.7; font-size: 14px; }
    .text-box { font-size: 16px; line-height: 1.7; }
    code { background: #f2f4f7; padding: 2px 5px; border-radius: 4px; }
    .error { color: #b42318; background: #fff4f3; border-color: #fecdca; }
  </style>
</head>
<body>
<main>
  <h1 class="page-title">네이버 복사용 미리보기</h1>

  ${result.errors?.length ? `<section class="error"><div class="label">변환 경고</div><pre>${esc(JSON.stringify(result.errors, null, 2))}</pre></section>` : ''}

  <section>
    <div class="row">
      <div>
        <div class="label">제목</div>
        <div id="title-content" class="text-box">${esc(result.title)}</div>
      </div>
      <button type="button" onclick="copyText('title-content', this)">제목 복사</button>
    </div>
  </section>

  <section>
    <div class="row">
      <div>
        <div class="label">본문 HTML</div>
        <div class="hint">본문 서식 복사 후 네이버 블로그 본문에 붙여넣기</div>
      </div>
      <button type="button" onclick="copyHtml(this)">본문 서식 복사</button>
    </div>
    <div id="body-content">${result.html}</div>
  </section>

  <section>
    <div class="row">
      <div>
        <div class="label">태그</div>
        <div id="tags-content" class="text-box">${esc(tagText)}</div>
      </div>
      <button type="button" onclick="copyText('tags-content', this)">태그 복사</button>
    </div>
  </section>

  <section>
    <div class="label">사진 처리</div>
    <div class="hint">
      본문 안의 <code>[사진 N 넣을 자리]</code> 위치에 티스토리 원문 사진을 직접 넣고,<br>
      안내 문구와 <code>원문 이미지 URL</code> 줄은 삭제하면 됩니다.
    </div>
  </section>
</main>
<script>
async function copyText(id, btn) {
  const text = document.getElementById(id).textContent;
  await navigator.clipboard.writeText(text);
  flash(btn);
}
async function copyHtml(btn) {
  const el = document.getElementById('body-content');
  const html = el.innerHTML;
  const text = el.innerText;
  const item = new ClipboardItem({
    'text/html': new Blob([html], { type: 'text/html' }),
    'text/plain': new Blob([text], { type: 'text/plain' })
  });
  await navigator.clipboard.write([item]);
  flash(btn);
}
function flash(btn) {
  const old = btn.textContent;
  btn.textContent = '복사됨';
  setTimeout(() => btn.textContent = old, 1400);
}
</script>
</body>
</html>`;

fs.writeFileSync(previewPath, previewHtml, 'utf8');

console.log(`title=${result.title}`);
console.log(`errors=${(result.errors || []).length}`);
console.log(`html=${htmlPath}`);
console.log(`preview=${previewPath}`);
console.log(`meta=${metaPath}`);
