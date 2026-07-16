import fs from 'node:fs/promises';
import path from 'node:path';

const ORIGINAL_MD_DIR = 'E:\\git-copilot\\dify-practice\\docs\\naver_migration_original_posts_20260606';
const OUTPUT_DIR = 'E:\\git-copilot\\dify-practice\\docs\\naver_mtnb_outputs_20260606';
const MIN_NO = Number(process.argv[2] ?? 5);

function getPostNo(filename) {
  const match = filename.match(/^(\d+)_/);
  return match ? Number(match[1]) : null;
}

function sanitizeName(name) {
  return name
    .replace(/[<>:"/\\|?*]/g, '_')
    .replace(/\s+/g, '_')
    .slice(0, 100);
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function normalizeUrl(rawUrl, baseUrl) {
  try {
    if (!rawUrl || rawUrl.startsWith('data:')) return null;
    return new URL(rawUrl, baseUrl).toString();
  } catch {
    return null;
  }
}

function extractSourceUrl(md) {
  const match = md.match(/^source_url:\s*(.+)$/m);
  return match ? match[1].trim().replace(/^["']|["']$/g, '') : null;
}

function extractImageUrlsFromMd(md) {
  const urls = [];

  for (const match of md.matchAll(/원문 이미지 URL:\s*(https?:\/\/[^\s]+)/g)) {
    urls.push(match[1].trim());
  }

  for (const match of md.matchAll(/!\[[^\]]*]\(([^)]+)\)/g)) {
    urls.push(match[1].trim());
  }

  return unique(urls);
}

async function fetchHtml(url) {
  const res = await fetch(url, {
    headers: {
      'user-agent': 'Mozilla/5.0',
      accept: 'text/html,application/xhtml+xml',
    },
  });

  if (!res.ok) {
    throw new Error(`HTML 요청 실패: ${res.status} ${res.statusText}`);
  }

  return await res.text();
}

function extractImageUrlsFromHtml(html, pageUrl) {
  const urls = [];

  for (const match of html.matchAll(/<img[^>]+>/gi)) {
    const tag = match[0];

    for (const attr of ['src', 'data-src', 'data-original', 'data-lazy-src']) {
      const attrMatch = tag.match(new RegExp(`${attr}=["']([^"']+)["']`, 'i'));
      if (attrMatch) {
        const url = normalizeUrl(attrMatch[1], pageUrl);
        if (url) urls.push(url);
      }
    }
  }

  for (const match of html.matchAll(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["'][^>]*>/gi)) {
    const url = normalizeUrl(match[1], pageUrl);
    if (url) urls.push(url);
  }

  return unique(urls).filter((url) =>
    /tistory|daumcdn|kakaocdn|blog\.kakaocdn/i.test(url)
  );
}

function getExt(url, contentType = '') {
  try {
    const ext = path.extname(new URL(url).pathname).toLowerCase();

    if (['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'].includes(ext)) {
      return ext === '.jpeg' ? '.jpg' : ext;
    }
  } catch {
    // ignore
  }

  const type = contentType.split(';')[0].toLowerCase();

  if (type === 'image/jpeg') return '.jpg';
  if (type === 'image/png') return '.png';
  if (type === 'image/gif') return '.gif';
  if (type === 'image/webp') return '.webp';
  if (type === 'image/bmp') return '.bmp';

  return '.jpg';
}

async function downloadImage(url, savePath, referer) {
  const res = await fetch(url, {
    headers: {
      'user-agent': 'Mozilla/5.0',
      accept: 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
      referer: referer ?? 'https://www.tistory.com/',
    },
  });

  if (!res.ok) {
    throw new Error(`이미지 요청 실패: ${res.status} ${res.statusText}`);
  }

  const contentType = res.headers.get('content-type') ?? '';
  const buffer = Buffer.from(await res.arrayBuffer());

  await fs.writeFile(savePath, buffer);

  return {
    contentType,
    size: buffer.length,
  };
}

async function main() {
  await fs.mkdir(OUTPUT_DIR, { recursive: true });

  const files = await fs.readdir(ORIGINAL_MD_DIR);

  const mdFiles = files
    .filter((file) => file.endsWith('.md'))
    .filter((file) => {
      const no = getPostNo(file);
      return no !== null && no >= MIN_NO;
    })
    .sort((a, b) => getPostNo(a) - getPostNo(b));

  console.log(`대상 파일 수: ${mdFiles.length}`);
  console.log(`저장 위치: ${OUTPUT_DIR}`);

  for (const mdFile of mdFiles) {
    const no = getPostNo(mdFile);
    const mdPath = path.join(ORIGINAL_MD_DIR, mdFile);
    const md = await fs.readFile(mdPath, 'utf8');
    const sourceUrl = extractSourceUrl(md);

    const titlePart = sanitizeName(path.basename(mdFile, '.md').replace(/^\d+_/, ''));
    const imageDir = path.join(
      OUTPUT_DIR,
      `${String(no).padStart(2, '0')}_${titlePart}_images`
    );

    let imageUrls = extractImageUrlsFromMd(md);

    if (imageUrls.length === 0 && sourceUrl) {
      console.log(`\n[${no}] MD 이미지 URL 없음. 티스토리 원문에서 추출합니다.`);
      console.log(sourceUrl);

      try {
        const html = await fetchHtml(sourceUrl);
        imageUrls = extractImageUrlsFromHtml(html, sourceUrl);
      } catch (error) {
        console.log(`원문 HTML 가져오기 실패: ${error.message}`);
      }
    }

    imageUrls = unique(imageUrls);

    console.log(`\n[${no}] ${mdFile}`);
    console.log(`이미지 수: ${imageUrls.length}`);

    if (imageUrls.length === 0) {
      console.log('이미지 없음. 건너뜀.');
      continue;
    }

    await fs.mkdir(imageDir, { recursive: true });

    const result = {
      md_file: mdFile,
      source_url: sourceUrl,
      image_count: imageUrls.length,
      images: [],
    };

    for (let i = 0; i < imageUrls.length; i += 1) {
      const imageUrl = imageUrls[i];
      const index = String(i + 1).padStart(2, '0');

      try {
        const tempPath = path.join(imageDir, `${index}.download`);
        const meta = await downloadImage(imageUrl, tempPath, sourceUrl);
        const ext = getExt(imageUrl, meta.contentType);
        const finalPath = path.join(imageDir, `${index}${ext}`);

        await fs.rename(tempPath, finalPath);

        result.images.push({
          index: i + 1,
          url: imageUrl,
          file: path.basename(finalPath),
          size: meta.size,
          content_type: meta.contentType,
        });

        console.log(`저장 완료: ${index}${ext}`);
      } catch (error) {
        result.images.push({
          index: i + 1,
          url: imageUrl,
          error: error.message,
        });

        console.log(`실패: ${imageUrl}`);
        console.log(`이유: ${error.message}`);
      }
    }

    await fs.writeFile(
      path.join(imageDir, '_images.json'),
      JSON.stringify(result, null, 2),
      'utf8'
    );
  }

  console.log('\n완료');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});