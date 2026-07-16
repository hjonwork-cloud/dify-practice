import re
import json
import time
import html as html_lib
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from lxml import html, etree

ROOT = Path(r'e:\git-copilot\dify-practice')
TITLE_TABLE = ROOT / 'docs' / 'naver_title_suggestions_20260606.md'
OUT_DIR = ROOT / 'docs' / 'naver_migration_original_posts_20260606'
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 네이버 이전 대상에서 제외할 운영성 글.
# 사용자가 "티스토리운영 제외"라고 했으므로 말머리뿐 아니라 티스토리 운영/수익화 계열도 제외한다.
EXCLUDE_KEYWORDS = [
    '[티스토리운영]',
    '네이버서치어드바이저',
    '구글서치콘솔',
    '티스토리 애드센스',
]

TAG_HINTS = {
    2: ['CJ제주맥주', '제주맥주콜라보', '편의점맥주', '맥주후기', '팝업스토어'],
    3: ['웨딩밴드', '코이누르', '결혼준비', '예물반지'],
    4: ['판교맛집', '스포키', '판교테크노밸리', '건강식'],
    5: ['노량진수산시장', '킹크랩', '킹크랩가격', '수산시장'],
    6: ['창원맛집', '진해맛집', '김해횟집', '대구탕'],
    7: ['노량진수산시장', '킹크랩', '새벽시장', '원소주'],
    8: ['마산맛집', '마산국밥', '남양돼지국밥', '고속터미널맛집'],
    12: ['마산맛집', '신태극갈비', '갈비맛집', '마산갈비'],
    13: ['황금향', '귤까는법', '생활꿀팁', '과일손질'],
    14: ['평촌맛집', '안양장군집', '특수부위', '평촌고기집'],
    15: ['캠핑용품', '코베아', '티타늄랜턴', '캠핑랜턴'],
    16: ['판교맛집', '안안', '베트남음식', '판교쌀국수'],
    17: ['라프로익10년', '위스키시음기', '피트위스키', '위스키추천'],
    18: ['제주맥주', '삐아프', '초콜릿맥주', '한정판맥주'],
    19: ['프랜차이즈박람회', '창업박람회', '박람회후기', '주차팁'],
    20: ['선정릉맛집', '롤리폴리꼬또', '오뚜기카레', '카레맛집'],
    21: ['연남동맛집', '굴짬뽕', '연남동짬뽕', '중식맛집'],
    23: ['양양캠핑장', '바다캠핑', '비바코로마', '리빙쉘텐트'],
    25: ['제로페이', '농할상품권', '상품권할인', '장보기절약'],
    26: ['강원도겨울여행', '원주', '정선맛집', '겨울여행'],
    27: ['강원도겨울여행', '하이원리조트', '하이원', '겨울여행'],
    28: ['평창여행', '강원도겨울여행', '평창가볼만한곳', '겨울여행'],
    29: ['양재천벚꽃', '벚꽃놀이', '서울벚꽃명소', '봄나들이'],
    30: ['금귤정과', '금귤레시피', '금귤손질', '정과만들기'],
    31: ['갤럭시Z플립5', 'Z플립5케이스', '풀커버케이스', '제품후기'],
    32: ['캠핑후기', '첫캠핑', '초보캠핑', '캠핑준비물'],
    33: ['오우드', '원목침대프레임', '혼수침대', '가구후기'],
    34: ['노지캠핑', '캠핑후기', '캠핑장소', '초보캠핑'],
    35: ['차박캠핑', '솔잎향캠핑파크', '캠핑장차박', '차박입문'],
}

VOID_TEXT_PATTERNS = [
    'adsbygoogle',
    'window.ReactionButtonType',
    'window.ReactionApiUrl',
    '좋아요',
    '공유하기',
    '게시글 관리',
    '구독하기',
]


def slugify_title(title: str, post_id: int) -> str:
    cleaned = re.sub(r'\[[^\]]+\]', '', title)
    cleaned = re.sub(r'[^0-9A-Za-z가-힣]+', '_', cleaned).strip('_')
    cleaned = re.sub(r'_+', '_', cleaned)
    if len(cleaned) > 44:
        cleaned = cleaned[:44].rstrip('_')
    return f'{post_id:02d}_{cleaned or "post"}.md'


