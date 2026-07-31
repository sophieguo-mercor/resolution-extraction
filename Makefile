.PHONY: help install dryrun sample pilot full collect aggregate clean clean-all

XLS ?= data/Time_entries_Mercor_v3_workflows_by_company__1_.xls
WF  ?= Password Reset
N   ?= 20

help:
	@echo "Resolution-step extraction (Anthropic Message Batches API)"
	@echo ""
	@echo "  make install     install dependencies"
	@echo "  make dryrun      print the prompt + request estimate, no API spend"
	@echo "  make sample      extract N=$(N) tickets from WF=\"$(WF)\""
	@echo "  make pilot       extract 60 tickets from EVERY workflow (~4.4k requests)"
	@echo "  make full        extract the entire corpus (~10.4k requests)"
	@echo "  make collect     reconnect to an in-flight batch and write its results"
	@echo "  make aggregate   compute variance metrics from results/raw/"
	@echo "  make clean       delete computed metrics, keep raw extractions"
	@echo "  make clean-all   delete everything in results/"
	@echo ""
	@echo "Each command submits ONE batch, waits for it, then writes results."
	@echo "Ctrl-C while it waits is safe: re-run (or 'make collect') to reconnect."
	@echo ""
	@echo "Overrides:  make sample WF=\"MFA Reset / Setup / Troubleshooting\" N=40"

install:
	pip install -r requirements.txt

dryrun:
	python extract.py --xls "$(XLS)" --workflows "$(WF)" --sample $(N) --dry-run

sample:
	python extract.py --xls "$(XLS)" --workflows "$(WF)" --sample $(N)

pilot:
	python extract.py --xls "$(XLS)" --sample 60

full:
	python extract.py --xls "$(XLS)"

collect:
	python extract.py --collect

aggregate:
	python aggregate.py

clean:
	rm -f results/scorecard.json results/scorecard.csv results/patterns.json

clean-all:
	rm -f results/scorecard.json results/scorecard.csv results/patterns.json
	rm -f results/batch_manifest.json
	rm -f results/raw/*.jsonl
