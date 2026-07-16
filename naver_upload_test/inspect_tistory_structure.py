from urllib.request import Request, urlopen
from lxml import html

url = 'https://alliwantischicken.tistory.com/2'
req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
raw = urlopen(req, timeout=20).read().decode('utf-8', 'replace')
doc = html.fromstring(raw)
print('page title:', doc.xpath('string(//title)')[:160])
print('h1:', doc.xpath('string(//h1[1])')[:160])
for xp in [
    '//article',
    '//*[contains(@class,"article")]',
    '//*[contains(@class,"entry")]',
    '//*[contains(@class,"contents")]',
    '//*[contains(@class,"tt_article_useless_p_margin")]',
    '//*[@id="content"]',
    '//*[@id="article-view"]',
]:
    nodes = doc.xpath(xp)
    print('\nXP:', xp, 'count=', len(nodes), 'tags=', [n.tag for n in nodes[:3]])
    if nodes:
        txt = ' '.join(t.strip() for t in nodes[0].xpath('.//text()') if t.strip())
        print(txt[:1000])