def parse_title_table():
    rows = []
    for line in TITLE_TABLE.read_text(encoding='utf-8').splitlines():
        if not line.startswith('|') or line.startswith('|---') or '기존 제목' in line:
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 6 or not cells[0].isdigit():
            continue
        url = cells[2]
        m = re.search(r'/([0-9]+)(?:\?|$)', url)
        if not m:
            continue
        post_id = int(m.group(1))
        rows.append({
            'no': int(cells[0]),
            'post_id': post_id,
            'old_title': cells[1],
            'url': url,
            'title1': cells[3],
            'title2': cells[4],
            'title3': cells[5],
        })
    return rows


def fetch_doc(url: str):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    raw = urlopen(req, timeout=30).read().decode('utf-8', 'replace')
    return html.fromstring(raw)


def text_content(node):
    text = ''.join(node.itertext())
    text = html_lib.unescape(text)
    text = re.sub(r'\xa0', ' ', text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return text.strip()


def should_skip_text(text: str) -> bool:
    if not text:
        return True
    return any(pat in text for pat in VOID_TEXT_PATTERNS)


def img_src(img):
    candidates = [
        img.get('src'),
        img.get('data-src'),
        img.get('data-original'),
        img.get('data-filename'),
    ]
    for src in candidates:
        if src:
            return src
    return ''


def md_escape(text: str) -> str:
    return text.replace('\u200b', '').strip()


def convert_inline(node) -> str:
    if isinstance(node, etree._Comment):
        return ''
    if not isinstance(node.tag, str):
        return ''

    tag = node.tag.lower()
    if tag in {'script', 'style', 'iframe', 'ins'}:
        return ''
    if tag == 'br':
        return '\n'
    if tag == 'img':
        return ''
    if tag == 'a':
        label = text_content(node)
        href = node.get('href') or ''
        if label and href and not href.startswith('#'):
            return f'[{label}]({href})'
        return label

    parts = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(convert_inline(child))
        if child.tail:
            parts.append(child.tail)
    result = ''.join(parts)
    result = html_lib.unescape(result)
    result = re.sub(r'\xa0', ' ', result)
    result = re.sub(r'[ \t\r\f\v]+', ' ', result)
    result = re.sub(r' *\n *', '\n', result)
    return result.strip()


def convert_block(node, image_counter) -> list[str]:
    if isinstance(node, etree._Comment) or not isinstance(node.tag, str):
        return []
    tag = node.tag.lower()
    cls = node.get('class') or ''

    if tag in {'script', 'style', 'iframe', 'ins'}:
        return []
    if 'adsbygoogle' in cls or 'container_postbtn' in cls or 'another_category' in cls:
        return []

    # 이미지/figure는 사진 자리 표시를 남긴다. 실제 이미지는 사용자가 직접 이동.
    imgs = node.xpath('.//img') if tag in {'figure', 'p', 'div', 'span'} else []
    if tag == 'img':
        imgs = [node]
    if imgs and not text_content(node).strip():
        out = []
        for img in imgs:
            image_counter[0] += 1
            alt = (img.get('alt') or '').strip()
            src = img_src(img)
            desc = alt or '원문 이미지'
            out.append(f'> [사진 {image_counter[0]} 넣을 자리: {desc}]')
            if src:
                out.append(f'> 원문 이미지 URL: {src}')
        return out

    if tag in {'h1', 'h2', 'h3', 'h4'}:
        level = {'h1': 2, 'h2': 2, 'h3': 3, 'h4': 4}[tag]
        txt = md_escape(convert_inline(node))
        return [f'{"#" * level} {txt}'] if txt and not should_skip_text(txt) else []

    if tag in {'ul', 'ol'}:
        lines = []
        for idx, li in enumerate(node.xpath('./li'), 1):
            txt = md_escape(convert_inline(li))
            if not txt:
                continue
            prefix = f'{idx}. ' if tag == 'ol' else '- '
            lines.append(prefix + txt.replace('\n', '\n  '))
        return lines

    if tag == 'blockquote':
        txt = md_escape(convert_inline(node))
        return ['> ' + line for line in txt.splitlines() if line.strip()] if txt else []

    # 본문 div는 자식 블록을 재귀 처리하되, leaf div는 문단으로 처리한다.
    block_children = [c for c in node if isinstance(c.tag, str) and c.tag.lower() in {'p','div','figure','h1','h2','h3','h4','ul','ol','blockquote','table'}]
    if tag in {'div', 'section'} and block_children:
        lines = []
        # node.text가 있는 경우 먼저 문단화
        if node.text and node.text.strip():
            txt = md_escape(node.text)
            if txt and not should_skip_text(txt):
                lines.append(txt)
        for child in node:
            lines.extend(convert_block(child, image_counter))
        return lines

    if tag == 'table':
        rows = []
        for tr in node.xpath('.//tr'):
            cells = [md_escape(text_content(td)) for td in tr.xpath('./th|./td')]
            if cells:
                rows.append(cells)
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [''] * (width - len(r)) for r in rows]
        md = ['| ' + ' | '.join(rows[0]) + ' |', '| ' + ' | '.join(['---'] * width) + ' |']
        for r in rows[1:]:
            md.append('| ' + ' | '.join(r) + ' |')
        return md

    txt = md_escape(convert_inline(node))
    if should_skip_text(txt):
        return []

    lines = []
    if imgs:
        # 텍스트와 이미지가 섞인 문단은 텍스트 먼저, 사진 자리 뒤에 배치
        if txt:
            lines.append(txt)
        for img in imgs:
            image_counter[0] += 1
            alt = (img.get('alt') or '').strip() or '원문 이미지'
            src = img_src(img)
            lines.append(f'> [사진 {image_counter[0]} 넣을 자리: {alt}]')
            if src:
                lines.append(f'> 원문 이미지 URL: {src}')
        return lines

    return [txt]


