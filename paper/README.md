# Paper source

`antfly.tex` is the paper, "Antfly: An Inference-Native Polymorphic Database",
in the ACM `acmart` format (`sigconf`, non-ACM preprint). `references.bib`
holds the bibliography and `figures/` the TikZ figures.

```sh
cd paper
make pdf      # writes build/antfly.pdf
make release  # rebuilds and refreshes the committed antfly.pdf
```

`antfly.pdf` is the committed build of the current source; refresh it with
`make release` whenever the source changes.

Building needs TeX Live with `acmart` and its fonts (Libertine, Inconsolata,
newtx). Without those fonts the class falls back to Computer Modern and still
builds.

The evaluation section points to the protocols in [`../evaluations`](../evaluations).
Keep the paper's experiment descriptions consistent with the frozen protocol
for each experiment, and never report results from a draft protocol.
