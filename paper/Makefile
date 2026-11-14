# Build the paper PDF into paper/build/. Requires a TeX Live installation
# with the acmart class (texlive-publishers) and its fonts
# (libertine, inconsolata, newtx; texlive-fonts-extra on Debian/Ubuntu).
.PHONY: pdf clean

pdf:
	latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build antfly.tex

clean:
	rm -rf build
