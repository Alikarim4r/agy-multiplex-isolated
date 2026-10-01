.PHONY: help install check test build-image doctor

help:
	@printf '%s\n' 'make install      Install local command links' \
	                 'make check        Static release checks' \
	                 'make test         Run unit tests' \
	                 'make build-image  Build pinned agy Docker image' \
	                 'make doctor       Check local runtime'

install:
	./install.sh

check:
	python3 -m compileall -q multiplex.py routing.py ui tests tools
	python3 tools/check_release.py

test:
	python3 -m unittest discover -s tests -v

build-image:
	./bin/agy-multiplex-isolated build-image

doctor:
	./bin/agy-multiplex-isolated doctor
