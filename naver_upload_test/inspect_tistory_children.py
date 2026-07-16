from urllib.request import Request, urlopen
from lxml import html

for post_id in [2, 30, 35]:
    url = f'https://alliwantischicken.tistory.com/{post_id}'
    raw = urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20).read().decode('utf-8', 'replace')
    doc = html.fromstring(raw)
    node = doc.xpath('//*[@id="article-view"]') or doc.xpath('//*[contains(@class,"tt_article_useless_p_margin")]')
    print('\n====', post_id, doc.xpath('string(//title)')[:100], '====')
    if not node:
        continue
    root = node[0]
    for i, child in enumerate(root[:30]):
        txt = ' '.join(t.strip() for t in child.xpath('.//text()') if t.strip())[:120]
        imgs = child.xpath('.//img/@src')
        print(i, child.tag, child.get('class'), 'TXT=', txt, 'IMGS=', len(imgs))
