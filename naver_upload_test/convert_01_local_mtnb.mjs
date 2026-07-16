import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { convert } from '../../md-to-naver-blog/packages/core/dist/index.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const inputPath = path.resolve(__dirname, '01_cj_jeju_beer_mtnb.md');
const outputPath = path.resolve(__dirname, '01_cj_jeju_beer_naver_local.html');
const metaPath = path.resolve(__dirname, '01_cj_jeju_beer_result_local.json');

const markdown = fs.readFileSync(inputPath, 'utf8');
const result = convert(markdown);

fs.writeFileSync(outputPath, result.html, 'utf8');
fs.writeFileSync(
  metaPath,
  JSON.stringify(
    {
      title: result.title,
      frontmatter: result.frontmatter,
      errors: result.errors || [],
      blocks: result.blocks?.map((block) => ({ id: block.id, type: block.type })) || [],
      htmlFile: path.basename(outputPath),
    },
    null,
    2,
  ),
  'utf8',
);

console.log(`title=${result.title}`);
console.log(`html=${outputPath}`);
console.log(`meta=${metaPath}`);
console.log(`errors=${(result.errors || []).length}`);
