"""Synthetic offline transport fixtures for the TV5 demo; never a final benchmark."""
import argparse
from io import BytesIO
from pathlib import Path
import sys
import tarfile
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import requests
from arxiv_pipeline.crawling.discovery import ArxivClient
from arxiv_pipeline.crawling.downloader import ArxivDownloader
from arxiv_pipeline.crawling.s2.s2_source import Outcome
from arxiv_pipeline.main import main


def atom(identifier, version):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>https://arxiv.org/abs/{identifier}v{version}</id><title>SYNTHETIC DEMO {identifier}</title>
    <summary>Synthetic fixture, not a scraped publication.</summary><author><name>Demo Author</name></author>
    <published>2023-01-01T00:00:00Z</published><updated>2023-01-0{version}T00:00:00Z</updated>
    <category term="cs.AI"/></entry></feed>'''.encode()


def opener(request, timeout):
    identifier = parse_qs(urlsplit(request.full_url).query)['id_list'][0]
    mapping = {'2301.00001': atom('2301.00001', 2), '2301.00001v1': atom('2301.00001', 1),
               '2301.00002': atom('2301.00002', 1)}
    return BytesIO(mapping.get(identifier, b'<feed xmlns="http://www.w3.org/2005/Atom"/>'))


class FixtureSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        if '2301.00002' in url:
            payload = b'%PDF-1.7 SYNTHETIC'
            response.headers['Content-Type'] = 'application/pdf'
        else:
            buffer = BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                for name, data in {
                    'main.tex': b'\\documentclass{article}\n\\begin{document}Demo.\\includegraphics{figure}\n\\end{document}',
                    'refs.bib': b'@article{demo,title={Synthetic reference},year={2020}}',
                    'figure.png': b'FAKE_IMAGE' * 1024,
                }.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, BytesIO(data))
            payload = buffer.getvalue()
            response.headers['Content-Type'] = 'application/x-eprint-tar'
        response.headers['Content-Length'] = str(len(payload))
        response.raw = BytesIO(payload)
        return response


def reference_fetch(self, identifiers):
    for identifier in identifiers:
        self.n_requests += 1
        if identifier == '2301.00002':
            yield Outcome(identifier, 'not_found')
        else:
            yield Outcome(identifier, 'ok', s2_paper_id='synthetic', references=[
                dict(arxiv_id='2001.00001', title='Synthetic reference', authors=['Demo'],
                     submission_date='2020-01-01', s2_id='demo1'),
                dict(arxiv_id=None, title='Synthetic book', authors=['Demo'],
                     submission_date='2019', s2_id='demo2')])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timings', required=True)
    args = parser.parse_args()
    with patch('arxiv_pipeline.crawling.downloader._DOWNLOADER', ArxivDownloader(session=FixtureSession())), \
            patch('arxiv_pipeline.crawling.s2.s2_source.S2Source.fetch', new=reference_fetch):
        # Includes one deliberately missing discovery result to exercise partial failure.
        raise SystemExit(main(['--ids', '2301.00001', '2301.00002', '2301.00003', '--stage', 'crawl',
                               '--output', args.output, '--timings', args.timings],
                              client=ArxivClient(opener=opener, sleep=lambda _: None)))
