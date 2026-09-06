PYTHON ?= python3

.PHONY: prepare enrich-plan enrich classify-plan classify build-web serve test

prepare:
	$(PYTHON) scripts/prepare_data.py

enrich-plan:
	$(PYTHON) scripts/enrich_motions.py

enrich:
	$(PYTHON) scripts/enrich_motions.py --execute

classify-plan:
	$(PYTHON) scripts/classify_with_codex.py

classify:
	$(PYTHON) scripts/classify_with_codex.py --execute

build-web:
	$(PYTHON) scripts/build_site_data.py

serve:
	$(PYTHON) scripts/serve.py

test:
	$(PYTHON) -m unittest discover -s tests -v