def extract_article_node(doc):
    candidates = doc.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " tt_article_useless_p_margin ")]')
    if candidates:
        return candidates[0]
    candidates = doc.xpath('//*[@id="article-view"]')
    if candidates:
        return candidates[0]
    return None


def build_markdown(row):
    doc = fetch_doc(row['url'])
    article = extract_article_node(doc)
    if article is None:
        raise RuntimeError(f'article not found: {row["url"]}')

    image_counter = [0]
    body_lines = []
    for child in article:
        body_lines.extend(convert_block(child, image_counter))

    # 빈 줄 정리
    cleaned = []
    for line in body_lines:
        line = line.rstrip()
        if not line:
            continue
        if cleaned and cleaned[-1] == line:
            continue
        cleaned.append(line)

    tags = TAG_HINTS.get(row['post_id'], [])
    frontmatter = [
        '---',
        'source_url: ' + row['url'],
        'original_title: "' + row['old_title'].replace('"', '\\"') + '"',
        'suggested_titles:',
        '  - "' + row['title1'].replace('"', '\\"') + '"',
        '  - "' + row['title2'].replace('"', '\\"') + '"',
        '  - "' + row['title3'].replace('"', '\\"') + '"',
        'tags: [' + ', '.join(tags) + ']',
        '---',
        '',
        '# ' + row['title1'],
        '',
        '> 사진 안내: 아래 [사진 N 넣을 자리] 위치에 티스토리 원문 사진을 직접 옮긴 뒤 안내 문구/URL은 삭제하세요.',
        '',
    ]
    return '\n'.join(frontmatter + ['\n\n'.join(cleaned), ''])


def main():
    rows = parse_title_table()
    included = []
    excluded = []
    for row in rows:
        if any(keyword in row['old_title'] for keyword in EXCLUDE_KEYWORDS):
            excluded.append(row)
        else:
            included.append(row)

    manifest = {
        'generated_at': '2026-06-06',
        'source_table': str(TITLE_TABLE),
        'exclude_keywords': EXCLUDE_KEYWORDS,
        'included_count': len(included),
        'excluded': [{'post_id': r['post_id'], 'title': r['old_title'], 'url': r['url']} for r in excluded],
        'files': [],
    }

    for idx, row in enumerate(included, 1):
        print(f'[{idx}/{len(included)}] {row["post_id"]} {row["old_title"]}')
        md = build_markdown(row)
        fname = slugify_title(row['title1'], row['post_id'])
        path = OUT_DIR / fname
        path.write_text(md, encoding='utf-8')
        manifest['files'].append({
            'post_id': row['post_id'],
            'old_title': row['old_title'],
            'suggested_title': row['title1'],
            'url': row['url'],
            'file': fname,
        })
        time.sleep(0.25)

    (OUT_DIR / '_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DONE', OUT_DIR, 'included=', len(included), 'excluded=', len(excluded))


if __name__ == '__main__':
    main()
