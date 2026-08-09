---
id: YYYY-MM-DD-short-slug
date: YYYY-MM-DD
status: open          # open | investigating | confirmed | rejected
source: performance-analyst   # who/what generated this
n_at_time: 0           # settled trade count summary_report() reported when this was written
---

## Observation

What in `research/metrics.py::summary_report()` (or a `findings/` snapshot) prompted
this — cite actual numbers, not vibes. Include the sample size.

## Hypothesis

One or two sentences, falsifiable. Example shape:

> Trades with 3-5% estimated edge have materially lower realized ROI than trades with
> >5% edge, suggesting the edge estimate may be miscalibrated in that band.

## Suggested experiment

What a Quant Research Agent should actually go compute to test this — which function
in `research/metrics.py`, which slice of data, what would count as confirming vs.
refuting the hypothesis.

## Known caveats going in

Anything that should make a Skeptic (or the Quant Research Agent itself) skip straight
to "insufficient sample size" or "already answered" — e.g. current settled-trade count,
whether this overlaps a previously-rejected idea in `failed_ideas/`.
