# T3-05 pilot diagnosis (25 November 2026)

Source: `output/diagnostic/first_pass/first_pass_manifest.json` and 60 pilot
publication bundles on Drive. This is a diagnostic sample, not the Auto Pool
handoff.

- 219 of 250 selected publications were ready. 86 had full heuristic coverage;
  63 of those had no MS1 reference candidates. Only 23 of 156 publications
  with references had full coverage. There were 294 uncertain BibTeX entries.
- Of those 294, 269 were `abstain_ambiguous`, 13 had a title/year conflict,
  eight had a title/author conflict, and four had ambiguous candidates.
- In 60 inspected bundles, 115 uncertain entries had a BibTeX title and
  authors; none had missing reference titles. The main obstacle in this sample
  was related but different titles, not empty metadata. Similar titles alone
  must not become `unmatched` or `matched` automatically.
- Three concrete true matches scored between 0.84 and 0.88: typo in
  “Explicit sections” (`math/0502017`), typo in “Tessellations”
  (`1308.5550`), and alternate mathematical notation in the Dawson title
  (`quant-ph/0408129`). The `v1.1.0` heuristic lowers the high-title threshold
  from 0.88 to 0.84; author overlap, year consistency and candidate margin
  checks still apply. A related Elma title at 0.8153 remains uncertain; a
  del Pezzo title with a five-year date difference remains uncertain.

Rerun Colab diagnostic cell 1 to pull the new branch commit, then cell 4 on
the existing prepared pilot. Cell 3 need not rerun if its prepared manifest
and bundles remain on Drive. `run_first_pass` invalidates cached per-paper
labels when the policy changes. Inspect `uncertain_details.json` for BibTeX
and top-three candidate context, and `spot_check_sample.json` for a
deterministic sample of up to three accepted rows per member and rule. Review
the accepted sample manually before estimating precision. The quota is 1,630
fully covered publications; neither 86 pilot candidates nor this small rule
change establishes that quota.
