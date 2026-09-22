"""B09/B10 tiny RSS/Atom/RDF fixtures with no network access."""
from bs_podcasts.feeds.parser import parse_feed


def main():
    for mime in ('application/octet-stream','binary/octet-stream','application/ogg','audio/mpeg; charset=binary'):
        enclosure=f'<enclosure url="https://m.invalid/a" type="{mime}" length="100000"/>'
        rss=parse_feed(f'<rss><channel><title>RSS</title><item><title>E</title>{enclosure}</item></channel></rss>'.encode())
        atom=parse_feed(f'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><entry><title>E</title><link rel="enclosure" href="https://m.invalid/a" type="{mime}"/></entry></feed>'.encode())
        assert len(rss.episodes)==len(atom.episodes)==1
        assert rss.skipped_video==atom.skipped_video==0
    for mime, video in [('application/pdf',0),('video/mp4',1)]:
        feed=parse_feed(f'<rss><channel><title>T</title><item><enclosure url="https://m.invalid/a" type="{mime}"/></item></channel></rss>'.encode())
        assert not feed.episodes and feed.skipped_video==video
    rdf=parse_feed(b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/" xmlns:enc="http://purl.oclc.org/net/rss_2.0/enc#" xml:base="https://m.invalid/">
        <channel rdf:about="feed"><title>RDF</title></channel>
        <item rdf:about="episode"><title>Sibling</title><enc:enclosure rdf:resource="e.ogg" enc:type="application/ogg" enc:length="100000"/></item>
        </rdf:RDF>''')
    assert len(rdf.episodes)==1 and rdf.episodes[0].media_url=='https://m.invalid/e.ogg'
    assert rdf.episodes[0].external_id=='https://m.invalid/episode'
    assert rdf.episodes[0].enclosure_bytes==100000
    print('B09/B10: PASS RSS/Atom binary audio, honest video counts, RDF sibling/namespace/base URL/identity')


if __name__=='__main__': main()
