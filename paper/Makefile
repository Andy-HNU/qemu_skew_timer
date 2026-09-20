PYTHON ?= python3
.PHONY: design examples check paper
design:
	$(PYTHON) scripts/architecture.py --csv figures/architecture.csv --out figures/design
examples:
	$(PYTHON) scripts/make_examples.py
	$(PYTHON) scripts/plot.py --all-examples
check:
	$(PYTHON) scripts/test_pipeline.py
paper: design
	mkdir -p build
	pdflatex -interaction=nonstopmode -halt-on-error -output-directory=build paper.tex
	cd build && BIBINPUTS=..: bibtex paper
	pdflatex -interaction=nonstopmode -halt-on-error -output-directory=build paper.tex
	pdflatex -interaction=nonstopmode -halt-on-error -output-directory=build paper.tex
