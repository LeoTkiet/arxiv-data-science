"""Deterministic synthetic Atom responses, not real paper metadata."""

from io import BytesIO
from urllib.parse import parse_qs, urlsplit


def feed(identifier="2301.00001", version=2):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry><id>http://arxiv.org/abs/{identifier}v{version}</id>
      <title> Test   Paper </title><summary>Test abstract</summary>
      <author><name>Alice Example</name></author><published>2023-01-01T00:00:00Z</published>
      <updated>2023-01-0{version}T00:00:00Z</updated>
      <category term="cs.AI"/><arxiv:journal_ref>Test Journal</arxiv:journal_ref></entry></feed>'''.encode()


EMPTY = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'


class FixtureOpener:
    def __init__(self, mapping=None):
        self.mapping = mapping or {"2301.00001": feed(), "2301.00001v1": feed(version=1)}
        self.calls = []

    def __call__(self, request, timeout):
        identifier = parse_qs(urlsplit(request.full_url).query)["id_list"][0]
        self.calls.append(identifier)
        result = self.mapping.get(identifier, EMPTY)
        if isinstance(result, BaseException):
            raise result
        return BytesIO(result)


def fixture_client(mapping=None):
    from arxiv_pipeline.crawling.discovery import ArxivClient
    opener = FixtureOpener(mapping)
    client = ArxivClient(opener=opener, sleep=lambda _: None)
    return client, opener
