const fs = require('node:fs');
const path = require('node:path');
const { convert } = require('@jjlabsio/md-to-naver-blog');

const inputPath = path.resolve(__dirname, '01_cj_jeju_beer_mtnb.md');
const outputPath = path.resolve(__dirname, '01_cj_jeju_beer_naver.html');
const metaPath = path.resolve(__dirname, '01_cj_jeju_beer_result.json');

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
