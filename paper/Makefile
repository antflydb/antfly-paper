# Build the paper PDF into paper/build/. Requires a TeX Live installation
# with the acmart class (texlive-publishers) and its fonts
# (libertine, inconsolata, newtx; texlive-fonts-extra on Debian/Ubuntu).
# `make release` refreshes the committed paper/antfly.pdf from a clean build.
.PHONY: pdf release clean

pdf:
	latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build antfly.tex

release: clean pdf
	cp build/antfly.pdf antfly.pdf

clean:
	rm -rf build
