# arxiv-data-science

Project thu thap va xu ly arXiv corpus theo ba milestone.

- src/arxiv_pipeline/common: config, schemas, logging, utilities.
- src/arxiv_pipeline/crawling: MS1 Discovery, download, extraction, metadata, references.
- src/arxiv_pipeline/parsing: MS2 parsing va normalization.
- src/arxiv_pipeline/matching: MS3 matching va evaluation.
- src/arxiv_pipeline/export: export va validation.
- src/arxiv_pipeline/pipeline: dieu phoi cac milestone.
- scripts: entry points; tests: kiem thu theo module.

Python 3.10+. Cai package: `python -m pip install -r requirements.txt`.
Hop dong du lieu baseline v1: [docs/CONTRACTS.md](docs/CONTRACTS.md).
Hop dong mo ta giao dien va hanh vi du kien de nhom tich hop.
Discovery, CLI va pipeline se duoc ban giao trong commit tiep theo.
Khong commit corpus hoac du lieu tai ve.
